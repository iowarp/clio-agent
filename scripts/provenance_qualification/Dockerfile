FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git procps util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

ENV UV_TOOL_DIR=/opt/uv-tools \
    UV_TOOL_BIN_DIR=/usr/local/bin
RUN uv tool install "clio-kit==2.2.3"

COPY pyproject.toml README.md uv.lock ./
RUN uv sync --frozen --extra flowcept --extra claude-code --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --extra flowcept --extra claude-code --no-dev

# CMF 0.1.0 / MLMD 1.15 require their own Python 3.9 runtime. Keep that
# compatibility boundary in the qualification image instead of borrowing a
# mutable host venv.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN uv python install 3.9 \
    && uv venv /opt/clio-cmf --python 3.9 \
    && uv pip install --python /opt/clio-cmf/bin/python \
        "cmflib==0.1.0" "ml-metadata==1.15.0"

COPY --chmod=0755 scripts/provenance_qualification/serve-qualification.sh \
    /usr/local/bin/clio-serve-qualification
COPY --chmod=0755 scripts/provenance_qualification/edges-drive.sh \
    /usr/local/bin/clio-edges-drive

ENV PATH="/app/.venv/bin:/root/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    CLIO_PQ_CMF_PYTHON=/opt/clio-cmf/bin/python

RUN install -d -o 1000 -g 1000 /qualification
WORKDIR /qualification

ENTRYPOINT ["/usr/local/bin/clio-serve-qualification"]
