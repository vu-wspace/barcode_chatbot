-- One key per retail code, whatever length it was scanned or stored in: a UPC-A item may be stored as 13 digits
-- (0 + UPC-A) while the scanner sends the 12-digit UPC-A, or a GTIN-14 with leading zeros. Numeric 8-14 digit codes
-- are zero-padded to GTIN-14; anything else is kept as is. Must match gtin() in server.py.
ALTER TABLE products ADD COLUMN IF NOT EXISTS gtin text
    GENERATED ALWAYS AS (CASE WHEN barcode ~ '^[0-9]{8,14}$' THEN lpad(barcode, 14, '0') ELSE barcode END) STORED;
-- Unique: '0012345678905' and '012345678905' are the same item, so two products cannot both claim it.
CREATE UNIQUE INDEX IF NOT EXISTS products_gtin_key ON products (gtin);
