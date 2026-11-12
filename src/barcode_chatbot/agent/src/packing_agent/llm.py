import os
import re
import groq
import httpx
from langchain_core.exceptions import (ModelAuthenticationError, ModelNotFoundError, ModelPermissionDeniedError,
                                       ModelRateLimitError)
from langchain_openrouter import ChatOpenRouter
from openrouter import errors as openrouter_errors
from langchain_nvidia_ai_endpoints import ChatNVIDIA
from langchain_groq import ChatGroq
from langchain_google_genai import ChatGoogleGenerativeAI

# Số lần SDK tự thử lại khi lỗi tạm thời (5xx, quá tải, mất kết nối) trước khi báo lỗi.
# Model đứng trước trong chuỗi dự phòng chỉ thử lại 1 lần rồi nhường model sau (nhanh hơn chờ backoff 3 lần);
# model cuối chuỗi (Groq) thử lại nhiều hơn vì sau nó không còn model nào.
RETRIES_IN_CHAIN = 1
RETRIES_LAST = 3

open_llm = ChatOpenRouter(
    model='nvidia/nemotron-3-ultra-550b-a55b:free',
    temperature=0,
    max_tokens=1024,
    # ChatOpenRouter tính timeout bằng MILI GIÂY (khác ChatGroq/ChatGoogleGenerativeAI tính bằng giây).
    # Đặt 60 là 60ms -> mọi request đều timeout rồi SDK thử lại liên tục, agent treo vài phút.
    timeout=30_000,
    max_retries=RETRIES_IN_CHAIN,
)

# NVIDIA API (build.nvidia.com). Các số đo bên dưới lấy từ bộ eval của dự án khác (bán điện thoại), chưa đo cho
# bàn đóng gói. Đo 29/10/2026: gọi tool đúng, 0.9-2.3 s/lần (Gemini dao động 1-23 s) -> dự phòng số 1.
# Phần "thinking" trả riêng trong additional_kwargs['reasoning_content'], không lẫn vào câu trả lời gửi khách.
# ChatNVIDIA không có max_retries (SDK không tự thử lại) và tự đặt timeout 60 s cho client bên trong.
nvidia_llm = ChatNVIDIA(
    usage_telemetry_enabled=False,  # mặc định gửi thống kê sử dụng về NVIDIA
    model='nvidia/nemotron-3.5-super-vl-120b-a12b',
    temperature=0,
    # Giới hạn cả token suy nghĩ lẫn câu trả lời. Đo 29/10/2026 (thinking bật, 24 câu eval, sau khi thêm quy tắc 10):
    #   8192 -> đủ ý 23/24, TB 13.1 s, chậm nhất 59 s | 4096 -> 23/24, TB 9.1 s, chậm nhất 46 s
    #   2048 -> 22/24 và 22/23 (2 lần), TB 8.7-8.8 s; không câu nào bị cắt thành rỗng
    # Chọn 4096: đúng ngang 8192, gần nhanh bằng 2048, còn dư chỗ cho lần suy nghĩ dài.
    max_completion_tokens=4096,
    # Truyền qua model_kwargs: truyền thẳng chat_template_kwargs thì thư viện vẫn chuyển vào đây nhưng in cảnh báo.
    # Giữ thinking BẬT. Đo 29/10/2026 trên 24 câu eval: tắt thinking (kể cả có quy tắc 10 "phải gọi tool trước khi nói
    # không có") nhanh gấp đôi (trung vị ~3 s) nhưng đủ ý chỉ 21/23 và vẫn nói sai "không có Samsung dưới 7 triệu"
    # dù tool đã trả về danh sách máy.
    model_kwargs={'chat_template_kwargs': {'enable_thinking': True}},
)

google_llm = ChatGoogleGenerativeAI(
    model='gemini-3.1-flash-lite',
    temperature=0,
    max_output_tokens=1024,  # trần chi phí mỗi lần gọi; câu trả lời thực tế dưới 300 token
    # Model chính: treo (lỗi 504 DEADLINE_EXCEEDED) thì không thử lại mà chuyển ngay model dự phòng.
    # Đo 12/11/2026, 8 câu qua agent: deadline 15 s -> 0 lỗi, trung vị 6.5 s; 10 s -> 5/8 lỗi (Gemini chậm hôm đó).
    # Dưới 10 s Gemini từ chối mọi request (400 "Minimum allowed deadline is 10s").
    timeout=15,
    max_retries=0,
)

# Gemini gói free tính quota riêng cho từng model -> model thứ 2 là thêm 1 phần quota khi model trên hết
google_llm_alt = ChatGoogleGenerativeAI(
    model='gemini-3.5-flash-lite',  # model này bỏ qua temperature (in cảnh báo nếu truyền)
    max_output_tokens=1024,
    timeout=60,
    max_retries=RETRIES_IN_CHAIN,
)

groq_llm = ChatGroq(
    model='qwen/qwen3.8-27b',
    temperature=0,
    timeout=60,
    max_retries=RETRIES_LAST,
    reasoning_format='hidden',
    # Câu trả lời thực tế ~15-120 token. Không đặt thì Groq tính mỗi request 2048 token đầu ra,
    # vượt giới hạn gói free (1000 token đầu ra/phút) -> lỗi 429.
    max_tokens=512,
)

# Thứ tự đo 12/11/2026 trên 5 câu đóng gói (tra mã, kiểm đơn, hết hàng, nội dung đơn, câu ngoài lề), qua agent thật:
#   gemini-3.1-flash-lite 5/5, trung vị 2.0-2.9 s, chậm nhất 4.5 s  -> model chính
#   OpenRouter nemotron-3-ultra 5/5, trung vị 4.8-5.5 s (có lần 33 s) | NVIDIA nemotron-3.5-super 5/5, 5-6 s (chậm nhất 25 s)
#   gemini-3.5-flash-lite 5/5, 6.1 s | Groq qwen3.8 4/5 (1 lần lỗi gọi tool), 1.1 s nhưng quota token/phút nhỏ -> cuối
MAIN_LLM = google_llm
def _has_key(*names: str) -> bool:
    return any(os.getenv(n, '').strip().lower() not in ('', 'unused') for n in names)


# Model chính hỏng/hết quota thì thử lần lượt các model này (xem fallback.py). Hãng không có key thì bỏ hẳn khỏi
# chuỗi: thư viện NVIDIA vẫn gửi request (cả hội thoại) khi thiếu key, chỉ in cảnh báo.
FALLBACK_LLMS = [m for m, var in [(open_llm, 'OPENROUTER_API_KEY'), (nvidia_llm, 'NVIDIA_API_KEY'),
                                  (google_llm_alt, ('GOOGLE_API_KEY', 'GEMINI_API_KEY')),
                                  (groq_llm, 'GROQ_API_KEY')] if _has_key(*([var] if isinstance(var, str) else var))]

def model_name(llm) -> str:
    return getattr(llm, 'model', None) or getattr(llm, 'model_name', None) or type(llm).__name__

def nvidia_status(e: Exception) -> int | None:
    # ChatNVIDIA ném Exception chung với nội dung "[429] Too Many Requests ..." -> đọc mã HTTP ở đầu thông báo.
    # Chỉ xét đúng lớp Exception gốc để không đọc nhầm thông báo của lỗi khác.
    m = re.match(r'\[(\d{3})\]', str(e)) if type(e) is Exception else None
    return int(m.group(1)) if m else None

def is_rate_limited(e: Exception) -> bool:
    # Gemini (langchain-google-genai) ném ModelRateLimitError chung của langchain_core.
    # Groq ném lỗi riêng: 429, hoặc 413 "Request too large" kèm code rate_limit_exceeded.
    # OpenRouter (SDK openrouter) cũng ném lỗi riêng, không kế thừa ModelError. NVIDIA: xem nvidia_status.
    if isinstance(e, (ModelRateLimitError, groq.RateLimitError, openrouter_errors.TooManyRequestsResponseError)):
        return True
    if nvidia_status(e) == 429:
        return True
    if not isinstance(e, groq.APIError):
        return False
    body = e.body if isinstance(e.body, dict) else {}
    err = body.get('error')
    return (err if isinstance(err, dict) else body).get('code') == 'rate_limit_exceeded'

def is_timeout(e: Exception) -> bool:
    # httpx (OpenRouter, Gemini, Groq), asyncio/aiohttp (NVIDIA), lỗi riêng của SDK (APITimeoutError...)
    return (isinstance(e, (TimeoutError, httpx.TimeoutException)) or 'Timeout' in type(e).__name__
            or nvidia_status(e) == 504 or 'DEADLINE_EXCEEDED' in str(e))  # Gemini: GoogleAPIError 504 (seen 12/11/2026)


def is_daily_limit(e: Exception) -> bool:
    # Hết quota NGÀY (OpenRouter: free-models-per-day, Gemini: ...PerDay...): chờ vài phút cũng vô ích
    text = str(e)
    return is_rate_limited(e) and ('per-day' in text or 'PerDay' in text or 'per day' in text.lower())

def is_permanent_error(e: Exception) -> bool:
    # Lỗi không tự hết khi thử lại: sai tên model, sai/hết hạn API key, không có quyền, hết credit.
    # Lỗi tạm thời (mất mạng, 5xx, quá tải) thì không tính vào đây.
    if nvidia_status(e) in (401, 402, 403, 404):
        return True
    if 'not a valid model' in str(e):  # OpenRouter: wrong model name is a 400, not a 404
        return True
    return isinstance(e, (
        ModelNotFoundError, ModelAuthenticationError, ModelPermissionDeniedError,     # Gemini (langchain_core)
        groq.NotFoundError, groq.AuthenticationError, groq.PermissionDeniedError,
        openrouter_errors.NotFoundResponseError, openrouter_errors.UnauthorizedResponseError,
        openrouter_errors.ForbiddenResponseError, openrouter_errors.PaymentRequiredResponseError,
    ))
