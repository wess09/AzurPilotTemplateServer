# 客户端和统一面板直连模板服务

## 生产流量边界

- 客户端图标上传：`https://templates-api.azurpilot.com/api/v3/post/templates/unknown`。
- 批量上传：同域名 `/api/v3/post/unknown-candidates`。两种上传均不需要服务令牌，按来源和设备限流。
- 统一面板：仍在 `https://api.azurpilot.com/admin/`，但模板页的列表、图标、导入、草稿、发布和状态请求由浏览器直接发往 `templates-api.azurpilot.com`。
- 资源：直接从 `https://templates.azurpilot.com/latest.json` 进入 Worker，主服务器不代理任何模板资源。
- 主服务器只接收 `POST /internal/v1/templates/events` 的小型映射变化和状态事件，不含图片或资源包。
- 主服务已移除新旧模板上传、下载、管理代理和旧模板候选/发布路由。旧 URL 返回 404，不转发、不重定向。

## 模板服务配置

```dotenv
DEPLOY_MODE=cloudflare
TEMPLATE_PUBLIC_BASE=https://templates.azurpilot.com
CF_WORKER_NAME=azurpilot-templates
CF_WORKER_DOMAIN=templates.azurpilot.com
MAIN_EVENT_URL=https://api.azurpilot.com/internal/v1/templates/events
TEMPLATE_ADMIN_TOKEN=<与统一面板 ADMIN_TOKEN 相同>
TEMPLATE_ADMIN_ORIGINS=https://api.azurpilot.com,https://stats.azurpilot.com
TEMPLATE_EVENT_TOKEN=<与主服务器相同>
AUTO_PUBLISH_SECONDS=5
```

不再配置 `TEMPLATE_INGEST_TOKEN`。客户端不需要分发任何内部令牌。

统一面板已有登录凭据由浏览器发送给模板域名；模板管理令牌配置为同一个管理员值，因此没有第二次登录，也没有主服务器管理代理。管理凭据不嵌入构建产物或 Worker。轮换管理员令牌时，两端同步轮换。

`TEMPLATE_ADMIN_ORIGINS` 为逗号分隔的精确来源白名单，只接受 HTTPS origin，本地开发允许 loopback HTTP。CORS 仅决定浏览器能否读取跨域结果，管理 API 仍始终校验 Bearer 令牌。本地面板联调需显式加入 `http://localhost:5173`，并将测试页面的 API 地址指向隔离的模板服务。

Cloudflare 账户和 API 凭据仍在模板机 `.env`，不发送到浏览器。

## 主服务器配置

```dotenv
TEMPLATE_EVENT_TOKEN=<与模板服务相同>
V3_ENABLED=true
AGG_INTERVAL=1h
```

主服务器不再使用 `TEMPLATE_ADMIN_URL`、`TEMPLATE_ADMIN_TOKEN`、`TEMPLATE_INGEST_URL`、`TEMPLATE_INGEST_TOKEN`、`TEMPLATE_PUBLIC_BASE` 或 `TEMPLATE_PROXY_CACHE`。没有模板 HTTP 客户端，也不会为了读取模板版本回源；版本只读本地事件同步状态。

映射变化通过事务写入 `template_item_mappings`、`template_sync_state`、`template_event_receipts`。原始统计仍保留 U-ID；映射发布后标记受影响历史小时并唤醒聚合。模板服务不连接主统计数据库。

`release.published` 事件只发送当次批准的映射变化；Worker 文档保留累计映射。重复事件精确 ack，同 ID 不同正文拒绝，旧版本不覆盖新映射。断网后 outbox 有序重试，最大间隔 256 秒。

## 生产目录

模板 API 位于 NAT 机 `160.202.238.171:57200`，目录 `/srv/azurpilot-template-service`，通过 `compose.production.yml` 监听映射端口 8090。

- API 容器：`azurpilot-template-identity-api-1`。
- PostgreSQL：`azurpilot-templates-postgres-1`，独立新库 `azurpilot_template_identity`，外部 Docker 网络 `azurpilot-templates_default`。
- `.env` 权限 600；数据目录 `data/`；签名私钥 `data/signing.key`。
- 非 root UID 10001 运行；Wrangler 在可写数据目录执行；启动迁移后 exec Python，正确接收停止信号。

```sh
cd /srv/azurpilot-template-service
docker compose -f compose.production.yml up -d --no-build
docker compose -f compose.production.yml logs --tail=100 api
```

主服务器位于 `43.133.246.197`，运行目录 `/srv/agentdock/AgentDock/azurpilot-cloud`，API 容器 `azurpilot-cloud-server-1`，端口 `127.0.0.1:18090`。

## 数据与签名

此前按用户要求清空两端旧业务数据，从空库 revision 1 开始。本次直连改造不再清库，不改变签名密钥或资源版本。

当前 Ed25519 原始 32 字节验签公钥的 Base64：

```text
r1HA3vwbOo1v4cDtrU5yJNAKZXuvmW8ot6cXeBC9IN4=
```

客户端需预置可信公钥，验证下载哈希和签名，在本地事务中同时更新模板、历史 U-ID 映射和版本。代码未修改游戏客户端本地数据库写入器。

## 验证

测试覆盖无服务令牌直传、批量去重、跨域预检、管理员鉴权、错误响应 CORS、限流和设备轮换、模板事件不含图像、只发送变化映射，以及主服务器所有模板路由不读取正文并返回 404。样式不在本次验收范围。
