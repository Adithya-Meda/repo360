#!/usr/bin/env bash
set -euo pipefail

REPO="Adithya-Meda/repo360"
API_URL="https://api.github.com/repos/${REPO}/releases/latest"

if ! command -v pipx >/dev/null 2>&1; then
  echo "Error: pipx is required but not found on PATH. Install it with: python3 -m pip install --user pipx && python3 -m pipx ensurepath" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "Error: curl is required to query the latest release." >&2
  exit 1
fi

echo "Fetching latest Repo360 release metadata..."
RELEASE_JSON="$(curl -fsSL -H "Accept: application/vnd.github+json" -H "User-Agent: repo360-installer" "$API_URL")"

TAG="$(printf '%s' "$RELEASE_JSON" | python3 -c '
import json, re, sys
data = json.load(sys.stdin)
tag = str(data.get("tag_name") or "").strip()
if not re.match(r"^v?\d+\.\d+\.\d+$", tag):
    sys.exit(1)
print(tag)
')"

WHEEL_URL="$(printf '%s' "$RELEASE_JSON" | python3 -c '
import json, sys
data = json.load(sys.stdin)
prefix = "https://github.com/Adithya-Meda/repo360/releases/download/"
for asset in data.get("assets") or []:
    url = str(asset.get("browser_download_url") or "")
    if url.startswith(prefix) and url.endswith(".whl"):
        print(url)
        break
')"

TARGET_SPEC="${WHEEL_URL:-git+https://github.com/${REPO}.git@${TAG}}"
echo "Installing Repo360 ${TAG} from ${TARGET_SPEC}..."
pipx install --force "$TARGET_SPEC"

echo "Installed Repo360 ${TAG} successfully."
if command -v repo360 >/dev/null 2>&1; then
  repo360 version
else
  echo "Run 'pipx ensurepath' and restart your shell if 'repo360' is not yet on PATH."
fi
