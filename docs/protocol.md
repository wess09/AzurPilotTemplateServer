# 模板协议 v1

## 未知图标上传

客户端入口沿用主服务器 `POST /api/v3/post/templates/unknown`，主服务器转发到独立模板服务的同路径，使用 `TEMPLATE_INGEST_TOKEN`。

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

批量接口为 `POST /api/v3/post/unknown-candidates`，正文 `{candidates:[...]}`，最多 50 项／256KiB；单图接口最多 512KiB。服务端在解码前检查尺寸和请求体大小。429 携带 `Retry-After`。场景不是统计业务白名单，不上传整张截图或事件行。

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

不可变目录为 `/releases/<revision>/`，其中有 `manifest.json`、`mappings.json`、`deltas/<from>.json` 和 `icons/<PNG sha256>.png`。签名 manifest 包含模板条目、匹配阈值及完整 `replace_ids`；增量包含 `upserts`、`deletions` 和累计 `replace_ids`。每次保留最近 50 个来源版本的差异，`0.json` 始终可用于全量恢复。

模板条目的稳定槽位键为 `identity_id:icon_sha256`。映射后该槽位的 `template_id` 从 U-ID 替换为正式 ID，并保留同一图标。读取器优先正式模板，再读临时模板。

`replace_ids` 示例：

```json
[{"unknown_id":"U-1A2B3C4D5E6F","item_id":"BlueprintT3","name":"某某图纸","tag":""}]
```

映射 `tag` 为空时保留事件原有的上下文标签；非空时才覆盖标签。同一事件中多个旧编号映射为相同物品和标签后，应合并数量并只计一次出现。

客户端必须将本地统计中对应 U-ID 的物品身份更新为正式 ID，合并同物品记录时保留原始数量与事件关联。重放同一映射必须幂等；不能只更改未来模板而遗留历史记录。49×49 图标给现有加载器使用时，贴回 96×96 黑底画布的 `(40,21)`。

本地记录应保留不可变的 `source_template_id`，另行更新解析后的物品 ID。否则，后续修正错误映射时无法区分原本就是正式物品的记录和由旧 U-ID 转换的记录。主服务器配套接入同样保留原始 U-ID。

## 管理接口

以下是模板服务内部接口，使用 `TEMPLATE_ADMIN_TOKEN`。浏览器通过主服务器的 `/api/v1/admin/template-service/*` 管理代理调用，不持有模板服务令牌。

| 方法 | 模板服务路径 | 功能 |
|---|---|---|
| GET | `/internal/v1/templates/status` | 版本、草稿数、发布和推送状态、公钥 |
| GET | `/internal/v1/templates` | 模板列表，支持 `kind/limit/offset` |
| POST | `/internal/v1/templates` | 导入 `{item_id,name,tag,image_base64}` 正式模板 |
| PUT | `/internal/v1/templates/mappings/<U-ID>` | 保存 `{item_id,name,tag}` 草稿 |
| POST | `/internal/v1/templates/publish` | 冻结当前草稿、排队发布，返回 202 和任务 ID |
| GET | `/internal/v1/templates/releases` | 发布历史与失败状态 |

已存在的未知图标如果要命名，使用映射接口，不通过重复导入绕过审核。

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

其他事件为 `template.registered`、`release.failed`。注册事件只更新状况，不能把未发布草稿当成正式映射。

主服务器提交事件与映射事务后返回 `{"ack":"<相同 event_id>"}`。没有精确 ack 就持续保留并重试。事件按序交付，失败指数退避最多 300 秒；重复事件幂等，同 ID 不同正文拒绝，旧 revision 不覆盖新映射。需要审计／补拉时可带事件令牌访问 `GET /internal/v1/templates/events?after=<sequence>`。
