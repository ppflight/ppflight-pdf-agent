#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR=""
VERSION=""
OUTPUT=""

usage() {
  echo "Usage: $0 --source DIRECTORY --version VERSION --output ARCHIVE.tar.gz" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE_DIR=${2:?missing source directory}; shift 2 ;;
    --version) VERSION=${2:?missing version}; shift 2 ;;
    --output) OUTPUT=${2:?missing output archive}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

[[ -n "${SOURCE_DIR}" && -n "${VERSION}" && -n "${OUTPUT}" ]] || { usage; exit 2; }
[[ "${VERSION}" =~ ^[A-Za-z0-9][-A-Za-z0-9._+]*$ ]] || { echo "invalid release version" >&2; exit 2; }
SOURCE_DIR="$(cd -P -- "${SOURCE_DIR}" && pwd)"
OUTPUT="$(realpath -m -- "${OUTPUT}")"
[[ -d "$(dirname -- "${OUTPUT}")" ]] || { echo "output directory does not exist" >&2; exit 2; }
[[ ! -e "${OUTPUT}" ]] || { echo "refusing to overwrite release archive" >&2; exit 2; }

GIT_ROOT="$(git -C "${SOURCE_DIR}" rev-parse --show-toplevel 2>/dev/null)" || {
  echo "release source must be a Git worktree" >&2
  exit 1
}
GIT_ROOT="$(cd -P -- "${GIT_ROOT}" && pwd)"
[[ "${GIT_ROOT}" == "${SOURCE_DIR}" ]] || {
  echo "release source must be the Git worktree root" >&2
  exit 1
}
if [[ -n "$(git -C "${SOURCE_DIR}" status --porcelain --untracked-files=normal)" ]]; then
  echo "release source worktree must be clean; commit or remove all changes first" >&2
  exit 1
fi

SOURCE_VERSION="$(python3 "${SOURCE_DIR}/agent.py" version)"
[[ "${SOURCE_VERSION}" == "${VERSION}" ]] || {
  echo "source version ${SOURCE_VERSION} does not match release version ${VERSION}" >&2
  exit 1
}
command -v composer >/dev/null || { echo "Composer 2 is required to build a release" >&2; exit 1; }
composer --version --no-ansi | grep -Eq '^Composer version 2\.' || {
  echo "Composer 2 is required to build a release" >&2
  exit 1
}

WORK_DIR="$(mktemp -d)"
cleanup() { rm -rf -- "${WORK_DIR}"; }
trap cleanup EXIT
ROOT_NAME="ppflight-pdf-agent-${VERSION}"
RELEASE_ROOT="${WORK_DIR}/${ROOT_NAME}"
mkdir -p -- "${RELEASE_ROOT}"

RELEASE_PATHS=(
  README.md agent.py ag-pdf pag bind.sh install.sh update.sh rollback.sh uninstall.sh
  pdf_agent renderer packaging scripts docs tests
)
for path in "${RELEASE_PATHS[@]}"; do
  git -C "${SOURCE_DIR}" cat-file -e "HEAD:${path}" 2>/dev/null || {
    echo "missing tracked release path at HEAD: ${path}" >&2
    exit 1
  }
done
# Bind every source byte to the current commit and exclude untracked/ignored
# worktree content by construction.
git -C "${SOURCE_DIR}" archive --format=tar HEAD -- "${RELEASE_PATHS[@]}" | \
  tar -C "${RELEASE_ROOT}" -xf -
# Never trust a working-tree vendor directory. Build dependencies from the
# committed lock file inside the isolated staging tree.
rm -rf -- "${RELEASE_ROOT}/renderer/vendor"
COMPOSER_ALLOW_SUPERUSER=1 composer validate --working-dir="${RELEASE_ROOT}/renderer" \
  --strict --no-check-publish
COMPOSER_ALLOW_SUPERUSER=1 composer install --working-dir="${RELEASE_ROOT}/renderer" \
  --no-dev --prefer-dist --no-interaction --no-progress --no-plugins --no-scripts \
  --classmap-authoritative
COMPOSER_ALLOW_SUPERUSER=1 composer audit --working-dir="${RELEASE_ROOT}/renderer" \
  --locked --no-interaction --no-ansi
# $argv is evaluated by PHP, not Bash.
# shellcheck disable=SC2016
php -r 'require $argv[1]; exit(class_exists("Dompdf\\Dompdf") ? 0 : 1);' \
  "${RELEASE_ROOT}/renderer/vendor/autoload.php" || {
    echo "freshly built renderer/vendor failed validation" >&2
    exit 1
  }
find "${RELEASE_ROOT}" -type d -name '__pycache__' -prune -exec rm -rf -- {} +
find "${RELEASE_ROOT}" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
if find "${RELEASE_ROOT}" -type l -print -quit | grep -q .; then
  echo "release staging tree contains a symbolic link" >&2
  exit 1
fi

if [[ -n "${SOURCE_DATE_EPOCH:-}" ]]; then
  [[ "${SOURCE_DATE_EPOCH}" =~ ^[0-9]+$ ]] || { echo "SOURCE_DATE_EPOCH must be numeric" >&2; exit 2; }
  RELEASE_EPOCH=${SOURCE_DATE_EPOCH}
elif git -C "${SOURCE_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  RELEASE_EPOCH="$(git -C "${SOURCE_DIR}" log -1 --format=%ct)"
else
  RELEASE_EPOCH="$(stat -c '%Y' "${SOURCE_DIR}/agent.py")"
fi

tar --sort=name --mtime="@${RELEASE_EPOCH}" --owner=0 --group=0 --numeric-owner \
  --pax-option=delete=atime,delete=ctime -C "${WORK_DIR}" -cf - "${ROOT_NAME}" | \
  gzip -n >"${OUTPUT}"
python3 "${SOURCE_DIR}/scripts/verify-release-archive.py" "${OUTPUT}" >/dev/null
echo "built ${OUTPUT} with bundled renderer dependencies"
