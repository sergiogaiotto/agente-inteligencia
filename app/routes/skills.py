"""Rotas de skills — parse canônico SKILL.md §5."""
import logging
import uuid, json, hashlib
from fastapi import APIRouter, HTTPException
from app.models.schemas import SkillCreateRaw, SkillCreateManual
from app.core.database import skills_repo
from app.skill_parser.parser import parse_skill_md, skill_to_db_dict
from app.skill_parser.linter import lint_skill

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


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
    s = await skills_repo.find_by_id(skill_id)
    if not s: raise HTTPException(404, "Skill não encontrada")
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
    return {
        "id": sid,
        "urn": parsed.frontmatter.id,
        "name": parsed.name,
        "kind": parsed.frontmatter.kind,
        "execution_mode": parsed.execution_mode,
        "warnings": parsed.validation_errors if not parsed.is_valid else [],
        "message": "Skill criada" + (" (com avisos de validação)" if not parsed.is_valid else ""),
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
    db_data = skill_to_db_dict(parsed)
    db_data["version"] = _bump_version(existing.get("version", "0.1.0"))
    db_data["tags"] = data.tags or existing.get("tags", "[]")
    try:
        return await skills_repo.update(skill_id, db_data)
    except HTTPException:
        raise
    except Exception as e:
        _raise_for_db_error(e, db_data["urn"])

@router.delete("/{skill_id}")
async def delete_skill(skill_id: str):
    if not await skills_repo.delete(skill_id): raise HTTPException(404)
    return {"message": "Skill removida"}

def _bump_version(v: str) -> str:
    parts = v.split(".")
    if len(parts) == 3:
        parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts)