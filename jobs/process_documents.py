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
import tempfile
import traceback

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

    parser.add_argument(
        '--source-prefix',
        default=os.getenv(
            'SOURCE_PREFIX',
            'source/',
        ),
    )

    parser.add_argument(
        '--output-prefix',
        default=os.getenv(
            'OUTPUT_PREFIX',
            'generated/',
        ),
    )

    parser.add_argument(
        '--max-files',
        default=os.getenv(
            'MAX_FILES',
            '1',
        ),
    )

    parser.add_argument(
        '--overwrite',
        default=os.getenv(
            'OVERWRITE',
            'false',
        ),
    )

    parser.add_argument(
        '--replace-existing',
        default=os.getenv(
            'REPLACE_EXISTING',
            'false',
        ),
    )

    parser.add_argument(
        '--dry-run',
        default=os.getenv(
            'DRY_RUN',
            'false',
        ),
    )

    parser.add_argument(
        '--pages-per-chunk',
        default=os.getenv(
            'PAGES_PER_CHUNK',
            '2',
        ),
    )

    parser.add_argument(
        '--force-ocr',
        default=os.getenv(
            'FORCE_OCR',
            'false',
        ),
    )

    parser.add_argument(
        '--drop-handwriting',
        default=os.getenv(
            'DROP_HANDWRITING',
            'false',
        ),
    )

    parser.add_argument(
        '--compute-mode',
        default=os.getenv(
            'COMPUTE_MODE',
            'gpu',
        ),
    )

    parser.add_argument(
        '--request-id',
        default=os.getenv(
            'REQUEST_ID',
            '',
        ),
    )

    return vars(
        parser.parse_args()
    )


def clear_directory(
    directory: Path,
) -> None:
    """
    Clear a runtime working directory without deleting the directory itself.

    The GPU container can run as a non-root UID. We therefore keep the
    runtime directories in place and delete only their contents.
    """

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    for child in directory.iterdir():

        if (
            child.is_dir()
            and not child.is_symlink()
        ):
            shutil.rmtree(
                child
            )

        else:
            child.unlink()


def write_fatal_report(
    request_id: str,
    exc: BaseException,
) -> None:
    """
    Best-effort fatal crash reporting.

    If the normal report.json path is never reached, attempt to upload:

        _marker_jobs/runs/<requestId>/fatal.json
    """

    request_id = (
        request_id
        or os.getenv(
            'REQUEST_ID',
            '',
        )
        or 'unknown'
    )

    fatal_blob = (
        f'_marker_jobs/runs/'
        f'{request_id}/fatal.json'
    )

    trace = traceback.format_exc()

    payload = {
        'requestId':
            request_id,

        'status':
            'FATAL',

        'errorType':
            type(exc).__name__,

        'error':
            str(exc),

        'traceback':
            trace,

        'finishedAt':
            utc_now(),

        'fatalBlob':
            fatal_blob,
    }

    print(
        '[MARKER][FATAL] '
        f'{type(exc).__name__}: '
        f'{exc}',
        flush=True,
    )

    print(
        trace,
        flush=True,
    )

    try:

        endpoint = os.environ[
            'AZURE_STORAGE_ENDPOINT'
        ]

        container = os.getenv(
            'AZURE_STORAGE_CONTAINER',
            'education',
        )

        mirror = AzureBlobMirror(
            endpoint,
            container,
        )

        with tempfile.TemporaryDirectory(
            prefix='marker-fatal-',
        ) as tmp_dir:

            local_file = (
                Path(tmp_dir)
                / 'fatal.json'
            )

            local_file.write_text(
                json.dumps(
                    payload,
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding='utf-8',
            )

            mirror.upload_file(
                local_file,
                fatal_blob,
                overwrite=True,
            )

        print(
            '[MARKER][FATAL] '
            'crash report written to '
            f'{fatal_blob}',
            flush=True,
        )

    except Exception as report_exc:

        print(
            '[MARKER][FATAL] '
            'unable to write crash report: '
            f'{type(report_exc).__name__}: '
            f'{report_exc}',
            flush=True,
        )


def main():

    params = parse_args()

    config = Config.from_env_and_params(
        os.environ,
        {
            'source_prefix':
                params[
                    'source_prefix'
                ],

            'output_prefix':
                params[
                    'output_prefix'
                ],

            'max_files':
                params[
                    'max_files'
                ],

            'overwrite':
                params[
                    'overwrite'
                ],

            'replace_existing':
                params[
                    'replace_existing'
                ],

            'dry_run':
                params[
                    'dry_run'
                ],

            'pages_per_chunk':
                params[
                    'pages_per_chunk'
                ],

            'force_ocr':
                params[
                    'force_ocr'
                ],

            'drop_handwriting':
                params[
                    'drop_handwriting'
                ],

            'compute_mode':
                params[
                    'compute_mode'
                ],

            'request_id':
                params[
                    'request_id'
                ],
        },
    )

    endpoint = os.environ[
        'AZURE_STORAGE_ENDPOINT'
    ]

    container = os.getenv(
        'AZURE_STORAGE_CONTAINER',
        'education',
    )

    mirror = AzureBlobMirror(
        endpoint,
        container,
    )

    #
    # LOCAL_WORK_ROOT now defaults to /tmp/marker-work.
    #
    # Dockerfile.gpu also creates:
    #
    #     /work -> /tmp/marker-work
    #
    # Therefore older configuration values referencing /work/source,
    # /work/generated, or /work/state remain compatible.
    #
    work_root = Path(
        os.getenv(
            'LOCAL_WORK_ROOT',
            '/tmp/marker-work',
        )
    )

    source_dir = Path(
        config.source_volume
    )

    output_dir = Path(
        config.output_volume
    )

    state_dir = Path(
        config.state_volume
    )

    print(
        '[MARKER] '
        f'local_work_root={work_root}',
        flush=True,
    )

    print(
        '[MARKER] '
        f'source_volume={source_dir}',
        flush=True,
    )

    print(
        '[MARKER] '
        f'output_volume={output_dir}',
        flush=True,
    )

    print(
        '[MARKER] '
        f'state_volume={state_dir}',
        flush=True,
    )

    #
    # Never delete /work or LOCAL_WORK_ROOT.
    #
    # Clear only the contents of the individual runtime directories.
    #
    work_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    clear_directory(
        source_dir
    )

    clear_directory(
        output_dir
    )

    clear_directory(
        state_dir
    )

    print(
        '[MARKER] '
        'Azure Container Apps GPU worker STARTED '
        f'request_id={config.request_id}',
        flush=True,
    )

    print(
        '[MARKER] '
        f'source_prefix={config.source_prefix} '
        f'output_prefix={config.output_prefix} '
        f'compute_mode={config.compute_mode}',
        flush=True,
    )

    #
    # Download eligible source documents from Azure Blob Storage.
    #
    blobs = mirror.list_documents(
        config.source_prefix,
        config.max_scan,
    )

    if not blobs:

        print(
            '[MARKER] '
            'no source documents found',
            flush=True,
        )

    for blob in blobs:

        mirror.download_blob(
            blob.name,
            work_root,
        )

    #
    # Download the existing Azure output subtree when needed.
    #
    output_suffix = (
        config.output_prefix[
            len(
                config.output_root
            ):
        ]
    )

    if output_suffix:

        mirror.download_prefix(
            config.output_prefix,
            work_root,
        )

    #
    # Start Spark.
    #
    from pyspark.sql import SparkSession

    spark = None

    try:

        with mirror.global_run_lock():

            spark = (
                SparkSession
                .builder
                .master(
                    os.getenv(
                        'SPARK_MASTER',
                        'local[*]',
                    )
                )
                .appName(
                    'marker-azure-serverless-pyspark'
                )
                .config(
                    'spark.ui.enabled',
                    'false',
                )
                .getOrCreate()
            )

            report = run_pipeline(
                config,
                spark,
            )

    except Exception as exc:

        #
        # Preserve more detail about the conversion exception.
        #
        print(
            '[MARKER][PIPELINE][ERROR] '
            f'{type(exc).__name__}: '
            f'{exc}',
            flush=True,
        )

        traceback.print_exc()

        report = {
            'requestId':
                config.request_id,

            'status':
                'FAILED',

            'succeeded':
                0,

            'failed':
                1,

            'needsGpu':
                0,

            'finishedAt':
                utc_now(),

            'errorType':
                type(exc).__name__,

            'error':
                str(exc),

            'reportBlob':
                (
                    f'_marker_jobs/runs/'
                    f'{config.request_id}/'
                    f'report.json'
                ),
        }

        if not config.dry_run:

            write_json(
                (
                    Path(
                        config.state_volume
                    )
                    / 'runs'
                    / config.request_id
                    / 'report.json'
                ),
                report,
            )

    finally:

        if spark is not None:

            spark.stop()

    #
    # Upload generated files to Azure Blob Storage.
    #
    #
    # IMPORTANT:
    #
    # config.output_volume is LOCAL container storage.
    #
    # config.output_prefix is the AZURE BLOB prefix.
    #
    # Therefore local files can live under:
    #
    #     /tmp/marker-work/generated/
    #
    # while permanent Azure files still go to:
    #
    #     education/generated/...
    #
    if not config.dry_run:

        mirror.upload_tree(
            Path(
                config.output_volume
            ),
            config.output_root.rstrip(
                '/'
            ),
            content_md_last=True,
        )

        mirror.upload_tree(
            Path(
                config.state_volume
            ),
            '_marker_jobs',
            content_md_last=False,
        )

    print(
        'MARKER_SUMMARY='
        + json.dumps(
            report,
            separators=(
                ',',
                ':',
            ),
        ),
        flush=True,
    )

    return (
        1
        if report.get(
            'failed',
            0,
        )
        else 0
    )


def run():
    """
    Top-level worker boundary.

    Exceptions that happen before normal report.json handling are
    captured in fatal.json whenever Azure Blob Storage is reachable.
    """

    request_id = os.getenv(
        'REQUEST_ID',
        '',
    )

    try:

        return main()

    except SystemExit:

        raise

    except BaseException as exc:

        write_fatal_report(
            request_id,
            exc,
        )

        return 1


if __name__ == '__main__':

    raise SystemExit(
        run()
    )