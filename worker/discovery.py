"""Actual PySpark metadata processing, compatible with Spark Connect.

Marker is NOT invoked inside a Spark UDF. Binary content is projected out before
collection, and the driver list is explicitly bounded.
"""
from pathlib import Path
from urllib.parse import unquote, urlsplit
from shared.errors import PipelineError


def document_metadata(spark, root):
    from pyspark.sql import functions as F
    return (spark.read.format('binaryFile').option('recursiveFileLookup', 'true').load(root)
        .select('path', 'length', 'modificationTime')
        .withColumn('extension', F.lower(F.regexp_extract('path', r'\.([^./]+)$', 1)))
        .where(F.col('extension').isin('pdf', 'png', 'jpg', 'jpeg'))
        .withColumn('stem', F.regexp_replace('path', r'\.[^./]+$', '')))


def discover(spark, config):
    suffix = config.source_prefix[len(config.source_root):].rstrip('/')
    root = Path(config.source_volume) / suffix
    if not root.is_dir():
        raise PipelineError('Source prefix does not exist or is not readable')
    # Empty directories make binaryFile schema inference fail on some runtimes.
    if not any(root.iterdir()):
        return []
    frame = document_metadata(spark, str(root))
    collisions = frame.groupBy('stem').count().where('count > 1').limit(1).collect()
    if collisions:
        raise PipelineError('PDF/image filename stems collide; rename inputs before conversion')
    rows = frame.orderBy('path').limit(config.max_scan + 1).collect()
    if len(rows) > config.max_scan:
        raise PipelineError('Source exceeds MAX_SCAN_FILES; select a narrower sourcePrefix')
    records = []
    configured = Path(config.source_volume).resolve()
    for row in rows:
        raw = row.path
        if raw.startswith('dbfs:'):
            path = Path(unquote(raw[5:]))
        elif raw.startswith('file:'):
            path = Path(unquote(urlsplit(raw).path))
        else:
            path = Path(raw)
        try:
            relative = str(path.resolve().relative_to(configured))
        except ValueError:
            raise PipelineError('Spark returned a path outside the source volume') from None
        records.append({'relative': relative, 'bytes': int(row.length)})
    return records
