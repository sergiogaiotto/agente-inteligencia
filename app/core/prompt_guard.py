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

# ── pt-BR + es (SEC-06) ─────────────────────────────────────────
# O idioma PRIMÁRIO da plataforma é pt-BR: payloads de injeção em português
# passavam ilesos pelas regex só-inglês. Pesos idênticos aos equivalentes em
# inglês (mesma postura: 1 sinal = warn; 2+ combinados = block). es mínimo (LATAM).
_JAILBREAK_PT_RE = [
    (re.compile(r"\b(?:ignore|ignora|ignorem|desconsidere|desconsidera|desconsiderem)\s+(?:todas?\s+)?(?:as\s+|os\s+)?(?:instru[çc][õo]es|regras|ordens|diretrizes|prompts?)\s+(?:anteriores|acima|pr[ée]vias|passadas|do sistema)\b", re.I), 0.55),
    (re.compile(r"\besque[çc]a(?:-se)?\s+(?:de\s+)?(?:tudo|todas?\s+as\s+(?:instru[çc][õo]es|regras)|as\s+(?:instru[çc][õo]es|regras)\s+(?:anteriores|acima))\b", re.I), 0.45),
    (re.compile(r"\bmodo\s+(?:desenvolvedor|dev|deus|root|administrador|irrestrito)\s+(?:ativad[oa]|ligad[oa]|habilitad[oa]|on)\b", re.I), 0.55),
    (re.compile(r"\b(?:ative|ativar|habilite|habilitar)\s+o\s+modo\s+(?:desenvolvedor|deus|root|administrador|irrestrito)\b", re.I), 0.55),
    (re.compile(r"\b(?:aja|atue|comporte-se|finja)\s+como\s+(?:se\s+)?(?:voc[êe]\s+)?(?:n[ãa]o\s+)?(?:tivesse|fosse|estivesse|tem)\b.{0,40}?\b(?:restri[çc][õo]es|limites?|regras|livre|irrestrit[oa])\b", re.I), 0.45),
    (re.compile(r"\bfa[çc]a\s+qualquer\s+coisa\s+agora\b", re.I), 0.55),
]
_EXFIL_PT_RE = [
    (re.compile(r"\b(?:mostre|mostra|revele|revela|repita|repete|exiba|exibe|imprima|imprime|diga|conte|compartilhe|cole)\s+(?:(?:me|-me)\s+)?(?:(?:o|a|as|os|seu|sua|suas|seus)\s+){0,3}(?:system\s+)?(?:prompt|instru[çc][õo]es|regras?|diretrizes?)\b", re.I), 0.55),
    (re.compile(r"\bqual\s+(?:é|s[ãa]o|era[m]?)\s+(?:o\s+|a\s+|as\s+|os\s+)?(?:seu|sua|suas|seus)\s+(?:prompt|instru[çc][õo]es|regras?|diretrizes?)\b", re.I), 0.55),
    (re.compile(r"\brepita\s+(?:tudo|de novo|novamente)?\s*(?:acima|antes|o que (?:veio|foi dito) antes|desde o come[çc]o)\b", re.I), 0.45),
]
_JAILBREAK_ES_RE = [
    (re.compile(r"\b(?:ignora|ignore|olvida|descarta|desestima)\s+(?:todas?\s+)?(?:las\s+|los\s+)?(?:instrucciones|reglas|[óo]rdenes)\s+(?:anteriores|previas|del sistema)\b", re.I), 0.55),
    (re.compile(r"\bmodo\s+(?:desarrollador|dios|root|administrador)\s+(?:activad[oa]|habilitad[oa])\b", re.I), 0.55),
]
_EXFIL_ES_RE = [
    (re.compile(r"\b(?:muestra|mu[ée]strame|revela|repite|imprime|dime|ense[ñn]a)\s+(?:me\s+)?(?:tu|el|la|las|los)\s+(?:prompt|instrucciones|reglas?)\b", re.I), 0.55),
]

# Listas combinadas (inglês + pt-BR + es) usadas pelo detector.
_JAILBREAK_ALL_RE = _JAILBREAK_RE + _JAILBREAK_PT_RE + _JAILBREAK_ES_RE
_EXFIL_ALL_RE = _EXFIL_RE + _EXFIL_PT_RE + _EXFIL_ES_RE

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

    for rx, weight in _JAILBREAK_ALL_RE:
        if rx.search(text):
            score += weight
            matched.append(f"jailbreak:{rx.pattern[:40]}")

    for rx, weight in _ROLE_MARKERS_RE:
        if rx.search(text):
            score += weight
            matched.append(f"role_marker:{rx.pattern[:40]}")

    for rx, weight in _EXFIL_ALL_RE:
        if rx.search(text):
            score += weight
            matched.append(f"exfil:{rx.pattern[:40]}")

    # Payloads codificados — tentativa de decodificar e re-escanear
    for m in _B64_RE.finditer(text):
        candidate = m.group(0)
        decoded = _decode_base64_safely(candidate)
        if decoded:
            # Recursão simples: aplica as regex de jailbreak ao decoded
            for rx, weight in _JAILBREAK_ALL_RE + _EXFIL_ALL_RE:
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
