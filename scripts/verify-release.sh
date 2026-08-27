#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR=""
VERSION=""

usage() {
  echo "Usage: $0 --source DIRECTORY [--version VERSION]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE_DIR=${2:?missing source directory}; shift 2 ;;
    --version) VERSION=${2:?missing version}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

[[ -n "${SOURCE_DIR}" ]] || { usage; exit 2; }
SOURCE_DIR="$(cd -P -- "${SOURCE_DIR}" && pwd)"
for file in install.sh update.sh rollback.sh uninstall.sh bind.sh ag-pdf pag \
  scripts/status-report.py scripts/verify-nginx-config.sh scripts/build-release.sh \
  scripts/ci-platform-smoke.sh tests/test-platform-support.sh \
  scripts/lib.sh scripts/verify-release-archive.py scripts/verify-runtime-config.py \
  packaging/systemd/ppflight-pdf-agent.service \
  packaging/config.example.json packaging/nginx/pdf-agent-local.conf.example \
  packaging/nginx/pdf-agent-public-tls.conf.example packaging/cloudflared/config.yml.example \
  README.md docs/protocol.md docs/operations.md; do
  [[ -f "${SOURCE_DIR}/${file}" ]] || { echo "missing release file: ${file}" >&2; exit 1; }
done
if [[ -f "${SOURCE_DIR}/renderer/composer.json" ]]; then
  [[ -f "${SOURCE_DIR}/renderer/composer.lock" ]] || {
    echo "renderer/composer.lock is required for reproducible Composer installs" >&2; exit 1;
  }
fi
grep -Fq '"php": ">=8.1"' "${SOURCE_DIR}/renderer/composer.json"
"${SOURCE_DIR}/tests/test-platform-support.sh" >/dev/null

if [[ -n "${VERSION}" ]]; then
  [[ "${VERSION}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]] || {
    echo "invalid immutable release version" >&2; exit 1;
  }
  SOURCE_VERSION="$(python3 "${SOURCE_DIR}/agent.py" version)"
  [[ "${SOURCE_VERSION}" == "${VERSION}" ]] || {
    echo "source version ${SOURCE_VERSION} does not match release version ${VERSION}" >&2; exit 1;
  }
fi
grep -Fqx 'User=ppflight-pdf' "${SOURCE_DIR}/packaging/systemd/ppflight-pdf-agent.service"
grep -Fqx 'ProtectSystem=strict' "${SOURCE_DIR}/packaging/systemd/ppflight-pdf-agent.service"
grep -Fq 'CapabilityBoundingSet=' "${SOURCE_DIR}/packaging/systemd/ppflight-pdf-agent.service"
grep -Fq 'verify-runtime-config.py' "${SOURCE_DIR}/packaging/systemd/ppflight-pdf-agent.service"
grep -Fq 'agent.py --config /etc/ppflight-pdf-agent/config.json run' "${SOURCE_DIR}/packaging/systemd/ppflight-pdf-agent.service"
grep -Fq '@ARTIFACT_DIR@' "${SOURCE_DIR}/packaging/systemd/ppflight-pdf-agent.service"
grep -Fqx 'ReadWritePaths=/var/lib/ppflight-pdf-agent @ARTIFACT_DIR@' "${SOURCE_DIR}/packaging/systemd/ppflight-pdf-agent.service"
if grep -Fqx 'ProcSubset=pid' "${SOURCE_DIR}/packaging/systemd/ppflight-pdf-agent.service"; then
  echo "unsupported ProcSubset=pid hardening is present" >&2; exit 1
fi
grep -Fqx 'CPUQuota=150%' "${SOURCE_DIR}/packaging/systemd/ppflight-pdf-agent.service"
grep -Fqx 'MemoryHigh=1536M' "${SOURCE_DIR}/packaging/systemd/ppflight-pdf-agent.service"
grep -Fqx 'MemoryMax=2G' "${SOURCE_DIR}/packaging/systemd/ppflight-pdf-agent.service"
grep -Fqx 'MemorySwapMax=512M' "${SOURCE_DIR}/packaging/systemd/ppflight-pdf-agent.service"
if grep -Fq -- '--locked' "${SOURCE_DIR}/install.sh"; then
  echo "unsupported Composer --locked option is present in installer" >&2; exit 1
fi
if grep -Fq -- '--exclude=renderer/vendor' "${SOURCE_DIR}/install.sh"; then
  echo "installer must preserve bundled renderer dependencies" >&2; exit 1
fi
grep -Fq 'Never trust a working-tree vendor directory' "${SOURCE_DIR}/scripts/build-release.sh"
grep -Fq 'composer install' "${SOURCE_DIR}/scripts/build-release.sh"
grep -Fq 'archive --format=tar HEAD' "${SOURCE_DIR}/scripts/build-release.sh"
if [[ -f "${SOURCE_DIR}/renderer/vendor/autoload.php" ]]; then
  # $argv is evaluated by PHP, not Bash.
  # shellcheck disable=SC2016
  php -r 'require $argv[1]; exit(class_exists("Dompdf\\Dompdf") ? 0 : 1);' \
    "${SOURCE_DIR}/renderer/vendor/autoload.php"
fi
grep -Fqx '# PPFLIGHT_PDF_AGENT_AG_PDF_WRAPPER=1' "${SOURCE_DIR}/ag-pdf"
grep -Fq 'ag-pdf 状态' "${SOURCE_DIR}/README.md"
for nginx_config in \
  "${SOURCE_DIR}/packaging/nginx/pdf-agent-local.conf.example" \
  "${SOURCE_DIR}/packaging/nginx/pdf-agent-public-tls.conf.example"; do
  grep -Fq 'access_log off;' "${nginx_config}"
  grep -Fq 'error_log /dev/null crit;' "${nginx_config}"
  if grep -Eq '^[[:space:]]*(root|alias)[[:space:]]' "${nginx_config}"; then
    echo "Nginx example must not expose a filesystem root or alias" >&2; exit 1
  fi
  grep -Fq '/v1/download/' "${nginx_config}"
done
grep -Fq 'listen 443 ssl http2;' "${SOURCE_DIR}/packaging/nginx/pdf-agent-public-tls.conf.example"
grep -Fq 'limit_req zone=ppflight_pdf_rate' "${SOURCE_DIR}/packaging/nginx/pdf-agent-public-tls.conf.example"
grep -Fq 'service: http://127.0.0.1:9761' "${SOURCE_DIR}/packaging/cloudflared/config.yml.example"
grep -Fq 'http_status:404' "${SOURCE_DIR}/packaging/cloudflared/config.yml.example"
if grep -Eq '^[[:space:]]*service:[[:space:]]*http://127\.0\.0\.1:9760([[:space:]]|$)' \
  "${SOURCE_DIR}/packaging/cloudflared/config.yml.example"; then
  echo "Cloudflare Tunnel example must not point at the Agent listener" >&2; exit 1
fi
grep -Fq 'limit_except GET HEAD' "${SOURCE_DIR}/packaging/nginx/pdf-agent-local.conf.example"
grep -Fq 'no Internet-facing inbound port' "${SOURCE_DIR}/docs/operations.md"
grep -Fq 'Dashboard-managed token Tunnels and locally-managed credential-file Tunnels' "${SOURCE_DIR}/docs/operations.md"
grep -Fq 'quic.cftunnel.com' "${SOURCE_DIR}/README.md"
echo "release layout and mandatory local-only hardening checks passed"
