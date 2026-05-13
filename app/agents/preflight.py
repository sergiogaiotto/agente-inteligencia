"""Pre-flight checks para agent create/edit — Onda 4.

10 checks ortogonais. Cada um retorna CheckResult ou None (passou).
run_preflight() orquestra e produz PreflightReport.

Filosofia:
- error: bloqueia save (config quebrada na cara — pioraria depois).
- warning: passa o save mas sinaliza (pode quebrar em alguns casos).
- info: dica/observação, sem julgamento de "está errado".
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from app.models.schemas import PreflightCheckResult, PreflightReport

logger = logging.getLogger(__name__)


# ─── Catálogos locais (atualizar quando provider lançar modelo) ────────
KNOWN_MODELS: dict[str, set[str]] = {
    "openai": {
        "gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-4.1",
        "gpt-4.1-mini", "gpt-3.5-turbo", "o1-preview", "o1-mini", "o3-mini",
    },
    "maritaca": {"sabia-3", "sabiazinho-3", "sabia-2-medium", "sabia-2-small"},
    "ollama": set(),  # local, qualquer modelo é potencialmente válido
    "azure": set(),  # custom deployment names, sem catálogo
}

GENERIC_PROMPT_MARKERS = (
    "você é um agente inteligente",
    "you are an intelligent agent",
    "você é um assistente",
    "you are an assistant",
    "você é um agente especializado",
    "you are a specialized agent",
)

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([-+][a-zA-Z0-9.-]+)?$")

PLACEHOLDER_KEY_PREFIXES = ("sk-your", "your-", "mrt-your", "change", "placeholder")


def _check(id_: str, severity: str, title: str, detail: str,
           fix_hint: Optional[str] = None, field: Optional[str] = None) -> PreflightCheckResult:
    return PreflightCheckResult(
        id=id_, severity=severity, title=title, detail=detail,
        fix_hint=fix_hint, field=field,
    )


# ═══════════════════════════════════════════════════════════════════════
# Síncronos — sem I/O
# ═══════════════════════════════════════════════════════════════════════

def check_api_key(payload: dict, settings) -> Optional[PreflightCheckResult]:
    """C1 — provider declarado tem API key real configurada."""
    provider = (payload.get("llm_provider") or "").lower()
    # Onda 7 Wave 5: "openai" semantic vira alias de Azure (cleanup OPENAI_API_KEY).
    # Ambos consultam azure_openai_api_key. Mapping também corrige "azure" pra
    # azure_openai_api_key (era "azure_api_key" — bug latent que nunca casava).
    key_attr_map = {
        "openai": "azure_openai_api_key",
        "azure": "azure_openai_api_key",
        "maritaca": "maritaca_api_key",
        "ollama": "ollama_api_key",
    }
    key_attr = key_attr_map.get(provider)
    if not key_attr:
        return _check(
            "C1_api_key", "warning",
            f"Provider '{provider}' desconhecido",
            f"Não há mapeamento de API key para o provider '{provider}'. "
            f"Providers suportados: {', '.join(sorted(key_attr_map))}.",
            fix_hint="/settings", field="llm_provider",
        )
    key = (getattr(settings, key_attr, None) or "").strip()
    # Ollama dispensa key real (default 'ollama'); demais precisam de algo válido.
    if provider == "ollama":
        return None
    if not key or any(key.startswith(p) for p in PLACEHOLDER_KEY_PREFIXES):
        return _check(
            "C1_api_key", "error",
            f"API key do '{provider}' não configurada",
            f"O agente usa provider '{provider}' mas a API key correspondente "
            "não está setada (ou ainda é placeholder). "
            "Sem isso, o agente não consegue chamar o LLM.",
            fix_hint="/settings", field="llm_provider",
        )
    return None


def check_passthrough_intent(payload: dict) -> Optional[PreflightCheckResult]:
    """C6 — alerta sobre pass-through não-intencional."""
    if payload.get("skill_id"):
        return None  # com skill, nunca é pass-through
    sp = (payload.get("system_prompt") or "").strip()
    if not sp:
        return _check(
            "C6_passthrough", "warning",
            "Agente sem skill e sem system prompt",
            "Sem SKILL.md vinculado e sem system prompt, este agente "
            "será pass-through em pipelines (input propagado sem LLM call). "
            "Se a intenção é processamento ativo, vincule uma skill ou "
            "escreva um prompt detalhado.",
            field="system_prompt",
        )
    if len(sp) < 50:
        return _check(
            "C6_passthrough", "warning",
            "System prompt muito curto",
            f"System prompt tem só {len(sp)} caracteres. "
            "Sem skill vinculada, agentes com prompt < 50 chars viram "
            "pass-through em pipelines.",
            field="system_prompt",
        )
    if len(sp) < 200:
        sp_low = sp.lower()
        for marker in GENERIC_PROMPT_MARKERS:
            if marker in sp_low:
                return _check(
                    "C6_passthrough", "warning",
                    "System prompt parece genérico",
                    f"O prompt curto contém marcador genérico ('{marker}'). "
                    "Em pipeline, agentes assim viram pass-through. "
                    "Se a intenção é processar ativamente, especialize o prompt.",
                    field="system_prompt",
                )
    return None


def check_model_known(payload: dict) -> Optional[PreflightCheckResult]:
    """C7 — modelo bate com lista conhecida do provider."""
    provider = (payload.get("llm_provider") or "").lower()
    model = (payload.get("model") or "").strip()
    if not model:
        return _check(
            "C7_model", "warning",
            "Modelo não definido",
            "Sem nome de modelo, o agente não consegue chamar o LLM.",
            field="model",
        )
    known = KNOWN_MODELS.get(provider)
    if known is None or not known:
        return None  # sem catálogo (ollama/azure) — passa sem julgar
    if model not in known:
        sample = ", ".join(sorted(known)[:6])
        return _check(
            "C7_model", "info",
            f"Modelo '{model}' não está na lista conhecida de '{provider}'",
            f"Pode ser typo ou modelo recente não catalogado aqui. "
            f"Modelos conhecidos: {sample}…",
            field="model",
        )
    return None


def check_version_semver(payload: dict) -> Optional[PreflightCheckResult]:
    """C8 — versão segue semver (info, não bloqueia)."""
    v = (payload.get("version") or "").strip()
    if not v:
        return None  # default kicks in
    if not SEMVER_RE.match(v):
        return _check(
            "C8_semver", "info",
            "Versão não está em formato semver",
            f"Versão '{v}' não bate semver (MAJOR.MINOR.PATCH). "
            "Ferramentas de auto-bump assumem semver.",
            field="version",
        )
    return None


def check_temperature_sane(payload: dict) -> Optional[PreflightCheckResult]:
    """C9 — temperatura > 1.5 é alta."""
    t = payload.get("temperature")
    if t is None:
        return None
    try:
        t = float(t)
    except (TypeError, ValueError):
        return None
    if t > 1.5:
        return _check(
            "C9_temperature", "info",
            f"Temperatura alta ({t})",
            "Temperaturas > 1.5 podem produzir saídas erráticas. "
            "Para produção, prefira ≤ 1.0. Pydantic já bloqueia > 2.0.",
            field="temperature",
        )
    return None


# ═══════════════════════════════════════════════════════════════════════
# Async — DB I/O (skills, tools)
# ═══════════════════════════════════════════════════════════════════════

async def check_skill_parses(payload: dict, skills_repo) -> Optional[PreflightCheckResult]:
    """C2 — skill_id existe e SKILL.md parseia OK."""
    skill_id = payload.get("skill_id")
    if not skill_id:
        return None  # sem skill é cenário válido
    skill = await skills_repo.find_by_id(skill_id)
    if not skill:
        return _check(
            "C2_skill_exists", "error",
            "Skill vinculada não existe",
            f"O agente referencia skill_id='{skill_id}' mas essa skill "
            "não existe no banco. Vincule a uma skill válida.",
            fix_hint="/skills", field="skill_id",
        )
    raw = skill.get("raw_content") or ""
    if not raw.strip():
        return _check(
            "C2_skill_empty", "error",
            "SKILL.md vinculada está vazia",
            f"A skill '{skill.get('name','?')}' não tem conteúdo em raw_content.",
            fix_hint=f"/skills/{skill_id}/edit", field="skill_id",
        )
    try:
        from app.skill_parser.parser import parse_skill_md
        parsed = parse_skill_md(raw)
    except Exception as e:
        return _check(
            "C2_skill_parse", "error",
            "SKILL.md vinculada falhou no parser",
            f"Erro ao parsear: {type(e).__name__}: {str(e)[:200]}",
            fix_hint=f"/skills/{skill_id}/edit", field="skill_id",
        )
    if not parsed.is_valid:
        errs = "; ".join(parsed.validation_errors[:3]) or "validação falhou"
        return _check(
            "C2_skill_invalid", "error",
            "SKILL.md vinculada tem erros de validação",
            f"Parser apontou: {errs}",
            fix_hint=f"/skills/{skill_id}/edit", field="skill_id",
        )
    return None


async def check_mcp_tools_resolve(payload: dict, skills_repo, tools_repo) -> Optional[PreflightCheckResult]:
    """C3 — MCP tools do SKILL.md resolvem no Tools Registry."""
    skill_id = payload.get("skill_id")
    if not skill_id:
        return None
    skill = await skills_repo.find_by_id(skill_id)
    if not skill:
        return None  # C2 já reporta
    raw = skill.get("raw_content") or ""
    if not raw.strip():
        return None
    try:
        from app.skill_parser.parser import parse_skill_md
        from app.mcp.runtime import parse_tool_bindings, match_with_registry
        parsed = parse_skill_md(raw)
        bindings = parse_tool_bindings(parsed.tool_bindings)
        if not bindings:
            return None  # SKILL sem tools — nada a checar
        enriched = await match_with_registry(bindings, tools_repo)
        unmatched = [t.get("name", "?") for t in enriched if not t.get("db_id")]
        if unmatched:
            return _check(
                "C3_mcp_unmatched", "warning",
                f"{len(unmatched)} ferramenta(s) MCP não resolvem no Registry",
                f"Declaradas no SKILL.md mas ausentes do Tools Registry: "
                f"{', '.join(unmatched[:5])}. "
                "O agente vai rodar sem essas ferramentas.",
                fix_hint="/tools", field="skill_id",
            )
    except Exception as e:
        logger.warning(f"check_mcp_tools_resolve falhou: {e}")
    return None


async def check_output_contract_json(payload: dict, skills_repo) -> Optional[PreflightCheckResult]:
    """C4 — Output Contract claima JSON e parseia."""
    skill_id = payload.get("skill_id")
    if not skill_id:
        return None
    skill = await skills_repo.find_by_id(skill_id)
    if not skill or not skill.get("raw_content"):
        return None
    try:
        from app.skill_parser.parser import parse_skill_md
        parsed = parse_skill_md(skill["raw_content"])
    except Exception:
        return None  # C2 já reporta
    contract = parsed.output_contract or ""
    if not contract.strip():
        return None
    lower = contract.lower()
    has_json_claim = "json" in lower or '"type"' in lower or contract.strip().startswith("{")
    if not has_json_claim:
        return None
    block_match = re.search(r"```(?:json)?\s*(.*?)```", contract, re.DOTALL)
    raw = block_match.group(1).strip() if block_match else contract.strip()
    if not (raw.startswith("{") or raw.startswith("[")):
        return None  # claim mas sem bloco objeto/array — fora de escopo do C4
    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        return _check(
            "C4_output_contract_json", "warning",
            "Output Contract claima JSON mas não parseia",
            f"Skill declara contrato JSON, mas o bloco não é JSON válido: "
            f"{e.msg} (linha {e.lineno}).",
            fix_hint=f"/skills/{skill_id}/edit", field="skill_id",
        )
    return None


async def check_tool_calling_support(payload: dict, skills_repo) -> Optional[PreflightCheckResult]:
    """C10 — modelo suporta function calling se skill declara tool_bindings.

    Maritaca/Sabia aceita o parâmetro `tools` no request (compat OpenAI) mas
    não retorna `tool_calls` no response — agente carrega Tavily/MCP, log mostra
    "Ferramenta(s) MCP vinculada(s)" mas o LLM nunca invoca. Falha silenciosa:
    nenhuma exception, métrica MCP TOOLS fica zerada, usuário não sabe por quê.

    Bypass: se `task_type=tool_calling`, routing live-resolve pro modelo certo
    (Azure GPT-4o por default em app/llm_routing.py) — não warna.
    """
    skill_id = payload.get("skill_id")
    if not skill_id:
        return None
    # task_type=tool_calling pula o snapshot e usa routing — sempre safe.
    if (payload.get("task_type") or "").strip().lower() == "tool_calling":
        return None
    skill = await skills_repo.find_by_id(skill_id)
    if not skill or not skill.get("raw_content"):
        return None
    try:
        from app.skill_parser.parser import parse_skill_md
        parsed = parse_skill_md(skill["raw_content"])
    except Exception:
        return None
    if not (parsed.tool_bindings or "").strip():
        return None  # sem bindings, nada pra invocar
    provider = (payload.get("llm_provider") or "").strip().lower()
    model = (payload.get("model") or "").strip().lower()
    # Maritaca/Sabia não retorna tool_calls mesmo com tools bound.
    # Ollama varia por modelo (Llama-3.1+ suporta, Llama-2 não) — não warna pra
    # não gerar false positives; deixa user-deployment decidir.
    is_unsupported = provider == "maritaca" or model.startswith("sabia")
    if not is_unsupported:
        return None
    return _check(
        "C10_tool_calling_support", "warning",
        "Modelo não suporta function calling",
        f"O SKILL.md vinculado declara `tool_bindings` (MCP tools) mas o agente "
        f"usa provider/modelo '{provider}/{model}'. Esses modelos aceitam o "
        "parâmetro `tools` no request mas não geram `tool_calls` no response — "
        "as ferramentas serão listadas como disponíveis mas nunca invocadas "
        "(métrica MCP TOOLS no painel de rastreabilidade fica zerada). "
        "Use um modelo OpenAI/Azure (ex: gpt-4o, gpt-4-turbo) ou Anthropic "
        "(claude-3.5+), ou defina `task_type=tool_calling` para que o routing "
        "automaticamente selecione um modelo compatível.",
        fix_hint="/settings", field="model",
    )


async def check_inputs_cover_refs(payload: dict, skills_repo) -> Optional[PreflightCheckResult]:
    """C5 — Inputs declarados cobrem `{{inputs.X}}` dos api_bindings."""
    skill_id = payload.get("skill_id")
    if not skill_id:
        return None
    skill = await skills_repo.find_by_id(skill_id)
    if not skill or not skill.get("raw_content"):
        return None
    try:
        from app.skill_parser.parser import parse_skill_md
        parsed = parse_skill_md(skill["raw_content"])
    except Exception:
        return None
    if not parsed.api_bindings_parsed:
        return None
    # Reusa helpers privados de routes/agents.py — refator pra utils
    # quando aparecer um terceiro consumidor.
    from app.routes.agents import _extract_referenced_inputs, _extract_inputs_schema
    referenced = _extract_referenced_inputs(parsed.api_bindings_parsed)
    if not referenced:
        return None
    schema = _extract_inputs_schema(parsed.inputs) or {}
    declared = set((schema.get("properties") or {}).keys())
    # Refs vêm sem prefixo "inputs." (helper já strip). Caminho aninhado
    # "foo.bar" passa se "foo" estiver declarado (não validamos sub-tree).
    missing = []
    for ref in referenced:
        head = ref.split(".", 1)[0]
        if head not in declared:
            missing.append(ref)
    if missing:
        return _check(
            "C5_inputs_missing", "warning",
            f"{len(missing)} input(s) referenciados mas não declarados",
            f"API bindings usam {{{{inputs.X}}}} para: "
            f"{', '.join(missing[:5])}, mas a seção `## Inputs` "
            "do SKILL.md não declara esses campos.",
            fix_hint=f"/skills/{skill_id}/edit", field="skill_id",
        )
    return None


# ═══════════════════════════════════════════════════════════════════════
# Orquestrador público
# ═══════════════════════════════════════════════════════════════════════

# Mapeia ordem de severidade pra ordenação consistente do report.
_SEV_ORDER = {"error": 0, "warning": 1, "info": 2}


async def run_preflight(payload: dict) -> PreflightReport:
    """Roda os 9 checks contra `payload` (dict no formato AgentCreate).
    Retorna PreflightReport com lista ordenada (errors → warnings → infos).

    Síncronos rodam serial; async com gather.
    """
    from app.core.config import get_settings
    from app.core.database import skills_repo, tools_repo

    settings = get_settings()
    results: list[PreflightCheckResult] = []

    # Síncronos (sem I/O) — serial, custo desprezível.
    sync_runs = (
        ("check_api_key", lambda: check_api_key(payload, settings)),
        ("check_passthrough_intent", lambda: check_passthrough_intent(payload)),
        ("check_model_known", lambda: check_model_known(payload)),
        ("check_version_semver", lambda: check_version_semver(payload)),
        ("check_temperature_sane", lambda: check_temperature_sane(payload)),
    )
    for name, runner in sync_runs:
        try:
            r = runner()
            if r:
                results.append(r)
        except Exception as e:
            logger.warning(f"sync check {name} explodiu: {type(e).__name__}: {e}")

    # Async (DB queries) — paralelo via gather.
    async_coros = [
        check_skill_parses(payload, skills_repo),
        check_mcp_tools_resolve(payload, skills_repo, tools_repo),
        check_output_contract_json(payload, skills_repo),
        check_inputs_cover_refs(payload, skills_repo),
        check_tool_calling_support(payload, skills_repo),
    ]
    async_outputs = await asyncio.gather(*async_coros, return_exceptions=True)
    for r in async_outputs:
        if isinstance(r, Exception):
            logger.warning(f"async check explodiu: {type(r).__name__}: {r}")
        elif r:
            results.append(r)

    # Ordenação: errors → warnings → infos, com tie-break por id.
    results.sort(key=lambda c: (_SEV_ORDER.get(c.severity, 3), c.id))

    has_errors = any(c.severity == "error" for c in results)
    has_warnings = any(c.severity == "warning" for c in results)

    return PreflightReport(
        checks=results,
        has_errors=has_errors,
        has_warnings=has_warnings,
        blocked=has_errors,
    )
