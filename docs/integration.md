# 两个独立服务的接入

## 模板服务器

部署本仓库自己的进程和 PostgreSQL。设置：

```dotenv
DEPLOY_MODE=cloudflare
TEMPLATE_PUBLIC_BASE=https://templates.example.com
CF_WORKER_DOMAIN=templates.example.com
MAIN_EVENT_URL=https://main.example.com/internal/v1/templates/events
TEMPLATE_INGEST_TOKEN=<上传转发令牌>
TEMPLATE_ADMIN_TOKEN=<模板管理服务令牌>
TEMPLATE_EVENT_TOKEN=<事件推送令牌>
```

Cloudflare 账户、API token、Worker 名称见 `.env.example`。凭据放服务器环境，不放前端、资源包或 Git。多个模板实例共享数据库、持久化签名密钥和发布目录；单个自动发布器与事件投递器通过 ORM 行锁协调。

## 主服务器 AzurPillotCloudServer

使用本次配套提交，设置：

```dotenv
TEMPLATE_ADMIN_URL=https://templates-api.example.com
TEMPLATE_ADMIN_TOKEN=<与模板服务相同>
TEMPLATE_INGEST_URL=https://templates-api.example.com/api/v3/post/templates/unknown
TEMPLATE_INGEST_TOKEN=<与模板服务相同>
TEMPLATE_EVENT_TOKEN=<与模板服务相同>
TEMPLATE_PUBLIC_BASE=https://templates.example.com
```

已有 `ADMIN_TOKEN` 是管理员的主后台登录凭据，与模板服务的管理令牌分开。浏览器访问主服务器 `/admin/` 的“模板服务”，管理 API 由主服务器代理至独立服务。模板服务器没有第二个 `/admin/` 页面。

主服务器只保存 `template_item_mappings`、`template_sync_state` 和 `template_event_receipts` 等映射副本／接收状态，不读写模板库。收到发布事件后事务保存映射与回执，标记受影响的历史小时并唤醒聚合；原始统计行仍保留旧 U-ID，聚合时解析为正式物品，不丢数量。新业务可复用相同映射表，不需要重新编号。

本次接入依赖主服务器现有 `V3_ENABLED=true`；关闭时映射接收不可用，模板端会持久化等待，不丢事件。

## 验收顺序

1. 启动空模板服务，确认 PostgreSQL 已迁移、签名密钥已持久化。
2. 在主后台确认能打开模板服务页，不需要第二次登录。
3. 上传同一图标两次并改变 `scene`，确认只得到一个 U-ID。
4. 从 Worker 读取临时模板包，确认包含上传图标和编号。
5. 保存映射草稿，确认此时仍返回 U-ID。
6. 发布映射，确认 Worker 包含正式 ID 替换与累计 `replace_ids`。
7. 确认主服务器事件已 ack、历史数据可按正式物品统计。
8. 断开主服务器后重新发布，确认积压可见；恢复后确认自动补发。
9. 使用旧版客户端资源版本拉增量／全量，确认仍包含历史映射。

## 故障与恢复

- Worker 发布失败：当前 DB revision 不推进，冻结快照保留，后台显示失败并自动重试。
- Worker 已上线而模板进程中断：重启继续确认和激活同一 revision，避免分配另一个版本覆盖中间状态。
- 主服务器离线：outbox 保留事件；恢复后重试，主服务器按事件 ID 幂等处理。
- 签名密钥丢失：必须从备份恢复；不可生成新密钥后让客户端盲信。
- 修正错误映射：编辑同一 U-ID 的新草稿，发布更大的 revision；历史 U-ID 保留，因此可重新解析。
- 客户端更新失败：不提交本地版本，下一次重放同一已签名映射；正式数据更新需由客户端事务保证。

当前代码不实现客户端本地数据库写入器，不要求重新上传或废弃旧统计数据。

## NAT 生产部署

本次部署使用以下固定入口：

- 模板 API：`https://templates-api.azurpilot.com`，NAT 机映射到 HTTP 8090。
- 静态分发：`https://templates.azurpilot.com`，由 Cloudflare Worker 提供资源。
- 主服务器事件接收：`https://api.azurpilot.com/internal/v1/templates/events`。

生产服务目录为 `/srv/azurpilot-template-service`，配置与三个服务间令牌保存在该目录的 `.env`（权限 600），签名私钥位于 `data/signing.key`。主服务器保留自己的管理员登录令牌；浏览器不接触模板服务的内部令牌。

小内存 NAT 机可以使用 `compose.production.yml`，复用已有 PostgreSQL 实例，但使用独立数据库。`DATABASE_URL` 指向新库；`TEMPLATE_DB_NETWORK` 指向数据库所在的外部 Docker 网络，不能直接复用旧表结构。本次按用户要求清空主服务器和模板服务器的旧业务数据，以空库重新部署。

```sh
docker compose -f compose.production.yml up -d --no-build
```

先设置 `TEMPLATE_PUBLISHED_PORT=127.0.0.1:18093` 做本机验证，再改为 `8090` 并停止旧 API 容器后切换。`TEMPLATE_IMAGE` 指定已构建的镜像。不要同时让两个服务争用 8090，也不要停止被新服务复用的 PostgreSQL 容器。

本次是完整重置：不迁移旧模板、旧映射或旧发布记录，使用新的签名密钥，从 revision 1 重新发布 Worker 资源。客户端需使用本次新公钥；常规升级仍应保持版本单调递增和签名密钥稳定。

Wrangler 在可写的数据目录执行；运行用户的 HOME 位于 `/home/templates`，避免非 root 容器向只读应用目录写缓存。镜像基础层固定到已验证的 SHA-256。

容器在迁移完成后通过 `exec` 让 Python 服务成为 PID 1，以正确接收停止信号；本次生产实测重启 1.88 秒，重启后 revision 与签名公钥保持不变。

当前生产验签公钥（Ed25519 原始 32 字节，Base64）：

```text
r1HA3vwbOo1v4cDtrU5yJNAKZXuvmW8ot6cXeBC9IN4=
```

本次线上验证包括：空模板库 revision 1、Worker 签名与 manifest SHA-256、ETag/304、未授权上传 401、非法图标 400、重复事件精确 ack、主服务器统一管理代理及待同步事件为 0。未向生产库写入演示模板或演示统计。
