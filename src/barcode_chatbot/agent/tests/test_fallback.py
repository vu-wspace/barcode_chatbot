import asyncio

import groq
import httpx
import pytest
from langgraph.errors import GraphInterrupt

from packing_agent import fallback as F
from packing_agent.fallback import FallbackMiddleware


class M:
    def __init__(self, name): self.model = name


def rate_limited(daily=False):
    r = httpx.Response(429, request=httpx.Request('POST', 'https://x'))
    return groq.RateLimitError('free-models-per-day' if daily else 'limit', response=r, body=None)


def test_skips_a_rate_limited_model_until_its_cooldown_ends():
    now = [0.0]
    a, b = M('a'), M('b')
    fb = FallbackMiddleware(b, clock=lambda: now[0])
    calls = []
    def invoke(m):
        calls.append(m.model)
        if m is a: raise rate_limited()
        return m.model
    assert fb.call(a, invoke) == 'b'
    assert fb.call(a, invoke) == 'b'
    assert calls == ['a', 'b', 'b']          # a is not retried on every question
    now[0] = 61
    fb.call(a, invoke)
    assert calls[-2:] == ['a', 'b']          # tried again after a minute


def test_daily_limit_waits_longer_and_last_error_is_raised():
    now = [0.0]
    a = M('a')
    fb = FallbackMiddleware(clock=lambda: now[0])
    with pytest.raises(groq.RateLimitError):
        fb.call(a, lambda m: (_ for _ in ()).throw(rate_limited(daily=True)))
    now[0] = 600
    assert fb._chain(a) == [a]               # every model cooling down: still tried, not an instant error
    assert fb.skip_until['a'] == 3600


def test_nvidia_status_read_only_from_plain_exception():
    from packing_agent.llm import is_permanent_error, is_rate_limited
    assert is_rate_limited(Exception('[429] Too Many Requests'))
    assert is_permanent_error(Exception('[401] Unauthorized'))
    assert not is_rate_limited(ValueError('[429] in some other text'))
    assert is_permanent_error(RuntimeError('nvidia/x:free is not a valid model ID'))


def test_async_path_falls_back_and_skips_the_cooling_model():
    # /chat runs the agent with astream -> awrap_model_call -> acall: the path production actually uses
    now = [0.0]
    a, b = M('a'), M('b')
    fb = FallbackMiddleware(b, clock=lambda: now[0])
    calls = []
    async def ainvoke(m):
        calls.append(m.model)
        if m is a: raise rate_limited()
        return m.model
    assert asyncio.run(fb.acall(a, ainvoke)) == 'b'
    assert asyncio.run(fb.acall(a, ainvoke)) == 'b'
    assert calls == ['a', 'b', 'b']
    async def all_fail(m): raise rate_limited()
    with pytest.raises(groq.RateLimitError):
        asyncio.run(FallbackMiddleware(b).acall(a, all_fail))


@pytest.mark.parametrize('use_async', [False, True])
def test_graph_interrupt_is_not_a_model_failure(use_async):
    a, b = M('a'), M('b')
    fb = FallbackMiddleware(b)
    calls = []
    def invoke(m):
        calls.append(m.model)
        raise GraphInterrupt()
    async def ainvoke(m): return invoke(m)
    with pytest.raises(GraphInterrupt):
        asyncio.run(fb.acall(a, ainvoke)) if use_async else fb.call(a, invoke)
    assert calls == ['a'] and fb.skip_until == {}   # not answered by another model, nothing cooled down


def _status(code):
    return httpx.Response(code, request=httpx.Request('POST', 'https://x'))


@pytest.mark.parametrize('error,cooldown', [
    (Exception('[401] Unauthorized'), F.PERMANENT_COOLDOWN_S),
    (groq.AuthenticationError('bad key', response=_status(401), body=None), F.PERMANENT_COOLDOWN_S),
    (RuntimeError('nvidia/x:free is not a valid model ID'), F.PERMANENT_COOLDOWN_S),
    (RuntimeError('[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed'), F.SSL_COOLDOWN_S),
    (httpx.ReadTimeout('read timed out'), F.TIMEOUT_COOLDOWN_S),
    (groq.APITimeoutError(request=httpx.Request('POST', 'https://x')), F.TIMEOUT_COOLDOWN_S),
    (Exception('[504] Gateway Timeout'), F.TIMEOUT_COOLDOWN_S),
    (RuntimeError('504 Gateway Timeout. {"status": "DEADLINE_EXCEEDED"}'), F.TIMEOUT_COOLDOWN_S),  # Gemini
    (__import__('langchain_core.exceptions', fromlist=['x']).ModelRateLimitError(
        'Quota exceeded for metric: GenerateRequestsPerDayPerProjectPerModel'), F.DAILY_COOLDOWN_S),
    (rate_limited(), F.MINUTE_COOLDOWN_S),
    (RuntimeError('500 internal error'), None),          # transient: next model, but no cooldown
])
def test_cooldown_per_error_class(error, cooldown):
    a = M('a')
    fb = FallbackMiddleware(M('b'), clock=lambda: 0.0)
    fb.call(a, lambda m: (_ for _ in ()).throw(error) if m is a else 'ok')
    assert fb.skip_until.get('a') == cooldown


def test_success_clears_the_cooldown():
    now = [0.0]
    a = M('a')
    fb = FallbackMiddleware(clock=lambda: now[0])
    with pytest.raises(groq.RateLimitError):
        fb.call(a, lambda m: (_ for _ in ()).throw(rate_limited()))
    assert 'a' in fb.skip_until
    assert fb.call(a, lambda m: 'ok') == 'ok'   # still cooling down, tried anyway (only model) and it works
    assert 'a' not in fb.skip_until


def test_providers_without_a_key_are_left_out_of_the_chain(monkeypatch):
    import importlib
    from packing_agent import llm
    try:
        monkeypatch.setenv('NVIDIA_API_KEY', '')
        monkeypatch.setenv('GROQ_API_KEY', ' UNUSED ')
        monkeypatch.setenv('OPENROUTER_API_KEY', 'k')
        monkeypatch.setenv('GOOGLE_API_KEY', 'k')
        importlib.reload(llm)
        assert [llm.model_name(m) for m in llm.FALLBACK_LLMS] == [llm.model_name(llm.open_llm), llm.model_name(llm.google_llm_alt)]
        monkeypatch.setenv('NVIDIA_API_KEY', 'k')
        monkeypatch.setenv('GROQ_API_KEY', 'k')
        importlib.reload(llm)
        assert llm.MAIN_LLM is llm.google_llm
        assert llm.FALLBACK_LLMS == [llm.open_llm, llm.nvidia_llm, llm.google_llm_alt, llm.groq_llm]  # measured order
    finally:
        monkeypatch.undo()
        importlib.reload(llm)
