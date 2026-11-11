import uuid
from types import SimpleNamespace

import groq
import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from packing_agent import api


class FakeAgent:
    """Giả lập agent có checkpointer: lưu lịch sử theo thread_id, lượt nào cũng gọi 1 tool."""

    def __init__(self, error: Exception | None = None):
        self.threads: dict[str, list] = {}
        self.error = error

    def get_state(self, config):
        return SimpleNamespace(values={'messages': list(self.threads.get(config['configurable']['thread_id'], []))})

    def invoke(self, input, config):
        if self.error:
            raise self.error
        history = self.threads.setdefault(config['configurable']['thread_id'], [])
        text = input['messages'][0]['content']
        call_id = f'call-{len(history)}'
        history += [
            HumanMessage(text),
            AIMessage('', tool_calls=[{'name': 'get_product', 'args': {}, 'id': call_id}]),
            ToolMessage('...', tool_call_id=call_id),
            AIMessage(f'Trả lời: {text}'),
        ]
        return {'messages': list(history)}


@pytest.fixture
def client():
    # Không dùng `with TestClient(...)` để bỏ qua lifespan (không cần model thật)
    api.app.state.agent = FakeAgent()
    return TestClient(api.app)


def test_chat_new_thread(client):
    r = client.post('/chat', json={'message': 'mã 8935000100011'})
    assert r.status_code == 200
    data = r.json()
    uuid.UUID(data['thread_id'])
    assert data['answer'] == 'Trả lời: mã 8935000100011'
    assert data['tools'] == ['get_product']


def test_chat_same_thread_only_reports_tools_of_current_turn(client):
    tid = client.post('/chat', json={'message': 'câu 1'}).json()['thread_id']
    r = client.post('/chat', json={'message': 'câu 2', 'thread_id': tid})
    data = r.json()
    assert data['thread_id'] == tid
    assert data['answer'] == 'Trả lời: câu 2'
    assert data['tools'] == ['get_product']  # không lặp lại tool của lượt trước
    assert len(api.app.state.agent.threads[tid]) == 8


@pytest.mark.parametrize('body', [{'message': ''}, {'message': 'x' * 4001}, {}, {'message': 'hi', 'thread_id': 'x' * 101}])
def test_chat_validation(client, body):
    assert client.post('/chat', json=body).status_code == 422


def test_chat_rate_limit_returns_429(client):
    resp = httpx.Response(429, request=httpx.Request('POST', 'https://api.groq.com'))
    api.app.state.agent = FakeAgent(error=groq.RateLimitError('rate limit', response=resp, body=None))
    r = client.post('/chat', json={'message': 'hi'})
    assert r.status_code == 429
    assert 'quá tải' in r.json()['detail']


def test_chat_request_too_large_rate_limit_returns_429(client):
    # Groq trả 413 khi vượt giới hạn token đầu vào/phút (lỗi người dùng gặp thực tế)
    resp = httpx.Response(413, request=httpx.Request('POST', 'https://api.groq.com'))
    body = {'error': {'message': 'Request too large ... (ITPM): Limit 7000, Requested 7142',
                      'type': 'tokens', 'code': 'rate_limit_exceeded'}}
    api.app.state.agent = FakeAgent(error=groq.APIStatusError('413', response=resp, body=body))
    r = client.post('/chat', json={'message': 'hi'})
    assert r.status_code == 429


def test_chat_gemini_rate_limit_returns_429(client):
    from langchain_google_genai.chat_models import GoogleRateLimitError
    api.app.state.agent = FakeAgent(error=GoogleRateLimitError("Error calling model 'gemini-3.5-flash' (RESOURCE_EXHAUSTED)"))
    assert client.post('/chat', json={'message': 'hi'}).status_code == 429


def test_chat_gemini_other_error_returns_502(client):
    from langchain_google_genai.chat_models import GoogleInvalidRequestError
    api.app.state.agent = FakeAgent(error=GoogleInvalidRequestError('bad request'))
    assert client.post('/chat', json={'message': 'hi'}).status_code == 502


def test_chat_openrouter_rate_limit_returns_429(client):
    import openrouter.errors as E
    resp = httpx.Response(429, request=httpx.Request('POST', 'https://openrouter.ai'))
    data = E.TooManyRequestsResponseErrorData(error={'code': 429, 'message': 'Rate limit exceeded: free-models-per-min'})
    api.app.state.agent = FakeAgent(error=E.TooManyRequestsResponseError(data, resp))
    assert client.post('/chat', json={'message': 'hi'}).status_code == 429


@pytest.mark.parametrize('make_error', [
    lambda E: E.InternalServerResponseError(E.InternalServerResponseErrorData(error={'code': 500, 'message': 'boom'}),
                                            httpx.Response(500, request=httpx.Request('POST', 'https://openrouter.ai'))),
    lambda E: E.NoResponseError(),  # mất kết nối, không nhận được phản hồi
])
def test_chat_openrouter_other_error_returns_502(client, make_error):
    # Lỗi OpenRouter không kế thừa ModelError: không bắt riêng thì API trả 500 kèm traceback
    import openrouter.errors as E
    api.app.state.agent = FakeAgent(error=make_error(E))
    assert client.post('/chat', json={'message': 'hi'}).status_code == 502


def test_chat_upstream_error_returns_502(client):
    resp = httpx.Response(500, request=httpx.Request('POST', 'https://api.groq.com'))
    api.app.state.agent = FakeAgent(error=groq.InternalServerError('boom', response=resp, body=None))
    r = client.post('/chat', json={'message': 'hi'})
    assert r.status_code == 502


def test_health(client):
    assert client.get('/health').json() == {'status': 'ok'}
