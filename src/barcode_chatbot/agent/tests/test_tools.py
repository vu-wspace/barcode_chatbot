import json

import httpx
import pytest

from packing_agent import tools


@pytest.fixture
def be(monkeypatch):
    """BE giả: ghi lại request, trả response theo đường dẫn."""
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.url.path == '/products/8935000100011':
            return httpx.Response(200, json={'sku': 'HD-1001', 'name': 'Tai nghe', 'price': 890000})
        if req.url.path == '/orders/SO-1/verify':
            return httpx.Response(200, json={'complete': False, 'missing': {'CB-3001': 1}})
        if req.url.path == '/boom':
            return httpx.Response(500)
        return httpx.Response(404, json={'detail': 'not found'})

    monkeypatch.setattr(tools, 'client', httpx.Client(base_url='http://be', transport=httpx.MockTransport(handler)))
    return seen


def test_get_product_returns_json_with_vietnamese(be):
    out = tools.get_product.invoke({'code': ' 8935000100011 '})
    assert json.loads(out)['sku'] == 'HD-1001'
    assert 'Tai nghe' in out  # ensure_ascii=False: model đọc được tiếng Việt


def test_not_found_is_a_message_not_an_exception(be):
    assert 'Không có sản phẩm' in tools.get_product.invoke({'code': '123'})
    assert 'Không có đơn hàng' in tools.get_order.invoke({'order_id': 'X'})


def test_path_is_escaped(be):
    tools.get_order.invoke({'order_id': '../products/1'})
    assert be[-1].url.raw_path == b'/orders/..%2Fproducts%2F1'  # không thoát ra endpoint khác của BE


def test_verify_order_sends_all_scans_including_repeats(be):
    out = tools.verify_order.invoke({'order_id': 'SO-1', 'barcodes': ['A', ' A ', '', 'B']})
    assert json.loads(be[-1].content) == {'barcodes': ['A', 'A', 'B']}
    assert json.loads(out)['missing'] == {'CB-3001': 1}


def test_search_passes_query(be):
    tools.search_products.invoke({'query': ' cáp '})
    assert be[-1].url.params['q'] == 'cáp'


def test_server_error_and_connection_error(be, monkeypatch):
    assert 'HTTP 500' in tools._call('GET', '/boom', 'x')

    def down(req):
        raise httpx.ConnectError('refused')
    monkeypatch.setattr(tools, 'client', httpx.Client(base_url='http://be', transport=httpx.MockTransport(down)))
    assert 'không kết nối được' in tools.get_product.invoke({'code': '1'})


def test_get_order_hides_the_customer_name(monkeypatch):
    order = {'order_id': 'SO-1', 'customer': 'Nguyễn Văn A', 'phone': '0901', 'status': 'pending', 'items': []}
    monkeypatch.setattr(tools, 'client', httpx.Client(base_url='http://be', transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=order))))
    out = tools.get_order.invoke({'order_id': 'SO-1'})
    assert 'Nguyễn' not in out and '0901' not in out and json.loads(out)['order_id'] == 'SO-1'
