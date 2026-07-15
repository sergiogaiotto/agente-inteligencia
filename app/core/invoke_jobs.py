"""Job store DURÁVEL do invoke assíncrono 202 — Onda 6 (34.0.0).

POST /pipelines/{id}/invoke/async valida TUDO no aceite (mesmos gates do sync),
persiste o contexto validado em `invoke_jobs` e devolve 202 + job_id; a execução
roda numa task deste processo e o cliente faz polling em GET /jobs/{job_id}.

Decisões (deliberadas, ≠ verifier_jobs onde o retry é barato):
- O INSERT do job é ON-PATH e fail-loud: o 202 devolve um id que TEM que existir
  (desvio consciente do padrão best-effort do dispatcher do juiz).
- Órfão 'running' de um boot anterior vira 'lost' e NUNCA é re-executado — um
  invoke paga LLM e pode ter efeitos colaterais (bindings HTTP). Só 'queued'
  (nunca começou a executar) é retomado no boot/reaper.
- O write de conclusão tem retry: se falhasse silencioso (padrão best-effort),
  o cliente pollaria para sempre um job já terminado.
- O reaper é o PRIMEIRO loop periódico do app: retenção de jobs terminais +
  despacho de 'queued' quando abre vaga (o verifier só retoma no boot).
- Single-process por design (uvicorn 1 worker, mesmo pressuposto do resto do
  app): o claim atômico (UPDATE ... WHERE status='queued' RETURNING) já deixa
  o caminho pronto para multi-worker sem double-run.
"""
import asyncio
import json
import logging
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

REAPER_INTERVAL_SECONDS = 60
# Teto de latência das CARONAS de higiene (sweep/purga) no loop do reaper — uma
# carona lenta sob contenção de pool não atrasa o despacho de jobs (35.14.4).
_CARONA_TIMEOUT_S = 30.0
# 'running' mais velho que isto SEM task viva neste processo = zumbi (a folga
# cobre a janela entre o claim e o registro em _active_job_ids — que na prática
# é zero, pois o id entra no set ANTES do claim).
_ZOMBIE_GRACE_MINUTES = 10

# Retenção in-process (anti-GC + inventário p/ o zumbi-check e o shutdown).
_active_tasks: set = set()
_active_job_ids: set = set()
_reaper_task: Optional[asyncio.Task] = None


def _pool():
    from app.core.database import _get_pool
    return _get_pool()


def _max_concurrent() -> int:
    from app.core.config import get_settings
    try:
        return max(1, int(get_settings().invoke_jobs_max_concurrent or 4))
    except Exception:
        return 4


# ─────────────────────────────────────────────────────────────────────────────
# Criação (ON-PATH no POST 202) + Idempotency-Key
# ─────────────────────────────────────────────────────────────────────────────

async def find_existing_job(*, owner_user_id: Optional[str], api_key_id: Optional[str],
                            pipeline_id: str, idempotency_key: Optional[str]) -> Optional[dict]:
    """Replay-lookup da Idempotency-Key. A rota chama ANTES dos gates mutáveis
    (orçamento/aposentado/escopo) — review adversarial: um retry legítimo NÃO
    pode perder acesso ao job que já pagou porque o débito do próprio job
    estourou o orçamento no meio-tempo. Escopo POR KEY (COALESCE): integrações
    irmãs do mesmo dono não colidem chaves entre si."""
    if not idempotency_key:
        return None
    async with _pool().acquire() as con:
        row = await con.fetchrow(
            "SELECT * FROM invoke_jobs WHERE owner_user_id = $1 AND pipeline_id = $2 "
            "AND idempotency_key = $3 AND COALESCE(api_key_id, '') = COALESCE($4, '')",
            owner_user_id, pipeline_id, idempotency_key, api_key_id,
        )
    return dict(row) if row else None


async def create_job(*, pipeline_id: str, owner_user_id: Optional[str],
                     api_key_id: Optional[str], idempotency_key: Optional[str],
                     request_payload: dict, customer_hash: Optional[str] = None) -> tuple[dict, bool]:
    """INSERT do job. Retorna (job, created).

    Replay de Idempotency-Key (mesmo dono+key-criadora+pipeline+chave) NÃO cria
    linha nova: o UNIQUE parcial dispara o ON CONFLICT DO NOTHING e devolvemos o
    job EXISTENTE (created=False) — é assim que o retry de um proxy com timeout
    deixa de re-executar pagando LLM. A comparação de corpo (request_hash)
    fica com o caller (409 se a mesma key vier com payload diferente)."""
    job_id = f"ij_{uuid.uuid4().hex[:16]}"
    payload_txt = json.dumps(request_payload, ensure_ascii=False, default=str)
    async with _pool().acquire() as con:
        row = await con.fetchrow(
            "INSERT INTO invoke_jobs (id, pipeline_id, owner_user_id, api_key_id, "
            "idempotency_key, request_payload, status, customer_hash) "
            "VALUES ($1, $2, $3, $4, $5, $6, 'queued', $7) "
            "ON CONFLICT DO NOTHING RETURNING *",
            job_id, pipeline_id, owner_user_id, api_key_id, idempotency_key, payload_txt,
            customer_hash,
        )
    if row:
        return dict(row), True
    # Conflito = replay da Idempotency-Key (único UNIQUE além da PK aleatória).
    existing = await find_existing_job(
        owner_user_id=owner_user_id, api_key_id=api_key_id,
        pipeline_id=pipeline_id, idempotency_key=idempotency_key,
    )
    if existing:
        return existing, False
    raise RuntimeError("invoke_jobs: INSERT conflitou sem job existente (corrida de replay?)")


# ─────────────────────────────────────────────────────────────────────────────
# Despacho + worker
# ─────────────────────────────────────────────────────────────────────────────

def dispatch(job_id: str) -> bool:
    """Agenda a execução se houver vaga (invoke_jobs_max_concurrent). Sem vaga,
    o job fica 'queued' e o reaper despacha quando abrir. Retorna se agendou."""
    if job_id in _active_job_ids:
        return False  # já em voo neste processo
    if len(_active_job_ids) >= _max_concurrent():
        return False
    try:
        # id entra no set ANTES do claim — o zumbi-check do reaper nunca vê um
        # 'running' recém-claimado como órfão.
        _active_job_ids.add(job_id)
        task = asyncio.create_task(_run_job(job_id), name=f"invoke_job_{job_id[-8:]}")
    except RuntimeError:
        _active_job_ids.discard(job_id)
        logger.warning("event=invoke_job_no_loop job_id=%s", job_id)
        return False
    _active_tasks.add(task)

    def _done(t, jid=job_id):
        _active_tasks.discard(t)
        _active_job_ids.discard(jid)

    task.add_done_callback(_done)
    return True


def _record_async_failure(status: str, duration_s: float = 0.0) -> None:
    """RED (35.14.3): invoke_async que falhou conta nas métricas — o worker
    só registrava no SUCESSO e no TIMEOUT; os demais error branches (aposentado,
    session, key revogada, orçamento, ValueError, Exception, payload corrupt)
    ficavam invisíveis ao dashboard/alerta.

    `duration_s` (35.14.7, achado de auditoria #3): abortos PÓS-execução
    (timeout/rejected/error) alimentam o histograma de latência RED com o tempo
    real gasto — antes o custo parcial o fazia via _record_invoke_analytics, mas
    o emit_metrics=False (35.14.6) que fechou a dupla-contagem também apagou a
    ÚNICA amostra de latência dos timeouts (p95/p99 cegos a abortos). Rechecks
    pré-execução mantêm 0.0 (não houve trabalho a medir)."""
    try:
        from app.core.metrics import record_invocation
        record_invocation(kind="invoke_async", status=status,
                          duration_s=duration_s, error=True)
    except Exception:
        logger.warning("event=invoke_async_failure_metric_failed status=%s", status)


def _schedule_partial_cost(job: dict, req: dict, done_steps: list, final_state: str) -> None:
    """Agenda o custo dos steps CONCLUÍDOS antes de um aborto (timeout/erro/
    rejeição) para o SSOT invocation_costs + débito por-key + RED — o gasto de
    LLM já realizado NÃO some (35.14.4: antes só o timeout o preservava, os
    demais aborts o descartavam). Best-effort; result sintético."""
    if not done_steps:
        return
    try:
        _iid = next((s.get("interaction_id") for s in done_steps
                     if s.get("interaction_id")), None)
        synthetic = {
            "status": "failed", "final_state": final_state,
            "interaction_id": _iid,
            "duration_ms": sum(float(s.get("duration_ms") or 0) for s in done_steps),
            "pipeline_steps": done_steps,
        }
        from app.core.analytics_tasks import schedule_analytics
        from app.routes.pipelines import _record_invoke_analytics
        schedule_analytics(_record_invoke_analytics(
            pid=job.get("pipeline_id"), root=req.get("root"),
            member_count=len(req.get("members") or []), result=synthetic,
            api_key_id=req.get("api_key_id"), api_key_name=req.get("api_key_name"),
            actor_user_id=job.get("owner_user_id"),
            arg_keys=req.get("arg_keys") or [], channel=req.get("channel"),
            kind="invoke_async",
            # RED já é contabilizado pelo _record_async_failure do MESMO branch de
            # aborto — sem isto, o synthetic status='failed' contava um 2º erro
            # (dupla-contagem só em aborto com steps parciais, 35.14.6).
            emit_metrics=False,
        ))
    except Exception:
        logger.warning("event=invoke_job_partial_cost_failed job_id=%s", job.get("id"))


async def _run_job(job_id: str) -> None:
    """Worker de UM job: claim atômico → rechecks baratos → execute_pipeline →
    stamp de posse → persistência do resultado → analytics off-path."""
    async with _pool().acquire() as con:
        row = await con.fetchrow(
            "UPDATE invoke_jobs SET status='running', attempts=attempts+1, "
            "started_at=COALESCE(started_at, now()), updated_at=now() "
            "WHERE id=$1 AND status='queued' RETURNING *",
            job_id,
        )
    if not row:
        return  # outro caminho (boot × reaper) já claimou/terminou — no-op
    job = dict(row)
    try:
        req = json.loads(job.get("request_payload") or "{}")
    except Exception:
        _record_async_failure("payload_corrupt")
        await _finish_failed(job_id, {"error": "job_payload_corrupt"})
        return

    pid = job.get("pipeline_id")
    # Rechecks baratos: o mundo pode mudar entre o 202 e a execução.
    try:
        from app.core.database import pipelines_repo
        p = await pipelines_repo.find_by_id(pid)
        if not p or p.get("status") == "aposentado":
            await _finish_failed(job_id, {
                "error": "pipeline_not_invocable",
                "hint": "O pipeline foi removido ou aposentado depois do aceite do job.",
            })
            _record_async_failure("pipeline_not_invocable")
            _notify_finish(job_id, pid, req, "failed", "pipeline_not_invocable")
            return
        # TOCTOU do IDOR (review): a posse do session_id foi checada no aceite,
        # mas uma interaction legada-sem-dono pode GANHAR dono no meio-tempo —
        # não injetar a conversa de outro usuário no LLM.
        sess = req.get("session_id")
        if sess:
            from app.core.interaction_access import owner_of_interaction
            sess_owner = await owner_of_interaction(sess)
            if sess_owner and sess_owner != job.get("owner_user_id"):
                await _finish_failed(job_id, {"error": "session_not_accessible"})
                _record_async_failure("session_not_accessible")
                _notify_finish(job_id, pid, req, "failed", "session_not_accessible")
                return
        api_key_id = req.get("api_key_id")
        if api_key_id:
            # Key revogada entre o aceite e a execução → não executa em nome dela.
            async with _pool().acquire() as con:
                krow = await con.fetchrow(
                    "SELECT revoked_at FROM api_keys WHERE id = $1", api_key_id)
            if krow is None or krow["revoked_at"] is not None:
                await _finish_failed(job_id, {"error": "api_key_revoked"})
                _record_async_failure("api_key_revoked")
                _notify_finish(job_id, pid, req, "failed", "api_key_revoked")
                return
            from fastapi import HTTPException
            from app.core.api_key_budget import enforce_budget
            try:
                await enforce_budget(api_key_id)
            except HTTPException as e:
                detail = e.detail if isinstance(e.detail, dict) else {"error": "cost_budget_exceeded"}
                await _finish_failed(job_id, detail)
                _record_async_failure("cost_budget_exceeded")
                _notify_finish(job_id, pid, req, "failed", "cost_budget_exceeded")
                return
    except Exception:
        # Recheck é defesa extra — falha DELE não pode matar um job válido.
        logger.warning("event=invoke_job_recheck_failed job_id=%s", job_id, exc_info=True)

    from app.agents.engine import execute_pipeline
    # Deadline por job (35.4.0, fast-follow do #590): um execute_pipeline
    # pendurado (LLM/tool travado além dos timeouts internos) ocupava vaga do
    # cap PARA SEMPRE — o reaper de propósito não mata 'running' com task viva.
    # wait_for CANCELA a execução no estouro (o engine aborta a chamada em
    # curso; persistências parciais de steps já concluídos são aceitas).
    from app.core.config import get_settings as _gs
    try:
        _timeout_min = float(_gs().invoke_job_timeout_minutes or 30)
    except Exception:
        _timeout_min = 30.0

    # Coletor de steps CONCLUÍDOS (review do FF4): num timeout, o gasto REAL de
    # LLM dos steps que completaram não pode sumir do ledger/orçamento — o
    # agent_done carrega cost_usd/tokens_used/interaction_id (35.4.0) e este
    # callback os acumula fora da task cancelável.
    _done_steps: list = []

    async def _collect(event) -> None:
        if isinstance(event, dict) and event.get("type") == "agent_done":
            _done_steps.append({
                "agent_id": event.get("agent_id"),
                "agent_name": event.get("agent_name"),
                "status": "completed",
                "cost_usd": event.get("cost_usd") or 0.0,
                "tokens_used": event.get("tokens_used") or 0,
                "duration_ms": event.get("duration_ms") or 0,
                "interaction_id": event.get("interaction_id"),
            })

    # Re-herança do pivô LGPD em RUNTIME (35.15.1, achado da auditoria #4): a
    # herança no ACEITE (customer_hash_of_interaction) podia perder a corrida —
    # um follow-up sem customer_ref aceito ANTES do 1º job criar/carimbar a
    # interaction nascia com hash NULL e a conversa (request_payload/result)
    # sobrevivia ao forget. Aqui a interaction da sessão JÁ existe (o worker roda
    # depois): re-resolve e PERSISTE no job (para o forget o alcançar).
    _job_chash = job.get("customer_hash")
    if not _job_chash and req.get("session_id"):
        from app.core.interaction_access import customer_hash_of_interaction
        _job_chash = await customer_hash_of_interaction(req.get("session_id"))
        if _job_chash:
            try:
                async with _pool().acquire() as con:
                    await con.execute(
                        "UPDATE invoke_jobs SET customer_hash = $1 "
                        "WHERE id = $2 AND customer_hash IS NULL", _job_chash, job_id)
            except Exception:
                logger.warning("event=invoke_job_rehash_failed job_id=%s", job_id)

    # Relógio do trecho executável (35.14.7): latência REAL dos abortos pós-
    # execução → histograma RED (o custo parcial não a alimenta mais desde o
    # emit_metrics=False do 35.14.6).
    _exec_t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(
            execute_pipeline(
                entry_agent_id=req.get("root"),
                user_input=req.get("user_input") or "",
                channel=req.get("channel") or "api",
                session_id=req.get("session_id"),
                context_mode=req.get("context_mode") or "auto",
                attachments=req.get("attachments") or None,
                allowed_agent_ids=set(req.get("members") or []) or None,  # SELA ao subgrafo
                sealed_inputs=req.get("sealed_inputs") or None,
                pipeline_id=pid,
                progress_callback=_collect,
                # Dono na CRIAÇÃO (review do FF4): o cancel do deadline era o 1º
                # aborto DETERMINÍSTICO pós-criação — sem isto, master/filhas
                # ficavam órfãs SEM dono (listáveis/sequestráveis: IDOR).
                owner_user_id=job.get("owner_user_id"),
                customer_hash=_job_chash,  # 35.14.2: hash (não ref cru); re-herdado 35.15.1
            ),
            timeout=_timeout_min * 60.0,
        )
    except (TimeoutError, asyncio.TimeoutError):
        # ANTES do except Exception (TimeoutError herda de Exception): o
        # cliente recebe um erro nomeado e a vaga do cap é liberada.
        logger.error("event=invoke_job_timeout job_id=%s pipeline_id=%s timeout_min=%s",
                     job_id, pid, _timeout_min)
        await _finish_failed(job_id, {
            "error": "job_timeout",
            "timeout_minutes": _timeout_min,
            "hint": "A execução excedeu o deadline (invoke_job_timeout_minutes) "
                    "e foi cancelada. Ajuste o parâmetro se o pipeline for "
                    "legitimamente longo.",
        })
        # O gasto dos steps CONCLUÍDOS entra no ledger/orçamento/RED mesmo no
        # aborto (custo parcial não some — 35.14.4).
        _schedule_partial_cost(job, req, _done_steps, "JobTimeout")
        _record_async_failure("timeout", time.monotonic() - _exec_t0)
        _notify_finish(job_id, pid, req, "failed", "job_timeout")
        return
    except ValueError as e:
        # Paridade com o sync (409): mensagem do engine é controlada/exposta.
        await _finish_failed(job_id, {"error": "pipeline_execution_rejected",
                                      "detail": str(e)[:300]})
        _schedule_partial_cost(job, req, _done_steps, "Rejected")  # 35.14.4
        _record_async_failure("rejected", time.monotonic() - _exec_t0)
        _notify_finish(job_id, pid, req, "failed", "pipeline_execution_rejected")
        return
    except Exception:
        # Paridade com o sync (500): NUNCA vazar str(e) ao cliente do GET.
        logger.exception("event=invoke_job_failed job_id=%s pipeline_id=%s request_id=%s",
                         job_id, pid, req.get("request_id"))
        await _finish_failed(job_id, {
            "error": "pipeline_execution_failed",
            "request_id": req.get("request_id"),
            "hint": "Falha ao executar o pipeline. Cite o request_id ao suporte.",
        })
        _schedule_partial_cost(job, req, _done_steps, "Failed")  # 35.14.4
        _record_async_failure("error", time.monotonic() - _exec_t0)
        _notify_finish(job_id, pid, req, "failed", "pipeline_execution_failed")
        return

    # Posse ANTES de expor o interaction_id ao polling (primitivo de segurança —
    # mesmo racional do await no caminho sync).
    try:
        from app.core.interaction_access import stamp_interaction_owner
        await stamp_interaction_owner((result or {}).get("interaction_id"),
                                      job.get("owner_user_id"))
    except Exception:
        logger.warning("event=invoke_job_stamp_failed job_id=%s", job_id)

    r = result or {}
    payload_full = {
        "pipeline_id": pid,
        "status": r.get("status", "completed"),
        "output": r.get("output", ""),
        "output_agent": r.get("output_agent"),  # Pacote B: QUEM respondeu
        "final_state": r.get("final_state"),
        "interaction_id": r.get("interaction_id"),
        "total_agents": r.get("total_agents", 0),
        "completed_agents": r.get("completed_agents", 0),
        "pipeline_steps": r.get("pipeline_steps", []),
        "duration_ms": r.get("duration_ms"),
        # Cond-C (36.1.0): sem esta chave o dado se PERDIA na persistência do
        # job — o polling do 202 é o único canal do consumidor async (o texto
        # persistido já vem strippado do engine; major do review pré-push).
        "decision": r.get("decision"),
    }
    await _finish_completed(job_id, payload_full)
    _notify_finish(job_id, pid, req, "completed")

    # Analytics/custo/métricas: o MESMO recorder off-path do sync (auditoria +
    # atribuição por-key + débito + SSOT invocation_costs + Prometheus), com
    # kind próprio — invocações async não podem sumir do RED/ledger.
    try:
        from app.core.analytics_tasks import schedule_analytics
        from app.routes.pipelines import _record_invoke_analytics
        schedule_analytics(_record_invoke_analytics(
            pid=pid, root=req.get("root"),
            member_count=len(req.get("members") or []), result=result,
            api_key_id=req.get("api_key_id"), api_key_name=req.get("api_key_name"),
            actor_user_id=job.get("owner_user_id"),
            arg_keys=req.get("arg_keys") or [], channel=req.get("channel"),
            kind="invoke_async",
        ))
    except Exception:
        logger.warning("event=invoke_job_analytics_failed job_id=%s", job_id)


async def _finish_completed(job_id: str, payload_full: dict) -> None:
    """Persistência de conclusão COM retry — falha silenciosa aqui deixaria o
    cliente pollando para sempre um job que já terminou."""
    txt = json.dumps(payload_full, ensure_ascii=False, default=str)
    await _finish_write(
        job_id,
        "UPDATE invoke_jobs SET status='completed', result_payload=$2, error=NULL, "
        "finished_at=now(), updated_at=now() WHERE id=$1",
        txt,
    )


async def _finish_failed(job_id: str, error_obj: dict) -> None:
    txt = json.dumps(error_obj, ensure_ascii=False, default=str)
    await _finish_write(
        job_id,
        "UPDATE invoke_jobs SET status='failed', error=$2, "
        "finished_at=now(), updated_at=now() WHERE id=$1",
        txt,
    )


async def _finish_write(job_id: str, sql: str, arg: str, retries: int = 3) -> None:
    for attempt in range(retries):
        try:
            async with _pool().acquire() as con:
                await con.execute(sql, job_id, arg)
            return
        except Exception as e:
            if attempt == retries - 1:
                logger.error(
                    "event=invoke_job_finish_write_failed job_id=%s error=%s "
                    "(cliente pode ficar pollando um running fantasma; o reaper "
                    "marcará como lost)", job_id, str(e)[:200],
                )
            else:
                await asyncio.sleep(0.5 * (attempt + 1))


# ─────────────────────────────────────────────────────────────────────────────
# Boot-resume + reaper + shutdown (fiação no lifespan de app/main.py)
# ─────────────────────────────────────────────────────────────────────────────

_LOST_ERROR = {"error": "job_interrupted",
               "hint": "A execução foi interrompida (restart da aplicação ou falha "
                       "ao persistir o resultado). Re-submeta o invoke (com uma "
                       "NOVA Idempotency-Key) se ainda precisar."}


# ─────────────────────────────────────────────────────────────────────────────
# Webhook de conclusão (35.6.0, padrão fallback: callback_url por request >
# webhook_url da API-key). Payload LEVE — nunca envia o result (o receptor
# busca via GET autenticado): não exfiltra PII para a URL e o SSRF vira só
# um ping assinado. HMAC-SHA256 com segredo = key_hash (sha256 da key — o
# cliente DETÉM a key e deriva o mesmo segredo; nada novo p/ gerir); jobs de
# cookie/UI assinam com MAESTRO_SECRET_KEY.
# ─────────────────────────────────────────────────────────────────────────────

WEBHOOK_ATTEMPTS = 3
WEBHOOK_TIMEOUT_S = 10.0


async def _webhook_secret(api_key_id: Optional[str]) -> str:
    if api_key_id:
        try:
            async with _pool().acquire() as con:
                row = await con.fetchrow(
                    "SELECT key_hash FROM api_keys WHERE id = $1", api_key_id)
            if row and row.get("key_hash"):
                return str(row["key_hash"])
        except Exception:
            pass
    import os
    return os.environ.get("MAESTRO_SECRET_KEY", "") or "maestro-webhook"


async def _deliver_webhook(url: str, payload: dict, api_key_id: Optional[str]) -> None:
    """POST assinado, best-effort com retries — NUNCA afeta o job.
    Re-valida SSRF no ENVIO (DNS pode ter mudado desde o aceite)."""
    import hashlib as _hashlib
    import hmac as _hmac
    try:
        from app.core.ssrf import validate_public_url
        validate_public_url(url, allow_http=True)
    except Exception as e:
        logger.warning("event=invoke_job_webhook_blocked url_invalid error=%s", str(e)[:150])
        return
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    secret = await _webhook_secret(api_key_id)
    signature = _hmac.new(secret.encode("utf-8"), body, _hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Maestro-Event": str(payload.get("event") or "invoke_job.finished"),
        "X-Maestro-Signature": f"sha256={signature}",
    }
    import httpx
    for attempt in range(WEBHOOK_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_S,
                                         follow_redirects=False) as client:
                resp = await client.post(url, content=body, headers=headers)
            if resp.status_code < 300:
                logger.info("event=invoke_job_webhook_delivered job_id=%s status=%s",
                            payload.get("job_id"), resp.status_code)
                return
            logger.warning("event=invoke_job_webhook_rejected job_id=%s http=%s attempt=%s",
                           payload.get("job_id"), resp.status_code, attempt + 1)
        except Exception as e:
            logger.warning("event=invoke_job_webhook_failed job_id=%s attempt=%s error=%s",
                           payload.get("job_id"), attempt + 1, str(e)[:150])
        await asyncio.sleep(1.0 * (attempt + 1))


def _notify_finish(job_id: str, pipeline_id: Optional[str], req: dict,
                   status: str, error_code: Optional[str] = None) -> None:
    """Agenda a notificação off-path (fila de analytics, drenada no shutdown).
    No-op sem webhook resolvido no aceite."""
    url = (req or {}).get("webhook_url")
    if not url:
        return
    payload = {
        "event": "invoke_job.finished",
        "job_schema_version": "1",
        "job_id": job_id,
        "pipeline_id": pipeline_id,
        "status": status,
        "status_url": f"/api/v1/pipelines/{pipeline_id}/jobs/{job_id}",
        **({"error": error_code} if error_code else {}),
        **({"idempotency_key": req.get("idempotency_key")} if req.get("idempotency_key") else {}),
    }
    try:
        from app.core.analytics_tasks import schedule_analytics
        schedule_analytics(_deliver_webhook(url, payload, (req or {}).get("api_key_id")))
    except Exception:
        logger.warning("event=invoke_job_webhook_schedule_failed job_id=%s", job_id)


def _async_enabled() -> bool:
    from app.core.config import get_settings
    try:
        return bool(get_settings().invoke_async_enabled)
    except Exception:
        return False


async def resume_invoke_jobs() -> dict:
    """Boot (lifespan, após init_db, antes de servir): 'running' órfão → 'lost'
    (nunca re-executa — ver docstring do módulo); 'queued' → despacha até o cap.

    Kill-switch (review adversarial): com invoke_async_enabled OFF, a HIGIENE
    (running órfão → lost) roda sempre, mas NADA da fila é despachado — desligar
    o toggle tem que parar o backlog, não só os 202 novos. Religar retoma."""
    out = {"lost": 0, "dispatched": 0}
    async with _pool().acquire() as con:
        lost_rows = await con.fetch(
            "UPDATE invoke_jobs SET status='lost', error=$1, finished_at=now(), "
            "updated_at=now() WHERE status='running' "
            "RETURNING id, pipeline_id, request_payload",
            json.dumps(_LOST_ERROR, ensure_ascii=False),
        )
        out["lost"] = len(lost_rows)
        for lr in lost_rows:
            try:
                _lreq = json.loads(lr.get("request_payload") or "{}")
                _notify_finish(lr["id"], lr.get("pipeline_id"), _lreq, "lost", "job_interrupted")
            except Exception:
                pass
        rows = []
        if _async_enabled():
            rows = await con.fetch(
                "SELECT id FROM invoke_jobs WHERE status='queued' ORDER BY created_at LIMIT $1",
                _max_concurrent(),
            )
    for r in rows:
        if dispatch(r["id"]):
            out["dispatched"] += 1
    if out["lost"] or out["dispatched"]:
        logger.info("event=invoke_jobs_resumed lost=%s dispatched=%s",
                    out["lost"], out["dispatched"])
    return out


async def reap_once() -> dict:
    """Uma passada do reaper: retenção de terminais + zumbis + despacho de queued."""
    from app.core.config import get_settings
    out = {"deleted": 0, "lost": 0, "dispatched": 0}
    try:
        retention_h = float(get_settings().invoke_jobs_retention_hours or 72)
    except Exception:
        retention_h = 72.0
    async with _pool().acquire() as con:
        res = await con.execute(
            "DELETE FROM invoke_jobs WHERE status IN ('completed','failed','lost') "
            "AND updated_at < now() - ($1 * interval '1 hour')",
            retention_h,
        )
        try:
            out["deleted"] = int(str(res).split()[-1])
        except Exception:
            pass
        # Zumbi: 'running' velho SEM task viva neste processo (ex.: o write de
        # conclusão falhou nas 3 tentativas). Single-process → o set é autoridade.
        rows = await con.fetch(
            "SELECT id FROM invoke_jobs WHERE status='running' "
            "AND updated_at < now() - ($1 * interval '1 minute')",
            float(_ZOMBIE_GRACE_MINUTES),
        )
        zombies = [r["id"] for r in rows if r["id"] not in _active_job_ids]
        if zombies:
            await con.execute(
                "UPDATE invoke_jobs SET status='lost', error=$2, finished_at=now(), "
                "updated_at=now() WHERE id = ANY($1) AND status='running'",
                zombies, json.dumps(_LOST_ERROR, ensure_ascii=False),
            )
            out["lost"] = len(zombies)
        # Kill-switch: OFF = a fila congela (queued fica queued; nada paga LLM).
        # Retenção + zumbi-check acima são higiene e rodam SEMPRE.
        queued = []
        if _async_enabled():
            queued = await con.fetch(
                "SELECT id FROM invoke_jobs WHERE status='queued' ORDER BY created_at LIMIT 50",
            )
    for r in queued:
        if dispatch(r["id"]):
            out["dispatched"] += 1
    return out


async def _reaper_loop() -> None:
    while True:
        await asyncio.sleep(REAPER_INTERVAL_SECONDS)
        # reap_once é o despacho de jobs — roda SEM timeout (é o trabalho
        # principal). As CARONAS (sweep do juiz, purga de retenção) ganham um
        # teto de latência (35.14.4): sob contenção de pool, uma carona lenta
        # (ex.: cascade DELETE de 500 interactions segurando conexão) NÃO pode
        # atrasar o próximo tick de despacho de invoke-jobs.
        try:
            await reap_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("event=invoke_jobs_reaper_failed error=%s", str(e)[:200])
        # Carona (35.3.0): sweep periódico da fila do JUIZ.
        try:
            from app.verifier.async_dispatcher import sweep_pending
            await asyncio.wait_for(sweep_pending(), timeout=_CARONA_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("event=verifier_sweep_timeout")
        except Exception as e:
            logger.warning("event=verifier_sweep_failed error=%s", str(e)[:200])
        # Carona (35.8.0, arco LGPD-1): retenção de conversas por IDADE.
        # Auto-limita a ~1x/hora (o loop roda a cada 60s); no-op quando o
        # setting é 0. try/except PRÓPRIO — falha na purga não derruba o reaper.
        try:
            from app.core.retention import maybe_purge
            await asyncio.wait_for(maybe_purge(), timeout=_CARONA_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("event=retention_purge_timeout")
        except Exception as e:
            logger.warning("event=retention_purge_failed error=%s", str(e)[:200])


def start_reaper() -> None:
    """Cria o loop do reaper (1º task periódico do app). Idempotente."""
    global _reaper_task
    if _reaper_task is not None and not _reaper_task.done():
        return
    _reaper_task = asyncio.create_task(_reaper_loop(), name="invoke_jobs_reaper")


async def shutdown_invoke_jobs(timeout: float = 5.0) -> None:
    """Shutdown gracioso ANTES do close_db: cancela o reaper, espera os jobs
    ativos até o timeout e marca os restantes como 'lost' (o cliente não fica
    pollando um 'running' que nunca vai terminar — o processo está morrendo)."""
    global _reaper_task
    if _reaper_task is not None:
        _reaper_task.cancel()
        await asyncio.gather(_reaper_task, return_exceptions=True)
        _reaper_task = None
    pending = {t for t in _active_tasks if not t.done()}
    if pending:
        await asyncio.wait(pending, timeout=timeout)
    leftovers = sorted(_active_job_ids)
    if leftovers:
        stragglers = [t for t in _active_tasks if not t.done()]
        for t in stragglers:
            t.cancel()  # CancelledError NÃO cai no except Exception do worker
        if stragglers:
            # Aguarda o cancelamento aterrissar ANTES do mark-lost — senão a task
            # morrendo corre contra o close_db (mesma razão dos drains do lifespan).
            await asyncio.gather(*stragglers, return_exceptions=True)
        try:
            async with _pool().acquire() as con:
                await con.execute(
                    "UPDATE invoke_jobs SET status='lost', error=$2, finished_at=now(), "
                    "updated_at=now() WHERE id = ANY($1) AND status='running'",
                    leftovers, json.dumps(_LOST_ERROR, ensure_ascii=False),
                )
            logger.info("event=invoke_jobs_shutdown_lost count=%s", len(leftovers))
        except Exception as e:
            logger.warning("event=invoke_jobs_shutdown_mark_failed error=%s", str(e)[:200])


def _reset_for_tests() -> None:
    """Higiene de estado módulo-level entre testes (lição do breaker #566).
    Cancela workers vivos — um worker vazado escreveria no FakePool do teste
    seguinte."""
    global _reaper_task
    for t in list(_active_tasks):
        try:
            if not t.done():
                t.cancel()
        except RuntimeError:
            pass  # loop do teste anterior já fechou — nada a cancelar
    _active_tasks.clear()
    _active_job_ids.clear()
    try:
        if _reaper_task is not None and not _reaper_task.done():
            _reaper_task.cancel()
    except RuntimeError:
        pass
    _reaper_task = None
