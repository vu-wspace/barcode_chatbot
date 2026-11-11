import logging
import os
import threading
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager

import groq
from fastapi import FastAPI, HTTPException, Request
from langchain_core.exceptions import ModelError, ModelRateLimitError
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from openrouter import errors as openrouter_errors
from pydantic import BaseModel, Field

from packing_agent.agent import build_agent

log = logging.getLogger('uvicorn.error')

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)  # FE gửi kèm danh sách mã đã quét
    # Chỉ nhận UUID (do /chat cấp ở lượt đầu): không đoán được thread của người khác như '1', 'abc'.
    # shortcut: chưa có đăng nhập nên chưa kiểm tra chủ thread; khi có user thì lưu thread_id theo user.
    thread_id: uuid.UUID | None = None

class ChatResponse(BaseModel):
    thread_id: str
    answer: str
    tools: list[str]

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

HISTORY_DAYS = 7  # hội thoại không dùng quá ngần này ngày thì bị xóa lúc khởi động

def delete_old_threads(saver, pool, days: int = HISTORY_DAYS) -> int:
    with pool.connection() as conn:
        old = [r['thread_id'] for r in conn.execute(
            "SELECT thread_id FROM checkpoints GROUP BY thread_id"
            " HAVING max((checkpoint->>'ts')::timestamptz) < now() - make_interval(days => %s)", (days,))]
    for t in old:
        saver.delete_thread(t)
    return len(old)

@asynccontextmanager
async def lifespan(app: FastAPI):
    db_url = os.getenv('DATABASE_URL')
    if not db_url:
        # Chạy local/test không có DB: lịch sử trong RAM, mất khi restart
        app.state.agent = build_agent(checkpointer=InMemorySaver())
        yield
        return
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    # Lịch sử hội thoại lưu trong Postgres (bảng checkpoints*) -> còn sau khi restart container
    with ConnectionPool(db_url, max_size=5, kwargs={'autocommit': True, 'prepare_threshold': 0,
                                                    'row_factory': dict_row}) as pool:
        saver = PostgresSaver(pool)
        saver.setup()
        log.info('Đã xóa %d hội thoại cũ hơn %d ngày', delete_old_threads(saver, pool), HISTORY_DAYS)
        app.state.agent = build_agent(checkpointer=saver)
        yield

# Các lượt của cùng 1 thread chạy lần lượt: 2 request song song sẽ đọc cùng lịch sử rồi ghi đè lẫn nhau.
# Khóa nằm trong process -> chỉ chạy 1 worker. shortcut: dict khóa không bao giờ thu nhỏ (vài trăm byte/thread).
_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()

def thread_lock(thread_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks[thread_id]

app = FastAPI(title='Packing agent', version='0.1.0', lifespan=lifespan)

@app.get('/health')
def health():
    return {'status': 'ok'}

@app.post('/chat', response_model=ChatResponse)
def chat(req: ChatRequest, request: Request):
    agent = request.app.state.agent
    thread_id = str(req.thread_id or uuid.uuid4())
    config = {'configurable': {'thread_id': thread_id}}

    try:
        with thread_lock(thread_id):
            # Agent trả về cả lịch sử của thread -> chỉ lấy các message mới của lượt này
            n_before = len(agent.get_state(config).values.get('messages', []))
            res = agent.invoke({'messages': [{'role': 'user', 'content': req.message}]}, config)
    except (groq.APIError, openrouter_errors.OpenRouterError, openrouter_errors.NoResponseError,
            ModelError, GraphRecursionError) as e:
        if is_rate_limited(e):
            # Ghi lại loại giới hạn (token/phút, token/ngày...) để biết chờ hay phải nâng gói
            log.warning('LLM rate limit: %s', e)
            raise HTTPException(429, 'Hệ thống đang quá tải, anh/chị vui lòng thử lại sau ít phút.')
        log.exception('Lỗi khi chạy agent')
        raise HTTPException(502, f'Không tạo được câu trả lời ({type(e).__name__}).')

    new_messages = res['messages'][n_before:]
    tools = [tc['name'] for m in new_messages for tc in (getattr(m, 'tool_calls', None) or [])]
    return ChatResponse(thread_id=thread_id, answer=res['messages'][-1].text, tools=tools)
