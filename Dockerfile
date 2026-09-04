FROM python:3.12-slim-bookworm AS builder

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv "$VIRTUAL_ENV"

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir '.[speed]'


FROM python:3.12-slim-bookworm AS runtime

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates ffmpeg fonts-noto-cjk tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app/data /app/work

COPY --from=builder /opt/venv /opt/venv

USER root
WORKDIR /app
STOPSIGNAL SIGINT
ENTRYPOINT ["channel-operator"]
CMD ["--config", "/app/config.toml", "schedule"]
