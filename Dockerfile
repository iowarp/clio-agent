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
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1
ENTRYPOINT ["uvicorn", "clio_agent.ui.api:app", "--host", "0.0.0.0", "--port", "8000"]
