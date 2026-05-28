FROM python:3.12-slim

WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/app/packages/shared/src:/app/services/gateway/src

RUN python -m venv /opt/venv

COPY packages/shared /app/packages/shared
COPY services/gateway /app/services/gateway
COPY pyproject.toml /app/pyproject.toml

RUN pip install --no-cache-dir /app/packages/shared /app/services/gateway
RUN groupadd --gid 10001 puzzle \
    && useradd --uid 10001 --gid 10001 --create-home puzzle \
    && mkdir -p /app/.local /data/object-store \
    && chown -R puzzle:puzzle /app/.local /data

EXPOSE 8000
USER puzzle
CMD ["sh", "-c", "uvicorn puzzle_gateway.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
