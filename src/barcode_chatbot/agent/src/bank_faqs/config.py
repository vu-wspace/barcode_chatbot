import os
from pathlib import Path

# Đường dẫn dữ liệu (dự án cài dạng editable nên tính từ vị trí file này: src/bank_faqs/config.py)
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / 'data'
CONVERSATIONS_FILE = DATA_DIR / 'raw' / 'cskh_hoi_thoai_ngan_hang.csv'   # mỗi dòng 1 lượt nói
MERGED_FILE = DATA_DIR / 'processed' / 'cskh_gop_theo_id.csv'          # đã gộp theo hội thoại, nạp vào Qdrant
RATE_FILE = DATA_DIR / 'raw' / 'lai_suat_ngan_hang_23-10-2026.csv'
RATE_DATE = '23/10/2026'
BRANCH_FILE = DATA_DIR / 'raw' / 'chi_nhanh_ngan_hang_quy_nhon.csv'

# Vector DB
QDRANT_URL = os.getenv('QDRANT_URL', 'http://localhost:6333')
COLLECTION = 'cskh_bkai'
EMBEDDING_MODEL = 'bkai-foundation-models/vietnamese-bi-encoder'
VECTOR_SIZE = 768

# Web app: collection rỗng thì tự nạp dữ liệu khi khởi động
AUTO_INGEST = os.getenv('AUTO_INGEST', 'true').lower() == 'true'
