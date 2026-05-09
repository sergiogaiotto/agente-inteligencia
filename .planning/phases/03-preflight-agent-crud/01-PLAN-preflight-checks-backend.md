---
wave: 1
depends_on: []
files_modified:
  - app/agents/preflight.py (novo)
  - app/routes/agents.py
  - app/models/schemas.py
autonomous: true
estimated_diff_lines: ~280
---

# Plan 01 — Backend: módulo de checks + endpoint + integração no save

## Objective

Criar módulo `app/agents/preflight.py` autocontido com 9 checks ortogonais. Cada check é uma função pura ou async que recebe o payload do agente + repos relevantes, e retorna `CheckResult | None`. Função pública `run_preflight(payload, ...)` orquestra os 9 e devolve `PreflightReport`.

Endpoint `POST /api/v1/agents/preflight` chama a função pública. `POST /agents` e `PUT /agents/{id}` rodam preflight antes do save; **errors** retornam 422 com a lista; warnings/info passam.

## Why

Backend isolado primeiro porque:
1. Cada check é puro — testável em smoke sem UI nem DB real (mock dos repos).
2. UI da Wave 2 só consome o response — quanto mais estável o contrato, menor o churn de template.
3. Bloqueio de save real é o garante: mesmo se um power-user enviar o `POST /agents` direto via curl, o preflight valida.

## Tasks

<task id="1" type="edit">
<file>app/models/schemas.py</file>
<location>após `AgentUpdate` (linha ~32)</location>
<change>
Adicionar response schemas:

```python
class PreflightCheckResult(BaseModel):
    id: str  # ex: "C1_api_key"
    severity: str = Field(..., pattern="^(error|warning|info)$")
    title: str  # ex: "API key não configurada"
    detail: str  # mensagem completa
    fix_hint: Optional[str] = None  # ex: "/settings → API Keys"
    field: Optional[str] = None  # ex: "llm_provider" — UI usa pra destacar


class PreflightReport(BaseModel):
    checks: list[PreflightCheckResult] = Field(default_factory=list)
    has_errors: bool = False
    has_warnings: bool = False
    blocked: bool = False  # = has_errors
```

Manter ao lado de `AgentUpdate` para discoverability — agente CRUD lifecycle inteiro num lugar só.
</change>
<acceptance>
- Schemas Pydantic v2 (BaseModel + Field).
- `severity` validado por regex.
- Defaults seguros (lista vazia, bools False).
</acceptance>
</task>

<task id="2" type="new">
<file>app/agents/preflight.py</file>
<change>
Criar módulo do zero:

```python
"""Pre-flight checks para agent create/edit — onda 4.

9 checks ortogonais. Cada um retorna CheckResult ou None (passou).
run_preflight() orquestra e produz PreflightReport.

Filosofia:
- error: bloqueia save (config quebrada na cara — pioraria depois).
- warning: passa o save mas sinaliza (pode quebrar em alguns casos).
- info: dica/observação, sem julgamento de "está errado".
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from app.models.schemas import PreflightCheckResult, PreflightReport

logger = logging.getLogger(__name__)


# ─── Catálogos locais (ajustar quando provider lançar modelo novo) ─────
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


# ─── Builders ───────────────────────────────────────────────────────────
def _check(id_: str, severity: str, title: str, detail: str,
           fix_hint: Optional[str] = None, field: Optional[str] = None) -> PreflightCheckResult:
    return PreflightCheckResult(
        id=id_, severity=severity, title=title, detail=detail,
        fix_hint=fix_hint, field=field,
    )


# ─── C1: API key ────────────────────────────────────────────────────────
def check_api_key(payload: dict, settings) -> Optional[PreflightCheckResult]:
    provider = (payload.get("llm_provider") or "").lower()
    key_attr = {
        "openai": "openai_api_key",
        "maritaca": "maritaca_api_key",
        "ollama": "ollama_api_key",
        "azure": "azure_api_key",
    }.get(provider)
    if not key_attr:
        return _check("C1_api_key", "warning",
                      f"Provider '{provider}' desconhecido",
                      f"Não há mapeamento de API key para o provider '{provider}'. "
                      "Verifique o nome em Configurações.",
                      fix_hint="/settings", field="llm_provider")
    key = (getattr(settings, key_attr, None) or "").strip()
    if not key or key.startswith(("sk-your", "your-", "mrt-your", "change", "placeholder")):
        # Ollama dispensa key real (default 'ollama')
        if provider == "ollama":
            return None
        return _check("C1_api_key", "error",
                      f"API key do '{provider}' não configurada",
                      f"O agente usa provider '{provider}' mas a API key correspondente "
                      f"não está setada (ou ainda é placeholder). "
                      f"Sem isso, o agente não consegue chamar o LLM.",
                      fix_hint="/settings", field="llm_provider")
    return None


# ─── C2: SKILL.md vinculado parseia ─────────────────────────────────────
async def check_skill_parses(payload: dict, skills_repo) -> Optional[PreflightCheckResult]:
    skill_id = payload.get("skill_id")
    if not skill_id:
        return None  # sem skill é cenário válido
    skill = await skills_repo.find_by_id(skill_id)
    if not skill:
        return _check("C2_skill_exists", "error",
                      "Skill vinculada não existe",
                      f"O agente referencia skill_id='{skill_id}' mas essa skill "
                      "não existe no banco. Vincule a uma skill válida.",
                      fix_hint="/skills", field="skill_id")
    raw = skill.get("raw_content") or ""
    if not raw.strip():
        return _check("C2_skill_empty", "error",
                      "SKILL.md vinculada está vazia",
                      f"A skill '{skill.get('name','?')}' não tem conteúdo em raw_content.",
                      fix_hint=f"/skills/{skill_id}/edit", field="skill_id")
    try:
        from app.skill_parser.parser import parse_skill_md
        parsed = parse_skill_md(raw)
    except Exception as e:
        return _check("C2_skill_parse", "error",
                      "SKILL.md vinculada falhou no parser",
                      f"Erro ao parsear: {type(e).__name__}: {str(e)[:200]}",
                      fix_hint=f"/skills/{skill_id}/edit", field="skill_id")
    if not parsed.is_valid:
        errs = "; ".join(parsed.validation_errors[:3]) or "validação falhou"
        return _check("C2_skill_invalid", "error",
                      "SKILL.md vinculada tem erros de validação",
                      f"Parser apontou: {errs}",
                      fix_hint=f"/skills/{skill_id}/edit", field="skill_id")
    return None


# ─── C3: MCP tools resolvem ─────────────────────────────────────────────
async def check_mcp_tools_resolve(payload: dict, skills_repo, tools_repo) -> Optional[PreflightCheckResult]:
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
            return None  # SKILL.md sem tools — nada a checar
        enriched = await match_with_registry(bindings, tools_repo)
        unmatched = [t.get("name", "?") for t in enriched if not t.get("db_id")]
        if unmatched:
            return _check("C3_mcp_unmatched", "warning",
                          f"{len(unmatched)} ferramenta(s) MCP não resolvem no Registry",
                          f"Declaradas no SKILL.md mas ausentes do Tools Registry: "
                          f"{', '.join(unmatched[:5])}. "
                          "O agente vai rodar sem essas ferramentas.",
                          fix_hint="/tools", field="skill_id")
    except Exception as e:
        logger.warning(f"check_mcp_tools_resolve falhou: {e}")
    return None


# ─── C4: Output Contract JSON parseável ─────────────────────────────────
async def check_output_contract_json(payload: dict, skills_repo) -> Optional[PreflightCheckResult]:
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
    # Detecta claim de JSON
    lower = contract.lower()
    has_json_claim = "json" in lower or '"type"' in lower or contract.strip().startswith("{")
    if not has_json_claim:
        return None
    # Extrai bloco entre fences se houver
    block_match = re.search(r"```(?:json)?\s*(.*?)```", contract, re.DOTALL)
    raw = block_match.group(1).strip() if block_match else contract.strip()
    if not raw.startswith("{") and not raw.startswith("["):
        return None  # claim mas sem bloco — não é responsabilidade do C4
    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        return _check("C4_output_contract_json", "warning",
                      "Output Contract claima JSON mas não parseia",
                      f"Skill declara contrato JSON, mas o bloco não é JSON válido: {e.msg} (linha {e.lineno}).",
                      fix_hint=f"/skills/{skill_id}/edit", field="skill_id")
    return None


# ─── C5: Inputs cobrem refs api_bindings ────────────────────────────────
async def check_inputs_cover_refs(payload: dict, skills_repo) -> Optional[PreflightCheckResult]:
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
    # Reusa helpers do agents.py
    from app.routes.agents import _extract_referenced_inputs, _extract_inputs_schema
    referenced = _extract_referenced_inputs(parsed.api_bindings_parsed)
    if not referenced:
        return None
    schema = _extract_inputs_schema(parsed.inputs) or {}
    declared = set((schema.get("properties") or {}).keys())
    # Aceita qualquer X em inputs.X declarado E também caminhos aninhados (inputs.foo.bar — declared='foo' basta)
    missing = []
    for ref in referenced:
        # ref vem como "inputs.X" ou "X" — ambos formatos do regex
        head = ref.split(".", 1)[0]
        if head == "inputs":
            head = ref.split(".", 2)[1] if "." in ref[7:] else ref[7:]
        if head not in declared:
            missing.append(ref)
    if missing:
        return _check("C5_inputs_missing", "warning",
                      f"{len(missing)} input(s) referenciados mas não declarados",
                      f"API bindings usam {{{{inputs.X}}}} para: "
                      f"{', '.join(missing[:5])}, mas a seção `## Inputs` "
                      "do SKILL.md não declara esses campos.",
                      fix_hint=f"/skills/{skill_id}/edit", field="skill_id")
    return None


# ─── C6: Anti pass-through não-intencional ──────────────────────────────
def check_passthrough_intent(payload: dict) -> Optional[PreflightCheckResult]:
    if payload.get("skill_id"):
        return None  # com skill, nunca é pass-through
    sp = (payload.get("system_prompt") or "").strip()
    if not sp:
        return _check("C6_passthrough", "warning",
                      "Agente sem skill e sem system prompt",
                      "Sem SKILL.md vinculado e sem system prompt, este agente "
                      "será pass-through em pipelines (input propagado sem LLM call). "
                      "Se a intenção é processamento ativo, vincule uma skill ou "
                      "escreva um prompt detalhado.",
                      field="system_prompt")
    if len(sp) < 50:
        return _check("C6_passthrough", "warning",
                      "System prompt muito curto",
                      f"System prompt tem só {len(sp)} caracteres. "
                      "Sem skill vinculada, agentes com prompt < 50 chars viram "
                      "pass-through em pipelines.",
                      field="system_prompt")
    if len(sp) < 200:
        sp_low = sp.lower()
        for marker in GENERIC_PROMPT_MARKERS:
            if marker in sp_low:
                return _check("C6_passthrough", "warning",
                              "System prompt parece genérico",
                              f"O prompt curto contém marcador genérico ('{marker}'). "
                              "Em pipeline, agentes assim viram pass-through. "
                              "Se a intenção é processar ativamente, especialize o prompt.",
                              field="system_prompt")
    return None


# ─── C7: Model conhecido do provider ────────────────────────────────────
def check_model_known(payload: dict) -> Optional[PreflightCheckResult]:
    provider = (payload.get("llm_provider") or "").lower()
    model = (payload.get("model") or "").strip()
    if not model:
        return _check("C7_model", "warning",
                      "Modelo não definido",
                      "Sem nome de modelo, o agente não consegue chamar o LLM.",
                      field="model")
    known = KNOWN_MODELS.get(provider)
    if known is None or not known:  # sem catálogo (ollama/azure) — passa sem julgar
        return None
    if model not in known:
        return _check("C7_model", "info",
                      f"Modelo '{model}' não está na lista conhecida de '{provider}'",
                      f"Pode ser typo ou modelo recente não catalogado aqui. "
                      f"Modelos conhecidos: {', '.join(sorted(known)[:6])}…",
                      field="model")
    return None


# ─── C8: Versão semver ──────────────────────────────────────────────────
def check_version_semver(payload: dict) -> Optional[PreflightCheckResult]:
    v = (payload.get("version") or "").strip()
    if not v:
        return None  # default kicks in
    if not SEMVER_RE.match(v):
        return _check("C8_semver", "info",
                      "Versão não está em formato semver",
                      f"Versão '{v}' não bate semver (MAJOR.MINOR.PATCH). "
                      "Ferramentas de auto-bump assumem semver.",
                      fix_hint=None, field="version")
    return None


# ─── C9: Temperature alta ───────────────────────────────────────────────
def check_temperature_sane(payload: dict) -> Optional[PreflightCheckResult]:
    t = payload.get("temperature")
    if t is None:
        return None
    try:
        t = float(t)
    except (TypeError, ValueError):
        return None
    if t > 1.5:
        return _check("C9_temperature", "info",
                      f"Temperatura alta ({t})",
                      "Temperaturas > 1.5 podem produzir saídas erráticas. "
                      "Para produção, prefira ≤ 1.0. Pydantic já bloqueia > 2.0.",
                      field="temperature")
    return None


# ─── Orchestrator público ───────────────────────────────────────────────
async def run_preflight(payload: dict) -> PreflightReport:
    """Roda os 9 checks. Retorna PreflightReport com lista ordenada
    (errors primeiro, depois warnings, depois infos).
    """
    from app.core.config import get_settings
    from app.core.database import skills_repo, tools_repo
    settings = get_settings()
    
    results: list[PreflightCheckResult] = []
    
    # Síncronos primeiro (sem I/O)
    for fn in (check_api_key, check_passthrough_intent,
               check_model_known, check_version_semver,
               check_temperature_sane):
        try:
            r = fn(payload, settings) if fn is check_api_key else fn(payload)
            if r:
                results.append(r)
        except Exception as e:
            logger.warning(f"check {fn.__name__} explodiu: {e}")
    
    # Async (DB queries) — paralelizar
    import asyncio
    async_checks = [
        check_skill_parses(payload, skills_repo),
        check_mcp_tools_resolve(payload, skills_repo, tools_repo),
        check_output_contract_json(payload, skills_repo),
        check_inputs_cover_refs(payload, skills_repo),
    ]
    async_results = await asyncio.gather(*async_checks, return_exceptions=True)
    for r in async_results:
        if isinstance(r, Exception):
            logger.warning(f"async check explodiu: {r}")
        elif r:
            results.append(r)
    
    # Ordenar: errors → warnings → infos, dentro do mesmo bucket por id
    sev_order = {"error": 0, "warning": 1, "info": 2}
    results.sort(key=lambda c: (sev_order.get(c.severity, 3), c.id))
    
    has_errors = any(c.severity == "error" for c in results)
    has_warnings = any(c.severity == "warning" for c in results)
    
    return PreflightReport(
        checks=results,
        has_errors=has_errors,
        has_warnings=has_warnings,
        blocked=has_errors,
    )
```
</change>
<acceptance>
- 9 funções de check independentes; cada uma retorna `CheckResult | None`.
- `run_preflight` roda síncronos + async (gather) — total < 100ms em DB local.
- Ordenação consistente (errors primeiro).
- Cada check tolera exceções internas (loga + segue).
</acceptance>
</task>

<task id="3" type="edit">
<file>app/routes/agents.py</file>
<location>após `delete_agent` (linha ~75)</location>
<change>
Adicionar endpoint:

```python
@router.post("/preflight")
async def preflight_agent(data: AgentCreate):
    """Roda 9 checks contra o payload de agente (sem persistir).
    Retorna PreflightReport. UI usa pra mostrar checks antes do save.
    """
    from app.agents.preflight import run_preflight
    return await run_preflight(data.model_dump())
```

E modificar `create_agent` e `update_agent` para rodar preflight antes do save:

```python
@router.post("", status_code=201)
async def create_agent(data: AgentCreate):
    from app.agents.preflight import run_preflight
    report = await run_preflight(data.model_dump())
    if report.blocked:
        raise HTTPException(422, detail={
            "message": "Configuração tem erros — corrija antes de salvar",
            "preflight": report.model_dump(),
        })
    aid = str(uuid.uuid4())
    # ... resto existente
```

Mesmo para `update_agent`: rodar preflight com payload mesclado (existing + upd).
</change>
<acceptance>
- `POST /agents/preflight` retorna 200 com `PreflightReport`.
- `POST /agents` com payload errado retorna 422 com `detail.preflight`.
- `PUT /agents/{id}` mesma lógica.
- Pydantic valida o body normalmente antes do preflight.
</acceptance>
</task>

## Verification

- [ ] Smoke import + 9 funções de check existem.
- [ ] `run_preflight` com payload mínimo (provider sem key) retorna 1 error (C1_api_key).
- [ ] `run_preflight` com skill_id inexistente → 1 error (C2_skill_exists).
- [ ] `run_preflight` com payload OK → `checks=[]`, `blocked=False`.
- [ ] Manual: `curl -X POST /api/v1/agents/preflight -d '{name:"x",llm_provider:"openai",model:"gpt-4o"}'` retorna report.
- [ ] Manual: `curl -X POST /api/v1/agents -d '{...}' ` com erro → 422.

## must_haves

- Plan 02 (UI) consome `PreflightReport` e renderiza por severidade.
- Comportamento legacy preservado para payloads que passam todos os checks.

## Notes

- Reusa `_extract_referenced_inputs`/`_extract_inputs_schema` de [agents.py](app/routes/agents.py) — import privado é dívida menor, refator pra `app/agents/utils.py` quando aparecer 3º consumidor.
- `KNOWN_MODELS` é manutenção: quando provider lançar, adicionar. Sem isso, falsos info-warnings. Severity=info segura o ruído.
- A combinação skill_id → SKILL.md → tool_bindings → tools_repo dá 4 checks (C2/C3/C4/C5) com sub-cascata. C2 falha → C3/C4/C5 são no-op (skill não existe ou não parseia → cedem). Aceitável: lista de checks fica enxuta quando há erro grave.
