#!/usr/bin/env bash
# Standalone entry point: no checkout, Composer, nginx or sourced files needed.
set -Eeuo pipefail

readonly RELEASE_VERSION=1.0.10
readonly RELEASE_BASE="https://github.com/ppflight/ppflight-pdf-agent/releases/download/v${RELEASE_VERSION}"
readonly APP_CURRENT=/opt/ppflight-pdf-agent/current
readonly CONFIG_PATH=/etc/ppflight-pdf-agent/config.json
WORK_DIR=''
STARTED=$SECONDS

note() { printf '\n[PPFlight PDF] %s\n' "$*" >&2; }
fail() { note "$*"; exit 1; }
cleanup() { [[ -z "${WORK_DIR}" ]] || rm -rf -- "${WORK_DIR}"; }
trap cleanup EXIT
trap 'note "安装未完成，请按上方错误处理后重新运行；已有账单文件不会被清空。"' ERR

apt_for_installer() (
  # All transport/source overrides are private to this APT process.
  local force_ipv4=${PPFLIGHT_APT_FORCE_IPV4:-true} apt_config source_file copied_file
  local source_dir=''
  local -a source_options=() source_files=()
  [[ "${force_ipv4}" == true || "${force_ipv4}" == false ]] || \
    fail "PPFLIGHT_APT_FORCE_IPV4 must be true or false"
  apt_config=$(apt-config dump 2>/dev/null)
  # Preserve custom source locations. HTTPS conversion is limited to official
  # Ubuntu mirrors and keeps suites, components and Signed-By unchanged.
  if [[ -s /etc/ssl/certs/ca-certificates.crt ]] &&
      [[ "${apt_config}" == *'Dir::Etc "etc/apt";'* || "${apt_config}" == *'Dir::Etc "/etc/apt";'* ]] &&
      [[ "${apt_config}" == *'Dir::Etc::sourcelist "sources.list";'* ]] &&
      [[ "${apt_config}" == *'Dir::Etc::sourceparts "sources.list.d";'* ]]; then
    for source_file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
      [[ ! -f "${source_file}" ]] || source_files+=("${source_file}")
    done
    if [[ ${#source_files[@]} -gt 0 ]] && grep -Eq 'http://(([a-z]{2}\.)?archive|security)\.ubuntu\.com/ubuntu' "${source_files[@]}"; then
      source_dir=$(mktemp -d /var/tmp/ppflight-pdf-apt.XXXXXX)
      trap 'rm -rf -- "${source_dir}"' EXIT
      mkdir "${source_dir}/sources.list.d"
      : >"${source_dir}/sources.list"
      for source_file in "${source_files[@]}"; do
        if [[ "${source_file}" == /etc/apt/sources.list ]]; then
          copied_file="${source_dir}/sources.list"
        else
          copied_file="${source_dir}/sources.list.d/${source_file##*/}"
        fi
        cp -L --preserve=mode -- "${source_file}" "${copied_file}"
        sed -E -i 's#http://(([a-z]{2}\.)?archive|security)\.ubuntu\.com/ubuntu#https://\1.ubuntu.com/ubuntu#g' "${copied_file}"
      done
      source_options=(-o "Dir::Etc::sourcelist=${source_dir}/sources.list" -o "Dir::Etc::sourceparts=${source_dir}/sources.list.d")
      # Minimal images using OpenSSL may not yet have the default cert.pem
      # symlink. Point APT at the verified CA bundle without overriding any
      # administrator-configured CAInfo policy or disabling verification.
      if [[ "${apt_config}" != *'::CaInfo '* && "${apt_config}" != *'::CAInfo '* ]]; then
        source_options+=(-o Acquire::https::CaInfo=/etc/ssl/certs/ca-certificates.crt)
      fi
      note 'using HTTPS for official Ubuntu mirrors in temporary APT sources (system sources unchanged)'
    fi
  fi
  DEBIAN_FRONTEND=noninteractive apt-get "${source_options[@]}" \
    -o "Acquire::ForceIPv4=${force_ipv4}" \
    -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 \
    -o Acquire::Retries=2 "$@"
)


main() {
  case "${1:-}" in
    -h|--help) printf '%s\n' 'Usage: sudo bash bootstrap.sh [--skip-bind]' 'Default: install, then interactively bind using a one-time code.'; return ;;
    ''|--skip-bind) ;;
    *) fail '不支持的参数；使用 --help 查看说明。' ;;
  esac
  [[ $# -le 1 ]] || fail '参数过多。'
  [[ ${EUID} -eq 0 ]] || fail '请使用 root 或 sudo 执行。'
  [[ -d /run/systemd/system ]] || fail '需要使用 systemd 的 Linux 服务器。'
  command -v curl >/dev/null || fail '缺少 curl，请先通过系统包管理器安装 curl。'
  # Downloads use verified HTTPS only, with progress and bounded stalls.
  local -a curl_options=(--fail --location --proto '=https' --proto-redir '=https'
    --connect-timeout 20 --max-time 600 --retry 2 --speed-time 30 --speed-limit 1024
    --max-filesize 104857600)
  note '1/4 检查环境'
  if ! command -v python3 >/dev/null; then
    if command -v apt-get >/dev/null; then
      apt_for_installer -o APT::Update::Error-Mode=any update
      apt_for_installer install -y --no-install-recommends python3
    elif command -v dnf >/dev/null; then
      dnf install -y --setopt=install_weak_deps=False python3
    else
      fail '缺少 Python 3，且未找到支持的包管理器。'
    fi
  fi
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || fail '需要 Python 3.9 或更新版本。'
  umask 077
  WORK_DIR=$(mktemp -d /var/tmp/ppflight-pdf-setup.XXXXXX)
  local archive="ppflight-pdf-agent-${RELEASE_VERSION}.tar.gz"
  note "2/4 下载 ${RELEASE_VERSION}（GitHub Release，已包含 PDF 渲染依赖）"
  curl "${curl_options[@]}" "${RELEASE_BASE}/${archive}" -o "${WORK_DIR}/${archive}"
  curl "${curl_options[@]}" "${RELEASE_BASE}/${archive}.sha256" -o "${WORK_DIR}/${archive}.sha256"
  # Validate checksum and all members before extracting anything as root.
  python3 - "${WORK_DIR}" "${archive}" <<'PY'
import hashlib, pathlib, re, sys, tarfile
work = pathlib.Path(sys.argv[1])
filename = sys.argv[2]
checksum = (work / (filename + '.sha256')).read_text().strip()
match = re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?' + re.escape(filename), checksum)
if not match or hashlib.sha256((work / filename).read_bytes()).hexdigest() != match[1].lower():
    raise SystemExit('安装包 SHA-256 校验失败，请重新下载。')
expected_root = filename.removesuffix('.tar.gz')
with tarfile.open(work / filename, 'r:gz') as archive:
    total = 0
    names = set()
    for count, member in enumerate(archive, 1):
        path = pathlib.PurePosixPath(member.name)
        if (count > 10000 or not path.parts or path.parts[0] != expected_root
                or path.is_absolute() or '..' in path.parts or '\\' in member.name
                or str(path) in names or not (member.isfile() or member.isdir())
                or not 0 <= member.size <= 50 * 1024 * 1024):
            raise SystemExit('安装包包含不安全的路径、类型或大小，已拒绝解压。')
        names.add(str(path))
        total += member.size
        if total > 250 * 1024 * 1024:
            raise SystemExit('安装包解压大小超过限制。')
    if expected_root not in names or expected_root + '/install.sh' not in names:
        raise SystemExit('安装包缺少必要文件。')
    # All members are now validated; no links or special files are permitted.
    archive.extractall(work)
PY
  local source_dir="${WORK_DIR}/ppflight-pdf-agent-${RELEASE_VERSION}"
  [[ -f "${source_dir}/renderer/vendor/autoload.php" ]] || fail '安装包未包含渲染依赖，拒绝转为在线 Composer 安装。'
  note '3/4 安装依赖和服务（沿用系统软件源；不清空已有文件）'
  local installed_version=''
  if [[ -x "${APP_CURRENT}/.venv/bin/python" && -f "${APP_CURRENT}/agent.py" ]]; then
    installed_version=$("${APP_CURRENT}/.venv/bin/python" "${APP_CURRENT}/agent.py" version)
  fi
  if [[ "${installed_version}" == "${RELEASE_VERSION}" ]]; then
    note '此版本已安装，保留现有配置，继续检查绑定。'
    systemctl is-active --quiet ppflight-pdf-agent.service || systemctl start ppflight-pdf-agent.service
  elif [[ -n "${installed_version}" ]]; then
    python3 - "${installed_version}" "${RELEASE_VERSION}" <<'PYVERSION'
import re, sys
if not all(re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', v) for v in sys.argv[1:]):
    raise SystemExit('现有版本格式无法自动比较，请使用受控升级。')
if tuple(map(int, sys.argv[1].split('.'))) > tuple(map(int, sys.argv[2].split('.'))):
    raise SystemExit('已安装更新版本，不自动降级。')
PYVERSION
    # The lifecycle installer preserves the installed artifact directory and
    # restores the previous release if its post-install health check fails.
    (umask 022; bash "${source_dir}/install.sh" --version "${RELEASE_VERSION}" --install-deps)
  else
    (umask 022; bash "${source_dir}/install.sh" --version "${RELEASE_VERSION}" --install-deps \
      --artifact-dir /var/lib/ppflight-pdf-agent/artifacts)
  fi
  note '4/4 检查绑定'
  local bound
  bound=$(python3 "${APP_CURRENT}/scripts/status-report.py" --config "${CONFIG_PATH}" --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["binding"])')
  if [[ "${bound}" == bound ]]; then
    note '已有绑定，保留现有绑定信息。'
  elif [[ "${1:-}" != --skip-bind ]]; then
    [[ -r /dev/tty ]] || fail '需要终端读取绑定码；可使用 --skip-bind 安装，稍后运行 ag-pdf 绑定。'
    note '请粘贴后台生成的一次性绑定码，然后按回车（输入不回显）：'
    bash "${APP_CURRENT}/bind.sh" </dev/tty
  else
    note '已跳过绑定；稍后运行 ag-pdf 绑定。'
  fi
  local tunnel_port
  tunnel_port=$(python3 - "${CONFIG_PATH}" <<'PY'
import json, sys
with open(sys.argv[1]) as stream:
    print(json.load(stream).get('tunnel_port', 0))
PY
)
  note "安装完成，用时 $((SECONDS - STARTED)) 秒。"
  if [[ "${tunnel_port}" != 0 ]]; then
    note "同机 Tunnel 的 Service 填写：http://127.0.0.1:${tunnel_port}"
  else
    note '沿用已有下载代理配置；本次升级未替换现有 Tunnel／Nginx。'
  fi
  note '公开域名请填写后台保存的 PDF 下载域名；日常管理命令：ag-pdf'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
