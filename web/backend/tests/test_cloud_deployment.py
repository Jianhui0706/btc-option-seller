from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "deploy" / "cloud"
DOMAIN = "btc-option-seller-43-160-202-151.sslip.io"


def test_caddy_site_isolated_from_existing_projects_and_protects_the_dashboard():
    source = (DEPLOY / "btc-option-seller.caddy").read_text(encoding="utf-8")

    assert DOMAIN in source
    assert "reverse_proxy 127.0.0.1:8310" in source
    assert "path /webhook/tradingview" in source
    assert "remote_ip 120.19.167.133" in source
    assert 'respond "Forbidden" 403' in source


def test_systemd_service_runs_only_on_the_private_application_port():
    source = (DEPLOY / "btc-option-seller.service").read_text(encoding="utf-8")

    assert "WorkingDirectory=/opt/btc-option-seller/web/backend" in source
    assert "server.py --host 127.0.0.1 --port 8310" in source
    assert "Restart=always" in source
    assert "User=ubuntu" in source


def test_installer_preserves_and_validates_the_existing_caddy_configuration():
    source = (DEPLOY / "install.sh").read_text(encoding="utf-8")

    assert 'CADDY_MAIN="/etc/caddy/Caddyfile"' in source
    assert 'cp -a "$CADDY_MAIN"' in source
    assert 'caddy validate --config "$CADDY_MAIN"' in source
    assert "systemctl reload caddy" in source
    assert "import /etc/caddy/sites/*.caddy" in source
    assert "cat > /etc/caddy/Caddyfile" not in source
    assert "DERIBIT_ENV=testnet" in source


def test_installer_accepts_utf8_bom_and_windows_line_endings_in_env_file():
    source = (DEPLOY / "install.sh").read_text(encoding="utf-8")

    assert "read_env_value" in source
    assert "1s/^\\xEF\\xBB\\xBF//" in source
    assert "s/\\r$//" in source
    assert '"$(read_env_value DERIBIT_ENV)" != "testnet"' in source


def test_installer_accepts_dashboard_403_for_its_server_side_https_probe():
    source = (DEPLOY / "install.sh").read_text(encoding="utf-8")

    assert "public_status=" in source
    assert '"$public_status" != "403"' in source
    assert '"$public_status" != "200"' in source
    assert 'curl --fail --silent --show-error --retry 12' not in source


def test_admin_ip_updater_changes_only_this_projects_caddy_site():
    source = (DEPLOY / "update-admin-ip.sh").read_text(encoding="utf-8")

    assert "/etc/caddy/sites/btc-option-seller.caddy" in source
    assert 'CADDY_MAIN="/etc/caddy/Caddyfile"' in source
    assert 'caddy validate --config "$CADDY_MAIN"' in source
    assert "systemctl reload caddy" in source


def test_package_builder_excludes_runtime_state_and_requires_local_secrets():
    source = (ROOT / "deploy" / "build-cloud-package.ps1").read_text(encoding="utf-8")

    assert "Join-Path $backendDir '.env'" in source
    assert "DERIBIT_TESTNET_CLIENT_ID" in source
    assert "WEBHOOK_SECRET" in source
    assert "web\\backend\\data" not in source
    assert "web\\backend\\logs" not in source
    assert "node_modules" not in source
    assert "\\\n" not in source
    assert "LiteralPath (Join-Path $projectRoot 'deploy\\cloud\\*')" not in source
    assert "UTF8Encoding]::new($false)" in source
    assert '.Replace("`r`n", "`n")' in source
