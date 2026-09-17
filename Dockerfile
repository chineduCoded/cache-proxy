LABEL authors="chinedum"

FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app

RUN pip install --no-cache-dir poetry==2.4.3

COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main

COPY app ./app
RUN poetry install --only-root

RUN useradd --create-home --shell /usr/sbin/nologin appuser
USER appuser

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/_health', timeout=2)" || exit 1

ENTRYPOINT ["caching-proxy", "run"]
CMD ["--port", "5000"]
