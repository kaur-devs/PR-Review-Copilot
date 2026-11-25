# syntax=docker/dockerfile:1

FROM python:3.14.5-slim AS builder

# No compiler: every pinned package ships a manylinux wheel for amd64 and
# arm64, checked 2026-11-25. If a future dependency has none, pip will say
# so loudly and build-essential goes back here.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt


FROM python:3.14.5-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000

# A shell, because ${PORT} needs expanding; exec, so uvicorn is PID 1 and
# gets SIGTERM. Never add --workers: the token budget in src/llm/budget.py is
# per process.
CMD ["/bin/sh", "-c", "exec uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
