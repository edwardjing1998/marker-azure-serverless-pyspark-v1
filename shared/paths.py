"""Provider-neutral path and request validation. No network access."""
import hashlib
import json
from pathlib import PurePosixPath

SUPPORTED = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}


def prefix(value: str) -> str:
    value = value.strip().replace('\\', '/')
    if not value or value.startswith('/') or any(ord(c) < 32 for c in value):
        raise ValueError('A nonempty relative blob prefix is required')
    if any(part in ('', '.', '..') for part in value.rstrip('/').split('/')):
        raise ValueError('Prefix must not contain empty, dot, or parent segments')
    return value.rstrip('/') + '/'


def roots(source: str, output: str) -> tuple[str, str]:
    source, output = prefix(source), prefix(output)
    if source.startswith(output) or output.startswith(source):
        raise ValueError('Source and output prefixes must not overlap')
    if source.startswith('_marker_jobs/') or output.startswith('_marker_jobs/'):
        raise ValueError('_marker_jobs/ is reserved for private job state')
    return source, output


def scoped_prefix(value: str | None, root: str) -> str:
    candidate = prefix(value or root)
    if not candidate.startswith(root):
        raise ValueError('Requested prefix must be inside the configured root')
    return candidate


def output_base(name: str, source_root: str, output_root: str) -> str:
    # Filtering a subfolder must not change the destination of the same source.
    if not name.startswith(source_root):
        raise ValueError('Source is outside the configured root')
    relative = name[len(source_root):]
    if not relative or any(p in ('', '.', '..') for p in relative.split('/')):
        raise ValueError('Unsupported source blob path')
    ext = PurePosixPath(relative).suffix
    if ext.lower() not in SUPPORTED:
        raise ValueError('Only PDF, PNG, JPG, and JPEG are supported')
    return output_root + relative[:-len(ext)]


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def boolean(value: str) -> bool:
    if str(value).lower() not in ('true', 'false'):
        raise ValueError('Boolean values must be true or false')
    return str(value).lower() == 'true'
