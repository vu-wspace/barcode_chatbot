from langchain_groq import ChatGroq
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openrouter import ChatOpenRouter

groq_llm = ChatGroq(
    model='qwen/qwen3.8-27b',
    temperature=0,
    timeout=60,
    max_retries=3,
    reasoning_format='hidden',
    # Câu trả lời thực tế ~15-120 token. Không đặt thì Groq tính mỗi request 2048 token đầu ra,
    # vượt giới hạn gói free (1000 token đầu ra/phút) -> lỗi 429.
    max_tokens=512,
)

google_llm = ChatGoogleGenerativeAI(
    model='gemini-3.1-flash-lite',
    temperature=0,
    timeout=60,
    max_retries=3,
)

open_llm = ChatOpenRouter(
    model='nvidia/nemotron-3-ultra-550b-a55b:free',
    temperature=0,
    max_tokens=1024,
    # ChatOpenRouter tính timeout bằng MILI GIÂY (khác ChatGroq/ChatGoogleGenerativeAI tính bằng giây).
    # Đặt 60 là 60ms -> mọi request đều timeout rồi SDK thử lại liên tục, agent treo vài phút.
    timeout=60_000,
    max_retries=3,
)
