# AzurPilotCloud

AzurPilotCloud 是《碧蓝航线》AzurPilot 的物品图标模板服务器。它接收客户端提交的图标，维护模板编号和物品映射，并生成供客户端下载的模板资源。

管理界面位于 [AzurPilotCloudWeb](https://github.com/wess09/AzurPilotCloudWeb)，统计服务位于 [AzurPillotCloudServer](https://github.com/wess09/AzurPillotCloudServer)。

## 运行

需要 Docker 和 Docker Compose。复制配置文件，填写数据库密码、管理凭据及管理页面来源后启动：

```sh
cp .env.example .env
docker compose up -d --build
```

服务默认监听 `127.0.0.1:8093`。数据库和服务数据保存在 Docker 卷中。

配置项见 [.env.example](.env.example)。`DEPLOY_MODE=local` 将模板资源写入本地目录；`DEPLOY_MODE=cloudflare` 将资源发布到 Cloudflare Worker，需要填写对应的账户和 API 凭据。

## 本地开发

需要 Python 3.12+、Node.js 22+、PostgreSQL 和 uv。配置 `.env` 后运行：

```sh
uv sync --frozen
npm ci
uv run --env-file .env alembic upgrade head
uv run --env-file .env template-server
```

## 文档

- [接口与资源协议](docs/protocol.md)
- [部署与服务接入](docs/integration.md)
