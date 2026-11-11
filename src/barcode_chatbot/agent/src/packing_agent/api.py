import logging
import uuid
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    # InMemorySaver: lịch sử hội thoại nằm trong RAM của process -> mất khi restart, chỉ chạy 1 worker
    app.state.agent = build_agent(checkpointer=InMemorySaver())
    yield

app = FastAPI(title='Packing agent', version='0.1.0', lifespan=lifespan)

@app.get('/health')
def health():
    return {'status': 'ok'}

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
