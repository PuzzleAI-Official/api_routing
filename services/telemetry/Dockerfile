FROM python:3.12-slim

WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/app/packages/shared/src:/app/services/gateway/src:/app/services/telemetry/src

RUN python -m venv /opt/venv

COPY packages/shared /app/packages/shared
COPY services/gateway /app/services/gateway
COPY services/telemetry /app/services/telemetry

RUN pip install --no-cache-dir /app/packages/shared /app/services/gateway /app/services/telemetry
RUN groupadd --gid 10001 puzzle \
    && useradd --uid 10001 --gid 10001 --create-home puzzle \
    && mkdir -p /app/.local /data/object-store \
    && chown -R puzzle:puzzle /app/.local /data

USER puzzle
CMD ["python", "-m", "puzzle_telemetry.main"]
