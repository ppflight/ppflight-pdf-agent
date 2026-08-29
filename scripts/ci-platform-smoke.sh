#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "${SOURCE_DIR}/scripts/lib.sh"

VERSION='ppflight-release-version-sentinel'
require_root
require_linux_distribution
[[ "${VERSION}" == 'ppflight-release-version-sentinel' ]] || \
  die "distribution check overwrote the lifecycle release version"
install_dependencies
[[ "${VERSION}" == 'ppflight-release-version-sentinel' ]] || \
  die "dependency installation overwrote the lifecycle release version"
PLATFORM_MINIMUM_PHP="$(current_required_php_version)" || die "cannot resolve the PHP policy"
[[ "${VERSION}" == 'ppflight-release-version-sentinel' ]] || \
  die "PHP policy resolution overwrote the lifecycle release version"
python_and_php_ok "${PLATFORM_MINIMUM_PHP}" || die "installed Python/PHP runtime failed validation"
for required_command in \
  curl find flock getent groupadd gzip realpath runuser sed stat systemctl \
  systemd-analyze tar useradd; do
  command -v "${required_command}" >/dev/null || \
    die "installed runtime is missing command: ${required_command}"
done

VENV_DIR="$(mktemp -d)"
UNIT_DIR="$(mktemp -d)"
CHECK_UNIT_FILE="${UNIT_DIR}/ppflight-pdf-agent.service"
cleanup() { rm -rf -- "${VENV_DIR}" "${UNIT_DIR}"; }
trap cleanup EXIT
python3 -m venv --without-pip "${VENV_DIR}/venv"

sed \
  -e 's|^ExecStartPre=.*|ExecStartPre=/bin/true|' \
  -e 's|^ExecStart=.*|ExecStart=/bin/true|' \
  -e 's|^ReadWritePaths=.*|ReadWritePaths=/tmp|' \
  "${SOURCE_DIR}/packaging/systemd/ppflight-pdf-agent.service" >"${CHECK_UNIT_FILE}"
systemd-analyze verify "${CHECK_UNIT_FILE}"

[[ -f "${SOURCE_DIR}/renderer/vendor/autoload.php" ]] || \
  die "CI platform smoke requires prebuilt renderer/vendor"
"$(fixed_php_binary)" "${SOURCE_DIR}/tests/renderer_test.php"
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest \
  tests.test_agent_core.AgentCoreTests.test_fixed_renderer_writes_a_real_ppflight_prefixed_pdf -v

# Publication is optional at install time, but every documented standard-Nginx
# platform must parse both the Tunnel filter and direct-DNS TLS examples.
read -r PLATFORM_ID PLATFORM_VERSION < <(
  (
    ID=''
    VERSION_ID=''
    # shellcheck disable=SC1091
    . /etc/os-release
    printf '%s %s\n' "${ID:-unknown}" "${VERSION_ID:-unknown}"
  )
)
[[ "${VERSION}" == 'ppflight-release-version-sentinel' ]] || \
  die "platform identity read overwrote the lifecycle release version"
PLATFORM_PACKAGE_FAMILY="$(distribution_package_family "${PLATFORM_ID}")"
case "${PLATFORM_PACKAGE_FAMILY}" in
  apt) apt-get install -y --no-install-recommends nginx openssl ;;
  dnf) dnf install -y --setopt=install_weak_deps=False nginx openssl ;;
  *) die "unsupported package manager family for Nginx smoke" ;;
esac
"${SOURCE_DIR}/scripts/verify-nginx-config.sh"

echo "platform smoke passed: ${PLATFORM_ID} ${PLATFORM_VERSION}"
