#!/bin/sh
# Console container start: validate config, render nginx config and headers,
# write /config.js, then run nginx. Refuses to start on any invalid value,
# because each one ends up inside an HTTP header or a <script>.
set -eu

fail() {
  echo "docker-entrypoint: $1" >&2
  exit 1
}

NL='
'

# ── API base URL and origin ───────────────────────────────────────────────
# Either a same-origin path (/api/v1) or an absolute http(s) URL. The allowed
# characters cannot break out of a JS string, a CSP header or an nginx
# directive (no quotes, backslashes, spaces, ';', '<' or newlines).
API_BASE_URL="${VITE_API_BASE_URL:-/api/v1}"
case "$API_BASE_URL" in
  *"$NL"*) fail "VITE_API_BASE_URL must be a single line." ;;
  # "//host/path" looks like a path but the browser treats it as another origin.
  //*) fail "VITE_API_BASE_URL must not start with //." ;;
esac
printf '%s' "$API_BASE_URL" \
  | grep -Eqx '(https?://[A-Za-z0-9.:-]+)?(/[A-Za-z0-9._~/-]*)?' \
  || fail "VITE_API_BASE_URL must be a path like /api/v1 or an http(s) URL."

case "$API_BASE_URL" in
  http://*|https://*)
    API_ORIGIN=$(printf '%s' "$API_BASE_URL" | sed -E 's#^(https?://[^/]+).*$#\1#')
    printf '%s' "$API_ORIGIN" | grep -Eqx '^https?://[A-Za-z0-9.:-]+$' \
      || fail "VITE_API_BASE_URL has an invalid origin."
    ;;
  *)
    # Same origin: 'self' already covers it in the CSP.
    API_ORIGIN=""
    ;;
esac
export API_ORIGIN

# CSP violation reports go to the API's /csp-report endpoint, on whichever
# origin serves the API. Built from the value checked above, so it has the
# same safe character set.
CSP_REPORT_URI="${API_BASE_URL%/}/csp-report"
export CSP_REPORT_URI

# ── CSP rollout mode ──────────────────────────────────────────────────────
# Report-Only by default for the first release; set CSP_REPORT_ONLY=false to
# enforce.
case "${CSP_REPORT_ONLY:-true}" in
  true) CSP_HEADER_NAME="Content-Security-Policy-Report-Only" ;;
  false) CSP_HEADER_NAME="Content-Security-Policy" ;;
  *) fail "CSP_REPORT_ONLY must be true or false." ;;
esac

# ── nginx config ──────────────────────────────────────────────────────────
# The image copies the template to conf.d/default.conf. Keep it aside on the
# first start so a restart renders from the template again.
TEMPLATE_DIR=/etc/nginx/console
TEMPLATE="$TEMPLATE_DIR/default.conf.template"
RENDERED=/etc/nginx/conf.d/default.conf
if [ ! -f "$TEMPLATE" ]; then
  [ -f "$RENDERED" ] || fail "nginx config template is missing."
  mkdir -p "$TEMPLATE_DIR"
  mv "$RENDERED" "$TEMPLATE"
fi
# Only ${API_ORIGIN} and ${CSP_REPORT_URI} are substituted; nginx's own
# $variables stay as they are.
# shellcheck disable=SC2016
envsubst '${API_ORIGIN} ${CSP_REPORT_URI}' <"$TEMPLATE" >"$RENDERED"

# Included in every location of nginx.conf (see the note there). The policy
# text is $console_csp and the HSTS value is $console_hsts, both defined in
# nginx.conf. HSTS is sent only behind TLS. Reporting-Endpoints names the
# "csp-endpoint" group that the policy's report-to directive points at.
mkdir -p /etc/nginx/snippets
cat >/etc/nginx/snippets/security-headers.conf <<EOF
add_header ${CSP_HEADER_NAME} \$console_csp always;
add_header Reporting-Endpoints 'csp-endpoint="${CSP_REPORT_URI}"' always;
add_header Strict-Transport-Security \$console_hsts always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-Frame-Options "DENY" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header Permissions-Policy "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()" always;
EOF

# ── Runtime config for the SPA ────────────────────────────────────────────
# The value was checked against the safe character set above, so it is a
# valid JSON string literal as written.
cat >/usr/share/nginx/html/config.js <<EOF
window.__APP_CONFIG__ = {
  VITE_API_BASE_URL: "${API_BASE_URL}"
};
EOF

exec nginx -g 'daemon off;'
