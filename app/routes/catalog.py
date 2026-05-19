"""Rotas REST do catálogo / marketplace (Onda 1, PR 2).

CRUD básico de entries. Workflow submit/approve fica para PR 3.

Convenção de auth:
- Listar/Obter: require_user, visibility filtrada.
- Criar: require_user; owner_user_id = current.
- Atualizar/Deletar: require_user; precisa ser owner OU root.

Lifecycle:
- POST cria sempre em status='draft'.
- PUT só aceita updates em draft (mudanças após approved/published exigem
  re-submissão — endpoint dedicado no PR 3).
- DELETE só permite draft ou archived (preserva histórico do que circulou).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.catalog.lifecycle import can_transition_entry, can_transition_review
from app.catalog.models import CatalogEntryCreate, CatalogEntryUpdate, SubmissionCreate, SubmissionDecision
from app.catalog.prechecks import run_prechecks
from app.catalog.queries import (
    can_user_see,
    db_row_to_entry_dict,
    is_root,
    list_visible_entries,
)
from app.catalog.urn import make_urn
from app.core.auth import require_user
from app.core.database import (
    audit_repo,
    catalog_entries_repo,
    catalog_submissions_repo,
    catalog_disclosure_repo,
    users_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])


# ─── Helpers ─────────────────────────────────────────────────────


async def _audit(action: str, entry_id: str, actor_id: str, details: Optional[dict] = None):
    """Registra evento no audit_log. Best-effort — falha não bloqueia."""
    try:
        await audit_repo.create({
            "entity_type": "catalog_entry",
            "entity_id": entry_id,
            "action": action,
            "actor": actor_id,
            "details": json.dumps(details or {}),
        })
    except Exception as e:
        logger.warning(f"audit log falhou para {action} on {entry_id}: {e}")


def _can_mutate(user: dict, entry: dict) -> bool:
    """Update/Delete permitido para owner ou root."""
    return is_root(user) or entry.get("owner_user_id") == user.get("id")


# ─── Endpoints ───────────────────────────────────────────────────


@router.get("/entries")
async def list_entries(
    kind: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    domain: Optional[str] = Query(None),
    owner_user_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_user),
):
    """Lista entries visíveis para o user, com filtros opcionais."""
    rows, total = await list_visible_entries(
        user,
        kind=kind,
        status=status,
        domain=domain,
        owner_user_id=owner_user_id,
        limit=limit,
        offset=offset,
    )
    return {"entries": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/entries/{entry_id}")
async def get_entry(entry_id: str, user: dict = Depends(require_user)):
    """Detalhe de uma entry. 404 se não existe OU não é visível ao user."""
    raw = await catalog_entries_repo.find_by_id(entry_id)
    if not raw:
        raise HTTPException(404, "Entry não encontrada")
    entry = db_row_to_entry_dict(raw)
    if not can_user_see(user, entry):
        # 404 (não 403) para não vazar existência de entries privadas
        raise HTTPException(404, "Entry não encontrada")
    return entry


@router.post("/entries", status_code=201)
async def create_entry(data: CatalogEntryCreate, user: dict = Depends(require_user)):
    """Cria entry em status='draft'. URN é gerado a partir de name+kind+version."""
    # Regra de produto: kind agent/skill/recipe precisa de vínculo a artefato
    try:
        data.require_artifact_link()
    except ValueError as e:
        raise HTTPException(422, str(e))

    try:
        urn = make_urn(data.kind, data.name, data.version)
    except ValueError as e:
        raise HTTPException(422, f"URN inválido: {e}")

    entry_id = str(uuid.uuid4())
    row = {
        "id": entry_id,
        "urn": urn,
        "name": data.name,
        "description": data.description,
        "kind": data.kind,
        "artifact_type": data.artifact_type,
        "artifact_id": data.artifact_id,
        "domain": data.domain,
        "version": data.version,
        "status": "draft",
        "visibility": data.visibility,
        "visibility_scope": data.visibility_scope,
        "owner_user_id": user["id"],
        "steward_team": data.steward_team,
        "adapter_type": data.adapter_type,
        "adapter_config": json.dumps(data.adapter_config or {}),
        "tags": json.dumps(data.tags or []),
    }
    try:
        await catalog_entries_repo.create(row)
    except Exception as e:
        # Postgres unique violation no urn → 409
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(409, f"URN já existe: {urn}")
        raise

    await _audit("created", entry_id, user["id"], {"urn": urn, "kind": data.kind})
    return db_row_to_entry_dict(row)


@router.put("/entries/{entry_id}")
async def update_entry(
    entry_id: str,
    data: CatalogEntryUpdate,
    user: dict = Depends(require_user),
):
    """Atualiza campos editáveis da entry (status NÃO é alterado aqui).

    Restrições:
    - Apenas owner ou root.
    - Apenas entries em status='draft' aceitam edição direta.
      Entries publicadas precisam ser depreciadas e re-submetidas (Onda 2+).
    """
    existing = await catalog_entries_repo.find_by_id(entry_id)
    if not existing:
        raise HTTPException(404, "Entry não encontrada")
    if not _can_mutate(user, existing):
        raise HTTPException(403, "Apenas owner ou root podem editar")
    if existing.get("status") != "draft":
        raise HTTPException(
            409,
            f"Entry em status '{existing.get('status')}' não pode ser editada diretamente — "
            "depreque e re-submeta",
        )

    changes = data.model_dump(exclude_unset=True)
    if not changes:
        return db_row_to_entry_dict(existing)

    # Serializa campos que vão como TEXT JSON
    if "adapter_config" in changes and changes["adapter_config"] is not None:
        changes["adapter_config"] = json.dumps(changes["adapter_config"])
    if "tags" in changes and changes["tags"] is not None:
        changes["tags"] = json.dumps(changes["tags"])

    # Se name ou version mudou, recalcula URN para refletir
    if "name" in changes or "version" in changes:
        new_name = changes.get("name") or existing["name"]
        new_version = changes.get("version") or existing["version"]
        try:
            changes["urn"] = make_urn(existing["kind"], new_name, new_version)
        except ValueError as e:
            raise HTTPException(422, f"URN inválido: {e}")

    changes["updated_at"] = "now()"  # Postgres function literal não funciona aqui — usar timezone-aware
    # asyncpg vai tratar 'now()' como string. Removemos e deixamos o DEFAULT trigger
    # do schema fazer; alternativa seria importar datetime.utcnow() — mais explícito.
    from datetime import datetime, timezone
    changes["updated_at"] = datetime.now(timezone.utc)

    try:
        updated = await catalog_entries_repo.update(entry_id, changes)
    except Exception as e:
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(409, "URN já existe")
        raise

    if updated is None:
        raise HTTPException(404, "Entry não encontrada após update")
    await _audit("updated", entry_id, user["id"], {"changed_keys": list(changes.keys())})
    return db_row_to_entry_dict(updated)


@router.delete("/entries/{entry_id}")
async def delete_entry(entry_id: str, user: dict = Depends(require_user)):
    """Deleta entry. Permitido apenas em status='draft' ou 'archived'."""
    existing = await catalog_entries_repo.find_by_id(entry_id)
    if not existing:
        raise HTTPException(404, "Entry não encontrada")
    if not _can_mutate(user, existing):
        raise HTTPException(403, "Apenas owner ou root podem deletar")
    if existing.get("status") not in ("draft", "archived"):
        raise HTTPException(
            409,
            f"Entry em status '{existing.get('status')}' não pode ser deletada — "
            "arquive primeiro",
        )
    ok = await catalog_entries_repo.delete(entry_id)
    if not ok:
        raise HTTPException(404, "Entry não encontrada")
    await _audit("deleted", entry_id, user["id"], {"urn": existing.get("urn")})
    return {"message": "Entry removida", "id": entry_id}


# ═════════════════════════════════════════════════════════════════
# Workflow: submit → review (decide) → publish → deprecate
# ═════════════════════════════════════════════════════════════════


def _entry_snapshot(entry: dict) -> dict:
    """Captura subset relevante da entry para audit/replay da submissão."""
    keys = (
        "id", "urn", "name", "description", "kind", "artifact_type", "artifact_id",
        "domain", "version", "visibility", "visibility_scope", "owner_user_id",
        "steward_team", "adapter_type",
    )
    return {k: entry.get(k) for k in keys}


async def _require_status_transition(entry: dict, to_state: str):
    """Valida que entry pode transitar para to_state. 409 se não."""
    current = entry.get("status")
    if not can_transition_entry(current, to_state):
        raise HTTPException(
            409,
            f"Entry em status '{current}' não pode transitar para '{to_state}'",
        )


@router.post("/entries/{entry_id}/submit", status_code=201)
async def submit_entry(
    entry_id: str,
    data: SubmissionCreate,
    user: dict = Depends(require_user),
):
    """Publisher submete entry para revisão Root.

    Efeitos: roda pré-checks, cria submission (review_status='pending'),
    transita entry draft→submitted, registra audit.
    """
    entry_row = await catalog_entries_repo.find_by_id(entry_id)
    if not entry_row:
        raise HTTPException(404, "Entry não encontrada")
    entry = db_row_to_entry_dict(entry_row)
    if not _can_mutate(user, entry):
        raise HTTPException(403, "Apenas owner ou root podem submeter")
    await _require_status_transition(entry, "submitted")

    # Insumos para pré-checks
    disclosure = await catalog_disclosure_repo.find_by_id(entry_id)  # PK = entry_id
    owner = await users_repo.find_by_id(entry.get("owner_user_id"))
    report = run_prechecks(entry, disclosure=disclosure, owner=owner)

    import json
    import uuid
    sub_id = str(uuid.uuid4())
    submission = {
        "id": sub_id,
        "entry_id": entry_id,
        "submitted_by": user["id"],
        "snapshot": json.dumps(_entry_snapshot(entry)),
        "precheck_report": json.dumps(report),
        "precheck_passed": report["passed"],
        "review_status": "pending",
        "review_notes": (data.notes or ""),
    }
    await catalog_submissions_repo.create(submission)
    await catalog_entries_repo.update(entry_id, {
        "status": "submitted",
        "updated_at": datetime.now(timezone.utc),
    })
    await _audit("submitted", entry_id, user["id"], {
        "submission_id": sub_id,
        "precheck_passed": report["passed"],
        "warnings": report["warnings_count"],
        "errors": report["errors_count"],
    })

    return {
        "submission_id": sub_id,
        "entry_status": "submitted",
        "precheck_report": report,
    }


@router.post("/submissions/{sub_id}/decide")
async def decide_submission(
    sub_id: str,
    data: SubmissionDecision,
    user: dict = Depends(require_user),
):
    """Root decide sobre uma submissão. approved → entry vai para 'approved'
    (publisher publica em seguida). rejected/changes_requested → entry volta
    para 'draft' para iteração."""
    if not is_root(user):
        raise HTTPException(403, "Apenas Root pode decidir submissões")

    sub = await catalog_submissions_repo.find_by_id(sub_id)
    if not sub:
        raise HTTPException(404, "Submissão não encontrada")
    if not can_transition_review(sub.get("review_status"), data.decision):
        raise HTTPException(
            409,
            f"Submissão em status '{sub.get('review_status')}' não admite transição para '{data.decision}'",
        )

    # Estado da entry segue a decisão
    new_entry_status = "approved" if data.decision == "approved" else "draft"
    entry = await catalog_entries_repo.find_by_id(sub["entry_id"])
    if not entry:
        raise HTTPException(404, "Entry da submissão não encontrada")
    # Validamos a transição da entry também (defesa em profundidade)
    if not can_transition_entry(entry.get("status"), new_entry_status):
        raise HTTPException(
            409,
            f"Entry em status '{entry.get('status')}' não pode transitar para '{new_entry_status}'",
        )

    now = datetime.now(timezone.utc)
    await catalog_submissions_repo.update(sub_id, {
        "review_status": data.decision,
        "reviewed_by": user["id"],
        "reviewed_at": now,
        "review_notes": data.notes or "",
    })
    await catalog_entries_repo.update(sub["entry_id"], {
        "status": new_entry_status,
        "updated_at": now,
    })
    await _audit(
        f"review_{data.decision}",
        sub["entry_id"],
        user["id"],
        {"submission_id": sub_id, "new_entry_status": new_entry_status, "notes": data.notes or ""},
    )

    updated_sub = await catalog_submissions_repo.find_by_id(sub_id)
    return {"submission": updated_sub, "entry_status": new_entry_status}


@router.post("/entries/{entry_id}/publish")
async def publish_entry(entry_id: str, user: dict = Depends(require_user)):
    """Owner (ou root) publica entry aprovada. approved → published."""
    entry = await catalog_entries_repo.find_by_id(entry_id)
    if not entry:
        raise HTTPException(404, "Entry não encontrada")
    if not _can_mutate(user, entry):
        raise HTTPException(403, "Apenas owner ou root podem publicar")
    await _require_status_transition(entry, "published")

    now = datetime.now(timezone.utc)
    updated = await catalog_entries_repo.update(entry_id, {
        "status": "published",
        "published_at": now,
        "updated_at": now,
    })
    await _audit("published", entry_id, user["id"], {"urn": entry.get("urn")})
    return db_row_to_entry_dict(updated) if updated else {"message": "publicada"}


@router.post("/entries/{entry_id}/deprecate")
async def deprecate_entry(entry_id: str, user: dict = Depends(require_user)):
    """Owner (ou root) deprecia entry publicada. published → deprecated.
    Entry continua invocável mas com aviso ao consumer (UI)."""
    entry = await catalog_entries_repo.find_by_id(entry_id)
    if not entry:
        raise HTTPException(404, "Entry não encontrada")
    if not _can_mutate(user, entry):
        raise HTTPException(403, "Apenas owner ou root podem depreciar")
    await _require_status_transition(entry, "deprecated")

    now = datetime.now(timezone.utc)
    updated = await catalog_entries_repo.update(entry_id, {
        "status": "deprecated",
        "deprecated_at": now,
        "updated_at": now,
    })
    await _audit("deprecated", entry_id, user["id"], {"urn": entry.get("urn")})
    return db_row_to_entry_dict(updated) if updated else {"message": "depreciada"}


@router.get("/submissions/queue")
async def submissions_queue(
    status: str = Query("pending"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_user),
):
    """Fila de submissões para Root revisar.

    Default lista pendentes; query param permite ver decididas.
    Apenas Root acessa.
    """
    if not is_root(user):
        raise HTTPException(403, "Apenas Root pode ver a fila de revisão")
    filters = {"review_status": status} if status else {}
    items = await catalog_submissions_repo.find_all(limit=limit, offset=offset, **filters)
    total = await catalog_submissions_repo.count(**filters)
    return {"submissions": items, "total": total, "limit": limit, "offset": offset}


@router.get("/entries/{entry_id}/submissions")
async def entry_submissions(entry_id: str, user: dict = Depends(require_user)):
    """Histórico de submissões de uma entry. Visível para owner/root
    (mesma regra de mutate — submissões podem expor pré-checks sensíveis)."""
    entry = await catalog_entries_repo.find_by_id(entry_id)
    if not entry:
        raise HTTPException(404, "Entry não encontrada")
    if not _can_mutate(user, entry):
        raise HTTPException(403, "Apenas owner ou root podem ver histórico")
    items = await catalog_submissions_repo.find_all(entry_id=entry_id, limit=100)
    return {"submissions": items, "total": len(items)}
