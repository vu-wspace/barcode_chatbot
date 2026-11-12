import json
import os
from urllib.parse import quote

import httpx
from langchain_core.tools import tool

# Tool không truy cập DB trực tiếp: gọi API của BE (BE chạy SQL), cùng API mà trang quét mã dùng.
BE_URL = os.getenv('BE_URL', 'http://localhost:8001')
def new_client() -> httpx.Client:
    return httpx.Client(base_url=BE_URL, timeout=10)

client = new_client()  # api.py tạo mới/đóng theo vòng đời app


def _call(method: str, path: str, not_found: str, **kwargs) -> str:
    try:
        r = client.request(method, path, **kwargs)
    except httpx.HTTPError as e:
        return f'Lỗi: không kết nối được hệ thống kho ({type(e).__name__}).'
    if r.status_code == 404:
        return not_found
    if r.is_error:
        return f'Lỗi: hệ thống kho trả về HTTP {r.status_code}.'
    try:
        return json.dumps(r.json(), ensure_ascii=False, default=str)
    except ValueError:  # 200 nhưng body không phải JSON (proxy trả trang lỗi HTML...)
        return 'Lỗi: hệ thống kho trả về dữ liệu không đọc được.'


@tool
def get_product(code: str) -> str:
    """Tra 1 sản phẩm theo mã vạch (barcode) hoặc SKU. Trả về sku, barcode, tên, danh mục, giá (VND), tồn kho."""
    return _call('GET', f'/products/{quote(code.strip(), safe="")}', f'Không có sản phẩm nào với mã "{code}".')


@tool
def search_products(query: str = '') -> str:
    """Tìm sản phẩm theo tên, SKU, mã vạch hoặc danh mục (khớp một phần, không phân biệt hoa thường).
    Để trống query để liệt kê tất cả sản phẩm."""
    return _call('GET', '/products', 'Không tìm thấy sản phẩm.', params={'q': query.strip()})


@tool
def get_order(order_id: str) -> str:
    """Xem đơn hàng: trạng thái và danh sách sản phẩm cần đóng gói (sku, tên, mã vạch, số lượng)."""
    out = _call('GET', f'/orders/{quote(order_id.strip(), safe="")}', f'Không có đơn hàng "{order_id}".')
    try:  # tên khách không gửi cho LLM (dữ liệu cá nhân, tới 5 hãng); màn hình đơn đã hiện tên khách
        order = json.loads(out)
    except ValueError:
        return out  # thông báo lỗi / không có đơn
    order.pop('customer', None)
    return json.dumps(order, ensure_ascii=False)


@tool
def verify_order(order_id: str, barcodes: list[str]) -> str:
    """Kiểm tra hàng đã quét có khớp đơn không. barcodes: mọi mã đã quét, mỗi lần quét 1 phần tử (được lặp lại).
    Trả về số đã quét từng sản phẩm, missing (còn thiếu), extra (thừa/sai), unknown (mã không có trong kho),
    complete (true = đúng và đủ)."""
    return _call('POST', f'/orders/{quote(order_id.strip(), safe="")}/verify', f'Không có đơn hàng "{order_id}".',
                 json={'barcodes': [b.strip() for b in barcodes if b.strip()]})


TOOLS = [get_product, search_products, get_order, verify_order]
