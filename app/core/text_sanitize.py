"""Higiene de texto para RESPOSTAS DE API — nunca deixar emoji sair.

Regra do produto: **nenhuma resposta de API pode conter emoji ou símbolos
decorativos "similares"** (⚠ 🔺 ▶ ✓ setas…). Emoji vazam por dois caminhos: o
LLM os gera no ``output``, e a própria plataforma injeta prefixos decorativos
(Recusa/Escalate, diagnósticos). Este módulo é a GARANTIA determinística: um
sanitizador aplicado no limite da API, mesmo que o LLM desobedeça.

Escopo (alinhado com o dono): aplica-se ao TEXTO que o consumidor recebe —
``output`` do invoke/explain, prefixos da FSM e mensagens de diagnóstico do
envelope. NÃO varre cegamente todo campo JSON (não adultera um dado do próprio
usuário ecoado de volta).

Segurança: remove SÓ pictogramas/decoração — não toca em letras acentuadas,
pontuação comum, marcadores de lista (•), indentação de código nem no conteúdo
textual. Idempotente.
"""
from __future__ import annotations

import re

# Blocos Unicode de emoji + pictogramas + os símbolos decorativos "similares"
# que a plataforma usa (⚠=U+26A0, 🔺=U+1F53A, ▶=U+25B6, ✓=U+2713, setas…).
# NÃO inclui: pontuação geral (• U+2022), letras/acentos, ™/©/® (podem ser
# legítimos em nomes de produto).
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"   # bandeiras (regional indicators)
    "\U0001F300-\U0001FAFF"   # emoticons, pictogramas, transporte, símbolos suplementares
    "\U00002600-\U000027BF"   # Misc Symbols (⚠ ☀ ☁) + Dingbats (✓ ✔ ✅ ✂)
    "\U00002B00-\U00002BFF"   # setas/estrelas suplementares (⭐ ⬆ ➤)
    "\U00002190-\U000021FF"   # setas (→ ← ↑ ↓ ↔ …)
    "\U000025A0-\U000025FF"   # formas geométricas decorativas (▶ ◀ ● ■ ◆ …)
    "\U00002139"               # ℹ (info, renderiza como emoji)
    "\U0000FE00-\U0000FE0F"   # variation selectors (força apresentação emoji)
    "\U0000200D"               # zero-width joiner (sequências de emoji)
    "\U000020E3"               # keycap combining (0️⃣ …)
    "]+",
    flags=re.UNICODE,
)


def strip_emoji(text: str | None) -> str | None:
    """Remove emoji/símbolos decorativos de ``text`` e limpa os espaços órfãos.

    Determinística e idempotente. Preserva indentação (não colapsa espaços no
    início da linha) e marcadores de lista. Devolve o mesmo tipo de entrada
    vazia/None que recebeu (``None``/``""`` passam direto)."""
    if not text or not isinstance(text, str):
        return text
    if not _EMOJI_RE.search(text):
        return text
    cleaned = _EMOJI_RE.sub("", text)
    # colapsa espaços SÓ entre palavras (preserva indentação de código no início
    # da linha), remove espaço pendurado no fim de cada linha, e apara as bordas.
    cleaned = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", cleaned)
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))
    return cleaned.strip()


def scrub_diagnostics(diagnostics):
    """Aplica ``strip_emoji`` no campo ``text`` de cada diagnóstico (lista de
    dicts ``{level, text}``). Best-effort: entrada malformada passa intacta."""
    if not isinstance(diagnostics, list):
        return diagnostics
    out = []
    for d in diagnostics:
        if isinstance(d, dict) and isinstance(d.get("text"), str):
            out.append({**d, "text": strip_emoji(d["text"])})
        else:
            out.append(d)
    return out
