FROM node:22-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca
COPY --from=node /usr/local/bin/node /usr/local/bin/node
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY package.json package-lock.json ./
COPY --from=node /usr/local/lib/node_modules/npm /opt/npm
RUN node /opt/npm/bin/npm-cli.js ci --ignore-scripts
COPY pyproject.toml uv.lock ./
COPY templateserver ./templateserver
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home templates && mkdir data && chown templates:templates data
USER templates
ENV HOME=/home/templates WRANGLER_SEND_METRICS=false LISTEN_HOST=0.0.0.0 PORT=8093
EXPOSE 8093
CMD ["sh", "-c", "/app/.venv/bin/alembic upgrade head && exec /app/.venv/bin/template-server"]
