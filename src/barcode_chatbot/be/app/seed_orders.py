"""Mock orders for demos and load tests: 2-5 random in-stock products per order, quantity 1-4.

`migrate` inserts them with the demo data (SEED_DEMO=true), so a fresh database has them too. Fixed random seed:
every run builds the same orders. ON CONFLICT DO NOTHING: an existing (or edited) order is never overwritten;
use --clean to rebuild them.

Usage:
    cd src/barcode_chatbot/be
    uv run python -m app.seed_orders                    # insert the missing ones
    uv run python -m app.seed_orders --clean            # delete SO-2xxxxx orders, then insert again
    uv run python -m app.seed_orders --count 300 --seed 7 --sql-out mock_orders.sql
"""
import argparse
import datetime
import os
import random
import sys
from pathlib import Path

import psycopg
from psycopg.rows import tuple_row

COUNT = 250
SEED = 42
START_ID = 200001
MOCK_ID = r'^SO-2[0-9]{5}$'  # only the generated shape: LIKE 'SO-2%' would also match real ids such as SO-2024-01

HO = ["Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Huỳnh", "Phan", "Vũ", "Võ",
      "Đặng", "Bùi", "Đỗ", "Hồ", "Ngô", "Dương", "Lý", "Đào", "Đinh", "Đoàn"]
DEM = ["Văn", "Thị", "Hữu", "Đức", "Minh", "Hải", "Thu", "Thanh", "Đình",
       "Ngọc", "Quang", "Tuấn", "Quốc", "Hoàng", "Trọng", "Xuân", "Kim", "Bảo"]
TEN = ["An", "Bình", "Cường", "Dũng", "Giang", "Hà", "Hải", "Hiếu", "Hùng", "Hương",
       "Khánh", "Khoa", "Kiên", "Lâm", "Linh", "Long", "Mai", "Nam", "Nghĩa",
       "Nhân", "Phát", "Phong", "Phúc", "Quân", "Sơn", "Tâm", "Thảo", "Thắng",
       "Trang", "Trí", "Trung", "Tuấn", "Tùng", "Vy", "Yên"]
STATUSES = ["pending", "pending", "pending", "packing", "completed"]


def generate(skus: list[str], count: int = COUNT, seed: int = SEED, start_id: int = START_ID):
    rng = random.Random(seed)  # own generator: same orders on every run, whatever else uses `random`
    now = datetime.datetime.now(datetime.timezone.utc)
    orders, items = [], []
    for n in range(start_id, start_id + count):
        order_id = f"SO-{n}"
        orders.append((order_id, f"{rng.choice(HO)} {rng.choice(DEM)} {rng.choice(TEN)}", rng.choice(STATUSES),
                       now - datetime.timedelta(days=rng.uniform(0.1, 30.0))))
        for sku in rng.sample(skus, rng.randint(2, min(5, len(skus)))):
            items.append((order_id, sku, rng.choices([1, 2, 3, 4], weights=[55, 30, 10, 5])[0]))
    return orders, items


def insert(conn, count: int = COUNT, seed: int = SEED, clean: bool = False) -> tuple[list, list]:
    """Insert the mock orders in the caller's transaction. Only in-stock products: every order can be packed."""
    q = conn.cursor(row_factory=tuple_row)  # migrate's connection returns dict rows
    skus = [r[0] for r in q.execute("SELECT sku FROM products WHERE stock > 0 ORDER BY sku").fetchall()]
    if len(skus) < 2:
        raise RuntimeError("cần ít nhất 2 sản phẩm còn hàng để tạo đơn giả")
    if clean:
        conn.execute("DELETE FROM orders WHERE order_id ~ %s", (MOCK_ID,))  # order_items go with them (CASCADE)
    orders, items = generate(skus, count, seed)
    # an order already in the DB (maybe edited by hand) keeps its own lines: add none to it
    have = {r[0] for r in q.execute("SELECT order_id FROM orders WHERE order_id = ANY(%s)",
                                       ([o[0] for o in orders],)).fetchall()}
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO orders (order_id, customer, status, created_at) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (order_id) DO NOTHING", [o for o in orders if o[0] not in have])
        cur.executemany("INSERT INTO order_items (order_id, sku, quantity) VALUES (%s, %s, %s) "
                        "ON CONFLICT (order_id, sku) DO NOTHING", [i for i in items if i[0] not in have])
    return orders, items


def _sql(orders, items) -> str:
    q = lambda v: "'" + str(v).replace("'", "''") + "'"
    lines = ["-- Mock orders (app/seed_orders.py)", "BEGIN;"]
    lines += [f"INSERT INTO orders (order_id, customer, status, created_at) VALUES ({q(o)}, {q(c)}, {q(s)}, "
              f"{q(t.isoformat())}) ON CONFLICT (order_id) DO NOTHING;" for o, c, s, t in orders]
    lines += [f"INSERT INTO order_items (order_id, sku, quantity) VALUES ({q(o)}, {q(s)}, {int(n)}) "
              f"ON CONFLICT (order_id, sku) DO NOTHING;" for o, s, n in items]
    return "\n".join(lines + ["COMMIT;", ""])


def main():
    ap = argparse.ArgumentParser(description="Tạo đơn hàng giả (2-5 sản phẩm còn hàng mỗi đơn).")
    ap.add_argument("--count", type=int, default=COUNT)
    ap.add_argument("--seed", type=int, default=SEED, help="cùng seed = cùng dữ liệu")
    ap.add_argument("--clean", action="store_true", help="xóa đơn giả cũ (SO-2 + 5 chữ số) rồi tạo lại")
    ap.add_argument("--sql-out", default="", help="ghi thêm file SQL")
    args = ap.parse_args()
    if args.count < 1:
        sys.exit("--count phải >= 1")
    from dotenv import load_dotenv
    for parent in Path(__file__).resolve().parents:  # the repo's root .env (DATABASE_URL on 127.0.0.1:5433)
        if (parent / ".env").exists():
            load_dotenv(parent / ".env")
            break
    url = os.environ.get("DATABASE_URL") or sys.exit("chưa đặt DATABASE_URL (xem .env.example)")
    print("Database:", psycopg.conninfo.conninfo_to_dict(url).get("host"))  # never print the password
    with psycopg.connect(url) as conn:
        orders, items = insert(conn, args.count, args.seed, args.clean)
        conn.commit()
        total = conn.execute("SELECT count(*) FROM orders WHERE order_id ~ %s", (MOCK_ID,)).fetchone()[0]
    per = {}
    for o, *_ in items:
        per[o] = per.get(o, 0) + 1
    print(f"{len(orders)} đơn, {len(items)} dòng sản phẩm; trong DB có {total} đơn giả")
    for k in sorted(set(per.values())):
        print(f"  {k} sản phẩm: {sum(v == k for v in per.values())} đơn")
    if args.sql_out:
        Path(args.sql_out).write_text(_sql(orders, items), encoding="utf-8")
        print("Đã ghi", args.sql_out)


if __name__ == "__main__":
    main()
