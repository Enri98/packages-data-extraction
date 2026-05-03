FROM python:3.11-slim

# curl is needed by HEALTHCHECK; ca-certificates for HTTPS to Drive/Sheets/Gemini.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv — pinned via digest in production; use :latest for scaffold
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency manifest first for layer caching
COPY pyproject.toml .
COPY uv.lock* ./

# Install production dependencies only (no dev group)
RUN uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev

# Copy application source
COPY src/ ./src/
COPY prompts/ ./prompts/

ENV PYTHONPATH=/app
ENV PATH="/app/.venv/bin:$PATH"
ENV PORT=8080

# Run as a non-root user (uid 10001 — Cloud Run honours USER directive).
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

EXPOSE 8080

# Local-docker convenience only — Cloud Run uses /health and /ready directly.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/health" || exit 1

# Cloud Run sets PORT env var; uvicorn reads it here
CMD ["sh", "-c", "uvicorn src.server:app --host 0.0.0.0 --port ${PORT} --workers 1"]
