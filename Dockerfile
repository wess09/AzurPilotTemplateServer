FROM node:22-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:0.11.32-python3.13-bookworm-slim
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
ENV LISTEN_HOST=0.0.0.0 PORT=8093
EXPOSE 8093
CMD ["sh", "-c", "uv run --no-sync alembic upgrade head && uv run --no-sync template-server"]
