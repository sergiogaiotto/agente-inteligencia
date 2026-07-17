"""Retenção de conversas por IDADE — arco LGPD-1 (35.8.0).

Purga periódica de interactions mais velhas que `interactions_retention_days`
(0 = DESLIGADO, default). Decisão do dono (2026-07-14): **delete + scrub do
texto do juiz** — apaga a conversa mas preserva a LINHA analítica das
verifications (scores/custo → /quality e drift sobrevivem).

Ordem segura de purga por LOTE de interaction_ids (do mapa LGPD):
  1. Seleciona ids antigos (created_at < now - N dias), LIMIT por lote.
  2. SCRUB das verifications (question/draft/reasons → placeholder; a linha
     FICA — cascatear apagaria a auditoria do juiz e quebraria agregados).
  3. Varre pelos MESMOS ids as tabelas que NÃO cascateiam de interactions e
     guardam conteúdo cru: api_call_logs (request/response_body, sem FK),
     verifier_jobs (payload). O forget também apaga invoke_jobs por
     customer_hash — a conversa vive no request_payload (35.14.2).
  4. DELETE das interactions → o CASCADE (#571) leva turns/tool_calls/
     binding_executions.
  5. invocation_costs / api_key_cost_ledger / catalog_costs FICAM (só números
     + ids; interesse legítimo FinOps; suas agregações nunca joinam interactions).

Carona no reaper do invoke_jobs (padrão do sweep do juiz): loop 60s, mas a
purga auto-limita a ~1x/hora (throttle módulo-level) e roda em try/except
isolado — uma falha aqui não derruba retenção/despacho de jobs.
"""
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Throttle: o reaper roda a cada 60s, mas purgar de hora em hora basta e evita
# marteladas no banco. Timestamp da última passada (monotonic); None = nunca.
_PURGE_MIN_INTERVAL_S = 3600.0
_last_purge_at: Optional[float] = None

# Lote por passada: limita o tamanho do DELETE (cascade multiplica) — o
# excedente é purgado nas próximas horas.
_PURGE_BATCH = 500

_SCRUB = "[removido por retenção]"


def _pool():
    from app.core.database import _get_pool
    return _get_pool()


def _retention_days() -> int:
    from app.core.config import get_settings
    try:
        return max(0, int(get_settings().interactions_retention_days or 0))
    except Exception:
        return 0


def _synthetic_retention_days() -> int:
    """Retenção PRÓPRIA das interações sintéticas do harness (43.0.0):
    origin='harness'. Independe da retenção LGPD global — massa de avaliação
    não é conversa de titular e pode (deve) expirar mais cedo."""
    from app.core.config import get_settings
    try:
        return max(0, int(get_settings().harness_synthetic_retention_days or 0))
    except Exception:
        return 0


def _now_monotonic() -> float:
    # time.monotonic() é permitido (≠ time.time()/Date.now()); não retrocede.
    return time.monotonic()


async def purge_interactions_once() -> dict:
    """UMA passada de purga (1 lote). No-op quando desligado. Retorna contadores.
    Best-effort: exceção é logada, nunca propagada ao caller do loop."""
    days = _retention_days()
    if days <= 0:
        return {"deleted": 0, "scrubbed_verifications": 0, "purged_jobs": 0}  # DESLIGADO
    async with _pool().acquire() as con:
        rows = await con.fetch(
            "SELECT id FROM interactions "
            "WHERE created_at < now() - ($1 * interval '1 day') "
            "ORDER BY created_at LIMIT $2",
            float(days), _PURGE_BATCH,
        )
        out = await _purge_ids(con, [r["id"] for r in rows])
        # invoke_jobs guarda a conversa CRUA (request_payload.user_input) e tem
        # ciclo de vida próprio (reaper por invoke_jobs_retention_hours, default
        # 72h). Sem purgá-los AQUI, essa cópia sobrevivia à janela de retenção
        # PROMETIDA por interactions_retention_days — dias além (achado de
        # auditoria 35.14.6). Purga por IDADE, em lote (DELETE não tem LIMIT →
        # subselect). O forget por titular já cobre invoke_jobs em _purge_ids.
        # SÓ status TERMINAL (35.14.7, achado de auditoria #3): apagar
        # queued/running por idade violaria o contrato ("jobs na fila/executando
        # nunca são apagados", config.py) e podia deletar um job EM VOO (cliente
        # pollando a Location do 202 recebe 404; o UPDATE de conclusão vira no-op
        # silencioso). Mesmo filtro do reaper de jobs.
        jres = await con.execute(
            "DELETE FROM invoke_jobs WHERE id IN ("
            "  SELECT id FROM invoke_jobs "
            "  WHERE created_at < now() - ($1 * interval '1 day') "
            "    AND status IN ('completed', 'failed', 'lost') "
            "  ORDER BY created_at LIMIT $2)",
            float(days), _PURGE_BATCH,
        )
        try:
            out["purged_jobs"] = int(str(jres).split()[-1])
        except Exception:
            out["purged_jobs"] = 0
        # Arquivos de upload por IDADE (35.15.0, G): binários mais velhos que a
        # janela — inclui os ÓRFÃOS (nunca associados a titular: upload abandonado
        # sem invoke). Mesma janela em dias.
        out["purged_files"] = await _unlink_uploaded_files(con, older_than_days=float(days))
    if out["deleted"] or out["scrubbed_verifications"] or out.get("purged_jobs") \
            or out.get("purged_files"):
        logger.info("event=retention_purged deleted=%s scrubbed_verifications=%s "
                    "purged_jobs=%s purged_files=%s days=%s",
                    out["deleted"], out["scrubbed_verifications"],
                    out.get("purged_jobs", 0), out.get("purged_files", 0), days)
    return out


async def _purge_ids(con, ids: list) -> dict:
    """Miolo compartilhado por retenção (idade) e esquecimento (titular): dado
    um lote de interaction_ids, SCRUB das verifications (preserva a linha) →
    varre órfãs não-cascade → DELETE das interactions (cascade). No esquecimento
    (customer_hash dado) também apaga os invoke_jobs do titular — a conversa vive
    em invoke_jobs.request_payload e sem isto sobrevivia ao forget (achado de
    auditoria 35.14.2).

    TRANSACIONAL (achado de auditoria): scrub + deletes num con.transaction() —
    all-or-nothing por lote. Antes, em autocommit, um timeout no DELETE das
    interactions (o statement mais pesado) deixava a conversa VIVA com a
    auditoria do juiz já redigida (scrub irreversível de dado não-apagado)."""
    out = {"deleted": 0, "scrubbed_verifications": 0}
    if not ids:
        return out
    async with con.transaction():
        res = await con.execute(
            "UPDATE verifications SET "
            "question_redacted = $2, draft_redacted = $2, "
            "factuality_reason = NULL, completeness_reason = NULL, "
            "tone_reason = NULL, safety_reason = NULL, "
            "unsupported_claims = '[]' "
            "WHERE interaction_id = ANY($1)",
            ids, _SCRUB,
        )
        try:
            out["scrubbed_verifications"] = int(str(res).split()[-1])
        except Exception:
            pass
        await con.execute("DELETE FROM api_call_logs WHERE interaction_id = ANY($1)", ids)
        await con.execute("DELETE FROM verifier_jobs WHERE interaction_id = ANY($1)", ids)
        res = await con.execute("DELETE FROM interactions WHERE id = ANY($1)", ids)
        try:
            out["deleted"] = int(str(res).split()[-1])
        except Exception:
            out["deleted"] = len(ids)
    return out


def hash_customer_ref(customer_ref: Optional[str]) -> Optional[str]:
    """Pseudonimização (LGPD-2): guardamos SÓ o SHA-256 do identificador do
    cliente-final (CPF/id/email), nunca o valor cru. Determinístico →
    'esquecer o cliente X' = hash(X). Normaliza (trim+lower) p/ casar o mesmo
    cliente escrito de formas levemente diferentes."""
    import hashlib
    ref = (customer_ref or "").strip().lower()
    if not ref:
        return None
    return hashlib.sha256(ref.encode("utf-8")).hexdigest()


async def _unlink_uploaded_files(con, *, customer_hash: Optional[str] = None,
                                 older_than_days: Optional[float] = None) -> int:
    """Apaga o BINÁRIO em data/uploads + a linha de uploaded_files — por TITULAR
    (forget) OU por IDADE (retenção/órfãos). O banco não bastava: o arquivo cru
    com PII sobrevivia no disco (35.15.0, decisão do dono G).

    Best-effort no filesystem: falha de unlink loga mas NÃO impede a remoção da
    linha. Anti path-traversal: só apaga dentro de UPLOAD_DIR. Retorna nº de
    binários removidos do disco."""
    import os
    import asyncio
    from app.routes.workspace import UPLOAD_DIR
    if not customer_hash and older_than_days is None:
        return 0
    base = str(UPLOAD_DIR.resolve())

    def _unlink_batch(names: list) -> list:
        # I/O de filesystem SÍNCRONO fora do event loop (achado da auditoria #4):
        # data/uploads em volume lento/travado (NFS/overlay) congelaria o loop
        # inteiro — e o teto de 30s da carona do reaper não interrompe seção
        # síncrona. Roda em thread. Anti path-traversal: basename + prefixo.
        _done = []
        _removed = 0
        for name in names:
            try:
                p = (UPLOAD_DIR / os.path.basename(name)).resolve()
                if (str(p) == base or str(p).startswith(base + os.sep)) and p.exists():
                    p.unlink()
                    _removed += 1
                _done.append(name)
            except Exception as e:
                # NÃO remover a linha de um unlink que FALHOU — ela é o único
                # rastro do binário (só hash+nome, sem PII); mantê-la permite o
                # retry no próximo ciclo (deletá-la órfãva o arquivo com PII).
                logger.warning("event=retention_unlink_failed name=%s: %s", name, str(e)[:150])
        return [_removed, _done]

    total_removed = 0
    while True:
        if customer_hash:
            rows = await con.fetch(
                "SELECT disk_name FROM uploaded_files WHERE customer_hash = $1 "
                "ORDER BY created_at LIMIT $2", customer_hash, _PURGE_BATCH)
        else:
            rows = await con.fetch(
                "SELECT disk_name FROM uploaded_files "
                "WHERE created_at < now() - ($1 * interval '1 day') "
                "ORDER BY created_at LIMIT $2",
                float(older_than_days), _PURGE_BATCH)
        names = [r["disk_name"] for r in rows]
        if not names:
            break
        removed, done = await asyncio.to_thread(_unlink_batch, names)
        total_removed += removed
        if done:
            await con.execute("DELETE FROM uploaded_files WHERE disk_name = ANY($1)", done)
        # forget (por titular) ESGOTA em lotes — completude LGPD (achado da
        # auditoria #5: um LIMIT sem loop deixava >500 arquivos do titular no
        # disco). Retenção por IDADE é incremental (1 lote/passada, throttle 1x/h).
        # Guarda contra loop infinito: se NADA foi removível (todos os unlinks
        # falharam → done vazio → linhas ficam), para (o próximo ciclo tenta).
        if older_than_days is not None or len(names) < _PURGE_BATCH or not done:
            break
    return total_removed


async def forget_customer(customer_hash: str) -> dict:
    """Direito ao esquecimento (LGPD Art.18): apaga TODAS as conversas do
    titular (pelo customer_hash pseudônimo). Mesmo delete+scrub da retenção,
    por TITULAR em vez de idade e COMPLETO (varre em lotes até esgotar).
    O caller (rota root/admin) resolve o hash do customer_ref e audita."""
    total = {"deleted": 0, "scrubbed_verifications": 0, "batches": 0}
    if not customer_hash:
        return total
    async with _pool().acquire() as con:
        # invoke_jobs FORA do loop de interactions: um job 'queued'/'running' do
        # titular pode ainda NÃO ter criado interaction — o loop abaixo sairia
        # vazio e a conversa (request_payload) sobreviveria. Este DELETE por
        # customer_hash cobre TODOS os jobs do titular, com ou sem interaction.
        res = await con.execute(
            "DELETE FROM invoke_jobs WHERE customer_hash = $1", customer_hash)
        try:
            total["invoke_jobs_deleted"] = int(str(res).split()[-1])
        except Exception:
            total["invoke_jobs_deleted"] = 0
        while True:
            rows = await con.fetch(
                "SELECT id FROM interactions WHERE customer_hash = $1 LIMIT $2",
                customer_hash, _PURGE_BATCH,
            )
            ids = [r["id"] for r in rows]
            if not ids:
                break
            out = await _purge_ids(con, ids)
            total["deleted"] += out["deleted"]
            total["scrubbed_verifications"] += out["scrubbed_verifications"]
            total["batches"] += 1
            if len(ids) < _PURGE_BATCH:
                break
        # Turns de SESSÃO MISTA (35.15.0, decisão do dono D): uma sessão reusada
        # por mais de um cliente-final tem a interaction carimbada só com o 1º
        # titular (first-writer-wins) — os turns dos DEMAIS ficavam inalcançáveis
        # pelo forget. Com o pivô por-turno, apaga os turns DESTE titular que
        # sobrevivem em interactions de OUTRO (a interaction do outro FICA viva).
        # Scrub das verifications desses turns antes (por turn_id). Transacional.
        #
        # PII FORA de turns (achado do review adversarial do arco): o invoke do
        # titular na sessão mista também gravou tool_calls/api_call_logs/
        # binding_executions (input/output crus) — tabelas SEM turn_id, só
        # alcançáveis por interaction_id. Apagamos essas linhas das interactions
        # MISTAS inteiras: over-delete DELIBERADO (leva telemetria do outro
        # titular da MESMA sessão junto) — direção privacy-safe; a conversa
        # (interaction+turns) do outro fica intacta. evidences tem turn_id →
        # delete cirúrgico por turno, ANTES do delete dos turns (subselect).
        async with con.transaction():
            mixed = await con.fetch(
                "SELECT DISTINCT interaction_id FROM turns WHERE customer_hash = $1",
                customer_hash)
            mids = [r["interaction_id"] for r in mixed]
            vres = "UPDATE 0"
            if mids:
                # Over-delete DELIBERADO por interaction_id (achado da auditoria
                # #4): as tabelas por-interaction NÃO distinguem o titular numa
                # sessão mista → apaga a telemetria toda (privacy-safe; a
                # conversa/turns do OUTRO titular fica). Guardam PII crua:
                # api_call_logs (request/response_body), tool_calls (input/output),
                # binding_executions, verifier_jobs (draft/user_question no payload).
                for _tbl in ("api_call_logs", "tool_calls", "binding_executions",
                             "verifier_jobs"):
                    await con.execute(
                        "DELETE FROM " + _tbl + " WHERE interaction_id = ANY($1)", mids)
                # evidences NÃO tem interaction_id — só turn_id (achado da auditoria
                # #5, BLOCKER: por interaction_id dava UndefinedColumnError e o
                # ROLLBACK deixava turns/telemetria do titular VIVOS → PII sobrevivia
                # ao forget). Delete cirúrgico pelos turns DESTE titular (o snippet
                # é do turno; os turns de A na mesma interaction ficam).
                await con.execute(
                    "DELETE FROM evidences WHERE turn_id IN "
                    "(SELECT id FROM turns WHERE customer_hash = $1)", customer_hash)
                # verifications.turn_id é NULL no runtime (o verifier persiste
                # sem turn_id) → scrub por interaction_id, não por turn_id (que
                # afetaria 0 linhas). Over-scrub deliberado, mesma direção.
                vres = await con.execute(
                    "UPDATE verifications SET "
                    "question_redacted = $2, draft_redacted = $2, "
                    "factuality_reason = NULL, completeness_reason = NULL, "
                    "tone_reason = NULL, safety_reason = NULL, unsupported_claims = '[]' "
                    "WHERE interaction_id = ANY($1)", mids, _SCRUB)
                # title (mensagem CRUA do último turno, sem redação) + trace_data
                # (outputs crus dos steps) do master REUSADO refletem o titular
                # deste turno → scrub (achado da auditoria #4).
                await con.execute(
                    "UPDATE interactions SET title = $2, trace_data = '{}' "
                    "WHERE id = ANY($1)", mids, _SCRUB)
            tres = await con.execute(
                "DELETE FROM turns WHERE customer_hash = $1", customer_hash)
        try:
            total["scrubbed_verifications"] += int(str(vres).split()[-1])
            total["turns_deleted"] = int(str(tres).split()[-1])
        except Exception:
            total["turns_deleted"] = 0
        # Arquivos de upload do titular (35.15.0, decisão do dono G): apaga o
        # BINÁRIO em data/uploads (o banco não bastava — a PII crua vivia no disco).
        total["files_deleted"] = await _unlink_uploaded_files(con, customer_hash=customer_hash)
    logger.info("event=customer_forgotten deleted=%s scrubbed=%s turns=%s invoke_jobs=%s "
                "files=%s batches=%s",
                total["deleted"], total["scrubbed_verifications"],
                total.get("turns_deleted", 0), total.get("invoke_jobs_deleted", 0),
                total.get("files_deleted", 0), total["batches"])
    return total


async def purge_synthetic_once() -> dict:
    """UMA passada de purga das interações SINTÉTICAS do harness (43.0.0):
    origin='harness' mais velhas que harness_synthetic_retention_days. Mesmo
    miolo transacional da retenção LGPD (_purge_ids: scrub das verifications
    preserva a linha analítica de /quality; DELETE cascateia turns/telemetria).
    No-op quando desligado (0)."""
    days = _synthetic_retention_days()
    if days <= 0:
        return {"deleted": 0, "scrubbed_verifications": 0}
    async with _pool().acquire() as con:
        rows = await con.fetch(
            "SELECT id FROM interactions WHERE origin = 'harness' "
            "AND created_at < now() - ($1 * interval '1 day') "
            "ORDER BY created_at LIMIT $2",
            float(days), _PURGE_BATCH,
        )
        out = await _purge_ids(con, [r["id"] for r in rows])
        # experiment_case_results (48.0.0, PR4a): guarda o TEXTO do output do
        # agente (potencial PII derivada de gold cases) e NÃO tem
        # interaction_id/customer_hash — o CASCADE de eval_runs nunca dispara
        # (eval_runs é histórico de métrica, não purgado). Purga própria por
        # IDADE na mesma janela sintética (review [11]).
        eres = await con.execute(
            "DELETE FROM experiment_case_results WHERE id IN ("
            "  SELECT id FROM experiment_case_results "
            "  WHERE created_at < now() - ($1 * interval '1 day') "
            "  ORDER BY created_at LIMIT $2)",
            float(days), _PURGE_BATCH,
        )
        try:
            out["experiment_captures_deleted"] = int(str(eres).split()[-1])
        except Exception:
            out["experiment_captures_deleted"] = 0
        # optimization_runs TERMINAIS por idade (49.0.0, PR4b, review [12]):
        # guardam system_prompts de candidatos (CASCADE apaga a árvore) e o
        # result — crescem indefinidamente sem isto. Só terminal (queued/
        # running em voo nunca são apagados).
        ores = await con.execute(
            "DELETE FROM optimization_runs WHERE id IN ("
            "  SELECT id FROM optimization_runs "
            "  WHERE status IN ('completed','failed','timeout','interrupted') "
            "    AND updated_at < now() - ($1 * interval '1 day') "
            "  ORDER BY updated_at LIMIT $2)",
            float(days), _PURGE_BATCH)
        try:
            out["optimization_runs_deleted"] = int(str(ores).split()[-1])
        except Exception:
            out["optimization_runs_deleted"] = 0
    if out["deleted"] or out["scrubbed_verifications"] \
            or out.get("experiment_captures_deleted"):
        logger.info("event=retention_synthetic_purged deleted=%s "
                    "scrubbed_verifications=%s experiment_captures=%s days=%s",
                    out["deleted"], out["scrubbed_verifications"],
                    out.get("experiment_captures_deleted", 0), days)
    return out


async def maybe_purge() -> Optional[dict]:
    """Chamada a cada tick do reaper; só executa 1x por _PURGE_MIN_INTERVAL_S.
    Retorna os contadores quando rodou, None quando pulou (throttle/desligado).
    Cobre as DUAS retenções (cada uma checa o próprio toggle): por idade
    (LGPD, interactions_retention_days) e sintética do harness (43.0.0)."""
    global _last_purge_at
    if _retention_days() <= 0 and _synthetic_retention_days() <= 0:
        return None
    now = _now_monotonic()
    if _last_purge_at is not None and (now - _last_purge_at) < _PURGE_MIN_INTERVAL_S:
        return None
    _last_purge_at = now
    out = await purge_interactions_once()
    synth = await purge_synthetic_once()
    out["synthetic_deleted"] = synth.get("deleted", 0)
    out["synthetic_scrubbed"] = synth.get("scrubbed_verifications", 0)
    return out


def _reset_for_tests() -> None:
    global _last_purge_at
    _last_purge_at = None
