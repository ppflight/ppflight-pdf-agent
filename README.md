# PPFlight PDF Agent

PPFlight PDF Agent 是 PPFlight ADMIN 的独立异步账单 PDF 工作节点。它从
`https://www.ppflight.com/api/pdf-agent/v1` 轮询任务，在服务器负载合适时生成
PDF，并把文件保存在 Agent 所在服务器的私有目录中。

- 生成的文件是真实 PDF，文件名统一以 `PPFlight-` 开头。
- PDF 使用 PPFlight 官网图标，不使用 `PPFlight Cloud` 名称。
- Agent 不连接主站数据库，只通过 PPFlight HTTPS API 通信。
- ADMIN 只允许绑定一个主 Agent。
- 下载必须经过最长 5 分钟的签名链接，不能把 PDF 目录设为静态网站目录。
- Agent 固定监听 `127.0.0.1:9760`，不可直接暴露到公网。

## 支持环境

- Debian 12、Debian 13；
- Ubuntu 22.04 LTS、24.04 LTS、26.04 LTS；
- CentOS Stream 9/10、Rocky Linux 9/10、AlmaLinux 9/10；
- Python 3.9 或更高版本；
- PHP 8.2 或更高版本，启用 `mbstring`、`xml`、`gd`；Ubuntu 22.04 例外，允许
  使用其发行版维护的 PHP 8.1；
- curl、systemd；GitHub Release 已包含按锁文件构建的渲染依赖，目标机不需要 Composer；
- Tunnel 模式还需要 Nginx 和当前受支持的 `cloudflared`。`cloudflared` 请使用
  [Cloudflare 官方软件包](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/)，不要从未知脚本安装；
- 公网下载可使用 Nginx HTTPS；异地 Agent 推荐使用 Cloudflare Tunnel，不需要向
  Internet 开放 Agent 服务器的入站端口。

安装器只接受上表明确列出的发行版和主版本，不会按 `ID_LIKE` 猜测兼容性，也不会把
RHEL、Fedora 或其他衍生版当成已验证平台。EL 9 系列的干净主机会从系统 AppStream
选择 PHP 8.2。如果固定命令路径中已有不完整或过旧的自定义 PHP，安装器会停止，不会
切换模块、替换软链或影响现有网站。aaPanel 仅在自己的私有路径提供 PHP、且系统没有
CLI PHP 时，安装器可另装发行版的 CLI PHP；这不会修改 aaPanel 的 PHP/FPM。当前发布
CI 在 x86_64 上验证全部发行版；未将其他 CPU 架构列为本版本的发布保证。

## 从 GitHub 自助安装

仓库：`ppflight/ppflight-pdf-agent`

### 方法一：克隆指定版本（开发或维护）

源码仓库不提交 `renderer/vendor`。从源码安装需要由管理员预先提供 Composer 2，并且
必须先按锁文件安装渲染依赖；获取方式应使用
[Composer 官方下载说明](https://getcomposer.org/download/)。如果只是部署服务器，
优先使用下方自带依赖、目标机无需 Composer 的 GitHub Release。

如果使用 SSH 克隆私有仓库，请先在服务器配置 GitHub SSH Key，然后：

```bash
git clone --branch v1.0.3 --depth 1 git@github.com:ppflight/ppflight-pdf-agent.git
cd ppflight-pdf-agent
composer install --working-dir=renderer --no-dev --prefer-dist --no-interaction \
  --no-progress --no-plugins --no-scripts --classmap-authoritative
sudo ./install.sh --version 1.0.3 --install-deps \
  --artifact-dir /srv/ppflight-pdf-artifacts
```

如果使用 GitHub CLI，请先执行 `gh auth login`，然后：

```bash
gh repo clone ppflight/ppflight-pdf-agent
cd ppflight-pdf-agent
git checkout v1.0.3
composer install --working-dir=renderer --no-dev --prefer-dist --no-interaction \
  --no-progress --no-plugins --no-scripts --classmap-authoritative
sudo ./install.sh --version 1.0.3 --install-deps \
  --artifact-dir /srv/ppflight-pdf-artifacts
```

安装器会：

1. 创建无登录权限的 `ppflight-pdf` 系统账户；
2. 安装不可变版本到 `/opt/ppflight-pdf-agent/releases/1.0.3`；
3. 创建 `/etc/ppflight-pdf-agent/config.json`；
4. 创建并启动 `ppflight-pdf-agent.service`；
5. 安装中文运维命令 `/usr/local/bin/ag-pdf`。

首次安装建议明确使用持久目录 `/srv/ppflight-pdf-artifacts`。如果源码本身位于允许
Agent 写入的持久磁盘，也可以省略参数并使用源码目录下的 `artifacts/`；位于
`/root`、`/home` 或临时目录的源码不能作为 PDF 存储位置。

如需使用其他独立磁盘，只能在首次安装时指定：

```bash
sudo ./install.sh --version 1.0.3 --install-deps --artifact-dir /srv/ppflight-pdf-artifacts
```

### 方法二：安装 GitHub Release（推荐）

Release 同时提供压缩包和 SHA-256 文件，并已包含锁定的 PDF 渲染依赖。不要跳过校验：

```bash
work_dir="$(mktemp -d)"
gh release download v1.0.3 \
  --repo ppflight/ppflight-pdf-agent \
  --dir "$work_dir" \
  --pattern 'ppflight-pdf-agent-1.0.3.tar.gz*'
cd "$work_dir"
sha256sum -c ppflight-pdf-agent-1.0.3.tar.gz.sha256
tar -xzf ppflight-pdf-agent-1.0.3.tar.gz
cd ppflight-pdf-agent-1.0.3
sudo ./install.sh --version 1.0.3 --install-deps \
  --artifact-dir /srv/ppflight-pdf-artifacts
```

## 绑定 ADMIN

1. 登录 PPFlight ADMIN；
2. 打开“系统设置 → PDF Agent”；
3. 填写销售方公司名、地址、底部邮箱、下载域名等设置；
4. 生成一次性绑定码；
5. 在 Agent 服务器上保存并使用该绑定码：

```bash
sudo install -m 0600 /dev/null /root/ppflight-pdf-bind-code
sudoedit /root/ppflight-pdf-bind-code
sudo ag-pdf 绑定 --code-file /root/ppflight-pdf-bind-code
sudo rm -f /root/ppflight-pdf-bind-code
ag-pdf 状态
```

绑定码不会作为命令参数进入 Shell 历史；成功后 ADMIN 与本地均只保留所需的安全凭据。

## `ag-pdf` 中文运维菜单

直接输入以下命令即可查看总览：

```bash
ag-pdf
```

总览包含：服务状态、本地健康状态、当前版本、绑定状态、Agent UUID、ADMIN
连接、生成负载条件、可用/撤销账单数、PDF 文件数量与空间、成功/失败/待回传任务数。

```text
ag-pdf 状态               显示完整状态（默认）
ag-pdf 检查               严格检查本地服务和 ADMIN 连接
ag-pdf 统计               显示绑定、任务、PDF、磁盘统计
ag-pdf 日志 -n 100        查看最近 100 条服务日志
ag-pdf 绑定               绑定一次性 ADMIN 代码
ag-pdf 服务 启动          启动服务
ag-pdf 服务 停止          停止服务
ag-pdf 服务 重启          重启服务
ag-pdf 版本               显示当前不可变版本
ag-pdf 路径               显示版本、配置、状态、PDF 路径
ag-pdf 帮助               显示中文菜单
```

英文子命令仍兼容自动化脚本：`status`、`check`、`stats`、`logs`、`bind`、
`service`、`version`、`paths`、`help`。旧命令 `pag` 仅作为兼容别名保留。

## 公网下载：Cloudflare Tunnel（异地 Agent 推荐）

Cloudflare Tunnel 由 Agent 服务器上的 `cloudflared` **主动向外**建立连接。因此，
只为本 Agent 服务时，异地 VPS 不需要向公网开放任何入站端口；不要为 Tunnel 添加
UFW、云安全组或路由器的入站规则。`127.0.0.1:9760`（Agent）和
`127.0.0.1:9761`（过滤 Nginx）都是本机回环监听，绝不能加入 UFW 公网允许规则。

```text
客户浏览器
    ↓ https://pdf-worker.ppflight.com/v1/download/...
Cloudflare 边缘
    ↓ 已建立的 Tunnel（cloudflared 主动出站）
127.0.0.1:9761（仅下载路径的 Nginx 过滤器）
    ↓
127.0.0.1:9760（PDF Agent）
```

### 出站网络要求

让防火墙/上游网络保留既有 DNS 解析能力（UDP/TCP 53），并允许以下**出站**流量；
不需要相应的入站放行：

| 目的 | 协议和端口 | 用途 |
| --- | --- | --- |
| Cloudflare Tunnel 端点 | UDP 7844（QUIC，优先） | `cloudflared` Tunnel 连接 |
| Cloudflare Tunnel 端点 | TCP 7844（HTTP/2 回退） | UDP 7844 不可用时保持 Tunnel 可用 |
| `www.ppflight.com` | TCP 443 | Agent 轮询 PPFlight API、回传任务结果 |
| GitHub 及其下载域名 | TCP 443 | 仅在克隆、升级或下载 Release 时需要 |
| 现有 DNS 解析器 | UDP/TCP 53 | 解析上述域名；沿用服务器现有 DNS 配置 |

当前 cloudflared 的 HTTP/2 回退使用 **TCP 7844，不是 TCP 443**；只允许 443 而
封锁 TCP/UDP 7844 会使 Tunnel 不能工作。TCP 443 可按需要允许
`api.cloudflare.com`/`update.argotunnel.com` 的 cloudflared 管理和更新访问（本示例
使用 `--no-autoupdate`），以及上表的 PPFlight/GitHub HTTPS 服务。应按组织的出站策略
允许 Cloudflare Tunnel 端点域名（例如 `region1.v2.argotunnel.com` 和
`region2.v2.argotunnel.com`），而不是写死边缘 IP。若出站防火墙强制检查 SNI，还要在
7844 端口允许 `_v2-origintunneld._tcp.argotunnel.com`、`cftunnel.com`、
`h2.cftunnel.com` 和 `quic.cftunnel.com`。若服务器还承载其他服务，保留其已有规则
即可；本项目不执行、也不要求执行任何 `ufw allow` 入站命令。端点和端口的最新清单以
[Cloudflare Tunnel firewall 文档](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)
为准。

### 部署

1. 安装 Nginx/网络检查工具并确认版本。若 `cloudflared` 尚未安装，请先按上方官方
   软件包文档安装。不要在 aaPanel 服务器上再安装第二套 Nginx：

Debian / Ubuntu：

```bash
sudo apt-get update
sudo apt-get install -y nginx netcat-openbsd
```

CentOS Stream / Rocky Linux / AlmaLinux：

```bash
sudo dnf install -y nginx nmap-ncat
```

```bash
nginx -v
cloudflared --version
```

2. 启用回环 Nginx 过滤器；它没有静态文件目录，只允许签名下载路径：

```bash
sudo cp packaging/nginx/pdf-agent-local.conf.example \
  /etc/nginx/conf.d/ppflight-pdf-agent-local.conf
sudo nginx -t
sudo systemctl reload nginx
```

aaPanel 或其他自带 Nginx 的环境应先运行 `nginx -T` 找到实际生效的 `include` 目录，
再把同一配置放入该目录；常见 aaPanel 路径是
`/www/server/panel/vhost/nginx/ppflight-pdf-agent-local.conf`。仍必须先 `nginx -t`，并只
reload 当前正在运行的 Nginx，不能安装或启动第二套服务。

3. 推荐使用 **Dashboard 管理的 Tunnel**，不要和本地 credentials 模式混用：

   - 如果这台 VPS 已有正常运行的 Dashboard Tunnel connector：在 Cloudflare Zero
     Trust 的 **Networks → Tunnels** 打开该 Tunnel，只新增 Public Hostname
     `pdf-worker.ppflight.com`，Service 选择 `HTTP` 并填写 `127.0.0.1:9761`。不要重复
     安装或覆盖现有 `cloudflared.service`。
   - 如果这台 VPS 尚无 connector：在 Dashboard 创建 Tunnel，按页面给出的当前 Linux
     安装命令注册 connector，再添加上述 Public Hostname。Dashboard 给出的 Tunnel
     token 是机密；不要把它提交到仓库、工单、聊天或长期保留在 Shell 历史中。

   Dashboard 模式由 Cloudflare 保存 ingress，不需要
   `packaging/cloudflared/config.yml.example`，也不会生成本地 `<UUID>.json`。

4. 只有明确选择 **locally-managed Tunnel** 时，才使用仓库内的配置示例。先按
   [Cloudflare locally-managed Tunnel 文档](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/create-local-tunnel/)
   完成 `tunnel login`、`tunnel create` 和 DNS route；这些步骤才会生成本机
   `<UUID>.json`。之后复制示例，填入真实 UUID 和受保护的 credentials 路径：

```bash
sudo install -d -m 0750 /etc/cloudflared
sudo cp packaging/cloudflared/config.yml.example /etc/cloudflared/config.yml
sudoedit /etc/cloudflared/config.yml
sudo cloudflared --config /etc/cloudflared/config.yml tunnel ingress validate
```

   示例中的 Tunnel origin 必须保持为 `http://127.0.0.1:9761`，不能改为 Agent 的
   `9760`。credentials JSON、Tunnel token 和签名下载 URL 都不能提交到仓库、工单或
   聊天中。

5. locally-managed 模式可先前台验证；验证通过后，再按 Cloudflare 官方 Linux service
   文档让该配置常驻。Dashboard 模式直接检查已注册 connector 的服务状态：

```bash
# 仅 locally-managed 模式
sudo cloudflared --no-autoupdate --config /etc/cloudflared/config.yml tunnel run

# Dashboard 模式或既有 connector
sudo systemctl status cloudflared --no-pager
```

### 部署前检查

以下命令只检查本机监听和出站连通性，不会打印凭据。`nc -u` 的成功结果取决于网络
设备是否会回应 UDP 探测，仍应结合实际 Tunnel 注册日志判断。

```bash
sudo ss -lntp '( sport = :9760 or sport = :9761 )'
getent ahosts region1.v2.argotunnel.com
getent ahosts region2.v2.argotunnel.com
nc -zvu region1.v2.argotunnel.com 7844
nc -zv region1.v2.argotunnel.com 7844
nc -zvu region2.v2.argotunnel.com 7844
nc -zv region2.v2.argotunnel.com 7844
curl --connect-timeout 5 -sS -o /dev/null -w 'PPFlight API: %{http_code}\n' \
  https://www.ppflight.com/api/pdf-agent/v1/
curl --connect-timeout 5 -sS -o /dev/null -w 'GitHub: %{http_code}\n' https://github.com/
```

### 部署后检查

```bash
curl -fsS http://127.0.0.1:9760/healthz
curl -sS -o /dev/null -w '9761 /healthz: %{http_code}\n' \
  http://127.0.0.1:9761/healthz
sudo ss -lntp '( sport = :9760 or sport = :9761 )'
sudo systemctl status cloudflared --no-pager
curl -sS -o /dev/null -w '公网 /healthz: %{http_code}\n' \
  https://pdf-worker.ppflight.com/healthz
```

locally-managed 模式再额外执行：

```bash
sudo cloudflared --config /etc/cloudflared/config.yml tunnel ingress validate
sudo cloudflared --config /etc/cloudflared/config.yml tunnel ingress rule \
  'https://pdf-worker.ppflight.com/v1/download/check'
```

本机 `9761 /healthz` 和公网 `/healthz` 都必须返回 `404`，证明健康接口没有穿过 Nginx；
`ss` 的两个监听地址必须都以 `127.0.0.1:` 开头。Tunnel 已连接后，在 ADMIN 生成一条新的签名下载链接，
从异地浏览器下载一次即可验证完整链路。不得在 Tunnel 前对客户下载启用全局
Cloudflare Access 或 IP 白名单：下载链接本身的短时签名已经是客户授权边界；可以继续
使用 WAF 和速率限制。

## 公网下载：直接 DNS + Nginx HTTPS（替代方案）

不使用 Tunnel 时，直接部署方式：

```text
pdf-worker.ppflight.com
        ↓ A/AAAA 或 Cloudflare 代理 DNS
VPS Nginx 公网 443
        ↓
127.0.0.1:9760（PDF Agent）
```

1. 把 `pdf-worker.ppflight.com` 的 DNS 指向 Agent VPS；
2. 申请有效 HTTPS 证书；
3. 复制并检查 Nginx 示例：

```bash
sudo cp packaging/nginx/pdf-agent-public-tls.conf.example \
  /etc/nginx/conf.d/ppflight-pdf-agent.conf
sudo nginx -t
sudo systemctl reload nginx
```

aaPanel 环境同样应使用 `nginx -T` 显示的现有 include 目录，而不是
`/etc/nginx/conf.d`。

只允许公网访问 TCP 443。`9760` 和 `9761` 必须保持仅本机可用。Nginx 示例只转发
`/v1/download/...`，不会公开 `/healthz`、配置、状态文件或 PDF 文件夹，也不会记录
带签名参数的下载 URL。

## 异步生成策略

Agent 每次只处理一个任务。账单创建、支付、取消、退款或税务状态变更时，主站只追加
一个冻结快照任务；Agent 在以下条件满足后领取并生成：

- 1 分钟负载不高于配置阈值；
- 可用内存不少于 2 GiB；
- PDF 磁盘可用空间不少于 1 GiB。

所以支付完成无需等待 PDF 渲染。生成未完成时 APP 显示“正在生成”，完成后才提供短时
签名下载链接。

## 目录与数据

| 用途 | 默认路径 |
| --- | --- |
| 不可变版本 | `/opt/ppflight-pdf-agent/releases/<version>` |
| 当前版本链接 | `/opt/ppflight-pdf-agent/current` |
| 配置 | `/etc/ppflight-pdf-agent/config.json` |
| 绑定和任务状态 | `/var/lib/ppflight-pdf-agent` |
| PDF 文件 | 首次安装的源码目录 `artifacts/` 或 `--artifact-dir` 指定路径 |

配置、绑定状态和 PDF 不会在普通升级时被覆盖。不要手动编辑
`/var/lib/ppflight-pdf-agent/state.json`。

systemd 服务继续启用 `NoNewPrivileges`、空 capability 集、严格只读系统目录、
私有临时目录、设备/内核/命名空间限制及 syscall allowlist。不要额外加入
`MemoryDenyWriteExecute=yes`：受支持发行版的 PHP/Dompdf 固定渲染运行时需要可执行内存映射，
启用该项会令 Python 调用 PHP 的完整渲染链路失败。

## 升级与回滚

每个版本必须使用新的不可变版本号。升级包必须校验 SHA-256；生产环境建议同时启用
仓库提供的签名校验接口。

```bash
sudo ./update.sh --version 1.0.3 \
  --url https://可信下载地址/ppflight-pdf-agent-1.0.3.tar.gz \
  --sha256 64位SHA256值
```

升级安装、启动或健康检查失败时会自动恢复上一个版本。手动回滚：

```bash
sudo ./rollback.sh 1.0.1
```

## 卸载

保留配置、状态、版本和 PDF：

```bash
sudo ./uninstall.sh
```

同时删除程序、配置、状态和服务账户（PDF 文件仍故意保留）：

```bash
sudo ./uninstall.sh --purge
```

## 开发与发布检查

```bash
shellcheck install.sh update.sh rollback.sh uninstall.sh bind.sh ag-pdf pag scripts/*.sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
php tests/renderer_test.php
./tests/test-platform-support.sh
./scripts/verify-release.sh --source . --version 1.0.3
```

GitHub Actions 会执行 Python 3.9/3.12/3.13/3.14、PHP 8.1/8.2/8.4/8.5、Composer、
ShellCheck、真实 PDF 渲染，并在每个发行版容器内安装依赖、解析 systemd 单元和两种
Nginx 发布配置。容器镜像按摘要固定；发布资产只从当前 Git commit 的跟踪文件构建，
并重新按锁文件生成渲染依赖。协议和安全边界详见
[docs/protocol.md](docs/protocol.md) 与 [docs/operations.md](docs/operations.md)。
