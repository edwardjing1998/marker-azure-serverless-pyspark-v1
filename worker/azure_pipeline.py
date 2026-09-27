"""Single-replica PySpark + Marker pipeline for Azure Container Apps GPU."""
import traceback
from pathlib import Path

from shared.errors import PipelineError
from worker.config import route_document
from worker.discovery import discover
from worker.document import convert_and_publish, existing_output, inspect_source, utc_now
from worker.local_marker import LocalMarker
from worker.volumes import write_json


def run_pipeline(config, spark):
    report = {
        'requestId': config.request_id,
        'status': 'RUNNING',
        'computeMode': config.compute_mode,
        'startedAt': utc_now(),
        'results': [],
        'succeeded': 0,
        'failed': 0,
        'needsGpu': 0,
        'skipped': 0,
        'reportBlob': f'_marker_jobs/runs/{config.request_id}/report.json',
    }

    report_path = Path(config.state_volume) / 'runs' / config.request_id / 'report.json'

    if not config.dry_run:
        write_json(report_path, report)

    print(
        f'[MARKER][pipeline] discovery started source_prefix={config.source_prefix} max_files={config.max_files}',
        flush=True,
    )

    records = discover(spark, config)

    print(
        f'[MARKER][pipeline] discovery completed records={len(records)}',
        flush=True,
    )

    gpu_engine = None
    attempted = 0

    for item in records:
        if attempted >= config.max_files:
            break

        attempted += 1
        source_blob = config.source_root + item['relative']

        try:
            if existing_output(config, item['relative']) == 'SKIP':
                result = {
                    'sourceBlob': source_blob,
                    'status': 'SKIPPED',
                }
            else:
                record = inspect_source(config, item['relative'])

                route, reason = route_document(
                    config,
                    pages=record['pages'],
                    size=record['bytes'],
                )

                print(
                    f'[MARKER][pipeline] routing source={source_blob} route={route} reason={reason}',
                    flush=True,
                )

                if route == 'gpu':
                    if not config.gpu_enabled:
                        raise PipelineError(
                            'GPU processing is disabled by deployment policy'
                        )

                    if gpu_engine is None:
                        gpu_engine = LocalMarker(
                            config.model_cache_dir,
                        )
                        gpu_engine.preflight()

                    result = convert_and_publish(
                        config,
                        record,
                        'cuda',
                        engine=gpu_engine,
                    )
                else:
                    result = convert_and_publish(
                        config,
                        record,
                        'cpu',
                    )

        except Exception as exc:
            message = f'{type(exc).__name__}: {exc}'

            print(
                f'[MARKER][pipeline][ERROR] source={source_blob} error={message}',
                flush=True,
            )

            traceback.print_exc()

            result = {
                'sourceBlob': source_blob,
                'status': 'FAILED',
                'error': message,
            }

        report['results'].append(result)

        status = result.get('status')

        if status == 'SUCCEEDED':
            report['succeeded'] += 1
        elif status == 'SKIPPED':
            report['skipped'] += 1
        else:
            report['failed'] += 1

        if not config.dry_run:
            write_json(report_path, report)

    report['status'] = (
        'COMPLETED'
        if report['failed'] == 0
        else 'COMPLETED_WITH_ERRORS'
    )

    report['finishedAt'] = utc_now()

    if not config.dry_run:
        write_json(report_path, report)

    print(
        f"[MARKER][pipeline] completed status={report['status']} succeeded={report['succeeded']} failed={report['failed']}",
        flush=True,
    )

    return report