FROM python:3.11-slim

WORKDIR /app

# Install uv — pinned via digest in production; use :latest for scaffold
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency manifest first for layer caching
COPY pyproject.toml .
# uv.lock will be generated after first `uv sync`; copy if present
COPY uv.lock* ./

# Install production dependencies only (no dev group)
RUN uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev

# Copy application source
COPY src/ ./src/
COPY prompts/ ./prompts/

ENV PYTHONPATH=/app
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

# Cloud Run sets PORT env var; uvicorn reads it here
CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
