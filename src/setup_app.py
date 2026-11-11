"""Automated 1-Click GitHub App Registration Wizard for Repo360.

Uses GitHub's official App Manifest flow:
1. Prompts interactively for target Organization and parameters.
2. Spins up a temporary local HTTP server on http://localhost:8989
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
import socketserver
import time
import urllib.parse
import webbrowser
from typing import Optional
import requests

from .auth.github_app_auth import GitHubAppAuth
from .credentials import (
    save_private_key,
    save_user_credentials,
)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("setup-wizard")

PORT = 8989
CALLBACK_URL = f"http://localhost:{PORT}/callback"


def _normalize_github_domain(domain: str) -> str:
    """Normalize a GitHub base URL to scheme + host without trailing slashes."""
    cleaned = (domain or "https://github.com").strip().rstrip("/")
    if not cleaned.startswith(("http://", "https://")):
        cleaned = f"https://{cleaned}"
    parsed = urllib.parse.urlparse(cleaned)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else cleaned


def get_manifest(github_domain: str, app_name: str = "Repo360", state_nonce: str = "") -> dict:
    """Generate the GitHub App manifest payload.

    GitHub's manifest validator requires `redirect_url` to be a clean callback URL
    without query parameters; CSRF `state` must be passed via the form action query string.
    """
    _ = state_nonce  # Retained for backwards compatibility with callers; state is sent on form action.
    clean_domain = _normalize_github_domain(github_domain)
    return {
        "name": app_name or "Repo360",
        "url": clean_domain,
        "description": "360-Degree Platform Governance, Compliance & Engineering Intelligence for GitHub",
        "hook_attributes": {
            "url": clean_domain,
            "active": False,
        },
        "redirect_url": CALLBACK_URL,
        "default_permissions": {
            "actions": "read",
            "administration": "read",
            "contents": "read",
            "issues": "read",
            "members": "read",
            "metadata": "read",
            "organization_administration": "read",
            "organization_self_hosted_runners": "read",
            "packages": "read",
            "pull_requests": "read",
            "secret_scanning_alerts": "read",
        },
        "public": False,
    }


class ManifestCallbackHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler to serve the initial auto-redirect and receive the GitHub callback."""

    target_org: str = ""
    app_name: str = ""
    github_domain: str = "https://github.com"
    api_domain: str = "https://api.github.com"
    conversion_result: dict | None = None
    session_nonce: str = secrets.token_urlsafe(32)

    def log_message(self, fmt: str, *args) -> None:
        """Suppress standard HTTP server access logs."""
        pass

    def do_GET(self) -> None:
        """Handle initial browser landing and GitHub callback redirect."""
        parsed_url = urllib.parse.urlparse(self.path)

        if parsed_url.path == "/":
            clean_domain = _normalize_github_domain(self.github_domain)
            # Auto-assign unique name using target_org to avoid global GitHub App name collisions
            clean_org = self.target_org.strip() if self.target_org else ""
            default_name = f"Repo360-{clean_org}" if clean_org else "Repo360"
            active_name = self.app_name or default_name
            manifest_json = json.dumps(get_manifest(clean_domain, app_name=active_name))

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
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_page.encode("utf-8"))
            return

        elif parsed_url.path == "/callback":
            # GitHub redirected back with ?code=...&state=...
            query_params = urllib.parse.parse_qs(parsed_url.query)
            state = query_params.get("state", [""])[0]
            code = query_params.get("code", [""])[0]

            if not code:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Error: Missing authorization code in callback.")
                return

            if ManifestCallbackHandler.session_nonce and not secrets.compare_digest(state, ManifestCallbackHandler.session_nonce):
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Security Error: Invalid or expired state nonce.")
                return

            # Exchange code for app credentials
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
                error_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Setup Error - Repo360</title>
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
      margin: 16px 0;
    }}
    p {{ color: #8b949e; font-size: 14px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>App Conversion Failed</h1>
    <pre>{html.escape(str(err))}</pre>
    <p>Please return to your terminal to inspect the error log or retry.</p>
  </div>
</body>
</html>"""
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


def run_wizard(
    org_name: str,
    github_domain: str = "https://github.com",
    app_name: str = "",
    open_browser: bool = True,
) -> None:
    """Run the interactive setup wizard."""
    github_domain = _normalize_github_domain(github_domain)
    api_domain = resolve_api_base_url(github_domain)

    default_name = f"Repo360-{org_name}" if org_name else "Repo360"
    chosen_name = app_name or default_name

    ManifestCallbackHandler.target_org = org_name
    ManifestCallbackHandler.app_name = chosen_name
    ManifestCallbackHandler.github_domain = github_domain
    ManifestCallbackHandler.api_domain = api_domain
    ManifestCallbackHandler.conversion_result = None
    ManifestCallbackHandler.session_nonce = secrets.token_urlsafe(32)

    print("\n" + "=" * 65)
    print("  REPO360 - 1-CLICK GITHUB APP SETUP")
    print("=" * 65)
    print(f"Target Organization: {org_name}")
    print(f"App Name:            {chosen_name}")
    print(f"GitHub Domain:       {github_domain}")
    print(f"Callback Listener:   {CALLBACK_URL}")
    print("-" * 65)
    print("Starting local listener and opening your browser...")

    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("localhost", PORT), ManifestCallbackHandler)
    server.timeout = 0.5
    if open_browser:
        webbrowser.open(f"http://localhost:{PORT}/")

    # Wait for the single callback request (timeout=0.5 ensures responsive Ctrl+C handling on Windows)
    try:
        while ManifestCallbackHandler.conversion_result is None:
            server.handle_request()
    finally:
        server.server_close()

    result = ManifestCallbackHandler.conversion_result
    if not result:
        print("Setup aborted: Could not obtain GitHub App credentials.")
        return

    app_id = result.get("id")
    app_slug = result.get("slug") or "repo360"
    pem_key = result.get("pem", "")
    install_url = f"{github_domain}/organizations/{org_name}/settings/apps/{app_slug}/installations"

    # 1. Automatically save private key to ~/.repo360/secrets/
    key_path = save_private_key(pem_key, app_slug=app_slug)
    # Also save a copy to ./secrets/ if local workspace exists
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

    # 2. App Installation & Auto-fetch Installation ID
    print("\n" + "=" * 65)
    print("  STEP 2: INSTALL APP ON YOUR ORGANIZATION")
    print("=" * 65)
    print(f"  Installation URL: {install_url}")
    print("  Please click 'Install' and choose 'All repositories' (recommended).")
    print("-" * 65)

    if open_browser:
        try:
            webbrowser.open(install_url)
        except Exception:
            pass

    installation_id: Optional[int] = None
    try:
        auth = GitHubAppAuth(app_id=app_id, private_key=pem_key, api_base_url=api_domain)
        prompt_msg = (
            "Press Enter once you clicked 'Install' in the browser to auto-fetch your Installation ID\n"
            "(or enter 'm' to input manually, 's' to skip and auto-discover at runtime): "
        )
        choice = input(prompt_msg).strip().lower()

        if choice == "m":
            manual_in = input("Enter Installation ID: ").strip()
            if manual_in.isdigit():
                installation_id = int(manual_in)
        elif choice != "s":
            print("Querying GitHub API for active installations...")
            for _ in range(3):
                installations = auth.discover_installations()
                if installations:
                    matched = [
                        i for i in installations
                        if i.get("account", {}).get("login", "").lower() == org_name.lower()
                    ]
                    selected = matched[0] if matched else installations[0]
                    installation_id = int(selected["id"])
                    org_login = selected.get("account", {}).get("login", org_name)
                    print(f"-> [SUCCESS] Auto-detected Installation ID: {installation_id} for '{org_login}'!")
                    break
                time.sleep(2)

            if not installation_id:
                print("Could not auto-detect active installation yet.")
                manual_in = input("Enter Installation ID manually (or press Enter to auto-discover at runtime): ").strip()
                if manual_in.isdigit():
                    installation_id = int(manual_in)
    except Exception as err:
        logger.debug("Installation discovery error: %s", err)
        manual_in = input("Enter Installation ID manually (or press Enter to auto-discover at runtime): ").strip()
        if manual_in.isdigit():
            installation_id = int(manual_in)

    # 3. Interactive AI Remediation Provider Selection
    print("\n" + "=" * 65)
    print("  STEP 3: AI REMEDIATION & GOVERNANCE ADVISORY ENGINE")
    print("=" * 65)
    print("Select your AI Remediation Provider:")
    print("  [1] Google Gemini")
    print("  [2] OpenAI")
    print("  [3] Anthropic Claude")
    print("  [4] None / Disable AI (Deterministic policy checks only)")
    print("-" * 65)

    llm_choice = input("Select provider [1-4] (default: 1): ").strip() or "1"
    llm_provider = "gemini"
    llm_model = "gemini-3.5-flash"
    llm_api_key = ""
    llm_enabled = True

    if llm_choice == "2":
        llm_provider = "openai"
        llm_model = "gpt-4o"
        llm_api_key = input("Enter OPENAI_API_KEY (press Enter to skip): ").strip()
        custom_model = input(f"Enter model name [{llm_model}]: ").strip()
        if custom_model:
            llm_model = custom_model
    elif llm_choice == "3":
        llm_provider = "anthropic"
        llm_model = "claude-3-5-sonnet-20241022"
        llm_api_key = input("Enter ANTHROPIC_API_KEY (press Enter to skip): ").strip()
        custom_model = input(f"Enter model name [{llm_model}]: ").strip()
        if custom_model:
            llm_model = custom_model
    elif llm_choice == "4":
        llm_provider = "none"
        llm_model = ""
        llm_api_key = ""
        llm_enabled = False
    else:
        llm_provider = "gemini"
        llm_model = "gemini-3.5-flash"
        llm_api_key = input("Enter GEMINI_API_KEY (press Enter to skip): ").strip()
        custom_model = input(f"Enter model name [{llm_model}]: ").strip()
        if custom_model:
            llm_model = custom_model

    # 4. Persist credentials securely
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

    # Optional: Also update local .env if present for project consistency
    try:
        from .validator import ConfigValidator
        env_path = os.path.abspath(".env")
        if os.path.isfile(env_path):
            ConfigValidator.update_env_file("REPO360_APP_ID", str(app_id), env_path)
            if installation_id:
                ConfigValidator.update_env_file("REPO360_APP_INSTALLATION_ID", str(installation_id), env_path)
            ConfigValidator.update_env_file("REPO360_APP_PRIVATE_KEY_PATH", key_path, env_path)
            if llm_enabled and llm_api_key:
                if llm_provider == "gemini":
                    ConfigValidator.update_env_file("GEMINI_API_KEY", llm_api_key, env_path)
                elif llm_provider == "openai":
                    ConfigValidator.update_env_file("OPENAI_API_KEY", llm_api_key, env_path)
                elif llm_provider == "anthropic":
                    ConfigValidator.update_env_file("ANTHROPIC_API_KEY", llm_api_key, env_path)
                ConfigValidator.update_env_file("LLM_PROVIDER", llm_provider, env_path)
                ConfigValidator.update_env_file("LLM_MODEL", llm_model, env_path)
    except Exception as err:
        logger.debug("Could not auto-update local .env: %s", err)

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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="repo360 setup",
        description="1-Click GitHub App Setup Wizard for Repo360"
    )
    parser.add_argument(
        "--org",
        required=False,
        default="",
        help="Target GitHub organization name (e.g. fintech-core).",
    )
    parser.add_argument(
        "--app-name",
        required=False,
        default="",
        help="Custom GitHub App name (defaults to Repo360-<org>).",
    )
    parser.add_argument(
        "--domain",
        required=False,
        default="https://github.com",
        help="GitHub domain (default: https://github.com).",
    )
    args = parser.parse_args()

    try:
        org = args.org
        if not org:
            org = input("Enter your target GitHub Organization name: ").strip()

        if not org:
            print("Error: Organization name is required.")
            return 1

        domain = args.domain
        if not args.org and domain == "https://github.com":
            custom_domain = input(
                "Enter GitHub Domain [https://github.com] (press Enter for default, or e.g. https://<enterprise>.ghe.com or https://ghe.company.com): "
            ).strip()
            if custom_domain:
                domain = custom_domain

        run_wizard(org_name=org, github_domain=domain, app_name=args.app_name)
        return 0
    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user (Ctrl+C). Exiting...")
        return 130


if __name__ == "__main__":
    import sys
    sys.exit(main())
