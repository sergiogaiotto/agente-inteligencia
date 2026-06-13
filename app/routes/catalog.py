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

import asyncio
import json
import logging
import uuid
from typing import Optional

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.catalog.lifecycle import can_transition_entry, can_transition_review
from app.catalog.models import (
    BulkDecisionPayload,
    CapabilityDisclosure,
    CatalogEntryCreate,
    CatalogEntryUpdate,
    ExternalPlatformMetadata,
    InvocationCostRecord,
    PipelinePublishRequest,
    ReassignPayload,
    RecipeDefinition,
    RecipeExecutionRequest,
    SubmissionCreate,
    SubmissionDecision,
)
from app.catalog.prechecks import run_prechecks
from app.catalog.queries import (
    aggregate_costs,
    can_user_see,
    can_user_see_execution,
    cleanup_orphan_submissions,
    create_execution,
    db_row_to_entry_dict,
    delete_disclosure,
    delete_recipe,
    get_disclosure,
    get_execution,
    get_external_metadata,
    get_recipe,
    is_root,
    list_costs_raw,
    list_executions_for_entry,
    list_inventory,
    list_stewardship,
    list_submissions_for_review,
    list_visible_entries,
    record_invocation_cost,
    upsert_disclosure,
    upsert_external_metadata,
    upsert_recipe,
)
from app.catalog.urn import make_urn, parse_urn
from app.core.auth import require_user
from app.core.federation_identity import local_workspace
from app.core.database import (
    audit_repo,
    catalog_entries_repo,
    catalog_submissions_repo,
    users_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])


# ─── Helpers ─────────────────────────────────────────────────────


def _naive_utc_now() -> datetime:
    """Retorna o UTC corrente como datetime tz-naive.

    As colunas de timestamp do schema usam TIMESTAMP (não TIMESTAMP WITH TIME
    ZONE), e asyncpg recusa datetime tz-aware nesses casos com
    'can't subtract offset-naive and offset-aware datetimes'. Este helper
    preserva o instante UTC mas remove o tzinfo, que é o que asyncpg espera.
    Use em qualquer write de coluna TIMESTAMP do projeto.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
        urn = make_urn(data.kind, data.name, data.version, workspace=await local_workspace())
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
        # Postgres unique violation no urn → 409 (mensagem orientada à ação,
        # sem expor o jargão URN; frontend detecta 409 para sugerir "subir versão")
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(
                409,
                f"Já existe uma publicação para '{data.name}' versão {data.version}. "
                f"Use uma versão diferente (ex: 1.0.1) ou edite a publicação existente."
            )
        raise

    await _audit("created", entry_id, user["id"], {"urn": urn, "kind": data.kind})
    return db_row_to_entry_dict(row)


@router.post("/entries/from-pipeline", status_code=201)
async def create_entry_from_pipeline(
    data: PipelinePublishRequest, user: dict = Depends(require_user)
):
    """PR4 — publica um pipeline do Estúdio como entry kind='pipeline' (draft).

    Cria a entry referenciando o pipeline (artifact_type='pipeline',
    artifact_id=pipeline_id) e devolve-a em status='draft'. Daqui segue o
    lifecycle EXISTENTE na página da entry (submit → approve → publish). O grafo
    (snapshot em catalog_pipeline_defs) e a execução via execute_pipeline são PR5.
    """
    from app.core.database import pipelines_repo

    pipeline = await pipelines_repo.find_by_id(data.pipeline_id)
    if not pipeline:
        raise HTTPException(404, "Pipeline não encontrado")

    name = (data.name or pipeline.get("name") or "").strip() or "Pipeline"
    try:
        urn = make_urn("pipeline", name, data.version, workspace=await local_workspace())
    except ValueError as e:
        raise HTTPException(422, f"URN inválido: {e}")

    entry_id = str(uuid.uuid4())
    row = {
        "id": entry_id,
        "urn": urn,
        "name": name,
        "description": pipeline.get("description") or "",
        "kind": "pipeline",
        "artifact_type": "pipeline",
        "artifact_id": data.pipeline_id,
        "domain": pipeline.get("domain"),
        "version": data.version,
        "status": "draft",
        "visibility": data.visibility,
        "visibility_scope": None,
        "owner_user_id": user["id"],
        "steward_team": None,
        "adapter_type": "a2a",
        # adapter_config guarda só a referência no PR4; o snapshot do grafo é PR5.
        "adapter_config": json.dumps({"pipeline_id": data.pipeline_id}),
        "tags": json.dumps([]),
    }
    try:
        await catalog_entries_repo.create(row)
    except Exception as e:
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(
                409,
                f"Já existe uma publicação para '{name}' versão {data.version}. "
                f"Use uma versão diferente (ex: {data.version.rsplit('.', 1)[0]}.1).",
            )
        raise

    await _audit(
        "created", entry_id, user["id"],
        {"urn": urn, "kind": "pipeline", "pipeline_id": data.pipeline_id},
    )
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
        # Preserva o workspace do URN existente — NÃO rebatiza p/ o workspace
        # local (uma entry federada/importada mantém seu namespace de origem).
        # Fallback ao workspace local só se o URN atual for ilegível.
        parsed = parse_urn(existing.get("urn") or "")
        ws = (parsed["workspace"] if parsed else None) or await local_workspace()
        try:
            changes["urn"] = make_urn(existing["kind"], new_name, new_version, workspace=ws)
        except ValueError as e:
            raise HTTPException(422, f"URN inválido: {e}")

    changes["updated_at"] = _naive_utc_now()

    try:
        updated = await catalog_entries_repo.update(entry_id, changes)
    except Exception as e:
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(
                409,
                "Já existe outra publicação com a mesma combinação de tipo, nome e versão. "
                "Altere a versão (ex: 1.0.1) para diferenciar."
            )
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

    # Bloco crítico: I/O em vários repos + serialização JSON. Qualquer falha aqui
    # (constraint do banco, valor inesperado, repo helper) virava 500 puro com
    # 'Internal Server Error' em text/plain, sem registro do erro real. Agora
    # capturamos, logamos com traceback e devolvemos detail informativo ao
    # frontend (tipo da exceção + mensagem truncada), preservando rollback do client.
    try:
        # Insumos para pré-checks (disclosure tem PK = entry_id, helper especializado)
        disclosure = await get_disclosure(entry_id)
        owner = await users_repo.find_by_id(entry.get("owner_user_id"))
        # External metadata e recipe só consultados quando kind aplicável (otimização)
        external_meta = None
        recipe_data = None
        if entry.get("kind") == "external_platform":
            external_meta = await get_external_metadata(entry_id)
        elif entry.get("kind") == "recipe":
            recipe_data = await get_recipe(entry_id)
        report = run_prechecks(
            entry,
            disclosure=disclosure,
            owner=owner,
            external_metadata=external_meta,
            recipe=recipe_data,
        )

        import uuid
        sub_id = str(uuid.uuid4())
        submission = {
            "id": sub_id,
            "entry_id": entry_id,
            "submitted_by": user["id"],
            "snapshot": json.dumps(_entry_snapshot(entry), default=str),
            "precheck_report": json.dumps(report, default=str),
            "precheck_passed": report["passed"],
            "review_status": "pending",
            "review_notes": (data.notes or ""),
        }
        await catalog_submissions_repo.create(submission)
        await catalog_entries_repo.update(entry_id, {
            "status": "submitted",
            "updated_at": _naive_utc_now(),
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
    except HTTPException:
        # Erros intencionais (validação, autorização) — propagam sem mascarar
        raise
    except Exception as e:
        logger.exception(
            f"submit_entry: erro inesperado em entry_id={entry_id} user={user.get('id')}"
        )
        raise HTTPException(
            500,
            f"Erro ao submeter entry: {type(e).__name__}: {str(e)[:160]}",
        )


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

    now = _naive_utc_now()
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

    now = _naive_utc_now()
    updated = await catalog_entries_repo.update(entry_id, {
        "status": "published",
        "published_at": now,
        "updated_at": now,
    })
    await _audit("published", entry_id, user["id"], {"urn": entry.get("urn")})
    # PR5: ao publicar um pipeline, congela o snapshot do subgrafo (display + raiz
    # p/ execução). Best-effort — não derruba a publicação se o snapshot falhar.
    if entry.get("kind") == "pipeline":
        try:
            from app.catalog.pipeline_defs import snapshot_pipeline_def
            await snapshot_pipeline_def(entry)
        except Exception as e:
            logger.warning(f"snapshot_pipeline_def falhou para {entry_id}: {e}")
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

    now = _naive_utc_now()
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

    Default lista pendentes; query param permite ver decididas. Apenas Root acessa.
    Submissions órfãs (entry deletada) são **filtradas** via INNER JOIN — Root não
    pode decidir sobre algo que não existe. O FK CASCADE deveria limpar essas rows,
    mas o filtro é defesa em profundidade para legado histórico.
    """
    if not is_root(user):
        raise HTTPException(403, "Apenas Root pode ver a fila de revisão")
    items, total = await list_submissions_for_review(
        status=status or None, limit=limit, offset=offset,
    )
    return {"submissions": items, "total": total, "limit": limit, "offset": offset}


@router.post("/admin/cleanup-orphan-submissions")
async def cleanup_orphan_submissions_endpoint(user: dict = Depends(require_user)):
    """Limpa submissions cuja entry foi deletada (FK órfã).

    Em condições normais, o `ON DELETE CASCADE` do FK já remove submissions
    quando a entry é deletada. Este endpoint cobre o caso de legado histórico
    em que o CASCADE foi adicionado após a tabela ter sido criada (Postgres
    não retroativa em FKs existentes) ou de deletes feitos fora da API.

    Idempotente. Só Root pode rodar. Registra audit com o count.
    """
    if not is_root(user):
        raise HTTPException(403, "Apenas Root pode rodar cleanup de submissions")
    deleted = await cleanup_orphan_submissions()
    if deleted > 0:
        # cleanup é cross-entry; usamos sentinel "-" em entity_id porque o audit
        # exige um entity_id específico mas não há uma entry alvo.
        await _audit(
            "cleanup_orphan_submissions",
            "-",
            user["id"],
            {"deleted_count": deleted},
        )
    return {"deleted_count": deleted}


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


# ═════════════════════════════════════════════════════════════════
# Capability Disclosure — "etiqueta nutricional" R6.3
# ═════════════════════════════════════════════════════════════════


@router.get("/entries/{entry_id}/capability")
async def get_capability(entry_id: str, user: dict = Depends(require_user)):
    """Lê a capability disclosure de uma entry.

    Visível para qualquer usuário que possa ver a entry (transparência —
    consumer precisa saber o que invoca ANTES de invocar). 404 se entry
    invisível OU se ainda não há disclosure declarada.
    """
    entry_row = await catalog_entries_repo.find_by_id(entry_id)
    if not entry_row:
        raise HTTPException(404, "Entry não encontrada")
    entry = db_row_to_entry_dict(entry_row)
    if not can_user_see(user, entry):
        raise HTTPException(404, "Entry não encontrada")
    disclosure = await get_disclosure(entry_id)
    if not disclosure:
        raise HTTPException(404, "Capability disclosure ainda não declarada")
    return disclosure


@router.put("/entries/{entry_id}/capability")
async def put_capability(
    entry_id: str,
    data: CapabilityDisclosure,
    user: dict = Depends(require_user),
):
    """Upsert da capability disclosure. Apenas owner/root.

    Restringido a entries em status='draft': mudança de capabilities após
    aprovação altera a postura de risco e exige re-submissão. Para alterar
    em entry publicada, depreque + crie nova versão.
    """
    entry = await catalog_entries_repo.find_by_id(entry_id)
    if not entry:
        raise HTTPException(404, "Entry não encontrada")
    if not _can_mutate(user, entry):
        raise HTTPException(403, "Apenas owner ou root podem declarar disclosure")
    if entry.get("status") != "draft":
        raise HTTPException(
            409,
            f"Entry em status '{entry.get('status')}' não aceita edição de disclosure — "
            "depreque + nova versão para alterar capabilities",
        )

    try:
        payload = data.model_dump()
        result = await upsert_disclosure(entry_id, payload)
        await _audit("capability_declared", entry_id, user["id"], {
            "processes_pii": data.processes_pii,
            "calls_external_apis": data.calls_external_apis,
            "data_residency": data.data_residency,
        })
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"put_capability: erro inesperado em entry_id={entry_id} user={user.get('id')}"
        )
        raise HTTPException(
            500,
            f"Erro ao salvar Capability Disclosure: {type(e).__name__}: {str(e)[:160]}",
        )


@router.delete("/entries/{entry_id}/capability")
async def delete_capability(entry_id: str, user: dict = Depends(require_user)):
    """Remove capability disclosure. Apenas owner/root, apenas em draft.

    Uso raro — limpa declaração para começar do zero (ex: refatorou e quer
    re-declarar). Submissão exigirá nova declaração.
    """
    entry = await catalog_entries_repo.find_by_id(entry_id)
    if not entry:
        raise HTTPException(404, "Entry não encontrada")
    if not _can_mutate(user, entry):
        raise HTTPException(403, "Apenas owner ou root podem remover disclosure")
    if entry.get("status") != "draft":
        raise HTTPException(
            409,
            f"Entry em status '{entry.get('status')}' não permite remover disclosure",
        )
    ok = await delete_disclosure(entry_id)
    if not ok:
        raise HTTPException(404, "Disclosure não encontrada")
    await _audit("capability_removed", entry_id, user["id"], {})
    return {"message": "Capability disclosure removida", "entry_id": entry_id}


# ═════════════════════════════════════════════════════════════════
# External Platforms metadata (Onda 2 — R10)
# ═════════════════════════════════════════════════════════════════


@router.get("/entries/{entry_id}/external-metadata")
async def get_external(entry_id: str, user: dict = Depends(require_user)):
    """Lê metadata externo (vendor/contrato/custo) — visível para qualquer
    user que veja a entry. 404 se kind != external_platform ou ainda não
    declarado."""
    entry_row = await catalog_entries_repo.find_by_id(entry_id)
    if not entry_row:
        raise HTTPException(404, "Entry não encontrada")
    entry = db_row_to_entry_dict(entry_row)
    if not can_user_see(user, entry):
        raise HTTPException(404, "Entry não encontrada")
    if entry.get("kind") != "external_platform":
        raise HTTPException(
            404,
            "Metadata externa só se aplica a kind='external_platform'",
        )
    meta = await get_external_metadata(entry_id)
    if not meta:
        raise HTTPException(404, "Metadata externa ainda não declarada")
    return meta


@router.put("/entries/{entry_id}/external-metadata")
async def put_external(
    entry_id: str,
    data: ExternalPlatformMetadata,
    user: dict = Depends(require_user),
):
    """Upsert da metadata externa. Apenas owner/root, apenas em draft,
    apenas para kind='external_platform'.

    vendor é obrigatório na primeira escrita. Updates posteriores podem
    omitir vendor (mantém valor anterior)."""
    entry = await catalog_entries_repo.find_by_id(entry_id)
    if not entry:
        raise HTTPException(404, "Entry não encontrada")
    if entry.get("kind") != "external_platform":
        raise HTTPException(
            422,
            "Metadata externa só se aplica a kind='external_platform'",
        )
    if not _can_mutate(user, entry):
        raise HTTPException(403, "Apenas owner ou root podem declarar metadata externa")
    if entry.get("status") != "draft":
        raise HTTPException(
            409,
            f"Entry em status '{entry.get('status')}' não aceita edição de metadata — "
            "depreque + nova versão para alterar",
        )

    payload = data.model_dump(exclude_none=True)
    try:
        result = await upsert_external_metadata(entry_id, payload)
        await _audit("external_metadata_declared", entry_id, user["id"], {
            "vendor": data.vendor,
            "contract_status": data.contract_status,
        })
        return result
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        logger.exception(
            f"put_external: erro inesperado em entry_id={entry_id} user={user.get('id')}"
        )
        raise HTTPException(
            500,
            f"Erro ao salvar metadata externa: {type(e).__name__}: {str(e)[:160]}",
        )


# ═════════════════════════════════════════════════════════════════
# Inventário Regulatório (Onda 2 — R13)
# ═════════════════════════════════════════════════════════════════


def _parse_optional_bool(v: Optional[str]) -> Optional[bool]:
    """Converte query param string em bool tristate (true/false/None)."""
    if v is None or v == "":
        return None
    return str(v).lower() in ("true", "1", "yes")


def _inventory_filters_from_query(
    processes_pii: Optional[str],
    processes_financial: Optional[str],
    processes_health: Optional[str],
    calls_external_apis: Optional[str],
    accesses_internet: Optional[str],
    stores_input: Optional[str],
    writes_user_kb: Optional[str],
    reads_user_kb: Optional[str],
    trains_on_input: Optional[str],
) -> dict:
    """Constrói dict de flags a partir dos query params (strings tristate)."""
    return {
        "processes_pii": _parse_optional_bool(processes_pii),
        "processes_financial": _parse_optional_bool(processes_financial),
        "processes_health": _parse_optional_bool(processes_health),
        "calls_external_apis": _parse_optional_bool(calls_external_apis),
        "accesses_internet": _parse_optional_bool(accesses_internet),
        "stores_input": _parse_optional_bool(stores_input),
        "writes_user_kb": _parse_optional_bool(writes_user_kb),
        "reads_user_kb": _parse_optional_bool(reads_user_kb),
        "trains_on_input": _parse_optional_bool(trains_on_input),
    }


@router.get("/inventory")
async def get_inventory(
    processes_pii: Optional[str] = Query(None),
    processes_financial: Optional[str] = Query(None),
    processes_health: Optional[str] = Query(None),
    calls_external_apis: Optional[str] = Query(None),
    accesses_internet: Optional[str] = Query(None),
    stores_input: Optional[str] = Query(None),
    writes_user_kb: Optional[str] = Query(None),
    reads_user_kb: Optional[str] = Query(None),
    trains_on_input: Optional[str] = Query(None),
    residency: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_user),
):
    """Inventário regulatório cross-entries (Root only).

    Junta entry + capability_disclosure + external_metadata. Filtros por flag
    aceitos como query params tristate (true/false/omitido).

    Útil para comitê de privacidade/segurança ('quais entries processam PII?',
    'quais APIs externas chamamos?', 'qual exposição em USD/mês?').
    """
    if not is_root(user):
        raise HTTPException(403, "Inventário regulatório é acessível apenas para Root")

    flags = _inventory_filters_from_query(
        processes_pii, processes_financial, processes_health,
        calls_external_apis, accesses_internet, stores_input,
        writes_user_kb, reads_user_kb, trains_on_input,
    )
    rows, total = await list_inventory(
        flags=flags,
        residency=residency,
        kind=kind,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {"entries": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/inventory/export.csv")
async def export_inventory_csv(
    processes_pii: Optional[str] = Query(None),
    processes_financial: Optional[str] = Query(None),
    processes_health: Optional[str] = Query(None),
    calls_external_apis: Optional[str] = Query(None),
    accesses_internet: Optional[str] = Query(None),
    stores_input: Optional[str] = Query(None),
    writes_user_kb: Optional[str] = Query(None),
    reads_user_kb: Optional[str] = Query(None),
    trains_on_input: Optional[str] = Query(None),
    residency: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    user: dict = Depends(require_user),
):
    """Export do inventário regulatório como CSV. Mesmos filtros do GET.

    Limite mais alto (2000) — para audit externa, exportar tudo. Sem paginação
    no CSV: tudo cabe no download.
    """
    if not is_root(user):
        raise HTTPException(403, "Export do inventário é acessível apenas para Root")

    flags = _inventory_filters_from_query(
        processes_pii, processes_financial, processes_health,
        calls_external_apis, accesses_internet, stores_input,
        writes_user_kb, reads_user_kb, trains_on_input,
    )
    rows, _ = await list_inventory(
        flags=flags,
        residency=residency,
        kind=kind,
        status=status,
        limit=2000,
        offset=0,
    )

    import csv
    import io
    from datetime import datetime as _dt
    from fastapi.responses import StreamingResponse

    columns = [
        "id", "urn", "name", "kind", "status", "version", "domain",
        "owner_user_id", "steward_team", "visibility",
        "processes_pii", "processes_financial", "processes_health",
        "calls_external_apis", "accesses_internet", "stores_input",
        "writes_user_kb", "reads_user_kb", "trains_on_input",
        "data_residency", "external_apis_list", "storage_retention_days",
        "vendor", "monthly_cost_usd", "contract_status", "contract_renewal_date",
        "created_at", "published_at",
    ]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        # Normaliza listas/datetimes para CSV
        row = dict(r)
        if isinstance(row.get("external_apis_list"), list):
            row["external_apis_list"] = "; ".join(row["external_apis_list"])
        for k in ("created_at", "published_at", "contract_renewal_date"):
            if row.get(k) is not None and not isinstance(row[k], str):
                row[k] = str(row[k])
        writer.writerow(row)

    buf.seek(0)
    timestamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    filename = f"maestro-catalog-inventory-{timestamp}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/inventory/{entry_id}/details")
async def get_inventory_entry_details(
    entry_id: str,
    user: dict = Depends(require_user),
):
    """Dossiê regulatório completo de UMA entry — payload do drawer lateral.

    Agrega em 1 chamada (sem N+1 no client): entry + disclosure + external +
    recipe + 5 últimas submissions + risk score + compliance matchers (LGPD,
    GDPR, HIPAA, Marco Civil) + alertas automáticos + nomes resolvidos de owner
    e dos reviewers. Root only.

    Retorna 404 se entry não existe — sem distinção entre "não existe" e "sem
    permissão" porque a tela inteira já é restrita a Root.
    """
    if not is_root(user):
        raise HTTPException(403, "Inventário regulatório é acessível apenas para Root")

    from app.catalog.risk_score import (
        compute_alerts,
        compute_compliance,
        compute_risk_score,
    )

    entry_row = await catalog_entries_repo.find_by_id(entry_id)
    if not entry_row:
        raise HTTPException(404, "Entry não encontrada")
    entry = db_row_to_entry_dict(entry_row)

    disclosure = await get_disclosure(entry_id)
    external_meta = None
    recipe_data = None
    if entry.get("kind") == "external_platform":
        external_meta = await get_external_metadata(entry_id)
    elif entry.get("kind") == "recipe":
        recipe_data = await get_recipe(entry_id)

    # Trilha de aprovação: 5 últimas submissões (mais recentes primeiro).
    # Find_all retorna por created_at DESC; o subset cobre histórico recente
    # de changes_requested → re-submitted → approved sem pesar o payload.
    submissions = await catalog_submissions_repo.find_all(entry_id=entry_id, limit=5)

    # Resolve nomes de users: owner + submitters + reviewers (deduplicado).
    user_ids = {entry.get("owner_user_id")}
    for s in submissions:
        if s.get("submitted_by"):
            user_ids.add(s["submitted_by"])
        if s.get("reviewed_by"):
            user_ids.add(s["reviewed_by"])
    user_ids.discard(None)
    users_map: dict[str, dict] = {}
    for uid in user_ids:
        try:
            u = await users_repo.find_by_id(uid)
            if u:
                users_map[uid] = {"id": uid, "email": u.get("email"), "role": u.get("role")}
        except Exception:
            pass  # users_repo.find_by_id pode falhar pra IDs órfãos; segue

    def _user_or_id(uid):
        if not uid:
            return None
        return users_map.get(uid) or {"id": uid, "email": None, "role": None}

    # Enriquece submissões com nomes resolvidos
    submissions_view = []
    for s in submissions:
        s_view = dict(s)
        s_view["submitter"] = _user_or_id(s.get("submitted_by"))
        s_view["reviewer"] = _user_or_id(s.get("reviewed_by")) if s.get("reviewed_by") else None
        # precheck_report vem como TEXT JSON — parse pra dict (caller pode renderizar)
        if isinstance(s_view.get("precheck_report"), str):
            try:
                s_view["precheck_report"] = json.loads(s_view["precheck_report"])
            except Exception:
                pass
        submissions_view.append(s_view)

    # Risk score / compliance / alerts — helpers puros, determinísticos.
    # Passamos trust_last_invoked_at no entry para o alerta stale_entry funcionar.
    entry_for_alerts = dict(entry)
    entry_for_alerts["last_invoked_at"] = entry.get("trust_last_invoked_at")
    risk = compute_risk_score(disclosure)
    compliance = compute_compliance(disclosure)
    alerts = compute_alerts(entry_for_alerts, disclosure, external_metadata=external_meta)

    # Métricas operacionais — vêm de catalog_entries.trust_* (atualizado pelo
    # engine após cada invocação real). Sem cálculo de janela aqui — payload
    # leve. Drill-down vai pra /catalog/cost se quiser ver série temporal.
    metrics = {
        "invocation_count": entry.get("trust_invocation_count") or 0,
        "avg_cost_usd": entry.get("trust_avg_cost_usd") or 0,
        "last_invoked_at": entry.get("trust_last_invoked_at"),
        "success_rate": entry.get("trust_score"),
    }

    return {
        "entry": entry,
        "owner": _user_or_id(entry.get("owner_user_id")),
        "disclosure": disclosure,
        "external_metadata": external_meta,
        "recipe": recipe_data,
        "submissions": submissions_view,
        "risk": risk,
        "compliance": compliance,
        "alerts": alerts,
        "metrics": metrics,
    }


# ═════════════════════════════════════════════════════════════════
# Stewardship Dashboard (Onda 2 — R11)
# ═════════════════════════════════════════════════════════════════


@router.get("/stewardship")
async def get_stewardship(
    steward_team: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
    user: dict = Depends(require_user),
):
    """Lista entries enriquecidas com flags de saúde para gestão de stewardship.

    Detecta: is_orphan (owner inativo/deletado), is_stale (published sem uso
    há 30+ dias), has_low_reliability (trust < 0.5). Agrega por steward_team.

    Visibilidade (Onda 3 — aberto a stewards de área):
    - Root: vê tudo.
    - Non-root: vê apenas entries cujo steward_team está em user.domains.
      Sem domains = vê nada (filtro retorna 0).
    """
    from app.catalog.queries import _user_domains
    restrict = None if is_root(user) else _user_domains(user)

    entries, by_team = await list_stewardship(
        steward_team=steward_team,
        restrict_to_teams=restrict,
        limit=limit,
    )
    return {
        "entries": entries,
        "by_team": by_team,
        "total": len(entries),
        "viewer_is_root": is_root(user),
        "viewer_domains": _user_domains(user),
    }


@router.post("/entries/{entry_id}/reassign")
async def reassign_entry(
    entry_id: str,
    data: ReassignPayload,
    user: dict = Depends(require_user),
):
    """Realoca owner e/ou steward_team de uma entry. Apenas Root.

    Usado quando publisher original sai da empresa ou área reorganiza
    responsabilidades. Audita action 'stewardship_reassigned' com valores
    antigos e novos.
    """
    if not is_root(user):
        raise HTTPException(403, "Apenas Root pode realocar entries")
    if not data.has_any_change():
        raise HTTPException(422, "Informe ao menos new_owner_user_id ou new_steward_team")

    entry = await catalog_entries_repo.find_by_id(entry_id)
    if not entry:
        raise HTTPException(404, "Entry não encontrada")

    updates: dict = {}
    audit_details = {}
    if data.new_owner_user_id is not None:
        # Valida que o novo owner existe
        target = await users_repo.find_by_id(data.new_owner_user_id)
        if not target:
            raise HTTPException(422, f"Usuário '{data.new_owner_user_id}' não encontrado")
        updates["owner_user_id"] = data.new_owner_user_id
        audit_details["owner"] = {
            "from": entry.get("owner_user_id"),
            "to": data.new_owner_user_id,
        }
    if data.new_steward_team is not None:
        # String vazia limpa o campo
        updates["steward_team"] = data.new_steward_team or None
        audit_details["steward_team"] = {
            "from": entry.get("steward_team"),
            "to": data.new_steward_team or None,
        }

    updates["updated_at"] = _naive_utc_now()
    updated = await catalog_entries_repo.update(entry_id, updates)
    await _audit("stewardship_reassigned", entry_id, user["id"], audit_details)
    return db_row_to_entry_dict(updated) if updated else {"message": "realocada"}


# ═════════════════════════════════════════════════════════════════
# Cost & Consumption (Onda 3 — R4.3)
# ═════════════════════════════════════════════════════════════════


@router.post("/entries/{entry_id}/invocation-cost", status_code=201)
async def record_cost(
    entry_id: str,
    data: InvocationCostRecord,
    user: dict = Depends(require_user),
):
    """Registra custo de uma invocação. Insert-only.

    Quem chama: integrações externas (Zapier/n8n), e — no futuro — o engine
    via auto-wire (Onda 4). Visibilidade: qualquer user que vê a entry pode
    registrar (caso comum: consumer registra seu próprio custo).

    Default de consumer_user_id = user.id. Para casos onde sistema registra
    em nome de outro user, payload pode override (mas auditável).
    """
    entry_row = await catalog_entries_repo.find_by_id(entry_id)
    if not entry_row:
        raise HTTPException(404, "Entry não encontrada")
    entry = db_row_to_entry_dict(entry_row)
    if not can_user_see(user, entry):
        raise HTTPException(404, "Entry não encontrada")

    consumer_id = data.consumer_user_id or user["id"]
    result = await record_invocation_cost(
        entry_id,
        consumer_user_id=consumer_id,
        consumer_department=data.consumer_department,
        interaction_id=data.interaction_id,
        cost_usd=data.cost_usd,
        tokens_used=data.tokens_used,
        latency_ms=data.latency_ms,
    )
    # Audit best-effort; cost records são insert-only e abundantes — não
    # auditamos cada um para evitar inflar audit_log. Caso de uso futuro
    # (anomaly detection) pode auditar limites/picos.
    return result


@router.get("/cost")
async def get_cost(
    group_by: str = Query("entry"),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    entry_id: Optional[str] = Query(None),
    consumer_user_id: Optional[str] = Query(None),
    consumer_department: Optional[str] = Query(None),
    scope: str = Query("auto"),
    limit: int = Query(200, ge=1, le=2000),
    user: dict = Depends(require_user),
):
    """Agrega catalog_costs por grupo.

    Scope:
    - 'mine': força consumer_user_id = user atual (qualquer user)
    - 'all':  sem restrição (apenas Root)
    - 'auto': Root vê tudo; demais vêem só próprio consumo

    group_by: 'entry' | 'consumer' | 'department' | 'day'
    """
    effective_scope = scope
    if effective_scope == "auto":
        effective_scope = "all" if is_root(user) else "mine"

    if effective_scope == "all" and not is_root(user):
        raise HTTPException(403, "scope='all' requer Root")

    # Quando scope=mine, força filtro pelo user atual mesmo se vier outro
    if effective_scope == "mine":
        consumer_user_id = user["id"]

    try:
        rows, totals = await aggregate_costs(
            group_by=group_by,
            since=since,
            until=until,
            entry_id=entry_id,
            consumer_user_id=consumer_user_id,
            consumer_department=consumer_department,
            limit=limit,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))

    return {
        "group_by": group_by,
        "scope": effective_scope,
        "rows": rows,
        "totals": totals,
        "since": since,
        "until": until,
    }


@router.get("/cost/export.csv")
async def export_cost_csv(
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    entry_id: Optional[str] = Query(None),
    consumer_user_id: Optional[str] = Query(None),
    consumer_department: Optional[str] = Query(None),
    scope: str = Query("auto"),
    user: dict = Depends(require_user),
):
    """Export raw das rows de catalog_costs. Mesmos filtros + scope que GET /cost."""
    effective_scope = scope
    if effective_scope == "auto":
        effective_scope = "all" if is_root(user) else "mine"
    if effective_scope == "all" and not is_root(user):
        raise HTTPException(403, "scope='all' requer Root")
    if effective_scope == "mine":
        consumer_user_id = user["id"]

    rows = await list_costs_raw(
        since=since, until=until,
        entry_id=entry_id,
        consumer_user_id=consumer_user_id,
        consumer_department=consumer_department,
        limit=5000,
    )

    import csv
    import io
    from datetime import datetime as _dt
    from fastapi.responses import StreamingResponse

    columns = [
        "id", "entry_id", "consumer_user_id", "consumer_department",
        "interaction_id", "cost_usd", "tokens_used", "latency_ms", "invoked_at",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        row = dict(r)
        if row.get("invoked_at") is not None and not isinstance(row["invoked_at"], str):
            row["invoked_at"] = str(row["invoked_at"])
        writer.writerow(row)

    buf.seek(0)
    timestamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    filename = f"maestro-catalog-costs-{timestamp}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/cost/anomalies")
async def get_cost_anomalies(
    scope: str = Query("auto"),
    consumer_department: Optional[str] = Query(None),
    user: dict = Depends(require_user),
):
    """Detecta anomalias no custo do dia atual.

    Tipos detectados (PR #71):
    - **pico_relativo**: custo hoje > 3× média 7d (ignora se baseline < $1)
    - **limite_global**: custo hoje > $100 absoluto

    Scope (alinhado com GET /cost):
    - 'mine': força consumer_user_id = user atual
    - 'all':  sem restrição (apenas Root)
    - 'auto': Root → all; demais → mine

    Audit `cost_anomaly_detected` é registrado quando count > 0.
    """
    from app.catalog.anomalies import detect_anomalies

    effective_scope = scope
    if effective_scope == "auto":
        effective_scope = "all" if is_root(user) else "mine"

    if effective_scope == "all" and not is_root(user):
        raise HTTPException(403, "scope='all' requer Root")

    consumer_user_id = user["id"] if effective_scope == "mine" else None

    result = await detect_anomalies(
        consumer_user_id=consumer_user_id,
        consumer_department=consumer_department,
    )

    if result["anomalies"]:
        await _audit("cost_anomaly_detected", entry_id="", actor_id=user["id"], details={
            "scope": effective_scope,
            "anomaly_count": len(result["anomalies"]),
            "anomaly_types": [a["type"] for a in result["anomalies"]],
            "today_usd": result["today_usd"],
        })

    return result


# ═════════════════════════════════════════════════════════════════
# Recipes (Onda 3 — R8.1 básico)
# ═════════════════════════════════════════════════════════════════


@router.get("/entries/{entry_id}/recipe")
async def get_recipe_endpoint(entry_id: str, user: dict = Depends(require_user)):
    """Lê o manifest do recipe. Transparente para quem vê a entry."""
    entry_row = await catalog_entries_repo.find_by_id(entry_id)
    if not entry_row:
        raise HTTPException(404, "Entry não encontrada")
    entry = db_row_to_entry_dict(entry_row)
    if not can_user_see(user, entry):
        raise HTTPException(404, "Entry não encontrada")
    if entry.get("kind") != "recipe":
        raise HTTPException(
            404,
            "Recipe manifest só se aplica a kind='recipe'",
        )
    recipe = await get_recipe(entry_id)
    if not recipe:
        raise HTTPException(404, "Recipe ainda não declarado (sem steps)")
    return recipe


@router.put("/entries/{entry_id}/recipe")
async def put_recipe(
    entry_id: str,
    data: RecipeDefinition,
    user: dict = Depends(require_user),
):
    """Upsert do manifest do recipe (lista de steps). Owner/root, draft,
    kind=recipe. Valida que cada target_entry_id existe e não há ciclo
    trivial (target == self)."""
    entry = await catalog_entries_repo.find_by_id(entry_id)
    if not entry:
        raise HTTPException(404, "Entry não encontrada")
    if entry.get("kind") != "recipe":
        raise HTTPException(422, "Recipe manifest só se aplica a kind='recipe'")
    if not _can_mutate(user, entry):
        raise HTTPException(403, "Apenas owner ou root podem editar recipe")
    if entry.get("status") != "draft":
        raise HTTPException(
            409,
            f"Entry em status '{entry.get('status')}' não aceita edição de recipe — "
            "depreque + nova versão para alterar composição",
        )

    steps_payload = [s.model_dump() for s in data.steps]
    try:
        result = await upsert_recipe(entry_id, steps_payload)
    except ValueError as e:
        raise HTTPException(422, str(e))
    await _audit("recipe_defined", entry_id, user["id"], {
        "step_count": len(steps_payload),
        "target_entry_ids": [s["target_entry_id"] for s in steps_payload],
    })
    return result


@router.delete("/entries/{entry_id}/recipe")
async def delete_recipe_endpoint(entry_id: str, user: dict = Depends(require_user)):
    """Limpa o manifest do recipe. Owner/root, draft."""
    entry = await catalog_entries_repo.find_by_id(entry_id)
    if not entry:
        raise HTTPException(404, "Entry não encontrada")
    if not _can_mutate(user, entry):
        raise HTTPException(403, "Apenas owner ou root podem limpar recipe")
    if entry.get("status") != "draft":
        raise HTTPException(
            409,
            f"Entry em status '{entry.get('status')}' não permite limpar recipe",
        )
    ok = await delete_recipe(entry_id)
    if not ok:
        raise HTTPException(404, "Recipe não encontrado")
    await _audit("recipe_cleared", entry_id, user["id"], {})
    return {"message": "Recipe limpo", "entry_id": entry_id}


# ═════════════════════════════════════════════════════════════════
# Bulk decide (Onda 2) — Root processa várias submissions de uma vez
# ═════════════════════════════════════════════════════════════════


@router.post("/submissions/bulk-decide")
async def bulk_decide(
    data: BulkDecisionPayload,
    user: dict = Depends(require_user),
):
    """Aplica a mesma decisão a múltiplas submissions. Apenas Root.

    Falhas individuais não interrompem as demais — response detalha
    sucessos e erros para o front exibir resumo.
    """
    if not is_root(user):
        raise HTTPException(403, "Apenas Root pode decidir submissões")

    new_entry_status = "approved" if data.decision == "approved" else "draft"
    now = _naive_utc_now()

    succeeded: list[str] = []
    failed: list[dict] = []

    for sub_id in data.submission_ids:
        try:
            sub = await catalog_submissions_repo.find_by_id(sub_id)
            if not sub:
                failed.append({"submission_id": sub_id, "reason": "não encontrada"})
                continue
            if not can_transition_review(sub.get("review_status"), data.decision):
                failed.append({
                    "submission_id": sub_id,
                    "reason": f"review_status='{sub.get('review_status')}' não admite '{data.decision}'",
                })
                continue
            entry = await catalog_entries_repo.find_by_id(sub["entry_id"])
            if not entry:
                failed.append({"submission_id": sub_id, "reason": "entry vinculada não existe"})
                continue
            if not can_transition_entry(entry.get("status"), new_entry_status):
                failed.append({
                    "submission_id": sub_id,
                    "reason": f"entry em '{entry.get('status')}' não pode ir para '{new_entry_status}'",
                })
                continue

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
                {
                    "submission_id": sub_id,
                    "new_entry_status": new_entry_status,
                    "notes": data.notes or "",
                    "bulk": True,
                },
            )
            succeeded.append(sub_id)
        except Exception as e:
            logger.warning(f"bulk_decide falhou para {sub_id}: {e}")
            failed.append({"submission_id": sub_id, "reason": str(e)})

    return {
        "decision": data.decision,
        "total": len(data.submission_ids),
        "succeeded_count": len(succeeded),
        "failed_count": len(failed),
        "succeeded": succeeded,
        "failed": failed,
    }


# ═════════════════════════════════════════════════════════════════
# Recipe Executions (Onda 4) — execução real de recipes publicados
# ═════════════════════════════════════════════════════════════════


@router.post("/entries/{entry_id}/execute", status_code=202)
async def execute_recipe_endpoint(
    entry_id: str,
    data: RecipeExecutionRequest,
    user: dict = Depends(require_user),
):
    """Dispara execução do recipe. Modo async: cria row em status='running',
    lança background task, retorna 202 + execution_id. Cliente faz polling
    em GET /executions/{id} até status virar completed|partial|failed.

    Pré-condições:
    - Entry existe e é visível para o user.
    - Entry é kind='recipe' e status='published'.
    - Manifest existe (sem steps → 422).
    """
    entry_row = await catalog_entries_repo.find_by_id(entry_id)
    if not entry_row:
        raise HTTPException(404, "Entry não encontrada")
    entry = db_row_to_entry_dict(entry_row)
    if not can_user_see(user, entry):
        raise HTTPException(404, "Entry não encontrada")
    if entry.get("kind") != "recipe":
        raise HTTPException(422, "Apenas entries kind='recipe' são executáveis")
    if entry.get("status") != "published":
        raise HTTPException(
            409,
            f"Recipe em status '{entry.get('status')}' não é executável — "
            "só recipes published podem rodar",
        )
    recipe = await get_recipe(entry_id)
    if not recipe or not recipe.get("steps"):
        raise HTTPException(422, "Recipe sem steps — declare o manifest antes de executar")

    execution = await create_execution(
        recipe_entry_id=entry_id,
        consumer_user_id=user["id"],
        input_text=data.input,
    )

    # Background task — não bloqueia o endpoint
    from app.catalog.executor import execute_recipe
    asyncio.create_task(execute_recipe(
        execution_id=execution["id"],
        recipe_entry_id=entry_id,
        steps=recipe["steps"],
        consumer_user=user,
        user_input=data.input,
    ))

    await _audit("recipe_execution_started", entry_id, user["id"], {
        "execution_id": execution["id"],
        "input_length": len(data.input),
        "step_count": len(recipe["steps"]),
    })

    return {
        "execution_id": execution["id"],
        "recipe_entry_id": entry_id,
        "status": "running",
        "step_count": len(recipe["steps"]),
        "started_at": execution.get("started_at").isoformat()
            if execution.get("started_at") and hasattr(execution["started_at"], "isoformat")
            else execution.get("started_at"),
    }


@router.post("/entries/{entry_id}/sandbox", status_code=202)
async def sandbox_recipe_endpoint(
    entry_id: str,
    data: RecipeExecutionRequest,
    user: dict = Depends(require_user),
):
    """Dispara execução de SANDBOX do recipe. Diferenças vs /execute:

    - **Auth**: apenas owner do recipe ou Root (não 'qualquer um que vê').
    - **Status**: aceita qualquer status (incl. draft) — sandbox é
      pra testar ANTES de publicar.
    - **Cost**: não grava em catalog_costs (sandbox é free tier de dev).
    - **LLM**: real (testa qualidade/latência de verdade).

    Modal de polling no UI mostra badge 'SANDBOX' para distinguir das
    runs de produção.
    """
    entry_row = await catalog_entries_repo.find_by_id(entry_id)
    if not entry_row:
        raise HTTPException(404, "Entry não encontrada")
    entry = db_row_to_entry_dict(entry_row)
    if not _can_mutate(user, entry):
        raise HTTPException(403, "Apenas owner ou Root podem rodar sandbox")
    if entry.get("kind") != "recipe":
        raise HTTPException(422, "Sandbox só se aplica a kind='recipe'")
    recipe = await get_recipe(entry_id)
    if not recipe or not recipe.get("steps"):
        raise HTTPException(422, "Recipe sem steps — declare o manifest antes de testar")

    execution = await create_execution(
        recipe_entry_id=entry_id,
        consumer_user_id=user["id"],
        input_text=data.input,
        is_sandbox=True,
    )

    from app.catalog.executor import execute_recipe
    asyncio.create_task(execute_recipe(
        execution_id=execution["id"],
        recipe_entry_id=entry_id,
        steps=recipe["steps"],
        consumer_user=user,
        user_input=data.input,
        is_sandbox=True,
    ))

    await _audit("recipe_sandbox_started", entry_id, user["id"], {
        "execution_id": execution["id"],
        "input_length": len(data.input),
        "step_count": len(recipe["steps"]),
        "entry_status": entry.get("status"),
    })

    return {
        "execution_id": execution["id"],
        "recipe_entry_id": entry_id,
        "status": "running",
        "step_count": len(recipe["steps"]),
        "is_sandbox": True,
        "started_at": execution.get("started_at").isoformat()
            if execution.get("started_at") and hasattr(execution["started_at"], "isoformat")
            else execution.get("started_at"),
    }


def _started_iso(execution: dict):
    sa = execution.get("started_at")
    return sa.isoformat() if sa is not None and hasattr(sa, "isoformat") else sa


@router.get("/entries/{entry_id}/pipeline-def")
async def get_pipeline_def_endpoint(entry_id: str, user: dict = Depends(require_user)):
    """PR5 — snapshot do GRAFO do pipeline (nodes/edges/root_agent_id). Alimenta a
    UI (PR6, mini-fluxograma read-only) e auditoria. 404 se ainda não gerado
    (gera-se na publicação)."""
    entry_row = await catalog_entries_repo.find_by_id(entry_id)
    if not entry_row:
        raise HTTPException(404, "Entry não encontrada")
    entry = db_row_to_entry_dict(entry_row)
    if not can_user_see(user, entry):
        raise HTTPException(404, "Entry não encontrada")
    from app.catalog.pipeline_defs import get_pipeline_def
    d = await get_pipeline_def(entry_id)
    if not d:
        raise HTTPException(404, "Snapshot do pipeline ainda não gerado — publique o pipeline para gerá-lo")
    return d


@router.post("/entries/{entry_id}/execute-pipeline", status_code=202)
async def execute_pipeline_endpoint(
    entry_id: str, data: RecipeExecutionRequest, user: dict = Depends(require_user)
):
    """PR5 — executa um pipeline publicado (kind='pipeline') reusando o motor do
    mesh (execute_pipeline) a partir da raiz do snapshot. Mesmo contrato dos
    recipes: 202 + polling em GET /executions/{id}."""
    entry_row = await catalog_entries_repo.find_by_id(entry_id)
    if not entry_row:
        raise HTTPException(404, "Entry não encontrada")
    entry = db_row_to_entry_dict(entry_row)
    if not can_user_see(user, entry):
        raise HTTPException(404, "Entry não encontrada")
    if entry.get("kind") != "pipeline":
        raise HTTPException(422, "Apenas entries kind='pipeline' são executáveis aqui (recipes usam /execute)")
    if entry.get("federated"):
        # Capability remota (PR8c): NÃO executável localmente (sem snapshot local).
        # Guarda explícita — não confiar só no resolver retornar (None, set()).
        raise HTTPException(422, "Entry federada (capability remota) — use POST /api/v1/federation/remote/{id}/invoke")
    if entry.get("status") != "published":
        raise HTTPException(
            409,
            f"Pipeline em status '{entry.get('status')}' não é executável — só pipelines published rodam",
        )
    from app.catalog.pipeline_defs import resolve_pipeline_exec
    root, allowed = await resolve_pipeline_exec(entry)
    if not root:
        raise HTTPException(422, "Pipeline sem agentes/raiz resolvível — nada a executar")

    execution = await create_execution(
        recipe_entry_id=entry_id,
        consumer_user_id=user["id"],
        input_text=data.input,
    )
    from app.catalog.executor import execute_pipeline_entry
    asyncio.create_task(execute_pipeline_entry(
        execution_id=execution["id"],
        pipeline_entry_id=entry_id,
        root_agent_id=root,
        consumer_user=user,
        user_input=data.input,
        allowed_agent_ids=allowed,  # SELA a execução ao subgrafo do snapshot (PR-A1)
    ))
    await _audit("pipeline_execution_started", entry_id, user["id"], {
        "execution_id": execution["id"],
        "root_agent_id": root,
        "member_count": len(allowed),
        "input_length": len(data.input),
    })
    return {
        "execution_id": execution["id"],
        "entry_id": entry_id,
        "status": "running",
        "started_at": _started_iso(execution),
    }


@router.post("/entries/{entry_id}/sandbox-pipeline", status_code=202)
async def sandbox_pipeline_endpoint(
    entry_id: str, data: RecipeExecutionRequest, user: dict = Depends(require_user)
):
    """PR5 — sandbox de pipeline. Como /execute-pipeline, mas: só owner/root,
    aceita qualquer status (testar antes de publicar) e NÃO grava em catalog_costs."""
    entry_row = await catalog_entries_repo.find_by_id(entry_id)
    if not entry_row:
        raise HTTPException(404, "Entry não encontrada")
    entry = db_row_to_entry_dict(entry_row)
    if not _can_mutate(user, entry):
        raise HTTPException(403, "Apenas owner ou Root podem rodar sandbox")
    if entry.get("kind") != "pipeline":
        raise HTTPException(422, "Sandbox de pipeline só se aplica a kind='pipeline'")
    if entry.get("federated"):
        raise HTTPException(422, "Entry federada (capability remota) — não roda sandbox local")
    from app.catalog.pipeline_defs import resolve_pipeline_exec
    root, allowed = await resolve_pipeline_exec(entry)
    if not root:
        raise HTTPException(422, "Pipeline sem agentes/raiz resolvível — nada a executar")

    execution = await create_execution(
        recipe_entry_id=entry_id,
        consumer_user_id=user["id"],
        input_text=data.input,
        is_sandbox=True,
    )
    from app.catalog.executor import execute_pipeline_entry
    asyncio.create_task(execute_pipeline_entry(
        execution_id=execution["id"],
        pipeline_entry_id=entry_id,
        root_agent_id=root,
        consumer_user=user,
        user_input=data.input,
        is_sandbox=True,
        allowed_agent_ids=allowed,  # SELA a execução ao subgrafo do snapshot (PR-A1)
    ))
    await _audit("pipeline_sandbox_started", entry_id, user["id"], {
        "execution_id": execution["id"],
        "root_agent_id": root,
        "member_count": len(allowed),
        "entry_status": entry.get("status"),
    })
    return {
        "execution_id": execution["id"],
        "entry_id": entry_id,
        "status": "running",
        "is_sandbox": True,
        "started_at": _started_iso(execution),
    }


@router.get("/executions/{execution_id}")
async def get_execution_endpoint(
    execution_id: str,
    user: dict = Depends(require_user),
):
    """Estado atual da execução (para polling). 404 se não existe ou
    se o user não pode ver. Quem vê: root | consumer (quem rodou) |
    owner do recipe."""
    execution = await get_execution(execution_id, enrich=True)
    if not execution:
        raise HTTPException(404, "Execução não encontrada")
    recipe_row = await catalog_entries_repo.find_by_id(execution["recipe_entry_id"])
    recipe_entry = db_row_to_entry_dict(recipe_row) if recipe_row else None
    if not can_user_see_execution(user, execution, recipe_entry):
        raise HTTPException(404, "Execução não encontrada")
    return execution


@router.get("/entries/{entry_id}/executions")
async def list_executions_endpoint(
    entry_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_user),
):
    """Histórico paginado de execuções de um recipe. Visível para quem
    pode ver a entry (mesma regra de listagem do catálogo)."""
    entry_row = await catalog_entries_repo.find_by_id(entry_id)
    if not entry_row:
        raise HTTPException(404, "Entry não encontrada")
    entry = db_row_to_entry_dict(entry_row)
    if not can_user_see(user, entry):
        raise HTTPException(404, "Entry não encontrada")
    if entry.get("kind") != "recipe":
        raise HTTPException(422, "Apenas recipes têm histórico de execução")
    items = await list_executions_for_entry(entry_id, limit=limit, offset=offset)
    return {
        "items": items,
        "limit": limit,
        "offset": offset,
        "has_more": len(items) == limit,
    }
