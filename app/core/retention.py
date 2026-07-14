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
     guardam conteúdo cru: invoke_jobs (request/result_payload), api_call_logs
     (request/response_body, sem FK), verifier_jobs (payload).
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


def _now_monotonic() -> float:
    # time.monotonic() é permitido (≠ time.time()/Date.now()); não retrocede.
    return time.monotonic()


async def purge_interactions_once() -> dict:
    """UMA passada de purga (1 lote). No-op quando desligado. Retorna contadores.
    Best-effort: exceção é logada, nunca propagada ao caller do loop."""
    days = _retention_days()
    if days <= 0:
        return {"deleted": 0, "scrubbed_verifications": 0}  # DESLIGADO
    async with _pool().acquire() as con:
        rows = await con.fetch(
            "SELECT id FROM interactions "
            "WHERE created_at < now() - ($1 * interval '1 day') "
            "ORDER BY created_at LIMIT $2",
            float(days), _PURGE_BATCH,
        )
        out = await _purge_ids(con, [r["id"] for r in rows])
    if out["deleted"] or out["scrubbed_verifications"]:
        logger.info("event=retention_purged deleted=%s scrubbed_verifications=%s days=%s",
                    out["deleted"], out["scrubbed_verifications"], days)
    return out


async def _purge_ids(con, ids: list) -> dict:
    """Miolo compartilhado por retenção (idade) e esquecimento (titular): dado
    um lote de interaction_ids, SCRUB das verifications (preserva a linha) →
    varre órfãs não-cascade → DELETE das interactions (cascade). Recebe a
    conexão (o caller controla pool/transação)."""
    out = {"deleted": 0, "scrubbed_verifications": 0}
    if not ids:
        return out
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


async def forget_customer(customer_hash: str) -> dict:
    """Direito ao esquecimento (LGPD Art.18): apaga TODAS as conversas do
    titular (pelo customer_hash pseudônimo). Mesmo delete+scrub da retenção,
    por TITULAR em vez de idade e COMPLETO (varre em lotes até esgotar).
    O caller (rota root/admin) resolve o hash do customer_ref e audita."""
    total = {"deleted": 0, "scrubbed_verifications": 0, "batches": 0}
    if not customer_hash:
        return total
    async with _pool().acquire() as con:
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
    logger.info("event=customer_forgotten deleted=%s scrubbed=%s batches=%s",
                total["deleted"], total["scrubbed_verifications"], total["batches"])
    return total


async def maybe_purge() -> Optional[dict]:
    """Chamada a cada tick do reaper; só executa 1x por _PURGE_MIN_INTERVAL_S.
    Retorna os contadores quando rodou, None quando pulou (throttle/desligado)."""
    global _last_purge_at
    if _retention_days() <= 0:
        return None
    now = _now_monotonic()
    if _last_purge_at is not None and (now - _last_purge_at) < _PURGE_MIN_INTERVAL_S:
        return None
    _last_purge_at = now
    return await purge_interactions_once()


def _reset_for_tests() -> None:
    global _last_purge_at
    _last_purge_at = None
