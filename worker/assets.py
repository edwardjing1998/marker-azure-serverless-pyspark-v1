"""Pure publication helpers retained from the previous application."""
import base64
import binascii
import html
import mimetypes
import re
from pathlib import PurePosixPath
from urllib.parse import quote
from shared.errors import PipelineError

def rewrite_image_links(markdown, original, replacement):
    # Rewrite destinations, not every occurrence of a filename in prose.
    variants = {original, quote(original, safe='/'), html.escape(original, quote=True)}
    for value in sorted(variants, key=len, reverse=True):
        name = re.escape(value)
        patterns = [r'(\]\(\s*<?)(?:\./)?' + name + r'(?=>?(?:\s|\)))',
                    r'(?m)^(\s{0,3}\[[^\]\n]+\]:\s*<?)(?:\./)?' + name + r'(?=>?(?:\s|$))',
                    r'''(\bsrc\s*=\s*["'])(?:\./)?''' + name + r'''(?=["'])''']
        for pattern in patterns:
            markdown = re.sub(pattern, lambda match: match.group(1) + replacement, markdown)
    return markdown


def prepare_images(result, publication):
    markdown = result['markdown']
    prepared = []
    images = result.get('images') or {}
    if not isinstance(images, dict):
        raise PipelineError('Invalid images response')
    for index, (name, encoded) in enumerate(images.items(), start=1):
        if not isinstance(name, str) or not isinstance(encoded, str):
            raise PipelineError('Unsupported image response entry')
        if name.startswith('/') or '\\' in name or any(p in ('', '.', '..') for p in name.split('/')):
            raise PipelineError('Unsafe figure filename from provider')
        suffix = PurePosixPath(name).suffix.lower()
        if suffix not in ('.png', '.jpg', '.jpeg', '.webp', '.gif'):
            raise PipelineError('Unsupported extracted image format; no active SVG/HTML is published')
        if encoded.startswith('data:'):
            if ';base64,' not in encoded:
                raise PipelineError('Unsupported image data URI')
            encoded = encoded.split(';base64,', 1)[1]
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise PipelineError('Invalid base64 figure from provider') from None
        if not content or len(content) > 50 * 1024 * 1024:
            raise PipelineError('Extracted figure is empty or exceeds the 50 MB safeguard')
        relative = f'figures/{publication}/figure-{index:03d}{suffix}'
        markdown = rewrite_image_links(markdown, name, relative)
        prepared.append((name, relative, content, mimetypes.guess_type(relative)[0] or 'application/octet-stream'))
    return markdown, prepared


def validate_magic(path, suffix):
    with open(path, 'rb') as source:
        head = source.read(1024)
    valid = ((suffix == '.pdf' and b'%PDF-' in head) or
             (suffix == '.png' and head.startswith(b'\x89PNG\r\n\x1a\n')) or
             (suffix in ('.jpg', '.jpeg') and head.startswith(b'\xff\xd8\xff')))
    if not valid:
        raise PipelineError('File contents do not match its PDF/PNG/JPEG extension')


