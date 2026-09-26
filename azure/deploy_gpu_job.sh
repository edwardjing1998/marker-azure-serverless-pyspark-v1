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
LOGS_DESTINATION="${AZURE_CONTAINERAPPS_LOGS_DESTINATION:-none}"

case "$LOGS_DESTINATION" in
  none|azure-monitor|log-analytics)
    ;;
  *)
    echo "Unsupported AZURE_CONTAINERAPPS_LOGS_DESTINATION: $LOGS_DESTINATION" >&2
    echo "Use one of: none, azure-monitor, log-analytics" >&2
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

# Resource-provider registration is a subscription bootstrap operation.
# The CI/CD identity intentionally does not try to register providers because
# that requires subscription-level Microsoft.<provider>/register/action.
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
    echo "::error::$provider is not registered. Register it at subscription level before deployment."
    exit 1
  fi
done

# Build log configuration. The default is 'none' so the deployment does not
# auto-create a Log Analytics workspace and therefore does not require
# Microsoft.OperationalInsights/workspaces/write.
LOG_ARGS=(--logs-destination "$LOGS_DESTINATION")

if [[ "$LOGS_DESTINATION" == "log-analytics" ]]; then
  : "${AZURE_LOG_ANALYTICS_WORKSPACE_ID:?Set AZURE_LOG_ANALYTICS_WORKSPACE_ID when using log-analytics}"
  : "${AZURE_LOG_ANALYTICS_WORKSPACE_KEY:?Set AZURE_LOG_ANALYTICS_WORKSPACE_KEY when using log-analytics}"
  LOG_ARGS+=(
    --logs-workspace-id "$AZURE_LOG_ANALYTICS_WORKSPACE_ID"
    --logs-workspace-key "$AZURE_LOG_ANALYTICS_WORKSPACE_KEY"
  )
fi

if ! az containerapp env show \
    -g "$AZURE_RESOURCE_GROUP" \
    -n "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    >/dev/null 2>&1; then

  echo "Creating Container Apps environment: $AZURE_CONTAINERAPPS_ENVIRONMENT"
  echo "Logs destination: $LOGS_DESTINATION"

  az containerapp env create \
    -g "$AZURE_RESOURCE_GROUP" \
    -n "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    -l "$AZURE_LOCATION" \
    "${LOG_ARGS[@]}"
else
  echo "Container Apps environment already exists: $AZURE_CONTAINERAPPS_ENVIRONMENT"
fi

if ! az containerapp env workload-profile show \
    -g "$AZURE_RESOURCE_GROUP" \
    -n "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    --workload-profile-name "$PROFILE_NAME" \
    >/dev/null 2>&1; then

  echo "Adding workload profile: $PROFILE_NAME ($PROFILE_TYPE)"

  az containerapp env workload-profile add \
    -g "$AZURE_RESOURCE_GROUP" \
    -n "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    --workload-profile-name "$PROFILE_NAME" \
    --workload-profile-type "$PROFILE_TYPE"
else
  echo "Workload profile already exists: $PROFILE_NAME"
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

  echo "Updating Container Apps job: $AZURE_GPU_JOB_NAME"

  az containerapp job update \
    -g "$AZURE_RESOURCE_GROUP" \
    -n "$AZURE_GPU_JOB_NAME" \
    --image "$AZURE_GPU_JOB_IMAGE" \
    --cpu "$GPU_CPU" \
    --memory "$GPU_MEMORY" \
    "${REGISTRY_ARGS[@]}"
else
  echo "Creating Container Apps job: $AZURE_GPU_JOB_NAME"

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

STORAGE_ID=$(az storage account show \
  -g "$AZURE_RESOURCE_GROUP" \
  -n "$AZURE_STORAGE_ACCOUNT" \
  --query id \
  -o tsv 2>/dev/null || true)

if [[ -z "$STORAGE_ID" ]]; then
  STORAGE_ID=$(az storage account list \
    --query "[?name=='$AZURE_STORAGE_ACCOUNT'].id | [0]" \
    -o tsv)
fi

if [[ -z "$STORAGE_ID" ]]; then
  echo "Storage account $AZURE_STORAGE_ACCOUNT was not found" >&2
  exit 1
fi

az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" \
  --scope "$STORAGE_ID" \
  >/dev/null 2>&1 || true

JOB_ID=$(az containerapp job show \
  -g "$AZURE_RESOURCE_GROUP" \
  -n "$AZURE_GPU_JOB_NAME" \
  --query id \
  -o tsv)

if [[ -n "${AZURE_GATEWAY_PRINCIPAL_OBJECT_ID:-}" ]]; then
  az role assignment create \
    --assignee-object-id "$AZURE_GATEWAY_PRINCIPAL_OBJECT_ID" \
    --assignee-principal-type ServicePrincipal \
    --role "Container Apps Jobs Operator" \
    --scope "$JOB_ID" \
    >/dev/null 2>&1 || true
fi

echo "Azure serverless GPU deployment completed."
echo "Environment: $AZURE_CONTAINERAPPS_ENVIRONMENT"
echo "Logs destination: $LOGS_DESTINATION"
echo "GPU job: $AZURE_GPU_JOB_NAME"
echo "Managed identity principal: $PRINCIPAL_ID"
echo "Workload profile: $PROFILE_NAME ($PROFILE_TYPE)"