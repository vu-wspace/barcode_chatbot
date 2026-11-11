import sys

# Windows: nếu pandas/truststore được import trước torch thì torch lỗi
# "WinError 1114 ... c10.dll" (xung đột bản MSVC runtime). Nạp trước msvcp140.dll của hệ thống để tránh.
if sys.platform == 'win32':
    import ctypes
    ctypes.WinDLL('msvcp140.dll')

# Dùng kho chứng chỉ của hệ điều hành (mạng có SSL inspection, vd với Gemini, LangSmith).
# Phải chạy trước khi tạo client LLM: các SDK (google-genai, groq...) tạo SSL context ngay lúc khởi tạo.
# Đặt sau đoạn fix DLL ở trên vì import truststore sẽ gọi module platform.
import truststore  # noqa: E402
truststore.inject_into_ssl()

# Nạp .env một lần cho cả package, trước khi các module đọc biến môi trường (QDRANT_URL, API key...)
from dotenv import load_dotenv  # noqa: E402
load_dotenv()
