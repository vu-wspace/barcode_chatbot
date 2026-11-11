from bank_faqs.llm import google_llm, groq_llm, open_llm


def test_openrouter_timeout_is_in_milliseconds():
    # ChatOpenRouter tính timeout bằng mili giây: đặt 60 (tưởng là giây) thì mọi request timeout sau 60ms,
    # SDK thử lại liên tục và agent treo vài phút
    assert open_llm.request_timeout >= 10_000


def test_other_llm_timeouts_are_in_seconds():
    assert 10 <= groq_llm.request_timeout <= 300
    assert 10 <= google_llm.timeout <= 300
