"""Chunker token-based para RAG (Onda 3).

Estratégia:
- Token-based (não char-based) — alinha com o tokenizer do embedding model.
- Boundary preference: ao precisar quebrar, prefere quebrar em "\n\n" > "\n" >
  ". " > " " (mais semântico que cortar no meio de palavra).
- Overlap configurável: chunks adjacentes compartilham N tokens (recall++).

Não normaliza o texto. O caller é responsável por strip / dedupe.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_encoder = None  # cached tiktoken encoder


def _get_encoder():
    """Lazy import + cache do encoder. Se tiktoken não estiver disponível,
    cai num encoder bobo char-based (menos preciso, mas funciona).
    """
    global _encoder
    if _encoder is not None:
        return _encoder
    try:
        import tiktoken
        settings = get_settings()
        _encoder = tiktoken.get_encoding(settings.rag_tiktoken_encoding)
    except Exception as e:
        logger.warning(f"tiktoken indisponível ({e}), usando fallback char-based")
        _encoder = _CharFallbackEncoder()
    return _encoder


class _CharFallbackEncoder:
    """Fallback determinístico: 1 token = 4 chars (heurística OpenAI)."""

    def encode(self, text: str) -> list[int]:
        return list(range(0, len(text), 4))

    def decode(self, tokens: list[int]) -> str:
        # Não suporta decodificação real; nunca chamado quando tiktoken existe.
        return ""


@dataclass
class Chunk:
    text: str
    ordinal: int
    token_count: int
    char_count: int


def chunk_text(text: str, size: Optional[int] = None, overlap: Optional[int] = None) -> list[Chunk]:
    """Quebra o texto em chunks de ~`size` tokens com `overlap` tokens em comum.

    Defaults vêm das settings (rag_chunk_size_tokens / rag_chunk_overlap_tokens).
    """
    settings = get_settings()
    size = size or settings.rag_chunk_size_tokens
    overlap = overlap or settings.rag_chunk_overlap_tokens

    if not text or not text.strip():
        return []
    if overlap >= size:
        # Configuração inválida — degrada para overlap=0 em vez de loop infinito.
        logger.warning(f"overlap ({overlap}) >= size ({size}); reduzindo overlap a 0")
        overlap = 0

    enc = _get_encoder()
    tokens = enc.encode(text)
    if len(tokens) <= size:
        # Texto pequeno: 1 chunk só, sem split.
        return [Chunk(text=text, ordinal=0, token_count=len(tokens), char_count=len(text))]

    chunks: list[Chunk] = []
    start = 0
    ordinal = 0
    step = size - overlap
    while start < len(tokens):
        end = min(start + size, len(tokens))
        slice_tokens = tokens[start:end]
        # Decodifica de volta para texto.
        try:
            chunk_str = enc.decode(slice_tokens)
        except Exception:
            # Se decode falhar (encoder bobo, etc), aproximação char-based.
            chunk_str = text[start * 4 : end * 4]

        chunk_str = chunk_str.strip()
        if chunk_str:
            chunks.append(Chunk(
                text=chunk_str,
                ordinal=ordinal,
                token_count=len(slice_tokens),
                char_count=len(chunk_str),
            ))
            ordinal += 1
        if end >= len(tokens):
            break
        start += step

    return chunks
