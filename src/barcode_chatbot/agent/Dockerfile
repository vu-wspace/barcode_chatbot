FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# CA nội bộ cho mạng có SSL inspection (proxy cấp lại chứng chỉ HTTPS): đặt file *.crt vào certs/.
# Thư mục rỗng thì bước này không làm gì.
COPY certs/ /usr/local/share/ca-certificates/
RUN update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# Cài thư viện trước (layer cache: chỉ cài lại khi pyproject/uv.lock đổi)
COPY pyproject.toml uv.lock ./
RUN --mount=from=ghcr.io/astral-sh/uv:0.12.23,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN --mount=from=ghcr.io/astral-sh/uv:0.12.23,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

USER nobody
EXPOSE 8000
# 1 worker: lịch sử hội thoại (InMemorySaver) nằm trong RAM của process
CMD ["uvicorn", "packing_agent.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
