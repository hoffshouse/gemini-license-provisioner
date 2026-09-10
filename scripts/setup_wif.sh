#!/usr/bin/env bash
# ==============================================================================
# setup_wif.sh
# One-time setup for Google Cloud Workload Identity Federation (WIF) so GitHub
# Actions can deploy without a long-lived service-account key.
#
# Configure via environment variables (all optional except the GitHub repo arg):
#   PROJECT_ID   GCP project id           (default: gcloud config value)
#   SA_NAME      app/CI service account   (default: sa-gemini-provisioner)
#   REGION       GCP region               (default: us-central1)
#   POOL_NAME / PROVIDER_NAME             (defaults: github-actions-pool / github-provider)
#
# Usage: PROJECT_ID=my-proj bash scripts/setup_wif.sh <github-owner>/<repo>
# ==============================================================================

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
POOL_NAME="${POOL_NAME:-github-actions-pool}"
PROVIDER_NAME="${PROVIDER_NAME:-github-provider}"
SA_NAME="${SA_NAME:-sa-gemini-provisioner}"
REGION="${REGION:-us-central1}"

if [ -z "${PROJECT_ID}" ] || [ "${PROJECT_ID}" = "(unset)" ]; then
  echo "Error: set PROJECT_ID (env var) or run 'gcloud config set project <id>' first."
  exit 1
fi

echo "==================================================================="
echo "Configuring Workload Identity Federation for GCP Project: ${PROJECT_ID}"
echo "==================================================================="

# 1. Ask for GitHub repo if not provided
if [ -z "${1:-}" ]; then
  read -rp "Enter your GitHub repository (format: owner/repo): " GITHUB_REPO
else
  GITHUB_REPO="$1"
fi

if [ -z "$GITHUB_REPO" ]; then
  echo "Error: GitHub repository cannot be empty."
  exit 1
fi

echo ">> Enabling IAM, Cloud Resource Manager, and STS APIs..."
gcloud services enable iam.googleapis.com \
  iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com \
  sts.googleapis.com \
  --project="${PROJECT_ID}"

# 2. Create Service Account if not exists
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" &>/dev/null; then
  echo ">> Creating service account ${SA_NAME}..."
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="Gemini License Provisioner Service Account" \
    --project="${PROJECT_ID}"
else
  echo ">> Service account ${SA_EMAIL} already exists."
fi

# 3. Grant necessary GCP project roles
echo ">> Granting project IAM roles to ${SA_EMAIL}..."
ROLES=(
  "roles/datastore.user"
  "roles/cloudscheduler.admin"
  "roles/run.admin"
  "roles/iam.serviceAccountUser"
  "roles/artifactregistry.admin"
  "roles/logging.logWriter"
  "roles/discoveryengine.admin"   # Gemini Enterprise license management
)

for role in "${ROLES[@]}"; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${role}" \
    --condition=None \
    --quiet &>/dev/null || true
done

# 3b. Allow the service account to sign JWTs as itself (keyless Domain-Wide Delegation).
# Without this, the deployed service authenticates as the bare service account and the
# Directory API returns "404: Domain not found" on the Test Connection button.
echo ">> Granting roles/iam.serviceAccountTokenCreator on ${SA_EMAIL} to itself..."
gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --project="${PROJECT_ID}" \
  --quiet &>/dev/null || true

# 4. Create Workload Identity Pool
if ! gcloud iam workload-identity-pools describe "${POOL_NAME}" --location="global" --project="${PROJECT_ID}" &>/dev/null; then
  echo ">> Creating Workload Identity Pool '${POOL_NAME}'..."
  gcloud iam workload-identity-pools create "${POOL_NAME}" \
    --location="global" \
    --display-name="GitHub Actions Pool" \
    --project="${PROJECT_ID}"
else
  echo ">> Workload Identity Pool '${POOL_NAME}' already exists."
fi

# 5. Create Workload Identity Provider
if ! gcloud iam workload-identity-pools providers describe "${PROVIDER_NAME}" \
    --workload-identity-pool="${POOL_NAME}" \
    --location="global" \
    --project="${PROJECT_ID}" &>/dev/null; then
  echo ">> Creating OIDC Provider for GitHub Actions..."
  gcloud iam workload-identity-pools providers create-oidc "${PROVIDER_NAME}" \
    --workload-identity-pool="${POOL_NAME}" \
    --location="global" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
    --attribute-condition="assertion.repository == '${GITHUB_REPO}'" \
    --project="${PROJECT_ID}"
else
  echo ">> Workload Identity Provider '${PROVIDER_NAME}' already exists."
fi

# 6. Bind GitHub Repo to Service Account
echo ">> Authorizing repository '${GITHUB_REPO}' to impersonate ${SA_EMAIL}..."
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
POOL_RESOURCE_ID="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}"
PROVIDER_RESOURCE_ID="${POOL_RESOURCE_ID}/providers/${PROVIDER_NAME}"

gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/${POOL_RESOURCE_ID}/attribute.repository/${GITHUB_REPO}" \
  --project="${PROJECT_ID}" \
  --quiet

# 7. Print Service Account Unique Client ID for Google Workspace DWD
SA_CLIENT_ID=$(gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" --format="value(uniqueId)")

echo ""
echo "==================================================================="
echo " Workload Identity Federation Configured Successfully!"
echo "==================================================================="
echo ""
echo "Configure these GitHub Secrets in your repository settings:"
echo " (Settings > Secrets and variables > Actions > New repository secret)"
echo ""
echo " 1. WIF_PROVIDER:"
echo "    ${PROVIDER_RESOURCE_ID}"
echo ""
echo " 2. WIF_SERVICE_ACCOUNT:"
echo "    ${SA_EMAIL}"
echo ""
echo " Also add repository VARIABLES (same screen, Variables tab):"
echo "    GCP_PROJECT_ID=${PROJECT_ID}"
echo "    GCP_REGION=${REGION}"
echo "    (see setup_instructions.md Step 5 for the full optional list)"
echo ""
echo "-------------------------------------------------------------------"
echo "Google Workspace Domain-Wide Delegation (DWD) Info:"
echo " Service Account Unique Client ID (paste into Admin Console DWD):"
echo "    ${SA_CLIENT_ID}"
echo ""
echo " After deploy, set the delegated admin on the Settings page (or the"
echo " DELEGATED_ADMIN_EMAIL env var) to a REAL, active, licensed admin user"
echo " in your Workspace tenant - e.g. workspace-admin@your-domain.com."
echo "==================================================================="
