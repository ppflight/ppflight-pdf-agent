#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "${SOURCE_DIR}/scripts/lib.sh"

# Keep the hyphen first in the second ERE character class. Some supported
# remote Bash/locale combinations reject the formerly trailing-hyphen form
# even though the release value itself is plain ASCII.
for release_version in 1.0.4 1.0.4-rc.1 v1.0.4 build_2026+08; do
  [[ "${release_version}" =~ ^[A-Za-z0-9][-A-Za-z0-9._+]*$ ]] || {
    echo "expected valid release version: ${release_version}" >&2
    exit 1
  }
done
for release_version in '' .1 /1 '1 2' '1/2' '1?2'; do
  if [[ "${release_version}" =~ ^[A-Za-z0-9][-A-Za-z0-9._+]*$ ]]; then
    echo "expected invalid release version" >&2
    exit 1
  fi
done

expect_supported() {
  supported_linux_distribution "$1" "$2" "$3" || {
    echo "expected supported: $1 $2 $3" >&2
    exit 1
  }
}

expect_rejected() {
  if supported_linux_distribution "$1" "$2" "$3"; then
    echo "expected rejected: $1 $2 $3" >&2
    exit 1
  fi
}

expect_supported debian 12 "Debian GNU/Linux"
expect_supported debian 13 "Debian GNU/Linux"
expect_supported ubuntu 22.04 Ubuntu
expect_supported ubuntu 24.04 Ubuntu
expect_supported ubuntu 26.04 Ubuntu
expect_supported centos 9 "CentOS Stream 9"
expect_supported centos 10 "CentOS Stream 10"
expect_supported rocky 9.6 "Rocky Linux"
expect_supported rocky 10.0 "Rocky Linux"
expect_supported almalinux 9.6 "AlmaLinux"
expect_supported almalinux 10.0 "AlmaLinux"

expect_rejected debian 11 "Debian GNU/Linux"
expect_rejected debian 14 "Debian GNU/Linux"
expect_rejected ubuntu 20.04 Ubuntu
expect_rejected ubuntu 24.10 Ubuntu
expect_rejected centos 9 "CentOS Linux"
expect_rejected rocky 8.10 "Rocky Linux"
expect_rejected almalinux 11.0 "AlmaLinux"
expect_rejected fedora 43 Fedora

[[ "$(distribution_package_family debian)" == apt ]]
[[ "$(distribution_package_family ubuntu)" == apt ]]
[[ "$(distribution_package_family centos)" == dnf ]]
[[ "$(distribution_package_family rocky)" == dnf ]]
[[ "$(distribution_package_family almalinux)" == dnf ]]
if distribution_package_family fedora >/dev/null 2>&1; then
  echo "unexpected package family for Fedora" >&2
  exit 1
fi

[[ "$(required_php_version_for_distribution ubuntu 22.04)" == 8.1 ]]
for platform in \
  'debian 12' 'debian 13' \
  'ubuntu 24.04' 'ubuntu 26.04' \
  'centos 9' 'centos 10' \
  'rocky 9' 'rocky 10' \
  'almalinux 9' 'almalinux 10'; do
  # shellcheck disable=SC2086
  [[ "$(required_php_version_for_distribution ${platform})" == 8.2 ]]
done
if required_php_version_for_distribution ubuntu 20.04 >/dev/null 2>&1; then
  echo "unexpected PHP policy for unsupported Ubuntu" >&2
  exit 1
fi

# Dependency mutation policy: an incomplete custom/control-panel PHP resolved
# from the Agent's fixed path must stop before any PHP package is installed.
set +e
CUSTOM_PHP_FAILURE="$(
  (
    apt-get() { :; }
    php_runtime_ok() { return 1; }
    fixed_php_binary() { printf '/opt/control-panel/php\n'; }
    php_binary_is_os_managed() { return 1; }
    install_apt_dependencies 8.2
  ) 2>&1
)"
CUSTOM_PHP_STATUS=$?
set -e
[[ ${CUSTOM_PHP_STATUS} -eq 1 ]]
[[ "${CUSTOM_PHP_FAILURE}" == *'existing custom PHP runtime is incomplete'* ]]

# A complete administrator-managed PHP in the fixed path is deliberately
# accepted (important for aaPanel hosts), and must not trigger PHP package or
# module changes.
APT_CALLS="$(mktemp)"
(
  apt-get() { printf '%s\n' "$*" >>"${APT_CALLS}"; }
  php_runtime_ok() { return 0; }
  fixed_php_binary() { printf '/usr/local/bin/php\n'; }
  install_apt_dependencies 8.2
)
if grep -Fq 'php-cli' "${APT_CALLS}"; then
  echo "complete custom PHP must not trigger package replacement" >&2
  exit 1
fi

DNF_CUSTOM_CALLS="$(mktemp)"
(
  dnf() { printf '%s\n' "$*" >>"${DNF_CUSTOM_CALLS}"; }
  php_runtime_ok() { return 0; }
  fixed_php_binary() { printf '/usr/local/bin/php\n'; }
  install_dnf_dependencies 9.6 8.2
)
if grep -Eq 'module enable|php-cli' "${DNF_CUSTOM_CALLS}"; then
  echo "complete custom PHP must not trigger DNF PHP changes" >&2
  exit 1
fi

: >"${DNF_CUSTOM_CALLS}"
set +e
DNF_CUSTOM_FAILURE="$(
  (
    dnf() { printf '%s\n' "$*" >>"${DNF_CUSTOM_CALLS}"; }
    php_runtime_ok() { return 1; }
    fixed_php_binary() { printf '/usr/local/bin/php\n'; }
    php_binary_is_os_managed() { return 1; }
    install_dnf_dependencies 9.6 8.2
  ) 2>&1
)"
DNF_CUSTOM_STATUS=$?
set -e
[[ ${DNF_CUSTOM_STATUS} -eq 1 ]]
[[ "${DNF_CUSTOM_FAILURE}" == *'existing custom PHP runtime is incomplete'* ]]
if grep -Eq 'module enable|php-cli' "${DNF_CUSTOM_CALLS}"; then
  echo "incomplete custom PHP must fail before DNF PHP changes" >&2
  exit 1
fi

# A clean EL9 host must select the official PHP 8.2 stream before packages;
# EL10 must use its default stream without changing module state.
DNF_CALLS="$(mktemp)"
cleanup() { rm -f -- "${APT_CALLS}" "${DNF_CUSTOM_CALLS}" "${DNF_CALLS}"; }
trap cleanup EXIT
(
  dnf() { printf '%s\n' "$*" >>"${DNF_CALLS}"; }
  php_runtime_ok() { return 1; }
  fixed_php_binary() { return 1; }
  install_dnf_dependencies 9.6 8.2
)
grep -Fxq 'module enable -y php:8.2' "${DNF_CALLS}"
grep -Fq 'php-cli php-mbstring php-xml php-gd' "${DNF_CALLS}"

: >"${DNF_CALLS}"
(
  dnf() { printf '%s\n' "$*" >>"${DNF_CALLS}"; }
  php_runtime_ok() { return 1; }
  fixed_php_binary() { return 1; }
  install_dnf_dependencies 10.0 8.2
)
if grep -Fq 'module enable' "${DNF_CALLS}"; then
  echo "EL10 must not change a PHP module stream" >&2
  exit 1
fi
grep -Fq 'php-cli php-mbstring php-xml php-gd' "${DNF_CALLS}"

echo "platform support matrix tests passed"
