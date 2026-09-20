# syntax=docker/dockerfile:1

FROM python:3.13-slim AS builder

ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app
RUN pip install --no-cache-dir poetry==2.4.3
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --no-root --only main
COPY app ./app
RUN poetry install --only-root --no-directory

# Final runtime image
FROM python:3.13-slim

LABEL org.opencontainers.image.authors="chinedum"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy installed packages + app from builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/_health', timeout=2)" || exit 1

ENTRYPOINT ["caching-proxy", "run"]
CMD ["--port", "5000"]