# Repo360

Platform governance, compliance, engineering intelligence, and infrastructure metrics for GitHub organizations. Evaluates repositories against security baselines, analyzes contributor activity, monitors Actions self-hosted runner fleets, profiles storage and artifact costs (FinOps), tracks cross-organization DORA delivery metrics, audits security and ruleset bypasses, and generates executive multi-tab Excel workbooks.

> [!WARNING]
> **Advisory Only:** Remediation snippets and policies are advisory. Validate suggested changes in non-production environments before applying.

---

## Prerequisites

- **Python 3.10+** and **[pipx](https://pypa.github.io/pipx/)** (for CLI use)
- **Organization Owner / Admin access** on GitHub (to install the read-only GitHub App)
- **AI API Key** (Required for audit: Google Gemini, OpenAI, or Anthropic to synthesize tailored remediations and executive briefings)

---

## Supported Platforms

| Platform | Domain Example | Setup Command |
| :--- | :--- | :--- |
| **GitHub.com** | `https://github.com` | `repo360 setup --org <your-org>` |
| **GHE.com** *(Data Residency)* | `https://<enterprise>.ghe.com` | `repo360 setup --org <your-org> --domain https://<enterprise>.ghe.com` |
| **GHES** *(On-Premises)* | `https://ghe.mycompany.com` | `repo360 setup --org <your-org> --domain https://ghe.mycompany.com` |

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
| `setup` | Register the GitHub App, configure credentials, and store keys under `~/.repo360/`. |
| `upgrade` | Upgrade to the latest version and verify the installed commit matches remote HEAD. |
| `health` | Check GitHub App authentication, organization installations, and AI engine status. |
| `version` | Show the installed version and commit SHA, with optional remote sync check. |
| `uninstall` | Remove the package, virtual environment, and config while preserving generated reports. |

---

## Required GitHub App Permissions

All permissions are read-only:

| Permission | Scope | Feature |
| :--- | :--- | :--- |
| `actions: read` | Repository | `audit`, `runners`, `finops`, `dora` |
| `administration: read` | Repository | `audit`, `runners`, `bypass` |
| `contents: read` | Repository | `audit`, `activity`, `dora` |
| `issues: read` | Repository | `activity` |
| `metadata: read` | Repository | `audit`, `activity`, `runners`, `finops`, `dora`, `bypass` |
| `packages: read` | Repository | `finops` |
| `pull_requests: read` | Repository | `activity` |
| `secret_scanning_alerts: read` | Repository | `bypass` |
| `administration: read` | Organization | `audit` |
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
Registers the read-only GitHub App with all required permissions, downloads the private key to `~/.repo360/secrets/`, and saves local configuration.

#### 3. Run Commands

Repositories are automatically discovered directly from GitHub App installations across your organizations:

```bash
# Governance Audit (Executive Excel workbook)
repo360 audit
repo360 audit --org <your-org>

# User Activity & Contributor Metrics (Executive Excel workbook)
repo360 activity
repo360 activity --org <your-org>

# Actions Self-Hosted Runners Analytics (Executive Excel workbook)
repo360 runners
repo360 runners --org <your-org>

# Per-Repository Storage Quota & Artifact Profiler (Executive Excel workbook)
repo360 finops
repo360 finops --org <your-org>

# Cross-Organization DORA & Delivery Flow Metrics (Executive Excel workbook)
repo360 dora
repo360 dora --org <your-org>

# Consolidated Push Protection & Ruleset Bypass Ledger (Executive Excel workbook)
repo360 bypass
repo360 bypass --org <your-org>

# Verify Installed Version & Latest Commit Sync
repo360 version
repo360 version --check

# Verify GitHub App Auth, Installations & AI Engine Health
repo360 health
```

All reports are written to `./repo360-output/`.

#### 4. Upgrade
```bash
repo360 upgrade
repo360 upgrade --check
repo360 upgrade --force
```
Stored credentials and GitHub App private keys in `~/.repo360/` are automatically preserved across upgrades.

#### 5. Uninstall
```bash
repo360 uninstall
```
Removes the installed `repo360` package, isolated virtual environment, and `~/.repo360/` configuration and secrets while preserving generated reports in `./repo360-output/`.

---

### Multi-Organization Support

Install the registered GitHub App across multiple organizations in your enterprise using the same App ID and private key:
- **Automatic Multi-Org Discovery:** Running `repo360 audit` (or `activity` / `runners`) automatically discovers every organization where the app is installed, provisions scoped tokens per organization, and aggregates findings into a single executive workbook.
- **Targeted Organization Filtering:** Restrict scans to specific organizations on demand using `--org`:
  ```bash
  # Audit specific organizations
  repo360 audit --org OrgAlpha,OrgBeta

  # Analyze user activity across specific organizations
  repo360 activity --org OrgAlpha,OrgBeta

  # Analyze runner fleet across specific organizations
  repo360 runners --org OrgAlpha,OrgBeta

  # Profile storage and workflow artifacts across specific organizations
  repo360 finops --org OrgAlpha,OrgBeta

  # Measure DORA delivery metrics across specific organizations
  repo360 dora --org OrgAlpha,OrgBeta

  # Audit security and ruleset bypasses across specific organizations
  repo360 bypass --org OrgAlpha,OrgBeta
  ```

---

### Method 2: GitHub Actions (CI/CD)

Repo360 is packaged as a composite action that sets up the environment, executes requested scans, and uploads reports as workflow artifacts.

#### 1. Configure Secrets & Variables
In repository **Settings** &rarr; **Secrets and variables** &rarr; **Actions**:
- `REPO360_APP_ID`: Numeric GitHub App ID (Variable or Secret)
- `REPO360_APP_PRIVATE_KEY`: RSA private key `.pem` string (Secret)
- `AI_API_KEY`: API key for Google Gemini, OpenAI, or Anthropic (Secret, required for audit)

#### 2. Workflow Example (`.github/workflows/audit.yml`)

```yaml
name: "Repo360 Platform Audit"

on:
  workflow_dispatch:
  schedule:
    - cron: "0 6 * * 1" # Weekly: Mondays at 06:00 UTC

permissions:
  contents: read

jobs:
  governance-audit:
    name: "Execute Advisory Audit"
    runs-on: ubuntu-latest

    steps:
      - name: "Run Repo360 Action"
        uses: Adithya-Meda/repo360@main
        with:
          app_id: ${{ vars.REPO360_APP_ID }} # GitHub App ID for authentication
          private_key: ${{ secrets.REPO360_APP_PRIVATE_KEY }} # Private key for token exchange
          ai_api_key: ${{ secrets.AI_API_KEY }} # Required for AI-tailored remediations in audit
          collect_activity: "true" # Scrapes contributor commit, PR, review, and issue metrics
          collect_runners: "true" # Audits self-hosted runner fleet compute, queues, and SLAs
          collect_finops: "true" # Profiles per-repository storage quotas, artifacts, and caches
          collect_dora: "true" # Calculates cross-org DORA flow metrics (DF, Lead Time, CFR, MTTR)
          collect_bypass: "true" # Audits secret scanning push protection & ruleset bypass ledgers
```

---

## Audited Checks & Platform Coverage

- **Org Governance:** Organization rulesets, secret scanning push protection, runner group least privilege isolation, default workflow token permissions.
- **Repository Security:** Branch protection review gates, `CODEOWNERS` validation, GitHub Actions commit SHA pinning, workflow concurrency groups, Dependabot.
- **Developer Enablement:** Dev containers (`devcontainer.json`), GitHub Copilot instructions, standardized issue & PR templates, pre-commit hooks.
- **User Activity Collection:** Contributor commit cadence, pull requests opened/merged, code review participation, issue discussions, active days, and adoption metrics.
- **Runner Fleet Intelligence:** Actions self-hosted runner availability, ephemeral provisioning, execution compute hours, queue latency, and SLA breach tracking (>60s).
- **FinOps & Storage Profiling:** Per-repository Git storage quotas, GitHub Actions workflow artifacts, default 90-day retention waste detection, stale Actions caches (>30d unaccessed), and container/package registries.
- **DORA & Flow Metrics:** Cross-organization Deployment Frequency (DF), Lead Time for Changes (LTTC), Change Failure Rate (CFR), and Mean Time to Recovery (MTTR) with Google DORA performance tier classification (Elite, High, Medium, Low).
- **Security & Policy Bypass Ledger:** Consolidated push protection bypass records (with secret type, bypassing actor, timestamp, and justification reason) and branch ruleset override audit history.

---

## License

[MIT License](LICENSE)
