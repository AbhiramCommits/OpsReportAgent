# OpsReportAgent — slim, non-root container.
# Entrypoint is the `opsreport` console script.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TEMPLATES_DIR=/app/templates \
    CORPUS_DIR=/app/data/corpus \
    GOLD_DIR=/app/data/gold \
    REPORTS_DIR=/app/reports \
    DATABASE_URL=sqlite:////app/data/opsreport.db

WORKDIR /app

# Install the package first so this layer is cached.
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip && pip install .

# Runtime assets: report templates, corpus generator, and the demo corpus.
COPY templates ./templates
COPY scripts ./scripts
COPY data ./data

# Non-root user owning everything it needs to write to.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin ops \
    && mkdir -p /app/reports \
    && chown -R ops:ops /app
USER ops

ENTRYPOINT ["opsreport"]
CMD ["--help"]
