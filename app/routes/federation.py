"""Rotas de federação A2A — provider/ingress (PR8b).

PR8b1: descoberta READ-ONLY via manifesto well-known. O endpoint de invoke
assinado (`POST /api/v1/federation/invoke`) é PR8b2.

Gate por `federation_enabled()` (default OFF → 404, instância invisível). Quando
ligada, o manifesto só expõe capabilities published+company (allowlist de kinds)
— ver `is_federation_exposable`. Sem auth no PR8b1 (descoberta de capabilities já
company-visíveis); peer-gating do manifesto é endurecimento de PR8c.
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.a2a.protocol import Envelope
from app.catalog import federation as fed
from app.catalog import federation_egress as egress
from app.catalog import federation_peers as peers
from app.catalog.federation import build_manifest
from app.catalog.queries import create_execution, db_row_to_entry_dict, get_execution, is_root
from app.core.auth import require_user
from app.core.database import (
    _get_pool,
    audit_repo,
    catalog_entries_repo,
    federation_peers_repo,
    settings_store,
)
from app.core.datetime_utils import naive_utc_now
from app.core.federation_identity import (
    ENABLED_SETTING_KEY,
    WORKSPACE_SETTING_KEY,
    federation_enabled,
    is_valid_workspace,
    local_workspace,
    secret_key_present,
)
from app.core.ssrf import SSRFError

_DEV_ALLOW_HTTP_KEY = "federation.dev_allow_http"


def _truthy(raw: str) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def _peer_failure_cause(e: Exception) -> str:
    """Causa curta e segura p/ o corpo do 502 (achado A2A-2). ValueError já
    carrega a mensagem montada no egress (inclui o detail do peer, capado);
    erros httpx usam o nome da classe porque o str() pode vir vazio
    (ex.: ConnectError sem texto)."""
    if isinstance(e, ValueError):
        return str(e)[:400]
    text = str(e)
    return f"{type(e).__name__}: {text[:300]}" if text else type(e).__name__


logger = logging.getLogger(__name__)

router = APIRouter(tags=["federation"])

# Defesas DoS aplicáveis no ingress. NOTA: o budget (tokens/usd) do envelope NÃO é
# enforçado pelo engine hoje (execute_pipeline não recebe budget) — é ADVISORY até
# o engine ganhar gate de budget. Aqui limitamos corpo, input e tempo de execução.
_MAX_BODY_BYTES = 262_144   # 256 KB — cap pré-parse (proxy/uvicorn é o limite duro)
_MAX_INPUT_CHARS = 100_000
_EXEC_TIMEOUT_S = 120
# Custo/latência vêm da RESPOSTA do peer (não-confiáveis) — clampados antes de
# gravar para um peer malicioso não inflar chargeback / poluir métricas locais.
_MAX_REMOTE_COST_USD = 1000.0
_MAX_REMOTE_LATENCY_MS = 3_600_000.0


@router.get("/.well-known/maestro-federation.json")
async def federation_manifest():
    """Manifesto de descoberta desta instância: capabilities published+company,
    com URN federada, resumo de disclosure e fingerprint (pipelines). 404 quando
    a federação está desligada (default — instância não anuncia nada)."""
    if not await federation_enabled():
        # 404 SEM detalhe custom: desligada deve ser indistinguível de inexistente
        # (instância "invisível"). Um detalhe próprio vazaria que a rota existe.
        raise HTTPException(404)
    return await build_manifest()


# ── Ingress assinado (PR8b3) — a ponta de execução de rede ───────────────────


@router.post("/api/v1/federation/invoke")
async def federation_invoke(request: Request):
    """Invoke ASSINADO inbound. Body JSON: `{"envelope": {...}, "signature": "<hex>"}`.
    Verifica o HMAC do peer + janela de replay, resolve o alvo (só published+company),
    executa SELADO ao snapshot e devolve o resultado. Custo/trust sob a identidade do
    peer (consumer 'federation:<ws>'). Sem auth de USUÁRIO — a autenticação é o HMAC."""
    if not await federation_enabled():
        raise HTTPException(404)  # invisível quando desligada
    if not secret_key_present():
        # Fail-closed: sem master key, crypto cai no fallback inseguro → HMAC forjável.
        raise HTTPException(503, "Federação indisponível (MAESTRO_SECRET_KEY ausente)")

    # 0) cap de corpo ANTES do parse (DoS pré-auth — json.loads de corpo gigante é o custo)
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise HTTPException(413, "Corpo excede o limite")
    try:
        payload = json.loads(raw)
        envelope_dict = payload["envelope"]
        signature = payload["signature"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Body inválido (esperado {envelope, signature})")

    # 1) reconstrói o envelope (from_dict é TOTAL → ValueError vira 400, nunca 500)
    try:
        env = Envelope.from_dict(envelope_dict)
    except (ValueError, TypeError):
        raise HTTPException(400, "Envelope malformado")
    if not env.origin_workspace or not env.target_skill_urn or not isinstance(signature, str) or not signature:
        raise HTTPException(400, "Envelope incompleto (origin_workspace/target_skill_urn/signature)")

    # 2) AUTENTICA (HMAC do peer) ANTES de revelar qualquer coisa sobre alvos.
    #    Defensivo: qualquer erro de verificação vira 403 (nunca 500 pré-auth).
    try:
        peer = await fed.verify_inbound_envelope(env, signature)
    except Exception:
        logger.warning("federation_invoke: verify_inbound_envelope erro", exc_info=True)
        peer = None
    if not peer:
        raise HTTPException(403, "Peer desconhecido ou assinatura inválida")
    ws = env.origin_workspace

    # 3) anti-replay (só após autenticar): janela de tempo + nonce single-use
    if not fed.within_replay_window(env.created_at, datetime.utcnow()):
        raise HTTPException(401, "Fora da janela de replay (created_at)")
    if not await fed.check_and_record_nonce(env.envelope_id, ws):
        raise HTTPException(409, "Replay detectado")

    # 4) resolve o alvo — só capabilities EXPONÍVEIS (re-check TOCTOU); 404 indistinto
    entry = await fed.get_entry_by_urn(env.target_skill_urn)
    if not entry or not fed.is_federation_exposable(entry):
        raise HTTPException(404, "Capability não encontrada")

    # 5) resolver SELADO (snapshot obrigatório; root∈membros; sem fallback de mesh vivo)
    root, members = await fed.resolve_federated_exec(entry)
    if not root or not members:
        raise HTTPException(422, "Capability sem snapshot selável — não executável")

    # 6) input vem ASSINADO em context.user_input
    user_input = (env.context or {}).get("user_input")
    if not isinstance(user_input, str) or not user_input.strip():
        raise HTTPException(400, "context.user_input ausente/vazio")
    if len(user_input) > _MAX_INPUT_CHARS:
        raise HTTPException(413, "user_input excede o limite")

    # 7) executa SELADO sob a identidade do peer (reusa execução+custo+trust)
    consumer_id = f"federation:{ws}"
    execution = await create_execution(
        recipe_entry_id=entry["id"], consumer_user_id=consumer_id, input_text=user_input,
    )
    from app.catalog.executor import execute_pipeline_entry
    try:
        result = await asyncio.wait_for(
            execute_pipeline_entry(
                execution_id=execution["id"],
                pipeline_entry_id=entry["id"],
                root_agent_id=root,
                consumer_user={"id": consumer_id},
                user_input=user_input,
                allowed_agent_ids=members,  # SELA ao subgrafo do snapshot
            ),
            timeout=_EXEC_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Execução excedeu o tempo limite")

    rec = await get_execution(execution["id"]) or {}
    # Audit guardado (N2): a execução já produziu efeitos (row+custo+trust); uma
    # falha de audit NÃO pode virar 500 (o peer reexecutaria → custo dobrado).
    try:
        await audit_repo.create({
            "entity_type": "federation_invoke",
            "entity_id": entry["id"],
            "action": "invoked",
            "actor": consumer_id,
            "details": json.dumps({
                "peer_workspace": ws,
                "target_urn": env.target_skill_urn,
                "execution_id": execution["id"],
                "status": rec.get("status"),
                "envelope_id": env.envelope_id,
            }),
        })
    except Exception:
        logger.warning("federation_invoke: audit falhou (execução já efetivada)", exc_info=True)
    return {
        "execution_id": execution["id"],
        "status": rec.get("status"),
        "output": (result or {}).get("output", ""),
        "total_cost_usd": rec.get("total_cost_usd"),
        "total_latency_ms": rec.get("total_latency_ms"),
        "workspace": ws,
    }


# ── Registro de peers (PR8b2) — ROOT-only; gere relações de confiança ────────
peers_router = APIRouter(prefix="/api/v1/federation/peers", tags=["federation"])


class PeerCreate(BaseModel):
    workspace: str
    base_url: Optional[str] = None


def _require_root(user: dict) -> None:
    if not is_root(user):
        raise HTTPException(403, "Apenas root pode gerir peers de federação")


def _peer_public(r: dict) -> dict:
    """View pública do peer — NUNCA expõe shared_secret/secret_prev."""
    return {
        "id": r["id"],
        "workspace": r["workspace"],
        "base_url": r.get("base_url"),
        "status": r.get("status"),
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        "rotated_at": r["rotated_at"].isoformat() if r.get("rotated_at") else None,
        "has_prev_secret": bool(r.get("secret_prev")),
    }


async def _audit_peer(
    action: str, peer_id: str, actor: str, workspace: str,
    extra: Optional[dict] = None,
) -> None:
    details = {"workspace": workspace}
    if extra:
        details.update(extra)
    await audit_repo.create({
        "entity_type": "federation_peer",
        "entity_id": peer_id,
        "action": action,
        "actor": actor,
        "details": json.dumps(details),
    })


async def _audit_sync_failed(peer_id: str, actor: str, workspace: str, cause_kind: str) -> None:
    """Falha de sync auditada (67.0.0): antes era SÓ log — o painel de federação
    deriva "última falha de sync" do audit_log, e sem esta linha a falha era
    invisível (ausência de 'synced' não distingue "nunca rodou" de "falhou").
    Best-effort: erro de auditoria nunca mascara o HTTPException da falha real.

    A causa é CATEGÓRICA de propósito (ssrf|peer_error|network|internal): a
    mensagem verbosa carrega host/IP interno (SSRFError) ou texto CONTROLADO
    PELO PEER (detail do corpo de erro), e audit_log.details é legível por
    qualquer usuário autenticado via /api/v1/history — a versão verbosa fica
    só no logger e na resposta HTTP da rota de sync (root-only)."""
    try:
        await _audit_peer("sync_failed", peer_id, actor, workspace,
                          extra={"cause_kind": cause_kind})
    except Exception:
        logger.warning("sync_peer: audit de sync_failed falhou", exc_info=True)


@peers_router.post("", status_code=201)
async def create_peer(data: PeerCreate, user: dict = Depends(require_user)):
    """Registra um peer confiável. Devolve o shared_secret em plaintext UMA vez
    (compartilhe com o peer; o banco só guarda cifrado)."""
    _require_root(user)
    try:
        row, secret = await peers.register_peer(data.workspace, data.base_url)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, f"Peer para workspace '{data.workspace}' já existe (use rotate p/ trocar o segredo)")
    except Exception as e:
        # Fallback p/ ambientes/fakes que não sobem UniqueViolationError tipada
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(409, f"Peer para workspace '{data.workspace}' já existe (use rotate p/ trocar o segredo)")
        raise
    await _audit_peer("created", row["id"], user["id"], row["workspace"])
    return {
        **_peer_public(row),
        "shared_secret": secret,  # ⚠ ÚNICA vez que o plaintext aparece
        "warning": "Compartilhe o segredo com o peer agora — não será mostrado de novo.",
    }


@peers_router.get("")
async def list_peers_route(user: dict = Depends(require_user)):
    """Lista peers (sem segredos)."""
    _require_root(user)
    rows = await peers.list_peers()
    return {"peers": [_peer_public(r) for r in rows]}


@peers_router.post("/{peer_id}/rotate")
async def rotate_peer_route(peer_id: str, user: dict = Depends(require_user)):
    """Roda o segredo do peer (janela de sobreposição). Devolve o novo plaintext."""
    _require_root(user)
    res = await peers.rotate_peer_secret(peer_id)
    if not res:
        raise HTTPException(404, "Peer não encontrado")
    row, secret = res
    await _audit_peer("rotated", peer_id, user["id"], row["workspace"])
    return {
        **_peer_public(row),
        "shared_secret": secret,
        "warning": "Compartilhe o novo segredo — o anterior ainda vale até a próxima rotação.",
    }


@peers_router.delete("/{peer_id}")
async def revoke_peer_route(peer_id: str, user: dict = Depends(require_user)):
    """Revoga o peer (status='revoked'; não apaga). Idempotente p/ ausência → 404."""
    _require_root(user)
    row = await peers.revoke_peer(peer_id)
    if not row:
        raise HTTPException(404, "Peer não encontrado")
    await _audit_peer("revoked", peer_id, user["id"], row.get("workspace", ""))
    return {"message": "Peer revogado", "id": peer_id}


@peers_router.post("/{peer_id}/sync")
async def sync_peer_route(peer_id: str, user: dict = Depends(require_user)):
    """Puxa o manifesto do peer e registra/atualiza as capabilities remotas como
    entries federadas (read-only) no catálogo local. ROOT-only. Guarda SSRF no egress."""
    _require_root(user)
    if not await federation_enabled():
        raise HTTPException(409, "Federação desabilitada")
    peer = await federation_peers_repo.find_by_id(peer_id)
    if not peer or peer.get("status") != "active":
        raise HTTPException(404, "Peer não encontrado ou revogado")
    try:
        res = await egress.sync_remote_entries(peer, user["id"])
    except SSRFError as e:
        await _audit_sync_failed(peer_id, user["id"], peer.get("workspace", ""), "ssrf")
        raise HTTPException(400, f"base_url do peer rejeitada (SSRF): {e}")
    except (ValueError, httpx.HTTPError) as e:
        # Mesmo tratamento do invoke (A2A-2): causa conhecida surfa no corpo.
        logger.warning("sync_peer: peer %s recusou/falhou: %s", peer_id, e)
        await _audit_sync_failed(peer_id, user["id"], peer.get("workspace", ""),
                                 "peer_error" if isinstance(e, ValueError) else "network")
        raise HTTPException(502, f"Falha ao sincronizar com o peer: {_peer_failure_cause(e)}")
    except Exception:
        logger.warning("sync_peer: falha ao sincronizar peer %s", peer_id, exc_info=True)
        await _audit_sync_failed(peer_id, user["id"], peer.get("workspace", ""), "internal")
        raise HTTPException(502, "Falha ao sincronizar com o peer")
    await _audit_peer("synced", peer_id, user["id"], peer.get("workspace", ""))
    return res


# ── Remote invoke (PR8c egress) — invoca uma capability FEDERADA via o peer ──


class RemoteInvokeRequest(BaseModel):
    input: str


@router.post("/api/v1/federation/remote/{entry_id}/invoke")
async def remote_invoke_route(
    entry_id: str, data: RemoteInvokeRequest, user: dict = Depends(require_user)
):
    """Invoca uma entry FEDERADA (capability de um peer) via o peer dono: assina o
    envelope e faz POST no /federation/invoke remoto. Devolve a resposta do peer.
    Requer usuário autenticado; fail-closed sem MAESTRO_SECRET_KEY."""
    if not await federation_enabled():
        raise HTTPException(409, "Federação desabilitada")
    if not secret_key_present():
        raise HTTPException(503, "Federação indisponível (MAESTRO_SECRET_KEY ausente)")
    entry_row = await catalog_entries_repo.find_by_id(entry_id)
    if not entry_row:
        raise HTTPException(404, "Entry não encontrada")
    entry = db_row_to_entry_dict(entry_row)
    if not entry.get("federated") or not entry.get("remote_peer_id"):
        raise HTTPException(422, "Entry não é federada — use /execute-pipeline para pipelines locais")
    user_input = (data.input or "").strip()
    if not user_input:
        raise HTTPException(400, "input vazio")
    if len(user_input) > _MAX_INPUT_CHARS:
        raise HTTPException(413, "input excede o limite")
    peer = await federation_peers_repo.find_by_id(entry["remote_peer_id"])
    if not peer or peer.get("status") != "active":
        raise HTTPException(409, "Peer da capability indisponível ou revogado")
    try:
        result = await egress.invoke_remote(entry, user_input, peer)
    except SSRFError as e:
        raise HTTPException(400, f"base_url do peer rejeitada (SSRF): {e}")
    except (ValueError, httpx.HTTPError) as e:
        # Causa CONHECIDA (status+detail do peer, segredo inutilizável, rede) —
        # surfar: o 502 mudo escondia p.ex. o 503 fail-closed do peer (A2A-2).
        logger.warning("remote_invoke: peer recusou/falhou na entry %s: %s", entry_id, e)
        raise HTTPException(502, f"Falha ao invocar o peer: {_peer_failure_cause(e)}")
    except Exception:
        logger.warning("remote_invoke: falha ao invocar peer da entry %s", entry_id, exc_info=True)
        raise HTTPException(502, "Falha ao invocar o peer")
    # best-effort: registra uso local da capability remota (visibilidade/chargeback).
    # Custo/latência são PEER-ATTESTED (não medidos localmente) → clamp a faixas sãs.
    def _clamp(v, cap):
        try:
            return max(0.0, min(float(v or 0), cap))
        except (ValueError, TypeError):
            return 0.0
    try:
        from app.catalog.queries import record_invocation_cost
        await record_invocation_cost(
            entry_id, consumer_user_id=user["id"],
            cost_usd=_clamp(result.get("total_cost_usd"), _MAX_REMOTE_COST_USD),
            latency_ms=_clamp(result.get("total_latency_ms"), _MAX_REMOTE_LATENCY_MS),
        )
    except Exception:
        logger.warning("remote_invoke: record_invocation_cost falhou", exc_info=True)
    try:
        await audit_repo.create({
            "entity_type": "federation_remote_invoke",
            "entity_id": entry_id,
            "action": "invoked",
            "actor": user["id"],
            "details": json.dumps({
                "peer_workspace": peer.get("workspace"),
                "remote_urn": entry.get("remote_urn"),
                "status": result.get("status"),
            }),
        })
    except Exception:
        logger.warning("remote_invoke: audit falhou", exc_info=True)
    return result


# ── Config (PR8d UI) — enable/workspace/dev_allow_http; ROOT-only ────────────


class FederationConfig(BaseModel):
    enabled: Optional[bool] = None
    workspace: Optional[str] = None
    dev_allow_http: Optional[bool] = None


async def _federation_config_dict() -> dict:
    return {
        "enabled": await federation_enabled(),
        "workspace": await local_workspace(),
        "dev_allow_http": _truthy(await settings_store.get(_DEV_ALLOW_HTTP_KEY, "")),
        "secret_key_present": secret_key_present(),
    }


@router.get("/api/v1/federation/config")
async def get_federation_config(user: dict = Depends(require_user)):
    """Config atual da federação (+ flag secret_key_present p/ a UI avisar fail-closed)."""
    _require_root(user)
    return await _federation_config_dict()


@router.put("/api/v1/federation/config")
async def put_federation_config(data: FederationConfig, user: dict = Depends(require_user)):
    """Atualiza enabled/workspace/dev_allow_http (parcial). Valida o charset do workspace."""
    _require_root(user)
    if data.workspace is not None:
        ws = data.workspace.strip()
        if not is_valid_workspace(ws):
            raise HTTPException(422, "workspace inválido (esperado [a-z0-9-]+)")
        await settings_store.set(WORKSPACE_SETTING_KEY, ws)
    if data.enabled is not None:
        await settings_store.set(ENABLED_SETTING_KEY, "true" if data.enabled else "false")
    if data.dev_allow_http is not None:
        await settings_store.set(_DEV_ALLOW_HTTP_KEY, "true" if data.dev_allow_http else "false")
    await _audit_peer("config_updated", "federation", user["id"], data.workspace or "")
    return await _federation_config_dict()


@router.get("/api/v1/federation/remote-entries")
async def list_remote_entries(user: dict = Depends(require_user)):
    """Lista as entries FEDERADAS (capabilities remotas espelhadas localmente)."""
    pool = _get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            "SELECT id, name, version, remote_urn, adapter_config "
            "FROM catalog_entries WHERE federated = TRUE ORDER BY name"
        )
    out = []
    for r in rows:
        d = dict(r)
        try:
            cfg = json.loads(d.get("adapter_config") or "{}")
        except (ValueError, TypeError):
            cfg = {}
        out.append({
            "id": d["id"], "name": d["name"], "version": d.get("version"),
            "remote_urn": d.get("remote_urn"), "peer_workspace": cfg.get("peer_workspace"),
        })
    return {"entries": out}


# ── Painel da federação (F1, 67.0.0) — visão consolidada SÓ com dados locais ─


def _iso(v) -> Optional[str]:
    """Timestamp de row → ISO 8601 (naive = UTC; a UI converte com tzParse)."""
    if isinstance(v, datetime):
        return v.isoformat()
    return v if isinstance(v, str) else None


_DASH_AUDIT_WINDOW = 1000    # eventos de peer varridos p/ derivar syncs (janela, não histórico)
_DASH_ACTIVITY_LIMIT = 20    # feed "últimas invocações federadas"


@router.get("/api/v1/federation/dashboard")
async def federation_dashboard(user: dict = Depends(require_user)):
    """Painel read-only da federação — agrega SÓ dados locais (peers, entries
    federadas, catalog_costs e audit_log). ROOT-only, como o resto da gestão de
    peers (a lista de peers é inventário sensível). NÃO é gated por
    federation_enabled: observabilidade funciona com a federação desligada
    (o payload diz federation_enabled=false).

    Honestidades deliberadas:
    - consumo federado vive em catalog_costs (o remote_invoke grava via
      record_invocation_cost de catalog.queries) — NÃO em invocation_costs;
    - custo/latência são PEER-ATTESTED (clampados na gravação); o payload
      carrega costs_peer_attested=True para a UI rotular;
    - last_sync_at = último sync BEM-SUCEDIDO auditado; falhas só são
      auditadas a partir de 67.0.0 (action 'sync_failed') e as contagens
      derivam de uma janela de auditoria, não do histórico completo.
    """
    _require_root(user)

    peer_rows = await peers.list_peers()

    # Eventos de peer — 1 fetch (created_at DESC), redução em Python.
    peer_events = await audit_repo.find_all(
        entity_type="federation_peer", limit=_DASH_AUDIT_WINDOW
    )
    last_sync: dict = {}
    last_sync_failed: dict = {}
    sync_failures: dict = {}
    for ev in peer_events:  # DESC — o primeiro visto por peer é o mais recente
        pid = ev.get("entity_id") or ""
        action = ev.get("action")
        if action == "synced":
            if pid not in last_sync:
                last_sync[pid] = _iso(ev.get("created_at"))
        elif action == "sync_failed":
            if pid not in last_sync_failed:
                last_sync_failed[pid] = _iso(ev.get("created_at"))
            sync_failures[pid] = sync_failures.get(pid, 0) + 1

    pool = _get_pool()
    async with pool.acquire() as con:
        entry_rows = await con.fetch(
            "SELECT id, name, kind, domain, version, remote_urn, remote_peer_id "
            "FROM catalog_entries WHERE federated = TRUE ORDER BY name"
        )
        entry_ids = [r["id"] for r in entry_rows]
        cost_rows = []
        if entry_ids:
            cost_rows = await con.fetch(
                "SELECT entry_id, COUNT(*) AS invocations, "
                "COALESCE(SUM(cost_usd), 0) AS total_cost_usd, "
                "COALESCE(AVG(latency_ms), 0) AS avg_latency_ms, "
                "MAX(invoked_at) AS last_invoked_at "
                "FROM catalog_costs WHERE entry_id = ANY($1::text[]) "
                "GROUP BY entry_id",
                entry_ids,
            )
    entries = [dict(r) for r in entry_rows]
    cost_by_entry = {r["entry_id"]: dict(r) for r in cost_rows}
    entry_name = {e["id"]: e.get("name") for e in entries}

    peer_by_id = {r["id"]: r for r in peer_rows}
    by_peer_entries: dict = {}
    orphans = []
    for e in entries:
        pid = e.get("remote_peer_id")
        peer = peer_by_id.get(pid) if pid else None
        if peer is None:
            orphans.append({"id": e["id"], "name": e.get("name"),
                            "remote_urn": e.get("remote_urn"), "reason": "peer_ausente"})
        elif peer.get("status") != "active":
            orphans.append({"id": e["id"], "name": e.get("name"),
                            "remote_urn": e.get("remote_urn"), "reason": "peer_revogado"})
        if pid:
            by_peer_entries.setdefault(pid, []).append(e)

    out_peers = []
    for r in peer_rows:
        mine = by_peer_entries.get(r["id"], [])
        cons = {"invocations": 0, "total_cost_usd": 0.0,
                "avg_latency_ms": None, "last_invoked_at": None}
        lat_weighted = 0.0
        for e in mine:
            c = cost_by_entry.get(e["id"])
            if not c:
                continue
            n = int(c.get("invocations") or 0)
            cons["invocations"] += n
            cons["total_cost_usd"] += float(c.get("total_cost_usd") or 0.0)
            lat_weighted += float(c.get("avg_latency_ms") or 0.0) * n
            li = _iso(c.get("last_invoked_at"))
            if li and (cons["last_invoked_at"] is None or li > cons["last_invoked_at"]):
                cons["last_invoked_at"] = li
        if cons["invocations"]:
            cons["avg_latency_ms"] = round(lat_weighted / cons["invocations"], 2)
        cons["total_cost_usd"] = round(cons["total_cost_usd"], 6)
        out_peers.append({
            **_peer_public(r),   # nunca a row crua — segredos ficam fora
            "capabilities": len(mine),
            "last_sync_at": last_sync.get(r["id"]),
            "last_sync_failed_at": last_sync_failed.get(r["id"]),
            "sync_failures_recent": sync_failures.get(r["id"], 0),
            "consumption": cons,
        })

    by_domain: dict = {}
    for e in entries:
        key = e.get("domain") or ""
        by_domain[key] = by_domain.get(key, 0) + 1

    activity_rows = await audit_repo.find_all(
        entity_type="federation_remote_invoke", limit=_DASH_ACTIVITY_LIMIT
    )
    recent = []
    for ev in activity_rows:
        try:
            det = json.loads(ev.get("details") or "{}")
        except (ValueError, TypeError):
            det = {}
        eid = ev.get("entity_id")
        recent.append({
            "entry_id": eid,
            "entry_name": entry_name.get(eid),  # None se a entry sumiu — sem inventar
            "peer_workspace": det.get("peer_workspace"),
            "status": det.get("status"),
            "created_at": _iso(ev.get("created_at")),
        })

    # Totais de consumo sobre TODAS as entries federadas (inclui órfãs — o
    # gasto delas existiu; somar só peers conhecidos esconderia consumo real).
    total_inv = sum(int(c.get("invocations") or 0) for c in cost_by_entry.values())
    total_cost = round(sum(float(c.get("total_cost_usd") or 0.0)
                           for c in cost_by_entry.values()), 6)
    return {
        "generated_at": naive_utc_now().isoformat(),
        "federation_enabled": await federation_enabled(),
        "secret_key_present": secret_key_present(),
        "costs_peer_attested": True,
        "peers": out_peers,
        "orphans": orphans,
        "by_domain": [{"domain": k or None, "count": v}
                      for k, v in sorted(by_domain.items(), key=lambda kv: -kv[1])],
        "recent_invocations": recent,
        "totals": {
            "peers_active": sum(1 for r in peer_rows if r.get("status") == "active"),
            "peers_revoked": sum(1 for r in peer_rows if r.get("status") == "revoked"),
            "remote_capabilities": len(entries),
            "orphan_capabilities": len(orphans),
            "invocations": total_inv,
            "total_cost_usd": total_cost,
        },
    }
