import bank_faqs  # noqa: F401 - phải import đầu tiên khi chạy file trực tiếp (xem __init__.py)
from langchain.agents import create_agent
from langchain.agents.middleware import ModelFallbackMiddleware
from langgraph.types import Checkpointer
from bank_faqs.history import TrimHistoryMiddleware
from bank_faqs.llm import google_llm, open_llm
from bank_faqs.prompts import get_system_prompt
from bank_faqs.tools import get_agent_tools

def build_agent(checkpointer: Checkpointer = None, fallback: bool = True):
    # checkpointer (vd: InMemorySaver) giúp agent nhớ hội thoại theo thread_id - api.py dùng cách này.
    # Không truyền khi chạy qua `langgraph dev` vì server tự quản lý bộ nhớ.
    middleware = [TrimHistoryMiddleware()]
    if fallback:
        # OpenRouter gói free hết quota ngày (hoặc lỗi) thì trả lời bằng Gemini thay vì báo lỗi cho khách.
        # Eval tắt fallback để điểm số phản ánh đúng model chính.
        middleware.append(ModelFallbackMiddleware(google_llm))
    return create_agent(
        model=open_llm,
        tools=get_agent_tools('agent_main'),
        system_prompt=get_system_prompt('agent_main'),
        middleware=middleware,
        checkpointer=checkpointer,
    )

agent = build_agent()

if __name__ == "__main__":
    tests = [
        'quên mk app rồi, lấy lại sao vậy',             # nên gọi search_faq
        'lãi suất gửi 12 tháng ở BIDV bao nhiêu',       # nên gọi get_interest_rate
        'em lỡ đưa otp cho người ta, mất hết tiền rồi', # nên báo chuyển nhân viên
        'nên mua vàng hay cổ phiếu',                    # nên từ chối
    ]
    for q in tests:
        res = agent.invoke({'messages': [{'role': 'user', 'content': q}]})
        called = [tc['name'] for m in res['messages'] for tc in (getattr(m, 'tool_calls', None) or [])]
        print(f"\nKhách: {q}\nTool đã gọi: {called}\nTrả lời: {res['messages'][-1].text}")
