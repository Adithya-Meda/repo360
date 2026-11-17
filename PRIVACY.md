# Privacy Notice & Data Handling Policy

**Last Updated:** November 17, 2026

Repo360 is an enterprise platform governance and engineering intelligence tool maintained by Adithya Meda. Because Repo360 interacts with organizational security and audit configurations, this document outlines transparent privacy and data handling practices.

---

## 1. Zero Telemetry Policy
* **No Tracking or Beacons:** Repo360 does **not** collect, store, or transmit any telemetry, analytics, usage statistics, or crash reports back to the maintainer or third-party servers.
* **No Remote Phone-Home:** The tool executes strictly on your local machine or CI/CD runner. Network requests are made solely to endpoints you explicitly configure.

---

## 2. Credential & Secret Management
* **Local Storage Only:** GitHub App credentials, RSA private keys, and configuration files are stored locally under `~/.repo360/`.
* **File Permissions:** Configuration files and private keys are created with restricted file permissions (`0600` files / `0700` directories on POSIX systems). On Windows they inherit the access control list of your user profile directory, which is private to your account by default.
* **Non-Persistent Authentication:** GitHub App installation tokens are generated in-memory on demand and are never written to disk or logged.
* **Credential Isolation:** Installation tokens are only ever sent to the configured GitHub API host over HTTPS; requests to any other host (including pagination links) are refused.

---

## 3. Network & API Destinations
Repo360 connects exclusively to:
1. **GitHub REST & GraphQL APIs:**
   * Target endpoints configured by you: `https://api.github.com`, GitHub Enterprise Cloud (`*.ghe.com`), or GitHub Enterprise Server (GHES). HTTPS is mandatory.
   * All operations are read-only. Data is fetched with REST `GET` requests and GraphQL queries (sent as `POST`, as GraphQL requires); the only other `POST` is the GitHub App installation-token exchange. No repository, organization, or settings data is ever modified.
2. **AI Provider APIs (Optional):**
   * If and only if `--require-ai yes` is enabled, the tool communicates with your configured AI provider (Google Gemini, OpenAI, or Anthropic) over HTTPS using your own API key.
   * Data sent is limited to audit metadata: organization and repository names, check names and findings, detected languages/frameworks, aggregate metrics, and — for push-protection bypass analysis — the secret type, bypassing actor's GitHub login, and the developer's bypass justification text.
   * Before transmission, every prompt is scanned and common credential formats (GitHub tokens, private keys, cloud/API keys, JWTs, bearer tokens, `password=`/`token=` assignments) are replaced with `[REDACTED_CREDENTIAL]`. Source code and generated spreadsheets are never sent.
   * Review your AI provider's data retention terms before enabling AI features for regulated organizations.

---

## 4. Generated Reports & Data Sovereignty
* All audit workbooks (`.xlsx`) and data outputs are saved locally to `./repo360-output/` or your specified `--output-dir`.
* You retain full ownership and control of all generated reports.
* The uninstaller (`repo360 uninstall`) strictly preserves the `./repo360-output/` directory by default to prevent data loss.

---

## 5. Contact
For questions regarding this privacy policy, please reach out to:
* **Contact:** `47377931+Adithya-Meda@users.noreply.github.com`
