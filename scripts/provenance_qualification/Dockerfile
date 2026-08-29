FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git procps util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

COPY pyproject.toml README.md uv.lock ./
RUN uv sync --frozen --extra flowcept --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --extra flowcept --no-dev

# CMF 0.1.0 / MLMD 1.15 require their own Python 3.9 runtime. Keep that
# compatibility boundary in the qualification image instead of borrowing a
# mutable host venv.
RUN uv python install 3.9 \
    && uv venv /opt/clio-cmf --python 3.9 \
    && uv pip install --python /opt/clio-cmf/bin/python \
        "cmflib==0.1.0" "ml-metadata==1.15.0"

ENV PATH="/app/.venv/bin:/root/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    CLIO_PQ_CMF_PYTHON=/opt/clio-cmf/bin/python

ENTRYPOINT ["clio-agent", "serve"]
