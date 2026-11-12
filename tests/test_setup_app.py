"""Unit tests for GitHub App setup wizard."""

from unittest.mock import MagicMock, patch
import pytest

from src.setup_app import get_manifest, ManifestCallbackHandler, resolve_api_base_url


def test_resolve_api_base_url_domains() -> None:
    """Test REST API base URL resolution across GitHub.com, ghe.com, and GHES."""
    assert resolve_api_base_url("https://github.com") == "https://api.github.com"
    assert resolve_api_base_url("github.com") == "https://api.github.com"
    # ghe.com data residency
    assert resolve_api_base_url("https://octocorp.ghe.com") == "https://api.octocorp.ghe.com"
    assert resolve_api_base_url("octocorp.ghe.com") == "https://api.octocorp.ghe.com"
    # GHES on-premises
    assert resolve_api_base_url("https://ghe.internal.net") == "https://ghe.internal.net/api/v3"
    assert resolve_api_base_url("ghe.internal.net") == "https://ghe.internal.net/api/v3"


def test_get_manifest_payload() -> None:
    """Verify GitHub App manifest contains clean URLs and required enterprise read permissions."""
    manifest = get_manifest("mycorp.ghe.com/", state_nonce="ignored-nonce")
    assert manifest["name"] == "Repo360"
    assert manifest["url"] == "https://mycorp.ghe.com"
    assert manifest["hook_attributes"]["url"] == "https://mycorp.ghe.com"
    assert manifest["hook_attributes"]["active"] is False
    # redirect_url must stay clean without embedded ?state=... query parameters
    assert manifest["redirect_url"] == "http://127.0.0.1:8989/callback"
    assert "?" not in manifest["redirect_url"]
    assert manifest["default_permissions"]["organization_administration"] == "read"
    assert manifest["default_permissions"]["administration"] == "read"
    assert manifest["default_permissions"]["actions"] == "read"
    assert manifest["default_permissions"]["contents"] == "read"
    assert manifest["default_permissions"]["deployments"] == "read"
    assert manifest["default_permissions"]["pull_requests"] == "read"
    assert manifest["default_permissions"]["issues"] == "read"
    assert manifest["default_permissions"]["members"] == "read"
    assert manifest["default_permissions"]["organization_self_hosted_runners"] == "read"


def test_app_name_limit_and_domain_quotes() -> None:
    """Verify GitHub App default name obeys 34-char cap and quoted domains normalize cleanly."""
    from src.setup_app import _build_default_app_name, _normalize_github_domain

    assert _build_default_app_name("") == "Repo360"
    long_name = _build_default_app_name("very-long-enterprise-organization-name-exceeding-limit")
    assert len(long_name) <= 34
    assert long_name.startswith("Repo360")
    assert _normalize_github_domain('  "https://mycorp.ghe.com/"  ') == "https://mycorp.ghe.com"
    assert _normalize_github_domain("  'github.com/'  ") == "https://github.com"


def test_manifest_callback_handler_redirect_form() -> None:
    """Test callback handler GET / sends CSRF state on form action and keeps redirect_url clean."""
    import html
    import json
    import re

    handler = ManifestCallbackHandler.__new__(ManifestCallbackHandler)
    handler.path = "/"
    handler.target_org = "test-org"
    handler.github_domain = "https://github.com"
    handler.api_domain = "https://api.github.com"
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    ManifestCallbackHandler.session_nonce = "nonce_test_value"

    handler.do_GET()

    handler.send_response.assert_called_with(200)
    handler.send_header.assert_any_call("Cache-Control", "no-store, no-cache, must-revalidate")
    handler.send_header.assert_any_call("Pragma", "no-cache")
    handler.send_header.assert_called_with("Content-Type", "text/html; charset=utf-8")
    assert handler.wfile.write.called
    written_html = handler.wfile.write.call_args[0][0].decode("utf-8")
    assert 'action="https://github.com/organizations/test-org/settings/apps/new?state=nonce_test_value"' in written_html

    match = re.search(r"name=\"manifest\"\s+value='([^']+)'", written_html)
    assert match is not None
    payload = json.loads(html.unescape(match.group(1)))
    assert payload["redirect_url"] == "http://127.0.0.1:8989/callback"


def test_manifest_callback_handler_validates_state_and_exchanges_code() -> None:
    """Verify /callback rejects mismatched state and exchanges valid code+state via GitHub API."""
    handler = ManifestCallbackHandler.__new__(ManifestCallbackHandler)
    handler.target_org = "test-org"
    handler.github_domain = "https://github.com"
    handler.api_domain = "https://api.github.com"
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    ManifestCallbackHandler.session_nonce = "expected_nonce"
    ManifestCallbackHandler.conversion_result = None

    # Mismatched state must return 403
    handler.path = "/callback?code=temp_code&state=wrong_nonce"
    handler.do_GET()
    handler.send_response.assert_called_with(403)

    # Matching state exchanges code via POST /app-manifests/{code}/conversions
    handler.path = "/callback?code=temp_code&state=expected_nonce"
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"id": 12345, "slug": "repo360-test-org", "pem": "PEM"}
    with patch("src.setup_app.requests.post", return_value=mock_resp) as mock_post:
        handler.do_GET()
        mock_post.assert_called_once()
        assert mock_post.call_args[0][0] == "https://api.github.com/app-manifests/temp_code/conversions"
    handler.send_response.assert_called_with(200)
    assert ManifestCallbackHandler.conversion_result == {"id": 12345, "slug": "repo360-test-org", "pem": "PEM"}


def test_setup_app_main_keyboard_interrupt(capsys) -> None:
    """Verify that Ctrl+C in setup_app exits silently with code 130 and no extra text."""
    from src.common.interrupts import SHUTDOWN_EVENT, reset_shutdown
    from src.setup_app import main as setup_main

    with patch("argparse.ArgumentParser.parse_args") as mock_args:
        mock_args.return_value = MagicMock(org="my-org", app_name="", domain="https://github.com")
        with patch("src.setup_app.run_wizard", side_effect=KeyboardInterrupt):
            code = setup_main()
            assert code == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

    # Simulate Windows Ctrl+C on the interactive Enter GitHub Domain prompt (ReadConsoleW -> EOFError)
    responses = iter(["my-org"])

    def _mock_domain_ctrlc(prompt: str = "") -> str:
        try:
            return next(responses)
        except StopIteration:
            SHUTDOWN_EVENT.set()
            raise EOFError

    try:
        with patch("sys.argv", ["repo360"]):
            with patch("builtins.input", side_effect=_mock_domain_ctrlc):
                code = setup_main()
                assert code == 130
        captured_domain = capsys.readouterr()
        assert "Operation cancelled" not in captured_domain.out
        assert "EOFError" not in captured_domain.err
    finally:
        reset_shutdown()


def test_get_manifest_custom_and_invalid_redirect_url() -> None:
    """Verify custom loopback callback URLs work and query params in redirect_url raise ValueError."""
    from src.setup_app import _normalize_org_name

    manifest = get_manifest("https://github.com", redirect_url="http://127.0.0.1:54321/callback")
    assert manifest["redirect_url"] == "http://127.0.0.1:54321/callback"

    with pytest.raises(ValueError, match="query parameters"):
        get_manifest("https://github.com", redirect_url="http://127.0.0.1:8989/callback?state=bad")

    with pytest.raises(ValueError, match="Invalid GitHub App redirect_url"):
        get_manifest("https://github.com", redirect_url="not-a-url")

    assert _normalize_org_name("  @fintech-core/ ") == "fintech-core"
    assert _normalize_org_name("https://github.com/organizations/my-org/") == "my-org"
    assert _normalize_org_name("https://github.com/my-org/") == "my-org"


def test_manifest_callback_handler_dynamic_port_and_oauth_error() -> None:
    """Verify GET / uses dynamic callback_url and /callback renders escaped OAuth error pages."""
    import html
    import json
    import re

    handler = ManifestCallbackHandler.__new__(ManifestCallbackHandler)
    handler.path = "/?s=abcd1234"
    handler.target_org = "test-org"
    handler.github_domain = "https://github.com"
    handler.api_domain = "https://api.github.com"
    handler.callback_url = "http://127.0.0.1:54321/callback"
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    ManifestCallbackHandler.session_nonce = "nonce_dynamic"

    handler.do_GET()
    written_html = handler.wfile.write.call_args[0][0].decode("utf-8")
    match = re.search(r"name=\"manifest\"\s+value='([^']+)'", written_html)
    assert match is not None
    payload = json.loads(html.unescape(match.group(1)))
    assert payload["redirect_url"] == "http://127.0.0.1:54321/callback"

    handler.path = "/callback?error=access_denied&error_description=User+%3Ccancelled%3E"
    handler.wfile.reset_mock()
    handler.do_GET()
    handler.send_response.assert_called_with(400)
    err_html = handler.wfile.write.call_args[0][0].decode("utf-8")
    assert "access_denied: User &lt;cancelled&gt;" in err_html


def test_bind_callback_server_fallback_on_occupied_port() -> None:
    """Verify _bind_callback_server falls back to an ephemeral port when preferred port is occupied."""
    import socket
    from src.setup_app import _bind_callback_server

    occupant = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupant.bind(("127.0.0.1", 0))
    occupant.listen(1)
    occupied_port = occupant.getsockname()[1]

    fallback_server = None
    try:
        fallback_server, bound_port = _bind_callback_server("127.0.0.1", occupied_port)
        assert bound_port != occupied_port
        assert bound_port > 0
    finally:
        if fallback_server is not None:
            fallback_server.server_close()
        occupant.close()
