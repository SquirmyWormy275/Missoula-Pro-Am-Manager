#!/usr/bin/env bash
# verify_security_headers.sh — post-deploy sanity check for the CSO security
# fixes (PR #10). Run after Railway has deployed main:
#
#   bash scripts/verify_security_headers.sh
#
# Or override the host:
#
#   PROAM_URL=https://staging.example.com bash scripts/verify_security_headers.sh
#
# Exit 0 = all checks passed. Exit 1 = at least one check failed.
set -u

URL="${PROAM_URL:-https://missoula-pro-am-manager-production.up.railway.app}"

# Color helpers (no-op when not a TTY).
if [ -t 1 ]; then
    G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; N=$'\033[0m'
else
    G=''; R=''; Y=''; N=''
fi

pass=0
fail=0

check() {
    local label="$1"
    local cmd="$2"
    if eval "$cmd" >/dev/null 2>&1; then
        echo "  ${G}PASS${N}  $label"
        pass=$((pass + 1))
    else
        echo "  ${R}FAIL${N}  $label"
        fail=$((fail + 1))
    fi
}

echo "Verifying security headers and routes at: $URL"
echo

echo "[1/4] HTTP availability"
HEADERS=$(curl -sI "$URL/auth/login" 2>&1)
check "/auth/login returns 200" "echo \"\$HEADERS\" | head -1 | grep -q '200'"
check "/sw.js returns 200"      "curl -sI '$URL/sw.js' | head -1 | grep -q '200'"
check "/static/js/csp_handlers.js returns 200" "curl -sI '$URL/static/js/csp_handlers.js' | head -1 | grep -q '200'"
echo

echo "[2/4] Security headers"
check "X-Content-Type-Options: nosniff"      "echo \"\$HEADERS\" | grep -qi 'X-Content-Type-Options: nosniff'"
check "X-Frame-Options: SAMEORIGIN"          "echo \"\$HEADERS\" | grep -qi 'X-Frame-Options: SAMEORIGIN'"
check "Referrer-Policy: strict-origin-when-cross-origin" "echo \"\$HEADERS\" | grep -qi 'Referrer-Policy: strict-origin-when-cross-origin'"
check "Strict-Transport-Security present"    "echo \"\$HEADERS\" | grep -qi 'Strict-Transport-Security'"
check "Content-Security-Policy present"      "echo \"\$HEADERS\" | grep -qi 'Content-Security-Policy'"
echo

echo "[3/4] CSP nonce migration (Finding #7)"
CSP=$(echo "$HEADERS" | grep -i 'Content-Security-Policy:' | head -1)
check "CSP carries a nonce-XXX in script-src"      "echo \"\$CSP\" | grep -q \"script-src.*'nonce-\""
check "CSP DOES NOT have 'unsafe-inline' in script-src" "! echo \"\$CSP\" | grep -oE \"script-src[^;]*\" | grep -q \"'unsafe-inline'\""
check "CSP keeps 'self' in script-src"             "echo \"\$CSP\" | grep -q \"script-src.*'self'\""
check "CSP has frame-ancestors 'none'"             "echo \"\$CSP\" | grep -q \"frame-ancestors 'none'\""

# Pull a real page and confirm inline scripts get the nonce stamped.
LOGIN_HTML=$(curl -s "$URL/auth/login" 2>&1)
check "Inline <script> tags carry nonce= attribute"   "echo \"\$LOGIN_HTML\" | grep -q '<script nonce='"
check "Inline <style> tags carry nonce= attribute"    "echo \"\$LOGIN_HTML\" | grep -q '<style nonce='"
check "csp_handlers.js loaded from base.html"         "echo \"\$LOGIN_HTML\" | grep -q 'csp_handlers.js'"
echo

echo "[4/4] Auth + IDOR + share-token gates"
check "/woodboss/1/share without token returns 403"  "[ \"\$(curl -s -o /dev/null -w '%{http_code}' '$URL/woodboss/1/share')\" = '403' ]"
check "/scoring/api/replay-token without auth is gated (302/401)" "code=\$(curl -s -o /dev/null -w '%{http_code}' '$URL/scoring/api/replay-token'); [ \"\$code\" = '302' ] || [ \"\$code\" = '401' ]"
check "/portal/competitor/1/pro/1/my-results redirects (no open IDOR)" "code=\$(curl -s -o /dev/null -w '%{http_code}' '$URL/portal/competitor/1/pro/1/my-results'); [ \"\$code\" = '302' ] || [ \"\$code\" = '404' ]"
echo

echo "================================================================"
echo "  Passed: ${G}$pass${N}    Failed: ${R}$fail${N}"
echo "================================================================"

if [ "$fail" -gt 0 ]; then
    echo
    echo "${Y}One or more checks failed. Investigate before proceeding.${N}"
    echo
    echo "For deeper inspection, dump headers manually:"
    echo "  curl -sI $URL/auth/login"
    exit 1
fi

echo
echo "${G}All security checks passed.${N}"
exit 0
