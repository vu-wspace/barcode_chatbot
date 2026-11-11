import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from bank_faqs import agent as agent_module


class FakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class QuotaExceededModel(FakeModel):
    def _generate(self, *args, **kwargs):
        raise RuntimeError('Rate limit exceeded: free-models-per-day')


@pytest.fixture
def models(monkeypatch):
    monkeypatch.setattr(agent_module, 'open_llm', QuotaExceededModel(messages=iter([])))
    monkeypatch.setattr(agent_module, 'google_llm', FakeModel(messages=iter([AIMessage('Dạ, em trả lời bằng model dự phòng ạ.')])))


def ask(agent) -> str:
    return agent.invoke({'messages': [{'role': 'user', 'content': 'chào'}]})['messages'][-1].text


def test_falls_back_to_gemini_when_primary_fails(models):
    assert ask(agent_module.build_agent()) == 'Dạ, em trả lời bằng model dự phòng ạ.'


def test_no_fallback_raises_primary_error(models):
    # Eval dùng fallback=False: lỗi của model chính phải lộ ra, không bị model khác che
    with pytest.raises(RuntimeError, match='free-models-per-day'):
        ask(agent_module.build_agent(fallback=False))
