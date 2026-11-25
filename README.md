# Repo360

Platform governance, compliance, engineering intelligence, and infrastructure metrics for **GitHub Enterprise Cloud** organizations. Evaluates repositories against security baselines, analyzes contributor activity, monitors Actions runner fleets, profiles storage and artifact costs (FinOps), tracks cross-organization DORA delivery metrics, audits security and ruleset bypasses, and generates executive multi-tab Excel workbooks.

> [!WARNING]
> **Advisory Only:** Remediation snippets and policies are advisory. Validate suggested changes in non-production environments before applying.

---

## Prerequisites

- **Python 3.10+** and **[pipx](https://pypa.github.io/pipx/)** (for CLI use)
- **Organization Owner / Admin access** on GitHub Enterprise Cloud (to install the read-only GitHub App)
- **AI API Key** (Optional: Google Gemini, OpenAI, or Anthropic — used when `--require-ai yes` is enabled; defaults to `--require-ai no` deterministic policy rules)

---

## Tool Features

### Analytics & Intelligence Commands

| Command | Description |
| :--- | :--- |
| `audit` | Security, governance, and CI/CD compliance audit across repositories and organizations. |
| `activity` | Contributor activity metrics — commits, pull requests, code reviews, and issue engagement. |
| `runners` | Self-hosted Actions runner fleet analytics — availability, compute hours, and queue latency. |
| `finops` | Storage and cost profiling — repository quotas, artifact retention, and package registries. |
| `dora` | DORA delivery metrics — Deployment Frequency, Lead Time, Change Failure Rate, and MTTR. |
| `bypass` | Push-protection and ruleset bypass audit ledger — actor, secret type, and timestamp records. |

### Configuration & Lifecycle Commands

| Command | Description |
| :--- | :--- |
| `setup` | Configure GitHub App credentials via auto-browser setup or manual config (`--mode auto` or `--mode manual`), and store keys under `~/.repo360/`. |
| `upgrade` | Upgrade Repo360 to the latest published release, or install a specific tag with `--tag TAG`. |
| `health` | Check GitHub App authentication, organization installations, and AI engine status. |
| `version` | Show the installed Repo360 version. |
| `uninstall` | Remove the package, virtual environment, and config while preserving generated reports. |

---

## Required GitHub App Permissions

All core permissions are read-only by default. Note that for organization-level rulesets, GitHub's REST API (`GET /orgs/{org}/rulesets`) strictly requires `administration: write` at the organization scope. This permission must be manually provided to the GitHub App if auditing organization-level rulesets is required; otherwise, org ruleset checks are skipped while individual repository branch protections are evaluated per repo.

| Permission | Scope | Feature |
| :--- | :--- | :--- |
| `actions: read` | Repository | `audit`, `runners`, `finops`, `dora` |
| `administration: read` | Repository | `audit`, `runners`, `bypass` |
| `contents: read` | Repository | `audit`, `activity`, `dora` |
| `deployments: read` | Repository | `dora` |
| `issues: read` | Repository | `activity`, `dora` |
| `metadata: read` | Repository | `audit`, `activity`, `runners`, `finops`, `dora`, `bypass` |
| `packages: read` | Repository | `finops` |
| `pull_requests: read` | Repository | `activity`, `dora` |
| `secret_scanning_alerts: read` | Repository | `bypass` |
| `administration: read` | Organization | `audit` |
| `administration: write` | Organization | `audit` (required for Organization Rulesets; manually provide this permission if org-level rulesets are required) |
| `members: read` | Organization | `audit`, `activity` |
| `self_hosted_runners: read` | Organization | `audit`, `runners` |

---

## How to Use

### Method 1: Local CLI

#### 1. Install
```bash
pipx install git+https://github.com/Adithya-Meda/repo360.git
```

#### 2. Setup (One-Time)
```bash
repo360 setup --org <your-org>
```
Prompts to choose between **auto-configuring** a read-only GitHub App in the browser or **manually configuring** credentials (entering an existing App's details or generating a `~/.repo360/config.json` template to edit directly). Pass `--mode auto` or `--mode manual` to skip the mode prompt.

#### 3. Run Commands

Repositories are automatically discovered directly from GitHub App installations across your organizations:

```bash
# Governance Audit
repo360 audit

# User Activity & Contributor Metrics
repo360 activity

# Actions Self-Hosted Runners Analytics
repo360 runners

# Per-Repository Storage Quota & Artifact Profiler
repo360 finops

# Cross-Organization DORA & Delivery Flow Metrics
repo360 dora

# Consolidated Push Protection & Ruleset Bypass Ledger
repo360 bypass

# Verify Installed Version
repo360 version

# Verify GitHub App Auth, Installations & AI Engine Health
repo360 health
```

#### 4. Upgrade or Switch Versions
```bash
# Upgrade to the latest published release
repo360 upgrade

# Install or switch to a specific release tag (upgrades or downgrades)
repo360 upgrade --tag v1.2.0
```
Release upgrades install the exact commit the release tag resolves to, so a re-pointed tag cannot change the code you install. Stored credentials and GitHub App private keys in `~/.repo360/` are automatically preserved across upgrades.

#### 5. Uninstall
```bash
repo360 uninstall
```
Removes the installed `repo360` package, isolated virtual environment, and `~/.repo360/` configuration and secrets while preserving generated reports in `./repo360-output/`. If `REPO360_CONFIG_DIR` points to a custom directory, only the files Repo360 created there (`config.json` and `secrets/*.private-key.pem`) are removed — the directory itself is never deleted.

#### 6. Configuration Reference
| Variable | Purpose |
| --- | --- |
| `REPO360_APP_ID`, `REPO360_APP_PRIVATE_KEY` (PEM or base64 PEM), `REPO360_APP_PRIVATE_KEY_PATH`, `REPO360_APP_INSTALLATION_ID` | GitHub App credentials (override `~/.repo360/config.json`) |
| `REPO360_API_BASE_URL` | GitHub API endpoint (HTTPS required) |
| `REPO360_CONFIG_DIR` | Alternate location for `config.json` and `secrets/` |
| `REPO360_TIMEZONE` / `TZ` | IANA timezone (e.g. `Asia/Kolkata`, `America/New_York`) for report timestamps |
| `REPO360_DEBUG=1` | Show full tracebacks instead of the concise failure banner |

A `.env` file in the current working directory is also loaded; real environment variables always take precedence.

---

### Multi-Organization Support

Install the registered GitHub App across multiple organizations in your enterprise using the same App ID and private key:
- **Automatic Multi-Org Discovery:** Running `repo360 <Command>` automatically discovers every organization where the app is installed, provisions scoped tokens per organization, and aggregates findings into a single executive workbook.
- **Targeted Organization Filtering:** Restrict scans to specific organizations on demand using `--org`:
  ```bash
  repo360 <Command> --org OrgAlpha,OrgBeta
  ```

---

### Method 2: GitHub Actions (CI/CD)

Repo360 is packaged as a composite action that sets up the environment, executes requested scans, and uploads reports as workflow artifacts.

#### 1. Configure Secrets & Variables
In repository **Settings** &rarr; **Secrets and variables** &rarr; **Actions**:
- `REPO360_APP_ID`: Numeric GitHub App ID (Variable or Secret)
- `REPO360_APP_PRIVATE_KEY`: RSA private key `.pem` string (Secret)
- `AI_API_KEY`: API key for Google Gemini, OpenAI, or Anthropic (Secret, optional — used when `--require-ai yes` is enabled)

#### 2. Workflow Example (`.github/workflows/audit.yml`)

```yaml
name: "Repo360 Platform Audit"

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  governance-audit:
    name: "Execute Advisory Audit"
    runs-on: ubuntu-latest

    steps:
      - name: "Run Repo360 Action"
        uses: Adithya-Meda/repo360@main # Pin to a release tag or commit SHA in production (e.g. @v1.2.0)
        with:
          app_id: ${{ vars.REPO360_APP_ID }}
          private_key: ${{ secrets.REPO360_APP_PRIVATE_KEY }}
          collect_audit: "true"
          collect_activity: "true"
          collect_runners: "true"
          collect_finops: "true"
          collect_dora: "true"
          collect_bypass: "true"
          require_ai: "no"
          ai_api_key: ${{ secrets.AI_API_KEY }}
```

#### 3. Action Inputs Reference (`with:`)

##### Command & Feature Flags

| Input Key | Description | Supported Values | Default |
| :--- | :--- | :--- | :--- |
| `collect_audit` | Audit platform governance policies | `"true"`, `"false"` | `"false"` |
| `collect_activity` | Collect contributor activity telemetry | `"true"`, `"false"` | `"false"` |
| `collect_runners` | Profile runner fleet metrics | `"true"`, `"false"` | `"false"` |
| `collect_finops` | Profile storage artifact quotas | `"true"`, `"false"` | `"false"` |
| `collect_dora` | Calculate DORA flow metrics | `"true"`, `"false"` | `"false"` |
| `collect_bypass` | Audit security policy bypasses | `"true"`, `"false"` | `"false"` |

##### Configuration Parameters

| Input Key | Description | Supported Values | Default |
| :--- | :--- | :--- | :--- |
| `app_id` | Numeric GitHub App ID | String (numeric) | *Required* |
| `private_key` | App RSA private key | String (`.pem`) | *Required* |
| `org` | Target organization filter list | String (`"org1,org2"`) | `""` *(All orgs)* |
| `lookback_days` | Telemetry lookback window days | Integer (`"1"`-`"400"`) | `"90"` |
| `include_archived` | Scan archived repository targets | `"true"`, `"false"` | `"false"` |
| `workers` | Worker thread concurrency limit | Integer (`"0"`-`"32"`) | `"0"` *(Auto)* |
| `require_ai` | Enable AI advisory insights | `"yes"`, `"no"` | `"no"` |
| `ai_api_key` | Unified AI API key | String | `""` |
| `ai_model` | Target AI model identifier | String | `""` *(User selected)* |
| `output_dir` | Report output directory path | String | `"./repo360-output"` |
| `upload_artifact` | Upload workflow report artifact | `"true"`, `"false"` | `"true"` |
| `artifact_name` | Workflow artifact bundle name | String | `"repo360-audit-reports"` |
| `artifact_retention_days` | Artifact retention window days | Integer (`"1"`-`"90"`) | `"60"` |

---

## Audited Checks & Platform Coverage

- **Org Governance:** Organization rulesets, secret scanning push protection, runner group least privilege isolation, default workflow token permissions.
- **Repository Security:** Branch protection review gates, `CODEOWNERS` validation, GitHub Actions commit SHA pinning, workflow concurrency groups, Dependabot.
- **Developer Enablement:** Dev containers (`devcontainer.json`), GitHub Copilot instructions, standardized issue & PR templates, pre-commit hooks.
- **User Activity Collection:** Contributor commit cadence, pull requests opened/merged, code review participation, issue discussions, active days, and adoption metrics.
- **Runner Fleet Intelligence:** Actions runner availability, ephemeral provisioning, execution compute hours, queue latency, and SLA breach tracking (>60s).
- **FinOps & Storage Profiling:** Per-repository Git storage quotas, GitHub Actions workflow artifacts, default 90-day retention waste detection, stale Actions caches, and container/package registries.
- **DORA & Flow Metrics:** Cross-organization Deployment Frequency (DF), Lead Time for Changes (LTTC), Change Failure Rate (CFR), and Mean Time to Recovery (MTTR) with Google DORA performance tier classification (Elite, High, Medium, Low).
- **Security & Policy Bypass Ledger:** Consolidated push protection bypass records (with secret type, bypassing actor, timestamp, and justification reason) and branch ruleset override audit history.

---

## Legal Disclaimers

### Trademarks & Non-Affiliation
* **GitHub**: GitHub and the GitHub logo are registered trademarks of GitHub, Inc. or Microsoft Corporation. Repo360 is an independent open-source project and is not affiliated with, endorsed by, sponsored by, or supported by GitHub, Inc. or Microsoft Corporation.
* **AI Providers**: Google, Gemini, OpenAI, Anthropic, and other third-party trademarks referenced in documentation or configuration are the property of their respective trademark holders. Repo360 is an independent project and is not affiliated with, endorsed by, or sponsored by any AI model provider.

### Advisory & Informational Purpose
Repo360 and its generated audit reports, metrics, DORA classifications, and AI-assisted insights are provided for informational, diagnostic, and governance analysis purposes only. They do not constitute formal legal, regulatory, or compliance certification. Users are solely responsible for evaluating, testing, and applying security configurations and changes within their environments.

---

## License

[MIT License](LICENSE) — Copyright (c) 2026 Adithya Meda
