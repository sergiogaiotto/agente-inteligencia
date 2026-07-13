"""Rotas de skills — parse canônico SKILL.md §5."""
import logging
import uuid, json, hashlib
from fastapi import APIRouter, HTTPException
from app.models.schemas import SkillCreateRaw, SkillCreateManual
from app.core.database import skills_repo, knowledge_repo
from app.skill_parser.parser import (
    parse_skill_md, skill_to_db_dict, REQUIRED_SECTIONS, OPTIONAL_SECTIONS,
)
from app.skill_parser.linter import lint_skill

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/skills", tags=["skills"])

# Nomes de seção conhecidos (H2) — um nome de skill NUNCA deve ser um desses.
_KNOWN_SECTION_NAMES = {s.strip().lower() for s in (REQUIRED_SECTIONS + OPTIONAL_SECTIONS)}


def _reject_bad_skill_name(name: str) -> None:
    """422 quando o nome extraído é um heading/seção vazado — ex.: SKILL.md sem
    título H1 cujo primeiro conteúdo era '## Evidence Policy'. Defense-in-depth
    com o parser (que já pula headings): barra também quem chega por outra via."""
    n = (name or "").strip()
    if n.startswith("#") or n.lstrip("#").strip().lower() in _KNOWN_SECTION_NAMES:
        raise HTTPException(
            422,
            detail="Nome inválido: a SKILL.md precisa de um título H1 (ex.: "
                   "'# Verificador de Número Primo') na primeira linha. O valor "
                   "extraído parece um cabeçalho de seção.",
        )


async def _warn_unknown_evidence_sources(parsed) -> list[str]:
    """Warnings para IDs do ## Evidence Policy que não existem em
    knowledge_sources (E2E Pulsar 2026-07-13: UUID digitado errado virava
    filtro SQL que casa 0 chunks — recusa silenciosa em runtime, sem pista).
    Non-blocking (coerente com o save de seções faltantes) e best-effort:
    falha de banco aqui nunca impede o save."""
    sources = (getattr(parsed, "evidence_policy_parsed", None) or {}).get("sources")
    if not sources:
        return []
    warnings: list[str] = []
    try:
        for sid in dict.fromkeys(sources):  # dedup preservando ordem
            row = await knowledge_repo.find_by_id(sid)
            if not row:
                warnings.append(
                    f"Evidence Policy: a source '{sid}' não existe em Bases de "
                    "Conhecimento — o retrieval retornará 0 evidências para ela. "
                    "Use o dropdown 'Fontes RAG' do editor para inserir o ID correto."
                )
            elif not row.get("authorized"):
                # Existe mas está desautorizada: BM25 e pgvector filtram
                # authorized=1, então o efeito em runtime é o mesmo do UUID
                # inexistente — 0 evidências, recusa silenciosa.
                warnings.append(
                    f"Evidence Policy: a source '{row.get('name') or sid}' existe "
                    "mas está DESAUTORIZADA (authorized=0) — o retrieval retornará "
                    "0 evidências para ela até que a base seja reautorizada."
                )
        if warnings:
            logger.warning(
                "SKILL.md salvo com source(s) desconhecida(s) no Evidence Policy",
                extra={
                    "event": "skill.evidence_policy.unknown_source",
                    "skill_name": parsed.name,
                    "declared_sources": list(sources),
                    "unknown_count": len(warnings),
                },
            )
    except Exception:
        logger.debug(
            "validação de sources do Evidence Policy falhou (best-effort)",
            extra={"event": "skill.evidence_policy.validation_failed"},
            exc_info=True,
        )
    return warnings


def _raise_for_db_error(e: Exception, urn: str) -> None:
    """Traduz erros do Postgres em HTTPException com mensagem acionável.

    Casos cobertos:
    - UniqueViolation no `urn` → 409 (Wizard IA reusa URNs genéricos como
      `knowledge-base-query`, e a segunda tentativa estourava 500 silencioso).
    - CHECK constraint (kind/stability) → 422 com enums aceitos. Defesa em
      profundidade caso o parser deixe passar valor fora do enum.
    - UndefinedColumn → 503 com nome da coluna faltante. Acontece quando o
      Postgres está em schema antigo e a migration idempotente correspondente
      não rodou (bug histórico da Onda Tabular: `data_tables` faltando).
      Mensagem inclui o comando SQL exato pra resolver.
    """
    msg = str(e).lower()
    if "duplicate key" in msg or "unique" in msg:
        raise HTTPException(
            409,
            f"Já existe uma skill com URN '{urn}'. "
            "Edite o frontmatter (campo `id`) com um slug diferente "
            "ou suba a `version` antes de criar.",
        )
    if "check constraint" in msg or "violates check" in msg:
        raise HTTPException(
            422,
            "Frontmatter inválido: `kind` deve ser orchestrator/router/subagent "
            f"e `stability` deve ser alpha/beta/stable/deprecated. Detalhe: {e}",
        )
    if "undefinedcolumnerror" in msg or "does not exist" in msg and "column" in msg:
        # Extrai nome da coluna da mensagem do Postgres:
        # `column "data_tables" of relation "skills" does not exist`
        import re as _re
        m = _re.search(r'column\s+"([^"]+)"', str(e))
        col = m.group(1) if m else "(desconhecida)"
        logger.error(
            "skills.create.schema_drift",
            extra={"event": "skills.create.schema_drift", "missing_column": col, "urn": urn},
        )
        raise HTTPException(
            503,
            f"Schema do banco desatualizado: coluna `{col}` ausente na tabela `skills`. "
            "Migration idempotente não foi aplicada neste ambiente. Rode no Postgres: "
            f"ALTER TABLE skills ADD COLUMN IF NOT EXISTS {col} TEXT DEFAULT '';  "
            "Ou reinicie o app — `init_db()` roda as migrations no startup.",
        )
    logger.exception("skills.create.unhandled_db_error", extra={"event": "skills.create.failed", "urn": urn})
    raise

@router.get("")
async def list_skills(limit: int = 50, offset: int = 0, kind: str = None, domain: str = None, stability: str = None):
    f = {}
    if kind: f["kind"] = kind
    if domain: f["domain"] = domain
    if stability: f["stability"] = stability
    return {"skills": await skills_repo.find_all(limit=limit, offset=offset, **f), "total": await skills_repo.count(**f)}

@router.get("/{skill_id}")
async def get_skill(skill_id: str):
    """Retorna a skill com metadata parsed da SKILL.md.

    Além das colunas brutas da tabela, devolve `summary` (objeto opt-in para
    clients) com derivações úteis pra UI mostrar sem ter que parsear o YAML
    no frontend: execution_mode, evidence_policy_parsed (com min_relevance/
    sources/max_age_days/cite_sources), e contagens de bindings (api/tables/
    tools). Útil pra agent_form step Revisão mostrar config-chave da skill
    vinculada sem o usuário ter que abrir Editar Skill.
    """
    s = await skills_repo.find_by_id(skill_id)
    if not s: raise HTTPException(404, "Skill não encontrada")
    # Parse defensivo — skill com raw_content inválido não derruba endpoint;
    # apenas devolve `summary` ausente. UI esconde a seção quando faltar.
    try:
        raw = s.get("raw_content") or ""
        if raw.strip():
            parsed = parse_skill_md(raw)
            # Contagem de tool bindings (texto markdown — split por linhas com
            # "- " ou "|" ; heurística simples sem parser de table)
            tool_bindings_text = parsed.tool_bindings or ""
            has_explicit_no_mcp = (
                "Nenhuma ferramenta MCP" in tool_bindings_text
                or "não usa ferramentas MCP" in tool_bindings_text
            )
            tool_count = 0
            if not has_explicit_no_mcp:
                # Conta linhas que começam com "- `" ou "- **" (formato típico
                # do wizard) ou linhas de tabela com "|"
                for line in tool_bindings_text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith(("- `", "- **")):
                        tool_count += 1
            s["summary"] = {
                "urn": parsed.frontmatter.id,
                "kind": parsed.frontmatter.kind,
                "stability": parsed.frontmatter.stability,
                "execution_mode": parsed.execution_mode,
                "evidence_policy_parsed": parsed.evidence_policy_parsed or {},
                "api_bindings_count": len(parsed.api_bindings_parsed or []),
                "data_tables_count": len(parsed.data_tables_parsed or []),
                "tool_bindings_count": tool_count,
                "tool_bindings_explicit_none": has_explicit_no_mcp,
                "sections_with_content": [
                    name for name, attr in [
                        ("Purpose", "purpose"),
                        ("Inputs", "inputs"),
                        ("Workflow", "workflow"),
                        ("Tool Bindings", "tool_bindings"),
                        ("Output Contract", "output_contract"),
                        ("Failure Modes", "failure_modes"),
                        ("Guardrails", "guardrails"),
                        ("Evidence Policy", "evidence_policy"),
                        ("Examples", "examples"),
                    ] if (getattr(parsed, attr, "") or "").strip()
                ],
            }
    except Exception as e:
        # Não derruba o GET — UI lida com summary ausente
        logger.warning(
            "skill_summary_parse_failed",
            extra={"event": "skills.summary.failed", "skill_id": skill_id, "error_type": type(e).__name__},
        )
    return s

@router.post("/lint", status_code=200)
async def lint_skill_endpoint(data: SkillCreateRaw):
    """Lint semântico de SKILL.md com foco em API Bindings declarativos.

    Retorna lista de issues (severity, binding_id, code, message) sem
    persistir nada. Útil para validar SKILL antes de criar ou publicar.
    """
    parsed = parse_skill_md(data.raw_content)
    issues = lint_skill(parsed)
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    return {
        "is_valid": parsed.is_valid and not errors,
        "parse_errors": parsed.validation_errors,
        "issues": issues,
        "summary": {
            "errors": len(errors),
            "warnings": len(warnings),
            "total": len(issues),
        },
        "execution_mode": parsed.execution_mode,
        "bindings_count": len(parsed.api_bindings_parsed or []),
    }


@router.post("/parse", status_code=200)
async def parse_skill(data: SkillCreateRaw):
    """Parse e valida SKILL.md sem salvar — preview."""
    parsed = parse_skill_md(data.raw_content)
    return {
        "is_valid": parsed.is_valid,
        "errors": parsed.validation_errors,
        "name": parsed.name,
        "kind": parsed.frontmatter.kind,
        "urn": parsed.frontmatter.id,
        "version": parsed.frontmatter.version,
        "stability": parsed.frontmatter.stability,
        "purpose": parsed.purpose[:200] if parsed.purpose else "",
        "execution_mode": parsed.execution_mode,
        "sections_found": [s for s in ["Purpose","Activation Criteria","Inputs","Workflow","Tool Bindings","Output Contract","Failure Modes","Delegations","Compensation","Guardrails","Budget","Examples","Evidence Policy","Gold Refs","Execution Profile"] if getattr(parsed, s.lower().replace(" ","_"), "")],
        "content_hash": parsed.content_hash,
    }

@router.post("", status_code=201)
async def create_skill(data: SkillCreateRaw):
    """Cria skill a partir de SKILL.md raw — parse canônico §5.
    Salva mesmo com avisos de validação (seções faltantes).
    Rejeita apenas se não houver frontmatter ou nome."""
    parsed = parse_skill_md(data.raw_content)
    _reject_bad_skill_name(parsed.name)

    if not parsed.name or parsed.name == "Skill sem nome" and len(data.raw_content.strip()) < 20:
        raise HTTPException(422, detail="Conteúdo insuficiente para criar skill")

    sid = str(uuid.uuid4())
    db_data = skill_to_db_dict(parsed)
    db_data["id"] = sid
    db_data["tags"] = data.tags or "[]"
    try:
        await skills_repo.create(db_data)
    except HTTPException:
        raise
    except Exception as e:
        _raise_for_db_error(e, db_data["urn"])
    warnings = (parsed.validation_errors if not parsed.is_valid else [])
    warnings += await _warn_unknown_evidence_sources(parsed)
    return {
        "id": sid,
        "urn": parsed.frontmatter.id,
        "name": parsed.name,
        "kind": parsed.frontmatter.kind,
        "execution_mode": parsed.execution_mode,
        "warnings": warnings,
        "message": "Skill criada" + (" (com avisos de validação)" if warnings else ""),
    }

@router.post("/manual", status_code=201)
async def create_skill_manual(data: SkillCreateManual):
    """Cria skill via formulário manual (sem parse SKILL.md)."""
    sid = str(uuid.uuid4())
    d = {"id": sid, **data.model_dump()}
    d["content_hash"] = hashlib.sha256(data.raw_content.encode()).hexdigest()
    try:
        await skills_repo.create(d)
    except HTTPException:
        raise
    except Exception as e:
        _raise_for_db_error(e, d.get("urn") or d.get("name", ""))
    return {"id": sid, "message": "Skill criada manualmente"}

@router.put("/{skill_id}")
async def update_skill(skill_id: str, data: SkillCreateRaw):
    existing = await skills_repo.find_by_id(skill_id)
    if not existing: raise HTTPException(404)
    parsed = parse_skill_md(data.raw_content)
    _reject_bad_skill_name(parsed.name)
    db_data = skill_to_db_dict(parsed)
    db_data["version"] = _bump_version(existing.get("version", "0.1.0"))
    db_data["tags"] = data.tags or existing.get("tags", "[]")
    try:
        updated = await skills_repo.update(skill_id, db_data)
    except HTTPException:
        raise
    except Exception as e:
        _raise_for_db_error(e, db_data["urn"])
    # Warnings aditivos no PUT (o create já devolvia; o update não devolvia nada
    # além do row — quem edita o UUID à mão faz isso no PUT, não no POST).
    warnings = (parsed.validation_errors if not parsed.is_valid else [])
    warnings += await _warn_unknown_evidence_sources(parsed)
    if isinstance(updated, dict) and warnings:
        updated = {**updated, "warnings": warnings}
    return updated

@router.delete("/{skill_id}")
async def delete_skill(skill_id: str):
    if not await skills_repo.delete(skill_id): raise HTTPException(404)
    return {"message": "Skill removida"}

def _bump_version(v: str) -> str:
    parts = v.split(".")
    if len(parts) == 3:
        parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts)