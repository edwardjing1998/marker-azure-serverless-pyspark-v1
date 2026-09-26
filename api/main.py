"""OpenShift REST gateway that launches Azure Container Apps serverless-GPU jobs."""
import os
import re
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from api.azure_containerapps_client import AzureContainerAppsJobsClient, AzureRemoteError
from shared.paths import digest, roots, scoped_prefix


@asynccontextmanager
async def lifespan(app):
    app.state.gpu_enabled = os.getenv('GPU_FALLBACK_ENABLED', 'true').lower() == 'true'
    app.state.source, app.state.output = roots(
        os.getenv('AZURE_STORAGE_SOURCE_PREFIX', 'source/'),
        os.getenv('AZURE_STORAGE_OUTPUT_PREFIX', 'generated/'),
    )
    app.state.client = AzureContainerAppsJobsClient()
    yield


app = FastAPI(
    title='marker-azure-serverless-pyspark',
    version='4.0.0',
    lifespan=lifespan,
    description='OpenShift REST gateway to an Azure Container Apps serverless-GPU PySpark + Marker job.',
)


@app.exception_handler(AzureRemoteError)
async def remote_error(_request, exception):
    return JSONResponse(status_code=502, content={'detail': str(exception)})


@app.get('/health/live', include_in_schema=False)
@app.get('/health/ready', include_in_schema=False)
def health():
    return {'status': 'UP', 'application': 'marker-azure-serverless-pyspark'}


@app.post('/api/storage-documents/process', status_code=202)
def process(
    request: Request,
    sourcePrefix: str | None = None,
    outputPrefix: str | None = None,
    maxFiles: int = Query(1, ge=1, le=100),
    overwrite: bool = False,
    replaceExisting: bool = False,
    dryRun: bool = False,
    pagesPerChunk: int = Query(2, ge=1, le=100),
    forceOcr: bool = False,
    dropHandwriting: bool = False,
    computeMode: str = Query('gpu', pattern='^(auto|cpu|gpu)$'),
    mode: str | None = Query(None, deprecated=True),
    idempotency_key: str | None = Header(None, alias='Idempotency-Key'),
):
    if computeMode == 'gpu' and not request.app.state.gpu_enabled:
        raise HTTPException(400, 'GPU processing is disabled by deployment policy')
    if mode is not None:
        raise HTTPException(400, 'Hosted API mode was removed')
    if replaceExisting and not overwrite:
        raise HTTPException(400, 'replaceExisting=true requires overwrite=true')
    try:
        source = scoped_prefix(sourcePrefix, request.app.state.source)
        output = scoped_prefix(outputPrefix, request.app.state.output)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    key = idempotency_key or uuid.uuid4().hex
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', key):
        raise HTTPException(400, 'Idempotency-Key must be 1-64 ASCII letters, digits, or ._:-')
    request_id = digest([key, source, output, maxFiles, overwrite, replaceExisting, dryRun, pagesPerChunk,
                         forceOcr, dropHandwriting, computeMode])[:32]
    params = {
        'source_prefix': source,
        'output_prefix': output,
        'max_files': str(maxFiles),
        'overwrite': str(overwrite).lower(),
        'replace_existing': str(replaceExisting).lower(),
        'dry_run': str(dryRun).lower(),
        'pages_per_chunk': str(pagesPerChunk),
        'force_ocr': str(forceOcr).lower(),
        'drop_handwriting': str(dropHandwriting).lower(),
        'compute_mode': computeMode,
        'request_id': request_id,
    }
    execution = request.app.state.client.start(params)
    run_id = execution['name']
    return {
        'status': 'ACCEPTED',
        'runId': run_id,
        'requestId': request_id,
        'idempotencyKey': key,
        'dryRun': dryRun,
        'statusPath': f'/api/storage-documents/runs/{run_id}',
        'reportBlob': None if dryRun else f'_marker_jobs/runs/{request_id}/report.json',
        'note': 'Accepted means the Azure Container Apps job execution was created. Poll statusPath.',
    }


@app.get('/api/storage-documents/runs/{run_id}')
def run_status(request: Request, run_id: str):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', run_id):
        raise HTTPException(400, 'Invalid run ID')
    execution = request.app.state.client.execution(run_id)
    if execution is None:
        raise HTTPException(404, 'Run was not found')
    props = execution.get('properties', {})
    env = {}
    for container in props.get('template', {}).get('containers', []):
        for item in container.get('env', []):
            if 'value' in item:
                env[item.get('name')] = item.get('value')
    request_id = env.get('REQUEST_ID')
    out = {
        'runId': run_id,
        'lifeCycleState': props.get('status'),
        'resultState': props.get('status'),
        'reason': props.get('reason'),
        'startTime': props.get('startTime'),
        'endTime': props.get('endTime'),
    }
    if request_id and re.fullmatch(r'[a-f0-9]{32}', request_id):
        out['requestId'] = request_id
        out['reportBlob'] = f'_marker_jobs/runs/{request_id}/report.json'
    return out
