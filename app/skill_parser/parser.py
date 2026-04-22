"""Parser canônico de SKILL.md — §5 da especificação.

Extrai frontmatter YAML e seções obrigatórias/opcionais do Markdown,
valida estrutura e gera hash de conteúdo.

MELHORIA 2026-04-21: Seção ## Execution Profile com auto-inferência.
Determina modo de execução (fast/standard/rigorous) para otimizar
pipeline — elimina chamadas LLM desnecessárias.
"""

import re
import hashlib
import json
import yaml
from dataclasses import dataclass, field, asdict
from typing import Optional


REQUIRED_SECTIONS = ["Purpose", "Activation Criteria", "Inputs", "Workflow", "Tool Bindings", "Output Contract", "Failure Modes"]
OPTIONAL_SECTIONS = ["Delegations", "Compensation", "Guardrails", "Budget", "Examples", "Telemetry", "Data Dependencies", "Model Constraints", "Evidence Policy", "Gold Refs", "Execution Profile"]
VALID_KINDS = {"orchestrator", "router", "subagent"}
VALID_STABILITY = {"alpha", "beta", "stable", "deprecated"}
VALID_EXEC_MODES = {"fast", "standard", "rigorous"}


@dataclass
class SkillFrontmatter:
    id: str = ""
    version: str = "0.1.0"
    kind: str = "subagent"
    owner: str = ""
    stability: str = "alpha"


@dataclass
class ParsedSkill:
    frontmatter: SkillFrontmatter = field(default_factory=SkillFrontmatter)
    name: str = ""
    purpose: str = ""
    activation_criteria: str = ""
    inputs: str = ""
    workflow: str = ""
    tool_bindings: str = ""
    output_contract: str = ""
    failure_modes: str = ""
    delegations: str = ""
    compensation: str = ""
    guardrails: str = ""
    budget: str = ""
    examples: str = ""
    telemetry: str = ""
    data_dependencies: str = ""
    model_constraints: str = ""
    evidence_policy: str = ""
    gold_refs: str = ""
    execution_profile: str = ""
    execution_mode: str = ""       # fast | standard | rigorous (parsed or inferred)
    raw_content: str = ""
    content_hash: str = ""
    validation_errors: list = field(default_factory=list)
    is_valid: bool = True


def parse_skill_md(content: str) -> ParsedSkill:
    """Faz parsing completo de um SKILL.md conforme anatomia canônica §5.
    Resiliente: remove code fences, busca frontmatter em qualquer posição,
    gera defaults quando ausente."""
    # ── Pré-processamento: remove code fences do wizard ──
    cleaned = content.strip()
    cleaned = re.sub(r'^```(?:markdown|md|yaml)?\s*\n', '', cleaned)
    cleaned = re.sub(r'\n```\s*$', '', cleaned)
    cleaned = cleaned.strip()

    result = ParsedSkill(raw_content=content)
    result.content_hash = hashlib.sha256(content.encode()).hexdigest()

    # ── Frontmatter YAML — busca em qualquer posição ──
    fm_match = re.search(r"^---\s*\n(.*?)\n---", cleaned, re.DOTALL | re.MULTILINE)
    if fm_match:
        try:
            fm_data = yaml.safe_load(fm_match.group(1)) or {}
            result.frontmatter = SkillFrontmatter(
                id=fm_data.get("id", ""),
                version=str(fm_data.get("version", "0.1.0")),
                kind=fm_data.get("kind", "subagent"),
                owner=fm_data.get("owner", ""),
                stability=fm_data.get("stability", "alpha"),
            )
            if result.frontmatter.kind not in VALID_KINDS:
                result.validation_errors.append(f"kind inválido: {result.frontmatter.kind}. Válidos: {VALID_KINDS}")
            if result.frontmatter.stability not in VALID_STABILITY:
                result.validation_errors.append(f"stability inválido: {result.frontmatter.stability}")
        except yaml.YAMLError as e:
            result.validation_errors.append(f"Frontmatter YAML inválido: {e}")
    else:
        result.validation_errors.append("Frontmatter YAML ausente — usando defaults (alpha/subagent)")
        result.frontmatter = SkillFrontmatter()

    # ── Nome (H1) — busca no conteúdo limpo ──
    h1_match = re.search(r"^#\s+(.+)$", cleaned, re.MULTILINE)
    if h1_match:
        result.name = h1_match.group(1).strip()
    else:
        for line in cleaned.split("\n"):
            line = line.strip()
            if line and not line.startswith("---") and not line.startswith("```"):
                result.name = line[:100]
                break
        if not result.name:
            result.name = "Skill sem nome"
        result.validation_errors.append("Nome (heading H1 com #) ausente — extraído da primeira linha")

    # ── Seções (H2) ──
    sections = _extract_sections(cleaned)

    for section_name in REQUIRED_SECTIONS:
        key = _section_to_attr(section_name)
        value = sections.get(section_name, "")
        setattr(result, key, value)
        if not value.strip():
            result.validation_errors.append(f"Seção obrigatória ausente: ## {section_name}")

    for section_name in OPTIONAL_SECTIONS:
        key = _section_to_attr(section_name)
        value = sections.get(section_name, "")
        setattr(result, key, value)

    # ── Execution Profile — parse ou auto-inferência ──
    if result.execution_profile.strip():
        result.execution_mode = _parse_execution_mode(result.execution_profile)
    else:
        result.execution_mode = _infer_execution_mode(result)

    result.is_valid = len(result.validation_errors) == 0
    return result


def _parse_execution_mode(profile_text: str) -> str:
    """Parse mode from ## Execution Profile section content.

    Aceita formatos:
      mode: fast
      modo: rápido
      fast (keyword isolado)
    """
    lower = profile_text.lower().strip()

    # Formato key:value
    for line in profile_text.split('\n'):
        line_stripped = line.strip().lower()
        if line_stripped.startswith('mode:') or line_stripped.startswith('modo:'):
            val = line.split(':', 1)[1].strip().lower()
            # Mapear português
            mode_map = {
                'fast': 'fast', 'rápido': 'fast', 'rapido': 'fast',
                'standard': 'standard', 'padrão': 'standard', 'padrao': 'standard',
                'rigorous': 'rigorous', 'rigoroso': 'rigorous',
            }
            return mode_map.get(val, 'standard')

    # Keyword isolado
    if 'fast' in lower or 'rápido' in lower or 'rapido' in lower:
        return 'fast'
    if 'rigorous' in lower or 'rigoroso' in lower:
        return 'rigorous'

    return 'standard'


def _infer_execution_mode(parsed: ParsedSkill) -> str:
    """Auto-infere execution mode a partir das seções presentes no SKILL.md.

    Lógica:
    - Tem Evidence Policy substancial + Guardrails substanciais → rigorous
    - Tem Evidence Policy OU Guardrails → standard
    - Tem Tool Bindings sem Evidence Policy → fast (agente MCP típico)
    - Nenhum dos acima → fast
    """
    has_evidence = bool(
        parsed.evidence_policy
        and parsed.evidence_policy.strip()
        and len(parsed.evidence_policy.strip()) > 20
    )
    has_guardrails = bool(
        parsed.guardrails
        and parsed.guardrails.strip()
        and len(parsed.guardrails.strip()) > 50
    )
    has_tools = bool(
        parsed.tool_bindings
        and parsed.tool_bindings.strip()
        and len(parsed.tool_bindings.strip()) > 10
    )

    if has_evidence and has_guardrails:
        return 'rigorous'
    if has_evidence or has_guardrails:
        return 'standard'
    # MCP agents sem evidence policy → fast
    if has_tools and not has_evidence:
        return 'fast'
    return 'fast'


def _extract_sections(content: str) -> dict[str, str]:
    """Extrai todas seções H2 do markdown."""
    pattern = r"^##\s+(.+)$"
    matches = list(re.finditer(pattern, content, re.MULTILINE))
    sections = {}
    for i, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        sections[name] = content[start:end].strip()
    return sections


def _section_to_attr(name: str) -> str:
    """Converte nome de seção para nome de atributo Python."""
    return name.lower().replace(" ", "_")


def skill_to_db_dict(parsed: ParsedSkill) -> dict:
    """Converte ParsedSkill para dicionário compatível com o banco."""
    fm = parsed.frontmatter
    return {
        "urn": fm.id,
        "name": parsed.name or "Untitled",
        "kind": fm.kind,
        "domain": fm.id.split(":")[2] if fm.id.count(":") >= 3 else "",
        "version": fm.version,
        "stability": fm.stability,
        "owner": fm.owner,
        "purpose": parsed.purpose,
        "activation_criteria": parsed.activation_criteria,
        "inputs_schema": parsed.inputs,
        "workflow": parsed.workflow,
        "tool_bindings": parsed.tool_bindings,
        "output_contract": parsed.output_contract,
        "failure_modes": parsed.failure_modes,
        "delegations": parsed.delegations,
        "compensation": parsed.compensation,
        "guardrails": parsed.guardrails,
        "budget": parsed.budget,
        "examples": parsed.examples,
        "telemetry": parsed.telemetry,
        "data_dependencies": parsed.data_dependencies,
        "model_constraints": parsed.model_constraints,
        "evidence_policy": parsed.evidence_policy,
        "gold_refs": parsed.gold_refs,
        "raw_content": parsed.raw_content,
        "content_hash": parsed.content_hash,
    }


def validate_skill_references(parsed: ParsedSkill, existing_tools: list[str] = None) -> list[str]:
    """Valida referências externas do skill (tools, skills filhos)."""
    errors = []
    if parsed.tool_bindings and existing_tools is not None:
        try:
            bindings = json.loads(parsed.tool_bindings) if parsed.tool_bindings.startswith("[") else []
            for b in bindings:
                tool_name = b.get("name", "") if isinstance(b, dict) else str(b)
                if tool_name and tool_name not in existing_tools:
                    errors.append(f"Tool '{tool_name}' referenciada mas não registrada no Tool Registry")
        except (json.JSONDecodeError, TypeError):
            pass
    return errors