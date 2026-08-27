# Operations and security boundaries

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

Tunnel is optional. In direct-DNS mode, keep 9760/9761 closed and expose only
443; a Cloudflare-proxied DNS record may still provide WAF and rate limits.
Do not place customer downloads behind a global Cloudflare Access/IP allow-list.
Agent control is always outbound HTTPS polling. No script runs `ufw`,
`iptables`, or any cloud firewall API.

Never configure a static `root` or `alias` pointing at releases, config, state,
or artifacts. The only valid public PDF path is an ADMIN-controlled, signed,
short-lived download grant as defined in [protocol.md](protocol.md).
