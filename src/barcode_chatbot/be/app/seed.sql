-- Demo data, applied at start when SEED_DEMO=true. ON CONFLICT DO NOTHING: never overwrites edited rows.
-- Barcodes are valid EAN-13 (the scanner rejects a wrong check digit).

INSERT INTO products (sku, barcode, name, category, price, stock) VALUES
    ('HD-1001', '8935000100011', 'Tai nghe chụp tai Bluetooth', 'Âm thanh', 890000, 25),
    ('CH-2001', '8935000200018', 'Củ sạc nhanh USB-C 20W', 'Phụ kiện', 250000, 60),
    ('CB-3001', '8935000300015', 'Cáp USB-C 1m', 'Phụ kiện', 90000, 120),
    ('MS-4001', '8935000400012', 'Chuột không dây', 'Máy tính', 320000, 40),
    ('KB-5001', '8935000500019', 'Bàn phím cơ', 'Máy tính', 1150000, 0),
    ('SALONPAS', '8935106211321', 'Miếng dán Salonpas', 'Y tế', 35000, 200),
    ('CLASSMATE-NB', '8936212227992', 'Vở ClassMate', 'Văn phòng phẩm', 15000, 150)
ON CONFLICT DO NOTHING;

-- Order lines go only to the demo orders this statement created: an order with the same id that already exists
-- (a real one, or a demo order edited by hand) never gets demo lines added to its checklist.
WITH new AS (
    INSERT INTO orders (order_id, customer) VALUES
        ('SO-104587', 'Nguyễn Văn A'),
        ('SO-104588', 'Trần Thị B'),
        -- box X55277605433 from the old barcode_cv system (mock_checklist.json / its boxes + checklist_items tables)
        ('X55277605433', '')
    ON CONFLICT DO NOTHING
    RETURNING order_id
)
INSERT INTO order_items (order_id, sku, quantity)
SELECT v.order_id, v.sku, v.quantity
FROM (VALUES
    ('SO-104587', 'HD-1001', 1),
    ('SO-104587', 'CH-2001', 1),
    ('SO-104587', 'CB-3001', 1),
    ('SO-104588', 'MS-4001', 2),
    ('SO-104588', 'SALONPAS', 3),
    ('X55277605433', 'SALONPAS', 1),
    ('X55277605433', 'CLASSMATE-NB', 3)
) AS v(order_id, sku, quantity)
JOIN new USING (order_id);
