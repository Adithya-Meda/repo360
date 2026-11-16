# Privacy Notice & Data Handling Policy

**Last Updated:** November 16, 2026

Repo360 is an enterprise platform governance and engineering intelligence tool. Because Repo360 interacts with organizational security and audit configurations, this document outlines our transparent privacy and data handling practices.

---

## 1. Zero Telemetry Policy
* **No Tracking or Beacons:** Repo360 does **not** collect, store, or transmit any telemetry, analytics, usage statistics, or crash reports back to the developers or third-party servers.
* **No Remote Phone-Home:** The tool executes strictly on your local machine or CI/CD runner. Network requests are made solely to endpoints you explicitly configure.

---

## 2. Credential & Secret Management
* **Local Storage Only:** GitHub App credentials, RSA private keys, and configuration files are stored locally under `~/.repo360/`.
* **File Permissions:** Configuration files and private keys are created with restricted file permissions (`0600` on POSIX systems, user-scoped ACLs on Windows) accessible only to the active user.
* **Non-Persistent Authentication:** GitHub App installation tokens are generated in-memory on demand and are never written to disk or logged.
* **Redaction in Logs:** All tokens and private keys are redacted from stdout and logging handlers.

---

## 3. Network & API Destinations
Repo360 connects exclusively to:
1. **GitHub REST & GraphQL APIs:**
   * Target endpoints configured by you: `https://api.github.com`, GitHub Enterprise Cloud (`*.ghe.com`), or GitHub Enterprise Server (GHES).
   * All API calls are read-only (`GET` requests), requiring minimal read scopes.
2. **AI Provider APIs (Optional):**
   * If and only if `--require-ai yes` is enabled, the tool communicates with your configured AI provider (e.g. Google Gemini API) using your own API key.
   * Only sanitized metadata (e.g., check name, remediation context) is transmitted. No proprietary source code, credentials, or audit spreadsheets are sent.

---

## 4. Generated Reports & Data Sovereignty
* All audit workbooks (`.xlsx`) and data outputs are saved locally to `./repo360-output/` or your specified `--output-dir`.
* You retain full ownership and control of all generated reports.
* The uninstaller (`repo360 uninstall`) strictly preserves the `./repo360-output/` directory by default to prevent data loss.

---

## 5. Contact
For questions regarding this privacy policy, please reach out to:
* **Contact:** `47377931+Adithya-Meda@users.noreply.github.com`
