"""Version-specific adapter for in-process Marker 1.10.2 on an explicitly selected CPU or CUDA GPU.

ML imports are lazy: unit tests and the lightweight OpenShift image do not need
Torch/Marker. No hosted conversion client or external LLM service is configured.
"""
import base64
import gc
import io
import os
import time
from importlib.metadata import version
from pathlib import Path

from shared.errors import PipelineError, GpuFatalError, CpuResourceLimit

MARKER_VERSION = '1.10.2'
SURYA_VERSION = '0.17.1'
TRANSFORMERS_VERSION = '4.56.2'
TORCH_VERSION = '2.7.1'
# Small initial batches are intentional; measure before increasing them.
BATCH_CONFIG = {
    'DETECTOR_BATCH_SIZE': '4', 'RECOGNITION_BATCH_SIZE': '16',
    'LAYOUT_BATCH_SIZE': '2', 'TABLE_REC_BATCH_SIZE': '2',
    'OCR_ERROR_BATCH_SIZE': '4', 'DETECTOR_POSTPROCESSING_CPU_WORKERS': '1',
    'COMPILE_ALL': 'false', 'COMPILE_FOUNDATION': 'false',
}
CHECKPOINTS = {
    'DETECTOR_MODEL_CHECKPOINT': 's3://text_detection/2025_05_07',
    'RECOGNITION_MODEL_CHECKPOINT': 's3://text_recognition/2025_09_23',
    'FOUNDATION_MODEL_CHECKPOINT': 's3://text_recognition/2025_09_23',
    'LAYOUT_MODEL_CHECKPOINT': 's3://layout/2025_09_23',
    'TABLE_REC_MODEL_CHECKPOINT': 's3://table_recognition/2025_02_18',
    'OCR_ERROR_MODEL_CHECKPOINT': 's3://ocr_error_detection/2025_02_18',
}


def remove_handwriting_references(document):
    """Remove only reachable Handwriting blocks, never their page-image pixels.

    The raw JSON is rendered BEFORE this mutation. Walk structure references,
    not the full children registry (which also contains replaced/inactive blocks).
    """
    removed, visited = [], set()

    def visit(parent):
        kept = []
        for block_id in list(getattr(parent, 'structure', None) or []):
            block = document.get_block(block_id)
            if block is None:
                raise PipelineError('Marker document contains an unresolved block reference')
            name = getattr(block.block_type, 'name', str(block.block_type))
            identifier = str(block.id)
            if name == 'Handwriting':
                if identifier not in visited:
                    removed.append({
                        'id': identifier, 'pageIndex': block.page_id,
                        'blockType': name, 'polygon': block.polygon.polygon,
                        'recognizedText': block.raw_text(document),
                    })
                visited.add(identifier)
                continue
            kept.append(block_id)
            if identifier not in visited:
                visited.add(identifier)
                visit(block)
        if getattr(parent, 'structure', None) is not None:
            parent.structure = kept

    for page in document.pages:
        visit(page)
    return removed


class LocalMarker:
    def __init__(self, model_cache_dir='/tmp/marker-models-v1.10.2', device='cpu'):
        if device not in ('cpu', 'cuda'):
            raise ValueError('device must be cpu or cuda; no automatic hardware selection')
        self.device = device
        self.model_cache_dir = str(model_cache_dir)
        self.models = None
        self.device_info = None
        self._torch = None

    def signature(self):
        return {'adapter': 'local-marker/v3', 'device': self.device, 'marker': MARKER_VERSION,
                'surya': SURYA_VERSION, 'transformers': TRANSFORMERS_VERSION,
                'torch': TORCH_VERSION, 'models': CHECKPOINTS, 'batches': BATCH_CONFIG}

    def preflight(self):
        """No model downloads here. Verify packages and a real operation on the selected device."""
        if self.device_info is not None:
            return self.device_info
        for name, expected in (('marker-pdf', MARKER_VERSION), ('surya-ocr', SURYA_VERSION),
                               ('transformers', TRANSFORMERS_VERSION), ('torch', TORCH_VERSION)):
            actual = version(name).split('+')[0]
            if actual != expected:
                raise PipelineError(f'Expected {name}=={expected}; installed {actual}. check the pinned task dependencies')
        import torch
        self._torch = torch
        if self.device == 'cpu':
            torch.set_num_threads(min(4, os.cpu_count() or 1))
            if (torch.ones(4, device='cpu') * 2).sum().item() != 8:
                raise PipelineError('CPU tensor preflight failed')
            self.device_info = {'device': 'cpu', 'torch': torch.__version__}
            return self.device_info
        if not torch.cuda.is_available():
            raise GpuFatalError('CUDA unavailable. Select Serverless GPU, not CPU Serverless or a SQL warehouse')
        if torch.version.cuda is None:
            raise GpuFatalError('CPU-only Torch wheel installed; use requirements-gpu.txt')
        try:
            # Verify execution, rather than relying only on device enumeration.
            value = (torch.ones(4, device='cuda') * 2).sum().item()
            torch.cuda.synchronize()
            if value != 8:
                raise RuntimeError('CUDA sanity check failed')
        except Exception:
            raise GpuFatalError('CUDA execution failed. Check the managed driver and pinned CUDA wheel compatibility') from None
        device = torch.cuda.get_device_properties(0)
        self.device_info = {
            'device': torch.cuda.get_device_name(0),
            'cudaRuntime': torch.version.cuda, 'torch': torch.__version__,
            'gpuMemoryGiB': round(device.total_memory / 1024**3, 2),
        }
        return self.device_info

    def _load_models(self):
        self.preflight()
        if self.models is not None:
            return
        # Must happen before importing Marker / Surya settings.
        os.environ['TORCH_DEVICE'] = self.device
        os.environ['TOKENIZERS_PARALLELISM'] = 'false'
        os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
        os.environ['DO_NOT_TRACK'] = '1'
        os.environ['MODEL_CACHE_DIR'] = self.model_cache_dir
        os.environ['HF_HOME'] = '/tmp/marker-huggingface-v1.10.2'
        os.environ.update(BATCH_CONFIG)
        os.environ.update(CHECKPOINTS)
        Path(self.model_cache_dir).mkdir(parents=True, exist_ok=True)
        from marker.models import create_model_dict
        try:
            self.models = create_model_dict(device=self.device,
                dtype=self._torch.float32 if self.device == 'cpu' else self._torch.float16)
        except (MemoryError, self._torch.OutOfMemoryError):
            if self.device == 'cpu':
                raise CpuResourceLimit('CPU model loading exceeded memory') from None
            raise GpuFatalError('GPU model loading exceeded memory') from None
        except Exception as exc:
            # Model-loading exceptions can contain download URLs. Avoid printing them.
            raise PipelineError(f'Marker model loading failed ({type(exc).__name__}); check dependencies and model-download access') from None

    @staticmethod
    def page_count(path):
        if Path(path).suffix.lower() == '.pdf':
            import pypdfium2 as pdfium
            try:
                with pdfium.PdfDocument(str(path)) as document:
                    count = len(document)
            except Exception:
                raise PipelineError('Cannot open PDF; check encryption or corruption') from None
            if count < 1:
                raise PipelineError('PDF has no pages')
            return count
        from PIL import Image
        try:
            with Image.open(path) as image:
                if getattr(image, 'n_frames', 1) != 1:
                    raise PipelineError('Animated/multi-frame images are unsupported; convert to separate source files')
                image.verify()
        except PipelineError:
            raise
        except Exception:
            raise PipelineError('Image cannot be decoded safely') from None
        return 1

    def convert_chunk(self, path, pages, *, force_ocr=False, drop_handwriting=False):
        if not pages or pages != list(range(pages[0], pages[-1] + 1)):
            raise ValueError('pages must be a nonempty consecutive zero-based page list')
        self._load_models()
        from marker.converters.pdf import PdfConverter
        from marker.renderers.json import JSONRenderer
        from marker.renderers.markdown import MarkdownRenderer
        from marker.output import text_from_rendered
        config = {
            'page_range': pages, 'use_llm': False, 'force_ocr': force_ocr,
            'extract_images': True, 'keep_pageheader_in_output': False,
            'keep_pagefooter_in_output': False, 'disable_tqdm': True,
        }
        started = time.monotonic()
        document = None
        rendered = None
        try:
            converter = PdfConverter(artifact_dict=self.models, config=config)
            with self._torch.inference_mode():
                document = converter.build_document(str(path))
                if [p.page_id for p in document.pages] != pages:
                    raise PipelineError('Marker returned unexpected pages; refusing a partial conversion')
                # No duplicate image payloads in audit JSON; Markdown owns image assets.
                raw = JSONRenderer(config={**config, 'extract_images': False,
                                           'keep_pageheader_in_output': True,
                                           'keep_pagefooter_in_output': True})(document).model_dump(mode='json')
                removed = remove_handwriting_references(document) if drop_handwriting else []
                rendered = MarkdownRenderer(config=config)(document)
                markdown, extension, images = text_from_rendered(rendered)
                if extension != 'md' or not isinstance(markdown, str):
                    raise PipelineError('Marker renderer did not return Markdown')
                encoded = {}
                for name, image in images.items():
                    suffix = Path(name).suffix.lower()
                    image_format = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG'}.get(suffix)
                    if image_format is None:
                        raise PipelineError('Unexpected Marker figure format')
                    with io.BytesIO() as buffer:
                        image.convert('RGB').save(buffer, format=image_format)
                        encoded[name] = base64.b64encode(buffer.getvalue()).decode('ascii')
            return {
                'pages': pages, 'markdown': markdown, 'images': encoded,
                'rawBlocks': raw, 'removedBlocks': removed,
                'metadata': rendered.metadata, 'device': self.device_info,
                'conversionSeconds': round(time.monotonic() - started, 3),
                'originalPixelsModified': False,
            }
        except PipelineError:
            raise
        except (MemoryError, self._torch.OutOfMemoryError):
            if self.device == 'cpu':
                raise CpuResourceLimit('CPU inference exceeded memory') from None
            raise GpuFatalError('CUDA out of memory. Reduce pagesPerChunk, or use 1xH100; completed chunks are reusable') from None
        except Exception as exc:
            raise PipelineError(f'Marker conversion failed ({type(exc).__name__}); inspect package compatibility and the source document') from None
        finally:
            # Never keep rendered page images alive across chunks.
            document = None
            rendered = None
            gc.collect()
            if self.device == 'cuda':
                self._torch.cuda.empty_cache()
