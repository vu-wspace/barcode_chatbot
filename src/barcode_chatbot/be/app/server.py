"""Backend: products and orders in PostgreSQL, used by the scanner page and by the AI agent's tools.

    uv run uvicorn app.server:app --host 0.0.0.0 --port 8000
    uv run python -m app.server check   # self-check against DATABASE_URL (needs the demo data: SEED_DEMO=true)

Needs DATABASE_URL, e.g. postgresql://postgres:<password>@localhost:5432/barcode_chatbot
"""
import os
import sys
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

load_dotenv()
HERE = Path(__file__).parent
SEED_DEMO = os.getenv("SEED_DEMO", "false").lower() == "true"

Code = Annotated[str, Field(min_length=1, max_length=64)]

PRODUCT_COLS = "sku, barcode, name, category, price, stock"

pool = ConnectionPool(os.getenv("DATABASE_URL", ""), open=False, min_size=1, max_size=10,
                      kwargs={"row_factory": dict_row})


MIGRATIONS = HERE / "migrations"


def migrate(conn, folder: Path = MIGRATIONS) -> list[str]:
    """Run migrations/NNN_name.sql files not yet in schema_migrations, in name order; returns the names run.

    All in one transaction: a failing file rolls back every file of this run, so the DB is never half-migrated.
    The advisory lock makes BE containers that start together wait for each other instead of running a file twice.
    Never edit a file that has been applied: add a new one. shortcut: no checksum detects an edited file.
    """
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('barcode_chatbot.migrate'))")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                     "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
        ran = []
        for f in sorted(folder.glob("*.sql")):
            if f.name not in done:
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
                ran.append(f.name)
        return ran


@asynccontextmanager
async def lifespan(_):
    if not os.getenv("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is not set (add it to .env)")
    pool.open(wait=True, timeout=30)
    try:
        with pool.connection() as conn:
            ran = migrate(conn)
            if ran:
                print("applied migrations:", ", ".join(ran), flush=True)
            if SEED_DEMO:
                conn.execute((HERE / "seed.sql").read_text(encoding="utf-8"))
        yield
    finally:
        pool.close()  # also when a migration fails, so the process exits cleanly


app = FastAPI(title="Barcode chatbot BE", lifespan=lifespan)


@app.get("/health")
def health():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/products")
def search_products(q: str = Query("", max_length=100), limit: int = Query(20, ge=1, le=100)):
    """Search by name, SKU, barcode or category (case-insensitive substring). Empty q lists everything."""
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with pool.connection() as conn:
        return conn.execute(
            f"SELECT {PRODUCT_COLS} FROM products WHERE name ILIKE %(l)s OR sku ILIKE %(l)s OR barcode LIKE %(l)s"
            " OR category ILIKE %(l)s ORDER BY sku LIMIT %(n)s", {"l": like, "n": limit}).fetchall()


@app.get("/products/{code}")
def get_product(code: str):
    """One product by barcode or SKU. A code that is one product's barcode and another's SKU means the barcode."""
    with pool.connection() as conn:
        row = conn.execute(f"SELECT {PRODUCT_COLS} FROM products WHERE barcode = %(c)s OR sku = %(c)s"
                           " ORDER BY barcode = %(c)s DESC LIMIT 1", {"c": code}).fetchone()
    if row is None:
        raise HTTPException(404, "unknown product")
    return row


def load_order(conn, order_id):
    order = conn.execute("SELECT order_id, customer, status, created_at FROM orders WHERE order_id = %s",
                         (order_id,)).fetchone()
    if order is None:
        raise HTTPException(404, "unknown order")
    order["items"] = conn.execute(
        "SELECT p.sku, p.barcode, p.name, i.quantity FROM order_items i JOIN products p USING (sku)"
        " WHERE i.order_id = %s ORDER BY p.sku", (order_id,)).fetchall()
    return order


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    with pool.connection() as conn:
        return load_order(conn, order_id)


class Scans(BaseModel):
    barcodes: list[Code] = Field(max_length=500)  # one entry per scanned unit; a barcode may repeat


def compare(expected: dict[str, int], scanned: dict[str, int]):
    """Expected vs scanned counts per SKU -> (missing, extra). Extra = over the quantity or not in the order."""
    missing = {s: q - scanned.get(s, 0) for s, q in expected.items() if scanned.get(s, 0) < q}
    extra = {s: n - expected.get(s, 0) for s, n in scanned.items() if n > expected.get(s, 0)}
    return missing, extra


@app.post("/orders/{order_id}/verify")
def verify_order(order_id: str, body: Scans):
    """Compare the scanned barcodes (or SKUs) against the order. Read-only: nothing is stored."""
    with pool.connection() as conn:
        order = load_order(conn, order_id)
        rows = conn.execute("SELECT sku, barcode FROM products WHERE barcode = ANY(%(c)s) OR sku = ANY(%(c)s)",
                            {"c": list(set(body.barcodes))}).fetchall()
    to_sku = {r["sku"]: r["sku"] for r in rows} | {r["barcode"]: r["sku"] for r in rows}  # barcode wins, as in get_product
    scanned = Counter(to_sku[c] for c in body.barcodes if c in to_sku)
    expected = {i["sku"]: i["quantity"] for i in order["items"]}
    missing, extra = compare(expected, scanned)
    return {
        "order_id": order_id,
        "items": [{**i, "scanned": scanned.get(i["sku"], 0)} for i in order["items"]],
        "missing": missing,
        "extra": extra,
        "unknown": sorted({c for c in body.barcodes if c not in to_sku}),
        "complete": not missing and not extra and all(c in to_sku for c in body.barcodes),
    }


def check_migrations():
    """migrate() on a scratch schema: runs in order, skips applied files, a failing file rolls back the whole run."""
    import tempfile

    pool.open(wait=True, timeout=30)
    with tempfile.TemporaryDirectory() as d, pool.connection() as conn:
        d = Path(d)
        conn.execute("DROP SCHEMA IF EXISTS migrate_check CASCADE")
        conn.execute("CREATE SCHEMA migrate_check")
        conn.execute("SET search_path TO migrate_check")
        conn.commit()
        try:
            (d / "001_a.sql").write_text("CREATE TABLE t (id int)", encoding="utf-8")
            (d / "002_b.sql").write_text("ALTER TABLE t ADD COLUMN v text", encoding="utf-8")
            assert migrate(conn, d) == ["001_a.sql", "002_b.sql"]
            assert migrate(conn, d) == []  # already applied: nothing runs twice
            (d / "003_c.sql").write_text("ALTER TABLE t ADD COLUMN w int", encoding="utf-8")
            (d / "004_bad.sql").write_text("ALTER TABLE nope ADD COLUMN x int", encoding="utf-8")
            try:
                migrate(conn, d)
                raise AssertionError("a failing migration must raise")
            except Exception as e:
                assert "nope" in str(e), e
            cols = [r["column_name"] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'migrate_check'"
                " AND table_name = 't' ORDER BY ordinal_position")]
            assert cols == ["id", "v"], cols  # 003 rolled back together with 004
            assert [r["name"] for r in conn.execute("SELECT name FROM schema_migrations ORDER BY name")] == \
                ["001_a.sql", "002_b.sql"]
        finally:
            conn.rollback()
            conn.execute("RESET search_path")
            conn.execute("DROP SCHEMA migrate_check CASCADE")
            conn.commit()


def check():
    """Self-check through the HTTP API against DATABASE_URL, using the demo data (SEED_DEMO=true)."""
    from fastapi.testclient import TestClient

    assert compare({"A": 2, "B": 1}, {"A": 1, "C": 1}) == ({"A": 1, "B": 1}, {"C": 1})
    assert compare({"A": 1}, {"A": 3}) == ({}, {"A": 2})
    check_migrations()
    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok"}
        p = c.get("/products/8935000100011").json()
        assert p["sku"] == "HD-1001" and c.get("/products/HD-1001").json() == p, p
        assert c.get("/products/0000000000000").status_code == 404
        assert [r["sku"] for r in c.get("/products", params={"q": "cáp"}).json()] == ["CB-3001"]
        assert c.get("/products", params={"q": "%"}).json() == []  # LIKE wildcards are matched literally
        assert len(c.get("/orders/SO-104587").json()["items"]) == 3
        assert c.get("/orders/NOPE").status_code == 404

        v = c.post("/orders/SO-104587/verify", json={"barcodes": ["8935000100011", "CH-2001", "8935000300015"]}).json()
        assert v["complete"] and v["missing"] == {} and all(i["scanned"] == 1 for i in v["items"]), v
        v = c.post("/orders/SO-104587/verify", json={"barcodes": ["8935000100011", "8935000100011", "8935000400012",
                                                                  "123"]}).json()
        assert not v["complete"], v
        assert v["missing"] == {"CH-2001": 1, "CB-3001": 1} and v["extra"] == {"HD-1001": 1, "MS-4001": 1}, v
        assert v["unknown"] == ["123"], v
        assert c.post("/orders/NOPE/verify", json={"barcodes": []}).status_code == 404

        # a code that is one product's barcode and another product's SKU resolves to the barcode
        with pool.connection() as conn:
            conn.execute("INSERT INTO products (sku, barcode, name) VALUES ('8935000100011', 'CHECK-COLLIDE', 'x')")
        try:
            assert c.get("/products/8935000100011").json()["sku"] == "HD-1001"
            v = c.post("/orders/SO-104587/verify", json={"barcodes": ["8935000100011"]}).json()
            assert v["items"][2]["sku"] == "HD-1001" and v["items"][2]["scanned"] == 1, v
        finally:
            with pool.connection() as conn:
                conn.execute("DELETE FROM products WHERE barcode = 'CHECK-COLLIDE'")
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        check()
    else:
        print(__doc__)
