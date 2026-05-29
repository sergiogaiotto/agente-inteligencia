"""Dry-run de tool calls declarados na SKILL.md — preview/validação sem
chamar servidor MCP real.

User pediu (2026-05-29): "faz sentido na tela de Skill em preview/validação
ter a simulação de funcionamento de qualquer function calling escolhidos".
Sim — encurta o loop de feedback de 6 passos (gerar→salvar→agente→
vincular→workspace→mensagem) pra 1 botão.

Fase 1 deste módulo: dry-run determinístico SEM chamar servidor MCP.
- Parse a SKILL.md
- Roda validador (regras estruturais G1-G4, operation.*, section.duplicated)
- Constrói o function spec que o engine criaria
- Valida que operation citada no Workflow está no enum do Registry
- Monta payload simulado que SERIA enviado
- Retorna estrutura com ok + payload + diagnóstico

Custo: zero tokens, zero side-effects, zero rede.

Fase 2 (PR futuro): modo "Live" — usa execute_tool_call do engine pra
chamar o servidor real, mostra latência + status code + retorno.

Fase 3 (PR opcional): fuzz com N inputs canônicos.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


class DryRunRequest(BaseModel):
    """Payload do POST /skills/dry-run-tool.

    skill_md: markdown completo da SKILL (incluindo frontmatter).
    tool_id: UUID da tool MCP declarada (precisa estar em Tool Bindings).
    operation_override: opcional. Quando vazio, usa a primeira do enum.
    sample_query: input de teste pra montar o payload (back-compat Fase 1).
    extra_params: PR #197 (Fase 2). Dict de field→value pra schemas
        customizados declarados em ## Inputs da SKILL. Quando presente,
        backend usa esses valores no payload simulado em vez do par
        {operation, query} fixo da Fase 1.
    """
    skill_md: str
    tool_id: str
    operation_override: Optional[str] = ""
    sample_query: Optional[str] = "exemplo de consulta"
    extra_params: Optional[dict] = None


class DryRunIssue(BaseModel):
    severity: str   # "critical" | "warning" | "info"
    rule: str
    message: str
    suggestion: str = ""


class DryRunResult(BaseModel):
    ok: bool
    """True quando nenhuma issue critical. Avisos não bloqueiam."""

    payload_that_would_be_sent: dict
    """Shape exato que o engine enviaria pra tool MCP em runtime.
    Quando há extra_params na request, reflete eles. Caso contrário,
    fallback Fase 1 {operation, query}."""

    function_spec: dict
    """Function spec OpenAI que o ENGINE constrói hoje em
    build_openai_tools — sempre {operation enum, query string}. Operador
    vê exatamente o que o LLM verá em runtime no estado atual da
    plataforma."""

    function_spec_skill_declared: Optional[dict] = None
    """PR #197 (Fase 2). Function spec que a SKILL DECLARA em ## Inputs.
    None quando ## Inputs não traz schema JSON parseável. Compara com
    function_spec pra detectar mismatch (causa raiz dos bugs Context7
    #1-#5 onde SKILL declara {action, subject, content} mas engine força
    {operation, query})."""

    issues: list[DryRunIssue]
    """Diagnóstico estruturado. UI mostra com cores/agrupado por severidade."""

    operation_resolved: str
    """Operation efetivamente usada (após override do user OU primeira do enum)."""


# ───────────────────────────────────────────────────────────────
# Helpers — parsing leve de Tool Bindings + Workflow
# ───────────────────────────────────────────────────────────────


def _extract_tool_id_from_bindings(skill_md: str) -> list[str]:
    """Acha UUIDs no formato Wizard `- \`uuid\` (Name) — desc` em ## Tool
    Bindings. Não toca outras seções."""
    m = re.search(r"##\s+Tool Bindings\s*\n([\s\S]*?)(?=\n##\s|$)", skill_md)
    if not m:
        return []
    block = m.group(1)
    return re.findall(r"`([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})`", block)


def _extract_tool_name_from_bindings(skill_md: str, tool_id: str) -> str:
    """Acha o nome humano da tool (entre parênteses depois do UUID)."""
    pattern = rf"`{re.escape(tool_id)}`\s*\(([^)]+)\)"
    m = re.search(pattern, skill_md)
    return m.group(1).strip() if m else ""


def _split_csv_or_json(ops_raw: str) -> list[str]:
    """Operations vêm como CSV ou JSON list do Registry. Normaliza."""
    if not ops_raw:
        return []
    s = ops_raw.strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            import json
            arr = json.loads(s)
            return [str(x).strip() for x in arr if str(x).strip()]
        except (ValueError, TypeError):
            pass
    return [tok.strip() for tok in s.split(",") if tok.strip()]


def _sanitize_function_name(name: str) -> str:
    """Alinha com runtime.py:build_openai_tools — function name pra OpenAI."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", (name or "tool")).strip("_")[:64]


def _extract_inputs_schema(skill_md: str) -> Optional[dict]:
    """Extrai o JSON Schema declarado em `## Inputs` da SKILL.md.

    A seção tipicamente tem formato:

        ## Inputs
        ```json
        {"type": "object", "properties": {...}, "required": [...]}
        ```

    Returns:
        dict com o JSON Schema parseado OU None quando:
        - Seção ## Inputs ausente
        - Não tem bloco fenced JSON
        - JSON malformado
        - Schema não tem 'properties' (não dá pra usar como function spec)

    PR #197 (Fase 2): permite o dry-run mostrar o schema REAL que a SKILL
    declara, separado do schema fixo que o engine MCP força. Quando os
    dois divergem, é a causa raiz dos bugs Context7 #1-#5 (SKILL declara
    {action, subject, content}, engine força {operation, query}).
    """
    if not skill_md:
        return None
    # Acha a seção ## Inputs até próxima seção
    m = re.search(r"##\s+Inputs\s*\n([\s\S]*?)(?=\n##\s|$)", skill_md)
    if not m:
        return None
    block = m.group(1)
    # Primeiro bloco fenced json
    fence = re.search(r"```(?:json|JSON)?\s*\n([\s\S]*?)\n```", block)
    if not fence:
        return None
    import json
    try:
        schema = json.loads(fence.group(1).strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(schema, dict):
        return None
    # Schema sem properties não vira function spec útil
    if not isinstance(schema.get("properties"), dict):
        return None
    return schema


def _build_function_spec_from_skill_inputs(
    tool: dict,
    inputs_schema: dict,
) -> dict:
    """Constrói function spec OpenAI a partir do JSON Schema declarado
    em ## Inputs da SKILL. Diferente de _build_function_spec (que força
    {operation, query}), aqui o operador vê o schema REAL que a SKILL
    quer expor à tool.

    Preserva: type, properties, required do schema original.
    Limpa: $schema, title, description top-level, additionalProperties
    (não fazem sentido no function spec do LLM).
    """
    name = _sanitize_function_name(tool.get("name", ""))
    props = inputs_schema.get("properties") or {}
    required = inputs_schema.get("required") or []
    # Mantém só atributos relevantes pra function spec
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": (
                f"[SKILL-declared schema] Ferramenta MCP '{tool.get('name', '')}'."
            )[:900],
            "parameters": {
                "type": inputs_schema.get("type") or "object",
                "properties": props,
                "required": required,
            },
        },
    }


def _schemas_have_field_mismatch(
    engine_spec: dict,
    skill_spec: Optional[dict],
) -> Optional[tuple[list[str], list[str]]]:
    """Compara o function spec do engine vs o schema declarado pela SKILL.

    Returns tuple (skill_only, engine_only) com os nomes dos campos:
    - skill_only: campos que a SKILL declara mas o engine NÃO envia
    - engine_only: campos que o engine envia mas a SKILL não declara

    Returns None quando não há schema declarado (não dá pra comparar).
    """
    if not skill_spec:
        return None
    skill_props = set((skill_spec.get("function") or {}).get("parameters", {}).get("properties", {}).keys())
    engine_props = set((engine_spec.get("function") or {}).get("parameters", {}).get("properties", {}).keys())
    skill_only = sorted(skill_props - engine_props)
    engine_only = sorted(engine_props - skill_props)
    return (skill_only, engine_only)


# ───────────────────────────────────────────────────────────────
# Core: dry-run simulator
# ───────────────────────────────────────────────────────────────


async def _resolve_tool_from_registry(tool_id: str) -> Optional[dict]:
    """Lookup do Registry por UUID. Devolve dict {id, name, description,
    operations} ou None se não achar.

    Faz lookup direto no repo de tools. Não depende do cache do engine.
    """
    try:
        from app.core.database import tools_repo
        rows = await tools_repo.find_all(limit=500)
    except Exception as e:
        logger.warning(f"dry-run: tools_repo.find_all falhou: {e}")
        return None
    for r in rows:
        if str(r.get("id", "")) == tool_id:
            return {
                "id": r.get("id", ""),
                "name": r.get("name", ""),
                "description": r.get("description", "") or "",
                "operations": r.get("operations", "") or "",
            }
    return None


def _build_function_spec(tool: dict) -> dict:
    """Reconstrói o function spec OpenAI que app/mcp/runtime.py:build_openai_tools
    geraria — sem importar o módulo (mantém este endpoint isolado de runtime).

    Operador vê exatamente o JSON que o LLM em runtime vai ver — sem
    surpresas entre dry-run e execução real.
    """
    name = _sanitize_function_name(tool.get("name", ""))
    ops = _split_csv_or_json(tool.get("operations") or "")
    ops_str = ", ".join(ops) if ops else "(sem operações declaradas)"
    desc = (
        f"Ferramenta MCP '{tool.get('name', '')}'. Operações disponíveis: {ops_str}. "
        "Chame esta função quando o usuário solicitar dados via esta tool."
    )
    properties = {
        "operation": {
            "type": "string",
            "description": (
                f"Operação a executar. Disponíveis: {ops_str}."
                if ops else "Operação a executar."
            ),
        },
        "query": {
            "type": "string",
            "description": "Consulta/parâmetros para a operação.",
        },
    }
    if ops:
        properties["operation"]["enum"] = ops
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc[:900],
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": ["operation", "query"],
            },
        },
    }


def _diagnose(
    skill_md: str,
    tool: dict,
    operation_chosen: str,
    declared_ops: list[str],
    engine_spec: dict,
    skill_spec: Optional[dict] = None,
) -> list[DryRunIssue]:
    """Coleta issues do validador estático + checagens específicas de dry-run.

    PR #197 (Fase 2): nova regra schema.mismatch quando ## Inputs da SKILL
    declara campos diferentes do schema que o engine envia (causa raiz
    dos bugs Context7 #1-#5).
    """
    issues: list[DryRunIssue] = []

    # ── Validador estático completo (G1-G4, operation.*, section.*) ──
    try:
        from app.skill_parser.parser import parse_skill_md
        from app.skill_parser.wizard_validator import validate_generated_skill
        parsed = parse_skill_md(skill_md)
        bindings_for_validator = {
            "mcp_tools": [tool],
            "rag_sources": [],
            "data_tables": [],
            "api_endpoints": [],
        }
        result = validate_generated_skill(parsed, bindings_for_validator, raw_md=skill_md)
        for v in result.violations:
            issues.append(DryRunIssue(
                severity=v.severity, rule=v.rule,
                message=v.message, suggestion=v.suggestion,
            ))
    except Exception as e:
        logger.warning(f"dry-run: validate_generated_skill falhou: {e}")
        issues.append(DryRunIssue(
            severity="warning", rule="validator.error",
            message=f"Validador estático falhou: {type(e).__name__}",
            suggestion="Verifique se SKILL.md tem frontmatter YAML válido.",
        ))

    # ── Checagem específica de dry-run: operation escolhida bate com enum ──
    if declared_ops:
        if operation_chosen and operation_chosen not in declared_ops:
            issues.append(DryRunIssue(
                severity="critical",
                rule="dryrun.operation_not_in_enum",
                message=(
                    f"Operation escolhida '{operation_chosen}' NÃO está no enum "
                    f"do Registry ({declared_ops}). Servidor MCP rejeitará."
                ),
                suggestion=(
                    f"Use uma das declaradas: {declared_ops}."
                ),
            ))
    else:
        issues.append(DryRunIssue(
            severity="warning",
            rule="dryrun.no_operations_in_registry",
            message=(
                "Tool não tem operations declaradas no Registry. Function spec "
                "será gerado sem 'enum' — LLM em runtime pode chutar nome inválido."
            ),
            suggestion=(
                "Cadastre as operations da tool em /tools antes de usar a skill "
                "em produção."
            ),
        ))

    # ── PR #197 (Fase 2): schema.mismatch ──
    # Causa raiz dos bugs Context7 #1-#5: SKILL declara em ## Inputs um schema
    # tipo {action, subject, content} mas o engine MCP sempre força
    # {operation, query} em build_openai_tools. Em runtime o LLM tem que
    # "comprimir" um no outro e erra.
    diff = _schemas_have_field_mismatch(engine_spec, skill_spec)
    if diff is not None:
        skill_only, engine_only = diff
        if skill_only or engine_only:
            # Mensagem articula o problema arquitetural
            parts = []
            if skill_only:
                parts.append(f"SKILL declara em ## Inputs {skill_only} que o engine NÃO envia")
            if engine_only:
                parts.append(f"engine força {engine_only} que a SKILL NÃO declara")
            issues.append(DryRunIssue(
                severity="warning",  # warning porque o engine atual SEMPRE força
                                     # operation+query — não é falha da SKILL, é
                                     # gap arquitetural pra evolução futura
                rule="schema.mismatch",
                message=(
                    "Schema declarado pela SKILL diverge do que o engine MCP "
                    "envia em runtime: " + "; ".join(parts) + ". O LLM precisa "
                    "'comprimir' os campos da SKILL nos {operation, query} do "
                    "engine — frequente causa raiz de chamadas MCP mal-formadas."
                ),
                suggestion=(
                    "Curto prazo: documente no Workflow como mapear "
                    + ", ".join(skill_only or ["campos da SKILL"]) + " "
                    "em {operation, query}. Médio prazo: aguarde Fase 3 do "
                    "dry-run que vai expor schema customizado por tool via "
                    "Registry (sem precisar comprimir)."
                ),
            ))

    return issues


@router.post("/dry-run-tool")
async def dry_run_tool(data: DryRunRequest) -> DryRunResult:
    """Dry-run de uma tool MCP declarada na SKILL.md, SEM chamar servidor.

    Validações executadas:
    1. Tool com tool_id existe no Registry
    2. Validador estático completo (G1-G4, operation.missing/invented/
       contradicts_registry, section.duplicated)
    3. Operation escolhida bate com enum declarado
    4. Function spec construído + payload simulado mostrados pra inspeção

    Returns:
        DryRunResult com ok + payload_that_would_be_sent + function_spec +
        issues + operation_resolved.
    """
    if not data.skill_md.strip():
        raise HTTPException(400, "skill_md obrigatório")
    if not data.tool_id.strip():
        raise HTTPException(400, "tool_id obrigatório")

    # 1. Resolve tool no Registry
    tool = await _resolve_tool_from_registry(data.tool_id)
    if not tool:
        raise HTTPException(
            404,
            f"Tool '{data.tool_id}' não encontrada no Registry. "
            "Cadastre em /tools ou verifique o UUID em ## Tool Bindings.",
        )

    declared_ops = _split_csv_or_json(tool.get("operations") or "")

    # 2. Decide operation final (override do user OU primeira do enum)
    override = (data.operation_override or "").strip()
    if override:
        operation_chosen = override
    elif declared_ops:
        operation_chosen = declared_ops[0]
    else:
        operation_chosen = ""  # sem default — issue será sinalizada

    # 3. Function spec que o ENGINE criaria HOJE (sempre {operation, query})
    function_spec = _build_function_spec(tool)

    # 4. PR #197 (Fase 2): Function spec que a SKILL DECLARA em ## Inputs.
    # Quando presente, expomos lado a lado pra mostrar o gap arquitetural.
    inputs_schema = _extract_inputs_schema(data.skill_md)
    function_spec_skill_declared = (
        _build_function_spec_from_skill_inputs(tool, inputs_schema)
        if inputs_schema else None
    )

    # 5. Payload simulado.
    # Fase 1: payload era SEMPRE {operation, query}. Fase 2: quando o user
    # mandou extra_params, refletimos eles — operador vê o que SERIA
    # enviado SE o engine respeitasse o schema da SKILL.
    if data.extra_params:
        payload = dict(data.extra_params)
        # Garante operation_resolved no payload pra observability
        if "operation" not in payload and operation_chosen:
            payload["operation"] = operation_chosen
    else:
        payload = {
            "operation": operation_chosen,
            "query": data.sample_query or "exemplo de consulta",
        }

    # 6. Coleta issues (inclui schema.mismatch quando há SKILL spec)
    issues = _diagnose(
        data.skill_md, tool, operation_chosen, declared_ops,
        engine_spec=function_spec,
        skill_spec=function_spec_skill_declared,
    )

    # 7. ok = sem criticals
    ok = all(i.severity != "critical" for i in issues)

    logger.info(
        "skill.dry_run.completed",
        extra={
            "event": "skill.dry_run",
            "tool_id": data.tool_id,
            "tool_name": tool.get("name", ""),
            "operation_resolved": operation_chosen,
            "ok": ok,
            "critical_count": sum(1 for i in issues if i.severity == "critical"),
            "warning_count": sum(1 for i in issues if i.severity == "warning"),
            "has_skill_declared_schema": function_spec_skill_declared is not None,
            "has_extra_params": data.extra_params is not None,
        },
    )

    return DryRunResult(
        ok=ok,
        payload_that_would_be_sent=payload,
        function_spec=function_spec,
        function_spec_skill_declared=function_spec_skill_declared,
        issues=issues,
        operation_resolved=operation_chosen,
    )
