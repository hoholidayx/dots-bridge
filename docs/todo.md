# 待办

## R2 附件联调

Bridge 已提供任意文件格式的双向附件 API，以及统一的 `send_to_app`。接口说明见[技术参考](reference.md#attachments-and-final-sends)，R2 配置和任务切换见[部署文档](../DEPLOYMENT.md#启用-r2-附件)。

- [ ] 更新 Dot 技能与任务指令，再刷新 MCP 工具并验证纯文本请求、回复和主动推送。
- [ ] 用真实 Dot 云端定时任务主动推送附件，通过私有 API 下载并核对原文件 SHA-256。
- [ ] 通过客户端向 Dot 发附件，验证 Dot 能下载原文件并用同一请求 ID 回传附件。覆盖图片、文档或压缩包、无扩展名文件和空文件。
- [ ] 用真实 R2 验证过期后重新签发、重复 PUT、手动删除后的下载失败，以及 Cloudflare 对私有 API 的拦截。
- [ ] 将 Bridge 附件接入 AionCore 和 AionUi，由 Aion 管理附件的会话关联、本地副本和展示。
