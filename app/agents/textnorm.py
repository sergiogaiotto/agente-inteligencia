"""Normalização de texto compartilhada — módulo FOLHA (stdlib-only, zero
imports de app/*, zero risco de ciclo).

Nasceu na revisão do 38.0.0: `_strip_accents` tinha TRÊS cópias gêmeas
(engine, conditional_suggest, args_suggest), cada docstring avisando "mudou
lá? mude aqui" — na terceira cópia o aviso virou dívida. O mesmo vale para o
strip de cercas de código de resposta de LLM (4 implementações independentes
no codebase). Quem normaliza texto para as vars `*_norm` do runtime E para os
repairs dos tradutores importa DAQUI — uma régua só.
"""
from __future__ import annotations

import unicodedata


def strip_accents(s: str) -> str:
    """NFKD + drop de combining chars: 'Não Reconheço' → 'Nao Reconheco'.
    É a régua das vars *_norm do runtime e dos repairs dos tradutores —
    mudar aqui muda TODOS de uma vez (o ponto da extração)."""
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(ch)
    )


def norm(s) -> str:
    """casefold + sem acento + trim — comparação canônica de literais."""
    return strip_accents(str(s).casefold().strip())


def strip_code_fences(text: str) -> str:
    """Remove cercas ``` de envoltório da resposta de um LLM, tolerante à
    cerca de LINHA ÚNICA ('```json {...} ```') — a variante multi-linha com
    splitlines()[1:] descartava o payload inteiro nesse caso (bug latente
    idêntico nos dois tradutores, achado da revisão do 38.0.0). Nunca devolve
    vazio quando havia conteúdo: sem cerca reconhecível, devolve o texto."""
    s = (text or "").strip()
    if not s.startswith("```"):
        return s
    body = s[3:]
    if body.endswith("```"):
        body = body[:-3]
    # rótulo de linguagem colado na cerca ('```json\n' ou '```json {...}')
    lines = body.splitlines()
    if lines and lines[0].strip().isalpha() and len(lines) > 1:
        lines = lines[1:]
    elif lines:
        first = lines[0].lstrip()
        for label in ("json", "jinja", "python", "javascript"):
            if first.startswith(label + " ") or first == label:
                lines[0] = first[len(label):].lstrip()
                break
    out = "\n".join(lines).strip().strip("`").strip()
    return out or s
