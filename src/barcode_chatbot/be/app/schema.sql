-- Applied on every server start; every statement is idempotent.

CREATE TABLE IF NOT EXISTS products (
    sku      text PRIMARY KEY,
    barcode  text NOT NULL UNIQUE,
    name     text NOT NULL,
    category text NOT NULL DEFAULT '',
    price    numeric(12, 0) NOT NULL DEFAULT 0 CHECK (price >= 0),  -- VND
    stock    integer NOT NULL DEFAULT 0 CHECK (stock >= 0)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id   text PRIMARY KEY,
    customer   text NOT NULL DEFAULT '',
    status     text NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Expected content of an order's box: what the packer must scan.
CREATE TABLE IF NOT EXISTS order_items (
    order_id text NOT NULL REFERENCES orders ON DELETE CASCADE,
    sku      text NOT NULL REFERENCES products,
    quantity integer NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (order_id, sku)
);
