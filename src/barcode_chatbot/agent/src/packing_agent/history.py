from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AnyMessage

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

class TrimHistoryMiddleware(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        return handler(request.override(messages=trim_history(request.messages)))

    async def awrap_model_call(self, request, handler):
        return await handler(request.override(messages=trim_history(request.messages)))
