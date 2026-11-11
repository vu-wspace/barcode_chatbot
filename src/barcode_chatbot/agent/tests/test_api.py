import uuid
from types import SimpleNamespace

import groq
import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from bank_faqs import api


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
            AIMessage('', tool_calls=[{'name': 'get_interest_rate', 'args': {}, 'id': call_id}]),
            ToolMessage('...', tool_call_id=call_id),
            AIMessage(f'Trả lời: {text}'),
        ]
        return {'messages': list(history)}


@pytest.fixture
def client():
    # Không dùng `with TestClient(...)` để bỏ qua lifespan (không cần Qdrant/model thật)
    api.app.state.agent = FakeAgent()
    return TestClient(api.app)


def test_index_serves_chat_page(client):
    r = client.get('/')
    assert r.status_code == 200
    assert 'text/html' in r.headers['content-type']
    assert 'Trợ lý chăm sóc khách hàng' in r.text


def test_chat_new_thread(client):
    r = client.post('/chat', json={'message': 'lãi suất BIDV'})
    assert r.status_code == 200
    data = r.json()
    uuid.UUID(data['thread_id'])
    assert data['answer'] == 'Trả lời: lãi suất BIDV'
    assert data['tools'] == ['get_interest_rate']


def test_chat_same_thread_only_reports_tools_of_current_turn(client):
    tid = client.post('/chat', json={'message': 'câu 1'}).json()['thread_id']
    r = client.post('/chat', json={'message': 'câu 2', 'thread_id': tid})
    data = r.json()
    assert data['thread_id'] == tid
    assert data['answer'] == 'Trả lời: câu 2'
    assert data['tools'] == ['get_interest_rate']  # không lặp lại tool của lượt trước
    assert len(api.app.state.agent.threads[tid]) == 8


@pytest.mark.parametrize('body', [{'message': ''}, {'message': 'x' * 1001}, {}, {'message': 'hi', 'thread_id': 'x' * 101}])
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


def test_health_ok(client, monkeypatch):
    monkeypatch.setattr(api, 'count_documents', lambda: 34)
    assert client.get('/health').json() == {'status': 'ok', 'documents': 34}


def test_health_qdrant_down(client, monkeypatch):
    def boom():
        raise ConnectionError()
    monkeypatch.setattr(api, 'count_documents', boom)
    assert client.get('/health').status_code == 503


def test_qdrant_client_is_reused():
    # /health được gọi mỗi 30s: tạo client mới mỗi lần sẽ tích tụ kết nối
    from bank_faqs.vector_db import get_client
    assert get_client() is get_client()


def test_prepare_vector_db_ingests_when_empty(monkeypatch):
    monkeypatch.setattr(api, 'get_vector_store', lambda: 'db')
    monkeypatch.setattr(api, 'count_documents', lambda: 0)
    monkeypatch.setattr(api, 'ingest', lambda db: 34)
    assert api.prepare_vector_db() == 34


def test_prepare_vector_db_retries_then_raises(monkeypatch):
    calls = []
    def boom():
        calls.append(1)
        raise ConnectionError()
    monkeypatch.setattr(api, 'get_vector_store', boom)
    with pytest.raises(ConnectionError):
        api.prepare_vector_db(retries=3, delay=0)
    assert len(calls) == 3
