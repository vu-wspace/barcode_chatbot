"""seed_orders without a database: a fake connection records the SQL it gets."""
import datetime
import re

import pytest

from app import seed_orders as so

SKUS = ['CB-3001', 'CH-2001', 'HD-1001', 'MS-4001', 'SALONPAS']
NOW = datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc)


def test_generate_is_deterministic_and_well_formed():
    a = so.generate(SKUS, 250, now=NOW)
    assert a == so.generate(SKUS, 250, now=NOW)            # same seed and clock: identical, created_at included
    assert a[1] != so.generate(SKUS, 250, seed=7, now=NOW)[1]
    orders, items = a
    assert [o[0] for o in orders] == [f'DEMO-{n:06d}' for n in range(1, 251)]
    per = {}
    for oid, sku, qty in items:
        assert sku in SKUS and 1 <= qty <= 4
        per.setdefault(oid, []).append(sku)
    assert all(2 <= len(v) <= 5 and len(set(v)) == len(v) for v in per.values())
    assert {o[2] for o in orders} <= {'pending', 'packing', 'completed'}


def test_mock_ids_can_never_be_real_order_ids():
    mock = re.compile(so.MOCK_ID)  # Python re stands in for Postgres ~ here: same syntax for this pattern
    assert mock.match('DEMO-000001') and mock.match('DEMO-999999')
    for real in ('SO-104587', 'SO-200001', 'SO-2024-01', 'X55277605433', 'DEMO-1', 'DEMO-0000001'):
        assert not mock.match(real), real
    with pytest.raises(ValueError):
        so.generate(SKUS, so.MAX_COUNT + 1)                 # ids would leave the --clean pattern


def test_sql_export_escapes_quotes():
    sql = so.to_sql([("DEMO-000001", "O'Brien", 'pending', NOW)], [("DEMO-000001", "X'Y", 2)])
    assert "'O''Brien'" in sql and "'X''Y'" in sql and "standard_conforming_strings" in sql
    lines = sql.strip().splitlines()
    assert lines[1] == 'BEGIN;' and lines[-1] == 'COMMIT;'
    assert all(l.endswith('DO NOTHING;') for l in lines if l.startswith('INSERT'))  # never overwrites a row


class FakeConn:
    def __init__(self, skus, existing=()):
        self.skus, self.existing, self.sql, self.many = skus, set(existing), [], {}

    def cursor(self, row_factory=None):
        return self

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, q, params=None):
        self.sql.append(q)
        self.params = getattr(self, 'params', []) + [params]
        self.rows = ([(s,) for s in self.skus] if 'FROM products' in q
                     else [(i,) for i in params[0] if i in self.existing] if 'ANY' in q else [])
        return self

    def fetchall(self): return self.rows

    def executemany(self, q, rows):
        self.many['orders' if 'INTO orders' in q else 'items'] = list(rows)


def test_insert_uses_in_stock_products_and_leaves_existing_orders_alone():
    conn = FakeConn(SKUS, existing={'DEMO-000002'})
    new, orders, items = so.insert(conn, count=3)
    assert 'stock > 0' in conn.sql[0] and not any('DELETE' in q for q in conn.sql)
    assert new == ['DEMO-000001', 'DEMO-000003']
    assert [o[0] for o in conn.many['orders']] == new
    assert {i[0] for i in conn.many['items']} == set(new)  # an existing (maybe edited) order gets no new lines


def test_clean_deletes_only_mock_ids():
    conn = FakeConn(SKUS)
    so.insert(conn, count=2, clean=True)
    i = [n for n, q in enumerate(conn.sql) if 'DELETE' in q]
    assert [conn.sql[n] for n in i] == ["DELETE FROM orders WHERE order_id ~ %s"]
    assert conn.params[i[0]] == (so.MOCK_ID,)               # the pattern actually sent, not just the SQL text


def test_too_few_products_in_stock():
    with pytest.raises(ValueError):
        so.insert(FakeConn(['HD-1001']))                    # CLI: an error
    conn = FakeConn(['HD-1001'])
    assert so.insert(conn, strict=False) == ([], [], [])    # migrate: skipped, the stack still starts
    assert conn.many == {}


@pytest.mark.parametrize('flag', [False, True])
def test_mock_orders_only_when_seed_mock_orders_is_on(monkeypatch, flag):
    from app import server
    calls = []
    monkeypatch.setattr(server, 'migrate', lambda conn: [])
    monkeypatch.setattr(server, 'provision_roles', lambda conn: None)
    monkeypatch.setattr(server.seed_orders, 'insert', lambda conn, **kw: calls.append(kw) or (['DEMO-000001'], [], []))
    monkeypatch.setattr(server, 'SEED_DEMO', True)
    monkeypatch.setattr(server, 'SEED_MOCK_ORDERS', flag)

    class Conn:
        def execute(self, *a, **k): return self
        def commit(self): pass
        def rollback(self): pass
    server._setup(Conn())
    assert calls == ([{'strict': False}] if flag else [])


def test_seed_mock_orders_is_off_by_default(monkeypatch):
    import importlib
    from app import server
    monkeypatch.delenv('SEED_MOCK_ORDERS', raising=False)
    monkeypatch.setattr('dotenv.load_dotenv', lambda *a, **k: False)  # the developer's own .env may turn it on
    try:
        assert importlib.reload(server).SEED_MOCK_ORDERS is False
    finally:
        monkeypatch.undo()
        importlib.reload(server)



@pytest.mark.parametrize('url,ok', [
    ('postgresql://postgres:x@127.0.0.1:5433/barcode_chatbot', True),
    ('postgresql://postgres:x@db:5432/barcode_chatbot', True),
    ('postgresql://postgres:x@localhost/barcode_chatbot', True),
    ('postgresql:///barcode_chatbot', True),                                  # local socket
    ('postgresql://postgres:x@prod-db.example.com/barcode_chatbot', False),
    ('postgresql://postgres:x@10.0.0.5:5432/barcode_chatbot', False),
])
def test_self_check_refuses_a_remote_database(monkeypatch, url, ok):
    from app import server
    monkeypatch.delenv('ALLOW_SELF_CHECK', raising=False)
    if ok:
        server.check_target_is_local(url)
    else:
        with pytest.raises(SystemExit):
            server.check_target_is_local(url)
        monkeypatch.setenv('ALLOW_SELF_CHECK', '1')
        server.check_target_is_local(url)                                     # explicit opt-in


def test_mock_orders_seeding_warns(monkeypatch, capsys):
    from app import server
    monkeypatch.setattr(server, 'migrate', lambda conn: [])
    monkeypatch.setattr(server, 'provision_roles', lambda conn: None)
    monkeypatch.setattr(server.seed_orders, 'insert', lambda conn, **kw: (['DEMO-000001'], [1, 2], []))
    monkeypatch.setattr(server, 'SEED_DEMO', False)
    monkeypatch.setattr(server, 'SEED_MOCK_ORDERS', True)

    class Conn:
        def execute(self, *a, **k): return self
        def commit(self): pass
        def rollback(self): pass
    server._setup(Conn())
    assert 'SEED_MOCK_ORDERS=true: 1 new fake orders' in capsys.readouterr().err


def test_check_starts_with_the_local_database_guard(monkeypatch):
    from app import server
    monkeypatch.setenv('DATABASE_URL', 'postgresql://postgres:x@prod-db.example.com/barcode_chatbot')
    monkeypatch.delenv('ALLOW_SELF_CHECK', raising=False)
    monkeypatch.setattr(server, 'check_migrations', lambda: pytest.fail('check() touched the database before the guard'))
    with pytest.raises(SystemExit):
        server.check()
