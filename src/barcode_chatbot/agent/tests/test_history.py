from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from bank_faqs.history import trim_history


def turn(i: int, tool_output: str = 'x' * 1000) -> list:
    call_id = f'call-{i}'
    return [
        HumanMessage(f'câu {i}'),
        AIMessage('', tool_calls=[{'name': 'search_faq', 'args': {'query': str(i)}, 'id': call_id}]),
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
                               AIMessage('', tool_calls=[{'name': 'search_faq', 'args': {}, 'id': 'c2'}]),
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
