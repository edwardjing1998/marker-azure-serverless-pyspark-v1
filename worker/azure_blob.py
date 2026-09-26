"""Azure Blob Storage mirror used by the Container Apps job.

Authentication uses DefaultAzureCredential. In Azure Container Apps the intended
production identity is the job's managed identity with Storage Blob Data
Contributor on the target storage account/container.
"""
from pathlib import Path
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, BlobLeaseClient
from azure.core.exceptions import AzureError, ResourceExistsError
from contextlib import contextmanager
from shared.errors import PipelineError


class AzureBlobMirror:
    def __init__(self, endpoint: str, container: str):
        if not endpoint.startswith('https://'):
            raise ValueError('AZURE_STORAGE_ENDPOINT must be https://')
        self.endpoint = endpoint.rstrip('/')
        self.container_name = container
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        service = BlobServiceClient(account_url=self.endpoint, credential=credential)
        self.container = service.get_container_client(container)

    @staticmethod
    def _safe_blob_name(name: str) -> str:
        if not name or name.startswith('/') or '\\' in name or '\x00' in name:
            raise PipelineError('Unsafe blob name')
        parts = name.split('/')
        if any(p in ('', '.', '..') for p in parts):
            raise PipelineError('Unsafe blob name')
        return '/'.join(parts)


    @contextmanager
    def global_run_lock(self):
        """Serialize job executions like the original Databricks max_concurrent_runs=1.

        Uses an infinite Azure Blob lease so two independently started Container
        Apps executions cannot publish the same output tree concurrently.
        """
        name = '_marker_jobs/locks/marker-azure-gpu.lock'
        blob = self.container.get_blob_client(name)
        try:
            blob.upload_blob(b'lock', overwrite=False)
        except ResourceExistsError:
            pass
        lease = BlobLeaseClient(blob)
        try:
            lease.acquire(lease_duration=-1)
        except AzureError as exc:
            raise PipelineError(
                'Another Marker GPU execution holds the global Azure Blob lease; retry after it finishes'
            ) from None
        try:
            yield
        finally:
            try:
                lease.release()
            except AzureError:
                pass

    def list_documents(self, prefix: str, max_scan: int):
        allowed = {'.pdf', '.png', '.jpg', '.jpeg'}
        rows = []
        for blob in self.container.list_blobs(name_starts_with=prefix):
            suffix = Path(blob.name).suffix.lower()
            if suffix in allowed:
                rows.append(blob)
                if len(rows) > max_scan:
                    raise PipelineError('Source exceeds MAX_SCAN_FILES; select a narrower sourcePrefix')
        rows.sort(key=lambda b: b.name)
        return rows

    def download_blob(self, blob_name: str, local_root: Path):
        blob_name = self._safe_blob_name(blob_name)
        target = local_root.joinpath(*blob_name.split('/'))
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('wb') as stream:
            self.container.download_blob(blob_name).readinto(stream)
        return target

    def download_prefix(self, prefix: str, local_root: Path):
        count = 0
        for blob in self.container.list_blobs(name_starts_with=prefix):
            if blob.name.endswith('/'):
                continue
            self.download_blob(blob.name, local_root)
            count += 1
        return count

    def upload_file(self, local_path: Path, blob_name: str, *, overwrite=True):
        blob_name = self._safe_blob_name(blob_name)
        with Path(local_path).open('rb') as stream:
            self.container.upload_blob(blob_name, stream, overwrite=overwrite)

    def upload_tree(self, local_root: Path, blob_prefix: str, *, content_md_last=True):
        local_root = Path(local_root)
        if not local_root.exists():
            return 0
        files = [p for p in local_root.rglob('*') if p.is_file()]
        if content_md_last:
            files.sort(key=lambda p: (p.name == 'content.md', str(p)))
        else:
            files.sort()
        count = 0
        for path in files:
            relative = path.relative_to(local_root).as_posix()
            name = (blob_prefix.rstrip('/') + '/' + relative).strip('/')
            self.upload_file(path, name, overwrite=True)
            count += 1
        return count
