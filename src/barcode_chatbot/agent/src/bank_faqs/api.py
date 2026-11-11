import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import groq
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from langchain_core.exceptions import ModelError, ModelRateLimitError
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from openrouter import errors as openrouter_errors
from pydantic import BaseModel, Field

from bank_faqs.agent import build_agent
from bank_faqs.config import AUTO_INGEST
from bank_faqs.vector_db import count_documents, get_vector_store, ingest

log = logging.getLogger('uvicorn.error')
STATIC = Path(__file__).parent / 'static'

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    thread_id: str | None = Field(default=None, max_length=100)

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

def prepare_vector_db(retries: int = 30, delay: float = 2.0) -> int:
    # Qdrant có thể khởi động chậm hơn API khi chạy bằng docker compose -> thử lại vài lần
    for attempt in range(1, retries + 1):
        try:
            db = get_vector_store()  # tạo collection nếu chưa có
            n = count_documents()
            if n == 0 and AUTO_INGEST:
                log.info('Collection rỗng, đang nạp dữ liệu...')
                n = ingest(db)
            return n
        except Exception as e:
            if attempt == retries:
                raise
            log.warning('Chưa kết nối được Qdrant (%s), thử lại sau %ss (%d/%d)',
                        type(e).__name__, delay, attempt, retries)
            time.sleep(delay)

@asynccontextmanager
async def lifespan(app: FastAPI):
    n = await asyncio.to_thread(prepare_vector_db)
    log.info('Qdrant sẵn sàng: %d tài liệu', n)
    # InMemorySaver: lịch sử hội thoại nằm trong RAM của process -> mất khi restart, chỉ chạy 1 worker
    app.state.agent = build_agent(checkpointer=InMemorySaver())
    yield

app = FastAPI(title='Bank FAQs', version='0.1.0', lifespan=lifespan)

@app.get('/', include_in_schema=False)
def index():
    return FileResponse(STATIC / 'index.html')

@app.get('/health')
def health():
    try:
        return {'status': 'ok', 'documents': count_documents()}
    except Exception as e:
        raise HTTPException(503, f'Qdrant không sẵn sàng: {type(e).__name__}')

@app.post('/chat', response_model=ChatResponse)
def chat(req: ChatRequest, request: Request):
    agent = request.app.state.agent
    thread_id = req.thread_id or str(uuid.uuid4())
    config = {'configurable': {'thread_id': thread_id}}

    # Agent trả về cả lịch sử của thread -> chỉ lấy các message mới của lượt này
    n_before = len(agent.get_state(config).values.get('messages', []))
    try:
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
