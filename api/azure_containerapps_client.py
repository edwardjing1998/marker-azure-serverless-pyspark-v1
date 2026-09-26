"""Minimal Azure Container Apps Jobs ARM client using OAuth client credentials."""
import os
import time
import requests


class AzureRemoteError(RuntimeError):
    pass


class AzureContainerAppsJobsClient:
    def __init__(self):
        self.tenant_id = os.environ['AZURE_TENANT_ID']
        self.client_id = os.environ['AZURE_CLIENT_ID']
        self.client_secret = os.environ['AZURE_CLIENT_SECRET']
        self.subscription_id = os.environ['AZURE_SUBSCRIPTION_ID']
        self.resource_group = os.environ['AZURE_CONTAINERAPPS_RESOURCE_GROUP']
        self.job_name = os.environ['AZURE_GPU_JOB_NAME']
        self.api_version = os.getenv('AZURE_CONTAINERAPPS_API_VERSION', '2026-01-01')
        self.image = os.environ['AZURE_GPU_JOB_IMAGE']
        self.container_name = os.getenv('AZURE_GPU_JOB_CONTAINER_NAME', 'marker-gpu')
        self.cpu = float(os.getenv('AZURE_GPU_JOB_CPU', '8'))
        self.memory = os.getenv('AZURE_GPU_JOB_MEMORY', '56Gi')
        self.storage_endpoint = os.environ['AZURE_STORAGE_ENDPOINT']
        self.storage_container = os.getenv('AZURE_STORAGE_CONTAINER', 'education')
        self._token = None
        self._expires_at = 0

    def _access_token(self):
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        response = requests.post(
            f'https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token',
            data={
                'client_id': self.client_id,
                'client_secret': self.client_secret,
                'grant_type': 'client_credentials',
                'scope': 'https://management.azure.com/.default',
            },
            timeout=30,
        )
        if not response.ok:
            raise AzureRemoteError(f'Azure OAuth failed with HTTP {response.status_code}')
        payload = response.json()
        self._token = payload['access_token']
        self._expires_at = time.time() + int(payload.get('expires_in', 3600))
        return self._token

    def _url(self, suffix=''):
        base = (
            f'https://management.azure.com/subscriptions/{self.subscription_id}'
            f'/resourceGroups/{self.resource_group}/providers/Microsoft.App/jobs/{self.job_name}'
        )
        return f'{base}{suffix}?api-version={self.api_version}'

    def _headers(self):
        return {'Authorization': f'Bearer {self._access_token()}', 'Content-Type': 'application/json'}

    def start(self, params):
        env = {
            'AZURE_STORAGE_ENDPOINT': self.storage_endpoint,
            'AZURE_STORAGE_CONTAINER': self.storage_container,
            'AZURE_STORAGE_SOURCE_PREFIX': os.getenv('AZURE_STORAGE_SOURCE_PREFIX', 'source/'),
            'AZURE_STORAGE_OUTPUT_PREFIX': os.getenv('AZURE_STORAGE_OUTPUT_PREFIX', 'generated/'),
            'GPU_FALLBACK_ENABLED': 'true',
            'MAX_SCAN_FILES': os.getenv('MAX_SCAN_FILES', '10000'),
            'MAX_FILE_MB': os.getenv('MAX_FILE_MB', '200'),
            'MAX_DOCUMENT_PAGES': os.getenv('MAX_DOCUMENT_PAGES', '5000'),
            'CPU_MAX_PAGES': os.getenv('CPU_MAX_PAGES', '20'),
            'CPU_MAX_FILE_MB': os.getenv('CPU_MAX_FILE_MB', '20'),
            'CPU_TIMEOUT_SECONDS': os.getenv('CPU_TIMEOUT_SECONDS', '900'),
            'CPU_MAX_RSS_MB': os.getenv('CPU_MAX_RSS_MB', '10000'),
            'MARKER_CACHE_REVISION': os.getenv('MARKER_CACHE_REVISION', 'azure-serverless-v1'),
            'RELEASE_ID': os.getenv('RELEASE_ID', 'manual'),
            'SOURCE_PREFIX': params['source_prefix'],
            'OUTPUT_PREFIX': params['output_prefix'],
            'MAX_FILES': params['max_files'],
            'OVERWRITE': params['overwrite'],
            'REPLACE_EXISTING': params['replace_existing'],
            'DRY_RUN': params['dry_run'],
            'PAGES_PER_CHUNK': params['pages_per_chunk'],
            'FORCE_OCR': params['force_ocr'],
            'DROP_HANDWRITING': params['drop_handwriting'],
            'COMPUTE_MODE': params['compute_mode'],
            'REQUEST_ID': params['request_id'],
        }
        body = {
            'containers': [{
                'name': self.container_name,
                'image': self.image,
                'resources': {'cpu': self.cpu, 'memory': self.memory},
                'env': [{'name': k, 'value': str(v)} for k, v in env.items()],
            }]
        }
        response = requests.post(self._url('/start'), headers=self._headers(), json=body, timeout=60)
        if not response.ok:
            text = response.text[:1200]
            raise AzureRemoteError(f'Container Apps job start failed HTTP {response.status_code}: {text}')
        payload = response.json()
        if not payload.get('name'):
            raise AzureRemoteError('Container Apps job start response did not include execution name')
        return payload

    def execution(self, execution_name):
        response = requests.get(self._url(f'/executions/{execution_name}'), headers=self._headers(), timeout=30)
        if response.status_code == 404:
            return None
        if not response.ok:
            raise AzureRemoteError(f'Container Apps execution lookup failed HTTP {response.status_code}: {response.text[:1000]}')
        return response.json()
