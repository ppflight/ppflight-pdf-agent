#!/usr/bin/env bash
# Shared helpers for the PPFlight PDF Agent administrative scripts.
set -Eeuo pipefail

readonly APP_NAME="ppflight-pdf-agent"
readonly SERVICE_NAME="ppflight-pdf-agent.service"
readonly SERVICE_USER="ppflight-pdf"
readonly APP_ROOT="/opt/${APP_NAME}"
# Used by lifecycle scripts that source this library; ShellCheck also analyzes
# this file as a standalone target in CI.
# shellcheck disable=SC2034
readonly RELEASES_DIR="${APP_ROOT}/releases"
readonly CURRENT_LINK="${APP_ROOT}/current"
readonly CONFIG_DIR="/etc/${APP_NAME}"
readonly CONFIG_FILE="${CONFIG_DIR}/config.json"
readonly INSTALL_ENV="${CONFIG_DIR}/install.env"
readonly STATE_DIR="/var/lib/${APP_NAME}"
# shellcheck disable=SC2034
readonly UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}"
readonly LOCK_FILE="/var/lock/${APP_NAME}.lock"

die() { echo "${APP_NAME}: $*" >&2; exit 1; }
note() { echo "${APP_NAME}: $*" >&2; }

require_root() {
  [[ ${EUID} -eq 0 ]] || die "run this command as root"
}

supported_linux_distribution() {
  local distribution_id=${1:-} version_id=${2:-} distribution_name=${3:-} major
  major=${version_id%%.*}
  case "${distribution_id}" in
    debian) [[ "${version_id}" == "12" || "${version_id}" == "13" ]] ;;
    ubuntu) [[ "${version_id}" == "22.04" || "${version_id}" == "24.04" || "${version_id}" == "26.04" ]] ;;
    centos)
      [[ "${distribution_name}" == *"CentOS Stream"* && ( "${major}" == "9" || "${major}" == "10" ) ]]
      ;;
    rocky|almalinux) [[ "${major}" == "9" || "${major}" == "10" ]] ;;
    *) return 1 ;;
  esac
}

distribution_package_family() {
  case "${1:-}" in
    debian|ubuntu) printf 'apt\n' ;;
    centos|rocky|almalinux) printf 'dnf\n' ;;
    *) return 1 ;;
  esac
}

required_php_version_for_distribution() {
  local distribution_id=${1:-} version_id=${2:-} major
  major=${version_id%%.*}
  case "${distribution_id}" in
    # Ubuntu 22.04's distribution-maintained runtime is PHP 8.1. All newer
    # supported platforms must use PHP 8.2 or later.
    ubuntu)
      case "${version_id}" in
        22.04) printf '8.1\n' ;;
        24.04|26.04) printf '8.2\n' ;;
        *) return 1 ;;
      esac
      ;;
    debian)
      [[ "${major}" == 12 || "${major}" == 13 ]] || return 1
      printf '8.2\n'
      ;;
    centos|rocky|almalinux)
      [[ "${major}" == 9 || "${major}" == 10 ]] || return 1
      printf '8.2\n'
      ;;
    *) return 1 ;;
  esac
}

current_required_php_version() {
  [[ -r /etc/os-release ]] || return 1
  local ID='' VERSION_ID='' NAME=''
  # shellcheck disable=SC1091
  . /etc/os-release
  required_php_version_for_distribution "${ID:-}" "${VERSION_ID:-}"
}

require_linux_distribution() {
  [[ -r /etc/os-release ]] || die "a supported Debian, Ubuntu, CentOS Stream, Rocky Linux or AlmaLinux release is required"
  local ID='' VERSION_ID='' NAME=''
  # shellcheck disable=SC1091
  . /etc/os-release
  supported_linux_distribution "${ID:-}" "${VERSION_ID:-}" "${NAME:-}" || \
    die "unsupported Linux release: ${ID:-unknown} ${VERSION_ID:-unknown}; see README.md for the supported matrix"
}

take_lock() {
  # update.sh invokes the downloaded install.sh while retaining this lock. The
  # child inherits fd 9, so it must lock that same open file description rather
  # than open a second descriptor and deadlock against its parent.
  if [[ "${PPFLIGHT_PDF_LOCK_HELD:-}" == "1" ]]; then
    flock -n 9 || die "inherited lifecycle lock is unavailable"
    return
  fi
  install -d -m 0755 /var/lock
  exec 9>"${LOCK_FILE}"
  flock -n 9 || die "another install, update, rollback or uninstall is running"
}

source_dir_of() {
  cd -P "$(dirname "${BASH_SOURCE[1]}")" && pwd
}

absolute_dir() {
  local target=$1 resolved
  [[ -n "${target}" && ! -L "${target}" ]] || die "artifact directory must not be empty or a symlink"
  resolved="$(realpath -m -- "${target}")"
  [[ "${resolved}" =~ ^/[A-Za-z0-9._/@+=-]+$ ]] || \
    die "artifact directory contains characters unsafe for JSON or systemd templates"
  case "${resolved}" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/proc|/root|/run|/sbin|/sys|/tmp|/usr|/var|/www|/WWW|\
    /bin/*|/boot/*|/dev/*|/etc/*|/home/*|/lib/*|/lib64/*|/proc/*|/root/*|/run/*|/sbin/*|/sys/*|/tmp/*|/usr/*|/var/tmp/*)
      die "artifact directory points at a protected or ephemeral filesystem path"
      ;;
  esac
  mkdir -p -- "${resolved}"
  [[ -d "${resolved}" && ! -L "${resolved}" ]] || die "artifact directory must resolve to a real directory"
  cd -P -- "${resolved}" && pwd
}

sed_replacement() {
  # Artifact paths are normalized before this point; escape the remaining
  # characters meaningful to the systemd/config template substitution.
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

atomic_symlink() {
  local target=$1 link=$2 temporary
  temporary="${link}.new.$$"
  ln -s -- "${target}" "${temporary}"
  mv -Tf -- "${temporary}" "${link}"
}

current_release() {
  [[ -L "${CURRENT_LINK}" ]] || return 1
  readlink -f -- "${CURRENT_LINK}"
}

artifact_dir_from_install_env() {
  [[ -r "${INSTALL_ENV}" ]] || return 1
  # install.env is root-owned and generated by this installer; only accept a
  # simple quoted value to avoid evaluating configuration as shell code.
  sed -n 's/^PDF_AGENT_ARTIFACT_DIR=//p' "${INSTALL_ENV}" | head -n 1
}

write_install_env_if_missing() {
  local artifact_dir=$1 temp
  [[ -e "${INSTALL_ENV}" ]] && return 0
  temp="${INSTALL_ENV}.new.$$"
  umask 077
  printf 'PDF_AGENT_ARTIFACT_DIR=%s\n' "${artifact_dir}" >"${temp}"
  chown root:"${SERVICE_USER}" "${temp}"
  chmod 0640 "${temp}"
  mv -f -- "${temp}" "${INSTALL_ENV}"
}

ensure_service_account() {
  if ! getent group "${SERVICE_USER}" >/dev/null; then
    groupadd --system "${SERVICE_USER}"
  fi
  if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --gid "${SERVICE_USER}" --home-dir "${STATE_DIR}" \
      --shell /usr/sbin/nologin --comment "PPFlight PDF Agent" "${SERVICE_USER}"
  fi
}

ensure_runtime_dirs() {
  local artifact_dir=$1
  install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${STATE_DIR}"
  [[ -d "${artifact_dir}" && ! -L "${artifact_dir}" ]] || die "artifact directory is not a real directory"
  # Never take ownership of a populated directory that belongs to another
  # account. This prevents a typo such as --artifact-dir /srv from turning a
  # broad existing tree into service-owned data.
  if [[ "$(stat -c '%U' "${artifact_dir}")" != "${SERVICE_USER}" ]] && \
     find "${artifact_dir}" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    die "refusing to take ownership of a populated artifact directory"
  fi
  chown "${SERVICE_USER}:${SERVICE_USER}" "${artifact_dir}"
  chmod 0750 "${artifact_dir}"
}

python_runtime_ok() {
  command -v python3 >/dev/null || return 1
  python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' || return 1
  python3 -c 'import venv' || return 1
}

php_runtime_ok() {
  local minimum_php=${1:-} php_binary extension
  if [[ -z "${minimum_php}" ]]; then
    minimum_php="$(current_required_php_version)" || return 1
  fi
  php_binary="$(fixed_php_binary)" || return 1
  PHP_MINIMUM="${minimum_php}" "${php_binary}" -r \
    'exit(version_compare(PHP_VERSION, getenv("PHP_MINIMUM"), ">=") ? 0 : 1);' || return 1
  for extension in mbstring xml gd; do
    "${php_binary}" -m | grep -Fxq "${extension}" || return 1
  done
}

python_and_php_ok() {
  python_runtime_ok && php_runtime_ok "${1:-}"
}

fixed_php_binary() {
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin command -v php
}

php_binary_is_os_managed() {
  local php_binary
  php_binary="$(fixed_php_binary)" || return 1
  php_binary="$(readlink -f -- "${php_binary}")"
  case "${1:-}" in
    apt) dpkg-query -S "${php_binary}" >/dev/null 2>&1 ;;
    dnf) rpm -qf "${php_binary}" >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

install_apt_dependencies() {
  local minimum_php=$1
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates coreutils curl findutils grep gzip passwd python3 \
    python3-venv sed systemd tar util-linux
  if ! php_runtime_ok "${minimum_php}"; then
    if fixed_php_binary >/dev/null && ! php_binary_is_os_managed apt; then
      die "the existing custom PHP runtime is incomplete; install PHP >=${minimum_php} with mbstring, xml and gd without changing the control-panel runtime"
    fi
    if fixed_php_binary >/dev/null && \
       ! PHP_MINIMUM="${minimum_php}" "$(fixed_php_binary)" -r \
         'exit(version_compare(PHP_VERSION, getenv("PHP_MINIMUM"), ">=") ? 0 : 1);'; then
      die "refusing to replace an existing PHP runtime older than ${minimum_php}"
    fi
    apt-get install -y --no-install-recommends php-cli php-mbstring php-xml php-gd
  fi
}

install_dnf_dependencies() {
  local version_id=$1 minimum_php=$2 major
  major=${version_id%%.*}
  dnf install -y --setopt=install_weak_deps=False \
    ca-certificates coreutils curl findutils grep gzip python3 sed shadow-utils \
    systemd tar util-linux
  if php_runtime_ok "${minimum_php}"; then
    return 0
  fi
  if fixed_php_binary >/dev/null; then
    if ! php_binary_is_os_managed dnf; then
      die "the existing custom PHP runtime is incomplete; install PHP >=${minimum_php} with mbstring, xml and gd without changing the control-panel runtime"
    fi
    if ! PHP_MINIMUM="${minimum_php}" "$(fixed_php_binary)" -r \
      'exit(version_compare(PHP_VERSION, getenv("PHP_MINIMUM"), ">=") ? 0 : 1);'; then
      die "refusing to switch or replace an existing PHP runtime older than ${minimum_php}"
    fi
  elif [[ "${major}" == "9" ]]; then
    # EL9 defaults to PHP 8.0. Select an official AppStream before installing
    # any PHP RPMs, but never reset or switch a pre-existing system stream.
    dnf module enable -y php:8.2 || \
      die "the official PHP 8.2 AppStream is unavailable; configure a supported OS repository and retry"
  fi
  dnf install -y --setopt=install_weak_deps=False php-cli php-mbstring php-xml php-gd
}

install_dependencies() {
  require_linux_distribution
  local ID='' VERSION_ID='' NAME='' package_family minimum_php
  # shellcheck disable=SC1091
  . /etc/os-release
  package_family="$(distribution_package_family "${ID:-}")" || die "unsupported package manager family"
  minimum_php="$(required_php_version_for_distribution "${ID:-}" "${VERSION_ID:-}")" || \
    die "cannot resolve the PHP policy for this distribution"
  case "${package_family}" in
    apt) install_apt_dependencies "${minimum_php}" ;;
    dnf) install_dnf_dependencies "${VERSION_ID:-}" "${minimum_php}" ;;
    *) die "unsupported package manager family" ;;
  esac
}

health_check() {
  local attempts=15
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if curl --fail --silent --show-error --max-time 3 http://127.0.0.1:9760/healthz >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

post_start_check() {
  health_check || return 1
  # A first installation cannot yet authenticate to ADMIN. Once binding state
  # exists, verify both the local listener and a real authenticated heartbeat.
  if [[ -s "${STATE_DIR}/state.json" ]]; then
    local release
    release="$(current_release)" || return 1
    runuser -u "${SERVICE_USER}" -- "${release}/.venv/bin/python" \
      "${release}/agent.py" --config "${CONFIG_FILE}" check >/dev/null
  fi
}
