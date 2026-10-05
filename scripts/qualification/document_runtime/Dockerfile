FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libcairo2 libx11-6 libxext6 libxinerama1 libsm6 libice6 \
    libfontconfig1 libcups2 libdbus-1-3 libnss3 libglib2.0-0 libx11-xcb1 \
    fonts-dejavu-core fonts-liberation && rm -rf /var/lib/apt/lists/*
COPY src/clio_agent/runtime/document_stack /stack
ENV UV_PROJECT_ENVIRONMENT=/runtime
ENV CLIO_DOCUMENT_OFFICE_ROOT=/office-cache/renderer
RUN uv sync --frozen --project /stack --no-install-project
ENTRYPOINT ["/runtime/bin/python", "/stack/smoke.py", "/evidence"]
