"""Output Shape — presets de tamanho/forma da resposta (Onda 1 do roadmap).

Tabela única usada por:
- parser.py: valida o `length_preset` declarado em `## Output Shape` do SKILL.md
- engine.py: injeta instrução de tamanho no system_prompt + truncate hard
- routes/wizard.py: schema da WizardSkillRequest + injeção no prompt
- skill_form.html: dropdown na UI
- verifier: dimensão `format_compliance`

Decisão de design: preset é uma TUPLA `(max_chars, label, description)` —
não um JSON Schema gigante. Operador escolhe pelo rótulo humano, engine
aplica o limite, verifier audita. Quando precisa de schema rígido (tipos
estritos), use `## Output Contract` (já existe).

Faixa de presets calibrada com pesquisa de UX: usuários consomem
resposta diferente em chat conversacional (curtos) vs relatório (longos).
"""
from __future__ import annotations

from typing import Optional, TypedDict


class LengthPreset(TypedDict):
    max_chars: Optional[int]   # None = sem limite (preset "unbounded")
    label: str                 # Rótulo humano pra UI
    description: str           # Sub-rótulo descritivo


# Ordem mantida (intent → unbounded) — UI usa esta ordem no dropdown.
# Mudar é breaking pra skills existentes que referenciam por chave.
LENGTH_PRESETS: dict[str, LengthPreset] = {
    "intent": {
        "max_chars": 600,
        "label": "Intenção",
        "description": "frase curta",
    },
    "summary": {
        "max_chars": 600,
        "label": "Resumo",
        "description": "síntese curta",
    },
    "digest": {
        "max_chars": 1500,
        "label": "Sumário",
        "description": "default",
    },
    "analysis": {
        "max_chars": 4000,
        "label": "Análise",
        "description": "análise estruturada",
    },
    "report": {
        "max_chars": 8000,
        "label": "Relatório",
        "description": "texto longo",
    },
    "unbounded": {
        "max_chars": None,
        "label": "Livre",
        "description": "sem corte, todo o texto preservado",
    },
}

# Preset usado quando skill não declara nada. Calibrado pra chat
# conversacional típico — operador pode override por skill.
DEFAULT_LENGTH_PRESET = "digest"


def is_valid_preset(key: str) -> bool:
    """True se `key` é um preset conhecido. Usado por parser pra validação
    defensiva — preset inválido vira default em vez de quebrar a skill."""
    return key in LENGTH_PRESETS


def get_max_chars(preset_key: str) -> Optional[int]:
    """Retorna o limite de chars do preset, ou None pra unbounded.

    Defensivo: preset desconhecido cai no DEFAULT_LENGTH_PRESET. Operador
    pode digitar `length_preset: typo` no YAML — não derrubamos a skill.
    """
    cfg = LENGTH_PRESETS.get(preset_key) or LENGTH_PRESETS[DEFAULT_LENGTH_PRESET]
    return cfg["max_chars"]


def build_directive(preset_key: str) -> str:
    """Constrói diretiva imperativa pra inserir no system_prompt do LLM.

    Modelos open-weight respondem melhor a instrução clara + número exato.
    Citamos categoria humana ("Sumário") + limite numérico ("≤ 1500 chars").
    """
    cfg = LENGTH_PRESETS.get(preset_key) or LENGTH_PRESETS[DEFAULT_LENGTH_PRESET]
    max_chars = cfg["max_chars"]
    label = cfg["label"]
    desc = cfg["description"]
    if max_chars is None:
        return (
            f"[TAMANHO DA RESPOSTA] Categoria: {label} ({desc}). "
            "Não há limite de caracteres — preserve todo o conteúdo necessário."
        )
    return (
        f"[TAMANHO DA RESPOSTA] Categoria: {label} ({desc}). "
        f"Limite estrito: NO MÁXIMO {max_chars} caracteres. "
        "Conte URLs e código como caracteres normais. Se o conteúdo natural "
        "exceder o limite, priorize completude semântica e seja conciso — "
        f"NÃO ultrapasse {max_chars}."
    )


def enforce_truncate(text: str, preset_key: str) -> tuple[str, bool]:
    """Trunca `text` se exceder o limite do preset. Hard cap pós-LLM.

    Returns:
        (texto_final, foi_truncado)
        - foi_truncado=True sinaliza que o LLM violou o preset declarado —
          Verifier pode marcar `format_compliance` como falha.

    Truncate é em chars (não tokens), sufixo "…" pra sinalizar visualmente.
    Preset `unbounded` ou desconhecido: passa intacto.
    """
    if not text:
        return text, False
    max_chars = get_max_chars(preset_key)
    if max_chars is None or len(text) <= max_chars:
        return text, False
    # Truncate hard com ellipsis. -1 pra caber o caractere "…".
    return text[: max_chars - 1] + "…", True
