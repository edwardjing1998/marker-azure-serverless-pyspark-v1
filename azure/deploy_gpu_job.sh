#!/usr/bin/env bash

set -euo pipefail

#
# Required configuration
#
: "${AZURE_SUBSCRIPTION_ID:?AZURE_SUBSCRIPTION_ID is required}"
: "${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
: "${AZURE_LOCATION:?AZURE_LOCATION is required}"

: "${AZURE_CONTAINERAPPS_ENVIRONMENT:?AZURE_CONTAINERAPPS_ENVIRONMENT is required}"

: "${AZURE_GPU_JOB_NAME:?AZURE_GPU_JOB_NAME is required}"
: "${AZURE_GPU_JOB_IMAGE:?AZURE_GPU_JOB_IMAGE is required}"

: "${AZURE_STORAGE_ACCOUNT:?AZURE_STORAGE_ACCOUNT is required}"

: "${AZURE_DEPLOYER_OBJECT_ID:?Set this to the object ID of the GitHub Azure service principal}"


#
# Optional/default configuration
#
PROFILE_NAME="${AZURE_GPU_WORKLOAD_PROFILE_NAME:-gpu-t4}"

PROFILE_TYPE="${AZURE_GPU_WORKLOAD_PROFILE_TYPE:-Consumption-GPU-NC8as-T4}"

GPU_CPU="${AZURE_GPU_JOB_CPU:-8}"

GPU_MEMORY="${AZURE_GPU_JOB_MEMORY:-56Gi}"

GPU_TIMEOUT="${AZURE_GPU_JOB_TIMEOUT:-7200}"

LOGS_DESTINATION="${AZURE_CONTAINERAPPS_LOGS_DESTINATION:-none}"

REGISTRY_SERVER="${AZURE_GPU_REGISTRY_SERVER:-ghcr.io}"

REGISTRY_USERNAME="${AZURE_GPU_REGISTRY_USERNAME:-}"

REGISTRY_PASSWORD="${AZURE_GPU_REGISTRY_PASSWORD:-}"


RESOURCE_GROUP_SCOPE="/subscriptions/${AZURE_SUBSCRIPTION_ID}/resourceGroups/${AZURE_RESOURCE_GROUP}"


echo "Azure subscription: $AZURE_SUBSCRIPTION_ID"
echo "Azure resource group: $AZURE_RESOURCE_GROUP"
echo "Azure location: $AZURE_LOCATION"
echo "Container Apps environment: $AZURE_CONTAINERAPPS_ENVIRONMENT"
echo "GPU job: $AZURE_GPU_JOB_NAME"
echo "GPU image: $AZURE_GPU_JOB_IMAGE"
echo "GPU workload profile: $PROFILE_NAME"
echo "GPU workload profile type: $PROFILE_TYPE"


#
# Validate logging configuration
#
case "$LOGS_DESTINATION" in

  none|azure-monitor|log-analytics)
    ;;

  *)
    echo "::error::Unsupported AZURE_CONTAINERAPPS_LOGS_DESTINATION: $LOGS_DESTINATION"
    echo "::error::Expected one of: none, azure-monitor, log-analytics"
    exit 1
    ;;

esac


#
# Install/update Container Apps CLI extension
#
echo "Installing/updating Azure Container Apps CLI extension..."

az extension add \
  --name containerapp \
  --upgrade \
  --yes \
  --allow-preview true \
  >/dev/null


#
# Verify required Azure resource providers.
#
# Resource provider registration is intentionally NOT performed here.
# Registration requires subscription-level permissions that should normally
# be handled during Azure subscription bootstrap.
#
REQUIRED_PROVIDERS=(
  Microsoft.App
  Microsoft.Storage
)

if [[ "$LOGS_DESTINATION" == "log-analytics" ]]; then
  REQUIRED_PROVIDERS+=(
    Microsoft.OperationalInsights
  )
fi

if [[ "$LOGS_DESTINATION" == "azure-monitor" ]]; then
  REQUIRED_PROVIDERS+=(
    Microsoft.Insights
  )
fi


echo "Checking required Azure resource providers..."

for provider in "${REQUIRED_PROVIDERS[@]}"; do

  state=$(
    az provider show \
      --namespace "$provider" \
      --query registrationState \
      -o tsv
  )

  echo "$provider: $state"

  if [[ "$state" != "Registered" ]]; then

    echo "::error::$provider is not registered."
    echo "::error::Register this provider at subscription scope before running deployment."

    exit 1

  fi

done


#
# RBAC helper
#
ensure_role_assignment() {

  local principal_object_id="$1"
  local role_name="$2"
  local scope="$3"
  local principal_type="${4:-ServicePrincipal}"

  local existing

  existing=$(
    az role assignment list \
      --assignee-object-id "$principal_object_id" \
      --scope "$scope" \
      --query "[?roleDefinitionName=='$role_name'] | length(@)" \
      -o tsv \
      2>/dev/null \
      || echo "0"
  )

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

    echo "::error::The GitHub Azure principal must already have Role Based Access Control Administrator, User Access Administrator, or Owner at this scope."

    exit 1

  fi

}


#
# Make sure the GitHub deployment service principal can manage
# Container Apps environments and jobs.
#
echo "Ensuring deployment RBAC roles..."

ensure_role_assignment \
  "$AZURE_DEPLOYER_OBJECT_ID" \
  "Container Apps ManagedEnvironments Contributor" \
  "$RESOURCE_GROUP_SCOPE"

ensure_role_assignment \
  "$AZURE_DEPLOYER_OBJECT_ID" \
  "Container Apps Jobs Contributor" \
  "$RESOURCE_GROUP_SCOPE"


#
# Prepare Container Apps environment logging arguments
#
LOG_ARGS=(
  --logs-destination "$LOGS_DESTINATION"
)


if [[ "$LOGS_DESTINATION" == "log-analytics" ]]; then

  : "${AZURE_LOG_ANALYTICS_WORKSPACE_ID:?AZURE_LOG_ANALYTICS_WORKSPACE_ID is required when using log-analytics}"

  : "${AZURE_LOG_ANALYTICS_WORKSPACE_KEY:?AZURE_LOG_ANALYTICS_WORKSPACE_KEY is required when using log-analytics}"

  LOG_ARGS+=(
    --logs-workspace-id "$AZURE_LOG_ANALYTICS_WORKSPACE_ID"
    --logs-workspace-key "$AZURE_LOG_ANALYTICS_WORKSPACE_KEY"
  )

fi


#
# Create Container Apps environment when it does not already exist.
#
if ! az containerapp env show \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    >/dev/null 2>&1; then

  echo "Creating Container Apps environment: $AZURE_CONTAINERAPPS_ENVIRONMENT"

  echo "Logs destination: $LOGS_DESTINATION"

  az containerapp env create \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    --location "$AZURE_LOCATION" \
    "${LOG_ARGS[@]}"

else

  echo "Container Apps environment already exists: $AZURE_CONTAINERAPPS_ENVIRONMENT"

fi


#
# Wait for environment provisioning.
#
echo "Checking Container Apps environment provisioning state..."

ENV_STATE=$(
  az containerapp env show \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    --query properties.provisioningState \
    -o tsv
)

echo "Environment provisioning state: $ENV_STATE"


if [[ "$ENV_STATE" != "Succeeded" ]]; then

  echo "::error::Container Apps environment is not ready."
  echo "::error::Current provisioning state: $ENV_STATE"

  exit 1

fi


#
# Create the T4 serverless workload profile if missing.
#
if ! az containerapp env workload-profile show \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    --workload-profile-name "$PROFILE_NAME" \
    >/dev/null 2>&1; then

  echo "Adding workload profile: $PROFILE_NAME"

  echo "Workload profile type: $PROFILE_TYPE"

  az containerapp env workload-profile add \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    --workload-profile-name "$PROFILE_NAME" \
    --workload-profile-type "$PROFILE_TYPE"

else

  echo "Workload profile already exists: $PROFILE_NAME"

fi


#
# Registry arguments used ONLY during initial job creation.
#
REGISTRY_CREATE_ARGS=()

if [[ -n "$REGISTRY_USERNAME" && -n "$REGISTRY_PASSWORD" ]]; then

  REGISTRY_CREATE_ARGS+=(
    --registry-server "$REGISTRY_SERVER"
    --registry-username "$REGISTRY_USERNAME"
    --registry-password "$REGISTRY_PASSWORD"
  )

fi


#
# Determine whether the Container Apps job already exists.
#
if az containerapp job show \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$AZURE_GPU_JOB_NAME" \
    >/dev/null 2>&1; then

  #
  # Existing job
  #
  # IMPORTANT:
  # az containerapp job update does not accept
  # --registry-server / --registry-username / --registry-password
  # in the current Container Apps CLI extension.
  #
  echo "Updating Container Apps job: $AZURE_GPU_JOB_NAME"

  az containerapp job update \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$AZURE_GPU_JOB_NAME" \
    --image "$AZURE_GPU_JOB_IMAGE" \
    --cpu "$GPU_CPU" \
    --memory "$GPU_MEMORY"


  #
  # Update private registry credentials separately.
  #
  if [[ -n "$REGISTRY_USERNAME" && -n "$REGISTRY_PASSWORD" ]]; then

    echo "Updating container registry credentials for $REGISTRY_SERVER..."

    az containerapp job registry set \
      --resource-group "$AZURE_RESOURCE_GROUP" \
      --name "$AZURE_GPU_JOB_NAME" \
      --server "$REGISTRY_SERVER" \
      --username "$REGISTRY_USERNAME" \
      --password "$REGISTRY_PASSWORD"

  fi

else

  #
  # New job
  #
  echo "Creating Container Apps job: $AZURE_GPU_JOB_NAME"

  az containerapp job create \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$AZURE_GPU_JOB_NAME" \
    --environment "$AZURE_CONTAINERAPPS_ENVIRONMENT" \
    --trigger-type Manual \
    --replica-timeout "$GPU_TIMEOUT" \
    --replica-retry-limit 0 \
    --replica-completion-count 1 \
    --parallelism 1 \
    --image "$AZURE_GPU_JOB_IMAGE" \
    --workload-profile-name "$PROFILE_NAME" \
    --cpu "$GPU_CPU" \
    --memory "$GPU_MEMORY" \
    --system-assigned \
    "${REGISTRY_CREATE_ARGS[@]}"

fi


#
# Make sure system-assigned managed identity exists.
#
echo "Ensuring system-assigned managed identity..."

az containerapp job identity assign \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$AZURE_GPU_JOB_NAME" \
  --system-assigned \
  >/dev/null


#
# Read the job managed identity principal ID.
#
PRINCIPAL_ID=$(
  az containerapp job show \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$AZURE_GPU_JOB_NAME" \
    --query identity.principalId \
    -o tsv
)


if [[ -z "$PRINCIPAL_ID" ]]; then

  echo "::error::Unable to determine Container Apps Job managed identity principal ID."

  exit 1

fi


echo "Job managed identity principal ID: $PRINCIPAL_ID"


#
# Resolve the Azure Storage account resource ID.
#
STORAGE_ID=$(
  az storage account show \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$AZURE_STORAGE_ACCOUNT" \
    --query id \
    -o tsv \
    2>/dev/null \
    || true
)


#
# Fallback when storage account is not in AZURE_RESOURCE_GROUP.
#
if [[ -z "$STORAGE_ID" ]]; then

  STORAGE_ID=$(
    az storage account list \
      --query "[?name=='$AZURE_STORAGE_ACCOUNT'].id | [0]" \
      -o tsv
  )

fi


if [[ -z "$STORAGE_ID" ]]; then

  echo "::error::Storage account '$AZURE_STORAGE_ACCOUNT' was not found."

  exit 1

fi


echo "Storage account resource ID: $STORAGE_ID"


#
# Give the worker managed identity Blob data access.
#
echo "Ensuring Storage Blob Data Contributor role..."

ensure_role_assignment \
  "$PRINCIPAL_ID" \
  "Storage Blob Data Contributor" \
  "$STORAGE_ID"


#
# Get Container Apps Job resource ID.
#
JOB_ID=$(
  az containerapp job show \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$AZURE_GPU_JOB_NAME" \
    --query id \
    -o tsv
)


#
# Optional:
# allow the OpenShift gateway service principal to start the job.
#
if [[ -n "${AZURE_GATEWAY_PRINCIPAL_OBJECT_ID:-}" ]]; then

  echo "Ensuring gateway Container Apps Jobs Operator role..."

  ensure_role_assignment \
    "$AZURE_GATEWAY_PRINCIPAL_OBJECT_ID" \
    "Container Apps Jobs Operator" \
    "$JOB_ID"

fi


#
# Final verification
#
echo "Verifying Container Apps job..."

az containerapp job show \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$AZURE_GPU_JOB_NAME" \
  --query "{
    name:name,
    provisioningState:properties.provisioningState,
    image:properties.template.containers[0].image,
    cpu:properties.template.containers[0].resources.cpu,
    memory:properties.template.containers[0].resources.memory,
    workloadProfile:properties.workloadProfileName,
    identityType:identity.type,
    principalId:identity.principalId
  }" \
  -o json


echo
echo "Azure serverless GPU deployment completed."
echo
echo "Environment: $AZURE_CONTAINERAPPS_ENVIRONMENT"
echo "Logs destination: $LOGS_DESTINATION"
echo "GPU job: $AZURE_GPU_JOB_NAME"
echo "GPU image: $AZURE_GPU_JOB_IMAGE"
echo "Managed identity principal: $PRINCIPAL_ID"
echo "Workload profile: $PROFILE_NAME ($PROFILE_TYPE)"