# Operations and security boundaries

## Supported operating systems

The installer uses an explicit allow-list: Debian 12/13; Ubuntu 22.04, 24.04
and 26.04 LTS; CentOS Stream 9/10; Rocky Linux 9/10; and AlmaLinux 9/10.
Debian/Ubuntu dependencies come only from the configured APT repositories.
EL-family dependencies come only from configured DNF repositories; a clean EL9
host selects the official PHP 8.2 AppStream. The installer never resets or
switches an existing PHP stream and never adds EPEL, Remi, a PPA, or another
third-party repository.

The runtime requires Python 3.9+ and PHP 8.2+ with `mbstring`, `xml`, and `gd`.
Ubuntu 22.04 is the sole exception and may use its distribution-maintained PHP
8.1 runtime. Composer metadata remains compatible with PHP 8.1 so that this
explicit exception can install the same locked renderer.
Immutable GitHub Release archives include the renderer dependencies produced
from `renderer/composer.lock`, so Composer is not installed or executed on the
target. A source-checkout installation may use Composer 2 explicitly; that is a
development/maintenance path rather than the production deployment default.
The release CI uses digest-pinned x86_64 container images and runs package,
real-render, and Nginx-configuration smoke checks for every listed OS major
version. It also verifies the unit file with each distribution's
`systemd-analyze`; containers do not claim to exercise systemd as PID 1, and
this release does not claim a tested non-x86_64 architecture.

If a PHP resolved from the Agent's fixed command path is custom and does not
meet the complete runtime contract, dependency installation fails closed
instead of changing PHP alternatives, FPM services, or repository modules. If
aaPanel keeps PHP only under its private panel path and no system CLI PHP is
present, installing the distribution CLI PHP is isolated from the panel's
runtime and does not modify its sites or PHP/FPM configuration.

## Filesystem contract

| Purpose | Location | Owner/mode | Retention |
| --- | --- | --- | --- |
| Immutable releases | `/opt/ppflight-pdf-agent/releases/<version>` | root | retained across ordinary uninstall |
| Active release | `/opt/ppflight-pdf-agent/current` | atomic symlink | switched by install/update/rollback |
| Configuration | `/etc/ppflight-pdf-agent/config.json` | root:`ppflight-pdf` / `0640` | never overwritten |
| Bind/runtime state | `/var/lib/ppflight-pdf-agent` | `ppflight-pdf` / `0750` | never overwritten |
| Generated PDFs | source `artifacts/` or explicit directory | `ppflight-pdf` / `0750` | operator retention policy |

The generated PDF location defaults to the source checkout's `artifacts` folder
when `install.sh` begins. It is not relative to `/opt` and is written to the
installed unit as an absolute path. If that checkout is ephemeral, select a
durable mount with `--artifact-dir` during first installation.

On a later reinstall, the installer deliberately retains the previously
recorded artifact directory; it will reject an accidental `--artifact-dir`
change rather than create a unit/config mismatch or move PDFs. Treat a path
migration as a deliberate configuration/state migration.

## systemd hardening

The unit uses `User=ppflight-pdf`, empty capability sets (therefore no
`CAP_NET_RAW`), `NoNewPrivileges=yes`, `ProtectSystem=strict`, private devices
and `/tmp`, and read-write access only to state and artifacts. It also runs at
lower scheduling weight (`Nice=10`, low CPU/IO weights), `CPUQuota=150%`,
`MemoryHigh=1536M`, `MemoryMax=2G`, `MemorySwapMax=512M`, and `TasksMax=128`.
The core uses a single sequential polling/rendering loop; its local-only server
host and fixed port are `127.0.0.1` and `9760`, with the latter validated at service start. Do not
add a public bind address, a broader `ReadWritePaths`, or a capability simply to
work around a renderer problem; fix or isolate the renderer instead.

`poll_interval_seconds` may be configured only between 2 and 30 seconds. The
core defaults to a 2 GiB available-memory gate and a 10 MiB generated-PDF cap;
it also requires 1 GiB free on the artifact filesystem before claiming work.
These limits are intentionally not loosened by the packaging template.

## Upgrades

Use a release tarball with one top-level directory. `update.sh` requires a
literal release version and SHA-256. For a signed channel, supply both
`--signature-url` and `--gpg-keyring`; the script verifies with `gpgv` before
extracting. Before root extraction, the updater rejects traversal, links,
special files, duplicate paths and oversized archives. The commands take an
exclusive `flock` and retain releases so an
operator can roll back. A local `/healthz` failure automatically returns
`current` to the preceding release.

Never rewrite `config.json` or `state.json` to troubleshoot an update. Use
`ag-pdf 日志`, inspect the new release, and either repair forward or
`sudo ./rollback.sh VERSION`.

## Network publication

The agent is a loopback service, not an Internet origin. Two publication modes
are supported:

- Direct DNS: point `pdf-worker.ppflight.com` at the VPS and use the supplied
  public Nginx TLS virtual host. Nginx listens on 443 and proxies only the signed
  download route to `127.0.0.1:9760`.
- Cloudflare Tunnel: publish the supplied loopback Nginx filter on
  `127.0.0.1:9761`, which proxies the same route to `127.0.0.1:9760`.

Tunnel is optional. In Tunnel mode `cloudflared` initiates the connection, so
the Agent VPS needs **no Internet-facing inbound port** for this service.
`127.0.0.1:9760` and `127.0.0.1:9761` are loopback-only and must never be added
to UFW, a cloud security group, or a router port-forward. The hostname's Tunnel
origin is always `http://127.0.0.1:9761`, never the Agent listener.

Tunnel mode requires outbound UDP 7844 preferentially (QUIC) and outbound TCP
7844 for HTTP/2 fallback, plus the server's existing DNS resolution path
(UDP/TCP 53). TCP 443 is not a Tunnel transport fallback: use it for the
Agent's HTTPS connection to `www.ppflight.com`, for GitHub/release downloads
when installing or updating, and optionally for cloudflared management/update
endpoints. Allow Tunnel endpoint DNS names rather than pinning Cloudflare edge
IPs. An SNI-enforcing egress firewall must also allow
`_v2-origintunneld._tcp.argotunnel.com`, `cftunnel.com`, `h2.cftunnel.com`, and
`quic.cftunnel.com` on port 7844. Follow Cloudflare's current [Tunnel firewall requirements](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)
when creating egress rules; see the Chinese deployment and before/after checks
in [README.md](../README.md).

Dashboard-managed token Tunnels and locally-managed credential-file Tunnels
are separate deployment paths. Reuse an existing Dashboard connector by adding
the download public hostname, or register a new connector with the Dashboard's
token command. Only locally-managed deployments use the repository's
`packaging/cloudflared/config.yml.example` and a generated `<UUID>.json`.

In direct-DNS mode, keep 9760/9761 closed and expose only 443; a
Cloudflare-proxied DNS record may still provide WAF and rate limits. Do not put
customer downloads behind a global Cloudflare Access or IP allow-list: their
short-lived signed download grants are the authorization boundary. Agent
control is always outbound HTTPS polling. No script runs `ufw`, `iptables`, or
any cloud firewall API.

Never configure a static `root` or `alias` pointing at releases, config, state,
or artifacts. The only valid public PDF path is an ADMIN-controlled, signed,
short-lived download grant as defined in [protocol.md](protocol.md).
