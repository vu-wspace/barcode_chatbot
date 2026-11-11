"""POST /chat trả về SSE (text/event-stream). Mỗi event: `event: <tên>` + `data: <json>`.

    meta   {thread_id, request_id}      luôn là event đầu tiên
    tool   {name}                       agent bắt đầu gọi 1 tool
    token  {text}                       1 đoạn câu trả lời (chỉ để hiển thị dần)
    done   {answer, tools, thread_id}   câu trả lời cuối, CHÍNH THỨC (thay toàn bộ text đã stream)
    error  {status, detail}             lượt thất bại; status theo nghĩa HTTP (409, 429, 502, 503, 504...)

Giữa các event có comment `: ping` mỗi 15s để proxy/trình duyệt biết kết nối còn sống.
Client ngắt kết nối -> lượt đang chạy bị hủy. Gửi lại cùng request_id -> trả kết quả đã có, không chạy lại.
"""
import asyncio
import json
import logging
import os
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager

import groq
import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from langchain_core.exceptions import ModelError, ModelRateLimitError
from langchain_core.messages import RemoveMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from openrouter import errors as openrouter_errors
from pydantic import BaseModel, Field

from packing_agent import tools
from packing_agent.agent import build_agent

log = logging.getLogger('uvicorn.error')

TURN_DEADLINE = 120       # giây cho cả lượt (mọi lần gọi model, retry, fallback, tool); FE chờ tối đa 170s
LOCK_WAIT = 20            # chờ lượt trước của cùng hội thoại xong
MAX_WAITERS = 3           # số request trên 1 hội thoại, tính cả lượt đang chạy (1 chạy + 2 chờ)
MAX_ACTIVE = 8            # số lượt chạy đồng thời toàn server
SLOT_WAIT = 10
HEARTBEAT = 15
KEEP_STATE_TURNS = 10     # state chỉ giữ ngần này lượt hỏi gần nhất (model còn chỉ thấy 4, xem history.py)
HISTORY_DAYS = 7          # hội thoại không dùng quá ngần này ngày bị xóa (kiểm tra mỗi giờ)
MAX_CACHED_RESULTS = 1000


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)  # FE gửi kèm danh sách mã đã quét
    # UUID do client tạo khi mở trang (để không mất hội thoại nếu lượt đầu lỗi) hoặc do server cấp.
    # shortcut: chưa có đăng nhập nên chưa kiểm tra chủ thread; khi có user thì gắn thread_id với user.
    thread_id: uuid.UUID | None = None
    # ID của lượt: thử lại với cùng request_id không chạy agent lần 2
    request_id: uuid.UUID | None = None


class TurnError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def is_rate_limited(e: Exception) -> bool:
    # Gemini (langchain-google-genai) ném ModelRateLimitError chung của langchain_core.
    # Groq ném lỗi riêng: 429, hoặc 413 "Request too large" kèm code rate_limit_exceeded.
    # OpenRouter (SDK openrouter) cũng ném lỗi riêng, không kế thừa ModelError.
    if isinstance(e, (ModelRateLimitError, groq.RateLimitError, openrouter_errors.TooManyRequestsResponseError)):
        return True
    if not isinstance(e, groq.APIError):
        return False
    body = e.body if isinstance(e.body, dict) else {}
    err = body.get('error')
    return (err if isinstance(err, dict) else body).get('code') == 'rate_limit_exceeded'


def to_turn_error(e: Exception) -> TurnError:
    if isinstance(e, TurnError):
        return e
    if isinstance(e, TimeoutError):
        return TurnError(504, 'Trợ lý trả lời quá lâu, anh/chị vui lòng thử lại.')
    if is_rate_limited(e):
        log.warning('LLM rate limit: %s', e)  # ghi loại giới hạn (token/phút, token/ngày...) để biết chờ hay nâng gói
        return TurnError(429, 'Hệ thống đang quá tải, anh/chị vui lòng thử lại sau ít phút.')
    if isinstance(e, (groq.APIError, openrouter_errors.OpenRouterError, openrouter_errors.NoResponseError,
                      ModelError, GraphRecursionError)):
        log.exception('Lỗi khi chạy agent')
        return TurnError(502, f'Không tạo được câu trả lời ({type(e).__name__}).')
    if isinstance(e, psycopg.Error):
        log.exception('Lỗi DB lịch sử hội thoại')
        return TurnError(503, 'Không lưu/đọc được lịch sử hội thoại, anh/chị vui lòng thử lại.')
    log.exception('Lỗi không mong đợi')
    return TurnError(500, f'Lỗi hệ thống ({type(e).__name__}).')


# --- tuần tự hóa theo hội thoại ---
# Trong process: asyncio.Lock (không chiếm thread khi chờ). Khi có Postgres còn khóa advisory theo giao dịch,
# nên nhiều worker/replica dùng chung DB cũng không chạy 2 lượt của 1 hội thoại cùng lúc.
_locks: dict[str, list] = {}   # thread_id -> [Lock, số request đang giữ/chờ]; xóa khi về 0
_slots = asyncio.Semaphore(MAX_ACTIVE)
# (thread_id, request_id) -> event done: request_id của hội thoại khác không lấy được câu trả lời của hội thoại này.
# shortcut: trong RAM của 1 process (mất khi restart, không chia sẻ giữa worker)
_results: OrderedDict[tuple[str, str], dict] = OrderedDict()


@asynccontextmanager
async def turn_slot(thread_id: str, pool):
    entry = _locks.get(thread_id) or [asyncio.Lock(), 0]
    if entry[1] >= MAX_WAITERS:  # kiểm tra trước khi ghi vào _locks: từ chối thì không để lại khóa rác
        raise TurnError(429, 'Đang có nhiều yêu cầu chờ trong hội thoại này, anh/chị chờ chút rồi gửi lại.')
    _locks[thread_id] = entry
    entry[1] += 1
    try:
        try:
            await asyncio.wait_for(entry[0].acquire(), LOCK_WAIT)
        except TimeoutError:
            raise TurnError(409, 'Lượt trước của hội thoại này chưa xong, anh/chị vui lòng thử lại sau.')
        try:
            try:
                await asyncio.wait_for(_slots.acquire(), SLOT_WAIT)
            except TimeoutError:
                raise TurnError(503, 'Trợ lý đang bận, anh/chị vui lòng thử lại sau ít giây.')
            try:
                if pool is None:
                    yield
                else:
                    async with pool.connection() as conn, conn.transaction():
                        # khóa tự nhả khi giao dịch kết thúc, kể cả khi lượt bị hủy hay kết nối đứt
                        cur = await conn.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))',
                                                 (thread_id,))
                        if not (await cur.fetchone())['pg_try_advisory_xact_lock']:
                            raise TurnError(409, 'Lượt trước của hội thoại này chưa xong, anh/chị vui lòng thử lại sau.')
                        yield
            finally:
                _slots.release()
        finally:
            entry[0].release()
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            _locks.pop(thread_id, None)


# --- giữ state gọn ---
async def repair_dangling_tool_calls(agent, config):
    """Lượt trước bị hủy giữa chừng (client ngắt, quá hạn) có thể để lại tool_call chưa có kết quả:
    model sẽ báo lỗi ở lượt sau. Thêm kết quả 'đã hủy' cho các tool_call đó."""
    msgs = (await agent.aget_state(config)).values.get('messages', [])
    answered = {m.tool_call_id for m in msgs if m.type == 'tool'}
    missing = [tc for m in msgs if m.type == 'ai' for tc in (m.tool_calls or []) if tc['id'] not in answered]
    if missing:
        await agent.aupdate_state(config, {'messages': [
            ToolMessage('Đã hủy: lượt trước bị gián đoạn.', tool_call_id=tc['id'], name=tc['name']) for tc in missing]},
            as_node='tools')


async def trim_state(agent, config, saver):
    msgs = (await agent.aget_state(config)).values.get('messages', [])
    human = [i for i, m in enumerate(msgs) if m.type == 'human']
    if len(human) > KEEP_STATE_TURNS:  # cắt tại ranh giới lượt hỏi -> không tách tool_call khỏi kết quả
        old = msgs[:human[-KEEP_STATE_TURNS]]
        await agent.aupdate_state(config, {'messages': [RemoveMessage(id=m.id) for m in old]})
    try:
        await saver.aprune([config['configurable']['thread_id']], strategy='keep_latest')  # bỏ checkpoint cũ
    except NotImplementedError:
        pass  # InMemorySaver (chạy local/test) không hỗ trợ prune


async def delete_old_threads(saver, pool, days: int = HISTORY_DAYS) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT thread_id FROM checkpoints GROUP BY thread_id"
            " HAVING max((checkpoint->>'ts')::timestamptz) < now() - make_interval(days => %s)", (days,))
        old = [r['thread_id'] for r in await cur.fetchall()]
    for t in old:
        await saver.adelete_thread(t)
    return len(old)


async def retention_loop(saver, pool):
    while True:
        try:
            n = await delete_old_threads(saver, pool)
            if n:
                log.info('Đã xóa %d hội thoại cũ hơn %d ngày', n, HISTORY_DAYS)
        except Exception:
            log.exception('Dọn hội thoại cũ thất bại')
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_url = os.getenv('DATABASE_URL')
    tools.client.close()  # client tạo lúc import; dùng client mới gắn với vòng đời app
    tools.client = tools.new_client()
    try:
        if not db_url:
            # Chạy local/test không có DB: lịch sử trong RAM, mất khi restart
            app.state.saver, app.state.pool = InMemorySaver(), None
            app.state.agent = build_agent(checkpointer=app.state.saver)
            yield
            return
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
        async with AsyncConnectionPool(db_url, max_size=2 * MAX_ACTIVE + 2, open=False, kwargs={
                'autocommit': True, 'prepare_threshold': 0, 'row_factory': dict_row}) as pool:
            saver = AsyncPostgresSaver(pool)
            await saver.setup()
            app.state.saver, app.state.pool = saver, pool
            app.state.agent = build_agent(checkpointer=saver)
            cleaner = asyncio.create_task(retention_loop(saver, pool))
            try:
                yield
            finally:
                cleaner.cancel()
    finally:
        tools.client.close()


# Docs chỉ bật khi DOCS=true (chạy local): gate cũng chặn, nhưng redirect có dấu / cuối từng lộ tên host nội bộ
DOCS = os.getenv('DOCS', 'false').lower() == 'true'
app = FastAPI(title='Packing agent', version='0.2.0', lifespan=lifespan,
              docs_url='/docs' if DOCS else None, redoc_url=None, openapi_url='/openapi.json' if DOCS else None)


@app.get('/health')
async def health():
    return {'status': 'ok'}


def sse(event: str, data: dict) -> str:
    return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'


async def run_turn(app, req: ChatRequest, thread_id: str, request_id: str, out: asyncio.Queue):
    """Chạy 1 lượt, đẩy event vào out. Bị hủy khi client ngắt kết nối."""
    agent, saver = app.state.agent, app.state.saver
    config = {'configurable': {'thread_id': thread_id}}
    try:
        key = (thread_id, request_id)
        if key in _results:  # thử lại 1 lượt đã xong
            return await out.put(sse('done', _results[key]))
        async with turn_slot(thread_id, app.state.pool):
            if key in _results:  # lượt gốc vừa xong trong lúc request này chờ khóa
                return await out.put(sse('done', _results[key]))
            async with asyncio.timeout(TURN_DEADLINE):
                await repair_dangling_tool_calls(agent, config)
                n_before = len((await agent.aget_state(config)).values.get('messages', []))
                sent_tools = set()
                async for msg, meta in agent.astream({'messages': [{'role': 'user', 'content': req.message}]},
                                                     config, stream_mode='messages'):
                    if meta.get('langgraph_node') != 'model':
                        continue
                    # model stream -> tool_call_chunks; model không stream (vd fallback) -> tool_calls đầy đủ
                    for tc in (getattr(msg, 'tool_call_chunks', None) or getattr(msg, 'tool_calls', None) or []):
                        if tc.get('name') and tc.get('id') not in sent_tools:
                            sent_tools.add(tc.get('id'))
                            await out.put(sse('tool', {'name': tc['name']}))
                    if msg.text:
                        await out.put(sse('token', {'text': msg.text}))
                new = (await agent.aget_state(config)).values['messages'][n_before:]
                result = {
                    'thread_id': thread_id,
                    'answer': new[-1].text if new else '',
                    'tools': [tc['name'] for m in new for tc in (getattr(m, 'tool_calls', None) or [])],
                }
                # Câu trả lời đã nằm trong checkpoint: ghi nhận ngay, để lỗi dọn dẹp bên dưới không biến lượt
                # thành công thành lỗi (thử lại sẽ chạy agent lần 2, lặp lượt hỏi và tốn LLM).
                _results[key] = result
                while len(_results) > MAX_CACHED_RESULTS:
                    _results.popitem(last=False)
                # Dọn TRƯỚC khi gửi done: trang đóng kết nối ngay khi đọc được done, và ngắt kết nối thì lượt bị hủy.
                try:
                    await trim_state(agent, config, saver)
                except Exception:  # vd aprune gặp SerializationFailure khi chạy đua với vòng dọn hội thoại cũ
                    log.exception('Dọn state hội thoại %s thất bại, sẽ dọn ở lượt sau', thread_id)
            await out.put(sse('done', result))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        err = to_turn_error(e)
        await out.put(sse('error', {'status': err.status, 'detail': err.detail}))
    finally:
        await out.put(None)


@app.post('/chat')
async def chat(req: ChatRequest, request: Request):
    thread_id = str(req.thread_id or uuid.uuid4())
    request_id = str(req.request_id or uuid.uuid4())

    async def stream():
        yield sse('meta', {'thread_id': thread_id, 'request_id': request_id})
        out: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(run_turn(request.app, req, thread_id, request_id, out))
        try:
            while True:
                try:
                    item = await asyncio.wait_for(out.get(), HEARTBEAT)
                except TimeoutError:
                    yield ': ping\n\n'
                    continue
                if item is None:
                    break
                yield item
        finally:
            if not task.done():  # client ngắt kết nối: dừng agent, nhả khóa
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
