# Gemini Enterprise License Provisioner for Google Workspace

A small, serverless service that keeps **Gemini Enterprise** license assignments in sync
with **Google Group** membership. It runs on Google Cloud Run, is driven by Cloud
Scheduler, and talks to Google Workspace through Domain‑Wide Delegation. An admin web UI
handles configuration, ad‑hoc runs, and history.

Deploy it to any GCP project and any Workspace domain — nothing in the repo is tied to a
particular tenant. Start with **[setup_instructions.md](setup_instructions.md)**;
**[EXAMPLE_DEPLOYMENT.md](EXAMPLE_DEPLOYMENT.md)** is the same guide with every value
filled in.

---

## What it does

- On a schedule (Cloud Scheduler cron) or on demand, reads the **direct members** of a
  configured set of Google Groups.
- Deduplicates users, then **skips anyone who already holds a license** from the
  selected subscription and **assigns it to the rest** (one additive batch call).
- Records every run — counts, per‑item errors, timing — to Firestore and, optionally,
  emails a full report.

## What it does not do

- **No deprovisioning.** Licenses are only added. Removal is left to normal offboarding.
- **No nested‑group expansion.** A group that contains another group is skipped and
  flagged as an error in the run history (`Nested groups are not supported for licensing`).
- **No user creation / directory changes.** It only reads the directory and manages
  Gemini Enterprise licenses via the Discovery Engine API.

---

## Architecture

```
   ┌───────────────────────┐   cron → Cloud Run Admin API (:run)
   │   Cloud Scheduler     │──────────────────────────────┐
   └───────────────────────┘                              ▼
                                          ┌──────────────────────────────┐
      (optional) ┌──────────────────┐      │  Cloud Run Job               │
   Google SSO ──►│ Identity-Aware   │      │  `python -m app.job_runner`  │
                 │ Proxy (IAP)      │      │  → runs the sync engine      │
                 └────────┬─────────┘      └──────────────┬───────────────┘
                          ▼                               │ (same image)
   ┌─────────────────────────────────────────────────────────┐
   │  Cloud Run — one container (FastAPI)                     │
   │                                                         │
   │  Admin UI                     Sync engine               │
   │  • Dashboard / history        • Read direct members     │
   │  • Group selection            • Dedupe users            │
   │  • Schedule + notifications   • Skip already-licensed    │
   │  • License subscription       • Batch-assign the rest    │
   │  • DWD connectivity test      • Flag nested groups       │
   │  • Super-admin auth check     • Record run + notify      │
   └───────┬──────────────────┬───────────────────┬──────────┘
           ▼                  ▼                   ▼
   ┌──────────────┐   ┌─────────────────┐  ┌──────────────────────┐
   │  Firestore   │   │ Workspace APIs  │  │  Gmail API           │
   │ • config     │   │ • Admin SDK     │  │  (run-report email,  │
   │ • sync_hist. │   │   Directory     │  │   optional)          │
   └──────────────┘   │ Discovery Engine│  └──────────────────────┘
                      │ • license mgmt  │
                      └─────────────────┘
   Build/deploy: GitHub Actions → Workload Identity Federation → Artifact Registry → Cloud Run
```

| Component | Role |
| :--- | :--- |
| **Cloud Run service** (`app/`) | Single FastAPI container: admin UI + `POST /api/sync/run` (manual "Run sync now" button) |
| **Cloud Run job** (`app/job_runner.py`) | Same image, command `python -m app.job_runner`; runs the sync engine for scheduled runs — no HTTP, no IAP in the path |
| **Cloud Scheduler** | On a cron schedule, calls the Cloud Run Admin API to execute the job (OAuth token, `roles/run.invoker` on the job) |
| **Firestore** (Native mode) | `config/gemini_provisioner` (settings) and `sync_history/*` (run logs) |
| **Domain‑Wide Delegation** | The runtime service account impersonates a Workspace admin — **keyless**, via the IAM Credentials API — to read groups/members (Admin SDK Directory) and, optionally, send report email (Gmail) |
| **Gemini Enterprise licensing** | The runtime service account calls the **Discovery Engine API** directly (no DWD) to list license subscriptions and check/assign user licenses |
| **Identity‑Aware Proxy** (optional) | Authenticates access; the app additionally requires the user to be a Workspace **super admin** |
| **GitHub Actions + WIF** | Builds the image and deploys to Cloud Run with no long‑lived keys |
| **Terraform** (`terraform/`) | Reference infrastructure‑as‑code for the same resources; the CI pipeline deploys with `gcloud`, so Terraform is optional |

---

## Repository layout

```
├── app/
│   ├── main.py               # FastAPI app: routes, middleware, template wiring
│   ├── config.py             # Settings (env-var backed) + AUTH_ENABLED
│   ├── auth.py               # IAP JWT verification + Workspace super-admin check
│   ├── workspace_client.py   # DWD credentials + Directory / Gmail clients
│   ├── gemini_licensing.py   # Gemini Enterprise license configs + user licenses (Discovery Engine)
│   ├── sync_worker.py        # Core sync engine
│   ├── job_runner.py         # Batch entrypoint for scheduled runs (Cloud Run job)
│   ├── notifications.py      # Post-run email report (Gmail API)
│   ├── firestore_db.py       # Config + run-history persistence
│   ├── scheduler_service.py  # Reads/updates the Cloud Scheduler job from the UI
│   ├── templates/*.html      # Server-rendered Jinja2 views (Tailwind via CDN)
│   └── static/css/custom.css
├── tests/                    # pytest: test_api, test_auth, test_gemini_licensing, test_job_runner, test_notifications, test_sync
├── scripts/
│   ├── setup_wif.sh          # One-time Workload Identity Federation bootstrap
│   └── test_local.py         # Dependency-free smoke test of the sync engine
├── terraform/                # main.tf, variables.tf, outputs.tf, terraform.tfvars.example
├── .github/workflows/deploy.yml   # Build + deploy pipeline (repo-variable driven)
├── Dockerfile
├── requirements.txt
├── setup_instructions.md     # Full deployment guide
└── EXAMPLE_DEPLOYMENT.md     # The guide with example values filled in
```

---

## Dependencies

### Runtime (Python, `requirements.txt`)

| Package | Why |
| :--- | :--- |
| `fastapi`, `uvicorn[standard]` | Web framework + ASGI server |
| `jinja2`, `python-multipart` | Server-rendered templates, form parsing |
| `pydantic`, `pydantic-settings` | Typed settings from environment variables |
| `google-cloud-firestore` | Config + run history |
| `google-cloud-scheduler` | Read/update the schedule from the UI |
| `google-api-python-client` | Admin SDK Directory, Gmail |
| `google-auth`, `google-auth-oauthlib`, `google-auth-httplib2` | Auth: keyless DWD (IAM Credentials signer), IAP JWT verification |
| `requests` | Transitive HTTP needs |
| `pytest`, `httpx` | Test suite only (kept here for convenience) |

Front-end assets (Tailwind, Font Awesome) load from public CDNs at page render time — no
Node/npm build.

### Google Cloud APIs (enabled during setup)

`run`, `cloudscheduler`, `firestore`, `artifactregistry`, `cloudbuild`, `iam`,
`iamcredentials`, `admin` (Admin SDK Directory), `discoveryengine` (Gemini Enterprise
license management). IAP (`iap`) and Gmail (`gmail`) only if you use those features.

### Google Cloud resources

One project with billing, a Firestore database (Native mode), an Artifact Registry
Docker repo, one runtime service account (also used by CI), a Cloud Scheduler service
account, a Cloud Run service, a Cloud Run job (scheduled sync), a Cloud Scheduler job
(the cron trigger), and a Workload Identity pool/provider for GitHub Actions. All
created by `setup_instructions.md` (or `terraform/`).

### Google Workspace

- A Cloud Identity / Workspace tenant on your domain.
- A **super administrator** to register Domain‑Wide Delegation once.
- A real, active, licensed **delegated‑admin user** the service impersonates at runtime.
- A **Gemini Enterprise** subscription in the GCP project (Gemini Enterprise console →
  *Manage subscriptions*) with free seats.

### CI/CD

GitHub Actions with repository **secrets** `WIF_PROVIDER`, `WIF_SERVICE_ACCOUNT` and the
repository **variables** in the table below.

---

## Configuration reference

All configuration is environment variables on the Cloud Run service. Terraform and the
deploy workflow set them from **GitHub Actions repository variables** (see
[setup_instructions.md](setup_instructions.md)); a few can also be changed at runtime
from the admin UI, which persists them to Firestore.

| Variable | Required | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `GCP_PROJECT_ID` | yes | — | Project for Firestore/Scheduler clients |
| `GCP_REGION` | no | `us-central1` | Region for the Scheduler client |
| `RUNTIME_SERVICE_ACCOUNT_EMAIL` | yes (on Cloud Run) | — | The attached service account; needed for keyless DWD |
| `DELEGATED_ADMIN_EMAIL` | yes | placeholder | Workspace admin the service impersonates (also editable on the Settings page) |
| `LICENSE_CONFIG` | no | unset | Optional headless default for the Gemini Enterprise subscription (a Discovery Engine license config resource name); normally chosen on the Settings page |
| `CLOUD_SCHEDULER_JOB_NAME` | no | `gemini-license-sync-job` | Cron trigger job the UI reads/updates |
| `IAP_AUDIENCE` | no | unset | **Turns on** IAP + super-admin enforcement; the IAP JWT `aud` to verify |
| `SYNC_INVOKER_SA_EMAIL` | no | unset | Optional: a service account allowed through `POST /api/sync/run` when enforcement is on (scheduled runs use the Cloud Run job, not this) |
| `AUTH_BOOTSTRAP_ADMINS` | no | empty | Comma-separated break-glass admin emails |
| `SUPER_ADMIN_CACHE_TTL` | no | `300` | Seconds to cache each super-admin lookup |
| `NOTIFICATION_SENDER_EMAIL` | no | delegated admin | Mailbox that run-report emails are sent as |
| `PUBLIC_BASE_URL` | no | learned from traffic | Base URL for links in emails; set explicitly behind a custom domain |
| `SERVICE_ACCOUNT_KEY_JSON` / `SERVICE_ACCOUNT_KEY_PATH` | no | unset | Local dev only: a DWD-enabled SA key instead of keyless impersonation |
| `DEBUG` | no | `false` | Verbose logging |

Repository variables consumed by `.github/workflows/deploy.yml`: `GCP_PROJECT_ID`
(required), `GCP_REGION`, `CLOUD_RUN_SERVICE`, `ARTIFACT_REPO`, `CLOUD_SCHEDULER_JOB`,
`CLOUD_RUN_JOB` (scheduled-sync job, default `gemini-license-sync-runner`),
`DELEGATED_ADMIN_EMAIL`, `LICENSE_CONFIG`, `IAP_AUDIENCE`, `SYNC_INVOKER_SA_EMAIL`,
`AUTH_BOOTSTRAP_ADMINS`, `SUPER_ADMIN_CACHE_TTL`, `NOTIFICATION_SENDER_EMAIL`,
`PUBLIC_BASE_URL`, `CLOUD_RUN_ALLOW_UNAUTH` (`false` after enabling IAP),
`CLOUD_RUN_ENABLE_IAP` (`true`/`false` to toggle IAP on deploy).

---

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest tests/ -v            # unit tests, no cloud access
python scripts/test_local.py   # dependency-mocked smoke test of the sync engine
```

To run the app locally against real Google APIs you need DWD credentials. Keyless
impersonation only works on GCP, so locally supply a service‑account key that has DWD:

```bash
export GCP_PROJECT_ID="your-project"
export DELEGATED_ADMIN_EMAIL="workspace-admin@your-domain.com"
export SERVICE_ACCOUNT_KEY_JSON="$(cat path/to/dwd-sa-key.json)"
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Without credentials the UI still renders; calls that hit Google APIs will error.

---

## Deployment

See **[setup_instructions.md](setup_instructions.md)** for the full walkthrough (GCP
project, Firestore, service account, DWD, Workload Identity Federation, GitHub Actions,
first configuration, notifications, and IAP). A pushed commit to `main` builds and
deploys automatically.

### Privileges to deploy (summary)

- **GCP operator**: `roles/owner`, or `serviceusage.serviceUsageAdmin` +
  `datastore.owner` + `iam.serviceAccountAdmin` + `resourcemanager.projectIamAdmin` +
  `run.admin` + `artifactregistry.admin` + `cloudscheduler.admin` +
  `iam.workloadIdentityPoolAdmin`.
- **Runtime/CI service account**: `datastore.user`, `discoveryengine.admin`,
  `cloudscheduler.admin`, `logging.logWriter`, `run.admin`, `artifactregistry.admin`,
  `iam.serviceAccountUser`, and `iam.serviceAccountTokenCreator` **on itself** (keyless DWD).
- **Workspace**: a super admin to register DWD once, plus the delegated‑admin user.

Full rationale: [setup_instructions.md → Required Privileges](setup_instructions.md#required-privileges).

---

## Security model

Optional, two layers, enabled together:

1. **Identity‑Aware Proxy** authenticates every request to the web service (Google SSO
   for browsers) and forwards a signed assertion.
2. **The app** verifies that assertion and requires the user to be an active Google
   Workspace **super administrator** (`isAdmin`); everyone else gets `403`.

Scheduled sync does **not** go through the web service or IAP — Cloud Scheduler executes
the Cloud Run job directly. `SYNC_INVOKER_SA_EMAIL` still lets a named service account
call `POST /api/sync/run` if you ever need a second HTTP trigger, but the schedule no
longer relies on it.

Enforcement turns on when `IAP_AUDIENCE` is set. **Until then the UI and all `/api/*`
endpoints are public** (`--allow-unauthenticated` + `allUsers` invoker) — acceptable
only for an initial smoke test. Setup and hardening notes:
[setup_instructions.md → Security Model](setup_instructions.md#security-model).

---

## Operations

- **Run history**: the **Run History** page (and `sync_history` in Firestore) has every
  run with status, counts, timing, and per‑item errors.
- **Failures don't stop the service.** A run that hits errors is recorded as `FAILED` or
  `PARTIAL_SUCCESS`; the next scheduled run proceeds normally. Common causes: no seats
  left on the subscription, a nested group, a suspended user.
- **Notifications** (optional): configure recipients on the **Sync Schedule** page and
  choose "all runs" or "failures only". Requires the `gmail.send` DWD scope.
- **Change the schedule**: the **Sync Schedule** page updates both Firestore and the
  Cloud Scheduler job.
- **Rotate the delegated admin**: update it on the **Settings** page (or the
  `DELEGATED_ADMIN_EMAIL` variable) — it must remain a real, active, licensed admin.
- **Logs**: `gcloud run services logs read <service> --region <region>`; app loggers are
  namespaced `gemini_provisioner.*`.

---

## Testing

`pytest tests/` covers the sync engine (dedupe, nested‑group handling), the API
endpoints and views, the notification builder/dispatch, and the auth gate
(IAP verification, super‑admin lookup + caching + fail‑closed, break‑glass).
`scripts/test_local.py` runs the sync engine with all cloud dependencies mocked.

---

## Status

Open source, run without a formal SLA. Review
[setup_instructions.md → Security Model](setup_instructions.md#security-model) before any
non‑trivial use, and treat the runtime service account as highly privileged.

---

## Contributing

Bug reports, feature requests, and pull requests are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md) for how to propose a change and the local dev/test
setup. Every pull request requires review from a code owner (see
[`.github/CODEOWNERS`](.github/CODEOWNERS)) before it can be merged. Found a security
issue? See [SECURITY.md](SECURITY.md) instead of filing a public issue.

## License

Licensed under the [Apache License 2.0](LICENSE).
