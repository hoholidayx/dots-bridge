# Dots Bridge VPS 部署

本文记录 Ubuntu VPS 的部署和验证步骤。示例中的 `<VPS_HOST>`、`<SSH_PORT>`、`<PUBLIC_MCP_HOST>`、`<BRIDGE_REPO_URL>` 和回调 URL 由部署者填写。

## 网络和目录

| 流量 | 路径 |
| --- | --- |
| ChatGPT 调用 Bridge | 公网 HTTPS → Cloudflare Tunnel → `127.0.0.1:8765` |
| 应用访问 Bridge HTTP API | 本机回环转发 → NAS HTTPS → Tailscale 业务端口 → `127.0.0.1:8765`，也可使用 SSH 本地端口转发 |
| Bridge 验证订阅及投递事件 | Bridge → VPS 上的 Mihomo SOCKS5 `127.0.0.1:17892` → 上游节点 → OpenAI |
| Bridge 查询 R2 | Bridge → 同一回环端口的 Mihomo HTTP 代理 → R2 HTTPS |

| 路径 | 内容 |
| --- | --- |
| `/root/workspace/dots-bridge/source/` | Bridge Git 工作树 |
| `/root/workspace/dots-bridge/runtime/` | `bridge.toml`、SQLite 数据、uv、Python 环境、管理脚本和日志 |
| `/root/workspace/cloudflared/` | Cloudflare 连接器、凭据文件、独立 DNS 配置、管理脚本和日志 |
| `/root/workspace/mihomo/` | Mihomo、私有上游配置、运行数据、管理脚本和日志 |

三个运行目录各自的 `service` 脚本支持 `start`、`stop`、`restart` 和 `status`。进程使用各自的系统用户。恢复到新 VPS 时，随运行目录恢复管理脚本，重建对应的系统用户和目录权限。为服务账户提供到各自目录的路径遍历权限，并按账户设置凭据文件的读取权限。迁移 Bridge 前停服务，并一起备份配置与 SQLite 数据，保留用于读取现有订阅数据的存储密钥。

## Bridge 安装和更新

在新 VPS 上以管理员身份运行。先在当前终端设置 `BRIDGE_REPO_URL`。使用项目所支持的 Python 版本和 uv，生产环境按锁文件安装依赖。

```bash
git clone "$BRIDGE_REPO_URL" /root/workspace/dots-bridge/source
cd /root/workspace/dots-bridge/source
UV_PROJECT_ENVIRONMENT=/root/workspace/dots-bridge/runtime/venv \
  uv sync --locked --no-dev --no-editable --no-python-downloads --python /usr/bin/python3.12
```

新部署使用 `dots-bridge init --config <CONFIG_PATH> --public-url https://<PUBLIC_MCP_HOST>` 初始化。迁移已有部署时使用备份的配置和数据库。更新代码时，先核对 Git 目标与工作树，再停止 Bridge、快进到发布提交、按锁文件同步依赖、检查运行账户可读取虚拟环境并启动。保持 `runtime/bridge.toml` 的私有权限。

```toml
public_base_url = "https://<PUBLIC_MCP_HOST>"
host = "127.0.0.1"
port = 8765
redirect_uri = "<EXACT_CHATGPT_OAUTH_CALLBACK_URL>"
webhook_proxy_url = "socks5://127.0.0.1:17892"
```

首次注册时，先把 `https://<PUBLIC_MCP_HOST>/mcp` 添加为 OAuth 自定义 MCP，使用 `bridge.toml` 配置的客户端 ID。把注册页面复制出的完整回调地址写入 `redirect_uri`，重启 Bridge 后完成授权。默认客户端 ID 为 `dots-bridge`，令牌端点认证方式为 `none`，权限范围为 `events:read messages:reply`。

### 启用 R2 附件

在私有 `bridge.toml` 末尾填写部署选定的 R2 桶。Bridge 将对象写入该桶的 `artifacts/` 前缀。

```toml
[r2]
account_id = "<CLOUDFLARE_ACCOUNT_ID>"
bucket = "<PRIVATE_R2_BUCKET>"
```

本部署的 `service` 启动器从 `runtime/r2-credentials.json` 读取 `DOTS_BRIDGE_R2_ACCESS_KEY_ID` 和 `DOTS_BRIDGE_R2_SECRET_ACCESS_KEY`，并把 `HTTPS_PROXY` 指向 `http://127.0.0.1:17892`。凭据文件由 root 持有，权限为 `0600`。其他部署可通过进程环境提供这两个变量，并按网络条件配置 HTTPS 代理。

Dot 和应用使用短期 URL 直接上传和下载。应用通过私有连接访问 `/api/artifacts/*` 申请授权。附件由操作者在 R2 手动清理。

升级前暂停 Dot 监听和主动推送任务，等在途请求完成，停止 Bridge 并备份 SQLite 和配置。升级会迁移到 schema 6。重启后刷新 Dot 的工具列表，用新版技能更新任务指令。回复使用 `send_to_app(kind="reply", request_id=...)`，主动推送使用 `kind="push"`。验证纯文本双向收发后再恢复日常任务。需要回退至 schema 5 时，使用升级前备份，并保留升级后的数据库以供恢复期间产生的记录。

## Cloudflare 公网入口

1. 创建托管 Tunnel，在 VPS 的 `cloudflared/token` 保存连接凭据。连接器通过文件读取凭据，启动时使用独立解析器及 HTTP/2。
2. 为 `<PUBLIC_MCP_HOST>` 创建 Published application，服务地址为 `http://127.0.0.1:8765`。VPS 允许连接器出站 TCP `7844`。
3. 给 Bridge 域名添加 WAF Block 条件，仅开放下列公网路径。与已有的地区或白名单拦截条件合并时，用 `or` 连接两个完整条件。

```text
(
  http.host eq "<PUBLIC_MCP_HOST>"
  and not (http.request.uri.path in {
    "/health" "/mcp" "/authorize" "/consent" "/token" "/revoke"
    "/.well-known/oauth-authorization-server"
    "/.well-known/oauth-protected-resource/mcp"
  })
)
```

ChatGPT 使用公网 `/mcp` 和 OAuth 路径。

## 应用私有入口

应用通过私有连接访问 `/api/*`，并使用 Bridge 的应用 API 密钥鉴权。Aion 的 Base URL 使用 `http://127.0.0.1:18765`。切换底层路径时沿用该地址和 Aion 数据目录，原有收件游标和请求关联即可继续使用。

### 经 NAS 和 Tailscale

应用主机在局域网内连接 NAS。NAS 和 VPS 加入同一个 Tailscale 网络，应用主机只需能访问 NAS。

1. 在 VPS 使用 Tailscale Serve 将业务端口 `18766` 转发到 Bridge 的回环端口。VPS 防火墙无需开放该公网端口。按 tailnet 的访问策略授权 NAS 访问该业务端口，Bridge 仍会校验应用 API 密钥。

   ```bash
   tailscale serve --bg --tcp=18766 tcp://127.0.0.1:8765
   tailscale serve status
   ```

2. 在 NAS 的 Nginx 增加专用 HTTPS 监听，使用 SAN 包含 `<NAS_LAN_IP>` 的证书，仅允许应用主机访问健康检查和应用 API。将下面的占位符替换为部署值，运行 `nginx -t` 后再重载。

   ```nginx
   server {
       listen <NAS_LAN_IP>:18766 ssl;
       server_name <NAS_LAN_IP>;
       ssl_certificate <NAS_TLS_CERT_PATH>;
       ssl_certificate_key <NAS_TLS_KEY_PATH>;
       ssl_protocols TLSv1.2 TLSv1.3;
       allow <APP_LAN_IP>;
       deny all;

       proxy_http_version 1.1;
       proxy_set_header Host 127.0.0.1:8765;
       proxy_set_header Connection "";
       proxy_connect_timeout 5s;
       proxy_read_timeout 15s;

       location = /health {
           proxy_pass http://<VPS_TAILSCALE_IP>:18766;
       }
       location /api/ {
           proxy_pass http://<VPS_TAILSCALE_IP>:18766;
       }
       location / {
           return 404;
       }
   }
   ```

3. 在应用主机运行本机转发，监听 `127.0.0.1:18765` 并通过 TLS 连接 `<NAS_LAN_IP>:18766`。从可信管理通道复制 NAS 的证书或签发 CA 证书，转发器必须验证证书和 NAS 地址。NAS 保管 TLS 私钥，应用主机只需信任证书。轮换证书时同步更新转发器的信任配置。
4. 验证本机 `/health` 和带应用令牌的 `/api/subscriptions`、收件游标。NAS 的 `/api/subscriptions` 在未带令牌时应返回 `401`，`/mcp` 应返回 `404`。VPS 公网地址上的 `18766` 应不可达。

Aion 工作区若已安装本机中转脚本，可用 `./manage-services.py {start|stop|restart|status} dots-bridge` 管理。`all` 在 Web 之前启动中转，并在 Web 停止后关闭中转。需要切回已配置的 SSH 转发时，运行 `./local/dots-bridge/manage-tunnel.py restart --transport ssh`。

### SSH 备用路径

在应用主机设置 `SSH_PORT`、`SSH_USER` 和 `VPS_HOST`，确保本机转发端口可用，再建立 SSH 本地端口转发。

```bash
ssh -NT -p "$SSH_PORT" \
  -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18765:127.0.0.1:8765 \
  "$SSH_USER@$VPS_HOST"
```

## VPS 独立回调代理

使用 Mihomo 官方 Linux 发行包并核对 SHA-256。将批准使用的上游节点配置放入 VPS 的 `mihomo/config.yaml`，权限为 `0640`，由管理员写入、Mihomo 服务账户读取。当前管理脚本读取 JSON 格式的 YAML 配置，使用以下监听和路由字段。

- `mixed-port` 为 `17892`，供回调使用 SOCKS5，并供 R2 查询使用 HTTP 代理。
- `allow-lan` 为 `false`，`bind-address` 为 `127.0.0.1`。
- `mode` 为 `rule`，专用入口的最终 `MATCH` 规则指向已配置的上游节点。

Bridge 在 webhook 路径上使用显式的 `webhook_proxy_url`，通过同一个 SOCKS5 出口做加密 DNS 查询，并连接验证过的目标 IP。代理分流需覆盖该入口收到的数字 IP 连接。Cloudflare Tunnel 和 SSH 使用 VPS 的常规路由。

## 启停与验收

在 VPS 上依次启动各服务。

```bash
/root/workspace/mihomo/service start
/root/workspace/dots-bridge/runtime/service start
/root/workspace/cloudflared/service start
```

把命令末尾替换为 `status` 查看 PID、监听地址和日志位置。停止整套服务时按相反顺序执行 `stop`。部署完成后按以下顺序验证链路。

1. 检查 Mihomo 的 SOCKS5 监听和 Cloudflare Tunnel 连接数。通过 SOCKS5 对 OpenAI 回调主机发起 HTTPS 探测，确认证书校验通过。
2. 公网 OAuth 发现端点应返回 `200`。公网 `/api/subscriptions` 和包含编码斜杠的 `/api%2Fsubscriptions` 都应返回 Cloudflare `403`。通过私有连接并携带应用 API 密钥访问同一 API 应成功。
3. 在目标 Dot 建立 `app.message.created` 订阅。通过私有 API 提交唯一测试标记，确认 webhook 接收事件，再按 `request_id` 检查收件箱里的最终回复。
4. 在 Dot 的授权范围内单独测试 `send_to_app`，在收件箱确认 `kind: "push"`。Webhook 接收成功与 Dot 执行结果可分别核对。Dot 的任务记录可查看操作授权和运行状态。

公开入口的拦截记录从 Cloudflare Security Events 按 Ray ID 查询。订阅请求到达 Bridge 后出现 `challenge_failed` 时，检查 VPS 代理、经代理的 DNS 解析及回调主机的 HTTPS 可达性。
