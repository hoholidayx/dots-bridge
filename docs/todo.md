# 待办

## R2 附件联调

Bridge 已提供任意文件格式的双向附件 API，以及统一的 `send_to_app`。接口说明见[技术参考](reference.md#attachments-and-final-sends)，R2 配置和任务切换见[部署文档](../DEPLOYMENT.md#启用-r2-附件)。

- [x] 更新 Dot 技能与任务指令，刷新 MCP 工具，验证纯文本请求和回复。
- [x] 用真实 Dot 云端定时任务主动推送无扩展名附件，通过私有 API 下载并核对原文件 SHA-256。
- [x] 通过客户端向 Dot 发 PNG 和 ZIP，验证 Dot 下载原文件并用同一请求 ID 回传附件，逐一核对 SHA-256。
- [x] 用真实 R2 验证空文件上传、校验和错误、重复 PUT 和手动删除后的下载失败，并验证 Cloudflare 拦截私有 API。
- [ ] 补测真实签名 URL 过期后的重新签发。
- [ ] 将 Bridge 附件接入 AionCore 和 AionUi，由 Aion 管理附件的会话关联、本地副本和展示。
