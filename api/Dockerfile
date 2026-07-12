FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY README.md alembic.ini ./
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm

RUN groupadd -r nimbleship && useradd -r -g nimbleship nimbleship
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/alembic.ini ./
COPY --from=builder /app/migrations ./migrations
ENV PATH="/app/.venv/bin:$PATH"
USER nimbleship
EXPOSE 8000

# Migrations run on boot: the schema is owned by Alembic, never create_all.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn nimbleship.main:create_app --factory --host 0.0.0.0 --port 8000"]
