"""Mock orders for demos and load tests: 2-5 random in-stock products per order, quantity 1-4.

Ids are DEMO-000001... on purpose: a prefix no real order uses, so a fake order can never stand in for a real one
(packed and sealed against fake contents) and --clean can never delete a real order.
`migrate` inserts them when SEED_MOCK_ORDERS=true (off by default). Fixed random seed: every run builds the same
orders (only created_at follows the clock). ON CONFLICT DO NOTHING: an existing or edited order is never overwritten;
use --clean to rebuild them.

Usage:
    cd src/barcode_chatbot/be
    uv run python -m app.seed_orders                    # insert the missing ones
    uv run python -m app.seed_orders --clean            # delete the DEMO- orders, then insert again
    uv run python -m app.seed_orders --count 300 --seed 7 --sql-out mock_orders.sql
"""
import argparse
import datetime
import logging
import os
import random
import sys
from pathlib import Path

import psycopg
from psycopg.rows import tuple_row

log = logging.getLogger(__name__)

COUNT = 250
MAX_COUNT = 999_999  # ids stay DEMO- + 6 digits, all reachable by --clean
SEED = 42
PREFIX = "DEMO-"
MOCK_ID = r'^DEMO-[0-9]{6}$'
LOCK = "SELECT pg_advisory_xact_lock(hashtext('barcode_chatbot.migrate'))"  # same key as migrate: never both at once

HO = ["Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Huỳnh", "Phan", "Vũ", "Võ",
      "Đặng", "Bùi", "Đỗ", "Hồ", "Ngô", "Dương", "Lý", "Đào", "Đinh", "Đoàn"]
DEM = ["Văn", "Thị", "Hữu", "Đức", "Minh", "Hải", "Thu", "Thanh", "Đình",
       "Ngọc", "Quang", "Tuấn", "Quốc", "Hoàng", "Trọng", "Xuân", "Kim", "Bảo"]
TEN = ["An", "Bình", "Cường", "Dũng", "Giang", "Hà", "Hải", "Hiếu", "Hùng", "Hương",
       "Khánh", "Khoa", "Kiên", "Lâm", "Linh", "Long", "Mai", "Nam", "Nghĩa",
       "Nhân", "Phát", "Phong", "Phúc", "Quân", "Sơn", "Tâm", "Thảo", "Thắng",
       "Trang", "Trí", "Trung", "Tuấn", "Tùng", "Vy", "Yên"]
STATUSES = ["pending", "pending", "pending", "packing", "completed"]


def generate(skus: list[str], count: int = COUNT, seed: int = SEED, now: datetime.datetime | None = None):
    if not 1 <= count <= MAX_COUNT:
        raise ValueError(f"count phải từ 1 đến {MAX_COUNT}")
    if len(skus) < 2:
        raise ValueError("cần ít nhất 2 sản phẩm còn hàng")
    rng = random.Random(seed)  # own generator: same orders on every run, whatever else uses `random`
    now = now or datetime.datetime.now(datetime.timezone.utc)
    orders, items = [], []
    for n in range(1, count + 1):
        order_id = f"{PREFIX}{n:06d}"
        orders.append((order_id, f"{rng.choice(HO)} {rng.choice(DEM)} {rng.choice(TEN)}", rng.choice(STATUSES),
                       now - datetime.timedelta(days=rng.uniform(0.1, 30.0))))
        for sku in rng.sample(skus, rng.randint(2, min(5, len(skus)))):
            items.append((order_id, sku, rng.choices([1, 2, 3, 4], weights=[55, 30, 10, 5])[0]))
    return orders, items


def insert(conn, count: int = COUNT, seed: int = SEED, clean: bool = False, strict: bool = True):
    """Insert the mock orders in the caller's transaction; returns (ids inserted, all generated orders, their items).
    Only in-stock products, so every order can be packed. strict=False (migrate): too few products in stock only
    skips the mock orders with a warning, it must not stop the stack from starting over demo data."""
    q = conn.cursor(row_factory=tuple_row)  # migrate's connection returns dict rows
    skus = [r[0] for r in q.execute("SELECT sku FROM products WHERE stock > 0 ORDER BY sku").fetchall()]
    if len(skus) < 2 and not strict:
        log.warning("bỏ qua đơn giả: kho còn dưới 2 sản phẩm còn hàng")
        return [], [], []
    orders, items = generate(skus, count, seed)
    if clean:
        q.execute("DELETE FROM orders WHERE order_id ~ %s", (MOCK_ID,))  # order_items go with them (CASCADE)
    # an order already in the DB (maybe edited by hand) keeps its own lines: add none to it
    have = {r[0] for r in q.execute("SELECT order_id FROM orders WHERE order_id = ANY(%s)",
                                    ([o[0] for o in orders],)).fetchall()}
    new = [o for o in orders if o[0] not in have]
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO orders (order_id, customer, status, created_at) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (order_id) DO NOTHING", new)
        cur.executemany("INSERT INTO order_items (order_id, sku, quantity) VALUES (%s, %s, %s) "
                        "ON CONFLICT (order_id, sku) DO NOTHING", [i for i in items if i[0] not in have])
    return [o[0] for o in new], orders, items


def to_sql(orders, items) -> str:
    q = lambda v: "'" + str(v).replace("'", "''") + "'"
    lines = ["-- Mock orders (app/seed_orders.py)", "BEGIN;", "SET LOCAL standard_conforming_strings = on;"]
    lines += [f"INSERT INTO orders (order_id, customer, status, created_at) VALUES ({q(o)}, {q(c)}, {q(s)}, "
              f"{q(t.isoformat())}) ON CONFLICT (order_id) DO NOTHING;" for o, c, s, t in orders]
    lines += [f"INSERT INTO order_items (order_id, sku, quantity) VALUES ({q(o)}, {q(s)}, {int(n)}) "
              f"ON CONFLICT (order_id, sku) DO NOTHING;" for o, s, n in items]
    return "\n".join(lines + ["COMMIT;", ""])


def main():
    ap = argparse.ArgumentParser(description="Tạo đơn hàng giả DEMO-xxxxxx (2-5 sản phẩm còn hàng mỗi đơn).")
    ap.add_argument("--count", type=int, default=COUNT, help=f"1-{MAX_COUNT}")
    ap.add_argument("--seed", type=int, default=SEED, help="cùng seed = cùng dữ liệu")
    ap.add_argument("--clean", action="store_true", help="xóa mọi đơn DEMO-xxxxxx rồi tạo lại")
    ap.add_argument("--sql-out", default="", help="ghi thêm file SQL")
    args = ap.parse_args()
    if not 1 <= args.count <= MAX_COUNT:
        sys.exit(f"--count phải từ 1 đến {MAX_COUNT}")
    from dotenv import load_dotenv
    for parent in Path(__file__).resolve().parents:  # the repo's root .env (DATABASE_URL on 127.0.0.1:5433)
        if (parent / ".env").exists():
            load_dotenv(parent / ".env")
            break
    url = os.environ.get("DATABASE_URL") or sys.exit("chưa đặt DATABASE_URL (xem .env.example)")
    print("Database:", psycopg.conninfo.conninfo_to_dict(url).get("host"))  # never print the password
    with psycopg.connect(url) as conn:
        conn.execute(LOCK)  # held until commit: a migrate running now waits, and the other way round
        new, orders, items = insert(conn, args.count, args.seed, args.clean)
        conn.commit()
        total = conn.execute("SELECT count(*) FROM orders WHERE order_id ~ %s", (MOCK_ID,)).fetchone()[0]
    print(f"Thêm {len(new)} đơn mới; trong DB có {total} đơn giả")
    if args.sql_out:
        Path(args.sql_out).write_text(to_sql(orders, items), encoding="utf-8")
        print("Đã ghi", args.sql_out)


if __name__ == "__main__":
    main()
