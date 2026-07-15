"""Decodificação de anexos base64 do corpo do /invoke — agente E pipeline.

Extraído de app/routes/agents.py (37.0.0): o invoke de PIPELINE ganhou o ramo
base64 e reusa este decoder em vez de duplicá-lo — era exatamente o bug do
drop silencioso (cliente mandava content_base64, o mapeamento do pipeline só
carregava os campos do upload-ref e a imagem "aceita" nunca chegava ao modelo;
o agent invoke teve o MESMO bug, ver comentário no corpo).

Tolerante a item dict (pipeline: attachments é lista crua) OU objeto pydantic
(agente: AttachmentInput) — `_field` resolve os dois.
"""
from __future__ import annotations

import base64
import logging
import mimetypes

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024   # 10MB raw (~13MB em base64)
MAX_ATTACHMENTS_PER_INVOKE = 5
ATTACHMENT_TEXT_TRUNCATE = 50_000          # alinhado com workspace/upload


def _field(att, key: str, default: str = ""):
    if isinstance(att, dict):
        return att.get(key) or default
    return getattr(att, key, default) or default


def decode_attachments(items: list) -> tuple[list, list]:
    """Decodifica anexos base64 → formato interno usado pelo engine
    e por _filter_attachments_by_agent ({name, type, size, content}).
    Aplica limites de quantidade e tamanho. Retorna (aceitos, rejeitados_meta)."""
    accepted: list = []
    rejected: list = []
    if not items:
        return accepted, rejected

    overflow = items[MAX_ATTACHMENTS_PER_INVOKE:]
    items = items[:MAX_ATTACHMENTS_PER_INVOKE]
    for att in overflow:
        rejected.append({
            "name": _field(att, "filename"),
            "type": _field(att, "content_type"),
            "kind": "overflow",
            "reason": f"Excedeu limite de {MAX_ATTACHMENTS_PER_INVOKE} anexos por invocação",
        })

    for att in items:
        # str(): itens de pipeline chegam como dict CRU — filename/content_type
        # não-string (int, etc.) estourariam guess_type/startswith com 500 em
        # vez de seguir o contrato (review). Coerção leniente: "42" é um nome
        # feio mas inofensivo; o conteúdo continua validado de verdade.
        filename = str(_field(att, "filename"))
        ctype = str(
            _field(att, "content_type")
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )
        try:
            raw = base64.b64decode(_field(att, "content_base64"), validate=True)
        except Exception as e:
            rejected.append({"name": filename, "type": ctype, "kind": "invalid_base64",
                             "reason": f"base64 inválido: {e}"})
            continue
        if len(raw) > MAX_ATTACHMENT_BYTES:
            rejected.append({"name": filename, "type": ctype, "kind": "oversize",
                             "reason": f"Excedeu 10MB ({len(raw)} bytes)"})
            continue
        # Ordem: UTF-8 direto → markitdown (PDF/PPTX/DOCX/XLSX/imagens/audio) →
        # content vazio. Antes (até 2026-06-01) o except UnicodeDecodeError caía
        # silenciosamente em content="" e o engine descartava o attachment —
        # sem qualquer rastro pra troubleshooting de "agente não viu o PDF".
        text_content = ""
        try:
            text_content = raw.decode("utf-8")[:ATTACHMENT_TEXT_TRUNCATE]
        except UnicodeDecodeError:
            try:
                from app.evidence.converters import convert_bytes
                text_content = convert_bytes(raw, filename, ctype)[
                    :ATTACHMENT_TEXT_TRUNCATE
                ]
            except Exception as conv_e:  # pragma: no cover - conversor falhou
                logger.warning(
                    "invoke.attachment_text_extract_failed",
                    extra={
                        "event": "invoke.attachments",
                        # 'filename' é built-in do LogRecord — renomeado.
                        "attachment_name": filename,
                        "content_type": ctype,
                        "size": len(raw),
                        "error_type": type(conv_e).__name__,
                        "error_msg": str(conv_e)[:200],
                    },
                )
                # mantém content="" — engine descarta (comportamento legado)
        item = {
            "name": filename,
            "type": ctype,
            "size": len(raw),
            "content": text_content,
        }
        # Imagem vai ao LLM multimodal como image_url (base64), NÃO como o texto
        # markitdown (só "ImageSize: LxA", ruído). `_attachment_image_data_url`
        # (engine) lê `content_base64`/`image_b64`/`abs_path`. Este decoder da API
        # só carregava `content` (texto), então a imagem era DESCARTADA no invoke
        # via /agents/{id}/invoke — mesmo com o modelo roteado corretamente pro
        # multimodal_fallback (azure/gpt-4o): sem base64,
        # `_build_user_message_content` cai no caminho text-only e o SA de visão
        # respondia "nenhuma imagem enviada". O caminho workspace/UI não sofria
        # porque grava o arquivo e passa `abs_path`. Só p/ imagem (documentos
        # usam `content` textual; anexar base64 dobraria a memória à toa).
        if ctype.startswith("image/"):
            item["content_base64"] = base64.b64encode(raw).decode()
        accepted.append(item)
    return accepted, rejected
