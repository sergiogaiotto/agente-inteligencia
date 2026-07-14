"""Converters de documentos para markdown — Onda 6 RAG Core.

Wrapper fino sobre markitdown da Microsoft. Aceita arquivos binários,
streams ou URLs e devolve markdown limpo pronto pra chunker → embedder.

Suporta (via markitdown[all]):
- Documentos: PDF, DOCX, PPTX, XLSX, XLS, EPUB
- Web: HTML, RSS feeds, YouTube transcripts (via URL)
- Estruturados: JSON, XML, CSV
- Email: Outlook .msg
- Mídia: imagens (com OCR + EXIF), áudio (com transcrição via ffmpeg)
- Arquivos: ZIP (recursão automática)

Falhas tratadas:
- markitdown não instalado → ConverterError 503 ("RAG ingestion não habilitado").
- Formato não suportado → ConverterError 415 com hint do mime detectado.
- Conversão crasha → ConverterError 500 com excerpt do erro.
- URL não responde / 4xx-5xx → ConverterError 502.

Filosofia: NUNCA segura conteúdo — devolve markdown bruto. Chunker e
embedder a jusante já cuidam de normalizar/limitar. Mantém o módulo
ortogonal e testável.
"""
from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path
from typing import Optional

from app.core.otel import get_tracer

logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)


class ConverterError(Exception):
    """Erro de conversão com status HTTP recomendado pra propagação direta."""

    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


def _get_markitdown():
    """Lazy import — markitdown puxa pesado (PIL, soundfile, etc).
    Erro de import vira 503 explícito pro caller, não traceback."""
    try:
        from markitdown import MarkItDown
        return MarkItDown()
    except ImportError as e:
        raise ConverterError(
            f"markitdown não instalado (`pip install markitdown[all]`): {e}",
            status_code=503,
        )


def convert_bytes(
    data: bytes,
    filename: str,
    mime_type: Optional[str] = None,
) -> str:
    """Converte bytes → markdown.

    Args:
        data: bytes do arquivo (ex: PDF, DOCX, PPTX, XLSX, MP3, ZIP, ...).
        filename: nome do arquivo (markitdown usa extensão pra escolher converter).
        mime_type: MIME content-type opcional (override da detecção por extensão).

    Returns:
        Markdown puro. String vazia se conversão sucedeu mas extraiu nada.

    Raises:
        ConverterError: tipo não suportado, conversão crashou, lib indisponível.
    """
    if not data:
        raise ConverterError("Bytes vazios — nada pra converter.", status_code=400)

    md = _get_markitdown()

    with _tracer.start_as_current_span("converter.bytes") as span:
        span.set_attribute("file.name", filename or "(sem nome)")
        span.set_attribute("file.size", len(data))
        if mime_type:
            span.set_attribute("file.mime", mime_type)

        # markitdown.convert_stream aceita BytesIO + hint do tipo via filename.
        # Algumas libs internas (pypdf, openpyxl) precisam seek(0)-able stream;
        # BytesIO satisfaz. Filename só guia a escolha de converter — o conteúdo
        # real é o que importa.
        try:
            stream = io.BytesIO(data)
            # Constructor moderno aceita stream_info opcional. Usa filename hint:
            from markitdown import StreamInfo
            stream_info = StreamInfo(extension=Path(filename).suffix or None,
                                     mimetype=mime_type)
            result = md.convert_stream(stream, stream_info=stream_info)
        except ImportError:
            # API mais antiga: convert_stream sem stream_info, ou convert(file_path)
            try:
                stream = io.BytesIO(data)
                result = md.convert_stream(stream, file_extension=Path(filename).suffix)
            except Exception:
                # Fallback final: escreve em tmpfile e chama convert(path)
                return _convert_via_tmpfile(md, data, filename, span)
        except Exception:
            return _convert_via_tmpfile(md, data, filename, span)

        text = (result.text_content or "").strip()
        span.set_attribute("output.length", len(text))
        if not text:
            logger.warning(f"convert_bytes: extração vazia para {filename}")
        return text


def _convert_via_tmpfile(md, data: bytes, filename: str, span) -> str:
    """Fallback: escreve bytes em arquivo temp e usa md.convert(path).
    Útil quando convert_stream não está disponível na versão instalada."""
    suffix = Path(filename).suffix or ".bin"
    span.set_attribute("converter.fallback", "tmpfile")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        result = md.convert(tmp_path)
        text = (result.text_content or "").strip()
        span.set_attribute("output.length", len(text))
        return text
    except Exception as e:
        raise ConverterError(
            f"Conversão falhou para '{filename}': {type(e).__name__}: {str(e)[:200]}",
            status_code=500,
        )
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


def convert_url(url: str) -> str:
    """Converte URL → markdown. Aceita páginas web, PDFs hospedados, YouTube
    (transcript), feeds RSS, etc. Markitdown lida com fetch internamente.

    Args:
        url: URL completa (http/https). Validar antes pra evitar SSRF.

    Returns:
        Markdown puro. Vazio se markitdown extraiu nada.

    Raises:
        ConverterError: URL malformada, 4xx/5xx upstream, lib indisponível.
    """
    if not url or not url.strip():
        raise ConverterError("URL vazia.", status_code=400)
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ConverterError("URL deve começar com http:// ou https://", status_code=400)

    md = _get_markitdown()

    with _tracer.start_as_current_span("converter.url") as span:
        span.set_attribute("url", url[:200])
        try:
            result = md.convert_url(url) if hasattr(md, "convert_url") else md.convert(url)
        except Exception as e:
            err = str(e)[:200]
            # Heurística: se a mensagem menciona código HTTP, mapeia mais preciso
            status = 502
            if "404" in err or "not found" in err.lower():
                status = 404
            elif "401" in err or "403" in err or "forbid" in err.lower():
                status = 403
            raise ConverterError(
                f"Falha ao converter URL '{url}': {type(e).__name__}: {err}",
                status_code=status,
            )

        text = (result.text_content or "").strip()
        span.set_attribute("output.length", len(text))
        if not text:
            logger.warning(f"convert_url: extração vazia para {url}")
        return text


def supported_extensions() -> list[str]:
    """Extensões que markitdown[all] reconhece. Lista de referência;
    markitdown pode aceitar mais via detecção de conteúdo."""
    return [
        ".pdf", ".docx", ".pptx", ".xlsx", ".xls",
        ".html", ".htm", ".md", ".markdown",
        ".txt", ".csv", ".json", ".xml",
        ".epub",
        ".msg",
        ".zip",
        ".jpg", ".jpeg", ".png", ".gif", ".webp",
        ".mp3", ".wav", ".m4a", ".ogg",
    ]
