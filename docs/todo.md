# 待办

## R2 图片附件双向转发

隔离测试已验证 Dot 可以把生成的 PNG 直接上传到 Asset Studio 的 R2，也可以下载本机上传的 PNG 并识图。两条链路都通过 Bridge 传递请求或结果，并核对了原文件的 SHA-256。首版把临时测试流程收进 Bridge，先支持 PNG。

### 模块职责

- [ ] 在 Bridge 内新增附件模块。通过 S3 SDK 封装预签名 `PUT`、`GET` 和 `HeadObject`，使用现有 SQLite `Store` 记录 `artifact_id`、对象键、所属 Bridge 用户、文件名、类型、大小和上传状态。
- [ ] HTTP 和 MCP 入口共用附件逻辑，沿用各自现有的鉴权。R2 凭据留在 Bridge 的运行环境。签发的 URL 只授权单个对象、对应操作和有限时间。
- [ ] 附件按 Bridge 身份管理。Bridge 的附件记录和接口独立于 Aion 的会话模型。Aion 自行决定如何把 `artifact_id` 关联到消息、是否保存本地副本，以及是否优先展示本地附件。

### 上传和转发

- [ ] 申请上传时分配唯一的 `artifact_id` 和对象键，返回短期 `PUT` URL 及所需请求头。客户端将 PNG 原始字节直传 R2。使用签名的条件写入保护对象，上传重试可以复用同一个文件引用。
- [ ] 上传完成后，通过 `HeadObject` 核对对象是否存在、大小和声明类型。验证通过后才允许消息引用附件。完成确认可重试。
- [ ] 应用发给 Dot 时，沿用 `/api/messages`，在事件中传递文字和附件 ID。Dot 处理请求时通过 MCP 按 ID 获取下载地址。
- [ ] Dot 发给应用时，给 `reply_to_app` 和 `send_to_app` 增加可选的附件 ID，沿用收件箱返回回复和主动推送。校验附件归属与上传状态，并和消息在同一事务内保存引用。沿用现有的回复和推送幂等语义，将附件引用纳入重复请求的内容比较。

### Dot 定时任务主动推送附件

- [ ] 新增 MCP 工具 `request_artifact_upload(filename, content_type, size_bytes, idempotency_key)`。沿用 `send_to_app` 的 Bridge OAuth 鉴权。定时任务用已获授权的连接调用，申请只绑定 Bridge 用户，无需 `request_id` 或事件订阅。Bridge 返回 `artifact_id`、`upload_url`、`required_headers` 和 `expires_at`。
- [ ] 临时上传授权采用只对单个对象生效的 [R2 预签名 `PUT` URL](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)。Bridge 生成对象键并签入 `Content-Type` 和 `If-None-Match: *`，防止重试覆盖已上传的原图。同一身份使用相同幂等键和文件参数重试时复用 `artifact_id`，待确认的上传可重新签发；同键异参报冲突。已确认的附件只返回已有记录，不再签发写入授权。
- [ ] Dot 将原图直传 R2，再调用 `complete_artifact_upload(artifact_id)`。Bridge 确认对象后，Dot 调用 `send_to_app(text, idempotency_key, artifact_ids=[artifact_id])`。消息携带附件 ID，Aion 收到后按 ID 向 Bridge 申请短期下载地址。

### 重新下载和手动清理

- [ ] AionCore 可以随时用 `artifact_id` 向 Bridge 重新申请短期 `GET` URL。Bridge 每次验证调用者的访问权限，并根据当前 R2 对象状态签发。链接到期后可以重新申请。
- [ ] R2 原文件被手动删除后，重新申请下载地址时按附件不存在处理。如果对象在签发后被删除，实际下载返回 R2 的不存在错误。Aion 可按自身逻辑继续显示已保存的本地副本。
- [ ] 首版所有附件对象在 R2 无限期保留，由操作者在 R2 手动删除。上传链接到期只结束该地址的写入权限，收件箱 ACK 只记录消费进度，消息维护命令只处理消息记录。

### 验收

- [ ] 验证应用上传原图后 Dot 可以下载和识图。验证 Dot 上传的原图可以随回复或主动推送在应用端下载，原图哈希保持一致。
- [ ] 用已授权的真实 Dot 定时任务验证整条主动推送链路。该任务无需应用先发消息或建立事件订阅。验收包括上传授权到期后重新申请、上传与推送重试，以及 Aion 按附件 ID 下载原图。
- [ ] 验证上传及回传重试、下载链接过期后重新签发、其他 Bridge 身份访问被拒绝，以及 R2 手动删除后的正常失败。
- [ ] 验证纯文本消息、现有收件箱游标和 ACK 行为保持兼容。
