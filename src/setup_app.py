"""Automated 1-Click GitHub App Registration Wizard for Repo360.

Uses GitHub's official App Manifest flow:
1. Prompts interactively for target Organization and parameters.
2. Spins up a temporary local HTTP server on http://127.0.0.1:8989
3. Opens browser to GitHub with pre-configured read-only governance permissions
4. Exchanges authorization code via POST /app-manifests/{code}/conversions
5. Automatically saves the RSA private key (.pem) to user config (~/.repo360/secrets/)
6. Opens GitHub App installation page and automatically fetches Installation ID
7. Prompts user to select AI Remediation Provider and enter API key
8. Persists all credentials securely to ~/.repo360/config.json (Zero manual setup required!)
"""

from __future__ import annotations

import argparse
import html
import http.server
import json
import logging
import os
import secrets
import socket
import socketserver
import sys
import time
import urllib.parse
import webbrowser
from typing import Optional
import requests

from .common.interrupts import (
    finalize_interrupt_exit,
    install_signal_handlers,
    interruptible_input,
    interruptible_sleep,
    is_shutting_down,
    register_closable,
    unregister_closable,
)
install_signal_handlers()

from .auth.github_app_auth import GitHubAppAuth
from .credentials import (
    get_credentials_path,
    get_secrets_dir,
    load_user_credentials,
    save_private_key,
    save_user_credentials,
)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("setup-wizard")

LOOPBACK_HOST = "127.0.0.1"
PORT = 8989
CALLBACK_URL = f"http://{LOOPBACK_HOST}:{PORT}/callback"


def _normalize_github_domain(domain: str) -> str:
    """Normalize a GitHub base URL to scheme + host without trailing slashes."""
    cleaned = (domain or "https://github.com").strip().strip("\"'").rstrip("/")
    if not cleaned.startswith(("http://", "https://")):
        cleaned = f"https://{cleaned}"
    parsed = urllib.parse.urlparse(cleaned)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else cleaned


def _normalize_org_name(org_name: str) -> str:
    """Normalize a GitHub organization slug, handling stray quotes, @ prefixes, or pasted URLs."""
    cleaned = (org_name or "").strip().strip("\"'").strip("/")
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    if "://" in cleaned or cleaned.lower().startswith("github.com/"):
        candidate = cleaned if "://" in cleaned else f"https://{cleaned}"
        parsed = urllib.parse.urlparse(candidate)
        segments = [seg for seg in parsed.path.split("/") if seg]
        if segments:
            if segments[0].lower() == "organizations" and len(segments) >= 2:
                return segments[1]
            return segments[0]
    return cleaned


def _validate_callback_url(callback_url: str) -> str:
    """Ensure the manifest redirect_url is a clean HTTP(S) URL without query params or fragments."""
    cleaned = (callback_url or "").strip()
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Invalid GitHub App redirect_url: {callback_url!r}")
    if parsed.query or parsed.fragment:
        raise ValueError(
            "GitHub App manifest redirect_url must not contain query parameters or fragments; "
            "pass CSRF state on the form action URL instead."
        )
    return cleaned


def _build_default_app_name(org_name: str) -> str:
    """Build a unique GitHub App name respecting GitHub's 34-character limit."""
    clean_org = _normalize_org_name(org_name)
    if not clean_org:
        return "Repo360"
    candidate = f"Repo360-{clean_org}"
    return candidate[:34].rstrip("-") or "Repo360"


DEFAULT_GITHUB_APP_PERMISSIONS: dict[str, str] = {
    "actions": "read",
    "administration": "read",
    "contents": "read",
    "deployments": "read",
    "issues": "read",
    "members": "read",
    "metadata": "read",
    "organization_administration": "read",
    "organization_self_hosted_runners": "read",
    "packages": "read",
    "pull_requests": "read",
    "secret_scanning_alerts": "read",
}


def get_manifest(
    github_domain: str,
    app_name: str = "Repo360",
    state_nonce: str = "",
    redirect_url: str = "",
) -> dict:
    """Generate the GitHub App manifest payload.

    GitHub's manifest validator requires `redirect_url` to be a clean callback URL
    without query parameters; CSRF `state` must be passed via the form action query string.
    """
    _ = state_nonce  # State is embedded in form action URL; param kept for compatibility.
    clean_domain = _normalize_github_domain(github_domain)
    clean_redirect = _validate_callback_url(redirect_url or CALLBACK_URL)
    return {
        "name": app_name or "Repo360",
        "url": clean_domain,
        "description": "360-Degree Platform Governance, Compliance & Engineering Intelligence for GitHub",
        "hook_attributes": {
            "url": clean_domain,
            "active": False,
        },
        "redirect_url": clean_redirect,
        "default_permissions": dict(DEFAULT_GITHUB_APP_PERMISSIONS),
        "public": False,
    }


def _render_error_page(title: str, detail: str, hint: str) -> str:
    """Render an escaped dark-themed error response page for local wizard failures."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{html.escape(title)} - Repo360</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      background-color: #0d1117;
      color: #c9d1d9;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      padding: 24px;
    }}
    .card {{
      background-color: #161b22;
      border: 1px solid #f85149;
      border-radius: 12px;
      max-width: 520px;
      width: 100%;
      padding: 40px 32px;
      text-align: center;
    }}
    h1 {{ color: #f85149; font-size: 24px; margin-bottom: 12px; }}
    pre {{
      background: #0d1117;
      border: 1px solid #30363d;
      border-radius: 6px;
      padding: 12px;
      font-size: 13px;
      color: #ff7b72;
      text-align: left;
      overflow-x: auto;
      white-space: pre-wrap;
      margin: 16px 0;
    }}
    p {{ color: #8b949e; font-size: 14px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{html.escape(title)}</h1>
    <pre>{html.escape(detail)}</pre>
    <p>{html.escape(hint)}</p>
  </div>
</body>
</html>"""


class ManifestCallbackHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler to serve the initial auto-redirect and receive the GitHub callback."""

    target_org: str = ""
    app_name: str = ""
    github_domain: str = "https://github.com"
    api_domain: str = "https://api.github.com"
    callback_url: str = CALLBACK_URL
    conversion_result: dict | None = None
    session_nonce: str = secrets.token_urlsafe(32)

    def log_message(self, fmt: str, *args) -> None:
        """Suppress standard HTTP server access logs."""
        pass

    def do_GET(self) -> None:
        """Handle initial browser landing and GitHub callback redirect."""
        parsed_url = urllib.parse.urlparse(self.path)

        if parsed_url.path in ("", "/"):
            clean_domain = _normalize_github_domain(self.github_domain)
            # Scope default name to target_org to avoid global GitHub App collisions
            clean_org = _normalize_org_name(self.target_org)
            default_name = _build_default_app_name(clean_org)
            active_name = self.app_name or default_name
            active_callback = self.callback_url or CALLBACK_URL
            manifest_payload = get_manifest(
                clean_domain,
                app_name=active_name,
                redirect_url=active_callback,
            )
            manifest_json = json.dumps(manifest_payload)
            logger.debug(
                "Serving manifest form action for org='%s' with redirect_url='%s'",
                clean_org,
                manifest_payload.get("redirect_url"),
            )

            encoded_org = urllib.parse.quote(clean_org, safe="") if clean_org else ""
            base_action = (
                f"{clean_domain}/organizations/{encoded_org}/settings/apps/new"
                if encoded_org
                else f"{clean_domain}/settings/apps/new"
            )
            active_nonce = ManifestCallbackHandler.session_nonce
            form_action = (
                f"{base_action}?{urllib.parse.urlencode({'state': active_nonce})}"
                if active_nonce
                else base_action
            )

            html_page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Connecting to GitHub - Repo360</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      background-color: #0d1117;
      color: #c9d1d9;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 24px;
    }}
    .card {{
      background-color: #161b22;
      border: 1px solid #30363d;
      border-radius: 12px;
      max-width: 520px;
      width: 100%;
      padding: 44px 32px;
      text-align: center;
      box-shadow: 0 12px 32px rgba(1, 4, 9, 0.6);
    }}
    .spinner {{
      width: 44px;
      height: 44px;
      border: 3px solid rgba(88, 166, 255, 0.15);
      border-top-color: #58a6ff;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      margin: 0 auto 24px auto;
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    h1 {{
      color: #3fb950;
      font-size: 28px;
      font-weight: 700;
      letter-spacing: -0.3px;
      margin-bottom: 12px;
    }}
    p.desc {{
      color: #c9d1d9;
      font-size: 16px;
      line-height: 1.5;
      margin-bottom: 8px;
    }}
    p.hint {{
      color: #8b949e;
      font-size: 14px;
      margin-bottom: 24px;
    }}
    .btn {{
      display: inline-block;
      background-color: #238636;
      color: #ffffff;
      border: 1px solid rgba(240, 246, 252, 0.1);
      border-radius: 6px;
      padding: 10px 24px;
      font-size: 15px;
      font-weight: 600;
      text-decoration: none;
      cursor: pointer;
      transition: background-color 0.2s cubic-bezier(0.3, 0, 0.5, 1);
    }}
    .btn:hover {{
      background-color: #2ea043;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="spinner"></div>
    <h1>Connecting to GitHub...</h1>
    <p class="desc">Redirecting to authorize and register the Repo360 GitHub App.</p>
    <p class="hint">Click the button below if you are not redirected automatically.</p>
    <form id="manifestForm" method="post" action="{html.escape(form_action)}">
      <input type="hidden" name="manifest" value='{html.escape(manifest_json)}'>
      <button type="submit" class="btn">Continue to GitHub</button>
    </form>
  </div>
  <script>document.getElementById('manifestForm').submit();</script>
</body>
</html>"""

            self.send_response(200)
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_page.encode("utf-8"))
            return

        elif parsed_url.path == "/callback":
            query_params = urllib.parse.parse_qs(parsed_url.query)
            state = query_params.get("state", [""])[0]
            code = query_params.get("code", [""])[0]
            oauth_error = query_params.get("error", [""])[0]
            oauth_error_desc = query_params.get("error_description", [""])[0]

            if not code:
                err_detail = (
                    f"{oauth_error}: {oauth_error_desc}".strip(": ")
                    if (oauth_error or oauth_error_desc)
                    else "Missing authorization code in GitHub callback."
                )
                error_html = _render_error_page(
                    "GitHub App Authorization Failed",
                    err_detail,
                    "Please return to your terminal and retry 'repo360 setup'.",
                )
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(error_html.encode("utf-8"))
                return

            if ManifestCallbackHandler.session_nonce and not secrets.compare_digest(state, ManifestCallbackHandler.session_nonce):
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Security Error: Invalid or expired state nonce.")
                return

            conversion_endpoint = f"{self.api_domain}/app-manifests/{code}/conversions"
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }

            try:
                resp = requests.post(conversion_endpoint, headers=headers, timeout=15)
                resp.raise_for_status()
                ManifestCallbackHandler.conversion_result = resp.json()

                success_html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>GitHub App Created - Repo360</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      background-color: #0d1117;
      color: #c9d1d9;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 24px;
    }
    .card {
      background-color: #161b22;
      border: 1px solid #30363d;
      border-radius: 12px;
      max-width: 520px;
      width: 100%;
      padding: 44px 32px;
      text-align: center;
      box-shadow: 0 12px 32px rgba(1, 4, 9, 0.6);
    }
    .icon {
      width: 48px;
      height: 48px;
      margin: 0 auto 20px auto;
      color: #3fb950;
    }
    h1 {
      color: #3fb950;
      font-size: 28px;
      font-weight: 700;
      letter-spacing: -0.3px;
      margin-bottom: 12px;
    }
    p.desc {
      color: #c9d1d9;
      font-size: 17px;
      line-height: 1.5;
      margin-bottom: 8px;
    }
    p.hint {
      color: #8b949e;
      font-size: 14px;
      margin-top: 16px;
    }
  </style>
</head>
<body>
  <div class="card">
    <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path>
      <polyline points="22 4 12 14.01 9 11.01"></polyline>
    </svg>
    <h1>GitHub App Created Successfully!</h1>
    <p class="desc">Your private key has been saved automatically.</p>
    <p class="hint">You can now close this browser tab and return to your terminal.</p>
  </div>
</body>
</html>"""

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(success_html.encode("utf-8"))

            except Exception as err:
                logger.error("Failed to convert manifest code: %s", err)
                error_html = _render_error_page(
                    "App Conversion Failed",
                    str(err),
                    "Please return to your terminal to inspect the error log or retry.",
                )
                self.send_response(500)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(error_html.encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()


def resolve_api_base_url(domain: str) -> str:
    """Resolve GitHub REST API base URL across GitHub.com, ghe.com, and GHES.

    - github.com -> https://api.github.com
    - *.ghe.com (e.g. octocorp.ghe.com) -> https://api.octocorp.ghe.com
    - GHES on-premises (e.g. ghe.mycompany.com) -> https://ghe.mycompany.com/api/v3
    """
    domain = domain.strip().rstrip("/")
    if not domain.startswith("http://") and not domain.startswith("https://"):
        domain = f"https://{domain}"

    parsed = urllib.parse.urlparse(domain)
    host = parsed.netloc.lower()

    if host == "github.com" or host.endswith(".github.com"):
        return "https://api.github.com"
    elif host.endswith(".ghe.com"):
        return f"{parsed.scheme}://api.{host}"
    else:
        return f"{domain}/api/v3"


class _LoopbackTCPServer(socketserver.TCPServer):
    """Loopback TCP server using exclusive port binding on Windows to prevent port hijacking."""

    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        if sys.platform == "win32":
            exclusive_opt = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive_opt is not None:
                try:
                    self.socket.setsockopt(socket.SOL_SOCKET, exclusive_opt, 1)
                except OSError:
                    pass
        super().server_bind()


def _bind_callback_server(
    host: str = LOOPBACK_HOST,
    preferred_port: int = PORT,
) -> tuple[socketserver.TCPServer, int]:
    """Bind local callback server on preferred_port, falling back to an ephemeral port if busy."""
    try:
        server = _LoopbackTCPServer((host, preferred_port), ManifestCallbackHandler)
    except OSError:
        if preferred_port == 0:
            raise
        server = _LoopbackTCPServer((host, 0), ManifestCallbackHandler)
    server.timeout = 0.25
    return server, int(server.server_address[1])


def _open_browser_url(url: str, fallback_message: str) -> None:
    """Best-effort browser launch with clear terminal fallback on headless hosts."""
    opened = False
    try:
        opened = bool(webbrowser.open(url))
    except Exception:
        opened = False
    if not opened:
        print(f"[INFO] {fallback_message}: {url}")


def run_wizard(
    org_name: str,
    github_domain: str = "https://github.com",
    app_name: str = "",
    open_browser: bool = True,
) -> None:
    """Run the interactive setup wizard."""
    clean_org = _normalize_org_name(org_name)
    github_domain = _normalize_github_domain(github_domain)
    api_domain = resolve_api_base_url(github_domain)

    default_name = _build_default_app_name(clean_org)
    chosen_name = app_name or default_name

    ManifestCallbackHandler.target_org = clean_org
    ManifestCallbackHandler.app_name = chosen_name
    ManifestCallbackHandler.github_domain = github_domain
    ManifestCallbackHandler.api_domain = api_domain
    ManifestCallbackHandler.conversion_result = None
    ManifestCallbackHandler.session_nonce = secrets.token_urlsafe(32)

    server, bound_port = _bind_callback_server(LOOPBACK_HOST, PORT)
    active_callback_url = f"http://{LOOPBACK_HOST}:{bound_port}/callback"
    ManifestCallbackHandler.callback_url = active_callback_url
    get_manifest(github_domain, app_name=chosen_name, redirect_url=active_callback_url)
    launch_url = f"http://{LOOPBACK_HOST}:{bound_port}/?s={ManifestCallbackHandler.session_nonce[:8]}"

    print("\n" + "=" * 65)
    print("  REPO360 - 1-CLICK GITHUB APP SETUP")
    print("=" * 65)
    print(f"Target Organization: {clean_org}")
    print(f"App Name:            {chosen_name}")
    print(f"GitHub Domain:       {github_domain}")
    print(f"Callback Listener:   {active_callback_url}")
    print(f"Wizard Launch URL:   {launch_url}")
    print("-" * 65)
    if bound_port != PORT:
        print(f"[INFO] Port {PORT} was unavailable; using clean fallback port {bound_port}.")
    print("Starting local listener and opening your browser...")
    print("(If an older http://localhost:8989 tab is open in your browser, close it.)")

    # Poll with a short timeout so Ctrl+C interrupts wait promptly
    register_closable(server)
    try:
        if open_browser:
            _open_browser_url(
                launch_url,
                "Could not open browser automatically; open the Wizard Launch URL manually",
            )
        while ManifestCallbackHandler.conversion_result is None and not is_shutting_down():
            server.handle_request()
        if is_shutting_down():
            raise KeyboardInterrupt
    finally:
        unregister_closable(server)
        server.server_close()

    result = ManifestCallbackHandler.conversion_result
    if not result:
        print("Setup aborted: Could not obtain GitHub App credentials.")
        return

    app_id = result.get("id")
    app_slug = result.get("slug") or "repo360"
    pem_key = result.get("pem", "")
    install_url = f"{github_domain}/organizations/{clean_org}/settings/apps/{app_slug}/installations"

    key_path = save_private_key(pem_key, app_slug=app_slug)
    try:
        local_secrets = os.path.join(os.getcwd(), "secrets")
        if os.path.isdir(local_secrets) or os.path.isfile(".env"):
            save_private_key(pem_key, app_slug=app_slug, dest_dir=local_secrets)
    except Exception:
        pass

    print("\n" + "=" * 65)
    print("  STEP 1: GITHUB APP CREATED")
    print("=" * 65)
    print(f"  App Name:         {result.get('name')}")
    print(f"  App ID:           {app_id}")
    print(f"  Private Key:      Saved to {key_path}")
    print("-" * 65)

    print("\n" + "=" * 65)
    print("  STEP 2: INSTALL APP ON YOUR ORGANIZATION")
    print("=" * 65)
    print(f"  Installation URL: {install_url}")
    print("  Please click 'Install' and choose 'All repositories' (recommended).")
    print("-" * 65)

    if open_browser:
        _open_browser_url(
            install_url,
            "Could not open browser automatically; open the Installation URL above manually",
        )

    installation_id: Optional[int] = None
    try:
        auth = GitHubAppAuth(app_id=app_id, private_key=pem_key, api_base_url=api_domain)
        prompt_msg = (
            "Press Enter once you clicked 'Install' in the browser to auto-fetch your Installation ID\n"
            "(or enter 'm' to input manually, 's' to skip and auto-discover at runtime): "
        )
        choice = interruptible_input(prompt_msg, eof_default="").strip().lower()

        if choice == "m":
            manual_in = interruptible_input("Enter Installation ID: ", eof_default="").strip()
            if manual_in.isdigit():
                installation_id = int(manual_in)
        elif choice != "s":
            print("Querying GitHub API for active installations...")
            for _ in range(3):
                installations = auth.discover_installations()
                if installations:
                    matched = [
                        i for i in installations
                        if i.get("account", {}).get("login", "").lower() == clean_org.lower()
                    ]
                    selected = matched[0] if matched else installations[0]
                    installation_id = int(selected["id"])
                    org_login = selected.get("account", {}).get("login", clean_org)
                    print(f"-> [SUCCESS] Auto-detected Installation ID: {installation_id} for '{org_login}'!")
                    break
                if is_shutting_down():
                    raise KeyboardInterrupt
                interruptible_sleep(2, sleep_fn=time.sleep)

            if not installation_id:
                print("Could not auto-detect active installation yet.")
                manual_in = interruptible_input(
                    "Enter Installation ID manually (or press Enter to auto-discover at runtime): ",
                    eof_default="",
                ).strip()
                if manual_in.isdigit():
                    installation_id = int(manual_in)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as err:
        logger.debug("Installation discovery error: %s", err)
        manual_in = interruptible_input(
            "Enter Installation ID manually (or press Enter to auto-discover at runtime): ",
            eof_default="",
        ).strip()
        if manual_in.isdigit():
            installation_id = int(manual_in)

    llm_provider, llm_model, llm_api_key, llm_enabled = _prompt_ai_configuration()

    cfg_path = save_user_credentials(
        app_id=app_id,
        private_key_path=key_path,
        installation_id=installation_id,
        api_base_url=api_domain,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_enabled=llm_enabled,
    )

    _update_local_env_if_present(
        app_id=app_id,
        key_path=key_path,
        installation_id=installation_id,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_enabled=llm_enabled,
    )

    print("\n" + "=" * 65)
    print("  SETUP COMPLETE! ZERO CONFIGURATION REQUIRED.")
    print("=" * 65)
    print(f"  App Name:         {result.get('name')}")
    print(f"  App ID:           {app_id}")
    print(f"  Installation ID:  {installation_id or 'Auto-discovered at runtime'}")
    print(f"  Private Key:      {key_path}")
    print(f"  Configuration:    {cfg_path}")
    ai_status = f"{llm_provider.upper()} ({llm_model})" if llm_enabled else "DISABLED"
    print(f"  AI Remediation:   {ai_status}")
    print("-" * 65)
    print("  Zero manual configuration files required!")
    print("  Run your platform commands anytime from any directory:")
    print()
    print("    repo360 --help")
    print("=" * 65 + "\n")


_LLM_PROVIDER_MAP: dict[str, tuple[str, str, str]] = {
    "1": ("gemini", "gemini-3.5-flash", "GEMINI_API_KEY"),
    "2": ("openai", "gpt-4o", "OPENAI_API_KEY"),
    "3": ("anthropic", "claude-3-5-sonnet-20241022", "ANTHROPIC_API_KEY"),
}


def _prompt_choice(prompt: str, valid: tuple[str, ...], default: str, error_msg: str) -> str:
    """Prompt until a valid option is selected or default is chosen on empty Enter."""
    while True:
        raw = interruptible_input(prompt).strip().lower()
        chosen = raw or default
        if chosen in valid:
            return chosen
        print(error_msg)


def _prompt_setup_mode() -> str:
    """Prompt user to choose browser auto-configuration vs manual setup."""
    print("\nSelect GitHub App Setup Method:")
    print("  [1] Auto-configure GitHub App (1-click registration via browser)")
    print("  [2] Manual setup (enter existing app details or update config file manually)")
    choice = _prompt_choice(
        "Select option [1-2] (default: 1): ",
        valid=("1", "2", "auto", "manual", "a", "m"),
        default="1",
        error_msg="Please enter 1 (Auto-configure) or 2 (Manual setup).",
    )
    return "manual" if choice in ("2", "manual", "m") else "auto"


def _prompt_domain(default_domain: str = "https://github.com") -> str:
    """Prompt for GitHub host URL and normalize it."""
    custom_domain = interruptible_input(
        f"Enter GitHub Domain [{default_domain}] (press Enter for default, or e.g. https://<enterprise>.ghe.com or https://ghe.company.com): ",
        eof_default="",
    ).strip()
    return _normalize_github_domain(custom_domain or default_domain)


def _prompt_ai_configuration() -> tuple[str, str, str, bool]:
    """Prompt interactively for optional AI remediation provider settings."""
    print("\n" + "=" * 65)
    print("  AI REMEDIATION & GOVERNANCE ADVISORY ENGINE")
    print("=" * 65)
    print("Select your AI Remediation Provider:")
    print("  [1] Google Gemini")
    print("  [2] OpenAI")
    print("  [3] Anthropic Claude")
    print("  [4] None / Disable AI (Deterministic policy checks only)")
    print("-" * 65)

    llm_choice = interruptible_input("Select provider [1-4] (default: 1): ", eof_default="").strip() or "1"
    if llm_choice == "4":
        return "none", "", "", False

    llm_provider, llm_model, env_key_name = _LLM_PROVIDER_MAP.get(
        llm_choice, _LLM_PROVIDER_MAP["1"]
    )
    llm_api_key = interruptible_input(f"Enter {env_key_name} (press Enter to skip): ", eof_default="").strip()
    custom_model = interruptible_input(f"Enter model name [{llm_model}]: ", eof_default="").strip()
    if custom_model:
        llm_model = custom_model
    return llm_provider, llm_model, llm_api_key, True


def _update_local_env_if_present(
    app_id: int | str,
    key_path: str,
    installation_id: Optional[int | str],
    llm_provider: str,
    llm_model: str,
    llm_api_key: str,
    llm_enabled: bool,
) -> None:
    """Sync saved credentials into local .env when present in the working directory."""
    try:
        from .validator import ConfigValidator

        env_path = os.path.abspath(".env")
        if not os.path.isfile(env_path):
            return
        if app_id:
            ConfigValidator.update_env_file("REPO360_APP_ID", str(app_id), env_path)
        if installation_id:
            ConfigValidator.update_env_file("REPO360_APP_INSTALLATION_ID", str(installation_id), env_path)
        if key_path:
            ConfigValidator.update_env_file("REPO360_APP_PRIVATE_KEY_PATH", key_path, env_path)
        if llm_enabled and llm_api_key:
            env_key_map = {
                "gemini": "GEMINI_API_KEY",
                "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
            }
            if llm_provider in env_key_map:
                ConfigValidator.update_env_file(env_key_map[llm_provider], llm_api_key, env_path)
            ConfigValidator.update_env_file("LLM_PROVIDER", llm_provider, env_path)
            ConfigValidator.update_env_file("LLM_MODEL", llm_model, env_path)
    except Exception as err:
        logger.debug("Could not auto-update local .env: %s", err)


def _print_manual_permissions_guide() -> None:
    """Display read-only GitHub App permissions required for manual registration."""
    org_only = {"members"}
    repo_perms: list[str] = []
    org_perms: list[str] = []
    for perm in sorted(DEFAULT_GITHUB_APP_PERMISSIONS):
        if perm.startswith("organization_"):
            org_perms.append(perm.removeprefix("organization_"))
        elif perm in org_only:
            org_perms.append(perm)
        else:
            repo_perms.append(perm)

    print("\n" + "=" * 65)
    print("  REPO360 - MANUAL GITHUB APP SETUP")
    print("=" * 65)
    print("Ensure your GitHub App grants these read-only permissions:")
    print(f"  Repository  : {', '.join(repo_perms)}")
    print(f"  Organization: {', '.join(sorted(org_perms))}")
    print("-" * 65)


def _prompt_positive_int(prompt: str, field_name: str) -> int:
    """Prompt until a valid positive integer is entered."""
    while True:
        raw = interruptible_input(prompt).strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        print(f"Error: {field_name} must be a positive number.")


def _prompt_private_key_file() -> tuple[str, str]:
    """Prompt for a readable RSA PEM private key path and validate its contents."""
    while True:
        raw = interruptible_input(
            "Enter path to GitHub App private key (.pem): "
        ).strip().strip("\"'")
        if not raw:
            print("Error: Private key path is required.")
            continue
        candidate = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isfile(candidate):
            print(f"Error: Private key file not found: {candidate}")
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                pem_content = f.read()
        except OSError as err:
            print(f"Error: Cannot read private key file ({err}).")
            continue
        if "-----BEGIN" not in pem_content or "PRIVATE KEY-----" not in pem_content:
            print("Error: File does not appear to contain a valid PEM private key.")
            continue
        return candidate, pem_content


def _prompt_or_discover_installation_id(
    app_id: int,
    pem_content: str,
    api_domain: str,
    target_org: str = "",
) -> Optional[int]:
    """Prompt for Installation ID or attempt live auto-discovery via GitHub API."""
    while True:
        raw = interruptible_input(
            "Enter Installation ID (press Enter to auto-discover): ",
            eof_default="",
        ).strip()
        if not raw:
            break
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        print("Error: Installation ID must be a positive number (or press Enter to auto-discover).")

    try:
        auth = GitHubAppAuth(app_id=app_id, private_key=pem_content, api_base_url=api_domain)
        installations = auth.discover_installations()
        if installations:
            matched = (
                [
                    i for i in installations
                    if i.get("account", {}).get("login", "").lower() == target_org.lower()
                ]
                if target_org
                else []
            )
            selected = matched[0] if matched else installations[0]
            inst_id = int(selected["id"])
            org_login = selected.get("account", {}).get("login", "")
            label = f" for '{org_login}'" if org_login else ""
            print(f"-> [SUCCESS] Auto-detected Installation ID: {inst_id}{label}!")
            return inst_id
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as err:
        logger.debug("Manual setup installation auto-discovery skipped: %s", err)

    print("Installation ID will be auto-discovered at runtime.")
    return None


def run_manual_setup(github_domain: str = "", org_name: str = "") -> int:
    """Guide manual GitHub App credential entry or config file template creation."""
    _print_manual_permissions_guide()
    clean_domain = _normalize_github_domain(github_domain) if github_domain else _prompt_domain()
    api_domain = resolve_api_base_url(clean_domain)
    clean_org = _normalize_org_name(org_name)

    print("\nManual Configuration Options:")
    print("  [1] Enter GitHub App credentials now (App ID, private key, Installation ID)")
    print("  [2] Write ~/.repo360/config.json template to fill in manually")
    sub_choice = _prompt_choice(
        "Select option [1-2] (default: 1): ",
        valid=("1", "2"),
        default="1",
        error_msg="Please enter 1 or 2.",
    )

    if sub_choice == "2":
        secrets_dir = get_secrets_dir()
        cfg_path = get_credentials_path()
        existing = load_user_credentials()
        existing_gh = existing.get("github", {}) if isinstance(existing, dict) else {}
        has_existing = os.path.isfile(cfg_path) and bool(
            existing_gh.get("app_id") or existing_gh.get("private_key_path")
        )
        write_template = True
        if has_existing:
            overwrite = interruptible_input(
                f"Existing config found at {cfg_path}. Overwrite with a blank template? [y/N]: ",
                eof_default="n",
            ).strip().lower()
            write_template = overwrite in ("y", "yes")

        if write_template:
            llm_provider, llm_model, llm_api_key, llm_enabled = _prompt_ai_configuration()
            placeholder_key = os.path.join(secrets_dir, "<your-app>.private-key.pem")
            cfg_path = save_user_credentials(
                app_id="",
                private_key_path=placeholder_key,
                installation_id=None,
                api_base_url=api_domain,
                llm_provider=llm_provider,
                llm_model=llm_model,
                llm_api_key=llm_api_key,
                llm_enabled=llm_enabled,
            )

        print("\n" + "=" * 65)
        print("  MANUAL CONFIG TEMPLATE READY" if write_template else "  EXISTING CONFIG PRESERVED")
        print("=" * 65)
        print(f"  Config File:   {cfg_path}")
        print(f"  Secrets Dir:   {secrets_dir}")
        print(f"  API Base URL:  {api_domain}")
        print("-" * 65)
        print("  Next steps:")
        print(f"    1. Place your downloaded .pem private key in {secrets_dir}")
        print(f"    2. Update {cfg_path} with:")
        print("       - github.app_id            : Your numeric GitHub App ID")
        print("       - github.private_key_path  : Full path to your .pem file")
        print("       - github.installation_id   : Numeric ID or null (auto-discovered)")
        print("    3. Verify connectivity: repo360 health")
        print("=" * 65 + "\n")
        return 0

    app_id = _prompt_positive_int("Enter GitHub App ID: ", "GitHub App ID")
    key_path, pem_content = _prompt_private_key_file()
    installation_id = _prompt_or_discover_installation_id(
        app_id=app_id,
        pem_content=pem_content,
        api_domain=api_domain,
        target_org=clean_org,
    )
    llm_provider, llm_model, llm_api_key, llm_enabled = _prompt_ai_configuration()

    cfg_path = save_user_credentials(
        app_id=app_id,
        private_key_path=key_path,
        installation_id=installation_id,
        api_base_url=api_domain,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_enabled=llm_enabled,
    )
    _update_local_env_if_present(
        app_id=app_id,
        key_path=key_path,
        installation_id=installation_id,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_enabled=llm_enabled,
    )

    ai_status = f"{llm_provider.upper()} ({llm_model})" if llm_enabled else "DISABLED"
    print("\n" + "=" * 65)
    print("  MANUAL SETUP COMPLETE")
    print("=" * 65)
    print(f"  App ID:           {app_id}")
    print(f"  Installation ID:  {installation_id or 'Auto-discovered at runtime'}")
    print(f"  Private Key:      {key_path}")
    print(f"  API Base URL:     {api_domain}")
    print(f"  Configuration:    {cfg_path}")
    print(f"  AI Remediation:   {ai_status}")
    print("-" * 65)
    print("  Verify your setup anytime with:")
    print("    repo360 health")
    print("=" * 65 + "\n")
    return 0


def _parse_app_name(value: str) -> str:
    """Validate optional custom GitHub App name against GitHub's 1-34 char manifest limit."""
    cleaned = value.strip()
    if cleaned and len(cleaned) > 34:
        raise argparse.ArgumentTypeError("must be 1-34 characters")
    return cleaned


def main() -> int:
    install_signal_handlers()
    parser = argparse.ArgumentParser(
        prog="repo360 setup",
        description="Register the GitHub App and configure local credentials.",
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "manual"),
        default="",
        help="Setup method: auto or manual (prompted if omitted).",
    )
    parser.add_argument(
        "--org",
        required=False,
        default="",
        metavar="ORG",
        help="GitHub org name (e.g. 'org1'; prompted if omitted).",
    )
    parser.add_argument(
        "--app-name",
        required=False,
        default="",
        type=_parse_app_name,
        metavar="NAME",
        help="Custom GitHub App name (1-34 chars; default: Repo360-<org>).",
    )
    parser.add_argument(
        "--domain",
        required=False,
        default="https://github.com",
        metavar="URL",
        help="GitHub host URL (default: https://github.com).",
    )

    try:
        args = parser.parse_args()
        raw_mode = getattr(args, "mode", "")
        mode = raw_mode.lower() if isinstance(raw_mode, str) and raw_mode.lower() in ("auto", "manual") else _prompt_setup_mode()

        org = _normalize_org_name(args.org)
        has_explicit_domain = args.domain != "https://github.com"

        if mode == "manual":
            manual_domain = args.domain if (org or has_explicit_domain) else ""
            return run_manual_setup(github_domain=manual_domain, org_name=org)

        if not org:
            org = _normalize_org_name(
                interruptible_input("Enter your target GitHub Organization name: ", eof_default="")
            )

        if not org:
            print("Error: Organization name is required.")
            return 1

        domain = args.domain if (args.org or has_explicit_domain) else _prompt_domain()
        run_wizard(org_name=org, github_domain=domain, app_name=args.app_name)
        return 0
    except EOFError:
        print("\nSetup cancelled (end of input).")
        return 1
    except KeyboardInterrupt:
        return finalize_interrupt_exit(130)


if __name__ == "__main__":
    sys.exit(main())
