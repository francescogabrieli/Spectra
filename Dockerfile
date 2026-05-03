FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_SYSTEM_PYTHON=1

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

COPY . /app

RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

RUN mkdir -p /app/data /app/inbox /app/processed

EXPOSE 8080

CMD ["uvicorn", "spectra.web.server:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "warning"]
