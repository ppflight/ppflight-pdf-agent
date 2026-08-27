#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SOURCE_DIR}/scripts/lib.sh"

CODE_FILE=""
REPLACE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --code-file) CODE_FILE=${2:?missing binding-code file}; shift 2 ;;
    --replace) REPLACE=1; shift ;;
    -h|--help)
      echo "Usage: sudo ./bind.sh [--replace] [--code-file FILE]"; exit 0 ;;
    *) die "binding codes are accepted only via stdin or --code-file, never command arguments" ;;
  esac
done
require_root
take_lock
[[ -f "${CONFIG_FILE}" ]] || die "missing configuration: ${CONFIG_FILE}"
RELEASE="$(current_release)" || die "agent is not installed"
[[ -x "${RELEASE}/.venv/bin/python" && -f "${RELEASE}/agent.py" ]] || die "current release is incomplete"

if [[ -n "${CODE_FILE}" ]]; then
  [[ -f "${CODE_FILE}" ]] || die "binding-code file not found"
  [[ ! -L "${CODE_FILE}" ]] || die "binding-code file must not be a symlink"
  CODE_MODE=$(stat -c '%a' "${CODE_FILE}")
  [[ $(stat -c '%u' "${CODE_FILE}") -eq 0 ]] || die "binding-code file must be root-owned"
  (( (8#${CODE_MODE} & 8#0077) == 0 )) || die "binding-code file must not be group/world-readable"
  BINDING_CODE="$(<"${CODE_FILE}")"
else
  [[ ! -t 0 ]] || note "paste the one-time binding code, then press Enter"
  IFS= read -r -s BINDING_CODE || true
  [[ ! -t 0 ]] || echo
fi
[[ -n "${BINDING_CODE}" ]] || die "binding code must not be empty"

ensure_service_account
ensure_runtime_dirs "$(artifact_dir_from_install_env || die "missing installed artifact directory")"
# The core bind interface consumes the code on stdin. Keep it out of argv and
# shell history even when invoked by automation.
BIND_ARGS=(bind --code-stdin)
[[ ${REPLACE} -eq 1 ]] && BIND_ARGS+=(--replace)
printf '%s\n' "${BINDING_CODE}" | runuser -u "${SERVICE_USER}" -- \
  "${RELEASE}/.venv/bin/python" "${RELEASE}/agent.py" --config "${CONFIG_FILE}" "${BIND_ARGS[@]}"
unset BINDING_CODE
chmod 0600 "${STATE_DIR}/state.json" 2>/dev/null || true
chown "${SERVICE_USER}:${SERVICE_USER}" "${STATE_DIR}/state.json" 2>/dev/null || true
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
post_start_check || die "service failed authenticated health check after binding"
note "binding accepted and service is healthy"
