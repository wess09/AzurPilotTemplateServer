# AzurPilot 模板服务器

独立部署的模板身份服务。接收图标，匹配正式或临时模板，给未知物品分配稳定的 `U-xxxx`；人工映射发布后，同时更新客户端资源与主统计服务器的映射。

客户端直接访问 `https://templates-api.azurpilot.com/api/v3/post/templates/unknown`，上传不需要共享服务令牌；主服务器禁止代理模板上传、下载和管理。

服务从空模板库启动，不依赖已有客户端模板。它不接收掉落事件、不计算统计、不识别整张截图，也没有第二套管理登录。模板管理页面位于 Cloudflare 托管的 `https://admin.azurpilot.com` 统一面板，通过 WebSocket 直接连接两个后端。前端独立仓库为新建 `wess09/AzurPilotCloudWeb`，由 GitHub 原生关联自动部署。

## 职责与数据流

```mermaid
flowchart LR
  C[客户端] -->|图标直接上传| T[独立模板服务器]
  A[统一管理面板的浏览器] -->|管理 WebSocket 直接连接| T
  T -->|发布静态资源| W[Cloudflare Worker Static Assets]
  C -->|直接下载模板与映射| W
  T -->|仅映射变化与状态通知| M[主统计服务器]
```

- 首次未知图标：保存规范图，分配随机且唯一的 `U-` 编号，自动排队发布临时模板。
- 正式模板或已编号临时模板：复用既有 ID，返回当前资源版本和 `update_required`。
- 编号全局有效，不以统计场景分库，`scene` 只作可选请求上下文。
- 映射先保存草稿；管理员发布时冻结当时的草稿，自动发布不会顺带发布未批准的映射。
- 旧 `U-` 永久作为别名保存。资源包同时包含正式模板替换和 `replace_ids`，离线客户端也能补齐历史映射。
- 主服务器保存映射副本并重算受影响的历史统计；模板服务不访问主服务器数据库。

## 技术与可维护性约定

Python / FastAPI / SQLAlchemy 2 / Alembic / PostgreSQL / OpenCV。数据库定义在 `templateserver/models.py`，迁移在 `migrations/versions/`。

**完全禁止手写 SQL：** 业务、迁移、约束、测试和维护工具均使用 ORM 或 SQLAlchemy 表达式；不使用 `text()`、`exec_driver_sql()`、SQL 字符串或 `.sql` 文件。测试会检查这些入口。新增字段通过 Alembic 新迁移升级，不在应用启动时猜测和重建表。

目录职责：

| 目录／文件 | 职责 |
|---|---|
| `models.py` / `store.py` | 模型、事务和数据访问 |
| `imaging.py` | PNG 规范化、像素哈希、pHash、颜色与 NCC/L2 匹配 |
| `publishing.py` / `signing.py` | 不可变快照、签名、增量、Worker 部署与恢复 |
| `delivery.py` | 有序 outbox 推送、确认和退避重试 |
| `api.py` | 请求模型、认证、大小限制及管理接口 |
| `worker/` | 纯静态 Worker 配置，无业务回源 |
| `tests/` | 真实 PostgreSQL 并发和闭环测试 |

## 本地启动

需要 Python 3.12+、Node.js 22+、PostgreSQL，以及 uv。

```powershell
uv sync --frozen
npm ci
Copy-Item .env.example .env
# 填写 .env 中的数据库、与统一面板一致的管理员令牌和事件令牌。
uv run --env-file .env alembic upgrade head
uv run --env-file .env template-server
```

本地模式设置 `DEPLOY_MODE=local`、`TEMPLATE_PUBLIC_BASE=http://127.0.0.1:8787`，另一个终端运行 `npm run worker:dev`。首次启动即生成空库的第一个版本；生产必须设为 `DEPLOY_MODE=cloudflare`。

签名私钥首次生成后保存在 `DATA_DIR/signing.key`。必须持久化、备份，多个实例共用同一密钥。管理状态接口返回公钥，客户端应通过可信配置预置公钥，不可接受资源包自带的任意新公钥。

## Docker 部署

填写 `.env` 中的 `POSTGRES_PASSWORD`（URI 安全字符）、与统一面板一致的管理员令牌、随机事件令牌和 Cloudflare 参数，然后运行：

```powershell
docker compose up -d --build
```

模板数据库与主服务器数据库独立。Compose 不公开数据库端口，模板 HTTP 只绑定本机 8093；通过自己的 HTTPS 入口供客户端和管理浏览器直连。镜像包含 Node、Wrangler 和锁定的 Python 依赖。启动先执行 Alembic，再启动服务。

Cloudflare 参数为 `CLOUDFLARE_ACCOUNT_ID`、`CLOUDFLARE_API_TOKEN`、`CF_WORKER_NAME`、`CF_WORKER_DOMAIN`、`TEMPLATE_PUBLIC_BASE`。发布器通过 Wrangler 部署静态文件，并读取 Worker 上的 `latest.json` 确认内容后才激活版本。没有配置凭据时不会假装已经云端部署。

Worker 使用 [Static Assets](https://developers.cloudflare.com/workers/static-assets/)，无业务脚本。版本文件长期缓存，`latest.json` 条件更新；ETag 和缓存规则遵循 [Cloudflare 静态资源文档](https://developers.cloudflare.com/workers/static-assets/headers/)。兼容日期固定为本次本地运行验证支持的 `2026-10-28`，避免 UTC 日期尚未到达时配置未来日期。

## 接入主服务器与客户端

未知物品还支持完整 1280×720 奖励截图：客户端直传模板服务，数据库按文件哈希去重，
关联各个 U-ID 和物品格坐标。后台通过认证的模板 WebSocket 分块读取原图，支持位置
标记和原始 PNG 下载；截图不会进入主服务器或公开 Worker 资源包。端点及管理命令见
[原图协议](docs/protocol.md#未知物品完整原图)。

详见 [协议](docs/protocol.md) 和 [部署配置](docs/integration.md)。配套主服务器仓库为 `AzurPillotCloudServer`：仅保留事件接收和同面板的直连管理页面，复用现有后台登录凭据；该仓库里的旧 `cmd/templateserver` 不参与本服务部署。

本项目提供客户端资源协议及验签函数，**没有修改游戏客户端源码**。客户端需按协议在本地事务中同时应用 `replace_ids` 和模板替换；这是客户端负责的数据库操作。

## 验证

```powershell
$env:TEST_DATABASE_URL='postgresql+psycopg://user:password@localhost:5432/templates_test'
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run --env-file .env alembic check
npm run worker:dry-run
```

数据库测试使用独立随机 schema，并通过 ORM 创建及清理，不碰生产数据。没有 `TEST_DATABASE_URL` 时会明确跳过 PostgreSQL 用例。CI 会启用 PostgreSQL，执行迁移、结构检查、测试和静态 Worker 部署检查。

`tools/verify_local.py` 是针对本机测试端口 18093 / 18096 / 8787 的完整闭环脚本：上传图标、写入 51 条主服务器测试记录、通过统一后台 API 发布映射、验证事件确认、历史重算以及 Worker 资源验签。只用于隔离的本地测试环境。

## 已知边界

- 尚无真实游戏模板样本，因此没有声称达到计划中的匹配召回率或假合并率。当前阈值是保守起点，需要真实正负样本校准。
- Cloudflare 分发、数据库激活、主服务器确认是可恢复的阶段，不能伪装成跨系统数据库事务。网络中断时同一冻结版本重试，客户端以签名发布包为准，主服务器以确认事件为准，后台显示同步积压。
- 新增临时图标会短暂等待自动合批发布；上传响应先返回 ID，并通过 `publication_pending` 明确说明。
