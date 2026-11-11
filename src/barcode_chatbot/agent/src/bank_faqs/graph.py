import bank_faqs  # noqa: F401 - phải import đầu tiên khi chạy file trực tiếp (xem __init__.py)
from typing import Annotated, TypedDict
from langchain_core.messages import AnyMessage, SystemMessage
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from bank_faqs.history import trim_history
from bank_faqs.llm import open_llm
from bank_faqs.prompts import get_system_prompt
from bank_faqs.tools import get_agent_tools

# Cùng agent với agent.py nhưng tự dựng bằng StateGraph: model -> (có tool_call) tools -> model ... -> END
TOOLS = get_agent_tools('agent_main')
SYSTEM_PROMPT = get_system_prompt('agent_main')

llm_with_tools = open_llm.bind_tools(TOOLS, parallel_tool_calls=False)

class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]

def call_model(state: State) -> dict:
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + trim_history(state['messages'])
    return {'messages': [llm_with_tools.invoke(messages)]}

builder = StateGraph(State)
builder.add_node('model', call_model)
builder.add_node('tools', ToolNode(TOOLS))
builder.add_edge(START, 'model')
builder.add_conditional_edges('model', tools_condition)  # có tool_call -> 'tools', không thì END
builder.add_edge('tools', 'model')
graph = builder.compile()

if __name__ == "__main__":
    res = graph.invoke({'messages': [{'role': 'user', 'content': 'quên mk app rồi, lấy lại sao vậy'}]})
    for m in res['messages']:
        m.pretty_print()
