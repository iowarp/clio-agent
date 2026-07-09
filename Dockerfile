# Stage 1: Build
FROM python:3.12-slim AS builder
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --frozen --extra optimizers --no-dev

# Stage 2: Runtime
FROM python:3.12-slim
LABEL org.opencontainers.image.title="CLIO Agent" \
      org.opencontainers.image.description="CLIO Agent — autonomous AI agent for scientific data (REST API)." \
      org.opencontainers.image.source="https://github.com/iowarp/clio-agent" \
      org.opencontainers.image.vendor="iowarp" \
      org.opencontainers.image.licenses="BSD-3-Clause"
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/
ENV PATH="/app/.venv/bin:$PATH"
ENV CLIO_ENVIRONMENT=production
EXPOSE 8100
# 503 means "server up, config pending" (install/clio health semantics) — treat
# HTTP 200 or 503 as healthy so a healthy-but-unconfigured container is not marked down.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,urllib.error,sys\ntry:\n    code=urllib.request.urlopen('http://localhost:8100/v1/health',timeout=4).status\nexcept urllib.error.HTTPError as exc:\n    code=exc.code\nexcept Exception:\n    sys.exit(1)\nsys.exit(0 if code in (200,503) else 1)"]
ENTRYPOINT ["clio-agent", "serve", "--host", "0.0.0.0", "--port", "8100"]
