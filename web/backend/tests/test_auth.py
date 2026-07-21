from pathlib import Path
from types import SimpleNamespace

from src.api import _auth


def _request():
    ctx = SimpleNamespace()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(ctx=ctx)),
        headers={},
    )
    return request, ctx


def test_dashboard_requests_do_not_require_an_access_token():
    request, ctx = _request()

    assert _auth(request) is ctx


def test_frontend_does_not_prompt_for_or_send_a_dashboard_token():
    backend_dir = Path(__file__).resolve().parents[1]
    pages = [
        backend_dir.parent / "frontend" / "index.html",
        backend_dir / "static" / "index.html",
    ]

    for page in pages:
        source = page.read_text(encoding="utf-8")
        assert "管理访问口令" not in source
        assert "X-Auth" not in source
        assert 'localStorage.getItem("xauth")' not in source
