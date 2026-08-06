#!/usr/bin/env bash
# Deploy Montreal Forced Aligner to Cloud Run. CPU only — no GPU, and therefore
# no GPU quota consumed.
#
#   ./deploy/gcp/deploy.sh
#   SKIP_BUILD=1 ./deploy/gcp/deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-mfa-aligner}"
REPO="${REPO:-mfa-aligner}"
IMAGE_NAME="${IMAGE_NAME:-mfa-aligner}"
TAG="${TAG:-v1}"
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-mfa-aligner-sa}"
BUILD_SA_NAME="${BUILD_SA_NAME:-mfa-aligner-build}"

# MFA parallelises across cores via --num_jobs, so vCPU count IS the throughput
# knob. One alignment per instance — MFA already saturates every core.
CPU="${CPU:-8}"
MEMORY="${MEMORY:-32Gi}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-10}"
CONCURRENCY="${CONCURRENCY:-1}"
TIMEOUT="${TIMEOUT:-3600}"
ALLOW_UNAUTH="${ALLOW_UNAUTH:-false}"

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${IMAGE_NAME}:${TAG}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
BUILD_SA="${BUILD_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

[[ -z "${PROJECT_ID}" ]] && { echo "ERROR: no project set." >&2; exit 1; }

echo "==> Project ${PROJECT_ID} / region ${REGION} / service ${SERVICE}"
echo "==> CPU-only (${CPU} vCPU, no GPU, MFA_NUM_JOBS=${CPU})"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com storage.googleapis.com --project "${PROJECT_ID}"

gcloud artifacts repositories describe "${REPO}" --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud artifacts repositories create "${REPO}" --repository-format=docker \
  --location "${REGION}" --description "MFA images" --project "${PROJECT_ID}"

gcloud iam service-accounts describe "${SERVICE_ACCOUNT}" --project "${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud iam service-accounts create "${SERVICE_ACCOUNT_NAME}" \
  --display-name "MFA Aligner Cloud Run runtime" --project "${PROJECT_ID}"

# gs:// inputs only; a failure here must not sink the deploy.
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member "serviceAccount:${SERVICE_ACCOUNT}" \
  --role roles/storage.objectViewer --condition=None >/dev/null 2>&1 \
  || echo "    WARNING: could not grant storage.objectViewer; gs:// inputs unavailable."

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  # New projects have no legacy Cloud Build identity; builds fail with
  # PERMISSION_DENIED without an explicit service account.
  gcloud iam service-accounts describe "${BUILD_SA}" --project "${PROJECT_ID}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "${BUILD_SA_NAME}" \
    --display-name "MFA Aligner Cloud Build" --project "${PROJECT_ID}"
  for role in roles/artifactregistry.writer roles/logging.logWriter roles/storage.objectAdmin; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member "serviceAccount:${BUILD_SA}" --role "${role}" --condition=None >/dev/null
  done

  echo "==> Building image"
  gcloud builds submit --config deploy/gcp/cloudbuild.yaml --project "${PROJECT_ID}" \
    --service-account "projects/${PROJECT_ID}/serviceAccounts/${BUILD_SA}" \
    --default-buckets-behavior=regional-user-owned-bucket \
    --substitutions "_REGION=${REGION},_REPO=${REPO},_IMAGE=${IMAGE_NAME},_TAG=${TAG}" .
fi

AUTH_FLAG="--no-allow-unauthenticated"
[[ "${ALLOW_UNAUTH}" == "true" ]] && AUTH_FLAG="--allow-unauthenticated"

# Startup probe is required: MFA boots a PostgreSQL instance before it can
# align, and without a probe Cloud Run routes traffic the moment the port opens.
echo "==> Deploying to Cloud Run (CPU, no GPU)"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" --region "${REGION}" --project "${PROJECT_ID}" \
  --platform managed --execution-environment gen2 \
  --cpu "${CPU}" --memory "${MEMORY}" \
  --min-instances "${MIN_INSTANCES}" --max-instances "${MAX_INSTANCES}" \
  --concurrency "${CONCURRENCY}" --timeout "${TIMEOUT}" --port 8080 \
  --service-account "${SERVICE_ACCOUNT}" --no-cpu-throttling \
  --set-env-vars "MFA_PRELOAD=true,MFA_NUM_JOBS=${CPU},MFA_MAX_CONCURRENCY=${CONCURRENCY}" \
  --startup-probe "httpGet.path=/ready,initialDelaySeconds=10,periodSeconds=10,timeoutSeconds=5,failureThreshold=24" \
  --liveness-probe "httpGet.path=/health,periodSeconds=30,timeoutSeconds=5,failureThreshold=3" \
  ${AUTH_FLAG}

URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --project "${PROJECT_ID}" --format 'value(status.url)')"
echo
echo "==> Deployed: ${URL}"
echo "    No GPU attached — this consumes zero GPU quota."
