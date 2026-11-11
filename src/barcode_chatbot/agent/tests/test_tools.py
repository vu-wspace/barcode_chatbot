import pytest

from bank_faqs import tools


@pytest.mark.parametrize('name, expected', [
    ('BIDV', 'BIDV'),
    ('vcb', 'Vietcombank'),
    ('Vietin', 'VietinBank'),
    ('mb bank', 'MBBank'),
    ('Bắc Á', 'Bắc Á Bank'),
    ('bac a bank', 'Bắc Á Bank'),
    ('ngân hàng Techcom', 'Techcombank'),
    ('Ngân hàng Nam Á', 'Nam Á Bank'),
    ('nông nghiệp', 'Agribank'),
    ('acb', 'ACB'),
])
def test_find_bank(name, expected):
    assert list(tools._find_bank(tools._rates(), name)['Mã NH']) == [expected]


@pytest.mark.parametrize('name', ['a', 'ab', 'TPBank', 'xyz'])
def test_find_bank_not_found(name):
    assert tools._find_bank(tools._rates(), name).empty


def test_norm():
    assert tools._norm('Ngân hàng Đông Á!') == 'nganhangdonga'


def test_interest_rate_one_bank():
    out = tools.get_interest_rate.invoke({'bank': 'BIDV'})
    assert out.startswith('BIDV (cập nhật 23/10/2026')
    assert 'Online: 1T 4.8%, 3T 4.8%, 6T 7.2%, 12T 7.4%' in out
    assert 'Tại quầy: 1T 2.1%' in out


def test_interest_rate_missing_terms():
    assert '18T không có' in tools.get_interest_rate.invoke({'bank': 'Sacombank'})


def test_interest_rate_all_banks():
    out = tools.get_interest_rate.invoke({})
    lines = out.splitlines()
    assert lines[0] == 'Lãi suất tiết kiệm online (%/năm), cập nhật 23/10/2026:'
    assert len(lines) == 1 + 17
    assert 'SHB: 1T 4.75%, 3T 4.75%, 6T 7.7%, 12T 7.8%' in out


def test_interest_rate_unknown_bank_lists_available():
    out = tools.get_interest_rate.invoke({'bank': 'TPBank'})
    assert "Không có dữ liệu lãi suất của 'TPBank'" in out
    assert 'Vietcombank' in out


def test_search_faq_handles_backend_error(monkeypatch):
    def boom():
        raise ConnectionError('qdrant down')
    monkeypatch.setattr(tools, '_retriever', boom)
    out = tools.search_faq.invoke({'query': 'quên mật khẩu'})
    assert out.startswith('Lỗi hệ thống khi tra cứu dữ liệu nội bộ (ConnectionError)')


def test_web_search_handles_backend_error(monkeypatch):
    def boom(*args, **kwargs):
        raise TimeoutError()
    monkeypatch.setattr(tools.tavily_client, 'search', boom)
    out = tools.web_search.invoke({'query': 'lãi suất'})
    assert out.startswith('Lỗi hệ thống khi tìm kiếm trên web (TimeoutError)')


def branch_names(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith('- ')]


def test_branch_by_bank_includes_sources_and_reliability():
    out = tools.get_branch_info.invoke({'bank': 'BIDV'})
    assert 'ghi rõ nguồn' in out  # dặn model phải trích nguồn
    assert len(branch_names(out)) == 3
    assert '72 Lê Duẩn, P. Lý Thường Kiệt, TP Quy Nhơn' in out
    assert out.count('Nguồn: https://') == 3
    assert 'Độ tin cậy: Địa chỉ từ đăng ký thuế' in out


def test_branch_splits_multiple_sources_and_hides_missing_values():
    out = tools.get_branch_info.invoke({'bank': 'vcb'})
    assert ('Nguồn: https://infodoanhnghiep.com/thong-tin/Ngan-Hang-Thuong-Mai-Co-Phan-Ngoai-Thuong-Viet-Nam-'
            'Chi-Nhanh-Quy-Nhon-12437-008.html; https://cellphones.com.vn/sforum/tong-dai-vietcombank') in out
    assert '0256 3541 377 (máy lẻ 108)' in out
    assert 'Chưa có nguồn' not in out
    assert 'Giờ làm việc: chưa có thông tin' in out


@pytest.mark.parametrize('args, expected', [
    ({'street': 'Lê Duẩn'}, 5),
    ({'street': 'le duan'}, 5),
    ({'bank': 'mb', 'street': 'Trường Chinh'}, 1),
    ({'bank': 'oceanbank'}, 1),
    ({}, 23),
])
def test_branch_filters(args, expected):
    assert len(branch_names(tools.get_branch_info.invoke(args))) == expected


def test_branch_blank_cells_do_not_crash(monkeypatch):
    # File chi nhánh sửa tay: ô bỏ trống được pandas đọc thành NaN
    df = tools._branches().copy()
    df.loc[df['Mã NH'] == 'BIDV', ['Nguồn', 'Điện thoại', 'Địa chỉ (theo nguồn)']] = float('nan')
    monkeypatch.setattr(tools, '_branches', lambda: df)
    out = tools.get_branch_info.invoke({'bank': 'BIDV'})
    assert len(branch_names(out)) == 3
    assert 'nan' not in out
    assert 'Điện thoại: chưa có thông tin' in out
    assert len(branch_names(tools.get_branch_info.invoke({'street': 'Lê Duẩn'}))) == 4  # BIDV mất địa chỉ


def test_branch_not_found_lists_available_banks():
    out = tools.get_branch_info.invoke({'bank': 'TPBank'})
    assert out.startswith('Không có dữ liệu chi nhánh khớp')
    assert 'MBV (OceanBank cũ)' in out


def test_agent_main_has_all_tools():
    names = [t.name for t in tools.get_agent_tools('agent_main')]
    assert names == ['search_faq', 'get_interest_rate', 'get_branch_info', 'web_search']
