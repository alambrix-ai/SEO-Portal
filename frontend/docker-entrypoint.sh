#!/bin/sh
set -eu

cat >/usr/share/nginx/html/config.js <<EOF
window.__APP_CONFIG__ = {
  VITE_API_BASE_URL: "${VITE_API_BASE_URL:-/api/v1}"
};
EOF

exec nginx -g 'daemon off;'
