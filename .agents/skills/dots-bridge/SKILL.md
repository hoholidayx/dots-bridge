---
name: dots-bridge
description: 通过已连接的 dots-bridge MCP 处理 Aion 发来的请求和附件，回传进度及结果，或主动把用户授权的定时任务结果、通知和文件发给 Aion。也用于检查连接和管理消息监听。
---

# 通过 Bridge 与 Aion 通信

使用已授权的 `dots-bridge` MCP 连接。需要确认连接时，调用 `get_current_user()` 核对 Bridge 用户。工具不可用、鉴权失败或需要审批时，说明卡在哪一步，按当前环境的提示处理。以工具返回结果确认 Bridge 是否已保存消息。

## 接收 Aion 请求并回复

- 用户要求监听时，用 Dot 的事件任务功能订阅 `app.message.created`，频道用用户指定的值，省略时用 `default`。已有匹配的监听任务时，更新它的指令。订阅结果不明确时，先检查已有任务再决定是否重试。用户要求停止时，通过 Dot 的任务管理停止对应监听。
- 监听任务读取事件 `data` 中的 `request_id`、`text` 和可选的 `artifact_ids`。按正文和附件处理该次请求。需要补全原文、元数据或确认请求状态时，调用 `get_message(request_id)`。同一个 `request_id` 再次送达时，先核对状态，避免重复执行有副作用的操作。
- 耗时任务可用 `report_progress(request_id, text, idempotency_key)` 回传有实质进展的更新。每条新进度用新键，重试同一进度时保留原键和原文。进度发出后，请求仍在等待最终回复。
- 调用 `send_to_app(kind="reply", request_id=原请求ID, text=正文, artifact_ids=[...])` 回传最终结果。正文和附件至少提供一项。频道和元数据由 Bridge 从原请求继承。结论是无需处理或任务无法继续时，也用最终回复回传原因。重试时复用原请求 ID、正文和有序附件列表。`reply_status` 为 `replied` 表示最终回复已保存。
- 后来的请求明确替换同一频道、同一订阅里的旧请求时，可用 `cancel_request(request_id, superseded_by)` 关闭仍未完成的旧请求。`superseded_by` 填实际的新请求 ID。独立请求各自继续并回传各自的结果。关闭 Bridge 请求不会中止 Dot 已经开始的工作。

## 主动推送和定时任务

- 对用户已授权发送给 Aion 的通知、文件或任务结果，调用 `send_to_app(kind="push", idempotency_key=..., text=..., artifact_ids=[...])`。正文和附件至少提供一项，可指定 `channel` 和 JSON 对象 `metadata`，默认频道为 `default`。定时任务可直接推送，无需 `request_id` 或事件订阅。
- 每次逻辑推送使用唯一且可重现的键，例如任务标识加原定触发时间。同一次运行重试时复用原键、正文、附件顺序、频道和元数据。遇到幂等冲突时核对原推送，避免换键重发。
- 定时任务写明要做的工作、触发时间和时区、何时需要发送结果，以及 Aion 的目标频道。任务只在用户授权的范围内发消息。带 `request_id` 的应用请求走上面的回复流程。

## 文件上传与下载

1. 拿到可读的原始文件，计算字节数和 MD5 原始摘要的 Base64 值。调用 `request_artifact_upload(filename, size_bytes, content_md5, idempotency_key, content_type?)`。文件名只填 basename，类型未知时可省略。每个逻辑文件有独立的上传幂等键。
2. 返回 `pending` 时，按 `required_headers` 将原始字节直接 `PUT` 到 `upload_url`。用二进制上传，保留所有签名请求头，Bridge 的 OAuth 令牌只用于 MCP。链接过期后用原上传参数和键重申。收到 `412` 或返回 `ready` 时，使用返回的附件 ID 继续提交，让 Bridge 核验已有对象。
3. 把附件 ID 放到相应的 `send_to_app` 调用中。Bridge 保存成功后才算交付。每条消息最多 4 个文件，每个最多 20 MiB，任意格式均可传。若拿不到文件原始字节，回传可验证的状态。
4. 读取附件时调用 `get_artifact_download(artifact_id)`，按返回的 `download_url` 下载字节，再用 `filename` 和 `content_type` 识别。链接有效期为 10 分钟，过期后凭原 ID 重申。

回复、进度和主动消息的正文上限为 64,000 字符。消息中使用附件 ID，签名 URL 仅用于传输文件。
