# PPFlight PDF Agent

PPFlight 的独立账单 PDF 工作节点：通过 HTTPS 向主站领取任务，在远端生成并私有保存文件。
不连接主站数据库，不使用 Docker。后台可查看生成记录、预览和下载账单。

## 1. 后台准备

在 PPFlight 后台的“账单文件设置”中：

1. 填写并保存账单销售方资料、邮箱及 HTTPS 下载域名，例如 `https://pdf-worker.ppflight.com`。
2. 生成一次性绑定码，有效期 15 分钟；安装时按提示粘贴，输入不回显。

后台只允许绑定一个主 PDF Agent。绑定码和其他凭据不要提交到仓库。

## 2. 一条命令安装

在 PDF 服务器以 root 执行：

```bash
bash <(curl -fsSL --connect-timeout 20 --retry 2 https://raw.githubusercontent.com/ppflight/ppflight-pdf-agent/v1.0.11/bootstrap.sh)
```

脚本自动下载 v1.0.11、校验归档、安装缺失依赖、启动服务并引导绑定。
发行包已包含 PDF 渲染依赖，目标服务器无需运行 Composer 或 pip。

重复执行会保留已有绑定和 PDF。同版本跳过安装；升级保留原目录与代理配置；不会自动降级。
新安装直接提供下载端口，无需 Nginx；脚本不安装或配置 cloudflared。

## 3. 配置同机 Cloudflare Tunnel

将后台保存的下载域名接入这台 PDF 服务器上的 Tunnel：

| 配置项 | 填写内容 |
| --- | --- |
| 公开域名 | 与后台下载域名一致，例如 `pdf-worker.ppflight.com` |
| Service 类型 | HTTP |
| Service 地址 | `127.0.0.1:9761` |
| 完整 Service URL | `http://127.0.0.1:9761` |

已有 Tunnel 可直接添加该域名路由，无需重复安装 connector。
普通 A 记录开启橙云不能代替 Tunnel 转发到本机回环端口。

两个端口的分工：

- **9760**：本机核心和健康检查，供安装器、运维命令使用。
- **9761**：仅供同机 Tunnel 转发的签名 PDF 下载与预览入口。

两者都只监听 `127.0.0.1`，不需要开放公网端口。v1.0.11 新安装由 Agent 原生提供 9761；
已有旧版 Nginx 代理的升级会保留原方案，不自动替换。

直接打开域名首页、`/healthz` 或没有有效签名的文件链接，**空白 404 是预期结果**。
文件只通过主站签发的短期授权链接访问；不依赖 Referer 判断，也不向子域名传递登录会话。
客户浏览器需要能打开这些签名链接，请勿再对整个下载域名设置 Access 登录门禁。

## 4. 启用并检查

回到后台“账单文件设置”，确认 Agent 在线、版本为 `1.0.11`，再启用 PDF Agent 交付并保存。
页面下方可按账单号、客户邮箱或生成状态筛选文件；就绪文件可预览和下载。
“在线”表示连接正常，“交付已启用”表示允许处理账单，两者分别显示。

文件异步生成，每次处理一个任务。负载过高、可用内存不足 2 GiB 或磁盘剩余不足 1 GiB 时会等待，
支付流程不会等待 PDF 渲染。主站本地渲染兜底由管理员独立控制。

日常输入 `ag-pdf` 查看总览；`ag-pdf 检查` 核对服务与主站认证，`ag-pdf 日志 -n 100` 查看日志。

**安装时出现一次 `curl: (7) ... port 9760` 怎么办？**

v1.0.11 在服务刚启动或重启时立即检查健康，尚未监听的早期尝试会显示该提示，然后自动重试。
若最终显示 `binding accepted and service is healthy`，说明本机健康与主站认证检查已通过，
不需要开放 9760 或重新安装。若最终检查失败或服务持续离线，请用 `ag-pdf 检查` 和日志定位；
一次历史成功不代替当前状态检查。

## 支持环境和目录

已验证 x86_64：Debian 12/13；Ubuntu 22.04/24.04/26.04 LTS；CentOS Stream、Rocky Linux、AlmaLinux 9/10。
需要 systemd、curl、Python 3.9+、PHP 8.2+ 及 mbstring/xml/gd；Ubuntu 22.04 可使用发行版维护的 PHP 8.1。
安装器只使用支持的发行版软件源，不替换现有不兼容的自定义 PHP，也不修改 aaPanel 的 PHP/FPM 配置。

| 用途 | 一键新安装路径 |
| --- | --- |
| 当前程序 | `/opt/ppflight-pdf-agent/current` |
| 版本目录 | `/opt/ppflight-pdf-agent/releases/<version>` |
| 配置 | `/etc/ppflight-pdf-agent/config.json` |
| 绑定与任务状态 | `/var/lib/ppflight-pdf-agent` |
| PDF 文件 | `/var/lib/ppflight-pdf-agent/artifacts` |

旧部署保留原文件路径，使用 `ag-pdf 路径` 查看。PDF 目录不能设成静态网站目录。

APT 默认仅 IPv4、网络等待 20 秒、失败重试 2 次；只补缺失基础包。
已有可信 CA 时，Ubuntu 官方 HTTP 源会通过临时副本使用同镜像 HTTPS，原系统源不变。
仅 IPv6 主机可设置 `PPFLIGHT_APT_FORCE_IPV4=false`，详见维护文档。

## 维护文档

- [高级安装、旧版代理、升级回滚和故障排查](docs/manual-installation.md)
- [运行约束与安全边界](docs/operations.md)
- [API 与文件授权协议](docs/protocol.md)
- [2026-08-29 历史交接记录（不代表当前状态）](docs/history-2026-08-29.md)
- [v1.0.11 发行包](https://github.com/ppflight/ppflight-pdf-agent/releases/tag/v1.0.11)
