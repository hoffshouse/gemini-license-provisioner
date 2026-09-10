# Worked Example

[`setup_instructions.md`](setup_instructions.md) with every placeholder filled in, using
a **fictional** tenant (`acme-licensing-prod` / `acme.example`). Nothing here is real or
required — substitute your own values throughout.

## Values

```bash
export PROJECT_ID="acme-licensing-prod"
export REGION="us-central1"
export SA_NAME="sa-gemini-provisioner"
export ARTIFACT_REPO="gemini-provisioner-docker"
export SERVICE_NAME="gemini-license-provisioner"
export SCHEDULER_JOB="gemini-license-sync-job"

export GITHUB_REPO="acme-corp/gemini-license-provisioner"

export WORKSPACE_DOMAIN="acme.example"
export DELEGATED_ADMIN_EMAIL="ws-provisioner@acme.example"   # real, active, licensed super admin

export SA_EMAIL="sa-gemini-provisioner@acme-licensing-prod.iam.gserviceaccount.com"
```

Project number for this example: `750123456789`.

## GitHub configuration

**Secrets** (Settings → Secrets and variables → Actions → Secrets):

| Secret | Value |
| :--- | :--- |
| `WIF_PROVIDER` | `projects/750123456789/locations/global/workloadIdentityPools/github-actions-pool/providers/github-provider` |
| `WIF_SERVICE_ACCOUNT` | `sa-gemini-provisioner@acme-licensing-prod.iam.gserviceaccount.com` |

**Variables** (same screen, Variables tab):

| Variable | Value |
| :--- | :--- |
| `GCP_PROJECT_ID` | `acme-licensing-prod` |
| `GCP_REGION` | `us-central1` |

`CLOUD_RUN_SERVICE`, `ARTIFACT_REPO`, `CLOUD_SCHEDULER_JOB` are left unset here because
the workflow defaults already match. `DELEGATED_ADMIN_EMAIL` is left unset and set on the
app's **Settings** page instead.

## Domain-Wide Delegation

Admin Console → Security → Access and data control → API controls →
Domain-wide delegation → Add new:

| Field | Value |
| :--- | :--- |
| Client ID | `109876543210987654321` (the service account's numeric `uniqueId`) |
| OAuth scopes | `https://www.googleapis.com/auth/admin.directory.group.readonly,https://www.googleapis.com/auth/admin.directory.user.readonly,https://www.googleapis.com/auth/gmail.send` |

`gmail.send` is only needed for run-notification emails. Gemini Enterprise licensing
does not use DWD.

Delegated admin impersonated at runtime: `ws-provisioner@acme.example`.

## Result

- Cloud Run service: `https://gemini-license-provisioner-abcde12345-uc.a.run.app`
- Settings / Test Connection: `…/settings`
- Runtime + CI/CD service account `sa-gemini-provisioner@acme-licensing-prod.iam.gserviceaccount.com`
  with `datastore.user`, `discoveryengine.admin`, `cloudscheduler.admin`,
  `logging.logWriter`, `run.admin`, `artifactregistry.admin`, `iam.serviceAccountUser`,
  and `iam.serviceAccountTokenCreator` on itself.
- On the **Settings** page, pick the Gemini Enterprise license subscription
  (e.g. `free_trial_gemini`) from the dropdown and Save.

## Locking it down (IAP + super-admin)

Terraform:

```hcl
enable_iap          = true
iap_audience        = "/projects/750123456789/locations/us-central1/services/gemini-license-provisioner"
iap_oauth_client_id = "750123456789-abc123def456.apps.googleusercontent.com"
# iap_members defaults to ["domain:acme.example"]
```

Or, with IAP enabled from the console, repository variables:

| Variable | Value |
| :--- | :--- |
| `CLOUD_RUN_ENABLE_IAP` | `true` |
| `IAP_AUDIENCE` | the IAP JWT `aud` (see [Security Model](setup_instructions.md#security-model)) |
| `SYNC_INVOKER_SA_EMAIL` | `sa-scheduler-invoker@acme-licensing-prod.iam.gserviceaccount.com` |
| `AUTH_BOOTSTRAP_ADMINS` | `ws-provisioner@acme.example` (break-glass) |
| `CLOUD_RUN_ALLOW_UNAUTH` | `false` |

Result: only active `isAdmin` users on `acme.example` can open the app; anyone else IAP
lets through gets `403` from the app.
