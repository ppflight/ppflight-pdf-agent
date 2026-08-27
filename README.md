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

- Debian 12 或受支持的 Ubuntu；
- Python 3.9 或更高版本；
- PHP 8.2 或更高版本，启用 `mbstring`、`xml`、`gd`；
- Composer、curl、systemd；
- 公网下载使用 Nginx HTTPS，Cloudflare Tunnel 只是可选方案。

## 从 GitHub 自助安装

仓库：`ppflight/ppflight-pdf-agent`

### 方法一：克隆指定版本（推荐）

如果使用 SSH 克隆私有仓库，请先在服务器配置 GitHub SSH Key，然后：

```bash
git clone --branch v1.0.0 --depth 1 git@github.com:ppflight/ppflight-pdf-agent.git
cd ppflight-pdf-agent
sudo ./install.sh --version 1.0.0 --install-deps \
  --artifact-dir /srv/ppflight-pdf-artifacts
```

如果使用 GitHub CLI，请先执行 `gh auth login`，然后：

```bash
gh repo clone ppflight/ppflight-pdf-agent
cd ppflight-pdf-agent
git checkout v1.0.0
sudo ./install.sh --version 1.0.0 --install-deps \
  --artifact-dir /srv/ppflight-pdf-artifacts
```

安装器会：

1. 创建无登录权限的 `ppflight-pdf` 系统账户；
2. 安装不可变版本到 `/opt/ppflight-pdf-agent/releases/1.0.0`；
3. 创建 `/etc/ppflight-pdf-agent/config.json`；
4. 创建并启动 `ppflight-pdf-agent.service`；
5. 安装中文运维命令 `/usr/local/bin/ag-pdf`。

首次安装建议明确使用持久目录 `/srv/ppflight-pdf-artifacts`。如果源码本身位于允许
Agent 写入的持久磁盘，也可以省略参数并使用源码目录下的 `artifacts/`；位于
`/root`、`/home` 或临时目录的源码不能作为 PDF 存储位置。

如需使用其他独立磁盘，只能在首次安装时指定：

```bash
sudo ./install.sh --version 1.0.0 --install-deps --artifact-dir /srv/ppflight-pdf-artifacts
```

### 方法二：安装 GitHub Release

Release 同时提供压缩包和 SHA-256 文件。不要跳过校验：

```bash
work_dir="$(mktemp -d)"
gh release download v1.0.0 \
  --repo ppflight/ppflight-pdf-agent \
  --dir "$work_dir" \
  --pattern 'ppflight-pdf-agent-1.0.0.tar.gz*'
cd "$work_dir"
sha256sum -c ppflight-pdf-agent-1.0.0.tar.gz.sha256
tar -xzf ppflight-pdf-agent-1.0.0.tar.gz
cd ppflight-pdf-agent-1.0.0
sudo ./install.sh --version 1.0.0 --install-deps \
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

## 公网下载：直接 DNS + Nginx HTTPS

Cloudflare Tunnel 不是必需项。直接部署方式：

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
  /etc/nginx/sites-available/ppflight-pdf-agent.conf
sudo ln -s /etc/nginx/sites-available/ppflight-pdf-agent.conf \
  /etc/nginx/sites-enabled/ppflight-pdf-agent.conf
sudo nginx -t
sudo systemctl reload nginx
```

只允许公网访问 TCP 443。`9760` 和 `9761` 必须保持仅本机可用。Nginx 示例只转发
`/v1/download/...`，不会公开 `/healthz`、配置、状态文件或 PDF 文件夹，也不会记录
带签名参数的下载 URL。

如果使用 Cloudflare Tunnel，使用
`packaging/nginx/pdf-agent-local.conf.example` 和
`packaging/cloudflared/config.yml.example`，Tunnel 目标为 `127.0.0.1:9761`。

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

## 升级与回滚

每个版本必须使用新的不可变版本号。升级包必须校验 SHA-256；生产环境建议同时启用
仓库提供的签名校验接口。

```bash
sudo ./update.sh --version 1.0.1 \
  --url https://可信下载地址/ppflight-pdf-agent-1.0.1.tar.gz \
  --sha256 64位SHA256值
```

升级安装、启动或健康检查失败时会自动恢复上一个版本。手动回滚：

```bash
sudo ./rollback.sh 1.0.0
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
./scripts/verify-release.sh --source . --version 1.0.0
```

GitHub Actions 会执行 Python 3.9/3.12、PHP 8.2、Composer、ShellCheck、真实 PDF
渲染和发布布局检查。协议和安全边界详见 [docs/protocol.md](docs/protocol.md) 与
[docs/operations.md](docs/operations.md)。
