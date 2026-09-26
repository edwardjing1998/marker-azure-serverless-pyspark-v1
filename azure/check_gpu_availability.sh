#!/usr/bin/env bash
set -euo pipefail
: "${AZURE_LOCATION:?Set AZURE_LOCATION}"
az extension add --name containerapp --upgrade --yes >/dev/null
az containerapp env workload-profile list-supported -l "$AZURE_LOCATION" \
  --query "[?contains(name, 'GPU') || contains(category, 'GPU')].[name,category,minimumCount,maximumCount]" \
  -o table
