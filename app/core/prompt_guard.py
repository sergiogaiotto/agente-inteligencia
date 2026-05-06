"""Detector heurístico de prompt injection — defesa OWASP LLM01.

Estratégia layered:
1. Regex de padrões de jailbreak conhecidos (DAN, "ignore previous", etc).
2. Markers de role-injection (`<|system|>`, `[INST]`, `### System:`).
3. Tentativas de exfiltração do system prompt.
4. Conteúdo codificado suspeito (base64/hex grandes blocos).
5. Idiomas mistos / encoding incomum.

Score em [0, 1] composto. Acima de `block_threshold` → bloqueia.
Entre `warn_threshold` e `block_threshold` → loga + flag em metadata.

Não é defesa absoluta — adversários mais sofisticados passam. É a primeira
linha; reforço vem de:
- guardrails do SKILL.md (semântica)
- Evidence Checker (saída ancorada em evidência)
- prompt-hardening do system message (engine.py)
- na Onda 4: PEP/PDP via OPA (autorização contextual)
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ── Padrões de jailbreak conhecidos ─────────────────────────────
_JAILBREAK_RE = [
    (re.compile(r"\bignore (?:all )?(?:previous|above|prior) (?:instructions|prompts?|rules?)\b", re.I), 0.55),
    (re.compile(r"\bdisregard (?:the )?(?:previous|above|prior|system) (?:instructions?|prompts?|rules?)\b", re.I), 0.55),
    (re.compile(r"\bforget (?:everything|all|previous|prior)\b", re.I), 0.45),
    (re.compile(r"\byou (?:are|will be) (?:now |a )?DAN\b", re.I), 0.6),
    (re.compile(r"\bdo anything now\b", re.I), 0.55),
    (re.compile(r"\bdeveloper mode (?:enabled|on|activated)\b", re.I), 0.55),
    (re.compile(r"\bjailbreak(?:ing|ed)?\b", re.I), 0.4),
    (re.compile(r"\bhypothetic(?:al(?:ly)?)? scenario\b.*\b(?:ignore|bypass|override)\b", re.I), 0.4),
    (re.compile(r"\bact(?:ing)? as (?:if|though) you (?:are|were) (?:not |un)?(?:bound|restricted|limited)\b", re.I), 0.45),
    (re.compile(r"\b(?:enable|activate) (?:god|root|admin) mode\b", re.I), 0.55),
]

# ── Markers de role-injection ───────────────────────────────────
# Tokens especiais ChatML/Llama instruct não têm uso legítimo num
# input de usuário — bloqueio direto (peso >= block_threshold).
_ROLE_MARKERS_RE = [
    (re.compile(r"<\|(?:system|im_start|im_end|endoftext)\|>", re.I), 0.8),
    (re.compile(r"\[INST\]|\[/INST\]"), 0.6),
    (re.compile(r"^\s*(?:###\s*)?(?:System|Assistant|Human)\s*:\s*", re.I | re.M), 0.4),
    (re.compile(r"</?(?:system|user|assistant|tool)>", re.I), 0.5),
]

# ── Exfiltração do system prompt ────────────────────────────────
_EXFIL_RE = [
    (re.compile(r"\b(?:show|print|reveal|repeat|display|expose|tell|give|share)\s+(?:me\s+)?(?:your|the)\s+(?:system|initial|original|hidden)?\s*(?:prompt|instructions|rules?|guidelines?)\b", re.I), 0.55),
    (re.compile(r"\bwhat (?:is|are) your (?:system|initial|original)?\s*(?:prompt|instructions?|rules?)\b", re.I), 0.55),
    (re.compile(r"\brepeat (?:back|the )?(?:everything|all) (?:above|before)\b", re.I), 0.45),
    (re.compile(r"\b(?:list|enumerate)\s+(?:all\s+)?(?:your\s+)?(?:tools|functions|capabilities)\s+(?:in\s+)?(?:detail|verbatim)\b", re.I), 0.35),
]

# ── Heurísticas de payload codificado ───────────────────────────
# Bloco base64 suspeito: >= 80 chars de [A-Za-z0-9+/=] sem espaço
_B64_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{80,}(?![A-Za-z0-9+/=])")
# Hex muito longo (>= 64 chars) — pode ser payload
_HEX_RE = re.compile(r"(?<![A-Fa-f0-9])[A-Fa-f0-9]{64,}(?![A-Fa-f0-9])")


@dataclass
class GuardResult:
    score: float = 0.0
    matched_patterns: list[str] = field(default_factory=list)
    blocked: bool = False
    warn: bool = False

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "matched": self.matched_patterns[:8],  # limita p/ não inflar log
            "blocked": self.blocked,
            "warn": self.warn,
        }


def _decode_base64_safely(s: str) -> Optional[str]:
    try:
        # Padding tolerante
        pad = len(s) % 4
        if pad:
            s = s + "=" * (4 - pad)
        return base64.b64decode(s, validate=False).decode("utf-8", errors="ignore")
    except Exception:
        return None


def detect(
    text: str,
    block_threshold: float = 0.7,
    warn_threshold: float = 0.4,
) -> GuardResult:
    """Detecta sinais de prompt injection em `text`."""
    if not text:
        return GuardResult()

    score = 0.0
    matched: list[str] = []

    for rx, weight in _JAILBREAK_RE:
        if rx.search(text):
            score += weight
            matched.append(f"jailbreak:{rx.pattern[:40]}")

    for rx, weight in _ROLE_MARKERS_RE:
        if rx.search(text):
            score += weight
            matched.append(f"role_marker:{rx.pattern[:40]}")

    for rx, weight in _EXFIL_RE:
        if rx.search(text):
            score += weight
            matched.append(f"exfil:{rx.pattern[:40]}")

    # Payloads codificados — tentativa de decodificar e re-escanear
    for m in _B64_RE.finditer(text):
        candidate = m.group(0)
        decoded = _decode_base64_safely(candidate)
        if decoded:
            # Recursão simples: aplica as regex de jailbreak ao decoded
            for rx, weight in _JAILBREAK_RE + _EXFIL_RE:
                if rx.search(decoded):
                    score += weight + 0.1   # pequeno bônus por ofuscação
                    matched.append(f"b64_payload:{rx.pattern[:30]}")
                    break

    # Hex grande sem contexto técnico → suspeita leve
    if _HEX_RE.search(text):
        score += 0.15
        matched.append("hex_long_block")

    # Cap em 1.0
    score = min(score, 1.0)
    blocked = score >= block_threshold
    warn = (not blocked) and score >= warn_threshold

    if blocked:
        logger.warning(f"prompt_guard: BLOCKED score={score:.2f} matched={matched[:5]}")
    elif warn:
        logger.info(f"prompt_guard: WARN score={score:.2f} matched={matched[:3]}")

    return GuardResult(score=score, matched_patterns=matched, blocked=blocked, warn=warn)


# ═══════════════════════════════════════════════════════════════
# LLM10 — Prompt Leak Guard
# ═══════════════════════════════════════════════════════════════


def sanitize_for_trace(prompt: str, preview_chars: int = 60) -> dict:
    """Substitui o system_prompt cru por hash + preview curto.

    Defesa contra OWASP LLM10: traces de retorno frequentemente são logados
    (LangFuse, Tempo, observabilidade local) e podem ser visualizados por
    operadores/analistas que não deveriam ler o prompt na íntegra. Hash
    estável permite cross-reference; preview ajuda no diagnóstico sem
    expor o conteúdo completo.
    """
    import hashlib

    if not prompt:
        return {"hash": "", "preview": "", "length": 0}

    raw = prompt.strip()
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    first_line = raw.split("\n", 1)[0]
    preview = first_line[:preview_chars]
    if len(first_line) > preview_chars or "\n" in raw:
        preview = preview + "…"
    return {
        "hash": h,
        "preview": preview,
        "length": len(raw),
    }
