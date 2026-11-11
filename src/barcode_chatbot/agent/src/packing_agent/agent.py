import packing_agent  # noqa: F401 - phải import đầu tiên khi chạy file trực tiếp (xem __init__.py)
from langchain.agents import create_agent
from langchain.agents.middleware import ModelFallbackMiddleware
from langgraph.types import Checkpointer
from packing_agent.history import TrimHistoryMiddleware
from packing_agent.llm import google_llm, open_llm
from packing_agent.prompts import SYSTEM_PROMPT
from packing_agent.tools import TOOLS

def build_agent(checkpointer: Checkpointer = None, fallback: bool = True):
    # checkpointer (vd: InMemorySaver) giúp agent nhớ hội thoại theo thread_id - api.py dùng cách này.
    # Không truyền khi chạy qua `langgraph dev` vì server tự quản lý bộ nhớ.
    middleware = [TrimHistoryMiddleware()]
    if fallback:
        # OpenRouter gói free hết quota ngày (hoặc lỗi) thì trả lời bằng Gemini thay vì báo lỗi.
        middleware.append(ModelFallbackMiddleware(google_llm))
    return create_agent(
        model=open_llm,
        tools=TOOLS,
        system_prompt=SYSTEM_PROMPT,
        middleware=middleware,
        checkpointer=checkpointer,
    )

agent = build_agent()

if __name__ == "__main__":
    tests = [
        'mã 8935000100011 là gì',                                                  # get_product
        'Đơn SO-104587. Đã quét: 8935000100011, 8935000200018. Kiểm tra giúp',    # verify_order: thiếu cáp
        'còn bàn phím không',                                                      # search_products: hết hàng
    ]
    for q in tests:
        res = agent.invoke({'messages': [{'role': 'user', 'content': q}]})
        called = [tc['name'] for m in res['messages'] for tc in (getattr(m, 'tool_calls', None) or [])]
        print(f"\nHỏi: {q}\nTool đã gọi: {called}\nTrả lời: {res['messages'][-1].text}")
