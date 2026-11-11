from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from packing_agent.history import trim_history


def turn(i: int, tool_output: str = 'x' * 1000) -> list:
    call_id = f'call-{i}'
    return [
        HumanMessage(f'câu {i}'),
        AIMessage('', tool_calls=[{'name': 'get_product', 'args': {'query': str(i)}, 'id': call_id}]),
        ToolMessage(tool_output, tool_call_id=call_id),
        AIMessage(f'trả lời {i}'),
    ]


def test_keeps_only_last_turns():
    msgs = [m for i in range(6) for m in turn(i)]
    out = trim_history(msgs, keep_turns=4)
    humans = [m.content for m in out if m.type == 'human']
    assert humans == ['câu 2', 'câu 3', 'câu 4', 'câu 5']
    assert out[0].type == 'human'  # không mở đầu bằng ToolMessage mồ côi


def test_shortens_old_tool_outputs_but_not_current_turn():
    msgs = turn(0) + turn(1) + [HumanMessage('câu 2'),
                               AIMessage('', tool_calls=[{'name': 'get_product', 'args': {}, 'id': 'c2'}]),
                               ToolMessage('y' * 1000, tool_call_id='c2')]
    out = trim_history(msgs, old_tool_chars=300)
    tools = [m for m in out if m.type == 'tool']
    assert all(len(t.content) < 350 and t.content.endswith('(đã rút gọn)') for t in tools[:2])
    assert tools[2].content == 'y' * 1000  # kết quả tool của lượt đang trả lời giữ nguyên


def test_does_not_mutate_state_messages():
    msgs = turn(0) + [HumanMessage('câu 1')]
    trim_history(msgs, old_tool_chars=10)
    assert msgs[2].content == 'x' * 1000


def test_short_history_unchanged():
    msgs = turn(0, tool_output='ngắn')
    assert trim_history(msgs) == msgs
    assert trim_history([]) == []


def test_strip_final_a():
    from packing_agent.prompts import strip_final_a
    assert strip_final_a('Dạ, còn 25 cái ạ.') == 'Dạ, còn 25 cái.'
    assert strip_final_a('Hết hàng rồi anh/chị ạ!\nAnh/chị đợi nha ạ') == 'Hết hàng rồi anh/chị!\nAnh/chị đợi nha'
    assert strip_final_a('Dạ, cảm ơn anh/chị, ạ.') == 'Dạ, cảm ơn anh/chị.'
    # words that merely contain ạ, and a mid-sentence ạ, stay
    keep = 'Dạ, hạn sử dụng còn lại 3 tháng, mạng ổn. Anh/chị ạ, em kiểm tra rồi.'
    assert strip_final_a(keep) == keep


def test_middleware_cleans_stored_answer():
    from langchain_core.messages import AIMessage
    from packing_agent.history import clean_response
    from langchain.agents.middleware.types import ModelResponse
    r = clean_response(ModelResponse(result=[AIMessage('Dạ, đủ hàng ạ.')]))
    assert r.result[0].content == 'Dạ, đủ hàng.'
    blocks = clean_response(AIMessage([{'type': 'text', 'text': 'Còn 2 cái ạ.'}]))
    assert blocks.content[0]['text'] == 'Còn 2 cái.'
    calls = clean_response(AIMessage('', tool_calls=[{'name': 'get_product', 'args': {}, 'id': 'c'}]))
    assert calls.tool_calls[0]['name'] == 'get_product'
