import asyncio
import json
import threading
import time
import uuid

import groq
import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from packing_agent import agent as agent_module
from packing_agent import api, tools


class FakeModel(GenericFakeChatModel):
    """Model giả trả lần lượt các message cho trước. disable_streaming: fake của langchain không stream được tool_call."""
    def bind_tools(self, tools, **kwargs):
        return self


def model(*replies, stream=False):
    return FakeModel(messages=iter(replies), disable_streaming=not stream)


def call(name, **args):
    return AIMessage('', tool_calls=[{'name': name, 'args': args, 'id': f'c-{uuid.uuid4().hex[:6]}'}])


class Raising(FakeModel):
    error: Exception = None

    def _generate(self, *a, **kw):
        raise self.error


@pytest.fixture
def client(monkeypatch):
    # BE giả cho tool
    monkeypatch.setattr(tools, 'new_client', lambda: httpx.Client(
        base_url='http://be', transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'sku': 'HD-1001'}))))
    api._results.clear()
    with TestClient(api.app) as c:  # chạy lifespan: InMemorySaver vì không có DATABASE_URL
        yield c


def use(monkeypatch, llm):
    monkeypatch.setattr(agent_module, 'open_llm', llm)
    api.app.state.agent = agent_module.build_agent(checkpointer=api.app.state.saver, fallback=False)


def chat(client, **body):
    r = client.post('/chat', json=body)
    assert r.status_code == 200 and r.headers['content-type'].startswith('text/event-stream'), r.text
    events = []
    for block in r.text.split('\n\n'):
        lines = [l for l in block.splitlines() if not l.startswith(':')]
        if lines:
            events.append((lines[0].removeprefix('event: '), json.loads(lines[1].removeprefix('data: '))))
    return events


def test_stream_order_and_final_answer(client, monkeypatch):
    use(monkeypatch, model(call('get_product', code='8935000100011'), AIMessage('Đây là tai nghe')))
    ev = chat(client, message='mã 8935000100011')
    names = [e for e, _ in ev]
    assert names[0] == 'meta' and names[-1] == 'done' and 'tool' in names
    uuid.UUID(ev[0][1]['thread_id'])
    assert ev[-1][1]['answer'] == 'Đây là tai nghe' and ev[-1][1]['tools'] == ['get_product']


def test_tokens_are_streamed(client, monkeypatch):
    use(monkeypatch, model(AIMessage('Dạ đây là tai nghe'), stream=True))
    ev = chat(client, message='hi')
    assert ''.join(d['text'] for e, d in ev if e == 'token') == 'Dạ đây là tai nghe'


def test_same_thread_only_reports_tools_of_current_turn(client, monkeypatch):
    use(monkeypatch, model(call('get_product', code='1'), AIMessage('a1'), AIMessage('a2')))
    tid = str(uuid.uuid4())
    chat(client, message='câu 1', thread_id=tid)
    done = chat(client, message='câu 2', thread_id=tid)[-1][1]
    assert done == {'thread_id': tid, 'answer': 'a2', 'tools': []}


def test_retry_with_same_request_id_does_not_run_again(client, monkeypatch):
    use(monkeypatch, model(AIMessage('một lần')))  # gọi model lần 2 sẽ lỗi vì hết reply
    body = {'message': 'hi', 'thread_id': str(uuid.uuid4()), 'request_id': str(uuid.uuid4())}
    assert chat(client, **body)[-1] == chat(client, **body)[-1]
    assert chat(client, **body)[-1][0] == 'done'


@pytest.mark.parametrize('body', [{'message': ''}, {'message': 'x' * 4001}, {}, {'message': 'hi', 'thread_id': '1'},
                                  {'message': 'hi', 'request_id': 'abc'}])
def test_validation(client, body):
    # thread_id kiểu '1' dễ đoán -> đọc được hội thoại người khác
    assert client.post('/chat', json=body).status_code == 422


def _groq_413():
    resp = httpx.Response(413, request=httpx.Request('POST', 'https://api.groq.com'))
    return groq.APIStatusError('413', response=resp, body={'error': {'code': 'rate_limit_exceeded', 'type': 'tokens'}})


def _openrouter(E, cls, data_cls, code):
    resp = httpx.Response(code, request=httpx.Request('POST', 'https://openrouter.ai'))
    return cls(data_cls(error={'code': code, 'message': 'x'}), resp)


@pytest.mark.parametrize('make_error,status', [
    (lambda: groq.RateLimitError('rl', response=httpx.Response(429, request=httpx.Request('POST', 'https://g')), body=None), 429),
    (_groq_413, 429),
    (lambda: groq.InternalServerError('boom', response=httpx.Response(500, request=httpx.Request('POST', 'https://g')), body=None), 502),
    (lambda: __import__('langchain_google_genai.chat_models', fromlist=['x']).GoogleRateLimitError('RESOURCE_EXHAUSTED'), 429),
    (lambda: __import__('langchain_google_genai.chat_models', fromlist=['x']).GoogleInvalidRequestError('bad'), 502),
    (lambda: (E := __import__('openrouter.errors', fromlist=['x'])) and _openrouter(E, E.TooManyRequestsResponseError, E.TooManyRequestsResponseErrorData, 429), 429),
    (lambda: (E := __import__('openrouter.errors', fromlist=['x'])) and _openrouter(E, E.InternalServerResponseError, E.InternalServerResponseErrorData, 500), 502),
    (lambda: __import__('openrouter.errors', fromlist=['x']).NoResponseError(), 502),
    (lambda: RuntimeError('bug'), 500),  # lỗi lạ vẫn ra event error, không làm đứt stream
])
def test_errors_become_error_events(client, monkeypatch, make_error, status):
    use(monkeypatch, Raising(messages=iter([]), error=make_error()))
    ev = chat(client, message='hi')
    assert ev[-1][0] == 'error' and ev[-1][1]['status'] == status, ev


def test_deadline(client, monkeypatch):
    class Slow(FakeModel):
        def _generate(self, *a, **kw):
            time.sleep(1)
            return super()._generate(*a, **kw)
    use(monkeypatch, Slow(messages=iter([AIMessage('muộn')]), disable_streaming=True))
    monkeypatch.setattr(api, 'TURN_DEADLINE', 0.2)
    assert chat(client, message='hi')[-1] == ('error', {'status': 504, 'detail': api.to_turn_error(TimeoutError()).detail})


def test_same_thread_turns_run_one_at_a_time(client, monkeypatch):
    running, overlap = [0], []

    class Slow(FakeModel):
        def _generate(self, *a, **kw):
            running[0] += 1
            overlap.append(running[0])
            time.sleep(0.2)
            running[0] -= 1
            return super()._generate(*a, **kw)
    use(monkeypatch, Slow(messages=iter([AIMessage(f'a{i}') for i in range(3)]), disable_streaming=True))
    tid, results = str(uuid.uuid4()), []
    ts = [threading.Thread(target=lambda i=i: results.append(chat(client, message=f'm{i}', thread_id=tid)[-1][0]))
          for i in range(3)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert max(overlap) == 1 and results == ['done'] * 3
    assert api._locks == {}  # khóa được thu hồi khi không còn ai dùng


def test_too_many_waiters_on_one_thread(client, monkeypatch):
    monkeypatch.setattr(api, 'MAX_WAITERS', 0)
    use(monkeypatch, model(AIMessage('x')))
    assert chat(client, message='hi')[-1][1]['status'] == 429
    assert api._locks == {}  # request bị từ chối không để lại khóa


def test_cancelled_turn_releases_lock_and_next_turn_works(client, monkeypatch):
    # Client ngắt kết nối = task của lượt bị hủy giữa lúc chạy tool
    started = threading.Event()

    def slow_be(req):
        started.set()
        time.sleep(0.5)
        return httpx.Response(200, json={'sku': 'HD-1001'})
    tools.client = httpx.Client(base_url='http://be', transport=httpx.MockTransport(slow_be))
    use(monkeypatch, model(call('get_order', order_id='SO-1'), AIMessage('lượt sau ok')))
    tid = str(uuid.uuid4())

    async def cancel_mid_turn():
        q = asyncio.Queue()
        task = asyncio.create_task(api.run_turn(api.app, api.ChatRequest(message='kiểm tra'), tid, str(uuid.uuid4()), q))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    client.portal.call(cancel_mid_turn)
    assert api._locks == {}
    done = chat(client, message='lượt sau', thread_id=tid)[-1]
    assert done[0] == 'done' and done[1]['answer'] == 'lượt sau ok', done  # tool_call dở dang đã được vá


def test_state_keeps_only_recent_turns(client, monkeypatch):
    monkeypatch.setattr(api, 'KEEP_STATE_TURNS', 2)
    use(monkeypatch, model(*[AIMessage(f'a{i}') for i in range(4)]))
    tid = str(uuid.uuid4())
    for i in range(4):
        chat(client, message=f'q{i}', thread_id=tid)
    msgs = api.app.state.agent.get_state({'configurable': {'thread_id': tid}}).values['messages']
    assert [m.text for m in msgs] == ['q2', 'a2', 'q3', 'a3']


def test_cleanup_failure_after_answer_still_reports_done(client, monkeypatch):
    import psycopg
    use(monkeypatch, model(AIMessage('xong')))  # gọi model lần 2 sẽ lỗi vì hết reply

    async def boom(*a, **kw):
        raise psycopg.errors.SerializationFailure('could not serialize access')
    monkeypatch.setattr(api.app.state.saver, 'aprune', boom, raising=False)
    body = {'message': 'hi', 'thread_id': str(uuid.uuid4()), 'request_id': str(uuid.uuid4())}
    assert chat(client, **body)[-1] == ('done', {'thread_id': body['thread_id'], 'answer': 'xong', 'tools': []})
    assert chat(client, **body)[-1][1]['answer'] == 'xong'  # thử lại: lấy kết quả đã có, không chạy agent lần 2


def test_request_id_from_other_thread_is_not_replayed(client, monkeypatch):
    use(monkeypatch, model(AIMessage('của A'), AIMessage('của B')))
    rid = str(uuid.uuid4())
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert chat(client, message='hi', thread_id=a, request_id=rid)[-1][1]['answer'] == 'của A'
    done = chat(client, message='hi', thread_id=b, request_id=rid)[-1][1]
    assert done == {'thread_id': b, 'answer': 'của B', 'tools': []}  # chạy lượt mới, không trả câu trả lời của A


def test_health(client):
    assert client.get('/health').json() == {'status': 'ok'}
