FROM python:3.12.15-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    HF_HOME=/app/.cache/huggingface \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# CA nội bộ cho mạng có SSL inspection (proxy cấp lại chứng chỉ HTTPS): đặt file *.crt vào certs/.
# Thư mục rỗng thì bước này không làm gì.
COPY certs/ /usr/local/share/ca-certificates/
RUN update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# uv chỉ mount lúc build, không nằm trong image
# Cài thư viện trước (layer cache: chỉ cài lại khi pyproject/uv.lock đổi)
COPY pyproject.toml uv.lock README.md ./
RUN --mount=from=ghcr.io/astral-sh/uv:0.12.23,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Tải sẵn model embedding vào image để container khởi động không cần mạng tới Hugging Face.
# Đặt trước COPY src để sửa code không phải tải lại model (~540MB). Phải trùng EMBEDDING_MODEL trong config.py.
ARG EMBEDDING_MODEL=bkai-foundation-models/vietnamese-bi-encoder
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"
ENV HF_HUB_OFFLINE=1

COPY src ./src
COPY data ./data
RUN --mount=from=ghcr.io/astral-sh/uv:0.12.23,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# Chạy bằng user thường. Không chown -R /app: lệnh đó nhân đôi ~2.5GB venv + model thành layer mới,
# mà app chỉ cần quyền đọc
RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# 1 worker: lịch sử hội thoại (InMemorySaver) nằm trong RAM của process
CMD ["uvicorn", "bank_faqs.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
