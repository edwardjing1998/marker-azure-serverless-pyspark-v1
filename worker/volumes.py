"""UC-volume file I/O. No Azure account keys, direct cloud clients, or Spark credentials.

Only one parent run is allowed. The sticky lock is an operational guard, not a
replacement for a transactional cross-application lock. Test FUSE rename support
before publishing; this adapter is deliberately fail-closed when unsupported.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid
from shared.errors import PipelineError


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def safe_child(root, relative):
    parts = relative.split('/')
    if not relative or relative.startswith('/') or any(p in ('', '.', '..') for p in parts) or '\\' in relative or '\x00' in relative:
        raise PipelineError('Unsafe relative document path')
    root = Path(root).resolve()
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise PipelineError('Path escapes the configured volume')
    return path


def replace_bytes(path, payload):
    """Write a complete sibling file first, then rename; no partial-buffer writes.

    Atomic rename semantics depend on the target FUSE implementation. This is not
    an Azure ETag compare-and-swap or a multi-file transaction.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('wb') as stream:
            stream.write(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    replace_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))


def read_json(path, max_mb=256):
    path = Path(path)
    if path.stat().st_size > max_mb * 1024 * 1024:
        raise PipelineError('JSON/checkpoint exceeds size safeguard')
    with path.open(encoding='utf-8') as stream:
        return json.load(stream)


def probe_volume(path):
    path = Path(path)
    if not path.is_dir():
        raise PipelineError('Configured volume path is missing or not accessible')
    probe = path / ('.marker-probe-' + uuid.uuid4().hex)
    try:
        replace_bytes(probe, b'marker-uc-probe')
        if probe.read_bytes() != b'marker-uc-probe':
            raise PipelineError('Volume verification failed')
    except OSError:
        raise PipelineError('Volume write/rename smoke test failed; verify permissions and FUSE support') from None
    finally:
        probe.unlink(missing_ok=True)


class RunLock:
    """Never automatically steal a lock after an uncertain child-GPU submission."""
    def __init__(self, state_root, request_id):
        self.path = Path(state_root) / 'locks' / 'cpu-first.lock'
        self.request_id = request_id
        self.release_allowed = True

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.mkdir()
        except FileExistsError:
            raise PipelineError('Previous workflow lock exists. Confirm parent and child runs have stopped before operator recovery') from None
        write_json(self.path / 'owner.json', {'requestId': self.request_id})
        return self

    def hold(self, **details):
        self.release_allowed = False
        write_json(self.path / 'owner.json', {'requestId': self.request_id, **details})

    def clear_child(self):
        self.release_allowed = True

    def __exit__(self, *args):
        if self.release_allowed:
            shutil.rmtree(self.path)
