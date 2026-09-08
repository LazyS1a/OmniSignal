FROM ghcr.io/astral-sh/uv:0.12.8-python3.12-alpine3.23@sha256:94a8635fa6d7c50e1eafea1d353b67ac381ec9b531916fb3a2432721fbd3a8ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src

RUN apk upgrade --no-cache \
    && addgroup -S omnisignal \
    && adduser -S -D -H -G omnisignal -h /app omnisignal

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra signals

COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
COPY governance/source_registry.yaml ./governance/source_registry.yaml
COPY config/collection_tasks.yaml ./config/collection_tasks.yaml
COPY examples/connectors/public_search_signals.yaml examples/connectors/youtube_visibility.yaml ./examples/connectors/
COPY examples/policies/public_search_signals.yaml examples/policies/youtube_visibility.yaml ./examples/policies/

RUN chown -R omnisignal:omnisignal /app
USER omnisignal

EXPOSE 8000
CMD ["uvicorn", "omnisignal.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
