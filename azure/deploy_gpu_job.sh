#!/usr/bin/env bash
set -euo pipefail

: "${AZURE_SUBSCRIPTION_ID:?}"
: "${AZURE_RESOURCE_GROUP:?}"
: "${AZURE_LOCATION:?}"
: "${AZURE_CONTAINERAPPS_ENVIRONMENT:?}"
: "${AZURE_GPU_JOB_NAME:?}"
: "${AZURE_GPU_JOB_IMAGE:?}"
: "${AZURE_STORAGE_ACCOUNT:?}"
: "${AZURE_DEPLOYER_OBJECT_ID:?Set this to the object ID of the GitHub Azure service principal}"

PROFILE_NAME="${AZURE_GPU_WORKLOAD_PROFILE_NAME:-gpu-t4}"
PROFILE_TYPE="${AZURE_GPU_WORKLOAD_PROFILE_TYPE:-Consumption-GPU-NC8as-T4}"
GPU_CPU="${AZURE_GPU_JOB_CPU:-8}"
GPU_MEMORY="${AZURE_GPU_JOB_MEMORY:-56Gi}"
LOGS_DESTINATION="${AZURE_CONTAINERAPPS_LOGS_DESTINATION:-none}"

RESOURCE_GROUP_SCOPE="/subscriptions/${AZURE_SUBSCRIPTION_ID}/resourceGroups/${AZURE_RESOURCE_GROUP}"

case "$LOGS_DESTINATION" in
  none|azure-monitor|log-analytics)
    ;;
  *)
    echo "Unsupported AZURE_CONTAINERAPPS_LOGS_DESTINATION: $LOGS_DESTINATION" >&2
    exit 1
    ;;
esac

echo "Installing/updating Azure Container Apps CLI extension..."

az extension add \
  --name containerapp \
  --upgrade \
  --yes \
  --allow-preview true \
  >/dev/null

REQUIRED_PROVIDERS=(
  Microsoft.App
  Microsoft.Storage
)

if [[ "$LOGS_DESTINATION" == "log-analytics" ]]; then
  REQUIRED_PROVIDERS+=(Microsoft.OperationalInsights)
fi

if [[ "$LOGS_DESTINATION" == "azure-monitor" ]]; then
  REQUIRED_PROVIDERS+=(Microsoft.Insights)
fi

echo "Checking required Azure resource providers..."

for provider in "${REQUIRED_PROVIDERS[@]}"; do

  state=$(az provider show \
    --namespace "$provider" \
    --query registrationState \
    -o tsv)

  echo "$provider: $state"

  if [[ "$state" != "Registered" ]]; then
    echo "::error::$provider is not registered."
    exit 1
  fi

done

ensure_role_assignment() {

  local principal_object_id="$1"
  local role_name="$2"
  local scope="$3"
  local principal_type="${4:-ServicePrincipal}"

  local existing

  existing=$(az role assignment list \
    --assignee-object-id "$principal_object_id" \
    --scope "$scope" \
    --query "[?roleDefinitionName=='$role_name'] | length(@)" \
    -o tsv 2>/dev/null || echo "0")

  if [[ "$existing" != "0" ]]; then
    echo "RBAC already present: $role_name"
    return 0
  fi

  echo "Assigning RBAC role '$role_name'"

  if ! az role assignment create \
      --assignee-object-id "$principal_object_id" \
      --assignee-principal-type "$principal_type" \
      --role "$role_name" \
      --scope "$scope" \
      >/dev/null; then

    echo "::error::Unable to create RBAC role assignment: $role_name"
    echo "::error::The GitHub Azure principal must already have User Access Administrator or Owner."
    exit 1

  fi
}

echo "Ensuring deployment RBAC roles..."

ensure_role_assignment \
  "$AZURE_DEPLOYER_OBJECT_ID" \
  "Container Apps ManagedEnvironments Contributor" \
  "$RESOURCE_GROUP_SCOPE"

ensure_role_assignment \
  "$AZURE_DEPLOYER_OBJECT_ID" \
  "Container Apps Jobs Contributor" \
  "$RESOURCE_GROUP_SCOPE"

LOG_ARGS=(--logs-destination "$LOGS_DESTINATION")

if [[ "$LOGS_DESTINATION" == "log-analytics" ]]; then

  : "${AZURE_LOG_ANALYTICS_WORKSPACE_ID:?}"
  : "${AZURE_LOG_ANALYTICS_WORKSPACE_KEY:?}"

  LOG_ARGS+=(
    --logs-workspace-id "$AZURE_LOG_ANALYTICS_WORKSPACE_ID"
    --logs-workspace-key "$AZURE_LOG_ANALYTICS_WORKSPACE_KEY"
  )
fi

if ! az containerapp env show \
    -g "$AZURE_RESOURCE_GROUP" \
    -n "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    >/dev/null 2>&1; then

  echo "Creating Container Apps environment..."

  az containerapp env create \
    -g "$AZURE_RESOURCE_GROUP" \
    -n "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    -l "$AZURE_LOCATION" \
    "${LOG_ARGS[@]}"
fi

if ! az containerapp env workload-profile show \
    -g "$AZURE_RESOURCE_GROUP" \
    -n "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    --workload-profile-name "$PROFILE_NAME" \
    >/dev/null 2>&1; then

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

  REGISTRY_ARGS+=(
    --registry-server "$REGISTRY_SERVER"
    --registry-username "$REGISTRY_USERNAME"
    --registry-password "$REGISTRY_PASSWORD"
  )
fi

if az containerapp job show \
    -g "$AZURE_RESOURCE_GROUP" \
    -n "$AZURE_GPU_JOB_NAME" \
    >/dev/null 2>&1; then

  az containerapp job update \
    -g "$AZURE_RESOURCE_GROUP" \
    -n "$AZURE_GPU_JOB_NAME" \
    --image "$AZURE_GPU_JOB_IMAGE" \
    --cpu "$GPU_CPU" \
    --memory "$GPU_MEMORY" \
    "${REGISTRY_ARGS[@]}"

else

  az containerapp job create \
    -g "$AZURE_RESOURCE_GROUP" \
    -n "$AZURE_GPU_JOB_NAME" \
    --environment "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    --trigger-type Manual \
    --replica-timeout "${AZURE_GPU_JOB_TIMEOUT:-7200}" \
    --replica-retry-limit 0 \
    --replica-completion-count 1 \
    --parallelism 1 \
    --image "$AZURE_GPU_JOB_IMAGE" \
    --workload-profile-name "$PROFILE_NAME" \
    --cpu "$GPU_CPU" \
    --memory "$GPU_MEMORY" \
    --system-assigned \
    "${REGISTRY_ARGS[@]}"

fi

az containerapp job identity assign \
  -g "$AZURE_RESOURCE_GROUP" \
  -n "$AZURE_GPU_JOB_NAME" \
  --system-assigned \
  >/dev/null

PRINCIPAL_ID=$(az containerapp job show \
  -g "$AZURE_RESOURCE_GROUP" \
  -n "$AZURE_GPU_JOB_NAME" \
  --query identity.principalId \
  -o tsv)

STORAGE_ID="/subscriptions/${AZURE_SUBSCRIPTION_ID}/resourceGroups/${AZURE_RESOURCE_GROUP}/providers/Microsoft.Storage/storageAccounts/${AZURE_STORAGE_ACCOUNT}"

ensure_role_assignment \
  "$PRINCIPAL_ID" \
  "Storage Blob Data Contributor" \
  "$STORAGE_ID"

JOB_ID=$(az containerapp job show \
  -g "$AZURE_RESOURCE_GROUP" \
  -n "$AZURE_GPU_JOB_NAME" \
  --query id \
  -o tsv)

if [[ -n "${AZURE_GATEWAY_PRINCIPAL_OBJECT_ID:-}" ]]; then

  ensure_role_assignment \
    "$AZURE_GATEWAY_PRINCIPAL_OBJECT_ID" \
    "Container Apps Jobs Operator" \
    "$JOB_ID"

fi

echo "Azure serverless GPU deployment completed."