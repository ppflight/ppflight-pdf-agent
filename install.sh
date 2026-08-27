#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SOURCE_DIR}/scripts/lib.sh"

VERSION=""
ARTIFACT_DIR=""
INSTALL_DEPS=0
START_SERVICE=1

usage() {
  cat <<'EOF'
Usage: sudo ./install.sh [--version VERSION] [--artifact-dir ABSOLUTE_OR_RELATIVE_DIR]
                         [--install-deps] [--no-start]

The default artifact directory is ./artifacts beside the source directory from
which this installer is executed. It is created, absolutized, and made private
to the ppflight-pdf service account.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION=${2:?missing version}; shift 2 ;;
    --artifact-dir) ARTIFACT_DIR=${2:?missing artifact directory}; shift 2 ;;
    --install-deps) INSTALL_DEPS=1; shift ;;
    --no-start) START_SERVICE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

require_root
require_linux_distribution
take_lock

if [[ ${INSTALL_DEPS} -eq 1 ]]; then
  install_dependencies
fi
python_and_php_ok || die "Python >=3.9, PHP >=8.2 with mbstring/xml/gd, and Composer are required (use --install-deps where available)"

SOURCE_VERSION="$(python3 "${SOURCE_DIR}/agent.py" version)" || die "cannot read source version"
[[ "${SOURCE_VERSION}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]] || die "source version is invalid"
if [[ -z "${VERSION}" ]]; then
  VERSION="${SOURCE_VERSION}"
fi
[[ "${VERSION}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]] || die "invalid release version"
[[ "${VERSION}" == "${SOURCE_VERSION}" ]] || \
  die "requested version ${VERSION} does not match source version ${SOURCE_VERSION}"

EXISTING_ARTIFACT_DIR="$(artifact_dir_from_install_env || true)"
if [[ -z "${ARTIFACT_DIR}" ]]; then
  # New installs are source-relative; subsequent installs retain the protected
  # original destination rather than unexpectedly moving generated PDFs.
  ARTIFACT_DIR="${EXISTING_ARTIFACT_DIR:-${SOURCE_DIR}/artifacts}"
elif [[ -n "${EXISTING_ARTIFACT_DIR}" && "${ARTIFACT_DIR}" != "${EXISTING_ARTIFACT_DIR}" ]]; then
  die "artifact directory is already configured as ${EXISTING_ARTIFACT_DIR}; preserve it or migrate config/state deliberately"
fi

ARTIFACT_DIR="$(absolute_dir "${ARTIFACT_DIR}")"
ARTIFACT_DIR_TEMPLATE="$(sed_replacement "${ARTIFACT_DIR}")"
mkdir -p -- "${RELEASES_DIR}" "${CONFIG_DIR}"
ensure_service_account
ensure_runtime_dirs "${ARTIFACT_DIR}"

PREVIOUS_RELEASE="$(current_release || true)"
RELEASE_DIR="${RELEASES_DIR}/${VERSION}"
[[ ! -e "${RELEASE_DIR}" ]] || die "release already exists: ${VERSION}; use rollback or choose a new immutable version"
UNIT_EXISTED=0
PAG_EXISTED=0
AG_PDF_EXISTED=0
SERVICE_WAS_ACTIVE=0
if [[ -e /usr/local/bin/ag-pdf ]] && ! grep -Fqx '# PPFLIGHT_PDF_AGENT_AG_PDF_WRAPPER=1' /usr/local/bin/ag-pdf; then
  die "refusing to overwrite an unrelated /usr/local/bin/ag-pdf"
fi
if [[ -e /usr/local/bin/pag ]] && ! grep -Fqx '# PPFLIGHT_PDF_AGENT_PAG_WRAPPER=1' /usr/local/bin/pag; then
  die "refusing to overwrite an unrelated /usr/local/bin/pag"
fi
UNIT_BACKUP="$(mktemp /var/tmp/${APP_NAME}-unit.XXXXXX)"
PAG_BACKUP="$(mktemp /var/tmp/${APP_NAME}-pag.XXXXXX)"
AG_PDF_BACKUP="$(mktemp /var/tmp/${APP_NAME}-ag-pdf.XXXXXX)"
if [[ -f "${UNIT_FILE}" ]]; then
  UNIT_EXISTED=1
  cp -- "${UNIT_FILE}" "${UNIT_BACKUP}"
fi
if [[ -f /usr/local/bin/pag ]]; then
  PAG_EXISTED=1
  cp -- /usr/local/bin/pag "${PAG_BACKUP}"
fi
if [[ -f /usr/local/bin/ag-pdf ]]; then
  AG_PDF_EXISTED=1
  cp -- /usr/local/bin/ag-pdf "${AG_PDF_BACKUP}"
fi
if systemctl is-active --quiet "${SERVICE_NAME}"; then
  SERVICE_WAS_ACTIVE=1
fi

rollback_install() {
  local exit_code=$?
  note "installation failed; restoring the previous current release"
  if [[ ${UNIT_EXISTED} -eq 1 ]]; then
    install -m 0644 "${UNIT_BACKUP}" "${UNIT_FILE}" || true
  else
    rm -f -- "${UNIT_FILE}"
    systemctl disable "${SERVICE_NAME}" >/dev/null 2>&1 || true
  fi
  if [[ ${PAG_EXISTED} -eq 1 ]]; then
    install -m 0755 "${PAG_BACKUP}" /usr/local/bin/pag || true
  else
    rm -f -- /usr/local/bin/pag
  fi
  if [[ ${AG_PDF_EXISTED} -eq 1 ]]; then
    install -m 0755 "${AG_PDF_BACKUP}" /usr/local/bin/ag-pdf || true
  else
    rm -f -- /usr/local/bin/ag-pdf
  fi
  if [[ -n "${PREVIOUS_RELEASE}" && -d "${PREVIOUS_RELEASE}" ]]; then
    atomic_symlink "${PREVIOUS_RELEASE}" "${CURRENT_LINK}" || true
  else
    rm -f -- "${CURRENT_LINK}"
  fi
  systemctl daemon-reload >/dev/null 2>&1 || true
  if [[ ${SERVICE_WAS_ACTIVE} -eq 1 && -n "${PREVIOUS_RELEASE}" ]]; then
    systemctl restart "${SERVICE_NAME}" >/dev/null 2>&1 || true
  fi
  rm -rf -- "${RELEASE_DIR}"
  rm -f -- "${UNIT_BACKUP}" "${PAG_BACKUP}" "${AG_PDF_BACKUP}"
  exit "${exit_code}"
}
trap rollback_install ERR

mkdir -p -- "${RELEASE_DIR}"
RELEASE_PATHS=(
  README.md agent.py ag-pdf pag bind.sh install.sh update.sh rollback.sh uninstall.sh
  pdf_agent renderer packaging scripts docs
)
tar -C "${SOURCE_DIR}" \
  --exclude=renderer/vendor --exclude='__pycache__' --exclude='*.pyc' \
  -cf - -- "${RELEASE_PATHS[@]}" | tar -C "${RELEASE_DIR}" -xf -

# The agent is stdlib-only. Do not bootstrap or upgrade pip from the network.
python3 -m venv --without-pip "${RELEASE_DIR}/.venv"
if [[ -f "${RELEASE_DIR}/renderer/composer.json" ]]; then
  [[ -f "${RELEASE_DIR}/renderer/composer.lock" ]] || die "renderer/composer.lock is required for reproducible installation"
  (cd "${RELEASE_DIR}/renderer" && \
    COMPOSER_ALLOW_SUPERUSER=1 composer validate --strict --no-check-publish && \
    COMPOSER_ALLOW_SUPERUSER=1 composer install --no-dev --prefer-dist --no-interaction --no-progress \
      --no-plugins --no-scripts --classmap-authoritative)
fi

install -m 0644 "${RELEASE_DIR}/packaging/systemd/${SERVICE_NAME}" "${UNIT_FILE}"
sed -i "s|@ARTIFACT_DIR@|${ARTIFACT_DIR_TEMPLATE}|g" "${UNIT_FILE}"
if [[ ! -e "${CONFIG_FILE}" ]]; then
  CONFIG_TEMP="${CONFIG_FILE}.new.$$"
  sed "s|@ARTIFACT_DIR@|${ARTIFACT_DIR_TEMPLATE}|g" \
    "${RELEASE_DIR}/packaging/config.example.json" >"${CONFIG_TEMP}"
  chown root:"${SERVICE_USER}" "${CONFIG_TEMP}"
  chmod 0640 "${CONFIG_TEMP}"
  mv -f -- "${CONFIG_TEMP}" "${CONFIG_FILE}"
  note "created ${CONFIG_FILE}; configure it before binding"
fi
write_install_env_if_missing "${ARTIFACT_DIR}"

atomic_symlink "${RELEASE_DIR}" "${CURRENT_LINK}"
install -m 0755 "${RELEASE_DIR}/ag-pdf" /usr/local/bin/ag-pdf
install -m 0755 "${RELEASE_DIR}/pag" /usr/local/bin/pag
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
if [[ ${START_SERVICE} -eq 1 ]]; then
  systemctl restart "${SERVICE_NAME}"
  post_start_check || die "health/ADMIN check failed (unbound installs perform local health preflight only)"
fi
trap - ERR
rm -f -- "${UNIT_BACKUP}" "${PAG_BACKUP}" "${AG_PDF_BACKUP}"
note "installed ${VERSION}; current -> ${RELEASE_DIR}"
