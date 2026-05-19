FROM python:3.12-slim

WORKDIR /app
ENV PYTHONPATH=/app/packages/shared/src:/app/services/gateway/src:/app/services/telemetry/src

COPY packages/shared /app/packages/shared
COPY services/gateway /app/services/gateway
COPY services/telemetry /app/services/telemetry

RUN pip install --no-cache-dir /app/packages/shared /app/services/gateway /app/services/telemetry

CMD ["python", "-m", "puzzle_telemetry.main"]
