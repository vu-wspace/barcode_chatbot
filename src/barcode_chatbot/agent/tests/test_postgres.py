"""Postgres-only paths of api.py: advisory lock, checkpoint pruning, retention.

Runs only with TEST_DATABASE_URL (a role that can create a schema), e.g. the compose DB:
    TEST_DATABASE_URL=postgresql://postgres:<POSTGRES_PASSWORD>@127.0.0.1:5433/barcode_chatbot uv run pytest tests/test_postgres.py
Everything happens in a scratch schema `test_agent`, dropped at the end.
"""
import asyncio
import json
import os
import uuid
from types import SimpleNamespace

import psycopg
import pytest
from langchain_core.messages import AIMessage
from psycopg.rows import dict_row

from packing_agent import agent as agent_module
from packing_agent import api
from tests.test_api import FakeModel

URL = os.getenv('TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not URL, reason='set TEST_DATABASE_URL to run the Postgres tests')
SCHEMA = 'test_agent'


def run(fn):
    # psycopg async cannot use Windows' default ProactorEventLoop
    return asyncio.run(fn(), loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(autouse=True)
def scratch_schema():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS {SCHEMA} CASCADE')
        c.execute(f'CREATE SCHEMA {SCHEMA}')
    yield
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute(f'DROP SCHEMA {SCHEMA} CASCADE')


async def make_app(*replies):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool
    pool = AsyncConnectionPool(URL, open=False, kwargs={'autocommit': True, 'prepare_threshold': 0, 'row_factory': dict_row,
                                                        'options': f'-c search_path={SCHEMA}'})
    await pool.open()
    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    agent_module.open_llm = FakeModel(messages=iter(replies), disable_streaming=True)
    agent = agent_module.build_agent(checkpointer=saver, fallback=False)
    return SimpleNamespace(state=SimpleNamespace(agent=agent, saver=saver, pool=pool)), pool


async def turn(app, thread_id, message='hi'):
    q = asyncio.Queue()
    await api.run_turn(app, api.ChatRequest(message=message), thread_id, str(uuid.uuid4()), q)
    events = []
    while (item := q.get_nowait()) is not None:
        lines = item.strip().splitlines()
        events.append((lines[0].removeprefix('event: '), json.loads(lines[1].removeprefix('data: '))))
    return events


async def count(pool, sql, *args):
    async with pool.connection() as c:
        return (await (await c.execute(sql, args)).fetchone())['n']


def test_turn_waits_for_advisory_lock_held_by_another_process():
    async def go():
        app, pool = await make_app(AIMessage('x'))
        tid = str(uuid.uuid4())
        try:
            # another worker/replica is running a turn of this thread
            async with await psycopg.AsyncConnection.connect(URL) as other, other.transaction():
                await other.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', (tid,))
                ev = await turn(app, tid)
            assert ev[-1][0] == 'error' and ev[-1][1]['status'] == 409, ev
            assert (await turn(app, tid))[-1] == ('done', {'thread_id': tid, 'answer': 'x', 'tools': []})  # lock gone
        finally:
            await pool.close()
    run(go)


def test_only_latest_checkpoint_is_kept():
    async def go():
        app, pool = await make_app(*[AIMessage(f'a{i}') for i in range(3)])
        tid = str(uuid.uuid4())
        try:
            for i in range(3):
                assert (await turn(app, tid, f'q{i}'))[-1][0] == 'done'
            assert await count(pool, 'SELECT count(*) AS n FROM checkpoints WHERE thread_id = %s', tid) == 1
            state = await app.state.agent.aget_state({'configurable': {'thread_id': tid}})
            assert [m.text for m in state.values['messages']][-2:] == ['q2', 'a2']  # pruning kept the latest state
        finally:
            await pool.close()
    run(go)


def test_delete_old_threads_removes_only_inactive_ones():
    async def go():
        app, pool = await make_app(AIMessage('old'), AIMessage('new'))
        old, new = str(uuid.uuid4()), str(uuid.uuid4())
        try:
            await turn(app, old)
            await turn(app, new)
            async with pool.connection() as c:
                await c.execute("UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{ts}', to_jsonb(%s::text))"
                                " WHERE thread_id = %s", ('2020-01-01T00:00:00+00:00', old))
            assert await api.delete_old_threads(app.state.saver, pool) == 1
            assert await count(pool, 'SELECT count(*) AS n FROM checkpoints WHERE thread_id = %s', old) == 0
            assert await count(pool, 'SELECT count(*) AS n FROM checkpoints WHERE thread_id = %s', new) == 1
        finally:
            await pool.close()
    run(go)
