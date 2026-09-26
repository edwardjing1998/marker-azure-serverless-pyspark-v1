"""Convert a local snapshot, checkpoint pages, then publish a complete document."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile
import uuid
from shared.errors import PipelineError
from shared.paths import digest
from worker.assets import prepare_images, validate_magic
from worker.local_marker import LocalMarker
from worker.volumes import safe_child, sha256_file, write_json, read_json, replace_bytes


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def destination(config, relative):
    extra = config.output_prefix[len(config.output_root):]
    return safe_child(config.output_volume, extra + str(Path(relative).with_suffix('')))


def existing_output(config, relative):
    target = destination(config, relative) / 'content.md'
    if not target.exists():
        return None
    if not config.overwrite:
        return 'SKIP'
    with target.open(encoding='utf-8') as stream:
        header = stream.read(8192)
    own = any('provider: ' + json.dumps(p) in header for p in ('marker-local-cpu', 'marker-local-gpu', 'datalab-marker'))
    if not own and not config.replace_existing:
        raise PipelineError('Other-provider output requires overwrite=true and replaceExisting=true')
    return sha256_file(target)


def inspect_source(config, relative):
    path = safe_child(config.source_volume, relative)
    size = path.stat().st_size
    if size > config.max_file_mb * 1024 * 1024:
        raise PipelineError('Source exceeds MAX_FILE_MB')
    validate_magic(path, path.suffix.lower())
    pages = LocalMarker.page_count(path)
    if pages > config.max_document_pages:
        raise PipelineError('Source exceeds MAX_DOCUMENT_PAGES; no truncation is performed')
    return {'relative': relative, 'bytes': size, 'pages': pages}


def build_document(local, work, checkpoint_dir, config, device, fingerprint, engine=None):
    """Marker inference is ordinary Python, never executor/UDF code."""
    engine = engine or LocalMarker(config.model_cache_dir, device=device)
    total = engine.page_count(local)
    if total < 1 or total > config.max_document_pages:
        raise PipelineError('Invalid document page count')
    package = Path(work) / 'package'
    package.mkdir(parents=True, exist_ok=True)
    publication = uuid.uuid4().hex
    sections, chunk_index = [], []
    resumed = 0
    removed_count = 0
    for start in range(0, total, config.pages_per_chunk):
        pages = list(range(start, min(start + config.pages_per_chunk, total)))
        label = f'pages-{pages[0]+1:06d}-{pages[-1]+1:06d}'
        checkpoint = Path(checkpoint_dir) / (label + '.json')
        if checkpoint.exists():
            saved = read_json(checkpoint)
            if saved.get('fingerprint') != fingerprint or saved.get('pages') != pages:
                raise PipelineError('Checkpoint mismatch; change MARKER_CACHE_REVISION')
            result = saved['result']
            resumed += 1
        else:
            result = engine.convert_chunk(local, pages, force_ocr=config.force_ocr,
                                          drop_handwriting=config.drop_handwriting)
            saved = {'fingerprint': fingerprint, 'pages': pages, 'result': result}
            if len(json.dumps(saved).encode()) > 256 * 1024 * 1024:
                raise PipelineError('Checkpoint too large; reduce pagesPerChunk')
            write_json(checkpoint, saved)
        if result.get('pages') != pages or not isinstance(result.get('markdown'), str):
            raise PipelineError('Unexpected Marker page result')
        markdown, images = prepare_images(result, publication + '/' + label)
        image_map = {}
        for name, relative, content, _mime in images:
            target = safe_child(package, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            image_map[name] = relative
        metadata_path = f'marker/{publication}/{label}.json'
        audit = {k: v for k, v in result.items() if k != 'images'}
        audit['images'] = image_map
        write_json(safe_child(package, metadata_path), audit)
        removed_count += len(result.get('removedBlocks') or [])
        chunk_index.append({'pagesOneBased': [pages[0]+1, pages[-1]+1], 'metadata': metadata_path})
        sections.append(markdown)
    if not any(s.strip() for s in sections):
        raise PipelineError('Whole-document Markdown is empty; refusing publication')
    metadata_path = f'marker/{publication}/result.json'
    metadata = {'provider': 'marker-local-' + ('gpu' if device == 'cuda' else 'cpu'),
                'pageCount': total, 'isPartial': False, 'needsReview': True,
                'originalPixelsModified': False, 'handwritingPixelErasureImplemented': False,
                'signature': engine.signature(), 'chunks': chunk_index,
                'fingerprint': fingerprint, 'resumedChunks': resumed,
                'handwritingBlocksRemoved': removed_count, 'generatedAt': utc_now()}
    write_json(safe_child(package, metadata_path), metadata)
    # sourceBlob is completed in the publishing process, not guessed by Marker.
    metadata['markerMetadata'] = metadata_path
    (package / 'body.md').write_text('\n\n'.join(sections), encoding='utf-8')
    write_json(Path(work) / 'completed.json', metadata)
    return metadata


def convert_and_publish(config, record, device, *, runner=None, engine=None):
    """CPU subprocess work is isolated from Spark; files are published by the parent."""
    relative = record['relative']
    before = existing_output(config, relative)
    if before == 'SKIP':
        return {'sourceBlob': config.source_root + relative, 'status': 'SKIPPED'}
    source = safe_child(config.source_volume, relative)
    with tempfile.TemporaryDirectory(prefix='marker-local-') as tmp:
        work = Path(tmp)
        local = work / ('source' + source.suffix.lower())
        shutil.copyfile(source, local)
        if local.stat().st_size > config.max_file_mb * 1024 * 1024:
            raise PipelineError('Local snapshot exceeds size limit')
        validate_magic(local, local.suffix.lower())
        source_hash = sha256_file(local)
        signature = (engine or LocalMarker(config.model_cache_dir, device=device)).signature()
        fingerprint = digest([relative, source_hash, signature, config.force_ocr,
                              config.drop_handwriting, config.pages_per_chunk,
                              config.cache_revision, config.release_id])
        local_cp = work / 'checkpoints'
        local_cp.mkdir()
        durable_cp = Path(config.state_volume) / 'checkpoints' / fingerprint
        if durable_cp.exists():
            for path in durable_cp.glob('pages-*.json'):
                if path.stat().st_size > 256 * 1024 * 1024:
                    raise PipelineError('Checkpoint exceeds safety limit')
                shutil.copyfile(path, local_cp / path.name)
        try:
            if runner:
                runner(local, work, local_cp, config, device, fingerprint)
                result = read_json(work / 'completed.json')
            else:
                result = build_document(local, work, local_cp, config, device, fingerprint, engine=engine)
        finally:
            # Completed chunks survive even a terminated CPU subprocess.
            for path in local_cp.glob('pages-*.json'):
                saved = read_json(path)
                if saved.get('fingerprint') != fingerprint:
                    raise PipelineError('Invalid completed checkpoint')
                write_json(durable_cp / path.name, saved)
        if sha256_file(source) != source_hash:
            raise PipelineError('Source changed during conversion')
        target_root = destination(config, relative)
        content_path = target_root / 'content.md'
        def verify_output():
            current = sha256_file(content_path) if content_path.exists() else None
            if current != before:
                raise PipelineError('Output changed concurrently; refusing replacement')
        verify_output()
        package = work / 'package'
        for path in package.rglob('*'):
            if path.is_file() and path.name != 'body.md':
                target = target_root / path.relative_to(package)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        if before is not None:
            backup = target_root / Path(result['markerMetadata']).parent / 'previous-content.md'
            shutil.copyfile(content_path, backup)
        header = {'sourceBlob': config.source_root + relative,
                  'generatedAt': result['generatedAt'], 'provider': result['provider'],
                  'needsReview': True, 'markerMetadata': result['markerMetadata'],
                  'pageCount': result['pageCount']}
        markdown = '---\n' + '\n'.join(f'{k}: {json.dumps(v, ensure_ascii=False)}' for k,v in header.items()) + '\n---\n\n'
        markdown += (package / 'body.md').read_text(encoding='utf-8')
        verify_output()
        if sha256_file(source) != source_hash:
            raise PipelineError('Source changed before publication')
        replace_bytes(content_path, markdown.encode('utf-8'))
        return {'sourceBlob': config.source_root + relative, 'status': 'SUCCEEDED',
                'device': device, 'pageCount': result['pageCount'],
                'resumedChunks': result['resumedChunks'],
                'markdownBlob': config.output_root + str(content_path.relative_to(config.output_volume)),
                'needsReview': True}
