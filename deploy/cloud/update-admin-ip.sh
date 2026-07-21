#!/usr/bin/env bash
set -Eeuo pipefail

SITE="/etc/caddy/sites/btc-option-seller.caddy"
CADDY_MAIN="/etc/caddy/Caddyfile"
NEW_IP="${1:-}"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run with sudo and provide the new public IPv4 address." >&2
    exit 1
fi
if [[ ! "$NEW_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    echo "Invalid IPv4 address." >&2
    exit 1
fi
IFS='.' read -r a b c d <<< "$NEW_IP"
for octet in "$a" "$b" "$c" "$d"; do
    if (( octet < 0 || octet > 255 )); then
        echo "Invalid IPv4 address." >&2
        exit 1
    fi
done
[[ -f "$SITE" ]] || {
    echo "Site configuration not found: $SITE" >&2
    exit 1
}

backup="${SITE}.backup-$(date -u +%Y%m%d-%H%M%S)"
cp -a "$SITE" "$backup"
sed -E -i "s/(remote_ip )[0-9]+(\.[0-9]+){3}/\1${NEW_IP}/" "$SITE"
if ! caddy validate --config "$CADDY_MAIN"; then
    cp -a "$backup" "$SITE"
    echo "Caddy validation failed; the previous IP was restored." >&2
    exit 1
fi
systemctl reload caddy
echo "Dashboard access updated for ${NEW_IP}."
