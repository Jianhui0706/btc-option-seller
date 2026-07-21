#!/usr/bin/env bash
set -Eeuo pipefail

DOMAIN="btc-option-seller-43-160-202-151.sslip.io"
APP_ROOT="/opt/btc-option-seller"
APP_PORT="8310"
SERVICE_NAME="btc-option-seller"
CADDY_MAIN="/etc/caddy/Caddyfile"
CADDY_SITE_DIR="/etc/caddy/sites"
CADDY_SITE="$CADDY_SITE_DIR/btc-option-seller.caddy"
IMPORT_LINE="import /etc/caddy/sites/*.caddy"
SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND_SOURCE="$SOURCE_ROOT/web/backend"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run with sudo: sudo bash deploy/cloud/install.sh" >&2
    exit 1
fi

if ! id ubuntu >/dev/null 2>&1; then
    echo "Required service user 'ubuntu' does not exist." >&2
    exit 1
fi

for required in \
    "$BACKEND_SOURCE/server.py" \
    "$BACKEND_SOURCE/requirements.txt" \
    "$BACKEND_SOURCE/.env" \
    "$SOURCE_ROOT/deploy/cloud/btc-option-seller.service" \
    "$SOURCE_ROOT/deploy/cloud/btc-option-seller.caddy"; do
    if [[ ! -e "$required" ]]; then
        echo "Missing deployment file: $required" >&2
        exit 1
    fi
done

read_env_value() {
    local key="$1"
    sed -e '1s/^\xEF\xBB\xBF//' -e 's/\r$//' "$BACKEND_SOURCE/.env" |
        awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}'
}

if [[ "$(read_env_value DERIBIT_ENV)" != "testnet" ]]; then
    echo "Cloud deployment is locked to DERIBIT_ENV=testnet." >&2
    exit 1
fi
if [[ -z "$(read_env_value DERIBIT_TESTNET_CLIENT_ID)" ]]; then
    echo "DERIBIT_TESTNET_CLIENT_ID is not configured." >&2
    exit 1
fi
if [[ -z "$(read_env_value DERIBIT_TESTNET_CLIENT_SECRET)" ]]; then
    echo "DERIBIT_TESTNET_CLIENT_SECRET is not configured." >&2
    exit 1
fi
if [[ -z "$(read_env_value WEBHOOK_PASSPHRASE)" && -z "$(read_env_value WEBHOOK_SECRET)" ]]; then
    echo "A TradingView webhook passphrase is not configured." >&2
    exit 1
fi

command -v caddy >/dev/null 2>&1 || {
    echo "Caddy is required because this server already uses it." >&2
    exit 1
}
[[ -f "$CADDY_MAIN" ]] || {
    echo "Existing Caddyfile was not found at $CADDY_MAIN." >&2
    exit 1
}

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip curl

timestamp="$(date -u +%Y%m%d-%H%M%S)"
# 先停服务再挪目录：旧进程仍持有 SQLite 的 WAL 句柄，
# 边写边拷会拿到不一致的快照。
if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    systemctl stop "${SERVICE_NAME}.service"
fi
PREVIOUS_ROOT=""
if [[ -d "$APP_ROOT" ]]; then
    PREVIOUS_ROOT="${APP_ROOT}.backup-${timestamp}"
    mv "$APP_ROOT" "$PREVIOUS_ROOT"
fi

install -d -m 0750 -o ubuntu -g ubuntu "$APP_ROOT/web/backend"
install -d -m 0755 -o ubuntu -g ubuntu "$APP_ROOT/deploy/cloud"
cp -a "$BACKEND_SOURCE/." "$APP_ROOT/web/backend/"
cp -a "$SOURCE_ROOT/deploy/cloud/." "$APP_ROOT/deploy/cloud/"
install -d -m 0750 -o ubuntu -g ubuntu \
    "$APP_ROOT/web/backend/data" "$APP_ROOT/web/backend/logs"
# 部署包不含 data/，不把上一版的库搬回来的话，
# 每次部署都会静默清空策略数据库（持仓归属、子组、事件历史全丢）。
if [[ -n "$PREVIOUS_ROOT" && -d "$PREVIOUS_ROOT/web/backend/data" ]]; then
    cp -a "$PREVIOUS_ROOT/web/backend/data/." "$APP_ROOT/web/backend/data/"
    echo "Carried over strategy database from $PREVIOUS_ROOT"
fi
chown -R ubuntu:ubuntu "$APP_ROOT"
chmod 0600 "$APP_ROOT/web/backend/.env"

python3 -m venv "$APP_ROOT/.venv"
"$APP_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$APP_ROOT/.venv/bin/python" -m pip install -r "$APP_ROOT/web/backend/requirements.txt"
chown -R ubuntu:ubuntu "$APP_ROOT/.venv"

install -m 0644 "$SOURCE_ROOT/deploy/cloud/btc-option-seller.service" \
    "/etc/systemd/system/${SERVICE_NAME}.service"

install -d -m 0755 "$CADDY_SITE_DIR"
caddy_backup="${CADDY_MAIN}.backup-${timestamp}"
cp -a "$CADDY_MAIN" "$caddy_backup"
# 面板白名单 IP 是运行期状态（由 update-admin-ip.sh 维护），不是部署产物。
# 直接拿仓库模板覆盖会把它重置成模板里硬编码的旧 IP，等于每次部署都
# 可能把管理员锁在面板外面（webhook 路径不受影响，所以问题不会立刻暴露）。
existing_admin_ip=""
if [[ -f "$CADDY_SITE" ]]; then
    existing_admin_ip="$(sed -nE 's/.*remote_ip ([0-9]+(\.[0-9]+){3}).*/\1/p' "$CADDY_SITE" | head -1)"
fi
install -m 0644 "$SOURCE_ROOT/deploy/cloud/btc-option-seller.caddy" "$CADDY_SITE"
if [[ -n "$existing_admin_ip" ]]; then
    sed -E -i "s/(remote_ip )[0-9]+(\.[0-9]+){3}/\1${existing_admin_ip}/" "$CADDY_SITE"
    echo "Preserved dashboard allowlist IP ${existing_admin_ip}"
fi
if ! grep -Fqx "$IMPORT_LINE" "$CADDY_MAIN"; then
    printf '\n%s\n' "$IMPORT_LINE" >> "$CADDY_MAIN"
fi

if ! caddy validate --config "$CADDY_MAIN"; then
    cp -a "$caddy_backup" "$CADDY_MAIN"
    rm -f "$CADDY_SITE"
    echo "Caddy validation failed; the previous configuration was restored." >&2
    exit 1
fi

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"

for _ in {1..30}; do
    if curl -fsS "http://127.0.0.1:${APP_PORT}/status" >/dev/null; then
        break
    fi
    sleep 1
done
curl -fsS "http://127.0.0.1:${APP_PORT}/status" >/dev/null

systemctl reload caddy
public_status="$(curl --silent --show-error --retry 12 --retry-delay 5 \
    --retry-all-errors --output /dev/null --write-out '%{http_code}' \
    "https://${DOMAIN}/status")"
if [[ "$public_status" != "403" && "$public_status" != "200" ]]; then
    echo "Unexpected public HTTPS status: ${public_status}" >&2
    exit 1
fi

echo "Deployment complete: https://${DOMAIN}"
echo "Webhook URL: https://${DOMAIN}/webhook/tradingview"
