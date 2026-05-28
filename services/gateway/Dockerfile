FROM python:3.12-slim

WORKDIR /app
ENV PYTHONPATH=/app/packages/shared/src:/app/services/gateway/src

COPY packages/shared /app/packages/shared
COPY services/gateway /app/services/gateway
COPY pyproject.toml /app/pyproject.toml

RUN pip install --no-cache-dir /app/packages/shared /app/services/gateway

EXPOSE 8000
CMD ["sh", "-c", "uvicorn puzzle_gateway.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
