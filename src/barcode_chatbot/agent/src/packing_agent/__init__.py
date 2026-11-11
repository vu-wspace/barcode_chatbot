# Dùng kho chứng chỉ của hệ điều hành (mạng có SSL inspection, vd với Gemini, LangSmith).
# Phải chạy trước khi tạo client LLM: các SDK (google-genai, groq...) tạo SSL context ngay lúc khởi tạo.
import truststore
truststore.inject_into_ssl()

# Nạp .env một lần cho cả package, trước khi các module đọc biến môi trường (BE_URL, API key...)
from dotenv import load_dotenv  # noqa: E402
load_dotenv()
