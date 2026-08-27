#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v nginx >/dev/null || { echo "nginx is required" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

WORK_DIR="$(mktemp -d)"
cleanup() { rm -rf -- "${WORK_DIR}"; }
trap cleanup EXIT

openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj /CN=pdf-worker.ppflight.com \
  -keyout "${WORK_DIR}/key.pem" -out "${WORK_DIR}/cert.pem" >/dev/null 2>&1

sed \
  -e "s|/etc/letsencrypt/live/pdf-worker.ppflight.com/fullchain.pem|${WORK_DIR}/cert.pem|" \
  -e "s|/etc/letsencrypt/live/pdf-worker.ppflight.com/privkey.pem|${WORK_DIR}/key.pem|" \
  "${SOURCE_DIR}/packaging/nginx/pdf-agent-public-tls.conf.example" >"${WORK_DIR}/public.conf"

printf '%s\n' \
  "pid ${WORK_DIR}/nginx.pid;" \
  'error_log stderr notice;' \
  'events {}' \
  'http {' \
  "  include ${WORK_DIR}/public.conf;" \
  "  include ${SOURCE_DIR}/packaging/nginx/pdf-agent-local.conf.example;" \
  '}' >"${WORK_DIR}/nginx.conf"

nginx -t -p "${WORK_DIR}" -c "${WORK_DIR}/nginx.conf"
echo "Nginx direct-DNS and Tunnel examples are syntactically valid"
