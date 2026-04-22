"""Rotas de skills — parse canônico SKILL.md §5."""
import uuid, json, hashlib
from fastapi import APIRouter, HTTPException
from app.models.schemas import SkillCreateRaw, SkillCreateManual
from app.core.database import skills_repo
from app.skill_parser.parser import parse_skill_md, skill_to_db_dict

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])

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
    await skills_repo.create(db_data)
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
    await skills_repo.create(d)
    return {"id": sid, "message": "Skill criada manualmente"}

@router.put("/{skill_id}")
async def update_skill(skill_id: str, data: SkillCreateRaw):
    existing = await skills_repo.find_by_id(skill_id)
    if not existing: raise HTTPException(404)
    parsed = parse_skill_md(data.raw_content)
    db_data = skill_to_db_dict(parsed)
    db_data["version"] = _bump_version(existing.get("version", "0.1.0"))
    db_data["tags"] = data.tags or existing.get("tags", "[]")
    return await skills_repo.update(skill_id, db_data)

@router.delete("/{skill_id}")
async def delete_skill(skill_id: str):
    if not await skills_repo.delete(skill_id): raise HTTPException(404)
    return {"message": "Skill removida"}

def _bump_version(v: str) -> str:
    parts = v.split(".")
    if len(parts) == 3:
        parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts)