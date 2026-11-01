# 模板协议 v1

## 未知图标上传

客户端直接调用 `POST https://templates-api.azurpilot.com/api/v3/post/templates/unknown`，不经过主服务器，不需要 `Authorization` 或共享上传令牌。可选 `X-AzurPilot-ID` 标识设备；同时按连接来源和设备限流，10 次/秒、突发 50。不要信任客户端自行填写的转发 IP 头。

```json
{
  "image_base64": "<49x49 RGB PNG base64>",
  "client_revision": 0,
  "scene": "optional-context",
  "client": "azurpilot/version"
}
```

响应示例：

```json
{
  "match": "new",
  "template_id": "U-1A2B3C4D5E6F",
  "identity_id": "U-1A2B3C4D5E6F",
  "item_id": null,
  "name": "",
  "revision": 1,
  "publication_pending": true,
  "update_required": true,
  "update_url": "https://templates.example.com/latest.json"
}
```

`match` 为 `new`、`cluster`、`formal`。正式命中直接返回正式 `template_id`；`identity_id` 保留服务器原始身份，可能是旧 U-ID。pHash 只用于筛选，不能充当唯一编号。模板接受严格的 49×49 不透明 RGB/RGBA PNG，正式导入另支持 96×96 物品格，裁剪 `(40,21)-(89,70)`。

批量接口为 `POST /api/v3/post/unknown-candidates`，正文 `{candidates:[...]}`，最多 50 项／256KiB；单图接口最多 512KiB。服务端在解码前检查尺寸和请求体大小。429 携带 `Retry-After`。这些接口只接受图标；原图使用下面的独立接口。

## 未知物品完整原图

客户端直接调用 `POST https://templates-api.azurpilot.com/api/v3/post/templates/screenshots`，无需共享凭据，使用相同的客户端限流。正文为：

```json
{
  "image_base64": "<完整 1280x720 RGB PNG base64>",
  "scene": "research",
  "client": "AzurPilot",
  "references": [{"identity_id": "U-1A2B3C4D5E6F", "bounds": [100, 200, 196, 296]}]
}
```

原始 PNG 最大 4 MiB、请求体最大 6 MiB，每张图最多关联 100 个物品位置。先注册图标，取得 `identity_id` 后再上传截图。坐标为完整截图中的物品格位置。响应 `{stored:true,sha256:"..."}` 中的哈希针对上传的原始 PNG 文件；服务端保留原始字节。相同截图只保存一次，重复关联幂等。

客户端将原图和待关联物品持久化，失败后重试、重启续传。即使本地已命中临时 U-ID，只要仍未正式映射，也会保存其完整奖励截图。正式已知物品不触发原图上传。原图只存独立模板数据库，由认证的模板管理 WebSocket 按需读取，不进入公开 Worker 资源包，也不推送或代理到主服务器。

旧版只上传过小图的历史模板不能还原原图；新版客户端再次遇到它时自动补传。管理列表的 `screenshot_count` 显示已有原图数。

## 资源下载与验签

所有下载直接访问 Worker：

1. 条件 GET `/latest.json`，用客户端预置 Ed25519 公钥验签。
2. 根据其中的 `manifest_url` 下载版本清单，先比对 `manifest_sha256` 再验签。
3. 对已知来源版本下载 `delta_base + <本地版本>.json`；来源太旧、文件不存在时使用 `0.json` 或全量 manifest。
4. 校验图标文件 SHA-256。`sha256` 是 PNG 文件哈希，`icon_sha256` 是规范 RGB 像素哈希，两者用途不同。
5. 在本地事务／可恢复更新流程中应用模板替换、历史编号映射和本地版本，全部成功才提交版本。

每份 JSON 是同一签名信封：

```json
{"payload_base64":"<UTF-8 JSON bytes base64>","signature":"<Ed25519 signature base64>"}
```

签名对象是解码后的原始字节，不依赖各语言对 JSON 的重排。Python 参考函数为 `templateserver.signing.verify(document, pinned_public_key)`。

不可变目录为 `/releases/<revision>/`，其中有 `manifest.json`、`mappings.json` 和 `deltas/<from>.json`。图标统一存放在 `/releases/assets/<PNG sha256>.png`，相同文件只保存一份，各版本通过 `url` 引用；manifest 和增量不重复嵌入图标 base64。签名 manifest 包含模板条目、匹配阈值及完整 `replace_ids`；增量包含 `upserts`、`deletions` 和累计 `replace_ids`。每次保留最近 50 个来源版本的差异，`0.json` 始终可用于全量恢复。

Worker 部署包包含当前完整版本及最近最多 50 个历史版本目录，总文件数不超过 20,000；达到预算时从最旧的历史目录开始退出公开部署。数据库和服务器本地发布归档保留完整历史。旧版本资源不存在时，客户端重新获取 `latest.json` 并使用当前完整版本；累计 `replace_ids` 不受历史目录裁剪影响。单个静态文件最大 25 MiB，超限时保留原线上版本并在管理端报告错误。

`CF_WORKER_DOMAIN` 默认使用 Custom Domain。域名已配置 CNAME 或 A 记录并通过 Worker 路由访问时，设置 `CF_WORKER_ZONE_NAME` 为所属区域名，发布沿用 `<域名>/*` 路由，不尝试创建冲突的 Custom Domain，也不修改已有 DNS 记录。

旧版本已签名清单中的 `/releases/<revision>/icons/<hash>.png` 由 Worker 静态资源规则内部改写到共享图标路径，返回原始 PNG 和 HTTP 200，不要求客户端跟随重定向，也不改变已发布清单的签名。

模板条目的稳定槽位键为 `identity_id:icon_sha256`。映射后该槽位的 `template_id` 从 U-ID 替换为正式 ID，并保留同一图标。读取器优先正式模板，再读临时模板。

服务启动后执行一次重复模板扫描，此后默认每 600 秒执行一次；`AUTO_MERGE_SECONDS=1800` 可改为每半小时。使用与在线匹配一致的图像相关性评分，默认 `TEMPLATE_MERGE_SIMILARITY=0.85`。达到阈值的临时模板合并为同一组，优先沿用已有正式编号，否则保留组内最早的 U-ID；不同正式物品编号不会相互合并。已有人工映射草稿会保留并扩展至同组旧编号，互相冲突的人工映射不被自动覆盖。

源 `identity_id`、图标变体、命中次数及原图关联保留；管理列表按合并后的 `canonical_id` 显示一行。发布清单将所有旧 U-ID 直接映射到最终编号，避免多级映射；以后将保留的 U-ID 指定为正式物品时，组内旧编号一并转为该正式物品。自动合并产生新发布版本，确认 Worker 部署后再向主服务发送变化的映射，由主服务重算受影响的历史统计。

管理员可丢弃无效模板，操作作用于同一 `canonical_id` 下的全部合并编号。丢弃后不参与在线匹配和自动合并，当前清单与增量移除这些图标条目，未发布的映射草稿清除。历史编号、已发布映射、命中次数与原图保留，不删除或改写历史统计。重复上传已丢弃的相同或相似图标不会重新建号或恢复模板；兼容旧客户端的上传确认附带 `discarded:true`。管理员恢复后以原编号重新进入后续发布。

`replace_ids` 示例：

```json
[{"unknown_id":"U-1A2B3C4D5E6F","item_id":"BlueprintT3","name":"某某图纸","tag":""}]
```

映射 `tag` 为空时保留事件原有的上下文标签；非空时才覆盖标签。同一事件中多个旧编号映射为相同物品和标签后，应合并数量并只计一次出现。

客户端必须将本地统计中对应 U-ID 的物品身份更新为映射的最终编号，包括合并后的 U-ID 和正式 ID，合并同物品记录时保留原始数量与事件关联。重放同一映射必须幂等；不能只更改未来模板而遗留历史记录。49×49 图标给现有加载器使用时，贴回 96×96 黑底画布的 `(40,21)`。

本地记录应保留不可变的 `source_template_id`，另行更新解析后的物品 ID。否则，后续修正错误映射时无法区分原本就是正式物品的记录和由旧 U-ID 转换的记录。主服务器配套接入同样保留原始 U-ID。

## 管理 WebSocket

统一入口 `https://admin.azurpilot.com`。浏览器分别连接：

- 主服务：`wss://api.azurpilot.com/api/v1/admin/ws`
- 模板服务：`wss://templates-api.azurpilot.com/internal/v1/templates/ws`

两个服务共用管理员凭据，连接后五秒内发送 `{"type":"auth","token":"管理员凭据"}`，成功返回 `{"type":"authenticated"}`。凭据不放入 URL，不通过主服务器代理模板请求。Origin 必须精确匹配各自白名单。

命令：`{"id":"唯一请求编号","method":"方法名","params":{}}`。

成功：`{"type":"result","id":"相同编号","data":{}}`。

失败：`{"type":"error","id":"相同编号","error":{"status":422,"code":"VALIDATION_ERROR","message":"参数不合法"}}`。

订阅使用 `state.subscribe`；初次响应含完整状态，变化时推送 `{"type":"event","topic":"state","data":{}}`。前端自动重连并恢复订阅，断线时拒绝尚未完成的操作，不自动重放写命令。重新连接后核对服务端状态。

| 主服务方法 | 参数 |
|---|---|
| `state.get` / `state.subscribe` / `ping` | 空对象 |
| `announcement.get` | 空对象 |
| `announcement.publish` | `title`，`content` 或 `url` 至少一项 |
| `announcement.withdraw` | 空对象 |
| `update_policy.get` | 空对象 |
| `update_policy.set` | `force_update` 布尔值 |
| `maintenance.list` / `maintenance.source` / `maintenance.sync` | 空对象 |
| `maintenance.save` | `server,name,starts_at`，可选 `ends_at`，RFC3339 时间 |
| `maintenance.delete` | `server` |
| `publish.status` | 空对象 |

| 模板服务方法 | 参数 |
|---|---|
| `state.get` / `state.subscribe` / `status.get` / `ping` | 空对象 |
| `templates.list` | 可选 `kind` formal/temporary、`limit` 1–200 默认50、`offset` 默认0、`discarded` 默认 false |
| `templates.discard` | `identity_id,discarded`；true 丢弃同组模板，false 恢复，自动排队发布 |
| `items.list` | 可选 `limit` 1–200 默认200、`offset`；返回正式物品定义 |
| `items.save` | `item_id,name,metadata`；新建物品或修改已有物品属性，自动排队发布，无需重新上传图标 |
| `templates.import` | `item_id,name,image_base64`，可选 `tag,metadata` |
| `mappings.save` | `unknown_id,item_id,name`，可选 `tag,metadata` |
| `publish` | 可选 `note`，返回排队任务 ID |
| `releases.list` | 空对象 |
| `icons.get` | `sha256`，返回 base64 图标 |
| `screenshots.list` | `identity_id`，可选 `limit` 1–50 默认10、`offset`；返回原图哈希、时间、场景、1280×720 尺寸和物品 `bounds` |
| `screenshots.get` | `sha256`，可选 `offset` 字节偏移、`length` 最大192 KiB；返回 `data_base64,offset,total_size,media_type` |

管理前端通过模板 WebSocket 分块读取原图，拼接后校验完整文件 SHA-256，支持位置标记、打开完整原图和下载 PNG。没有公开截图 GET 端点。

`metadata` 包含 `category,rarity,series,version,unit,scenes,game_id,faction,subtype,ship_id,equipment_id,tier`。其中分类、单位和统计场景必填；`series=0` 表示通用或未指定，稀有度为 0–9，版本、游戏物品 ID、阵营、细分类可留空。`ship_id` 为关联舰船组编号，仅舰船科研蓝图可填；`equipment_id` 为关联装备编号，仅装备设计图可填；`tier` 为 T0/T1 等装备版本或物品等级，与所属游戏版本 `version` 分开保存。舰船科研蓝图区分彩色与金色，装备设计图区分彩色、金色与其他；单位必须与分类一致。旧映射或导入调用可省略 metadata；`items.save` 必须提供。`templates.list` 和 `items.list` 返回已保存的属性，草稿属性放在 `draft.metadata`。正式导入和物品属性先保存在模板服务，人工映射属性随发布批准；只有部署成功后才向主服务同步。同批映射指向同一正式物品时，其名称和属性必须一致，否则拒绝发布。

签名 manifest 的 `items` 保存累计物品定义，`release.published.payload.items` 只发送本次变化的定义，结构为 `id,name` 加上述 metadata 字段。物品定义不含图像，可独立于图标变体和丢弃状态供管理端选择。主服务公开 `/api/v3/get/items` 将已发布定义与内置游戏目录合并，供产物选择和分类统计使用。

主服务单帧上限 64 KiB、每连接每秒10个命令/突发50、最多4个并行命令，单次最长100秒。模板服务单帧上限512 KiB、每连接每秒10个命令/突发50，顺序执行命令。状态变化通过连接推送，页面不做 HTTP 轮询。公开统计页面的 HTTP 请求不携带管理凭据。

服务间事件使用单独的 HTTP POST 和事件凭据，属于后端通讯，不属于管理前端 API。

## 主服务器事件

模板服务器 POST 到 `MAIN_EVENT_URL`，使用 `TEMPLATE_EVENT_TOKEN`：

```json
{
  "event_id":"00000000-0000-4000-8000-000000000001",
  "sequence":1,
  "type":"release.published",
  "revision":2,
  "created_at":"2026-10-30T00:00:00+08:00",
  "payload":{"revision":2,"replace_ids":[],"template_count":0,"manifest_url":"https://templates.example.com/releases/2/manifest.json"}
}
```

其他事件为 `template.registered`、`release.failed`。注册事件只更新状况，不能把未发布草稿当成正式映射。发送至主服务器的 `release.published.payload.replace_ids` 只包含本次已发布的映射变化，包括自动合并和人工批准的正式映射，不重复发送完整历史映射；Worker 的映射与增量仍包含累计映射。映射目标必须是最终编号，不能自引用或在同批映射中继续指向另一个来源编号。主服务器对收到的映射逐项更新，不清空未出现在当前事件中的旧映射。所有事件均不包含图标、图片 Base64 或模板包。

主服务器提交事件与映射事务后返回 `{"ack":"<相同 event_id>"}`。没有精确 ack 就持续保留并重试。事件按序交付，失败指数退避最多 256 秒；重复事件幂等，同 ID 不同正文拒绝，旧 revision 不覆盖新映射。需要审计／补拉时可带事件令牌访问 `GET /internal/v1/templates/events?after=<sequence>`。

## 主服务器流量边界

主服务器只提供 `POST /internal/v1/templates/events` 接收必要映射和状况通知。模板上传、列表、预览、导入、草稿、发布和发布记录全部直连模板服务器；模板包、增量、映射和图标下载全部直连 Worker。主服务器旧模板上传、资源代理、管理代理和旧候选/模板发布路由均已删除，返回 404。
