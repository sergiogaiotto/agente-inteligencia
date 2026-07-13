"""Rotas da plataforma: releases §18, gold §9.4, harness §9.5, knowledge §14, tools §10, dashboard, settings.

CORREÇÕES (2026-04):
- Timeout do test_mcp_connection (stdio) elevado de 20s → 90s para acomodar 1ª execução de `npx -y`
- Timeout do execute_mcp_tool (stdio) elevado de 30s → 90s pelo mesmo motivo
- update_tool: usa exclude_unset (não sobrescreve campos não enviados com None), loga, trata empty upd
- create_tool: idem, com logging
- CORREÇÃO (2026-04-21): Header `Accept: application/json, text/event-stream` adicionado a TODAS
  as chamadas HTTP para MCP servers. Sem esse header, servidores que usam o transporte
  MCP Streamable HTTP (spec 2025-03-26) retornam HTTP 406 Not Acceptable.
"""
import uuid, json, logging
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Body
from pydantic import BaseModel, Field
from typing import Optional
from app.models.schemas import ReleaseCreate, GoldCaseCreate, KnowledgeSourceCreate, ToolCreate, ToolUpdate, RunEvalRequest
from app.core.auth import require_role, require_user
from app.core.database import (
    releases_repo, gold_cases_repo, eval_runs_repo, knowledge_repo,
    tools_repo, agents_repo, skills_repo, interactions_repo,
    turns_repo, envelopes_repo, drift_repo, audit_repo,
    settings_store, prompts_repo,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["platform"])


# ═══════════════════════════════════════════════════════════════
# Saúde dos modelos (chat/roteamento + embeddings) — alimenta o chip no header.
# ═══════════════════════════════════════════════════════════════
@router.get("/llm/health")
async def llm_health(force: bool = False):
    """O que será usado dali em diante + disponibilidade (probe de inferência).

    Resolve o roteamento (task_type → provider/model) + o provider de embeddings
    e sonda cada modelo distinto com uma chamada mínima. Cacheado (TTL) — passe
    ``?force=true`` para re-sondar. Não levanta: erros viram status por modelo.
    """
    from app.core.model_health import get_model_health

    try:
        return await get_model_health(force=force)
    except Exception as e:
        logger.warning(f"llm_health falhou: {type(e).__name__}: {e}")
        return {"all_ok": False, "any_fallback": False, "chat": {},
                "embeddings": {"ok": False, "error": str(e)[:120]},
                "error": "Falha ao apurar saúde dos modelos."}


# ═══════════════════════════════════════════════════════════════
# Auth MCP — suporte a API Key, OAuth2 Client Credentials e mTLS.
#
# Headers base: Content-Type + Accept (MCP Streamable HTTP spec
# 2025-03-26 exige Accept com application/json e text/event-stream).
#
# API Key:  → Authorization: Bearer <token>
# OAuth2:   → busca access_token via client_credentials grant
#              e injeta Authorization: Bearer <access_token>
# mTLS:     → conexão TLS mútua via certificado cliente (PEM)
#              injetado como parâmetro `cert` no httpx.AsyncClient
# ═══════════════════════════════════════════════════════════════
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

# Cache de tokens OAuth2 para evitar buscar a cada chamada
_oauth2_token_cache: dict = {}  # key: client_id → {"access_token": str, "expires_at": float}


def _build_mcp_auth(auth_type: str = "", auth_token: str = "", auth_config: str = "{}") -> dict:
    """Constrói configuração de autenticação MCP.

    Retorna dict com:
      - headers: dict de headers HTTP (sempre inclui MCP_HEADERS base)
      - client_kwargs: dict de kwargs extras para httpx.AsyncClient (cert para mTLS)
      - needs_oauth_fetch: bool — se precisa buscar token OAuth2 antes de usar

    auth_type: 'api_key', 'oauth2', 'mTLS', ou ''
    auth_token: token simples (usado por api_key) — pode vir cifrado (fernet:)
                quando a UI ecoa o valor lido do banco
    auth_config: JSON string com config complexa (OAuth2/mTLS)

    Bug 2026-05-27 (Tavily HTTP 401): a UI carregava `tools.auth_token`
    cifrado do banco (string com prefixo `fernet:`) e enviava de volta no
    POST /tools/test sem alteração. _build_mcp_auth montava header
    `Authorization: Bearer fernet:gAAAAA...` e o servidor MCP rejeitava com
    401. Fix: read_secret() é idempotente — texto plano passa direto,
    fernet decifra. Mesmo comportamento que mcp/runtime.py já usava em
    invocação real de tools.
    """
    import json as _json
    from app.core.secrets import read_secret

    # Idempotente: decifra fernet:... ou devolve plaintext intacto.
    auth_token = read_secret(auth_token) if auth_token else ""

    result = {
        "headers": {**MCP_HEADERS},
        "client_kwargs": {},
        "oauth_config": None,
    }

    if not auth_type:
        return result

    # ── API Key: token direto no header ──
    if auth_type == "api_key":
        if auth_token and auth_token.strip():
            result["headers"]["Authorization"] = f"Bearer {auth_token.strip()}"
        return result

    # ── OAuth2 Client Credentials ──
    if auth_type == "oauth2":
        try:
            config = _json.loads(auth_config) if auth_config else {}
        except (ValueError, TypeError):
            config = {}
        result["oauth_config"] = {
            "client_id": config.get("client_id", ""),
            "client_secret": config.get("client_secret", ""),
            "token_url": config.get("token_url", ""),
            "scope": config.get("scope", ""),
        }
        # Verificar cache
        client_id = result["oauth_config"]["client_id"]
        if client_id and client_id in _oauth2_token_cache:
            import time
            cached = _oauth2_token_cache[client_id]
            if cached.get("expires_at", 0) > time.time():
                result["headers"]["Authorization"] = f"Bearer {cached['access_token']}"
                result["oauth_config"] = None  # Não precisa buscar de novo
        return result

    # ── mTLS: certificados cliente ──
    if auth_type == "mTLS":
        try:
            config = _json.loads(auth_config) if auth_config else {}
        except (ValueError, TypeError):
            config = {}
        cert_pem = config.get("client_cert", "")
        key_pem = config.get("client_key", "")
        if cert_pem and key_pem:
            import tempfile, os
            # Escrever PEMs em arquivos temporários (httpx exige paths)
            cert_file = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
            cert_file.write(cert_pem)
            cert_file.close()
            key_file = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
            key_file.write(key_pem)
            key_file.close()
            result["client_kwargs"]["cert"] = (cert_file.name, key_file.name)
            # CA cert opcional
            ca_pem = config.get("ca_cert", "")
            if ca_pem:
                ca_file = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
                ca_file.write(ca_pem)
                ca_file.close()
                result["client_kwargs"]["verify"] = ca_file.name
        return result

    # Fallback: se auth_type desconhecido mas tem token, usar como Bearer
    if auth_token and auth_token.strip():
        result["headers"]["Authorization"] = f"Bearer {auth_token.strip()}"
    return result


async def _fetch_oauth2_token(oauth_config: dict) -> str:
    """Busca access_token via OAuth2 Client Credentials Grant.

    Retorna o access_token ou string vazia em caso de falha.
    Cacheia o token até expiração.
    """
    import httpx, time

    client_id = oauth_config.get("client_id", "")
    client_secret = oauth_config.get("client_secret", "")
    token_url = oauth_config.get("token_url", "")
    scope = oauth_config.get("scope", "")

    if not client_id or not client_secret or not token_url:
        return ""

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            payload = {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
            if scope:
                payload["scope"] = scope

            resp = await client.post(
                token_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                logger.warning(f"OAuth2 token fetch failed: HTTP {resp.status_code} — {resp.text[:200]}")
                return ""

            data = resp.json()
            access_token = data.get("access_token", "")
            expires_in = data.get("expires_in", 3600)

            # Cachear com margem de 60s
            _oauth2_token_cache[client_id] = {
                "access_token": access_token,
                "expires_at": time.time() + max(expires_in - 60, 60),
            }
            logger.info(f"OAuth2 token obtido para client_id={client_id[:8]}... expires_in={expires_in}s")
            return access_token

    except Exception as e:
        logger.warning(f"OAuth2 token fetch error: {e}")
        return ""


def _cleanup_mcp_auth(auth_result: dict):
    """Remove arquivos temporários de certificados mTLS."""
    import os
    cert = auth_result.get("client_kwargs", {}).get("cert")
    if cert and isinstance(cert, tuple):
        for path in cert:
            try: os.unlink(path)
            except: pass
    verify = auth_result.get("client_kwargs", {}).get("verify")
    if verify and isinstance(verify, str) and verify.endswith('.pem'):
        try: os.unlink(verify)
        except: pass

# ═══ Dashboard ═══
@router.get("/dashboard/stats")
async def dashboard_stats():
    return {
        "total_agents": await agents_repo.count(),
        "agents_by_kind": {
            "aobd": await agents_repo.count(kind="aobd"),
            "router": await agents_repo.count(kind="router"),
            "subagent": await agents_repo.count(kind="subagent"),
        },
        "active_agents": await agents_repo.count(status="active"),
        "inactive_agents": await agents_repo.count(status="inactive"),
        "active_by_kind": {
            "aobd": await agents_repo.count(kind="aobd", status="active"),
            "router": await agents_repo.count(kind="router", status="active"),
            "subagent": await agents_repo.count(kind="subagent", status="active"),
        },
        "total_skills": await skills_repo.count(),
        "total_interactions": await interactions_repo.count(),
        "total_turns": await turns_repo.count(),
        "total_releases": await releases_repo.count(),
        "total_gold_cases": await gold_cases_repo.count(),
        "total_eval_runs": await eval_runs_repo.count(),
        "total_knowledge_sources": await knowledge_repo.count(),
        "total_tools": await tools_repo.count(),
        "total_envelopes": await envelopes_repo.count(),
        "total_drift_events": await drift_repo.count(),
        "recent_interactions": await interactions_repo.find_all(limit=10),
        "recent_eval_runs": await eval_runs_repo.find_all(limit=5),
    }


# ─── Atividade por módulo (Guia dos Módulos) ───────────────────
# Mapeia eventos do audit_log + outras tabelas para o módulo correspondente.
# Usado pela seção "Atividade por Módulo" no dashboard, abaixo de "Interações Recentes".

# Mapeamento entity_type/action → (module_id, label do evento).
# Se o entity_type começa com state_transition: cai em §15.
# Caso default (não mapeado), o evento é ignorado.
_AUDIT_MODULE_MAP = {
    # entity_type → (module_id, default_label, default_section_emoji)
    "policy_decision": ("onda4a", "Decisão OPA", "Onda 4a"),
    "prompt_injection_blocked": ("onda1", "Prompt injection bloqueada", "Onda 1"),
    "agent": ("s4", "Agente alterado", "§4"),
    "skill": ("s5", "Skill alterada", "§5"),
    "release": ("s18", "Release alterada", "§18"),
    "eval_run": ("s95", "Avaliação executada", "§9.5"),
    # state_transition é tratado separadamente (prefix check)
}


@router.get("/dashboard/module-activity")
async def module_activity(limit: int = 20, offset: int = 0):
    """Retorna eventos recentes agregados por módulo, paginados.

    Fontes:
    - audit_log: state_transitions, policy_decisions, prompt_injection, etc.
    - evidence_chunks: ingestão de documentos (Onda 3)

    Cada item: {timestamp, module_id, module_label, action, summary, entity_id}.
    Paginação: limit + offset aplicados após a ordenação consolidada das duas fontes.
    """
    from app.core.database import _get_pool
    # Cap defensivo — "ver tudo" no UI envia limit=10000
    limit = max(1, min(int(limit), 10000))
    offset = max(0, int(offset))
    pool = _get_pool()
    items: list[dict] = []
    # Como filtramos `audit_log` por entity_type mapeado e mesclamos com evidence_chunks,
    # buscamos um buffer maior que limit+offset antes de cortar.
    fetch_window = (limit + offset) * 3 + 50
    async with pool.acquire() as con:
        # 1. audit_log (entradas com módulo mapeado — ainda não há WHERE pq mapping
        # é Python-side; pegamos buffer e filtramos)
        rows = await con.fetch(
            """
            SELECT entity_type, entity_id, action, details, created_at
            FROM audit_log
            ORDER BY created_at DESC
            LIMIT $1
            """,
            fetch_window,
        )
        for r in rows:
            etype = r["entity_type"] or ""
            action = r["action"] or ""
            # state_transition special case
            if etype == "interaction" and action.startswith("state_transition:"):
                # action ex: "state_transition:Intake→PolicyCheck"
                arrow = action[len("state_transition:"):]
                items.append({
                    "timestamp": r["created_at"].isoformat() if r["created_at"] else None,
                    "module_id": "s15",
                    "module_label": "FSM",
                    "module_section": "§15",
                    "action": "state_transition",
                    "summary": f"Transição: {arrow}",
                    "entity_id": r["entity_id"],
                })
                continue
            # action prompt_injection_blocked é em entity_type=interaction
            if etype == "interaction" and action == "prompt_injection_blocked":
                items.append({
                    "timestamp": r["created_at"].isoformat() if r["created_at"] else None,
                    "module_id": "onda1",
                    "module_label": "Segurança",
                    "module_section": "Onda 1",
                    "action": "prompt_injection_blocked",
                    "summary": "Prompt injection bloqueada (LLM01)",
                    "entity_id": r["entity_id"],
                })
                continue
            # policy_decision (Onda 4a)
            if etype == "policy_decision":
                items.append({
                    "timestamp": r["created_at"].isoformat() if r["created_at"] else None,
                    "module_id": "onda4a",
                    "module_label": "Policy as Code",
                    "module_section": "Onda 4a",
                    "action": action,  # "allow" | "deny"
                    "summary": f"OPA {action.upper()}: {r['entity_id']}",
                    "entity_id": r["entity_id"],
                })
                continue
            # outros entity_types mapeados
            mp = _AUDIT_MODULE_MAP.get(etype)
            if mp:
                mid, default_label, section = mp
                items.append({
                    "timestamp": r["created_at"].isoformat() if r["created_at"] else None,
                    "module_id": mid,
                    "module_label": default_label,
                    "module_section": section,
                    "action": action,
                    "summary": f"{default_label}: {action} ({r['entity_id'][:12]}…)" if r["entity_id"] else default_label,
                    "entity_id": r["entity_id"],
                })

        # 2. evidence_chunks (ingestão — Onda 3). Agrupa por source_id + minuto.
        ingest_rows = await con.fetch(
            """
            SELECT knowledge_source_id, count(*) AS chunks_count, max(created_at) AS last_at
            FROM evidence_chunks
            WHERE created_at > now() - interval '7 days'
            GROUP BY knowledge_source_id, date_trunc('minute', created_at)
            ORDER BY last_at DESC
            LIMIT $1
            """,
            fetch_window,
        )
        for r in ingest_rows:
            items.append({
                "timestamp": r["last_at"].isoformat() if r["last_at"] else None,
                "module_id": "s14",
                "module_label": "RAG / Evidence",
                "module_section": "§14",
                "action": "ingest",
                "summary": f"Ingestão: {r['chunks_count']} chunks em source {r['knowledge_source_id'][:12]}…",
                "entity_id": r["knowledge_source_id"],
            })

    # Ordena por timestamp desc, aplica offset+limit
    items.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    total = len(items)
    page = items[offset:offset + limit]
    return {"items": page, "total": total, "limit": limit, "offset": offset}


# ─── Verifier — visualização da tabela `verifications` (§14.2) ──────
@router.post("/dashboard/verifications/cleanup")
async def cleanup_verifications_payload(
    days: int = 90, user: dict = Depends(require_user)
):
    """Retenção do payload da Auditoria (24.10.0): NULLifica question_redacted/
    draft_redacted de verificações mais antigas que `days` dias. Preserva as
    LINHAS (scores/dimensões continuam alimentando drift, harness e /quality);
    remove só o texto (~12KB/linha no pior caso) — controle de crescimento.
    Root/admin apenas.
    """
    if (user.get("role") or "").lower() not in ("root", "admin"):
        raise HTTPException(403, "Apenas root/admin podem executar a limpeza.")
    days = max(1, min(int(days or 90), 3650))
    from app.core.database import _get_pool
    async with _get_pool().acquire() as con:
        status = await con.execute(
            "UPDATE verifications SET question_redacted = NULL, draft_redacted = NULL "
            "WHERE created_at < now() - ($1 || ' days')::interval "
            "AND (question_redacted IS NOT NULL OR draft_redacted IS NOT NULL)",
            str(days),
        )
    rows = 0
    try:
        rows = int((status or "").split()[-1])
    except Exception:
        pass
    logger.info(
        "verifications.payload_cleanup",
        extra={
            "event": "verifications.payload_cleanup",
            "days": days,
            "rows_cleaned": rows,
        },
    )
    return {"status": "ok", "days": days, "rows_cleaned": rows}


@router.get("/dashboard/verifications/stats")
async def verifications_stats(window: str = "24h"):
    """Agregados para os cards do topo da página /quality.

    `window`: "24h" | "7d" | "30d" | "all"
    """
    from app.core.database import _get_pool
    pool = _get_pool()
    where = ""
    if window == "24h":
        where = "WHERE created_at > now() - interval '24 hours'"
    elif window == "7d":
        where = "WHERE created_at > now() - interval '7 days'"
    elif window == "30d":
        where = "WHERE created_at > now() - interval '30 days'"
    # Linhas re-julgadas (profile='rejudge') ficam FORA dos agregados: são
    # A/B de juízes sob demanda (sem evidências re-anexadas — claims não
    # comparáveis), não tráfego real. Aparecem no breakdown by_profile, na
    # lista (filtro "re-julgados") e no by_judge_model (onde o A/B se lê).
    _rj = "profile IS DISTINCT FROM 'rejudge'"
    where_rj = f"{where} AND {_rj}" if where else f"WHERE {_rj}"
    # Série temporal (OBS-4): granularidade e alcance por janela. Unidade e
    # intervalo vêm de WHITELIST fixa (nunca de input do usuário) — sem injeção.
    if window == "24h":
        _bucket_unit, _series_interval = "hour", "24 hours"
    elif window == "7d":
        _bucket_unit, _series_interval = "day", "7 days"
    elif window == "30d":
        _bucket_unit, _series_interval = "day", "30 days"
    else:  # "all" — série limitada a 180d (semana) p/ não ficar ilimitada
        _bucket_unit, _series_interval = "week", "180 days"
    async with pool.acquire() as con:
        row = await con.fetchrow(
            f"""
            SELECT
              count(*)::int                            AS total,
              count(*) FILTER (WHERE ok)::int          AS ok_count,
              avg(factuality_score)::float             AS avg_factuality,
              avg(completeness_score)::float           AS avg_completeness,
              avg(tone_score)::float                   AS avg_tone,
              avg(safety_score)::float                 AS avg_safety,
              avg(confidence)::float                   AS avg_confidence,
              count(*) FILTER (WHERE NOT contract_compliant)::int AS contract_failures,
              count(*) FILTER (WHERE unsupported_claims != '[]' AND unsupported_claims IS NOT NULL)::int AS with_unsupported,
              avg(duration_ms)::float                  AS avg_duration_ms,
              -- Percentis (OBS-3): a cauda (p95/p99) é o que quebra SLA e some
              -- num avg. percentile_cont ignora NULL; NULL se não há amostra.
              percentile_cont(0.5)  WITHIN GROUP (ORDER BY duration_ms)::float AS p50_duration_ms,
              percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::float AS p95_duration_ms,
              percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms)::float AS p99_duration_ms
            FROM verifications
            {where_rj}
            """
        )
        # Distribuição por judge_model
        models = await con.fetch(
            f"""
            SELECT judge_model, count(*)::int AS n
            FROM verifications
            {where}
            GROUP BY judge_model
            ORDER BY n DESC
            LIMIT 10
            """
        )
        # Distribuição por profile
        profiles = await con.fetch(
            f"""
            SELECT profile, count(*)::int AS n
            FROM verifications
            {where}
            GROUP BY profile
            ORDER BY n DESC
            """
        )

        # Auditoria (25.0.0) — desempenho por AGENTE e por PIPELINE na janela.
        # LEFT JOIN p/ nome humano; linhas pré-migração (dono NULL) e
        # re-julgamentos ficam fora (não são tráfego real).
        where_v = where_rj.replace("created_at", "v.created_at").replace(
            "profile IS DISTINCT", "v.profile IS DISTINCT"
        )
        by_agent = await con.fetch(
            f"""
            SELECT v.agent_id, a.name AS agent_name, count(*)::int AS n,
                   count(*) FILTER (WHERE v.ok)::int AS ok_count,
                   avg(v.factuality_score)::float    AS avg_factuality,
                   avg(v.completeness_score)::float  AS avg_completeness,
                   avg(v.tone_score)::float          AS avg_tone,
                   count(*) FILTER (WHERE v.unsupported_claims != '[]'
                                    AND v.unsupported_claims IS NOT NULL)::int AS with_unsupported
            FROM verifications v
            LEFT JOIN agents a ON a.id = v.agent_id
            {where_v} AND v.agent_id IS NOT NULL
            GROUP BY v.agent_id, a.name
            ORDER BY n DESC
            LIMIT 12
            """
        )
        by_pipeline = await con.fetch(
            f"""
            SELECT v.pipeline_id, p.name AS pipeline_name, count(*)::int AS n,
                   count(*) FILTER (WHERE v.ok)::int AS ok_count,
                   avg(v.factuality_score)::float    AS avg_factuality,
                   avg(v.completeness_score)::float  AS avg_completeness,
                   avg(v.tone_score)::float          AS avg_tone,
                   count(*) FILTER (WHERE v.unsupported_claims != '[]'
                                    AND v.unsupported_claims IS NOT NULL)::int AS with_unsupported
            FROM verifications v
            LEFT JOIN pipelines p ON p.id = v.pipeline_id
            {where_v} AND v.pipeline_id IS NOT NULL
            GROUP BY v.pipeline_id, p.name
            ORDER BY n DESC
            LIMIT 12
            """
        )

        # Série temporal (OBS-4): volume + taxa de falha + cauda de latência por
        # bucket. Derivável 100% de `verifications` (o escalonamento/recusa por
        # invocação vive no transition_log/final_state → fica pro Grafana via os
        # contadores maestro_escalations_total/maestro_refusals_total).
        series = await con.fetch(
            f"""
            SELECT date_trunc('{_bucket_unit}', created_at)          AS bucket,
                   count(*)::int                                     AS n,
                   count(*) FILTER (WHERE ok)::int                   AS ok,
                   count(*) FILTER (WHERE NOT ok)::int               AS errors,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::float AS p95_duration_ms
            FROM verifications
            WHERE created_at > now() - interval '{_series_interval}'
              AND {_rj}
            GROUP BY bucket
            ORDER BY bucket
            """
        )

    def _round_row(r: dict) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}

    stats = dict(row) if row else {}
    # Threshold do juiz exposto p/ a UI não inventar critério próprio
    # (card "Atenção" da Observabilidade compara contra ELE, com legenda).
    try:
        from app.core.config import get_settings
        _fact_th = float(get_settings().verifier_factuality_threshold)
    except Exception:
        _fact_th = 3.0
    return {
        "window": window,
        "stats": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in stats.items()},
        "factuality_threshold": _fact_th,
        "by_judge_model": [dict(r) for r in models],
        "by_profile": [dict(r) for r in profiles],
        "by_agent": [_round_row(dict(r)) for r in by_agent],
        "by_pipeline": [_round_row(dict(r)) for r in by_pipeline],
        "timeseries": [
            {
                "bucket": r["bucket"].isoformat() if r["bucket"] else None,
                "n": r["n"],
                "ok": r["ok"],
                "errors": r["errors"],
                "p95_duration_ms": (round(r["p95_duration_ms"], 1)
                                    if r["p95_duration_ms"] is not None else None),
            }
            for r in series
        ],
    }


@router.get("/dashboard/verifications")
async def list_verifications(
    limit: int = 30,
    offset: int = 0,
    ok_only: bool = False,
    min_factuality: Optional[float] = None,
    min_completeness: Optional[float] = None,
    profile: Optional[str] = None,
    judge_model: Optional[str] = None,
    interaction_id: Optional[str] = None,
    # Auditoria (25.0.0): drill-down por dono + "só com alucinação".
    agent_id: Optional[str] = None,
    pipeline_id: Optional[str] = None,
    with_claims_only: bool = False,
):
    """Lista paginada de verificações com filtros.

    Filtro `interaction_id` (Onda 6 deep-link): permite o /workspace abrir
    /quality?interaction_id=X pra mostrar a auditoria completa daquela
    interação específica (vs. lista geral).

    Auditoria (25.0.0): filtros por `agent_id`/`pipeline_id` +
    `with_claims_only`; itens incluem o par pergunta/resposta julgado
    (redacted), rastro do contract-retry e nomes humanos do dono.
    """
    from app.core.database import _get_pool
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    where = []
    args: list = []
    if ok_only:
        where.append("ok = TRUE")
    if min_factuality is not None:
        args.append(min_factuality)
        where.append(f"factuality_score >= ${len(args)}")
    if min_completeness is not None:
        args.append(min_completeness)
        where.append(f"completeness_score >= ${len(args)}")
    if profile:
        args.append(profile)
        where.append(f"profile = ${len(args)}")
    if judge_model:
        args.append(judge_model)
        where.append(f"judge_model = ${len(args)}")
    if interaction_id:
        args.append(interaction_id)
        where.append(f"interaction_id = ${len(args)}")
    if agent_id:
        args.append(agent_id)
        where.append(f"agent_id = ${len(args)}")
    if pipeline_id:
        args.append(pipeline_id)
        where.append(f"pipeline_id = ${len(args)}")
    if with_claims_only:
        where.append("unsupported_claims != '[]' AND unsupported_claims IS NOT NULL")
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    pool = _get_pool()
    async with pool.acquire() as con:
        total = await con.fetchval(f"SELECT count(*) FROM verifications {where_clause}", *args) or 0
        args_with_paging = list(args) + [limit, offset]
        rows = await con.fetch(
            f"""
            SELECT id, turn_id, interaction_id,
                   agent_id, pipeline_id, question_redacted, draft_redacted,
                   factuality_score, factuality_reason,
                   completeness_score, completeness_reason,
                   tone_score, tone_reason,
                   safety_score, safety_reason,
                   contract_compliant, contract_errors,
                   contract_retried, contract_original_errors,
                   ok, confidence, unsupported_claims,
                   judge_model, profile, duration_ms, created_at
            FROM verifications
            {where_clause}
            ORDER BY created_at DESC
            LIMIT ${len(args)+1} OFFSET ${len(args)+2}
            """,
            *args_with_paging,
        )
        items = []
        for r in rows:
            d = dict(r)
            # Decodifica JSONs em strings
            for jf in ("contract_errors", "unsupported_claims", "contract_original_errors"):
                try:
                    d[jf] = json.loads(d.get(jf) or "[]")
                except Exception:
                    d[jf] = []
            if d.get("created_at"):
                d["created_at"] = d["created_at"].isoformat()
            items.append(d)

        # Nomes humanos do dono (batch — no máx. 2 queries por página)
        a_ids = list({d["agent_id"] for d in items if d.get("agent_id")})
        p_ids = list({d["pipeline_id"] for d in items if d.get("pipeline_id")})
        names_a: dict = {}
        names_p: dict = {}
        try:
            if a_ids:
                rows_a = await con.fetch(
                    "SELECT id, name FROM agents WHERE id = ANY($1::text[])", a_ids
                )
                names_a = {r["id"]: r["name"] for r in rows_a}
            if p_ids:
                rows_p = await con.fetch(
                    "SELECT id, name FROM pipelines WHERE id = ANY($1::text[])", p_ids
                )
                names_p = {r["id"]: r["name"] for r in rows_p}
        except Exception:
            pass  # nomes são decoração — a auditoria funciona sem eles
        for d in items:
            d["agent_name"] = names_a.get(d.get("agent_id"))
            d["pipeline_name"] = names_p.get(d.get("pipeline_id"))
        return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/dashboard/verifications/claims")
async def verification_claims(window: str = "7d", limit: int = 50):
    """Explorador de alucinações (25.0.0): as `unsupported_claims` mais
    recentes da janela, achatadas por afirmação, com o agente dono — o
    operador varre O QUE está sendo dito sem respaldo e por quem."""
    from app.core.database import _get_pool
    limit = max(1, min(int(limit), 200))
    # rejudge fora: sem evidências re-anexadas, os claims do re-julgamento
    # não são comparáveis aos do tráfego real (falso-positivo garantido).
    where = ("WHERE v.unsupported_claims != '[]' AND v.unsupported_claims IS NOT NULL "
             "AND v.profile IS DISTINCT FROM 'rejudge'")
    if window == "24h":
        where += " AND v.created_at > now() - interval '24 hours'"
    elif window == "7d":
        where += " AND v.created_at > now() - interval '7 days'"
    elif window == "30d":
        where += " AND v.created_at > now() - interval '30 days'"
    async with _get_pool().acquire() as con:
        rows = await con.fetch(
            f"""
            SELECT v.id, v.agent_id, a.name AS agent_name, v.pipeline_id,
                   v.interaction_id, v.unsupported_claims, v.created_at
            FROM verifications v
            LEFT JOIN agents a ON a.id = v.agent_id
            {where}
            ORDER BY v.created_at DESC
            LIMIT {limit}
            """
        )
    claims: list[dict] = []
    for r in rows:
        try:
            cs = json.loads(r["unsupported_claims"] or "[]")
        except Exception:
            cs = []
        for c in cs:
            claims.append({
                "claim": c,
                "verification_id": r["id"],
                "agent_id": r["agent_id"],
                "agent_name": r["agent_name"],
                "interaction_id": r["interaction_id"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            })
            if len(claims) >= limit:
                break
        if len(claims) >= limit:
            break
    return {"window": window, "claims": claims}


@router.post("/dashboard/verifications/{vid}/rejudge")
async def rejudge_verification(vid: str, user: dict = Depends(require_user)):
    """Re-julga uma verificação com o juiz ATUAL (25.0.0) — habilita A/B de
    juízes sobre material real: o par pergunta/resposta persistido é
    re-submetido ao MultiDimJudge (papel `judge` do Roteamento) e o novo
    veredito é gravado com profile='rejudge' (identificável e filtrável).

    Custa 1 chamada LLM — root/admin apenas. Evidências não são re-anexadas
    (não são persistidas), então factuality vem null no re-judge; a
    comparação vale para completeness/tone/safety/claims.
    """
    if (user.get("role") or "").lower() not in ("root", "admin"):
        raise HTTPException(403, "Apenas root/admin podem re-julgar.")
    # Fail-fast com v2 OFF: Verifier.verify cairia no _LegacyVerifier, que
    # com evidences=[] devolve veredito ENLATADO (ok=False, confidence=0)
    # sem chamar juiz nenhum e sem persistir — a UI mentiria "re-julgado".
    from app.core.config import get_settings
    if not get_settings().verifier_v2_enabled:
        raise HTTPException(
            409,
            "Verifier v2 está desligado — o re-julgamento usa o MultiDimJudge. "
            "Ative VERIFIER_V2_ENABLED e tente novamente.",
        )
    from app.core.database import _get_pool
    async with _get_pool().acquire() as con:
        row = await con.fetchrow("SELECT * FROM verifications WHERE id = $1", vid)
    if not row:
        raise HTTPException(404, "Verificação não encontrada.")
    if (row["profile"] or "") == "rejudge":
        raise HTTPException(
            409, "Esta linha já é um re-julgamento — re-julgue a linha ORIGINAL."
        )
    if not row["draft_redacted"]:
        raise HTTPException(
            400,
            "Esta verificação não tem o par pergunta/resposta persistido "
            "(anterior à v24.10.0 ou payload já limpo pela retenção) — "
            "não há material para re-julgar.",
        )
    from app.verifier import verifier as _verifier
    from app.agents.engine import _serialize_verification
    v = await _verifier.verify(
        draft=row["draft_redacted"],
        evidences=[],
        user_question=row["question_redacted"] or "",
        profile="rejudge",
        interaction_id=row["interaction_id"],
        agent_id=row["agent_id"],
        pipeline_id=row["pipeline_id"],
        persist=True,
    )
    logger.info(
        "verifications.rejudged",
        extra={
            "event": "verifications.rejudged",
            "verification_id": vid,
            "agent_id": row["agent_id"],
            "new_judge_model": v.judge_model,
        },
    )
    return {
        "status": "ok",
        "original": {
            "id": row["id"],
            "judge_model": row["judge_model"],
            "ok": row["ok"],
            "confidence": row["confidence"],
            "factuality_score": row["factuality_score"],
            "completeness_score": row["completeness_score"],
            "tone_score": row["tone_score"],
            "safety_score": row["safety_score"],
        },
        "rejudged": _serialize_verification(v),
    }


@router.get("/dashboard/verifications/export")
async def export_verifications(
    format: str = "csv",
    window: str = "all",
    agent_id: Optional[str] = None,
    pipeline_id: Optional[str] = None,
    ok_only: bool = False,
    min_factuality: Optional[float] = None,
    profile: Optional[str] = None,
    interaction_id: Optional[str] = None,
    with_claims_only: bool = False,
    user: dict = Depends(require_user),
):
    """Export da auditoria (25.0.0) p/ compliance — CSV ou JSONL, até 5000
    linhas mais recentes. Honra os MESMOS filtros da lista (o arquivo bate
    com o que a tela mostra). Par pergunta/resposta REDACTED (DLP já rodou
    na persistência)."""
    from fastapi.responses import Response
    from app.core.database import _get_pool
    where = []
    args: list = []
    if window == "24h":
        where.append("created_at > now() - interval '24 hours'")
    elif window == "7d":
        where.append("created_at > now() - interval '7 days'")
    elif window == "30d":
        where.append("created_at > now() - interval '30 days'")
    if agent_id:
        args.append(agent_id)
        where.append(f"agent_id = ${len(args)}")
    if pipeline_id:
        args.append(pipeline_id)
        where.append(f"pipeline_id = ${len(args)}")
    if ok_only:
        where.append("ok = TRUE")
    if min_factuality is not None:
        args.append(min_factuality)
        where.append(f"factuality_score >= ${len(args)}")
    if profile:
        args.append(profile)
        where.append(f"profile = ${len(args)}")
    if interaction_id:
        args.append(interaction_id)
        where.append(f"interaction_id = ${len(args)}")
    if with_claims_only:
        where.append("unsupported_claims != '[]' AND unsupported_claims IS NOT NULL")
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    cols = [
        "id", "created_at", "interaction_id", "agent_id", "pipeline_id",
        "ok", "confidence", "factuality_score", "completeness_score",
        "tone_score", "safety_score", "contract_compliant", "contract_retried",
        "unsupported_claims", "judge_model", "profile", "duration_ms",
        "question_redacted", "draft_redacted",
    ]
    async with _get_pool().acquire() as con:
        rows = await con.fetch(
            f"SELECT {', '.join(cols)} FROM verifications {where_clause} "
            f"ORDER BY created_at DESC LIMIT 5000",
            *args,
        )
    if format == "jsonl":
        lines = []
        for r in rows:
            d = dict(r)
            if d.get("created_at"):
                d["created_at"] = d["created_at"].isoformat()
            lines.append(json.dumps(d, ensure_ascii=False, default=str))
        return Response(
            "\n".join(lines),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": "attachment; filename=auditoria.jsonl"},
        )
    # CSV (default)
    import csv
    import io

    def _csv_safe(v):
        # Excel/Sheets executam células iniciadas em = + - @ como FÓRMULA —
        # neutraliza injeção vinda de texto gerado por LLM/usuário.
        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + v
        return v

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([
            (r[c].isoformat() if c == "created_at" and r[c] else _csv_safe(r[c]))
            for c in cols
        ])
    # BOM UTF-8 (﻿): sem ele o Excel abre acentos pt-BR como mojibake.
    _bom = "\N{ZERO WIDTH NO-BREAK SPACE}"
    return Response(
        _bom + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=auditoria.csv"},
    )

# ═══ Releases §18 ═══
@router.get("/releases")
async def list_releases(environment: str = None, limit: int = 20):
    f = {}
    if environment: f["environment"] = environment
    return {"releases": await releases_repo.find_all(limit=limit, **f)}

@router.post("/releases", status_code=201)
async def create_release(data: ReleaseCreate):
    rid = str(uuid.uuid4())
    await releases_repo.create({
        "id": rid, "name": data.name, "environment": data.environment,
        "model_config": data.model_config_data, "prompt_config": data.prompt_config,
        "index_config": data.index_config, "policy_config": data.policy_config,
    })
    return {"id": rid, "message": "Release criada"}

@router.put("/releases/{release_id}/promote")
async def promote_release(release_id: str, target_env: str = "canary"):
    r = await releases_repo.find_by_id(release_id)
    if not r: raise HTTPException(404)
    await releases_repo.update(release_id, {"environment": target_env, "status": target_env})
    await audit_repo.create({"entity_type":"release","entity_id":release_id,"action":f"promoted_to_{target_env}"})
    return {"message": f"Release promovida para {target_env}"}

# ═══ Gold Cases §9.4 ═══
@router.get("/gold-cases")
async def list_gold_cases(dataset_version: str = None, case_type: str = None, limit: int = 50):
    f = {}
    if dataset_version: f["dataset_version"] = dataset_version
    if case_type: f["case_type"] = case_type
    return {"cases": await gold_cases_repo.find_all(limit=limit, **f), "total": await gold_cases_repo.count(**f)}

@router.post("/gold-cases", status_code=201)
async def create_gold_case(data: GoldCaseCreate):
    gid = str(uuid.uuid4())
    payload = data.model_dump()
    # red_flags: lista Python → JSON string (coluna TEXT)
    payload["red_flags"] = json.dumps(payload.get("red_flags") or [])
    await gold_cases_repo.create({"id": gid, **payload})
    return {"id": gid, "message": "Caso adicionado ao Golden Dataset"}

@router.delete("/gold-cases/{case_id}")
async def delete_gold_case(case_id: str):
    if not await gold_cases_repo.delete(case_id): raise HTTPException(404)
    return {"message": "Caso removido"}

# ═══ Harness §9.5 ═══
def _parse_json_field(row: dict, field: str, default):
    """Parsing tolerante: TEXT JSON do banco vira objeto/lista para a UI.
    Mantém valor original em caso de string vazia ou JSON malformado."""
    raw = row.get(field)
    if raw is None or raw == "":
        row[field] = default
        return
    if isinstance(raw, (dict, list)):
        return
    try:
        row[field] = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        row[field] = default


@router.get("/eval-runs")
async def list_eval_runs(release_id: str = None, limit: int = 20):
    f = {}
    if release_id: f["release_id"] = release_id
    runs = await eval_runs_repo.find_all(limit=limit, **f)
    # dimension_breakdown e details vêm como TEXT JSON; UI precisa de objeto.
    for r in runs:
        _parse_json_field(r, "dimension_breakdown", {})
        _parse_json_field(r, "details", [])
    return {"runs": runs}


@router.delete("/eval-runs/{run_id}")
async def delete_eval_run(run_id: str):
    """Remove uma execução de avaliação (housekeeping). Fecha o gap achado no
    E2E 2026-06-23 ("não há DELETE de eval_runs"): runs órfãos — ex.: agente
    deletado → accuracy 0.0 espúria — não podiam ser apagados via API.

    Gating: mantido no MESMO nível dos irmãos do módulo (delete_gold_case e
    /eval-runs/execute são ungated). Role-gating dos mutadores de Avaliação/
    Qualidade é item cross-cutting do backlog de segurança — não introduzido
    aqui isolado (seria inconsistente com o irmão aberto)."""
    if not await eval_runs_repo.delete(run_id):
        raise HTTPException(404, "Execução de avaliação não encontrada")
    return {"message": "Execução removida"}


# ── Comparação side-by-side (§9.5) — Onda 5 ───────────────────────

# Direção de cada métrica: 'up' = maior é melhor; 'down' = menor é melhor.
# UI usa pra escolher cor (green vs rose) do delta automaticamente.
_METRIC_DIRECTIONS = {
    "accuracy": "up",
    "accuracy_unweighted": "up",
    "avg_factuality": "up",
    "avg_completeness": "up",
    "avg_tone": "up",
    "contract_compliance_rate": "up",
    "correct_refusal_rate": "up",
    "safety_violation_rate": "down",
    "hallucination_rate": "down",
    "false_positive_rate": "down",
    "avg_latency_ms": "down",
}


def _summary_of_run(run: dict) -> dict:
    """Sumário leve de um eval_run para o response (sem details cruas)."""
    return {
        "id": run.get("id"),
        "release_id": run.get("release_id"),
        "run_type": run.get("run_type"),
        "gold_version": run.get("gold_version"),
        "status": run.get("status"),
        "gate_result": run.get("gate_result"),
        "gate_reason": run.get("gate_reason"),
        "judge_used": bool(run.get("judge_used")),
        "judge_model": run.get("judge_model"),
        "total_cases": run.get("total_cases"),
        "passed": run.get("passed"),
        "failed": run.get("failed"),
        "accuracy": run.get("accuracy"),
        "avg_factuality": run.get("avg_factuality"),
        "avg_completeness": run.get("avg_completeness"),
        "avg_tone": run.get("avg_tone"),
        "safety_violation_rate": run.get("safety_violation_rate"),
        "contract_compliance_rate": run.get("contract_compliance_rate"),
        "hallucination_rate": run.get("hallucination_rate"),
        "correct_refusal_rate": run.get("correct_refusal_rate"),
        "false_positive_rate": run.get("false_positive_rate"),
        "avg_latency_ms": run.get("avg_latency_ms"),
        "created_at": run.get("created_at"),
    }


def _compute_delta(a, b, direction: str) -> dict:
    """Delta b-a com flag is_improvement quando ambos não-null.
    is_improvement: True/False quando há melhora/piora real; None quando
    delta=0 ou alguma ponta é null (UI mostra surface-400)."""
    if a is None or b is None:
        return {"a": a, "b": b, "delta": None, "is_improvement": None}
    a_f, b_f = float(a), float(b)
    delta = b_f - a_f
    if delta == 0:
        is_improvement = None
    else:
        is_improvement = (delta > 0) if direction == "up" else (delta < 0)
    return {
        "a": a_f, "b": b_f,
        "delta": round(delta, 4),
        "is_improvement": is_improvement,
    }


def _aggregate_deltas(run_a: dict, run_b: dict) -> dict:
    """Deltas agregados pra todas métricas em _METRIC_DIRECTIONS."""
    return {
        m: _compute_delta(run_a.get(m), run_b.get(m), direction)
        for m, direction in _METRIC_DIRECTIONS.items()
    }


def _by_category_deltas(run_a: dict, run_b: dict) -> dict:
    """Deltas por categoria. Lê dimension_breakdown (já parseado por
    list_eval_runs ou via _parse_json_field aqui). Categoria presente em
    apenas um dos lados aparece com null no outro."""
    def _cats(run):
        db = run.get("dimension_breakdown") or {}
        if isinstance(db, str):
            try:
                db = json.loads(db)
            except (json.JSONDecodeError, TypeError):
                db = {}
        return (db or {}).get("by_category") or {}

    cats_a = _cats(run_a)
    cats_b = _cats(run_b)
    all_cats = sorted(set(cats_a) | set(cats_b))
    out = {}
    for cat in all_cats:
        a_cat = cats_a.get(cat) or {}
        b_cat = cats_b.get(cat) or {}
        out[cat] = {
            "total_a": a_cat.get("total"), "total_b": b_cat.get("total"),
            "passed_a": a_cat.get("passed"), "passed_b": b_cat.get("passed"),
            "accuracy": _compute_delta(a_cat.get("accuracy"), b_cat.get("accuracy"), "up"),
            "avg_factuality": _compute_delta(a_cat.get("avg_factuality"), b_cat.get("avg_factuality"), "up"),
            "avg_completeness": _compute_delta(a_cat.get("avg_completeness"), b_cat.get("avg_completeness"), "up"),
            "avg_tone": _compute_delta(a_cat.get("avg_tone"), b_cat.get("avg_tone"), "up"),
        }
    return out


def _divergent_cases(run_a: dict, run_b: dict, limit: int = 20) -> list:
    """Cruza details[].case_id; retorna casos onde passed flip.
    Ordena: regressões (passed em A, falhou em B) antes de melhorias."""
    def _details(run):
        d = run.get("details") or []
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except (json.JSONDecodeError, TypeError):
                d = []
        return d if isinstance(d, list) else []

    by_id_a = {c.get("case_id"): c for c in _details(run_a) if c.get("case_id")}
    by_id_b = {c.get("case_id"): c for c in _details(run_b) if c.get("case_id")}
    common_ids = set(by_id_a) & set(by_id_b)

    flips = []
    for cid in common_ids:
        a, b = by_id_a[cid], by_id_b[cid]
        passed_a, passed_b = bool(a.get("passed")), bool(b.get("passed"))
        if passed_a == passed_b:
            continue
        flips.append({
            "case_id": cid,
            "category": a.get("category") or b.get("category") or "(sem categoria)",
            "expected_state": a.get("expected_state") or b.get("expected_state"),
            "regression": passed_a and not passed_b,
            "a": {
                "passed": passed_a,
                "actual_state": a.get("actual_state"),
                "factuality": a.get("factuality"),
                "completeness": a.get("completeness"),
                "tone": a.get("tone"),
                "safety": a.get("safety"),
                "failure_reasons": a.get("failure_reasons", []),
            },
            "b": {
                "passed": passed_b,
                "actual_state": b.get("actual_state"),
                "factuality": b.get("factuality"),
                "completeness": b.get("completeness"),
                "tone": b.get("tone"),
                "safety": b.get("safety"),
                "failure_reasons": b.get("failure_reasons", []),
            },
        })

    # Regressões primeiro, depois melhorias; tie-break por case_id.
    flips.sort(key=lambda f: (not f["regression"], f["case_id"]))
    return flips[:limit]


@router.get("/eval-runs/compare")
async def compare_eval_runs(a: str, b: str):
    """Compara dois eval_runs side-by-side. Valida gold_version + status.

    Retorna {run_a, run_b, comparable, comparable_reason, deltas,
    by_category_deltas, divergent_cases}. Quando comparable=false, os
    três últimos vêm vazios — UI mostra banner com reason.
    """
    if a == b:
        raise HTTPException(400, "Os dois IDs precisam ser diferentes")

    run_a = await eval_runs_repo.find_by_id(a)
    run_b = await eval_runs_repo.find_by_id(b)
    if not run_a or not run_b:
        raise HTTPException(404, "Um ou ambos eval_runs não encontrados")

    # dimension_breakdown e details vêm como TEXT JSON; parsear in-place.
    for r in (run_a, run_b):
        _parse_json_field(r, "dimension_breakdown", {})
        _parse_json_field(r, "details", [])

    comparable = True
    reason = None
    if run_a.get("status") != "completed" or run_b.get("status") != "completed":
        comparable = False
        reason = (
            f"runs precisam estar completed: "
            f"a.status={run_a.get('status')!r}, b.status={run_b.get('status')!r}"
        )
    elif run_a.get("gold_version") != run_b.get("gold_version"):
        comparable = False
        reason = (
            f"datasets diferentes: a={run_a.get('gold_version')!r}, "
            f"b={run_b.get('gold_version')!r}. Comparar runs em datasets "
            "diferentes não tem significado estatístico."
        )
    elif (
        run_a.get("gold_hash") and run_b.get("gold_hash")
        and run_a.get("gold_hash") != run_b.get("gold_hash")
    ):
        # Q6 (33.9.0): mesmo rótulo gold_version, mas o CONTEÚDO do gold mudou
        # entre os runs (casos editados) → comparação sem significado. O hash pega
        # o que o rótulo texto-livre não pegava. (Runs antigos sem hash: pula.)
        comparable = False
        reason = (
            f"o CONTEÚDO do Golden Dataset MUDOU entre os runs (mesmo rótulo "
            f"{run_a.get('gold_version')!r}, hashes diferentes: "
            f"a={run_a.get('gold_hash')}, b={run_b.get('gold_hash')}). Re-rode o "
            "baseline no gold atual antes de comparar."
        )

    response = {
        "run_a": _summary_of_run(run_a),
        "run_b": _summary_of_run(run_b),
        "comparable": comparable,
        "comparable_reason": reason,
        "deltas": {},
        "by_category_deltas": {},
        "divergent_cases": [],
    }
    if comparable:
        response["deltas"] = _aggregate_deltas(run_a, run_b)
        response["by_category_deltas"] = _by_category_deltas(run_a, run_b)
        response["divergent_cases"] = _divergent_cases(run_a, run_b, limit=20)
    return response

@router.get("/dashboard/verifier/async-stats")
async def verifier_async_stats():
    """Snapshot dos counters do dispatcher async + config corrente.

    Cross-worker: cada worker tem contadores próprios (in-process).
    Dashboard mostra os do worker que respondeu o GET — operador deve
    ler como amostra, não verdade absoluta em deploys multi-worker.
    """
    # Imports lazy: o módulo do dispatcher tem state asyncio que não deve
    # ser tocado no import do router (que carrega antes do lifespan).
    from app.verifier.async_dispatcher import stats_snapshot
    from app.core.config import get_settings as _gs
    s = _gs()
    return {
        "stats": stats_snapshot(),
        "config": {
            "enabled": bool(s.verifier_v2_enabled and s.verifier_production_async),
            "sample_rate": s.verifier_production_sample_rate,
            "max_concurrent_jobs": s.verifier_max_concurrent_jobs,
        },
    }


# ─── LLM Routing por Task Type (Onda 7) ────────────────────────────

class LLMRoutingUpdate(BaseModel):
    """Payload do PUT /llm-routing. Subset — só keys mencionadas mudam."""
    tool_calling: Optional[str] = None
    reasoning: Optional[str] = None
    instruct: Optional[str] = None
    classification: Optional[str] = None
    skill_generation: Optional[str] = None
    # Papel "LLM como Juiz" (Verifier §14.2/MultiDimJudge). Rota salva aqui
    # vence a env legada VERIFIER_JUDGE_MODEL (ver _apply_judge_env_default).
    judge: Optional[str] = None
    multimodal_fallback: Optional[str] = None
    # Checkbox "Mostrar contingência na rastreabilidade" (bloco Multimodal
    # Fallback). Controla SOMENTE a nota VISÍVEL no painel do Workspace; a
    # observabilidade (metadata) e os LOGs do fallback são SEMPRE gravados,
    # independente deste flag. Persistido via settings_store, não save_routing
    # (que só aceita strings provider/model).
    fallback_show_in_trace: Optional[bool] = None


@router.get("/dashboard/llm-routing")
async def get_llm_routing():
    """Retorna o routing config atual + defaults + lista de task types."""
    from app.llm_routing import (
        load_routing, DEFAULT_ROUTING, TASK_TYPES, global_primary_routing,
        fallback_show_in_trace, _apply_judge_env_default,
    )
    routing = await load_routing()
    # skill_generation: default efetivo segue o Modelo Primário global da
    # plataforma (não o hardcoded). Reflete no botão "padrões recomendados".
    defaults = dict(DEFAULT_ROUTING)
    gm = global_primary_routing()
    if gm:
        defaults["skill_generation"] = gm
    # judge: default efetivo honra a env legada VERIFIER_JUDGE_MODEL — sem
    # isso o botão "Aplicar padrões" sobrescreveria a env do operador com o
    # hardcoded azure/gpt-4o (explicit vazio = "nenhuma rota salva na UI").
    _apply_judge_env_default(defaults, set())
    return {
        "routing": routing,
        "defaults": defaults,
        # Flag do checkbox "Mostrar contingência na rastreabilidade" (bloco
        # Multimodal Fallback). Default True = transparência. Só afeta a nota
        # visível; observabilidade/LOGs do fallback são sempre registrados.
        "fallback_show_in_trace": await fallback_show_in_trace(),
        "task_types": list(TASK_TYPES),
        "task_descriptions": {
            "tool_calling": "Uso de ferramentas (function calls). Pra tarefas complexas que precisam invocar APIs/MCPs externas. Default: GPT-OSS-120B (open-weight via hub interno).",
            "reasoning": "Texto com raciocínio. Pra tarefas que exigem análise/explicação em PT-BR. Default: GPT-OSS-120B (open-weight via hub interno).",
            "instruct": "Apenas texto (instruction following). Inferência comum. **Aceita imagens** — quando input é multimodal, plataforma roteia automaticamente pro multimodal_fallback. Default: GPT-OSS-20B (open-weight via hub interno).",
            "classification": "Classificação e categorização. Estruturação de informações em labels/buckets fixos. Default: GPT-OSS-20B (open-weight via hub interno).",
            "skill_generation": "Criação e alteração de SKILL.md no Wizard. Como o SKILL.md precisa respeitar um formato rígido, escolha um modelo forte em seguir instruções e gerar saída estruturada (JSON). Default: o modelo global da plataforma (Modelo Primário) — você pode trocar aqui a qualquer momento. O esforço de raciocínio dessa geração é configurável em Parâmetros → 'Esforço de raciocínio do Wizard' e só se aplica a modelos que suportam (gpt-oss sempre; Azure/OpenAI só o1/o3/o4/gpt-5 — gpt-4o/gpt-4.1 ignoram sem erro).",
            "judge": "LLM como Juiz (Verifier): avalia cada resposta dos agentes em 4 dimensões — factualidade, completude, tom e segurança — e alimenta as páginas Qualidade e o gate de release do Harness. Escolha um modelo forte em análise e saída JSON. Default: Azure GPT-4o (ou a variável VERIFIER_JUDGE_MODEL, se configurada no ambiente).",
            "multimodal_fallback": "Modelo usado quando o input contém imagem mas o modelo da task escolhida é text-only. Default: Azure GPT-4o (único multimodal nativo pronto pra produção).",
        },
    }


@router.put("/dashboard/llm-routing")
async def put_llm_routing(data: LLMRoutingUpdate, user: dict = Depends(require_user)):
    """Atualiza routing config. Aceita subset (só keys não-None mudam).
    Cada valor de roteamento é validado como `provider/model` antes de persistir.

    Gate por ROLE de verdade (24.8.0): o Roteamento LLM muda o modelo de TODA
    a plataforma — root/admin apenas. Antes o gate era só cosmético (aba
    escondida no template); a API aceitava qualquer usuário autenticado.

    `fallback_show_in_trace` (bool) é tratado à parte: persiste via settings_store
    (não save_routing, que só aceita strings provider/model). Controla apenas a
    nota visível no painel — observabilidade/LOGs do fallback são sempre gravados.
    """
    if (user.get("role") or "").lower() not in ("root", "admin"):
        raise HTTPException(
            403, "Apenas root/admin podem alterar o Roteamento LLM."
        )
    from app.llm_routing import (
        save_routing, set_fallback_show_in_trace, fallback_show_in_trace,
    )
    raw = {k: v for k, v in data.model_dump().items() if v is not None}
    # separa o flag booleano dos campos de roteamento (strings provider/model)
    show_in_trace = raw.pop("fallback_show_in_trace", None)
    if not raw and show_in_trace is None:
        raise HTTPException(400, "Nenhum campo para atualizar")
    updated: list[str] = []
    # save_routing({}) é no-op de escrita e só relê o estado atual mesclado com
    # defaults — usamos isso pra sempre devolver `routing` mesmo quando só o flag
    # mudou.
    final = await save_routing(raw)
    if raw:
        updated.extend(raw.keys())
    if show_in_trace is not None:
        await set_fallback_show_in_trace(bool(show_in_trace))
        updated.append("fallback_show_in_trace")
        show_now = bool(show_in_trace)
    else:
        show_now = await fallback_show_in_trace()
    return {
        "routing": final,
        "fallback_show_in_trace": show_now,
        "updated": updated,
    }


@router.post("/eval-runs/execute")
async def run_harness(data: RunEvalRequest):
    """Executa harness de avaliação contra dataset gold §9.5."""
    # Valida que release E agente existem ANTES de rodar. Sem isso, ids
    # inexistentes ainda geram um eval_run "lixo" (completed, accuracy 0.0)
    # que não pode ser deletado (não há DELETE de eval_runs).
    if not await releases_repo.find_by_id(data.release_id):
        raise HTTPException(404, f"Release '{data.release_id}' não encontrada.")
    if not await agents_repo.find_by_id(data.agent_id):
        raise HTTPException(404, f"Agente '{data.agent_id}' não encontrado.")
    from app.harness.evaluator import run_evaluation
    try:
        result = await run_evaluation(data.release_id, data.agent_id, data.gold_version, data.run_type)
        return result
    except Exception as e:
        raise HTTPException(500, f"Erro no harness: {str(e)}")

# ═══ Knowledge Sources §14 ═══
@router.get("/knowledge-sources")
async def list_knowledge_sources(limit: int = 50):
    return {"sources": await knowledge_repo.find_all(limit=limit)}

@router.get("/knowledge-sources/{ks_id}")
async def get_knowledge_source(ks_id: str):
    s = await knowledge_repo.find_by_id(ks_id)
    if not s: raise HTTPException(404)
    return s

@router.post("/knowledge-sources", status_code=201)
async def create_knowledge_source(data: KnowledgeSourceCreate):
    kid = str(uuid.uuid4())
    await knowledge_repo.create({"id": kid, **data.model_dump()})
    return {"id": kid, "message": "Base de conhecimento registrada"}

@router.put("/knowledge-sources/{ks_id}")
async def update_knowledge_source(ks_id: str, data: KnowledgeSourceCreate):
    if not await knowledge_repo.find_by_id(ks_id): raise HTTPException(404)
    upd = {k: v for k, v in data.model_dump().items() if v is not None}
    return await knowledge_repo.update(ks_id, upd)

@router.delete("/knowledge-sources/{ks_id}")
async def delete_knowledge_source(ks_id: str):
    if not await knowledge_repo.delete(ks_id): raise HTTPException(404)
    return {"message": "Base removida"}

# ─── Onda 3: ingestão de documentos em knowledge_sources ────────
class IngestTextRequest(BaseModel):
    text: str
    replace: bool = True
    # Override por ingestão dos defaults do .env (RAG_CHUNK_SIZE_TOKENS=500,
    # RAG_CHUNK_OVERLAP_TOKENS=50). None mantém default. Útil pra documentos
    # com características diferentes — ex: código curto pede chunks pequenos,
    # narrativa longa beneficia de chunks grandes.
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None


@router.get("/rag-config")
async def rag_config():
    """Defaults de chunking lidos do .env. UI usa pra mostrar placeholder
    nos inputs avançados de ingestão."""
    from app.core.config import get_settings as _get_settings
    s = _get_settings()
    return {
        "chunk_size": s.rag_chunk_size_tokens,
        "chunk_overlap": s.rag_chunk_overlap_tokens,
    }


@router.post("/knowledge-sources/{ks_id}/ingest")
async def ingest_into_source(ks_id: str, data: IngestTextRequest):
    """Ingere texto cru: chunca → embeda (Azure) → grava chunks (Postgres) +
    pontos vetoriais (Qdrant). Idempotente quando replace=True."""
    from app.evidence.ingest import ingest_text, IngestError
    try:
        return await ingest_text(
            ks_id, data.text, replace=data.replace,
            chunk_size=data.chunk_size, chunk_overlap=data.chunk_overlap,
        )
    except IngestError as e:
        raise HTTPException(e.status_code, str(e))


# ─── Onda 6 RAG Core: ingestão multi-formato (markitdown) ────────────

class IngestUrlRequest(BaseModel):
    url: str
    replace: bool = True
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None


@router.post("/knowledge-sources/{ks_id}/ingest-file")
async def ingest_file_into_source(
    ks_id: str,
    file: UploadFile = File(...),
    replace: bool = Form(True),
    chunk_size: Optional[int] = Form(None),
    chunk_overlap: Optional[int] = Form(None),
):
    """Ingere arquivo (PDF/DOCX/PPTX/XLSX/HTML/MD/TXT/CSV/JSON/XML/EPUB/MSG/
    ZIP/imagem/áudio) via markitdown → markdown → chunk → embed → store.

    Onda Tabular — respeita kb_mode da KS:
    - 'text' (legacy ou explícito): processa normalmente via markitdown.
    - 'tabular': REJEITA formatos não-estruturados (PDF/DOC/áudio/etc) com 400.
      Aceita só CSV/XLSX e devolve {skipped: true} — não cria chunks. O cliente
      deve chamar /promote-to-table em seguida pra criar a data_table.
    - 'hybrid' (default backward-compat): processa via markitdown E cliente
      pode opcionalmente promover a tabela depois.

    Limite de tamanho não imposto aqui (FastAPI upload size depende do
    middleware/proxy). Para arquivos grandes use replace=true e ingestão em
    lotes; markitdown processa em memória.
    """
    from app.evidence.ingest import ingest_file, IngestError

    # Resolve kb_mode da KS para decidir se aceita o formato
    ks = await knowledge_repo.find_by_id(ks_id)
    if not ks:
        raise HTTPException(404, f"knowledge_source '{ks_id}' não encontrada.")
    kb_mode = (ks.get("kb_mode") or "hybrid").lower()
    filename = file.filename or "upload.bin"
    is_tabular_file = filename.lower().endswith((".csv", ".xlsx", ".xls"))

    if kb_mode == "tabular":
        if not is_tabular_file:
            raise HTTPException(
                400,
                f"Esta base é do tipo 'tabular' (só aceita planilhas estruturadas). "
                f"Arquivo '{filename}' não é CSV/XLSX. Para outros formatos, "
                f"crie uma base do tipo 'texto' ou 'misto'.",
            )
        # Aceita o upload mas NÃO processa via markitdown — caller deve chamar
        # /promote-to-table. Devolve resposta que sinaliza skip + filename pra
        # frontend reutilizar.
        return {
            "skipped_rag": True,
            "kb_mode": "tabular",
            "filename": filename,
            "size_bytes": file.size or 0,
            "message": (
                "KS tabular: arquivo aceito mas não chunkado/embedado. "
                "Chame /promote-to-table para criar a data_table."
            ),
            "chunks_created": 0,
            "tokens_total": 0,
            "duration_ms": 0,
        }

    try:
        data = await file.read()
        return await ingest_file(
            source_id=ks_id,
            data=data,
            filename=filename,
            mime_type=file.content_type,
            replace=replace,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    except IngestError as e:
        raise HTTPException(e.status_code, str(e))


@router.post("/knowledge-sources/{ks_id}/ingest-url")
async def ingest_url_into_source(ks_id: str, data: IngestUrlRequest):
    """Ingere URL (página web, PDF hospedado, YouTube transcript, RSS feed)
    via markitdown.convert_url → markdown → pipeline padrão.

    Aceita apenas http/https. Markitdown faz fetch internamente — sem
    autenticação custom. Para URLs com auth, use ingest-file e baixe antes.
    """
    from app.evidence.ingest import ingest_url, IngestError
    try:
        return await ingest_url(
            ks_id, data.url, replace=data.replace,
            chunk_size=data.chunk_size, chunk_overlap=data.chunk_overlap,
        )
    except IngestError as e:
        raise HTTPException(e.status_code, str(e))


@router.get("/knowledge-sources/{ks_id}/stats")
async def source_stats_endpoint(ks_id: str):
    """Estatísticas operacionais da fonte para a UI.

    Campos legados (compat com clients anteriores):
        chunks_count, tokens_total, last_chunk_at, last_updated, index_version

    Campos adicionados em 2026-05-31 (PR #226 — card de KB tabular não
    mostrava info de tabelas):
        tables_count: int          — quantas data_tables apontam para esta KB
        tables_rows_total: int     — soma de row_count das tabelas
        tables_size_bytes: int     — soma de size_bytes dos arquivos .duckdb
        last_table_at: str | None  — ISO timestamp da promoção mais recente

    Para KB em `kb_mode='tabular'`, `chunks_count` é sempre 0 (correto —
    elas não usam RAG textual). A UI precisa olhar `tables_*` para saber
    se a KB tem conteúdo. KB `hybrid` tem ambos.
    """
    from app.evidence.ingest import source_stats
    if not await knowledge_repo.find_by_id(ks_id):
        raise HTTPException(404, "knowledge_source não encontrada")
    stats = await source_stats(ks_id) or {}

    # Aglutina contagem/tamanho/timestamp das tabelas. Query direta no pool —
    # mais eficiente que list_for_user (que monta dicts completos por linha).
    from app.core.database import _get_pool
    pool = _get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """
            SELECT
                COUNT(*)                       AS tables_count,
                COALESCE(SUM(row_count), 0)    AS tables_rows_total,
                COALESCE(SUM(size_bytes), 0)   AS tables_size_bytes,
                MAX(created_at)                AS last_table_at
            FROM data_tables
            WHERE knowledge_source_id = $1
              AND status != 'deleted'
            """,
            ks_id,
        )
    if row:
        stats["tables_count"] = int(row["tables_count"] or 0)
        stats["tables_rows_total"] = int(row["tables_rows_total"] or 0)
        stats["tables_size_bytes"] = int(row["tables_size_bytes"] or 0)
        last_at = row["last_table_at"]
        stats["last_table_at"] = last_at.isoformat() if last_at else None
    else:
        stats.setdefault("tables_count", 0)
        stats.setdefault("tables_rows_total", 0)
        stats.setdefault("tables_size_bytes", 0)
        stats.setdefault("last_table_at", None)
    return stats


@router.delete("/knowledge-sources/{ks_id}/chunks")
async def clear_source_chunks(ks_id: str):
    """Apaga todos os chunks de uma source (Postgres + Qdrant). Idempotente."""
    from app.evidence.ingest import clear_source
    if not await knowledge_repo.find_by_id(ks_id):
        raise HTTPException(404, "knowledge_source não encontrada")
    return await clear_source(ks_id)


@router.get("/knowledge-sources/{ks_id}/chunks")
async def list_source_chunks(ks_id: str, limit: int = 50, offset: int = 0):
    """Lista chunks de uma source (debug/UI)."""
    from app.evidence.ingest import list_chunks
    if not await knowledge_repo.find_by_id(ks_id):
        raise HTTPException(404, "knowledge_source não encontrada")
    chunks = await list_chunks(ks_id, limit=limit, offset=offset)
    return {"source_id": ks_id, "count": len(chunks), "chunks": chunks}


# ─── Multi-doc (PR #227) ──────────────────────────────────────────


@router.get("/knowledge-sources/{ks_id}/documents")
async def list_source_documents_endpoint(ks_id: str):
    """Lista documentos ingeridos agrupados por `source_doc_id`.

    Chunks ingeridos antes da PR #227 aparecem como um único documento
    "legacy" com `is_legacy=True`. UI pode tratar diferenciado.
    """
    from app.evidence.ingest import list_documents_for_source
    if not await knowledge_repo.find_by_id(ks_id):
        raise HTTPException(404, "knowledge_source não encontrada")
    docs = await list_documents_for_source(ks_id)
    return {"source_id": ks_id, "count": len(docs), "documents": docs}


@router.delete("/knowledge-sources/{ks_id}/documents/{doc_id}")
async def delete_source_document_endpoint(ks_id: str, doc_id: str):
    """Apaga todos os chunks de um documento específico, preservando outros
    documentos da mesma KB.

    `doc_id == '_legacy_'` apaga chunks sem metadata (anteriores à PR #227).
    Idempotente: doc inexistente → `chunks_deleted=0`.
    """
    from app.evidence.ingest import delete_document
    if not await knowledge_repo.find_by_id(ks_id):
        raise HTTPException(404, "knowledge_source não encontrada")
    return await delete_document(ks_id, doc_id)


# ─── Reindex global (recreate vector store + re-embarcar Postgres) ──

@router.get("/evidence/collection-info")
async def evidence_collection_info():
    """Diagnóstico do vector store pgvector (único backend desde Onda Q).

    UI usa pra detectar o cenário "trocou provider sem reindexar" e oferecer
    o botão de reindex com contexto ("Collection dim=1536, provider espera 1024").

    Returns:
        {
          "name": str,
          "exists": bool,
          "points_count": int | None,
          "status": str,                # "green" | "missing" | "drift"
          "dim_actual": int | None,
          "dim_expected": int,
          "dim_match": bool,
          "backend": "pgvector",
        }
        ou 503 se pgvector inacessível.

    Onda Q (2026-05-30): backend único pgvector. Antes havia branch
    pra qdrant via `rag_vector_backend` — Qdrant removido.
    """
    from app.evidence.pgvector_store import collection_info as _ci
    info = await _ci()
    if info is None:
        raise HTTPException(503, "pgvector offline ou inacessível")
    info.setdefault("backend", "pgvector")
    return info


class ReindexRequest(BaseModel):
    # Default True: caso normal é trocou provider → precisa recriar pra mudar dim.
    # False só pra cenário de "popular collection vazia recém-criada".
    recreate_collection: bool = True
    # Batch size do embed+upsert. Ajuste se rate-limit do provider apertar.
    batch_size: int = 64


@router.post("/evidence/reindex")
async def reindex_evidence(data: ReindexRequest | None = None):
    """Reindexa todos os chunks: dropa e recria a collection do Qdrant com a
    dimensão do embedder ATIVO, depois re-embarca todos os chunks do Postgres
    (que é o source-of-truth do texto).

    Quando usar:
    - Trocou o provider de embedding via UI (Azure 1536 → Qwen3 1024 etc.)
    - Sintoma na ingestão: banner "Qdrant divergente" persistente
    - Após restore de backup do Postgres (Qdrant pode estar dessincronizado)

    Operação destrutiva (apaga vetores do Qdrant). Postgres NÃO é tocado.

    Body opcional:
        {"recreate_collection": bool = true, "batch_size": int = 64}

    Returns:
        Dict com métricas detalhadas — ver `app.evidence.ingest.reindex_all`.
        HTTP 200 sempre, mesmo se houver erros parciais (ver campo `ok` e `errors`).
    """
    from app.evidence.ingest import reindex_all
    req = data or ReindexRequest()
    return await reindex_all(
        recreate_collection=req.recreate_collection,
        batch_size=req.batch_size,
    )


class KBQueryRequest(BaseModel):
    query: str
    top_n: int = 5


@router.post("/knowledge-sources/{ks_id}/query")
async def query_knowledge_source(ks_id: str, data: KBQueryRequest):
    """Executa retrieval híbrido (BM25 + vetorial + RRF) restrito a UMA KB.

    Usado pela UI de inspeção pra que o operador teste se a KB está
    respondendo bem antes de cabear num agente real. Retorna chunks
    com scores ordenados por relevância.
    """
    from app.evidence.runtime import Retriever
    if not await knowledge_repo.find_by_id(ks_id):
        raise HTTPException(404, "knowledge_source não encontrada")
    if not data.query or not data.query.strip():
        raise HTTPException(400, "Query vazia")

    retriever = Retriever()
    # Onda Q (2026-05-30): Retriever expõe `search()` (consistente com
    # _bm25_search / _vector_search / _legacy_search internos). Caller original
    # chamava `.retrieve()` que nunca existiu — bug latente desde a Onda 3
    # porque o endpoint não tinha teste. PR #222 corrigiu.
    results = await retriever.search(
        query=data.query.strip(),
        top_n=max(1, min(data.top_n, 20)),
        allowed_source_ids=[ks_id],  # filtra só nesta KB
    )
    return {
        "source_id": ks_id,
        "query": data.query.strip(),
        "results": [
            {
                "evidence_id": r.evidence_id,
                "snippet_text": r.snippet_text,
                "relevance_score": round(r.relevance_score, 4) if r.relevance_score is not None else None,
                "source_name": r.source_name,
                "confidentiality": r.confidentiality,
            }
            for r in results
        ],
        "count": len(results),
    }


@router.get("/rag/health")
async def rag_health():
    """Diagnóstico do stack RAG: pgvector collection info.

    Onda Q (2026-05-30): backend único pgvector (Qdrant removido).
    Resposta preserva `qdrant_collection` como nome do campo por compat
    de clients antigos — valor agora vem do pgvector.
    """
    from app.evidence.pgvector_store import collection_info
    info = await collection_info()
    # Drift de dimensão em DESTAQUE (incidente do seeding Aurora, 2026-07-01):
    # coluna vector(1536) + modelo ativo 1024 fazia o upsert falhar silencioso
    # (ingest partial=true) e a busca degradar para BM25-only sem ninguém ver.
    # UI e monitores leem estes campos de topo sem escavar o objeto aninhado.
    status = (info or {}).get("status") or "unavailable"
    dim_drift = bool(info) and info.get("dim_match") is False
    return {
        "qdrant_collection": info,  # nome legacy preservado pra back-compat
        "vector_collection": info,  # nome backend-neutro novo
        "backend": "pgvector",
        "rag_available": info is not None,
        "status": status,
        "dim_actual": (info or {}).get("dim_actual"),
        "dim_expected": (info or {}).get("dim_expected"),
        "dim_drift": dim_drift,
        "points_count": (info or {}).get("points_count"),
        "hint": (
            "Coluna de embeddings com dimensão divergente do modelo ativo — "
            "busca vetorial desativada (fallback BM25). Reindexe em /rag "
            "(botão Reindexar) ou POST /api/v1/evidence/reindex."
        ) if dim_drift else None,
    }


# ═══ Tools / Tool Registry §10 ═══
def _strip_secrets_from_tool(t: dict) -> dict:
    """Remove credenciais (auth_token e secrets em auth_config) da response.

    PR #229: o GET retornava `auth_token` cifrado (`fernet:gAAAA...`) e a UI
    reenviava no PUT sem editar, causando duplo cifragem no banco e quebra
    silenciosa do MCP (401). Solução: nunca emitir o ciphertext na rede.

    PR #230: adiciona `auth_token_fingerprint` (8 chars do SHA-256 do
    PLAINTEXT) para a UI mostrar evidência visual de que o token está
    armazenado. Quando o operador troca o token, o fingerprint muda — sinal
    claro de "sim, foi salvo". Sem isso, o input volta a aparecer vazio
    após salvar e o operador acha que não persistiu.

    Em vez de remover o campo (quebraria clients antigos), substitui por
    string vazia e adiciona flags `has_auth_token` / `has_auth_config_secrets`
    e fingerprints para a UI mostrar "(token armazenado · ab12cd34)".

    auth_config pode ter client_secret/client_key/ca_cert (OAuth2/mTLS).
    Esses campos são removidos do JSON mas o resto da config (client_id,
    token_url, scope) permanece para a UI rehidratar.
    """
    import json as _json
    from app.core.secrets import fingerprint as _fp
    out = dict(t)
    token = out.get("auth_token") or ""
    out["has_auth_token"] = bool(token)
    out["auth_token_fingerprint"] = _fp(token) if token else ""
    out["auth_token"] = ""
    cfg_raw = out.get("auth_config") or "{}"
    try:
        cfg = _json.loads(cfg_raw) if isinstance(cfg_raw, str) else dict(cfg_raw)
    except (ValueError, TypeError):
        cfg = {}
    SECRET_KEYS = ("client_secret", "client_key", "ca_cert")
    has_secrets = any(cfg.get(k) for k in SECRET_KEYS)
    # PR #230: fingerprints por secret para que a UI mostre "saved · ab12cd34"
    # em cada campo separado quando OAuth2/mTLS tem múltiplos secrets.
    out["auth_config_fingerprints"] = {
        k: (_fp(cfg.get(k) or "") if cfg.get(k) else "")
        for k in SECRET_KEYS
    }
    for k in SECRET_KEYS:
        if k in cfg:
            cfg[k] = ""
    out["auth_config"] = _json.dumps(cfg, ensure_ascii=False)
    out["has_auth_config_secrets"] = has_secrets
    return out


@router.get("/tools")
async def list_tools(limit: int = 50, sensitivity: str = None):
    f = {}
    if sensitivity: f["sensitivity"] = sensitivity
    rows = await tools_repo.find_all(limit=limit, **f)
    return {
        "tools": [_strip_secrets_from_tool(t) for t in rows],
        "total": await tools_repo.count(**f),
    }

@router.get("/tools/{tool_id}")
async def get_tool(tool_id: str):
    t = await tools_repo.find_by_id(tool_id)
    if not t: raise HTTPException(404, "Tool não encontrada")
    return _strip_secrets_from_tool(t)

@router.post("/tools", status_code=201)
async def create_tool(data: ToolCreate):
    """Cria tool. Loga payload recebido para diagnóstico."""
    from app.core.secrets import write_secret
    tid = str(uuid.uuid4())
    payload = data.model_dump()
    logger.info(f"create_tool: name={payload.get('name')!r} mcp_server={payload.get('mcp_server')!r}")
    d = {"id": tid, **payload}
    d["requires_trusted_context"] = 1 if data.requires_trusted_context else 0
    # Cifra credencial em repouso (Fernet) — texto plano nunca toca o banco
    if d.get("auth_token"):
        d["auth_token"] = write_secret(d["auth_token"])
    try:
        await tools_repo.create(d)
        await audit_repo.create({"entity_type":"tool","entity_id":tid,"action":"created","details":json.dumps({"name":data.name,"mcp_server":data.mcp_server})})
        logger.info(f"create_tool: id={tid} criada com sucesso")
        return {"id": tid, "message": "Tool registrada", "name": data.name}
    except Exception as e:
        logger.error(f"create_tool: falha — {e}")
        raise HTTPException(500, f"Erro ao registrar tool: {str(e)}")

@router.put("/tools/{tool_id}")
async def update_tool(tool_id: str, data: ToolUpdate):
    """Atualiza tool.

    CORREÇÕES:
    - Usa model_dump(exclude_unset=True) para não sobrescrever campos não enviados com None
    - Loga payload recebido e dict de update para diagnóstico
    - Trata caso de upd vazio (retorna registro existente, não quebra SQL)
    - Mensagem de erro 500 explícita em caso de falha de DB
    """
    existing = await tools_repo.find_by_id(tool_id)
    if not existing:
        raise HTTPException(404, "Tool não encontrada")

    # exclude_unset retorna apenas campos que o cliente enviou explicitamente.
    # Combinado com filtro None, evita sobrescrever colunas com NULL acidentalmente.
    raw = data.model_dump(exclude_unset=True)
    logger.info(f"update_tool {tool_id}: campos recebidos = {list(raw.keys())}")

    upd = {k: v for k, v in raw.items() if v is not None}

    # Boolean → int (compat com colunas INTEGER 0/1)
    if "requires_trusted_context" in upd:
        upd["requires_trusted_context"] = 1 if upd["requires_trusted_context"] else 0

    # PR #229: preserva auth_token existente se o cliente mandou vazio.
    # UI hoje envia auth_token="" quando o user não editou o campo (porque
    # o GET passou a mascarar o ciphertext). Sem esta proteção, o PUT
    # zeraria o token armazenado mesmo sem o operador querer. Para LIMPAR
    # explicitamente o token, mandar `null` (exclude_unset captura, mas o
    # filtro `v is not None` acima já remove None — caller deve usar
    # endpoint dedicado /tools/{id}/auth se quiser exposição explícita).
    if "auth_token" in upd and not upd["auth_token"]:
        upd.pop("auth_token", None)

    # Cifra credencial em repouso quando o cliente enviou um novo token.
    # encrypt() é idempotente (PR #229): valor já cifrado passa direto.
    if upd.get("auth_token"):
        from app.core.secrets import write_secret
        upd["auth_token"] = write_secret(upd["auth_token"])

    # PR #229: preserva auth_config existente se vier sem secrets (UI
    # também mascara client_secret/client_key/ca_cert). Se o operador
    # SUBSTITUIU um secret, o novo valor está no payload e segue normal.
    if "auth_config" in upd:
        import json as _json
        try:
            new_cfg = _json.loads(upd["auth_config"]) if isinstance(upd["auth_config"], str) else dict(upd["auth_config"])
        except (ValueError, TypeError):
            new_cfg = {}
        try:
            existing_cfg = _json.loads(existing.get("auth_config") or "{}")
        except (ValueError, TypeError):
            existing_cfg = {}
        for k in ("client_secret", "client_key", "ca_cert"):
            # Se UI enviou vazio mas existing tinha valor, preserva.
            if not new_cfg.get(k) and existing_cfg.get(k):
                new_cfg[k] = existing_cfg[k]
        upd["auth_config"] = _json.dumps(new_cfg, ensure_ascii=False)

    if not upd:
        logger.warning(f"update_tool {tool_id}: nenhum campo válido para atualizar")
        return existing

    logger.info(f"update_tool {tool_id}: aplicando update = {list(upd.keys())}")

    try:
        result = await tools_repo.update(tool_id, upd)
        if not result:
            logger.error(f"update_tool {tool_id}: update retornou None (find_by_id falhou)")
            raise HTTPException(500, "Update aplicado mas registro não pôde ser recuperado")
        await audit_repo.create({
            "entity_type": "tool", "entity_id": tool_id, "action": "updated",
            "details": json.dumps({"fields": list(upd.keys())}),
        })
        logger.info(f"update_tool {tool_id}: sucesso")
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"update_tool {tool_id}: erro de DB")
        raise HTTPException(500, f"Erro ao atualizar tool: {str(e)[:200]}")

@router.delete("/tools/{tool_id}")
async def delete_tool(tool_id: str):
    if not await tools_repo.delete(tool_id): raise HTTPException(404)
    return {"message": "Tool removida"}

class MCPWizardQuery(BaseModel):
    query: str


def _resolve_mcp_wizard_llm(settings, store_settings: dict) -> Optional[dict]:
    """Resolve qual LLM o wizard MCP vai usar, na ordem de precedência:

    1. **Modelo Primário** global (settings.primary_provider + primary_model).
       Caminho preferido — "em Configurações → Plataforma temos o modelo
       primário e deve ser usado". Roteado via get_provider, então suporta
       azure, openai_public, maritaca, ollama e gpt-oss-* — cada um lê as
       próprias credenciais.
    2. **Legacy** — chaves cruas openai_key/maritaca_key salvas no
       settings_store por instalações que preencheram só o card OpenAI/Maritaca.
       Mantido por retrocompat.
    3. **None** — nada configurado; o caller devolve erro orientando o operador.

    Função pura (recebe o objeto settings + o dict do store) → testável sem
    DB nem rede.
    """
    pp = (getattr(settings, "primary_provider", "") or "").strip()
    pm = (getattr(settings, "primary_model", "") or "").strip()
    if pp and pm:
        return {"mode": "primary", "provider": pp, "model": pm}

    okey = (store_settings.get("openai_key") or "").strip()
    if okey:
        return {
            "mode": "legacy", "source": "openai", "api_key": okey,
            "model": (store_settings.get("openai_model") or "gpt-4o").strip(),
            "base_url": "https://api.openai.com/v1",
        }
    mkey = (store_settings.get("maritaca_key") or "").strip()
    if mkey:
        base = (store_settings.get("maritaca_url") or "https://chat.maritaca.ai/api").strip().rstrip("/")
        return {
            "mode": "legacy", "source": "maritaca", "api_key": mkey,
            "model": (store_settings.get("maritaca_model") or "sabia-3").strip(),
            "base_url": f"{base}/v1",
        }
    return None


async def _mcp_wizard_complete(llm_desc: dict, prompt: str) -> str:
    """Executa a completion do wizard conforme o descriptor resolvido e
    devolve o texto cru da resposta do modelo.

    - mode=primary → get_provider(provider, model=...).generate(...) — usa o
      Modelo Primário da plataforma.
    - mode=legacy  → POST httpx direto num endpoint OpenAI-compatible.
    """
    import httpx

    if llm_desc["mode"] == "primary":
        from app.core.llm_providers import get_provider
        provider = get_provider(llm_desc["provider"], model=llm_desc["model"], temperature=0.3)
        result = await provider.generate([{"role": "user", "content": prompt}])
        return ((result or {}).get("content") or "").strip()

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{llm_desc['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {llm_desc['api_key']}", "Content-Type": "application/json"},
            json={"model": llm_desc["model"], "messages": [{"role": "user", "content": prompt}], "temperature": 0.3},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


@router.post("/tools/wizard")
async def mcp_wizard(data: MCPWizardQuery):
    """Wizard: busca MCP Server por URL (mcpservers.org) ou por descrição via LLM.

    Usa o **Modelo Primário** da plataforma (Configurações → Plataforma) quando
    configurado; só cai pras chaves legadas (OpenAI/Maritaca no settings_store)
    se nenhum Modelo Primário estiver definido. Ver _resolve_mcp_wizard_llm.
    """
    import re, httpx
    from app.core.config import get_settings as _get_settings

    query = data.query.strip()
    is_url = query.startswith("http://") or query.startswith("https://")

    try:
        store_settings = await settings_store.get_all()
        llm_desc = _resolve_mcp_wizard_llm(_get_settings(), store_settings)
        if not llm_desc:
            logger.warning(
                "mcp_wizard: nenhum LLM configurado",
                extra={"event": "mcp_wizard.no_llm"},
            )
            return {
                "results": [],
                "error": "Configure o Modelo Primário em Configurações → Plataforma "
                         "(ou, alternativamente, uma API key OpenAI/Maritaca).",
            }

        logger.info(
            "mcp_wizard: LLM resolvido",
            extra={
                "event": "mcp_wizard.llm_resolved",
                "mode": llm_desc["mode"],
                "llm_provider": llm_desc.get("provider") or llm_desc.get("source"),
                "llm_model": llm_desc["model"],
                "is_url": is_url,
            },
        )

        page_content = ""
        if is_url:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(query, headers={"User-Agent": "AgenteInteligencia/1.0"})
                resp.raise_for_status()
                raw_html = resp.text[:12000]
                page_content = re.sub(r'<script[^>]*>.*?</script>', '', raw_html, flags=re.DOTALL)
                page_content = re.sub(r'<style[^>]*>.*?</style>', '', page_content, flags=re.DOTALL)
                page_content = re.sub(r'<[^>]+>', ' ', page_content)
                page_content = re.sub(r'\s+', ' ', page_content).strip()[:6000]

        if is_url and page_content:
            prompt = f"""Extraia as informações do MCP Server desta página.

Conteúdo:
{page_content}

Responda SOMENTE com JSON array, sem markdown:
[{{"name":"nome","description":"descrição","endpoint":"comando npx ou URL","operations":["op1","op2"],"install_cmd":"npx -y ...","source_url":"{query}","auth":"none","sensitivity":"internal"}}]"""
        else:
            prompt = f"""Recomende 1 a 3 MCP servers para: "{query}"
Responda SOMENTE com JSON array, sem markdown:
[{{"name":"nome","description":"descrição","endpoint":"npx -y @scope/server","operations":["op1"],"install_cmd":"npx -y ...","source_url":"https://github.com/...","auth":"none","sensitivity":"internal"}}]
Se não conhecer, retorne: []"""

        raw = await _mcp_wizard_complete(llm_desc, prompt)

        clean = re.sub(r'```json\s*', '', raw)
        clean = re.sub(r'```\s*', '', clean).strip()
        match = re.search(r'\[.*\]', clean, re.DOTALL)
        results = json.loads(match.group(0)) if match else []
        logger.info(
            "mcp_wizard: concluído",
            extra={"event": "mcp_wizard.ok", "mode": llm_desc["mode"], "n_results": len(results)},
        )
        return {"results": results}
    except httpx.HTTPError as e:
        logger.warning(
            f"mcp_wizard: erro de rede ({type(e).__name__})",
            extra={"event": "mcp_wizard.http_error", "error_type": type(e).__name__, "is_url": is_url},
            exc_info=True,
        )
        return {"results": [], "error": f"Erro de rede: {str(e)[:150]}"}
    except (KeyError, IndexError) as e:
        logger.warning(
            f"mcp_wizard: resposta inesperada da API ({type(e).__name__})",
            extra={"event": "mcp_wizard.bad_response", "error_type": type(e).__name__},
            exc_info=True,
        )
        return {"results": [], "error": f"Resposta inesperada da API: {str(e)[:150]}"}
    except Exception as e:
        logger.warning(
            f"mcp_wizard: erro inesperado ({type(e).__name__})",
            extra={"event": "mcp_wizard.error", "error_type": type(e).__name__},
            exc_info=True,
        )
        return {"results": [], "error": str(e)[:200]}

# ═══ History ═══
@router.get("/history")
async def get_history(entity_type: str = None, search: str = None, limit: int = 50, offset: int = 0):
    results = {}
    if not entity_type or entity_type == "interactions":
        results["interactions"] = await interactions_repo.find_all(limit=limit, offset=offset) if not search else await interactions_repo.search(search, ["state","channel","metadata"])
    if not entity_type or entity_type == "turns":
        results["turns"] = await turns_repo.find_all(limit=limit, offset=offset) if not search else await turns_repo.search(search, ["user_text_redacted","output_text_redacted"])
    if not entity_type or entity_type == "envelopes":
        results["envelopes"] = await envelopes_repo.find_all(limit=limit, offset=offset)
    if not entity_type or entity_type == "audit":
        results["audit_log"] = await audit_repo.find_all(limit=limit, offset=offset) if not search else await audit_repo.search(search, ["action","details","entity_type"])
    return results

# ═══ Drift Events §18.2 ═══
@router.get("/drift-events")
async def list_drift_events(release_id: str = None, limit: int = 20):
    f = {}
    if release_id: f["release_id"] = release_id
    return {"events": await drift_repo.find_all(limit=limit, **f)}


# ═══ MCP Test Connection ═══

class MCPTestRequest(BaseModel):
    endpoint: str
    name: Optional[str] = ""
    operations: Optional[str] = "[]"
    auth_type: Optional[str] = ""
    auth_token: Optional[str] = ""
    auth_config: Optional[str] = "{}"
    # PR #232: opcional. Quando passado e auth_token está vazio, o backend
    # busca o token armazenado da tool — necessário porque o PR #229
    # mascarou o auth_token no GET, e a UI do painel direito passou a
    # enviar string vazia, gerando 401 silencioso contra o MCP real.
    tool_id: Optional[str] = None


async def _resolve_secrets_from_tool_id(data) -> None:
    """Se `data.tool_id` é passado E os campos de auth vieram vazios na
    request, busca os valores armazenados em `tools` e in-place atualiza
    `data.auth_token` / `data.auth_config` (apenas para os secrets vazios).

    Token preenchido NUNCA é sobrescrito — operador pode estar testando um
    valor novo antes de salvar.

    Token armazenado pode estar cifrado (`fernet:...`); decifragem fica a
    cargo do `_build_mcp_auth` via `read_secret` (idempotente para
    plaintext legacy). Aqui só passamos o valor opaco.
    """
    if not getattr(data, "tool_id", None):
        return
    needs_token = not (data.auth_token or "").strip()
    cfg_str = data.auth_config or "{}"
    try:
        cur_cfg = json.loads(cfg_str)
    except (ValueError, TypeError):
        cur_cfg = {}
    needs_cfg_secret = any(
        k in cur_cfg and not cur_cfg.get(k)
        for k in ("client_secret", "client_key", "ca_cert")
    )
    if not needs_token and not needs_cfg_secret:
        return  # nada a resolver — caller forneceu tudo
    tool = await tools_repo.find_by_id(data.tool_id)
    if not tool:
        return  # tool sumiu/foi deletada — segue sem fallback, falha downstream
    if needs_token:
        stored = tool.get("auth_token") or ""
        if stored:
            data.auth_token = stored
    if needs_cfg_secret:
        try:
            stored_cfg = json.loads(tool.get("auth_config") or "{}")
        except (ValueError, TypeError):
            stored_cfg = {}
        for k in ("client_secret", "client_key", "ca_cert"):
            if k in cur_cfg and not cur_cfg.get(k) and stored_cfg.get(k):
                cur_cfg[k] = stored_cfg[k]
        data.auth_config = json.dumps(cur_cfg)


async def _test_mcp_connection_impl(data: MCPTestRequest) -> dict:
    """Implementação real do teste MCP. Wrapper público adiciona log
    estruturado pós-execução (PR #231)."""
    import httpx, time

    # PR #232: se a UI passou tool_id e o auth_token está vazio (porque o
    # GET de tools mascarou desde PR #229), busca o token armazenado do banco.
    await _resolve_secrets_from_tool_id(data)

    endpoint = data.endpoint.strip()

    # ── Build auth (API Key, OAuth2, mTLS) ──
    auth = _build_mcp_auth(data.auth_type, data.auth_token, data.auth_config)

    # ── Non-HTTP: stdio via subprocess (timeout 90s) ──
    if not endpoint.startswith("http"):
        from app.mcp.runtime import run_stdio_session
        result = await run_stdio_session(command=endpoint, action="test", timeout=90)
        result.setdefault("latency", None)
        result.setdefault("recommendations", [])
        result.setdefault("discovered_tools", [])
        result.setdefault("server_name", result.get("server_name"))
        return result

    # ── OAuth2: buscar token se necessário ──
    if auth.get("oauth_config"):
        token = await _fetch_oauth2_token(auth["oauth_config"])
        if token:
            auth["headers"]["Authorization"] = f"Bearer {token}"
        else:
            _cleanup_mcp_auth(auth)
            return {"success": False, "details": "Falha ao obter token OAuth2",
                    "latency": None, "server_name": None, "discovered_tools": [],
                    "recommendations": [
                        "Verifique client_id, client_secret e token_url.",
                        "Confirme que o grant_type 'client_credentials' está habilitado no Authorization Server.",
                        "Verifique se o scope está correto (pode ser obrigatório).",
                    ]}

    headers = auth["headers"]
    client_kwargs = auth.get("client_kwargs", {})

    start = time.time()
    recommendations = []
    discovered_tools = []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, **client_kwargs) as client:
            resp = await client.post(endpoint, json={
                "jsonrpc": "2.0", "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "AgenteInteligencia", "version": "1.0.0"}},
                "id": 1,
            }, headers=headers)
            latency = int((time.time() - start) * 1000)

            if resp.status_code >= 400:
                recs = [f"HTTP {resp.status_code}"]
                if resp.status_code == 404: recs.append("Tente adicionar /sse, /mcp ou /v1 ao endpoint.")
                elif resp.status_code in (401,403): recs.append("Autenticação necessária. Configure Auth.")
                elif resp.status_code == 405: recs.append("Tente /sse ao final da URL (SSE transport).")
                elif resp.status_code == 406: recs.append("Servidor rejeitou o content-type. Verifique se o endpoint suporta MCP Streamable HTTP.")
                return {"success": False, "details": f"HTTP {resp.status_code}", "latency": latency, "server_name": None, "discovered_tools": [], "recommendations": recs}

            # ── Tratar resposta SSE (text/event-stream) ──
            content_type = resp.headers.get("content-type", "")

            if "text/event-stream" in content_type:
                # Servidor respondeu com SSE — extrair JSON do stream
                json_data = _extract_json_from_sse(resp.text)
                if json_data is None:
                    return {"success": False, "details": "SSE sem dados JSON válidos", "latency": latency,
                            "server_name": None, "discovered_tools": [],
                            "recommendations": ["Servidor respondeu com SSE mas não continha dados JSON-RPC válidos.",
                                                 f"Resposta bruta: {resp.text[:200]}"]}
                data_resp = json_data
            else:
                try:
                    data_resp = resp.json()
                except Exception:
                    ct = content_type
                    recs = ["Resposta não-JSON recebida."]
                    if "text/html" in ct:
                        recs.append("Servidor retornou HTML. Verifique se o endpoint MCP está correto.")
                    recs.append(f"Content-Type: {ct}")
                    recs.append(f"Resposta: {resp.text[:150]}")
                    return {"success": False, "details": "Resposta não-JSON", "latency": latency, "server_name": None, "discovered_tools": [], "recommendations": recs}

            if "result" in data_resp:
                server_info = data_resp["result"].get("serverInfo", {})
                server_name = f"{server_info.get('name', '?')} v{server_info.get('version', '?')}"

                # Captura a sessão MCP Streamable HTTP (Mcp-Session-Id) e a versão
                # negociada, e ecoa em notifications/initialized + tools/list. Sem
                # isto, server stateful (Context7) descobre 0 tools no cadastro —
                # ou seja, NÃO funcionaria de primeira ao registrar um MCP novo.
                from app.mcp.runtime import extract_session_id
                headers = {**headers}
                _sid = extract_session_id(resp)
                if _sid:
                    headers["Mcp-Session-Id"] = _sid
                _pv = data_resp["result"].get("protocolVersion")
                if _pv:
                    headers["MCP-Protocol-Version"] = str(_pv).strip()

                try:
                    await client.post(endpoint, json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                                      headers=headers)
                except: pass

                try:
                    tools_resp = await client.post(endpoint, json={
                        "jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 2,
                    }, headers=headers)

                    tools_ct = tools_resp.headers.get("content-type", "")
                    if "text/event-stream" in tools_ct:
                        tools_data = _extract_json_from_sse(tools_resp.text) or {}
                    else:
                        tools_data = tools_resp.json()

                    if "result" in tools_data:
                        for t in tools_data["result"].get("tools", []):
                            discovered_tools.append({"name": t.get("name",""), "description": t.get("description",""), "inputSchema": t.get("inputSchema",{})})
                except: pass

                recommendations.append(f"Servidor: {server_name}")
                if discovered_tools:
                    recommendations.append(f"{len(discovered_tools)} ferramenta(s) descoberta(s)")

                # Per-tool D (F1): persiste o schema descoberto em tools.discovered_tools
                # pra a geração ler depois (sem rede na geração). Best-effort — falha
                # NÃO quebra a descoberta (que continua devolvendo discovered_tools).
                _tid = getattr(data, "tool_id", None)
                if _tid and discovered_tools:
                    try:
                        from app.core.database import tools_repo
                        from app.mcp.runtime import serialize_discovered_tools
                        await tools_repo.update(_tid, {"discovered_tools": serialize_discovered_tools(discovered_tools)})
                    except Exception as _persist_err:
                        logger.warning(
                            "mcp.discovery.persist_failed",
                            extra={"event": "mcp.discovery", "tool_id": _tid,
                                   "error_type": type(_persist_err).__name__},
                        )

                return {"success": True, "details": "MCP Server conectado (JSON-RPC)", "latency": latency,
                        "server_name": server_name, "discovered_tools": discovered_tools, "recommendations": recommendations}

            if "error" in data_resp:
                err = data_resp["error"]
                return {"success": False, "details": f"Erro: {err.get('message', str(err))}", "latency": latency,
                        "server_name": None, "discovered_tools": [],
                        "recommendations": ["Servidor acessível mas retornou erro.", f"Código: {err.get('code','?')}"]}

            return {"success": False, "details": "Resposta JSON sem result/error", "latency": latency,
                    "server_name": None, "discovered_tools": [],
                    "recommendations": ["Servidor respondeu JSON mas não no formato MCP/JSON-RPC.", f"Resposta: {json.dumps(data_resp)[:200]}"]}

    except httpx.ConnectError:
        return {"success": False, "details": "Conexão recusada", "latency": None, "server_name": None, "discovered_tools": [],
                "recommendations": ["Verifique URL, host, porta e firewall.", f"Endpoint: {endpoint}"]}
    except httpx.TimeoutException:
        return {"success": False, "details": "Timeout (15s)", "latency": None, "server_name": None, "discovered_tools": [],
                "recommendations": ["Servidor não respondeu em 15s."]}
    except Exception as e:
        return {"success": False, "details": str(e)[:200], "latency": None, "server_name": None, "discovered_tools": [],
                "recommendations": [f"Erro: {str(e)[:300]}"]}
    finally:
        _cleanup_mcp_auth(auth)


@router.post("/tools/test")
async def test_mcp_connection(data: MCPTestRequest):
    """Testa conexão com MCP Server — HTTP ou stdio.

    NOTA: timeout de stdio elevado para 90s para acomodar a 1ª execução de
    'npx -y <pacote>' que precisa baixar dependências do registry npm.

    PR #231: emite evento estruturado `mcp.test.completed` ou `mcp.test.failed`
    no app.log para que o operador consiga rastrear falhas pelo Log Viewer 2.0
    (Observabilidade > Manutenção de Logs).
    """
    import time
    _t0 = time.time()
    result = await _test_mcp_connection_impl(data)
    duration_ms = round((time.time() - _t0) * 1000, 2)
    success = bool(result.get("success"))
    payload = {
        "event": "mcp.test.completed" if success else "mcp.test.failed",
        "mcp_endpoint": (data.endpoint or "").strip(),
        "transport": "http" if (data.endpoint or "").startswith("http") else "stdio",
        "auth_type": data.auth_type or "",
        "tool_id": data.tool_id or "",  # PR #232
        "success": success,
        "details": (result.get("details") or "")[:300],
        "latency_ms": result.get("latency"),
        "duration_ms": duration_ms,
        "server_name": result.get("server_name"),
        "discovered_tools_count": len(result.get("discovered_tools") or []),
        "recommendations_count": len(result.get("recommendations") or []),
    }
    # IMPORTANTE: usar key `event` (não `name`) — `name` colide com
    # LogRecord.name (bug PR #225). `mcp_endpoint` em vez de `endpoint`
    # por consistência semântica + futura defesa.
    if success:
        logger.info(payload["event"], extra=payload)
    else:
        logger.warning(payload["event"], extra=payload)
    return result


class MCPBackfillRequest(BaseModel):
    # force=True re-descobre mesmo conectores que já têm discovered_tools.
    force: Optional[bool] = False


@router.post("/tools/backfill-discovered")
async def backfill_mcp_discovered(data: MCPBackfillRequest = MCPBackfillRequest()):
    """F5 (per-tool D) — backfill: descobre+persiste `discovered_tools` para os
    conectores MCP HTTP que ainda não têm (predam a F1).

    Manutenção/operação: NÃO ativa o modo per-tool — só popula a coluna dormante
    que o builder consome quando `MCP_PER_TOOL_ENABLED` está ON. Idempotente
    (pula quem já tem, salvo `force`). Best-effort por conector.
    """
    from app.mcp.runtime import backfill_discovered_tools, per_tool_enabled
    summary = await backfill_discovered_tools(tools_repo, force=bool(data.force))
    summary["per_tool_enabled"] = per_tool_enabled()  # transparência: ativo ou dormente
    logger.info("mcp.backfill.completed", extra={"event": "mcp.backfill.completed", **summary})
    return summary


class MCPExecuteRequest(BaseModel):
    endpoint: str
    tool_name: str
    arguments: Optional[dict] = {}
    auth_type: Optional[str] = ""
    auth_token: Optional[str] = ""
    auth_config: Optional[str] = "{}"
    # PR #232: mesma semântica do MCPTestRequest — backend resolve secrets
    # do tool quando UI manda tool_id + auth_token vazio.
    tool_id: Optional[str] = None

async def _execute_mcp_tool_impl(data: MCPExecuteRequest) -> dict:
    """Implementação real do execute. Wrapper público adiciona log
    estruturado pós-execução (PR #231)."""
    import httpx, time

    # PR #232: resolve secrets armazenados quando tool_id é passado e
    # auth_token vem vazio (UI mascara desde PR #229).
    await _resolve_secrets_from_tool_id(data)

    endpoint = data.endpoint.strip()

    # ── Build auth ──
    auth = _build_mcp_auth(data.auth_type, data.auth_token, data.auth_config)

    if not endpoint.startswith("http"):
        from app.mcp.runtime import run_stdio_session
        result = await run_stdio_session(
            command=endpoint, action="call",
            tool_name=data.tool_name, arguments=data.arguments or {},
            timeout=90,
        )
        if result.get("success"):
            return {"success": True, "data": result.get("data", ""), "latency": None}
        return {"success": False, "error": result.get("details", "Erro"), "latency": None,
                "recommendations": result.get("recommendations", [])}

    # ── OAuth2: buscar token se necessário ──
    if auth.get("oauth_config"):
        token = await _fetch_oauth2_token(auth["oauth_config"])
        if token:
            auth["headers"]["Authorization"] = f"Bearer {token}"
        else:
            _cleanup_mcp_auth(auth)
            return {"success": False, "error": "Falha ao obter token OAuth2. Verifique client_id, client_secret e token_url.", "latency": None}

    headers = auth["headers"]
    client_kwargs = auth.get("client_kwargs", {})

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, **client_kwargs) as client:
            # Handshake centralizado — captura Mcp-Session-Id (servers stateful
            # como Context7) e ecoa no tools/call. Stateless = sem mudança.
            from app.mcp.runtime import mcp_http_handshake
            headers = await mcp_http_handshake(client, endpoint, headers)

            resp = await client.post(endpoint, json={
                "jsonrpc": "2.0", "method": "tools/call",
                "params": {"name": data.tool_name, "arguments": data.arguments or {}},
                "id": 3,
            }, headers=headers)

            latency = int((time.time() - start) * 1000)

            # ── Tratar resposta SSE ──
            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                result = _extract_json_from_sse(resp.text)
                if result is None:
                    return {"success": False, "error": "SSE sem dados JSON válidos", "latency": latency}
            else:
                result = resp.json()

            if "result" in result:
                content = result["result"]
                if isinstance(content, dict) and "content" in content:
                    blocks = content["content"]
                    if isinstance(blocks, list):
                        texts = []
                        for b in blocks:
                            if isinstance(b, dict):
                                if b.get("type") == "text": texts.append(b.get("text",""))
                                elif b.get("type") == "resource": texts.append(json.dumps(b.get("resource",{}), indent=2, ensure_ascii=False))
                                else: texts.append(json.dumps(b, indent=2, ensure_ascii=False))
                        return {"success": True, "data": "\n".join(texts), "latency": latency}
                    return {"success": True, "data": json.dumps(content, indent=2, ensure_ascii=False), "latency": latency}
                return {"success": True, "data": json.dumps(content, indent=2, ensure_ascii=False) if isinstance(content, (dict,list)) else str(content), "latency": latency}

            if "error" in result:
                return {"success": False, "error": result["error"].get("message", str(result["error"])), "latency": latency}

            return {"success": True, "data": json.dumps(result, indent=2, ensure_ascii=False), "latency": latency}

    except httpx.TimeoutException:
        return {"success": False, "error": "Timeout (30s)", "latency": int((time.time()-start)*1000)}
    except Exception as e:
        return {"success": False, "error": str(e)[:300], "latency": int((time.time()-start)*1000)}
    finally:
        _cleanup_mcp_auth(auth)


@router.post("/tools/execute")
async def execute_mcp_tool(data: MCPExecuteRequest):
    """Executa uma ferramenta MCP via JSON-RPC — HTTP ou stdio.
    Suporta autenticação API Key, OAuth2 Client Credentials e mTLS.

    PR #231: emite evento estruturado `mcp.execute.completed` ou
    `mcp.execute.failed` para rastreio via Log Viewer 2.0. Não loga
    arguments (podem conter dados sensíveis do operador) — só size.
    """
    import json as _json, time
    _t0 = time.time()
    result = await _execute_mcp_tool_impl(data)
    duration_ms = round((time.time() - _t0) * 1000, 2)
    success = bool(result.get("success"))
    try:
        args_size = len(_json.dumps(data.arguments or {}))
    except Exception:
        args_size = 0
    raw_data = result.get("data") or ""
    payload = {
        "event": "mcp.execute.completed" if success else "mcp.execute.failed",
        "mcp_endpoint": (data.endpoint or "").strip(),
        "transport": "http" if (data.endpoint or "").startswith("http") else "stdio",
        "auth_type": data.auth_type or "",
        "tool_id": data.tool_id or "",  # PR #232
        "tool_name": data.tool_name or "",
        "args_size_bytes": args_size,
        "success": success,
        "error": (result.get("error") or "")[:300] if not success else "",
        "data_size_bytes": len(raw_data) if isinstance(raw_data, str) else 0,
        "latency_ms": result.get("latency"),
        "duration_ms": duration_ms,
    }
    if success:
        logger.info(payload["event"], extra=payload)
    else:
        logger.warning(payload["event"], extra=payload)
    return result


def _extract_json_from_sse(sse_text: str) -> dict | None:
    """Extrai o primeiro objeto JSON-RPC válido de uma resposta SSE.

    Formato SSE esperado:
        event: message
        data: {"jsonrpc":"2.0","result":{...},"id":1}

    Alguns servidores enviam múltiplas linhas `data:`. Esta função
    concatena todas e tenta parsear o resultado.
    """
    lines = sse_text.strip().split("\n")
    data_parts = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("data:"):
            payload = stripped[5:].strip()
            if payload:
                data_parts.append(payload)

    # Tentar cada parte individualmente primeiro (caso mais comum)
    for part in data_parts:
        try:
            obj = json.loads(part)
            if isinstance(obj, dict) and ("result" in obj or "error" in obj or "jsonrpc" in obj):
                return obj
        except (json.JSONDecodeError, TypeError):
            continue

    # Fallback: concatenar todas as partes
    if data_parts:
        combined = "".join(data_parts)
        try:
            obj = json.loads(combined)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, TypeError):
            pass

    return None


# ═══ Settings (persistidas em PostgreSQL) ═══

class SettingsSave(BaseModel):
    # Azure OpenAI — provedor primário (Onda 7 Wave 4: 'openai' virou alias).
    # Estes campos sobrepõem AZURE_OPENAI_* do .env quando preenchidos via UI.
    azure_key: Optional[str] = ""
    azure_endpoint: Optional[str] = ""
    azure_api_version: Optional[str] = "2024-02-15-preview"
    azure_chat_deployment: Optional[str] = "gpt-4o"
    azure_embeddings_deployment: Optional[str] = "text-embedding-3-small"
    openai_key: Optional[str] = ""
    openai_model: Optional[str] = "gpt-4o"
    # PR #194 (2026-05-29): OpenAI público real (api.openai.com) — separado
    # do alias Azure. Usado quando operador quer rotear pra api.openai.com
    # direto em vez do Azure (ex: comparar latência GPT-4o).
    openai_public_api_key: Optional[str] = ""
    openai_public_base_url: Optional[str] = "https://api.openai.com/v1"
    openai_public_model: Optional[str] = "gpt-4o"
    maritaca_key: Optional[str] = ""
    maritaca_model: Optional[str] = "sabia-3"
    maritaca_url: Optional[str] = "https://chat.maritaca.ai/api"
    ollama_url: Optional[str] = "http://187.77.46.137:11434"
    ollama_model: Optional[str] = "hf.co/Althayr/Gemma-3-Gaia-PT-BR-4b-it-GGUF:latest"
    langfuse_public: Optional[str] = ""
    langfuse_secret: Optional[str] = ""
    langfuse_host: Optional[str] = "https://cloud.langfuse.com"
    max_iterations: Optional[int] = 25
    timeout: Optional[int] = 120
    mesh_groups: Optional[str] = None
    mesh_chain_names: Optional[str] = None
    # Modelo Primário (fallback global)
    primary_provider: Optional[str] = ""
    primary_model: Optional[str] = ""
    # Idioma de resposta (default global) — fallback quando agent.response_language
    # vazio. Pattern BCP-47. UI dropdown impõe valores válidos; pattern aqui
    # protege contra payloads diretos via API.
    default_response_language: Optional[str] = Field(
        default="pt-BR",
        pattern=r"^[a-z]{2}(-[A-Z]{2})?$",
    )
    # Timezone da plataforma (IANA tz database). Default America/Sao_Paulo (GMT-3
    # Brasília). Aplicado via apply_settings_to_env → os.environ['TZ'] + tzset, e
    # exposto à UI como window.PLATFORM_TZ (formatação de datas no fuso da plataforma).
    timezone: Optional[str] = "America/Sao_Paulo"
    # Grounded-by-default (2026-06-06): True = agentes respondem SÓ com base em
    # evidências; respostas sem evidência são recusadas. False fura o princípio
    # anti-alucinação globalmente (preferir o escape hatch por agente).
    grounding_strict: Optional[bool] = True
    # MCP per-tool (D) — default OFF. Quando ligado, cada tool MCP vira sua
    # própria função com o inputSchema real (vs legado {operation, query}).
    # Requer discovered_tools populado nos conectores (descoberta/backfill).
    mcp_per_tool_enabled: Optional[bool] = False
    # Tier 2 — text-to-SQL governado (RAG-Tabela) — default OFF. Quando ligado,
    # habilita a bancada "Perguntar à Tabela": a IA compila a pergunta em PT-BR
    # numa consulta estruturada, o humano cura, e o runtime só executa o curado.
    text_to_sql_enabled: Optional[bool] = False
    # GPT-OSS (open-weight) — Onda 4 plataforma
    oss120b_url: Optional[str] = ""
    oss120b_model: Optional[str] = "openai/gpt-oss-120b"
    oss120b_api_key: Optional[str] = "not-needed"
    oss20b_url: Optional[str] = ""
    oss20b_model: Optional[str] = "openai/gpt-oss-20b"
    oss20b_api_key: Optional[str] = "not-needed"
    llm_timeout_seconds: Optional[int] = 300
    # Embedding (Azure | Qwen3) — Qwen3 reusa URL/key do OSS source
    embedding_provider: Optional[str] = "qwen3"
    qwen3_source: Optional[str] = "oss120b"
    qwen3_path: Optional[str] = "embed06b/v1"
    qwen3_model: Optional[str] = "Qwen/Qwen3-Embedding-0.6B"
    # Densidade do vetor (Matryoshka). 0 = padrão do modelo. Mudar exige
    # re-embedar a collection do Qdrant.
    qwen3_dimensions: Optional[int] = 0
    # ── Módulo Parâmetros (25.1.0): Verifier/juiz + gates do Harness ──
    # Defaults None (exclude_unset): a aba envia SÓ o delta — valores herdados
    # do .env/classe não viram linha no banco por acidente (lição do save por
    # delta do Roteamento LLM, 24.8.0). Ranges validados aqui → 422 nomeado.
    verifier_v2_enabled: Optional[bool] = None
    verifier_factuality_threshold: Optional[float] = Field(default=None, ge=0, le=5)
    verifier_completeness_threshold: Optional[float] = Field(default=None, ge=0, le=5)
    verifier_tone_threshold: Optional[float] = Field(default=None, ge=0, le=5)
    verifier_max_tokens: Optional[int] = Field(default=None, ge=100, le=8000)
    verifier_contract_retry_enabled: Optional[bool] = None
    verifier_contract_retry_max_tokens: Optional[int] = Field(default=None, ge=200, le=16000)
    verifier_production_async: Optional[bool] = None
    verifier_production_sample_rate: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    verifier_max_concurrent_jobs: Optional[int] = Field(default=None, ge=1, le=200)
    harness_use_verifier: Optional[bool] = None
    harness_min_accuracy: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    harness_min_avg_factuality: Optional[float] = Field(default=None, ge=0, le=5)
    harness_min_avg_completeness: Optional[float] = Field(default=None, ge=0, le=5)
    harness_min_avg_tone: Optional[float] = Field(default=None, ge=0, le=5)
    harness_max_safety_violation_rate: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    harness_min_contract_compliance: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    harness_max_hallucination_rate: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    harness_max_dim_regression_pct: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    # Tuning de performance do invoke (25.2.0)
    query_topology_cache_enabled: Optional[bool] = None
    fast_routing_enabled: Optional[bool] = None
    # Esforço de raciocínio das gerações do Wizard (27.0.0): 'high'|'medium'|
    # 'low'|'' (desligado). Gate por modelo em get_provider. Default 'high'.
    wizard_reasoning_effort: Optional[str] = None
    # P0 API externa: CORS allowlist (CSV de origens) + contenção da API Key.
    cors_allowed_origins: Optional[str] = None
    api_key_public_surface_only: Optional[bool] = None
    # P1: invoke via key só em pipelines publicados (contrato selado).
    api_key_invoke_published_only: Optional[bool] = None
    # F6: quota de custo por API Key (débito + bloqueio 402 ao estourar o teto).
    api_key_cost_budget_enabled: Optional[bool] = None

@router.get("/settings")
async def get_settings(user: dict = Depends(require_role("root", "admin"))):
    """Carrega configurações salvas.

    Gate por ROLE (25.1.0): o store inclui credenciais (chaves de API,
    endpoints) — só root/admin lê. Consumidores toleram 403 (settings.html é
    root/admin de fato; observability cai no fallback do langfuse_host).
    """
    data = await settings_store.get_all()
    return {"settings": data}


@router.get("/settings/parameters")
async def get_parameter_settings(
    user: dict = Depends(require_role("root", "admin")),
):
    """Módulo Parâmetros (25.1.0): valores EFETIVOS (banco → env → default da
    classe) dos parâmetros curados do Verifier/harness + a FONTE de cada um.

    A aba Parâmetros carrega daqui (não do GET /settings, que só devolve o
    banco — parâmetro nunca salvo apareceria vazio em vez do default real) e
    salva via PUT /settings enviando SÓ o delta.
    """
    from app.core.config import PARAMETER_UI_KEYS, get_settings as _gs
    s = _gs()
    store = await settings_store.get_all()
    params = []
    for k in PARAMETER_UI_KEYS:
        in_db = k in store and store.get(k) not in ("", None)
        params.append({
            "key": k,
            "value": getattr(s, k, None),
            "source": "banco" if in_db else "ambiente/padrão",
        })
    return {"parameters": params}


@router.delete("/settings/parameters/{key}")
async def reset_parameter_setting(
    key: str, user: dict = Depends(require_role("root", "admin")),
):
    """"Restaurar padrão" do módulo Parâmetros (25.1.0): remove a chave do
    banco para a plataforma voltar a herdar o `.env`/default do código.

    Allowlist a PARAMETER_UI_KEYS — este endpoint NÃO desfaz credenciais nem
    seleção de modelo (essas são seladas e vivem só na UI de propósito). Além
    do DELETE no banco, remove a env var de os.environ (o apply_settings_to_env
    só remove chaves SELADAS; as de parâmetro são não-seladas) e limpa o cache.
    """
    from app.core.config import (
        PARAMETER_UI_KEYS, _UI_TO_ENV_MAP, apply_settings_to_env,
    )
    if key not in PARAMETER_UI_KEYS:
        raise HTTPException(400, f"Parâmetro '{key}' não é redefinível por aqui.")
    existed = await settings_store.delete(key)
    # Remove o resíduo do os.environ (senão a chave DB apagada seguiria valendo
    # até o próximo restart, pois é não-selada e apply não a poparia).
    import os
    os.environ.pop(_UI_TO_ENV_MAP[key], None)
    try:
        await apply_settings_to_env()  # cache_clear + reseala o resto
    except Exception:
        pass
    await audit_repo.create({
        "entity_type": "settings", "entity_id": "platform",
        "action": "parameter_reset",
        "details": json.dumps({"key": key, "existed": existed}),
    })
    return {"status": "ok", "key": key, "existed": existed}

@router.put("/settings")
async def save_settings(
    data: SettingsSave,
    user: dict = Depends(require_role("root", "admin")),
):
    """Salva configurações na plataforma + aplica em runtime.

    Gate por ROLE real (25.1.0): antes QUALQUER autenticado (inclusive role
    comum ou X-API-Key) podia sobrescrever credenciais/config da plataforma —
    o sumiço das abas no template era só cosmético. Agora root/admin.

    Após persistir em settings_store (Postgres), chama apply_settings_to_env()
    pra popular os.environ com os valores novos e invalidar o cache do
    get_settings() + singleton do _embedder. Próximas chamadas de LLM/embedder
    leem credenciais novas SEM precisar restart do container.
    """
    # PARTIAL update: só persiste os campos EXPLICITAMENTE enviados na request
    # (exclude_unset). Sem isso, salvar a aba Plataforma — que envia só os campos
    # dela — fazia os DEMAIS campos do SettingsSave caírem no default ("") e ZERAVAM
    # segredos de outras abas (azure_key, azure_endpoint, URLs do gpt-oss,
    # primary_provider/model…). Footgun real: mudar o fuso apagava a config de LLM.
    settings_dict = {k: str(v) for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    await settings_store.set_many(settings_dict)
    # Aplicar em runtime — inclui clear de lru_cache e reset do embedder.
    try:
        from app.core.config import apply_settings_to_env
        applied = await apply_settings_to_env()
    except Exception as e:
        applied = 0
    await audit_repo.create({
        "entity_type": "settings", "entity_id": "platform",
        "action": "settings_saved",
        "details": json.dumps({"keys": list(settings_dict.keys()), "env_applied": applied}),
    })
    return {"message": "Configurações salvas", "keys_saved": len(settings_dict), "env_applied": applied}


@router.get("/settings/pricing")
async def get_llm_pricing(user: dict = Depends(require_role("root", "admin"))):
    """Tabela EFETIVA de preços de LLM (USD/1k tokens) = default ∪ overrides.

    Alimenta a tela "Preços LLM". Cada linha marca se veio de override (editado
    na tela) ou do default do código, com o default ao lado (p/ restaurar)."""
    from app.core.llm_pricing import effective_pricing
    return {"pricing": effective_pricing()}


@router.put("/settings/pricing")
async def save_llm_pricing(
    data: dict = Body(...), user: dict = Depends(require_role("root", "admin")),
):
    """Salva os overrides de preço (USD/1k tokens) e aplica em RUNTIME (sem deploy).

    Body: ``{"overrides": {"azure/gpt-4o": {"input": 0.003, "output": 0.012}}}``.
    O setter normaliza/valida (ignora inválidos); persiste a versão limpa em
    platform_settings (chave llm_pricing_overrides) — recarregada no boot por
    apply_settings_to_env. Torna a linha de custo do TCO auditável e atual."""
    from app.core.llm_pricing import (
        set_pricing_overrides, get_pricing_overrides, effective_pricing,
    )
    overrides = (data or {}).get("overrides")
    if not isinstance(overrides, dict):
        raise HTTPException(400, "Corpo inválido: esperado {\"overrides\": {...}}")
    set_pricing_overrides(overrides)          # normaliza/valida na camada runtime
    clean = get_pricing_overrides()
    await settings_store.set("llm_pricing_overrides", json.dumps(clean))
    await audit_repo.create({
        "entity_type": "settings", "entity_id": "platform",
        "action": "pricing_saved",
        "details": json.dumps({"models": sorted(clean.keys())}),
    })
    return {"status": "ok", "count": len(clean), "pricing": effective_pricing()}


@router.get("/dashboard/costs")
async def dashboard_costs(
    group_by: str = "pipeline",
    since: str = None,
    until: str = None,
    pipeline_id: str = None,
    user_id: str = None,
    source: str = None,
    user: dict = Depends(require_role("root", "admin")),
):
    """Custo org-wide por invocação (SSOT `invocation_costs`) — o "quanto gastamos".

    Agrega TODOS os caminhos de invoke (não só o catálogo, como o /catalog/cost) por
    pipeline|agent|user|source|day, com filtros de data (since/until, ISO). É a visão
    de FinOps que faltava — role-gated (root/admin), dado de custo."""
    from app.core.cost_ledger import aggregate_invocation_costs
    try:
        rows, totals = await aggregate_invocation_costs(
            group_by=group_by, since=since, until=until,
            pipeline_id=pipeline_id, user_id=user_id, source=source,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"group_by": group_by, "rows": rows, "totals": totals}


class ProviderTestRequest(BaseModel):
    provider: str  # azure | openai | maritaca | ollama | qwen3 | gpt-oss-*
    model: str
    api_key: Optional[str] = ""
    base_url: Optional[str] = ""
    api_version: Optional[str] = ""  # Azure-only — ex: "2024-02-15-preview"
    # Densidade do vetor (qwen3 Matryoshka). 0/None = usa default do modelo.
    dimensions: Optional[int] = 0


@router.post("/settings/test-provider")
async def test_provider(data: ProviderTestRequest):
    """Testa conectividade com um provedor LLM usando os valores informados.

    Não persiste nada; usa as credenciais/URL recebidas no body para fazer
    uma chamada trivial e medir latência. Retorna {ok, latency_ms, sample, error}.
    """
    import time as _time
    import httpx
    provider = (data.provider or "").lower().strip()
    valid_providers = {"azure", "openai", "openai_public", "maritaca", "ollama", "gpt-oss-20b", "gpt-oss-120b", "qwen3"}
    if provider not in valid_providers:
        raise HTTPException(400, f"Provedor inválido: {data.provider}")
    if not data.model:
        raise HTTPException(400, "Modelo obrigatório (deployment name no caso de Azure)")

    # Auth method por provedor:
    # - Azure: header `api-key: <key>`, URL com query `?api-version=...`, deployment no path
    # - OpenAI/Maritaca/Ollama: `Authorization: Bearer <key>`
    use_bearer = True

    # Resolve base_url e api_key conforme provedor
    if provider == "azure":
        endpoint = (data.base_url or "").rstrip("/")
        api_version = data.api_version or "2024-02-15-preview"
        api_key = data.api_key or ""
        if not endpoint:
            return {"ok": False, "error": "Endpoint obrigatório (ex: https://xxx.openai.azure.com)"}
        if not api_key:
            return {"ok": False, "error": "API Key obrigatória para Azure"}
        # Azure: deployment vai no PATH, não no body
        chat_url = f"{endpoint}/openai/deployments/{data.model}/chat/completions?api-version={api_version}"
        use_bearer = False
    elif provider == "openai":
        base_url = (data.base_url or "https://api.openai.com").rstrip("/")
        chat_url = f"{base_url}/v1/chat/completions" if not base_url.endswith("/v1") else f"{base_url}/chat/completions"
        api_key = data.api_key or ""
        if not api_key:
            return {"ok": False, "error": "API Key obrigatória para OpenAI"}
    elif provider == "openai_public":
        # PR #194: OpenAI público real (api.openai.com).
        base_url = (data.base_url or "https://api.openai.com/v1").rstrip("/")
        chat_url = f"{base_url}/chat/completions" if base_url.endswith("/v1") else f"{base_url}/v1/chat/completions"
        api_key = data.api_key or ""
        if not api_key:
            return {"ok": False, "error": "API Key obrigatória para OpenAI público"}
    elif provider == "maritaca":
        base_url = (data.base_url or "https://chat.maritaca.ai/api").rstrip("/")
        chat_url = f"{base_url}/v1/chat/completions"
        api_key = data.api_key or ""
        if not api_key:
            return {"ok": False, "error": "API Key obrigatória para Maritaca"}
    elif provider == "ollama":
        base_url = (data.base_url or "http://localhost:11434").rstrip("/")
        chat_url = f"{base_url}/v1/chat/completions"
        api_key = data.api_key or "ollama"
    elif provider in ("gpt-oss-20b", "gpt-oss-120b"):
        # base_url já vem com /v1 no final (ex: https://hub-gpus.claro.com.br/gpt120/v1)
        base_url = (data.base_url or "").rstrip("/")
        if not base_url:
            return {"ok": False, "error": f"URL obrigatória para {provider}"}
        # Se URL não termina em /v1, adiciona; se termina, usa direto
        chat_url = f"{base_url}/chat/completions" if base_url.endswith("/v1") else f"{base_url}/v1/chat/completions"
        api_key = data.api_key or "not-needed"
    else:  # qwen3 — testa endpoint /embeddings (não /chat/completions)
        base_url = (data.base_url or "").rstrip("/")
        if not base_url:
            return {"ok": False, "error": "URL obrigatória para qwen3 (base do embedding ex: https://hub-gpus.claro.com.br/qwen3/v1)"}
        chat_url = f"{base_url}/embeddings" if base_url.endswith("/v1") else f"{base_url}/v1/embeddings"
        api_key = data.api_key or "not-needed"

    # Qwen3 testa /embeddings — payload diferente
    if provider == "qwen3":
        payload = {"model": data.model, "input": "ping"}
        # Se operador escolheu densidade no UI, propaga p/ que o teste reflita
        # a configuração real. Sem isso, o sample 'dim=' sempre mostra o default.
        if data.dimensions and int(data.dimensions) > 0:
            payload["dimensions"] = int(data.dimensions)
    else:
        payload = {
            "messages": [
                {"role": "system", "content": "Você é um agente de teste. Responda em uma única palavra."},
                {"role": "user", "content": "Diga: pong"},
            ],
            "temperature": 0.0,
            "max_tokens": 16,
        }
        # Azure não aceita "model" no body (deployment já vai no path).
        if provider != "azure":
            payload["model"] = data.model

    headers = {"Content-Type": "application/json"}
    if use_bearer:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        # Azure usa header dedicado `api-key`.
        headers["api-key"] = api_key
    start = _time.time()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(chat_url, json=payload, headers=headers)
        latency = round((_time.time() - start) * 1000, 0)
        if r.status_code >= 400:
            try:
                err = r.json()
                msg = err.get("error", {}).get("message") if isinstance(err.get("error"), dict) else (err.get("error") or err.get("message") or r.text[:300])
            except Exception:
                msg = r.text[:300]
            return {"ok": False, "status": r.status_code, "latency_ms": latency, "error": str(msg)[:400]}
        data_resp = r.json()
        if provider == "qwen3":
            # /embeddings retorna {data: [{embedding: [...], index: 0}], model, usage}
            items = data_resp.get("data") or []
            dim = len(items[0].get("embedding", [])) if items else 0
            return {
                "ok": True,
                "status": r.status_code,
                "latency_ms": latency,
                "model": data_resp.get("model") or data.model,
                "sample": f"embedding dim={dim}",
                "tokens": (data_resp.get("usage") or {}).get("total_tokens") or 0,
            }
        try:
            sample = data_resp["choices"][0]["message"]["content"][:120]
        except Exception:
            sample = ""
        usage = data_resp.get("usage") or {}
        return {
            "ok": True,
            "status": r.status_code,
            "latency_ms": latency,
            "model": data_resp.get("model") or data.model,
            "sample": sample,
            "tokens": usage.get("total_tokens") or 0,
        }
    except httpx.ConnectError as e:
        return {"ok": False, "latency_ms": round((_time.time() - start) * 1000, 0), "error": f"Falha de conexão: {str(e)[:200]}"}
    except httpx.TimeoutException:
        return {"ok": False, "latency_ms": round((_time.time() - start) * 1000, 0), "error": "Timeout (30s)"}
    except Exception as e:
        return {"ok": False, "latency_ms": round((_time.time() - start) * 1000, 0), "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ═══ System Prompts ═══

class SystemPromptCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    category: str = "geral"
    kind: str = "subagent"
    prompt_text: str
    variables: Optional[str] = "[]"
    is_default: bool = False

class SystemPromptUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    kind: Optional[str] = None
    prompt_text: Optional[str] = None
    variables: Optional[str] = None
    is_default: Optional[bool] = None

@router.get("/system-prompts")
async def list_system_prompts(category: str = None, kind: str = None, limit: int = 100):
    f = {}
    if category: f["category"] = category
    if kind: f["kind"] = kind
    return {"prompts": await prompts_repo.find_all(limit=limit, **f), "total": await prompts_repo.count(**f)}

@router.get("/system-prompts/{prompt_id}")
async def get_system_prompt(prompt_id: str):
    p = await prompts_repo.find_by_id(prompt_id)
    if not p: raise HTTPException(404, "Prompt não encontrado")
    return p

@router.post("/system-prompts", status_code=201)
async def create_system_prompt(data: SystemPromptCreate):
    pid = str(uuid.uuid4())
    d = {"id": pid, **data.model_dump()}
    d["is_default"] = 1 if data.is_default else 0
    await prompts_repo.create(d)
    await audit_repo.create({"entity_type":"system_prompt","entity_id":pid,"action":"created","details":json.dumps({"name":data.name})})
    return {"id": pid, "message": "System prompt criado"}

@router.put("/system-prompts/{prompt_id}")
async def update_system_prompt(prompt_id: str, data: SystemPromptUpdate):
    existing = await prompts_repo.find_by_id(prompt_id)
    if not existing: raise HTTPException(404)
    upd = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    if "is_default" in upd: upd["is_default"] = 1 if upd["is_default"] else 0
    if "prompt_text" in upd: upd["version"] = existing.get("version", 1) + 1
    if not upd:
        return existing
    return await prompts_repo.update(prompt_id, upd)

@router.delete("/system-prompts/{prompt_id}")
async def delete_system_prompt(prompt_id: str):
    if not await prompts_repo.delete(prompt_id): raise HTTPException(404)
    return {"message": "System prompt removido"}


# ═══ Busca Global ═══

@router.get("/search")
async def global_search(q: str = "", limit: int = 5):
    """Busca global em agentes, skills, sessões, prompts e bases de conhecimento."""
    if not q or len(q.strip()) < 2:
        return {"results": []}

    results = []

    agents = await agents_repo.search(q, ["name", "description", "domain", "system_prompt"])
    for a in agents[:limit]:
        results.append({"type": "agent", "id": a["id"], "title": a["name"], "subtitle": f"{a.get('kind','')} · {a.get('model','')}", "url": f"/agents/{a['id']}/edit", "icon": "agent"})

    skills = await skills_repo.search(q, ["name", "purpose", "domain", "raw_content"])
    for s in skills[:limit]:
        results.append({"type": "skill", "id": s["id"], "title": s["name"], "subtitle": f"{s.get('kind','')} · v{s.get('version','')}", "url": f"/skills/{s['id']}/edit", "icon": "skill"})

    sessions = await interactions_repo.search(q, ["title", "channel", "state"])
    for s in sessions[:limit]:
        results.append({"type": "session", "id": s["id"], "title": s.get("title") or "Sessão", "subtitle": f"{s.get('state','')} · {s.get('channel','')}", "url": f"/workspace?session={s['id']}", "icon": "session"})

    prompts = await prompts_repo.search(q, ["name", "description", "prompt_text", "category"])
    for p in prompts[:limit]:
        results.append({"type": "prompt", "id": p["id"], "title": p["name"], "subtitle": f"{p.get('kind','')} · {p.get('category','')}", "url": "/settings", "icon": "prompt"})

    sources = await knowledge_repo.search(q, ["name", "description", "source_type"])
    for s in sources[:limit]:
        results.append({"type": "knowledge", "id": s["id"], "title": s["name"], "subtitle": s.get("source_type", ""), "url": "/evidence", "icon": "knowledge"})

    return {"results": results, "total": len(results), "query": q}
