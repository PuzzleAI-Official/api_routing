FROM python:3.12-slim

WORKDIR /app
ENV PYTHONPATH=/app/packages/shared/src:/app/services/gateway/src:/app/services/worker/src

COPY packages/shared /app/packages/shared
COPY services/gateway /app/services/gateway
COPY services/worker /app/services/worker

RUN pip install --no-cache-dir /app/packages/shared /app/services/gateway /app/services/worker

CMD ["python", "-m", "puzzle_worker.main"]
