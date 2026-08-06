<#
.SYNOPSIS
    Deploy Montreal Forced Aligner to Google Cloud Run. CPU only — no GPU, and
    therefore no GPU quota consumed.

.EXAMPLE
    .\deploy\gcp\deploy.ps1 -ProjectId foundary-gcp

.EXAMPLE
    .\deploy\gcp\deploy.ps1 -ProjectId foundary-gcp -SkipBuild
#>
[CmdletBinding()]
param(
    [string]$ProjectId,
    [string]$Region = 'us-central1',
    [string]$Service = 'mfa-aligner',
    [string]$Repo = 'mfa-aligner',
    [string]$ImageName = 'mfa-aligner',
    [string]$Tag = 'v1',
    [string]$ServiceAccountName = 'mfa-aligner-sa',
    [string]$BuildServiceAccountName = 'mfa-aligner-build',
    # MFA parallelises across cores via --num_jobs, so vCPU count IS the
    # performance knob. 8 is the Cloud Run maximum without a quota increase.
    [int]$Cpu = 8,
    [string]$Memory = '32Gi',
    [int]$MinInstances = 0,
    [int]$MaxInstances = 10,
    # One alignment per instance: MFA already saturates every core it is given,
    # so a second concurrent request would only contend for the same CPUs.
    [int]$Concurrency = 1,
    [int]$Timeout = 3600,
    [switch]$AllowUnauthenticated,
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'

if (-not $ProjectId) { $ProjectId = (gcloud config get-value project 2>$null) }
if (-not $ProjectId -or $ProjectId -eq '(unset)') {
    throw "No project set. Pass -ProjectId, or run: gcloud config set project YOUR_PROJECT"
}

$image = "$Region-docker.pkg.dev/$ProjectId/$Repo/${ImageName}:$Tag"
$serviceAccount = "$ServiceAccountName@$ProjectId.iam.gserviceaccount.com"
$buildServiceAccount = "$BuildServiceAccountName@$ProjectId.iam.gserviceaccount.com"

Write-Host "==> Project $ProjectId / region $Region / service $Service"
Write-Host "==> Image $image"
Write-Host "==> CPU-only deployment ($Cpu vCPU, no GPU, MFA_NUM_JOBS=$Cpu)"

Write-Host '==> Enabling APIs'
gcloud services enable run.googleapis.com cloudbuild.googleapis.com `
    artifactregistry.googleapis.com storage.googleapis.com --project $ProjectId

Write-Host "==> Ensuring Artifact Registry repo '$Repo'"
gcloud artifacts repositories describe $Repo --location $Region --project $ProjectId 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    gcloud artifacts repositories create $Repo --repository-format=docker `
        --location $Region --description 'Montreal Forced Aligner images' --project $ProjectId
}

Write-Host "==> Ensuring runtime service account '$serviceAccount'"
gcloud iam service-accounts describe $serviceAccount --project $ProjectId 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    gcloud iam service-accounts create $ServiceAccountName `
        --display-name 'MFA Aligner Cloud Run runtime' --project $ProjectId
}

# Only needed for gs:// audio inputs, so a failure here must not sink the deploy.
gcloud projects add-iam-policy-binding $ProjectId `
    --member "serviceAccount:$serviceAccount" `
    --role roles/storage.objectViewer --condition=None 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Could not grant roles/storage.objectViewer; gs:// inputs will not work."
}

if (-not $SkipBuild) {
    # Projects created from ~2024 on have no legacy Cloud Build identity, and
    # builds then fail with PERMISSION_DENIED even for a project owner.
    Write-Host "==> Ensuring build service account '$buildServiceAccount'"
    gcloud iam service-accounts describe $buildServiceAccount --project $ProjectId 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        gcloud iam service-accounts create $BuildServiceAccountName `
            --display-name 'MFA Aligner Cloud Build' --project $ProjectId
    }
    foreach ($role in @('roles/artifactregistry.writer','roles/logging.logWriter','roles/storage.objectAdmin')) {
        gcloud projects add-iam-policy-binding $ProjectId `
            --member "serviceAccount:$buildServiceAccount" --role $role --condition=None | Out-Null
    }

    Write-Host '==> Building image'
    gcloud builds submit --config deploy/gcp/cloudbuild.yaml --project $ProjectId `
        --service-account "projects/$ProjectId/serviceAccounts/$buildServiceAccount" `
        --default-buckets-behavior=regional-user-owned-bucket `
        --substitutions "_REGION=$Region,_REPO=$Repo,_IMAGE=$ImageName,_TAG=$Tag" .
    if ($LASTEXITCODE -ne 0) { throw 'Cloud Build failed.' }
} else {
    Write-Host "==> -SkipBuild set, using existing $image"
}

$authFlag = '--no-allow-unauthenticated'
if ($AllowUnauthenticated) { $authFlag = '--allow-unauthenticated' }

# The startup probe is not optional: MFA has to boot a PostgreSQL instance
# before it can align anything, and without a probe Cloud Run routes traffic
# the moment the port opens.
Write-Host '==> Deploying to Cloud Run (CPU, no GPU)'
gcloud run deploy $Service `
    --image $image `
    --region $Region `
    --project $ProjectId `
    --platform managed `
    --execution-environment gen2 `
    --cpu $Cpu `
    --memory $Memory `
    --min-instances $MinInstances `
    --max-instances $MaxInstances `
    --concurrency $Concurrency `
    --timeout $Timeout `
    --port 8080 `
    --service-account $serviceAccount `
    --no-cpu-throttling `
    --set-env-vars "MFA_PRELOAD=true,MFA_NUM_JOBS=$Cpu,MFA_MAX_CONCURRENCY=$Concurrency" `
    --startup-probe "httpGet.path=/ready,initialDelaySeconds=10,periodSeconds=10,timeoutSeconds=5,failureThreshold=24" `
    --liveness-probe "httpGet.path=/health,periodSeconds=30,timeoutSeconds=5,failureThreshold=3" `
    $authFlag
if ($LASTEXITCODE -ne 0) { throw 'Cloud Run deploy failed.' }

$url = gcloud run services describe $Service --region $Region --project $ProjectId --format 'value(status.url)'
Write-Host ''
Write-Host "==> Deployed: $url"
Write-Host '    No GPU attached — this consumes zero GPU quota.'
Write-Host '    Smoke test:  .\deploy\gcp\smoke_test.ps1'
