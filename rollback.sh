#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SOURCE_DIR}/scripts/lib.sh"

TARGET_VERSION=${1:-}
if [[ "${TARGET_VERSION}" == "-h" || "${TARGET_VERSION}" == "--help" ]]; then
  echo "Usage: sudo ./rollback.sh VERSION"
  exit 0
fi
[[ $# -eq 1 ]] || { echo "Usage: sudo ./rollback.sh VERSION" >&2; exit 2; }
require_root
take_lock
[[ "${TARGET_VERSION}" =~ ^[A-Za-z0-9][-A-Za-z0-9._+]*$ ]] || die "invalid release version"
TARGET_RELEASE="${RELEASES_DIR}/${TARGET_VERSION}"
[[ -d "${TARGET_RELEASE}" ]] || die "release not found: ${TARGET_VERSION}"
PREVIOUS_RELEASE="$(current_release)" || die "no current release"

atomic_symlink "${TARGET_RELEASE}" "${CURRENT_LINK}"
if ! systemctl restart "${SERVICE_NAME}" || ! post_start_check; then
  atomic_symlink "${PREVIOUS_RELEASE}" "${CURRENT_LINK}"
  systemctl restart "${SERVICE_NAME}" || true
  die "rollback health check failed; previous release restored"
fi
note "rolled back to ${TARGET_VERSION}"
