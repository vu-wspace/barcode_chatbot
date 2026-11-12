from packing_agent.llm import google_llm, groq_llm, open_llm


def test_openrouter_timeout_is_in_milliseconds():
    # ChatOpenRouter tính timeout bằng mili giây: đặt 60 (tưởng là giây) thì mọi request timeout sau 60ms,
    # SDK thử lại liên tục và agent treo vài phút
    assert open_llm.request_timeout >= 10_000


def test_other_llm_timeouts_are_in_seconds():
    assert 10 <= groq_llm.request_timeout <= 300
    assert 10 <= google_llm.timeout <= 300  # seconds, not ms; Gemini rejects every request below 10 s



def test_timeouts_and_privacy_defaults():
    from packing_agent.llm import nvidia_llm
    assert google_llm.timeout == 15 and google_llm.max_retries == 0  # main model: a hung call goes to the fallback fast
    assert open_llm.request_timeout == 30_000      # milliseconds for OpenRouter
    assert nvidia_llm.usage_telemetry_enabled is False
