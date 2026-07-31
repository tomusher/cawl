# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/server/.venv/bin:$PATH

RUN groupadd --system cawl && useradd --system --gid cawl --home /app cawl

WORKDIR /app/server
COPY server/pyproject.toml server/uv.lock ./
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --extra postgres --no-install-project \
    && rm -rf /root/.cache/uv

COPY --chown=cawl:cawl server/ ./
# Docker initializes a new named volume from this directory, preserving its
# ownership so the unprivileged daemon can write Traefik dynamic routes.
RUN mkdir -p /var/lib/cawl/dynamic && chown -R cawl:cawl /var/lib/cawl

USER cawl
EXPOSE 8000
CMD ["gunicorn", "cawlserver.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
