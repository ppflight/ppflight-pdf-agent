#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SOURCE_DIR}/scripts/lib.sh"

PURGE=0
case "${1:-}" in
  '') ;;
  --purge) PURGE=1 ;;
  -h|--help) echo "Usage: sudo ./uninstall.sh [--purge]"; exit 0 ;;
  *) echo "Usage: sudo ./uninstall.sh [--purge]" >&2; exit 2 ;;
esac
require_root
take_lock

systemctl disable --now "${SERVICE_NAME}" >/dev/null 2>&1 || true
rm -f -- "${UNIT_FILE}"
systemctl daemon-reload
if [[ -f /usr/local/bin/pag ]] && grep -Fqx '# PPFLIGHT_PDF_AGENT_PAG_WRAPPER=1' /usr/local/bin/pag; then
  rm -f -- /usr/local/bin/pag
fi
if [[ -f /usr/local/bin/ag-pdf ]] && grep -Fqx '# PPFLIGHT_PDF_AGENT_AG_PDF_WRAPPER=1' /usr/local/bin/ag-pdf; then
  rm -f -- /usr/local/bin/ag-pdf
fi
rm -f -- "${CURRENT_LINK}"

if [[ ${PURGE} -eq 1 ]]; then
  [[ "${APP_ROOT}" == "/opt/${APP_NAME}" ]] || die "unexpected application root"
  rm -rf -- "${APP_ROOT}" "${CONFIG_DIR}" "${STATE_DIR}"
  userdel "${SERVICE_USER}" >/dev/null 2>&1 || true
  groupdel "${SERVICE_USER}" >/dev/null 2>&1 || true
  note "agent, releases, configuration, state and service account were removed"
else
  note "service removed; releases, configuration and state were retained"
  note "run again with --purge to permanently remove retained data"
fi
