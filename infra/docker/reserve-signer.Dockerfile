# No database/application packages are installed in the signer runtime.
FROM ghcr.io/astral-sh/uv:0.12.9 AS uv
FROM python:3.14-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
RUN uv sync --frozen --no-dev --no-editable --package reserve-signer
FROM python:3.14-slim-bookworm
RUN useradd --uid 10001 --create-home signer
COPY --from=builder /build/.venv /build/.venv
USER 10001
ENV PATH="/build/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1
CMD ["uvicorn", "reserve_signer.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
