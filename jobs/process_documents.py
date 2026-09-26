"""Azure Container Apps GPU job entrypoint.

PySpark runs in local mode inside the container for bounded metadata discovery.
Marker/PyTorch inference runs on CUDA in the same container process.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.errors import PipelineError
from worker.azure_blob import AzureBlobMirror
from worker.azure_pipeline import run_pipeline
from worker.config import Config
from worker.document import utc_now
from worker.volumes import write_json


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-prefix', default=os.getenv('SOURCE_PREFIX', 'source/'))
    parser.add_argument('--output-prefix', default=os.getenv('OUTPUT_PREFIX', 'generated/'))
    parser.add_argument('--max-files', default=os.getenv('MAX_FILES', '1'))
    parser.add_argument('--overwrite', default=os.getenv('OVERWRITE', 'false'))
    parser.add_argument('--replace-existing', default=os.getenv('REPLACE_EXISTING', 'false'))
    parser.add_argument('--dry-run', default=os.getenv('DRY_RUN', 'false'))
    parser.add_argument('--pages-per-chunk', default=os.getenv('PAGES_PER_CHUNK', '2'))
    parser.add_argument('--force-ocr', default=os.getenv('FORCE_OCR', 'false'))
    parser.add_argument('--drop-handwriting', default=os.getenv('DROP_HANDWRITING', 'false'))
    parser.add_argument('--compute-mode', default=os.getenv('COMPUTE_MODE', 'gpu'))
    parser.add_argument('--request-id', default=os.getenv('REQUEST_ID', ''))
    return vars(parser.parse_args())


def main():
    params = parse_args()
    config = Config.from_env_and_params(os.environ, {
        'source_prefix': params['source_prefix'],
        'output_prefix': params['output_prefix'],
        'max_files': params['max_files'],
        'overwrite': params['overwrite'],
        'replace_existing': params['replace_existing'],
        'dry_run': params['dry_run'],
        'pages_per_chunk': params['pages_per_chunk'],
        'force_ocr': params['force_ocr'],
        'drop_handwriting': params['drop_handwriting'],
        'compute_mode': params['compute_mode'],
        'request_id': params['request_id'],
    })

    endpoint = os.environ['AZURE_STORAGE_ENDPOINT']
    container = os.getenv('AZURE_STORAGE_CONTAINER', 'education')
    mirror = AzureBlobMirror(endpoint, container)

    work_root = Path(os.getenv('LOCAL_WORK_ROOT', '/work'))
    if work_root.exists():
        shutil.rmtree(work_root)
    Path(config.source_volume).mkdir(parents=True, exist_ok=True)
    Path(config.output_volume).mkdir(parents=True, exist_ok=True)
    Path(config.state_volume).mkdir(parents=True, exist_ok=True)

    print(f"[MARKER] Azure Container Apps GPU worker STARTED request_id={config.request_id}", flush=True)
    print(f"[MARKER] source_prefix={config.source_prefix} output_prefix={config.output_prefix} compute_mode={config.compute_mode}", flush=True)

    # Download only eligible source documents under the requested prefix.
    blobs = mirror.list_documents(config.source_prefix, config.max_scan)
    if not blobs:
        print('[MARKER] no source documents found', flush=True)
    for blob in blobs:
        mirror.download_blob(blob.name, work_root)

    # Mirror any existing output subtree for overwrite/provider checks.
    output_suffix = config.output_prefix[len(config.output_root):]
    if output_suffix:
        mirror.download_prefix(config.output_prefix, work_root)

    from pyspark.sql import SparkSession
    spark = None
    try:
        with mirror.global_run_lock():
            spark = (
                SparkSession.builder
                .master(os.getenv('SPARK_MASTER', 'local[*]'))
                .appName('marker-azure-serverless-pyspark')
                .config('spark.ui.enabled', 'false')
                .getOrCreate()
            )
            report = run_pipeline(config, spark)
    except Exception as exc:
        report = {
            'requestId': config.request_id,
            'status': 'FAILED',
            'succeeded': 0,
            'failed': 1,
            'needsGpu': 0,
            'finishedAt': utc_now(),
            'error': str(exc) if isinstance(exc, (PipelineError, ValueError)) else type(exc).__name__,
            'reportBlob': f'_marker_jobs/runs/{config.request_id}/report.json',
        }
        if not config.dry_run:
            write_json(Path(config.state_volume) / 'runs' / config.request_id / 'report.json', report)
    finally:
        if spark is not None:
            spark.stop()

    if not config.dry_run:
        # Generated content: content.md naturally uploads last within each tree.
        mirror.upload_tree(Path(config.output_volume), config.output_root.rstrip('/'), content_md_last=True)
        mirror.upload_tree(Path(config.state_volume), '_marker_jobs', content_md_last=False)

    print('MARKER_SUMMARY=' + json.dumps(report, separators=(',', ':')), flush=True)
    return 1 if report.get('failed', 0) else 0


if __name__ == '__main__':
    raise SystemExit(main())
