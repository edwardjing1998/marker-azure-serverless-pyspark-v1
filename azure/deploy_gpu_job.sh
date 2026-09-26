#!/usr/bin/env bash
set -euo pipefail
: "${AZURE_RESOURCE_GROUP:?}"
: "${AZURE_LOCATION:?}"
: "${AZURE_CONTAINERAPPS_ENVIRONMENT:?}"
: "${AZURE_GPU_JOB_NAME:?}"
: "${AZURE_GPU_JOB_IMAGE:?}"
: "${AZURE_STORAGE_ACCOUNT:?}"

PROFILE_NAME="${AZURE_GPU_WORKLOAD_PROFILE_NAME:-gpu-t4}"
PROFILE_TYPE="${AZURE_GPU_WORKLOAD_PROFILE_TYPE:-Consumption-GPU-NC8as-T4}"
GPU_CPU="${AZURE_GPU_JOB_CPU:-8}"
GPU_MEMORY="${AZURE_GPU_JOB_MEMORY:-56Gi}"

az extension add --name containerapp --upgrade --yes >/dev/null

if ! az containerapp env show -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_CONTAINERAPPS_ENVIRONMENT" >/dev/null 2>&1; then
  az containerapp env create \
    -g "$AZURE_RESOURCE_GROUP" \
    -n "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    -l "$AZURE_LOCATION"
fi

if ! az containerapp env workload-profile show \
    -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    --workload-profile-name "$PROFILE_NAME" >/dev/null 2>&1; then
  az containerapp env workload-profile add \
    -g "$AZURE_RESOURCE_GROUP" \
    -n "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    --workload-profile-name "$PROFILE_NAME" \
    --workload-profile-type "$PROFILE_TYPE"
fi

REGISTRY_SERVER="${AZURE_GPU_REGISTRY_SERVER:-ghcr.io}"
REGISTRY_USERNAME="${AZURE_GPU_REGISTRY_USERNAME:-}"
REGISTRY_PASSWORD="${AZURE_GPU_REGISTRY_PASSWORD:-}"
REGISTRY_ARGS=()
if [[ -n "$REGISTRY_USERNAME" && -n "$REGISTRY_PASSWORD" ]]; then
  REGISTRY_ARGS+=(--registry-server "$REGISTRY_SERVER" --registry-username "$REGISTRY_USERNAME" --registry-password "$REGISTRY_PASSWORD")
fi

if az containerapp job show -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_GPU_JOB_NAME" >/dev/null 2>&1; then
  az containerapp job update \
    -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_GPU_JOB_NAME" \
    --image "$AZURE_GPU_JOB_IMAGE" \
    --cpu "$GPU_CPU" --memory "$GPU_MEMORY" \
    "${REGISTRY_ARGS[@]}"
else
  az containerapp job create \
    -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_GPU_JOB_NAME" \
    --environment "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    --trigger-type Manual \
    --replica-timeout "${AZURE_GPU_JOB_TIMEOUT:-7200}" \
    --replica-retry-limit 0 \
    --replica-completion-count 1 \
    --parallelism 1 \
    --image "$AZURE_GPU_JOB_IMAGE" \
    --workload-profile-name "$PROFILE_NAME" \
    --cpu "$GPU_CPU" --memory "$GPU_MEMORY" \
    --system-assigned \
    "${REGISTRY_ARGS[@]}"
fi

# Ensure system-assigned managed identity exists even when updating a pre-existing job.
az containerapp job identity assign -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_GPU_JOB_NAME" --system-assigned >/dev/null

PRINCIPAL_ID=$(az containerapp job show -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_GPU_JOB_NAME" --query identity.principalId -o tsv)
STORAGE_ID=$(az storage account show -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_STORAGE_ACCOUNT" --query id -o tsv 2>/dev/null || true)
if [[ -z "$STORAGE_ID" ]]; then
  STORAGE_ID=$(az storage account list --query "[?name=='$AZURE_STORAGE_ACCOUNT'].id | [0]" -o tsv)
fi
if [[ -z "$STORAGE_ID" ]]; then
  echo "Storage account $AZURE_STORAGE_ACCOUNT was not found" >&2
  exit 1
fi

# Idempotent role assignment. The deploying principal needs permission to create RBAC assignments.
az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" \
  --scope "$STORAGE_ID" >/dev/null 2>&1 || true

JOB_ID=$(az containerapp job show -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_GPU_JOB_NAME" --query id -o tsv)
if [[ -n "${AZURE_GATEWAY_PRINCIPAL_OBJECT_ID:-}" ]]; then
  az role assignment create \
    --assignee-object-id "$AZURE_GATEWAY_PRINCIPAL_OBJECT_ID" \
    --assignee-principal-type ServicePrincipal \
    --role "Container Apps Jobs Operator" \
    --scope "$JOB_ID" >/dev/null 2>&1 || true
fi

echo "GPU job deployed: $AZURE_GPU_JOB_NAME"
echo "Managed identity principal: $PRINCIPAL_ID"
echo "Workload profile: $PROFILE_NAME ($PROFILE_TYPE)"
