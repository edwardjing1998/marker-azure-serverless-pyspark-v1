import os
from api.azure_containerapps_client import AzureContainerAppsJobsClient


def test_client_reads_job_settings(monkeypatch):
    values = {
        'AZURE_TENANT_ID': 't', 'AZURE_CLIENT_ID': 'c', 'AZURE_CLIENT_SECRET': 's',
        'AZURE_SUBSCRIPTION_ID': '11111111-1111-1111-1111-111111111111',
        'AZURE_CONTAINERAPPS_RESOURCE_GROUP': 'rg', 'AZURE_GPU_JOB_NAME': 'job',
        'AZURE_GPU_JOB_IMAGE': 'ghcr.io/example/worker:tag',
        'AZURE_STORAGE_ENDPOINT': 'https://example.blob.core.windows.net',
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    client = AzureContainerAppsJobsClient()
    assert client.job_name == 'job'
    assert client.cpu == 8
    assert client.memory == '56Gi'
