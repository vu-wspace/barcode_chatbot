import re
import unicodedata
import pandas as pd
from langchain.tools import tool
from typing import Any
from bank_faqs.config import BRANCH_FILE, RATE_DATE, RATE_FILE
from bank_faqs.vector_db import get_retriever
from tavily import TavilyClient
from functools import lru_cache

tavily_client = TavilyClient()

TERMS = ['1T', '3T', '6T', '12T', '18T', '24T', '36T']

NO_INFO = 'Chưa có nguồn'  # giá trị trong file chi nhánh khi chưa thu thập được

# Tên gọi tắt hay gặp -> Mã NH trong file lãi suất/chi nhánh (đã chuẩn hóa bằng _norm)
BANK_ALIASES = {
    'vcb': 'vietcombank', 'ngoaithuong': 'vietcombank',
    'dautuvaphattrien': 'bidv',
    'vietin': 'vietinbank', 'ctg': 'vietinbank', 'congthuong': 'vietinbank',
    'nongnghiep': 'agribank',
    'tcb': 'techcombank', 'techcom': 'techcombank',
    'stb': 'sacombank', 'sacom': 'sacombank',
    'vpb': 'vpbank',
    'mb': 'mbbank', 'quandoi': 'mbbank',
    'hdb': 'hdbank',
    'seab': 'seabank',
    'pvcom': 'pvcombank',
    'baca': 'bacabank', 'bab': 'bacabank',
    'baoviet': 'baovietbank', 'bvb': 'baovietbank',
    'nama': 'namabank', 'nab': 'namabank',
    'banviet': 'bvbank',
}

@lru_cache
def _retriever():
    return get_retriever()

def _norm(text: str) -> str:
    text = unicodedata.normalize('NFD', text.lower().replace('đ', 'd'))
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9]', '', text)

@lru_cache
def _load_banks(path) -> pd.DataFrame:
    # File lãi suất và file chi nhánh đều có cột 'Mã NH'; _key dùng để so khớp tên ngân hàng
    df = pd.read_csv(path, encoding='utf-8-sig')
    df['_key'] = df['Mã NH'].map(_norm)
    return df

def _rates() -> pd.DataFrame:
    return _load_banks(RATE_FILE)

def _branches() -> pd.DataFrame:
    return _load_banks(BRANCH_FILE)

def _find_bank(df: pd.DataFrame, bank: str) -> pd.DataFrame:
    key = _norm(bank).removeprefix('nganhang')
    key = BANK_ALIASES.get(key, key)
    exact = df[df['_key'] == key]
    if not exact.empty or len(key) < 3:
        return exact
    return df[df['_key'].str.contains(key, regex=False) | df['_key'].map(lambda k: k in key)]

def _fmt(v) -> str:
    return 'không có' if pd.isna(v) else f'{v:g}%'

def _fmt_rates(r: pd.Series, kind: str) -> str:
    # kind: 'Online' hoặc 'Tại quầy' (tiền tố tên cột trong file lãi suất)
    return ', '.join(f"{t} {_fmt(r[f'{kind} {t} (%/năm)'])}" for t in TERMS)

@tool
def search_faq(query: str) -> str:
    """Tra cứu câu trả lời chăm sóc khách hàng từ dữ liệu nội bộ: thẻ, chuyển tiền, tài khoản, 
    tiết kiệm, vay, lừa đảo, ứng dụng ngân hàng. Dùng cho câu hỏi về thủ tục và cách xử lý sự cố. 
    Không dùng để tra lãi suất (dùng get_interest_rate) hoặc tin tức mới nhất (dùng web_search)."""
    try:
        hits = _retriever().invoke(query)
    except Exception as e:
        # ToolNode chỉ bắt lỗi sai tham số; lỗi khác (Qdrant sập...) sẽ làm hỏng cả lượt hội thoại
        return (f'Lỗi hệ thống khi tra cứu dữ liệu nội bộ ({type(e).__name__}). '
                'Không tự đưa ra hướng dẫn; xin lỗi anh/chị và đề nghị liên hệ tổng đài hoặc chi nhánh.')
    if not hits:
        return 'Không tìm thấy câu trả lời trong dữ liệu nội bộ.'
    return '\n\n'.join(
        f"Hỏi: {h.page_content}\nĐáp: {h.metadata['answer']}" for h in hits
    )

@tool
def get_interest_rate(bank: str = '') -> str:
    """Tra lãi suất tiết kiệm (%/năm) theo kỳ hạn 1-36 tháng, gửi online và tại quầy, của các ngân hàng
    tại Quy Nhơn. Truyền tên ngân hàng (vd: BIDV, Vietcombank, MB). Để trống để lấy bảng lãi suất
    online của tất cả ngân hàng (dùng khi so sánh, hỏi ngân hàng nào cao nhất).
    Luôn dùng tool này trước web_search cho câu hỏi lãi suất tiết kiệm."""
    df = _rates()
    if not bank.strip():
        lines = [f"{r['Mã NH']}: {_fmt_rates(r, 'Online')}" for _, r in df.iterrows()]
        return f'Lãi suất tiết kiệm online (%/năm), cập nhật {RATE_DATE}:\n' + '\n'.join(lines)

    hits = _find_bank(df, bank)
    if hits.empty:
        return (f"Không có dữ liệu lãi suất của '{bank}'. "
                f"Các ngân hàng có dữ liệu: {', '.join(df['Mã NH'])}.")
    return '\n\n'.join(
        f"{r['Mã NH']} (cập nhật {RATE_DATE}, T = tháng):\n"
        f"- Online: {_fmt_rates(r, 'Online')}\n- Tại quầy: {_fmt_rates(r, 'Tại quầy')}"
        for _, r in hits.iterrows()
    )

def _fmt_branch(r: pd.Series) -> str:
    # File chi nhánh sửa tay: ô có thể ghi "Chưa có nguồn" hoặc bị bỏ trống (pandas đọc thành NaN).
    # Cả hai đều hiện "chưa có thông tin" để model không tự bịa, và không in ra chữ "nan".
    def info(col: str) -> str:
        v = r[col]
        return 'chưa có thông tin' if pd.isna(v) or str(v).strip() in ('', NO_INFO) else str(v).strip()

    sources = '; '.join(s.strip() for s in info('Nguồn').split(';') if s.strip())
    return (f"- {r['Tên ngân hàng']} ({r['Mã NH']}), {info('Đơn vị')}\n"
            f"  Địa chỉ (theo nguồn): {info('Địa chỉ (theo nguồn)')}\n"
            f"  Phường mới (sau 1/7/2025): {info('Phường mới (sau 1/7/2025)')}\n"
            f"  Điện thoại: {info('Điện thoại')} | Giờ làm việc: {info('Giờ làm việc')}\n"
            f"  Độ tin cậy: {info('Ghi chú / độ tin cậy')}\n"
            f"  Nguồn: {sources}")

@tool
def get_branch_info(bank: str = '', street: str = '') -> str:
    """Tra địa chỉ, số điện thoại chi nhánh ngân hàng tại Quy Nhơn (dữ liệu tổng hợp từ nguồn công khai).
    Truyền tên ngân hàng (vd: BIDV, Vietcombank, MB) và/hoặc tên đường (vd: Lê Duẩn). Để trống cả hai
    để lấy danh sách mọi chi nhánh. Kết quả có nguồn (URL) của từng chi nhánh: khi trả lời phải ghi rõ nguồn."""
    df = _branches()
    hits = _find_bank(df, bank) if bank.strip() else df
    if street.strip():
        key = _norm(street)
        hits = hits[hits['Địa chỉ (theo nguồn)'].map(lambda a: isinstance(a, str) and key in _norm(a))]
    if hits.empty:
        return (f"Không có dữ liệu chi nhánh khớp (ngân hàng: '{bank}', đường: '{street}'). "
                f"Các ngân hàng có dữ liệu: {', '.join(df['Mã NH'].unique())}.")
    return ('Chi nhánh ngân hàng tại Quy Nhơn (dữ liệu tổng hợp; địa chỉ có thể theo địa giới hành chính '
            'trước 1/7/2025). Khi trả lời: ghi rõ nguồn (URL) của từng chi nhánh, nêu độ tin cậy, '
            'không tự bổ sung số điện thoại hay giờ làm việc chưa có thông tin.\n\n'
            + '\n\n'.join(_fmt_branch(r) for _, r in hits.iterrows()))

@tool
def web_search(query: str) -> dict[str, Any] | str:
    """Tìm kiếm thông tin mới nhất trên web: khuyến mãi, tin tức ngân hàng, lãi suất vay,
    lãi suất của ngân hàng không có trong get_interest_rate.
    Chỉ dùng khi search_faq và get_interest_rate không có câu trả lời.
    Không dùng cho câu hỏi thủ tục, xử lý sự cố (dùng search_faq)."""
    try:
        return tavily_client.search(query, max_results=3, timeout=30)
    except Exception as e:
        return (f'Lỗi hệ thống khi tìm kiếm trên web ({type(e).__name__}). '
                'Không tự đưa ra số liệu; xin lỗi anh/chị và đề nghị xem trên ứng dụng hoặc liên hệ chi nhánh.')

# Tool của từng agent. Mô tả tool mà model nhìn thấy lấy từ docstring của hàm @tool ở trên.
TOOLS = {t.name: t for t in (search_faq, get_interest_rate, get_branch_info, web_search)}
AGENT_TOOLS = {
    'agent_main': ['search_faq', 'get_interest_rate', 'get_branch_info', 'web_search'],
}

def get_agent_tools(agent: str) -> list:
    return [TOOLS[name] for name in AGENT_TOOLS[agent]]
