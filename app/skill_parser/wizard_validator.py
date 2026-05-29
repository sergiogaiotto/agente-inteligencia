"""Validador pós-geração de SKILL.md — fecha o gap entre "regra no prompt
do Wizard" e "LLM gerador realmente seguiu a regra".

Motivação (2026-05-29): nossos testes de Wizard validam que o prompt
CONTÉM as regras (smoke estático de template), mas não validam que o LLM
gerador SEGUE as regras. Bugs Context7 v1 e v2 escaparam dessa lacuna:

- v1: Wizard gerou Workflow passivo ("enriquecimento com X") mesmo com
  regras G1-G2 no prompt
- v2: Wizard gerou `operation=search` (inválida) mesmo com lista de
  operations declaradas no prompt

Este validador roda DEPOIS do LLM gerar SKILL.md e ANTES de salvar.
Detecta as violações via regras Python puras (sem LLM extra), permitindo:
- Retry com instrução de correção específica (no Wizard endpoint)
- Warnings na response do Wizard (frontend mostra antes de salvar)
- CI tests determinísticos (smoke + regressão)

Severidade:
- CRITICAL: SKILL provavelmente falha em runtime (verb passivo, operation
  inventada, fonte negada quando há binding) → retry obrigatório
- WARNING: SKILL pode funcionar mas tem fragilidade (Examples sem tool
  call, frase confusa) → não bloqueia, só avisa
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ───────────────────────────────────────────────────────────────
# Tipos
# ───────────────────────────────────────────────────────────────


@dataclass
class Violation:
    rule: str          # ex: "G1.passive_verb" / "operation.invented"
    severity: str      # "critical" | "warning"
    section: str       # ex: "Workflow" / "Examples" / "Tool Bindings"
    message: str       # mensagem humana pra UI/log
    suggestion: str = ""  # como corrigir (vira instrução de retry)
    evidence: str = ""    # trecho da SKILL que disparou (debug)


@dataclass
class ValidationResult:
    ok: bool
    violations: list[Violation] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "critical")

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "warning")

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "critical_count": self.critical_count,
            "warning_count": self.warning_count,
            "violations": [
                {
                    "rule": v.rule, "severity": v.severity,
                    "section": v.section, "message": v.message,
                    "suggestion": v.suggestion,
                    "evidence": v.evidence[:200],
                }
                for v in self.violations
            ],
        }

    def critical_suggestions(self) -> list[str]:
        """Concatena suggestions das violações críticas pra usar no retry
        prompt. Limitado a 5 pra não inflar o retry."""
        out = []
        for v in self.violations:
            if v.severity == "critical" and v.suggestion:
                out.append(f"- [{v.rule}] {v.suggestion}")
                if len(out) >= 5:
                    break
        return out


# ───────────────────────────────────────────────────────────────
# Constantes (alinhadas com app/routes/wizard.py)
# ───────────────────────────────────────────────────────────────


# Verbos imperativos aceitos no Workflow quando há binding
IMPERATIVE_VERBS = (
    "chame", "consulte", "invoque", "execute", "acione", "query", "recupere",
    "busque", "select", "selecione",
)

# Verbos passivos PROIBIDOS (causa do bug Context7 v1)
PASSIVE_VERBS = (
    "enriquecimento", "enriquecer", "incorpora", "incorporar",
    "usando o binding", "com apoio de", "a partir de",
    "se valendo de", "se valendo do",
)

# Frases que sugerem ao LLM "você é autônomo" (causa do bug v1)
INTERNAL_PHRASES = (
    "template interno", "templates internos", "recursos internos",
    "conhecimento próprio", "base interna", "conhecimento prévio",
)

# Frases negativas que desabilitam o binding (causa do bug v1)
NEGATIVE_SOURCE_PHRASES = (
    "nenhuma fonte externa autorizada",
    "sem fontes externas",
    "toda informação vem de conhecimento interno",
    "nenhuma fonte de conhecimento externa",
)

# Nomes de operation que LLM gerador costuma inventar quando não vê lista
# (alinhado com a regra em wizard.py _mcp_block)
COMMONLY_INVENTED_OPS = ("search", "query", "fetch", "get", "find", "lookup")


# ───────────────────────────────────────────────────────────────
# Helpers de parsing
# ───────────────────────────────────────────────────────────────


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace pra detecção textual."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _split_operations_csv(ops_raw: str) -> list[str]:
    """Aceita CSV ou JSON list e retorna lista de operations normalizada."""
    if not ops_raw:
        return []
    s = ops_raw.strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            import json
            arr = json.loads(s)
            return [str(x).strip().lower() for x in arr if str(x).strip()]
        except (ValueError, TypeError):
            pass
    return [tok.strip().lower() for tok in s.split(",") if tok.strip()]


def _extract_operations_from_workflow(workflow: str) -> list[str]:
    """Extrai operations citadas na seção Workflow.

    Padrões reconhecidos (case-insensitive):
    - `operation=docs` (inglês — formato usado pelo Wizard)
    - `operation="docs"`, `operation: docs`, `` `operation=docs` ``
    - `operação=docs` (português, fallback caso LLM gerador traduza)

    Returns lista lowercase. Pode ter duplicatas — caller dedup se precisar.
    """
    if not workflow:
        return []
    # operation EN ou operação PT (com `çã`, `ca`, etc).
    # Aceita `=` ou `:`, opcionalmente envolto em ` " '.
    pattern = r"opera(?:tion|[çc][ãa]?o)\s*[=:]\s*[`\"']?([a-zA-Z][a-zA-Z0-9_]*)"
    matches = re.findall(pattern, workflow, flags=re.IGNORECASE)
    return [m.lower() for m in matches]


def _has_imperative_verb(workflow: str) -> bool:
    low = (workflow or "").lower()
    # Procura verbos imperativos como início de palavra ou após **bold** marker
    for v in IMPERATIVE_VERBS:
        # Bate em "Chame a tool" ou "**Chame** a tool"
        if re.search(rf"\b{re.escape(v)}\b", low):
            return True
    return False


def _find_passive_phrase(workflow: str) -> Optional[str]:
    low = (workflow or "").lower()
    for p in PASSIVE_VERBS:
        if p in low:
            return p
    return None


def _find_internal_phrase(workflow: str) -> Optional[str]:
    low = (workflow or "").lower()
    for p in INTERNAL_PHRASES:
        if p in low:
            return p
    return None


def _find_negative_source_phrase(text: str) -> Optional[str]:
    low = _normalize(text)
    for p in NEGATIVE_SOURCE_PHRASES:
        if p in low:
            return p
    return None


def _examples_have_tool_call_marker(examples: str) -> bool:
    """Heurística: Examples bem-formado tem ao menos 1 dos marcadores
    Entrada/Chamada/Resposta/Saída final OU referencia o nome da tool."""
    if not examples:
        return False
    low = examples.lower()
    markers = (
        "**chamada", "**resposta", "**execução", "**consulta",
        "**sql gerado", "**evidências", "**saída final",
        "tool_call", "chamada à tool", "chamada ao endpoint",
    )
    return any(m in low for m in markers)


# ───────────────────────────────────────────────────────────────
# Validador principal
# ───────────────────────────────────────────────────────────────


def validate_generated_skill(
    parsed_skill,
    bindings: dict,
) -> ValidationResult:
    """Valida a SKILL.md gerada contra as regras do Wizard.

    Args:
        parsed_skill: instância de app.skill_parser.parser.ParsedSkill —
            já com workflow/tool_bindings/evidence_policy/examples como
            strings. Caller deve fazer parser.parse(skill_md) antes.
        bindings: dict como passado a `_build_wizard_prompt`:
            {"mcp_tools": [...], "rag_sources": [...],
             "data_tables": [...], "api_endpoints": [...]}

        Cada tool MCP deve ter ao menos {name, operations} pra validação
        de operations funcionar. operations pode ser CSV ou JSON list.

    Returns:
        ValidationResult com .ok=True quando nenhuma violação CRITICAL,
        independente de warnings. Caller decide se quer bloquear apenas
        em critical ou também em warning.
    """
    violations: list[Violation] = []
    workflow = getattr(parsed_skill, "workflow", "") or ""
    examples = getattr(parsed_skill, "examples", "") or ""
    evidence_policy = getattr(parsed_skill, "evidence_policy", "") or ""
    tool_bindings_section = getattr(parsed_skill, "tool_bindings", "") or ""

    has_any_binding = bool(
        bindings.get("mcp_tools") or bindings.get("rag_sources")
        or bindings.get("data_tables") or bindings.get("api_endpoints")
    )

    # ──── G1.passive_verb (CRITICAL) ────
    passive = _find_passive_phrase(workflow)
    if passive and has_any_binding:
        violations.append(Violation(
            rule="G1.passive_verb",
            severity="critical",
            section="Workflow",
            message=(
                f"Workflow usa o verbo passivo \"{passive}\" para referenciar "
                "binding. Modelos open-weight ignoram silenciosamente sem "
                "verbo imperativo direto."
            ),
            suggestion=(
                f"Reescreva o passo do Workflow trocando \"{passive}\" por "
                f"um verbo IMPERATIVO: {', '.join(IMPERATIVE_VERBS[:5])}. "
                "Exemplo: \"Chame a tool X com operation=Y e query=...\""
            ),
            evidence=workflow[:300],
        ))

    # ──── G1.no_imperative (CRITICAL — somente quando há binding) ────
    if has_any_binding and workflow and not _has_imperative_verb(workflow):
        violations.append(Violation(
            rule="G1.no_imperative",
            severity="critical",
            section="Workflow",
            message=(
                "Workflow não contém nenhum verbo imperativo de invocação "
                "(Chame/Consulte/Execute/Acione/Query). Bindings declarados "
                "não serão acionados em runtime."
            ),
            suggestion=(
                "Adicione um passo numerado no Workflow começando com verbo "
                "imperativo: \"Chame a tool X com operation=Y e query=...\"."
            ),
            evidence=workflow[:300],
        ))

    # ──── G2.internal_phrase (CRITICAL) ────
    internal = _find_internal_phrase(workflow)
    if internal and has_any_binding:
        violations.append(Violation(
            rule="G2.internal_phrase",
            severity="critical",
            section="Workflow",
            message=(
                f"Workflow contém a frase \"{internal}\" que diz ao LLM "
                "\"você é autônomo\" — ele ignora os bindings declarados "
                "e responde de cabeça."
            ),
            suggestion=(
                f"Remova a frase \"{internal}\" do Workflow. Em vez disso, "
                "descreva o passo como uma consulta explícita ao binding "
                "declarado."
            ),
            evidence=workflow[:300],
        ))

    # ──── G3.examples_without_tool_call (WARNING) ────
    if has_any_binding and examples and not _examples_have_tool_call_marker(examples):
        violations.append(Violation(
            rule="G3.examples_without_tool_call",
            severity="warning",
            section="Examples",
            message=(
                "Examples não rastreia a interação com o binding (Entrada → "
                "Chamada → Resposta → Saída). Modelos em runtime aprendem "
                "do exemplo a pular o passo do binding."
            ),
            suggestion=(
                "Em cada exemplo, adicione bloco com **Chamada à tool**: "
                "nome operation=X query=... + **Resposta da tool**: <resumo> "
                "ANTES da **Saída final**."
            ),
            evidence=examples[:300],
        ))

    # ──── G4.negative_source (CRITICAL) ────
    if has_any_binding:
        for section_name, section_text in (
            ("Evidence Policy", evidence_policy),
            ("Workflow", workflow),
            ("Tool Bindings", tool_bindings_section),
        ):
            phrase = _find_negative_source_phrase(section_text)
            if phrase:
                violations.append(Violation(
                    rule="G4.negative_source",
                    severity="critical",
                    section=section_name,
                    message=(
                        f"Seção \"{section_name}\" diz \"{phrase}\" — "
                        "contradiz o binding declarado. LLM em runtime "
                        "interpreta como proibição e ignora a tool."
                    ),
                    suggestion=(
                        "Remova a frase negativa. Se a skill só usa o "
                        "binding (sem RAG), escreva: \"A única fonte "
                        "autorizada é o binding X declarado em "
                        "## Tool Bindings.\""
                    ),
                    evidence=section_text[:300],
                ))

    # ──── operation.invented (CRITICAL — Context7 v2 bug) ────
    # Cruzar operations CITADAS no Workflow com operations DECLARADAS
    # em cada tool MCP do bindings.
    if bindings.get("mcp_tools") and workflow:
        cited_ops = set(_extract_operations_from_workflow(workflow))
        declared_ops = set()
        for tool in bindings["mcp_tools"]:
            for op in _split_operations_csv(tool.get("operations") or ""):
                declared_ops.add(op)

        # Só valida quando há operations declaradas (tool nova sem ops
        # cadastradas no Registry não pode disparar esse check)
        if declared_ops:
            invented = sorted(cited_ops - declared_ops)
            if invented:
                # Lista os nomes inventados; foco em "search" porque é o mais
                # frequente. Mensagem detalha o que era válido.
                violations.append(Violation(
                    rule="operation.invented",
                    severity="critical",
                    section="Workflow",
                    message=(
                        f"Workflow chama tool MCP com operation(s) "
                        f"{invented} que NÃO estão declaradas no Registry. "
                        f"Servidor MCP REJEITARÁ. Operations válidas: "
                        f"{sorted(declared_ops)}."
                    ),
                    suggestion=(
                        f"Troque as operations inventadas {invented} por "
                        f"uma das declaradas: {sorted(declared_ops)}. "
                        "Não invente nomes de operation — use APENAS as "
                        "do Registry."
                    ),
                    evidence=workflow[:300],
                ))

    ok = all(v.severity != "critical" for v in violations)
    return ValidationResult(ok=ok, violations=violations)
