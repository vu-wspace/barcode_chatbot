from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ExtendedModelResponse, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage

from packing_agent.prompts import strip_final_a

# Gửi lại toàn bộ lịch sử (kể cả kết quả tool dài của các lượt trước) làm số token đầu vào tăng mãi:
# vài lượt là vượt giới hạn token của gói free (vd Groq 7000 token/phút -> lỗi 413), lại chậm và tốn token.
# Chỉ cắt bớt khi gửi model, state vẫn giữ đủ.
KEEP_TURNS = 4          # số lượt hỏi gần nhất được gửi cho model
OLD_TOOL_CHARS = 300    # kết quả tool của các lượt trước chỉ giữ ngần này ký tự

def trim_history(messages: list[AnyMessage], keep_turns: int = KEEP_TURNS,
                 old_tool_chars: int = OLD_TOOL_CHARS) -> list[AnyMessage]:
    human_idx = [i for i, m in enumerate(messages) if m.type == 'human']
    if not human_idx:
        return messages
    # Cắt tại ranh giới tin nhắn của khách -> không tách rời tool_call khỏi kết quả của nó
    if len(human_idx) > keep_turns:
        messages = messages[human_idx[-keep_turns]:]
    last_human = max(i for i, m in enumerate(messages) if m.type == 'human')

    out = []
    for i, m in enumerate(messages):
        content = m.content if isinstance(m.content, str) else str(m.content)
        if m.type == 'tool' and i < last_human and len(content) > old_tool_chars:
            m = m.model_copy(update={'content': content[:old_tool_chars] + ' …(đã rút gọn)'})
        out.append(m)
    return out

def _clean(msg):
    if msg.type != 'ai':
        return msg
    if isinstance(msg.content, str):
        content = strip_final_a(msg.content)
    else:  # content blocks (e.g. Gemini): clean the text parts only
        content = [{**b, 'text': strip_final_a(b['text'])} if isinstance(b, dict) and b.get('type') == 'text' else b
                   for b in msg.content]
    return msg.model_copy(update={'content': content})


def clean_response(resp):
    """Remove sentence-final "ạ" from the model's answer before it is stored (so later turns don't copy it)."""
    if isinstance(resp, ExtendedModelResponse):
        resp.model_response.result = [_clean(m) for m in resp.model_response.result]
    elif isinstance(resp, ModelResponse):
        resp.result = [_clean(m) for m in resp.result]
    elif isinstance(resp, AIMessage):
        resp = _clean(resp)
    return resp


class TrimHistoryMiddleware(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        return clean_response(handler(request.override(messages=trim_history(request.messages))))

    async def awrap_model_call(self, request, handler):
        return clean_response(await handler(request.override(messages=trim_history(request.messages))))
