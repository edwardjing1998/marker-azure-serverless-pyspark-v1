"""Configuration for the Azure Container Apps GPU worker.

The worker runs in one container replica. PySpark is used for bounded metadata
inspection only; Marker/PyTorch inference runs on the container GPU, never in a
Spark UDF.
"""
from dataclasses import dataclass
from pathlib import Path
import re
import uuid
from shared.paths import roots, scoped_prefix, boolean


@dataclass
class Config:
    source_volume: str
    output_volume: str
    state_volume: str
    source_root: str = 'source/'
    output_root: str = 'generated/'
    source_prefix: str = 'source/'
    output_prefix: str = 'generated/'
    max_files: int = 1
    max_scan: int = 10000
    overwrite: bool = False
    replace_existing: bool = False
    dry_run: bool = True
    force_ocr: bool = False
    drop_handwriting: bool = False
    pages_per_chunk: int = 2
    compute_mode: str = 'gpu'
    gpu_enabled: bool = True
    cpu_max_pages: int = 20
    cpu_max_file_mb: int = 20
    cpu_timeout_seconds: int = 900
    cpu_max_rss_mb: int = 10000
    max_file_mb: int = 200
    max_document_pages: int = 5000
    cache_revision: str = 'azure-serverless-v1'
    release_id: str = 'manual'
    request_id: str = ''
    model_cache_dir: str = '/tmp/marker-models-v1.10.2'

    def validate(self):
        self.source_root, self.output_root = roots(self.source_root, self.output_root)
        self.source_prefix = scoped_prefix(self.source_prefix, self.source_root)
        self.output_prefix = scoped_prefix(self.output_prefix, self.output_root)
        self.request_id = self.request_id or uuid.uuid4().hex
        if not re.fullmatch(r'[a-f0-9]{32}', self.request_id):
            raise ValueError('Invalid request ID')
        if self.compute_mode not in ('auto', 'cpu', 'gpu'):
            raise ValueError('compute_mode must be auto, cpu, or gpu')
        if self.compute_mode == 'gpu' and not self.gpu_enabled:
            raise ValueError('GPU use is disabled by deployment policy')
        for value, lo, hi in (
            (self.max_files, 1, 100), (self.max_scan, 1, 100000),
            (self.pages_per_chunk, 1, 100), (self.cpu_max_pages, 1, 10000),
            (self.cpu_max_file_mb, 1, 1024), (self.max_file_mb, 1, 1024),
            (self.cpu_timeout_seconds, 1, 21600), (self.cpu_max_rss_mb, 256, 64000),
            (self.max_document_pages, 1, 10000),
        ):
            if not lo <= value <= hi:
                raise ValueError('A processing safeguard is outside the permitted range')
        if self.replace_existing and not self.overwrite:
            raise ValueError('replace_existing requires overwrite')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', self.cache_revision):
            raise ValueError('Invalid cache revision')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', self.release_id):
            raise ValueError('Invalid release ID')
        # Container-local mirror roots, never UC /Volumes paths.
        for name in ('source_volume', 'output_volume', 'state_volume'):
            p = Path(getattr(self, name))
            if not p.is_absolute() or '..' in p.parts:
                raise ValueError(f'{name} must be an absolute container-local path')
        return self

    @classmethod
    def from_env_and_params(cls, env, params):
        values = {
            'source_volume': env.get('LOCAL_SOURCE_PATH', '/work/source'),
            'output_volume': env.get('LOCAL_OUTPUT_PATH', '/work/generated'),
            'state_volume': env.get('LOCAL_STATE_PATH', '/work/state'),
            'source_root': env.get('AZURE_STORAGE_SOURCE_PREFIX', 'source/'),
            'output_root': env.get('AZURE_STORAGE_OUTPUT_PREFIX', 'generated/'),
            'max_scan': env.get('MAX_SCAN_FILES', '10000'),
            'cpu_max_pages': env.get('CPU_MAX_PAGES', '20'),
            'cpu_max_file_mb': env.get('CPU_MAX_FILE_MB', '20'),
            'cpu_timeout_seconds': env.get('CPU_TIMEOUT_SECONDS', '900'),
            'cpu_max_rss_mb': env.get('CPU_MAX_RSS_MB', '10000'),
            'max_file_mb': env.get('MAX_FILE_MB', '200'),
            'max_document_pages': env.get('MAX_DOCUMENT_PAGES', '5000'),
            'cache_revision': env.get('MARKER_CACHE_REVISION', 'azure-serverless-v1'),
            'release_id': env.get('RELEASE_ID', 'manual'),
            'model_cache_dir': env.get('MODEL_CACHE_DIR', '/tmp/marker-models-v1.10.2'),
            'gpu_enabled': env.get('GPU_FALLBACK_ENABLED', 'true'),
        }
        values.update(params)
        ints = {
            'max_scan', 'max_files', 'pages_per_chunk', 'cpu_max_pages',
            'cpu_max_file_mb', 'cpu_timeout_seconds', 'cpu_max_rss_mb',
            'max_file_mb', 'max_document_pages',
        }
        bools = {
            'gpu_enabled', 'overwrite', 'replace_existing', 'dry_run',
            'force_ocr', 'drop_handwriting',
        }
        return cls(**{
            k: int(v) if k in ints else boolean(v) if k in bools else v
            for k, v in values.items()
        }).validate()


def route_document(config, *, pages, size):
    if config.compute_mode == 'gpu':
        return 'gpu', 'requested_gpu'
    if config.compute_mode == 'cpu':
        return 'cpu', 'requested_cpu'
    if pages > config.cpu_max_pages:
        return 'gpu', 'page_threshold'
    if size > config.cpu_max_file_mb * 1024 * 1024:
        return 'gpu', 'size_threshold'
    return 'cpu', 'cpu_first'
