import groq
import httpx
import pytest

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
