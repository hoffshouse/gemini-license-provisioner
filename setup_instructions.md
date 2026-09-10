# Setup Instructions: Google Workspace Gemini Enterprise License Provisioner

Step-by-step setup for Google Cloud, Google Workspace Domain-Wide Delegation (DWD),
Workload Identity Federation (WIF), and GitHub Actions.

Every command below uses shell variables for the values that are specific to **your**
deployment. Nothing in this repo is tied to a particular GCP project or Workspace
domain — see [`EXAMPLE_DEPLOYMENT.md`](EXAMPLE_DEPLOYMENT.md) for one filled-in example.

---

## 0. Fill in your values

Set these once in your shell; the rest of the guide references them.

```bash
# ── GCP ────────────────────────────────────────────────────────────────────────
export PROJECT_ID="your-gcp-project-id"            # GCP project to deploy into
export REGION="us-central1"                        # region for Cloud Run / Artifact Registry / Scheduler
export SA_NAME="sa-gemini-provisioner"             # name for the app + CI/CD service account (created below)
export ARTIFACT_REPO="gemini-provisioner-docker"   # Artifact Registry repository name
export SERVICE_NAME="gemini-license-provisioner"   # Cloud Run service name
export SCHEDULER_JOB="gemini-license-sync-job"     # Cloud Scheduler job name

# ── GitHub ────────────────────────────────────────────────────────────────────
export GITHUB_REPO="your-org/your-repo"            # repo that hosts this code (used by WIF)

# ── Google Workspace ──────────────────────────────────────────────────────────
export WORKSPACE_DOMAIN="your-domain.com"          # your Workspace primary domain
export DELEGATED_ADMIN_EMAIL="workspace-admin@${WORKSPACE_DOMAIN}"  # REAL, active, licensed admin user to impersonate

# ── Derived (do not edit) ─────────────────────────────────────────────────────
export SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud auth login
gcloud config set project "${PROJECT_ID}"
```

> `DELEGATED_ADMIN_EMAIL` **must resolve to an existing user** in your Workspace
> tenant. A made-up address fails at sync time with
> `invalid_grant: Invalid email or User ID`.

---

## Prerequisites

1. **GCP project** `${PROJECT_ID}` with billing enabled, and an operator identity holding the roles under [Required Privileges](#required-privileges).
2. **Google Workspace / Cloud Identity** tenant on `${WORKSPACE_DOMAIN}`, plus a **Super Admin** account to configure Domain-Wide Delegation.
3. **A dedicated delegated-admin user** (`${DELEGATED_ADMIN_EMAIL}`) — real, active, licensed — for the service to impersonate.
4. **GitHub repository** `${GITHUB_REPO}` with permission to add Actions secrets and variables.
5. **Local tooling**: `gcloud` and `git`.

---

## Required Privileges

### Google Cloud — operator (the person running Steps 1–5)

Granted on project `${PROJECT_ID}`. Either `roles/owner`, or this least-privilege set:

| Role | Needed for |
| :--- | :--- |
| `roles/serviceusage.serviceUsageAdmin` | Enable APIs (Step 1) |
| `roles/datastore.owner` | Create the Firestore database (Step 2) |
| `roles/iam.serviceAccountAdmin` | Create the service account and set IAM policy **on** it (Steps 3, 5) |
| `roles/resourcemanager.projectIamAdmin` | Grant project roles to the service account (Steps 3, 5) |
| `roles/artifactregistry.admin` | Create the Docker repository (Terraform / first deploy) |
| `roles/run.admin` | Create the Cloud Run service (first deploy) |
| `roles/iam.serviceAccountUser` on the runtime service account | Deploy Cloud Run "acting as" that account |
| `roles/cloudscheduler.admin` | Create the Cloud Scheduler job |
| `roles/iam.workloadIdentityPoolAdmin` | Create the WIF pool/provider (Step 5) |

### Google Cloud — the app service account `${SA_NAME}@${PROJECT_ID}` (runtime **and** CI/CD)

One service account both runs the Cloud Run service and is the identity GitHub Actions
impersonates to deploy. Steps 3 and 5 (`scripts/setup_wif.sh`) grant it:

| Role | Scope | Purpose |
| :--- | :--- | :--- |
| `roles/datastore.user` | project | Read/write config and sync history in Firestore |
| `roles/cloudscheduler.admin` | project | The **Sync Schedule** page edits the scheduler job at runtime |
| `roles/logging.logWriter` | project | Structured logs |
| `roles/iam.serviceAccountTokenCreator` | **on itself** | Sign JWTs for **keyless Domain-Wide Delegation** — without it, Test Connection returns `404: Domain not found` |
| `roles/run.admin` | project | GitHub Actions deploys new revisions |
| `roles/iam.serviceAccountUser` | on itself | GitHub Actions deploys Cloud Run as this account |
| `roles/artifactregistry.admin` | project | GitHub Actions pushes container images |

> For a stricter setup, split deployment onto a separate CI service account holding only
> `run.admin`, `artifactregistry.writer`, and `iam.serviceAccountUser` on the runtime
> account, and drop `run.admin` / `artifactregistry.admin` / `iam.serviceAccountUser`
> from the runtime account.

### Google Workspace

| Privilege | Held by | Needed for |
| :--- | :--- | :--- |
| **Super Admin** (one-time) | the admin doing Step 4 | Add the Domain-Wide Delegation entry in the Admin Console. DWD cannot be delegated to a custom admin role. |
| Admin roles: **Groups → Read**, **Users → Read**, and license management (Super Admin covers all three) | the delegated-admin user (`${DELEGATED_ADMIN_EMAIL}`) | The app calls Admin SDK Directory (`admin.directory.group.readonly`, `admin.directory.user.readonly`) and Enterprise License Manager (`apps.licensing`) **as this user** |
| An assignable **Gemini Enterprise** SKU with available seats | the Workspace tenant | Licenses to hand out during sync |

The delegated-admin user must be **active** (not suspended) and licensed. A Super Admin
account is the simplest choice; a custom admin role works only if it grants the Directory
read and licensing privileges above.

---

## Step 1: Enable Required Google Cloud APIs

```bash
gcloud services enable \
  admin.googleapis.com \
  licensing.googleapis.com \
  cloudscheduler.googleapis.com \
  firestore.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  cloudbuild.googleapis.com \
  --project="${PROJECT_ID}"
```

---

## Step 2: Initialize Cloud Firestore in Native Mode

If Firestore is not yet active in `${PROJECT_ID}`:

1. In the Google Cloud Console, open **Firestore**.
2. Click **Create Database**.
3. Select **Firestore Native mode**.
4. Choose a location (use `${REGION}` or your preference) and database id `(default)`.
5. Click **Create Database**.

---

## Step 3: Create the Service Account & Grant GCP Roles

This one account both **runs** the Cloud Run service and is impersonated by
**GitHub Actions** to deploy it; see [Required Privileges](#required-privileges) for
the rationale behind each role. `scripts/setup_wif.sh` (Step 5) applies the same
project-level bindings, so you can skip straight to Step 5 if you use it.

```bash
# Create the service account
gcloud iam service-accounts create "${SA_NAME}" \
  --display-name="Gemini License Provisioner Service Account" \
  --project="${PROJECT_ID}"

# Project-level roles (runtime + CI/CD deploy)
for ROLE in \
  roles/datastore.user \
  roles/cloudscheduler.admin \
  roles/logging.logWriter \
  roles/run.admin \
  roles/artifactregistry.admin \
  roles/iam.serviceAccountUser
do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" --role="${ROLE}" --condition=None
done

# Allow the service account to mint signed JWTs as itself (keyless Domain-Wide
# Delegation). Without this the deployed service authenticates as the bare service
# account and the Directory API returns "404: Domain not found" on Test Connection.
gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --project="${PROJECT_ID}"
```

> **Runtime environment variables** the Cloud Run service needs:
> - `RUNTIME_SERVICE_ACCOUNT_EMAIL` = the service account email (`${SA_EMAIL}`) — required for keyless DWD.
> - `DELEGATED_ADMIN_EMAIL` = `${DELEGATED_ADMIN_EMAIL}` — the user to impersonate (can also be set later from the **Settings** page).
> - `GCP_PROJECT_ID`, `GCP_REGION` — your project and region.
> - *(optional)* `NOTIFICATION_SENDER_EMAIL` — mailbox that run-notification emails are sent as; defaults to the delegated admin.
> - *(optional)* `PUBLIC_BASE_URL` — public https URL of the service, for links in those emails; the app also learns this from web traffic.
>
> Terraform and the GitHub Actions workflow set these for you. If you run
> `gcloud run deploy` by hand, pass them all in `--set-env-vars`.

Retrieve the service account's **Unique Numeric Client ID** (needed for Step 4):

```bash
gcloud iam service-accounts describe "${SA_EMAIL}" \
  --project="${PROJECT_ID}" \
  --format="value(uniqueId)"
```
*Note down this numeric ID (a ~21-digit number).*

---

## Step 4: Authorize Domain-Wide Delegation (DWD) in Google Workspace

Lets the service account read Google Groups and assign Gemini licenses to users in
`${WORKSPACE_DOMAIN}`:

1. Sign in to the [Google Admin Console](https://admin.google.com) as a **Super Administrator**.
2. Go to **Security** &gt; **Access and data control** &gt; **API controls**.
3. Under **Domain-wide delegation**, click **Manage Domain Wide Delegation**.
4. Click **Add new**.
5. **Client ID**: paste the Unique Numeric Client ID from Step 3.
6. **OAuth Scopes** (comma-delimited):
   ```text
   https://www.googleapis.com/auth/admin.directory.group.readonly,https://www.googleapis.com/auth/admin.directory.user.readonly,https://www.googleapis.com/auth/apps.licensing,https://www.googleapis.com/auth/gmail.send
   ```
   `gmail.send` is only needed for **run notification emails** (Step 8). Omit it if
   you will not use notifications — everything else still works.
7. Click **Authorize**.

---

## Step 5: Configure Workload Identity Federation (WIF) for GitHub Actions

WIF lets GitHub Actions deploy without a long-lived service-account key.

```bash
cd gemini-license-provisioner
PROJECT_ID="${PROJECT_ID}" SA_NAME="${SA_NAME}" REGION="${REGION}" \
  bash scripts/setup_wif.sh "${GITHUB_REPO}"
```

`setup_wif.sh` creates the service account (if missing), applies the project-level
roles from Step 3, grants `roles/iam.serviceAccountTokenCreator` on the account to
itself, and creates the Workload Identity pool/provider.

It prints two values. Add them under **GitHub → Settings → Secrets and variables →
Actions → Repository secrets**:

| Secret | Value |
| :--- | :--- |
| `WIF_PROVIDER` | `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github-actions-pool/providers/github-provider` |
| `WIF_SERVICE_ACCOUNT` | the service account email (`${SA_EMAIL}`) |

Then add the deployment **Repository variables** (same screen, **Variables** tab) — the
workflow reads these so nothing environment-specific is committed:

| Variable | Value | Default if unset |
| :--- | :--- | :--- |
| `GCP_PROJECT_ID` | `${PROJECT_ID}` | *(required)* |
| `GCP_REGION` | `${REGION}` | `us-central1` |
| `CLOUD_RUN_SERVICE` | `${SERVICE_NAME}` | `gemini-license-provisioner` |
| `ARTIFACT_REPO` | `${ARTIFACT_REPO}` | `gemini-provisioner-docker` |
| `CLOUD_SCHEDULER_JOB` | `${SCHEDULER_JOB}` | `gemini-license-sync-job` |
| `DELEGATED_ADMIN_EMAIL` | `${DELEGATED_ADMIN_EMAIL}` | *(unset — set it on the Settings page instead)* |

The workflow also forwards these optional variables when set:
`NOTIFICATION_SENDER_EMAIL`, `PUBLIC_BASE_URL` (Step 8) and `IAP_AUDIENCE`,
`SYNC_INVOKER_SA_EMAIL`, `AUTH_BOOTSTRAP_ADMINS`, `SUPER_ADMIN_CACHE_TTL`,
`CLOUD_RUN_ENABLE_IAP`, `CLOUD_RUN_ALLOW_UNAUTH` (Security Model). Full list and meanings:
[README → Configuration reference](README.md#configuration-reference).

---

## Step 6: Push Code to Trigger Deployment

```bash
cd gemini-license-provisioner
git remote add origin "https://github.com/${GITHUB_REPO}.git"   # if not already set
git push origin main
```

Pushing to `main` runs `.github/workflows/deploy.yml`: build the image, push it to
Artifact Registry, and deploy to Cloud Run.

---

## Step 7: Initial Configuration in the Admin Dashboard

1. Open the Cloud Run URL printed at the end of the workflow (or from the GCP Console).
2. **Settings &amp; Test**:
   - Set **Delegated Admin Email** to `${DELEGATED_ADMIN_EMAIL}` and **Save**.
   - Confirm **Product ID** (`Google-Apps` or `101047`) and **SKU ID** (`101031` or `1010470001`).
   - Click **Test Connection**.
3. **Monitored Groups**: select the Google Groups to track, then **Save**.
4. **Sync Schedule**: confirm/adjust the cron frequency, then **Update Cloud Scheduler**.
5. Click **Run Sync Now** for the first provisioning cycle.

---

## Step 8: Run Notifications (optional)

On the **Sync Schedule** page, under **Run Notifications**:

- Enter one or more recipient email addresses (comma- or newline-separated).
- Tick **Alert on all activity** to be emailed after every run; leave it unticked
  to be emailed only for **failed** and **partial-success** runs.

After each sync the service emails a full report — status, start/finish time,
trigger source, monitored groups, per-category counts, every error, and a link
back to the Run History page.

Requirements:

- `gmail.send` in the Domain-Wide Delegation scopes (Step 4).
- The sender mailbox is the delegated admin by default; override with
  `NOTIFICATION_SENDER_EMAIL`.
- Links use `PUBLIC_BASE_URL` if set, otherwise the `*.run.app` URL the app last
  saw serving web traffic. **Set `PUBLIC_BASE_URL` explicitly if you front the
  service with a custom domain** (a client-supplied Host header is not trusted).

Sending failures are logged (`gemini_provisioner.notifications`) and recorded on
the run, but never fail the sync itself.

---

## Security Model

Access control has two layers, both required once enabled:

1. **Identity-Aware Proxy (IAP)** authenticates the browser (Google SSO) and forwards a
   signed `x-goog-iap-jwt-assertion` header. IAP IAM (`roles/iap.httpsResourceAccessor`)
   is deliberately broad (the whole Workspace domain by default).
2. **The application** verifies that assertion against Google's public keys and the
   configured audience, extracts the email, and calls the Directory API (via the same
   DWD client used for provisioning) to confirm the user is an **`isAdmin`, non-suspended
   Google Workspace super administrator**. Anyone else gets `403`.

`POST /api/sync/run` additionally accepts the Cloud Scheduler service account
(`SYNC_INVOKER_SA_EMAIL`) so scheduled runs work. `GET /healthz` is always open (Cloud
Run probes). DWD credentials are never exposed to the browser.

**Enforcement is off until `IAP_AUDIENCE` is set** — until then every page and API is
public (`--allow-unauthenticated` + `allUsers` invoker), which is fine only for a first
smoke test. Turn it on:

### With Terraform

```hcl
enable_iap          = true
iap_audience        = "<the IAP JWT aud - see step 4 below>"
iap_oauth_client_id = "<client-id>.apps.googleusercontent.com"   # for scheduled runs
# iap_members       = ["group:workspace-admins@your-domain.com"] # optional; default is the whole domain
```

`terraform apply` enables IAP on the Cloud Run service, drops the `allUsers` invoker,
grants the IAP service agent `run.invoker`, grants `iap.httpsResourceAccessor` to
`iap_members` and the scheduler SA, sets `IAP_AUDIENCE` / `SYNC_INVOKER_SA_EMAIL`, and
points the scheduler's OIDC token at the IAP client. You still create the OAuth consent
screen (brand) once in the console if the project has none, and you still need the
audience from step 4.

### By hand (console + gcloud + CI)

1. **OAuth consent screen**: APIs & Services → OAuth consent screen → Internal (once per
   project). Most Workspace orgs already have one.
2. **Enable IAP on the service**: Cloud Run → the service → **Security** → toggle
   **Identity-Aware Proxy** on (accept the prompt to grant the IAP service agent the
   invoker role). Equivalent CLI: `gcloud run deploy ${SERVICE_NAME} --iap ...` (needs a
   recent gcloud) or set repository variable `CLOUD_RUN_ENABLE_IAP=true` and redeploy.
3. **Grant access through IAP**:
   ```bash
   gcloud iap web add-iam-policy-binding --resource-type=cloud-run \
     --service=${SERVICE_NAME} --region=${REGION} --project=${PROJECT_ID} \
     --member="domain:${WORKSPACE_DOMAIN}" --role="roles/iap.httpsResourceAccessor"
   gcloud iap web add-iam-policy-binding --resource-type=cloud-run \
     --service=${SERVICE_NAME} --region=${REGION} --project=${PROJECT_ID} \
     --member="serviceAccount:<scheduler-sa>" --role="roles/iap.httpsResourceAccessor"
   ```
   Use a `group:` instead of `domain:` to narrow it — the app still enforces super-admin
   on top.
4. **Find the IAP JWT audience** (the value for `IAP_AUDIENCE`). For a Cloud Run service
   with IAP enabled directly there is no load-balancer backend service; the `aud` has the
   form `/projects/<PROJECT_NUMBER>/locations/<REGION>/services/<SERVICE_NAME>`. Confirm
   the exact string with one of:
   - **IAP console** → the resource → ⋮ → *Get JWT audience code*, or
   - deploy this app, open it in a browser as an allowed user, then
     `gcloud run services logs read ${SERVICE_NAME} --region ${REGION} | grep "IAP assertion received"`
     — it logs the exact `aud` it observed.
5. **Add repository variables** so CI keeps it on:
   | Variable | Value |
   | :--- | :--- |
   | `CLOUD_RUN_ENABLE_IAP` | `true` |
   | `IAP_AUDIENCE` | the audience from step 4 |
   | `SYNC_INVOKER_SA_EMAIL` | the Cloud Scheduler service account email |
   | `AUTH_BOOTSTRAP_ADMINS` | your own admin email (break-glass, recommended) |
   | `CLOUD_RUN_ALLOW_UNAUTH` | `false` |
6. **Repoint the scheduler** OIDC token audience to the IAP OAuth client ID
   (`gcloud scheduler jobs update http ${SCHEDULER_JOB} --location=${REGION} --oidc-token-audience=<client-id>.apps.googleusercontent.com --oidc-service-account-email=<scheduler-sa>`).
7. Redeploy (push to `main`).

### Optional knobs

- `AUTH_BOOTSTRAP_ADMINS` — comma-separated emails always allowed (break-glass if the
  Directory API is unavailable). Use sparingly.
- `SUPER_ADMIN_CACHE_TTL` — seconds to cache each super-admin lookup (default 300).

### Still worth doing

- The runtime service account is highly privileged (deploys Cloud Run, self-impersonates
  for DWD). Split CI onto a separate identity per the note in
  [Required Privileges](#required-privileges) if you don't need in-place deploys.
- Consider `ingress = internal-and-cloud-load-balancing` if you front IAP with a load
  balancer.

---

## Troubleshooting

### Test Connection returns `API Error (404): Domain not found.`

The service reached Google but called the Directory API as the bare runtime service
account instead of impersonating a Workspace admin. Check, in order:

1. `roles/iam.serviceAccountTokenCreator` on the service account **to itself** (Step 3).
2. `RUNTIME_SERVICE_ACCOUNT_EMAIL` is set on the Cloud Run service and matches the attached service account.
3. `iamcredentials.googleapis.com` is enabled (Step 1).
4. The **Delegated Admin Email** domain is a real Workspace / Cloud Identity domain and the user is an **active super administrator**.
5. The DWD entry (Step 4) uses the service account's **numeric client ID** with all three scopes.

Once impersonation works but privileges are wrong, the error changes to `401
unauthorized_client` / `403` — that points at Step 4 or the admin user's role.

### Test Connection returns `invalid_grant: Invalid email or User ID`

Impersonation works, but **Delegated Admin Email** is not a real user in the tenant.
Set it (Settings page or `DELEGATED_ADMIN_EMAIL`) to an active, licensed admin account
on your domain and save.

### Test Connection returns `403` / `unauthorized_client`

The DWD entry (Step 4) is missing, has the wrong client ID, or lacks one of the three
scopes. Re-check it against the numeric client ID from Step 3.
