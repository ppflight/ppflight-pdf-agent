#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SOURCE_DIR}/scripts/lib.sh"

VERSION=""
ARCHIVE_URL=""
SHA256=""
SIGNATURE_URL=""
GPG_KEYRING=""
ARTIFACT_DIR=""

usage() {
  cat <<'EOF'
Usage: sudo ./update.sh --version VERSION --url RELEASE_TARBALL --sha256 SHA256
                       [--signature-url URL --gpg-keyring FILE] [--artifact-dir DIR]

Updates install an immutable, explicitly versioned release only. The SHA-256 is
mandatory. A detached-signature hook is available when a release signing key is
configured. A failed post-update health check atomically restores current.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION=${2:?missing version}; shift 2 ;;
    --url) ARCHIVE_URL=${2:?missing archive URL}; shift 2 ;;
    --sha256) SHA256=${2:?missing SHA-256}; shift 2 ;;
    --signature-url) SIGNATURE_URL=${2:?missing signature URL}; shift 2 ;;
    --gpg-keyring) GPG_KEYRING=${2:?missing keyring}; shift 2 ;;
    --artifact-dir) ARTIFACT_DIR=${2:?missing artifact directory}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

require_root
take_lock
[[ -n "${VERSION}" && -n "${ARCHIVE_URL}" && -n "${SHA256}" ]] || { usage >&2; exit 2; }
[[ "${VERSION}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]] || die "invalid release version"
[[ "${SHA256}" =~ ^[A-Fa-f0-9]{64}$ ]] || die "--sha256 must be a SHA-256 digest"
[[ -z "${SIGNATURE_URL}" || -n "${GPG_KEYRING}" ]] || die "--signature-url requires --gpg-keyring"
[[ -z "${GPG_KEYRING}" || -n "${SIGNATURE_URL}" ]] || die "--gpg-keyring requires --signature-url"

if [[ -z "${ARTIFACT_DIR}" ]]; then
  ARTIFACT_DIR="$(artifact_dir_from_install_env || true)"
fi
[[ -n "${ARTIFACT_DIR}" ]] || die "missing installed artifact directory; pass --artifact-dir explicitly"

PREVIOUS_RELEASE="$(current_release)" || die "no installed current release"
WORK_DIR="$(mktemp -d /var/tmp/${APP_NAME}-update.XXXXXX)"
cleanup() { rm -rf -- "${WORK_DIR}"; }
trap cleanup EXIT

ARCHIVE="${WORK_DIR}/${APP_NAME}-${VERSION}.tar.gz"
curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
  --max-filesize $((100 * 1024 * 1024)) --output "${ARCHIVE}" "${ARCHIVE_URL}"
[[ $(stat -c '%s' "${ARCHIVE}") -le $((100 * 1024 * 1024)) ]] || die "release archive exceeds 100 MiB"
printf '%s  %s\n' "${SHA256}" "${ARCHIVE}" | sha256sum --check --status || die "SHA-256 verification failed"
if [[ -n "${SIGNATURE_URL}" ]]; then
  command -v gpgv >/dev/null || die "gpgv is required for detached signature verification"
  SIGNATURE="${ARCHIVE}.asc"
  curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --max-filesize 1048576 --output "${SIGNATURE}" "${SIGNATURE_URL}"
  gpgv --keyring "${GPG_KEYRING}" "${SIGNATURE}" "${ARCHIVE}" || die "release signature verification failed"
fi

TOP_LEVEL="$(python3 "${SOURCE_DIR}/scripts/verify-release-archive.py" "${ARCHIVE}")" || \
  die "release archive failed the safe layout check"
[[ "${TOP_LEVEL}" == "${APP_NAME}-${VERSION}" ]] || \
  die "release archive root ${TOP_LEVEL} does not match requested version ${VERSION}"
tar --extract --gzip --file "${ARCHIVE}" --directory "${WORK_DIR}" \
  --no-same-owner --no-same-permissions
RELEASE_SOURCE="${WORK_DIR}/${TOP_LEVEL}"
[[ -x "${RELEASE_SOURCE}/install.sh" ]] || die "release archive does not contain install.sh"
ARCHIVE_VERSION="$(python3 "${RELEASE_SOURCE}/agent.py" version)" || die "release version cannot be read"
[[ "${ARCHIVE_VERSION}" == "${VERSION}" ]] || \
  die "release code version ${ARCHIVE_VERSION} does not match requested version ${VERSION}"

# The installed release records the immutable version and restores the previous
# symlink on any build/start/health error. Config and state are deliberately kept.
PPFLIGHT_PDF_LOCK_HELD=1 "${RELEASE_SOURCE}/install.sh" --version "${VERSION}" --artifact-dir "${ARTIFACT_DIR}"
if ! post_start_check; then
  atomic_symlink "${PREVIOUS_RELEASE}" "${CURRENT_LINK}"
  systemctl restart "${SERVICE_NAME}" || true
  die "updated service did not become healthy; previous release restored"
fi
note "updated to immutable release ${VERSION}"
