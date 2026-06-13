"""Federação A2A — lado PROVIDER (PR8b).

PR8b1 (este arquivo): autorização de exposição + manifesto de descoberta +
resolver SELADO (snapshot-only) para execução federada. O endpoint de invoke
assinado que usa `resolve_federated_exec` é PR8b2.

Regras de confiança (NÃO reusar `can_user_see`, que admite deprecated+department):
- Só `status='published'` + `visibility='company'` + kind no allowlist são
  EXPONÍVEIS a peers (`is_federation_exposable`). Re-checado no invoke (PR8b2)
  para fechar TOCTOU.
- Execução federada é SELADA ao snapshot congelado (`catalog_pipeline_defs`):
  `resolve_federated_exec` RECUSA o fallback de mesh vivo de `resolve_pipeline_exec`
  e exige root∈membros — o caller NUNCA deve chamar execute_pipeline com
  allowed_agent_ids=None num caminho federado (senão a BFS anda no mesh global).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Optional

from app.a2a.protocol import Envelope
from app.catalog import federation_peers as _peers
from app.catalog.pipeline_defs import get_pipeline_def
from app.catalog.queries import db_row_to_entry_dict, get_disclosure
from app.core.database import _get_pool
from app.core.federation_identity import local_workspace

logger = logging.getLogger(__name__)

# Anti-replay: janela de aceitação do created_at do envelope + TTL dos nonces.
REPLAY_WINDOW_SECONDS = 300       # ±5min de skew aceitável
_NONCE_TTL_SECONDS = 1200         # >> janela; só bound de crescimento da tabela

# Allowlist de kinds expostos via federação. Começa só com 'pipeline' (a unidade
# SELADA executável); agentes/recipes podem entrar numa onda futura.
_FEDERATION_KINDS = frozenset({"pipeline"})

_MANIFEST_SCHEMA_VERSION = "1.0"

# Caminho do invoke assinado (PR8b2). Anunciado no manifest como contrato de
# descoberta — o consumer (PR8c) lê daqui para onde fazer POST.
FEDERATION_INVOKE_PATH = "/api/v1/federation/invoke"

# Subconjunto do capability disclosure exposto no manifest (resumo "etiqueta
# nutricional" — o suficiente para o consumer decidir confiar, sem PII de config).
_DISCLOSURE_SUMMARY_FIELDS = (
    "calls_external_apis",
    "accesses_internet",
    "processes_pii",
    "processes_financial",
    "processes_health",
    "stores_input",
    "trains_on_input",
    "output_is_deterministic",
    "data_residency",
    "verification_method",
)


def is_federation_exposable(entry: dict) -> bool:
    """True se a entry pode ser exposta/invocada por um peer (função pura).

    Gate DEDICADO (não `can_user_see`): published + company + kind no allowlist."""
    return (
        entry.get("status") == "published"
        and entry.get("visibility") == "company"
        and entry.get("kind") in _FEDERATION_KINDS
    )


def pipeline_fingerprint(pdef: Optional[dict]) -> Optional[str]:
    """Fingerprint determinístico do snapshot do pipeline (raiz+nós+arestas).

    Detecta drift/adulteração da capability publicada. Independente de ordem
    (nós/arestas ordenados por id). None se não há snapshot/raiz. Prefixo de
    algoritmo p/ agilidade futura."""
    if not pdef or not pdef.get("root_agent_id"):
        return None
    # Ordena por (id, json canônico) — o tiebreak torna o fingerprint estável
    # mesmo se ids forem duplicados/ausentes (invariante de unicidade não assumida).
    def _k(o):
        return (str(o.get("id")), json.dumps(o, sort_keys=True, separators=(",", ":")))

    nodes = sorted((pdef.get("nodes") or []), key=_k)
    edges = sorted((pdef.get("edges") or []), key=_k)
    canon = json.dumps(
        {"root": pdef["root_agent_id"], "nodes": nodes, "edges": edges},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _disclosure_summary(disc: Optional[dict]) -> Optional[dict]:
    """Resumo compacto do disclosure p/ o manifest. None se ausente."""
    if not disc:
        return None
    return {k: disc.get(k) for k in _DISCLOSURE_SUMMARY_FIELDS}


async def list_exposable_entries() -> list[dict]:
    """Entries published+company com kind no allowlist (SQL nativo)."""
    pool = _get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            "SELECT * FROM catalog_entries "
            "WHERE status='published' AND visibility='company' AND kind = ANY($1::text[]) "
            "ORDER BY name",
            list(_FEDERATION_KINDS),
        )
    return [db_row_to_entry_dict(r) for r in rows]


async def build_manifest() -> dict:
    """Monta o manifesto de descoberta desta instância.

    {schema_version, workspace, capabilities:[{urn, name, kind, version, domain,
    description, disclosure, fingerprint?, invoke_path}]}. Só capabilities
    EXPONÍVEIS (re-aplica `is_federation_exposable` por garantia)."""
    ws = await local_workspace()
    entries = await list_exposable_entries()
    capabilities: list[dict] = []
    for e in entries:
        if not is_federation_exposable(e):
            continue
        cap = {
            "urn": e.get("urn"),
            "name": e.get("name"),
            "kind": e.get("kind"),
            "version": e.get("version"),
            "domain": e.get("domain"),
            "description": e.get("description") or "",
            "invoke_path": FEDERATION_INVOKE_PATH,
        }
        try:
            cap["disclosure"] = _disclosure_summary(await get_disclosure(e["id"]))
        except Exception:
            logger.warning("manifest: disclosure de %s falhou", e.get("id"), exc_info=True)
            cap["disclosure"] = None
        if e.get("kind") == "pipeline":
            try:
                cap["fingerprint"] = pipeline_fingerprint(await get_pipeline_def(e["id"]))
            except Exception:
                logger.warning("manifest: fingerprint de %s falhou", e.get("id"), exc_info=True)
                cap["fingerprint"] = None
        capabilities.append(cap)
    return {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "workspace": ws,
        "capabilities": capabilities,
    }


async def resolve_federated_exec(entry: dict) -> tuple[Optional[str], set]:
    """(root, allowed_agent_ids) p/ execução federada — SELADO, SEM fallback vivo.

    Exige snapshot real em catalog_pipeline_defs e verifica root∈membros. Devolve
    (None, set()) se não há snapshot/raiz, membros vazios, ou root∉membros — o
    caller (PR8b2) DEVE rejeitar (nunca chamar execute_pipeline com allowed=None).

    Difere de `pipeline_defs.resolve_pipeline_exec`, que CAI para o mesh vivo —
    inseguro num caminho federado, onde a fronteira de execução é obrigatória."""
    pdef = await get_pipeline_def(entry["id"])
    if not pdef or not pdef.get("root_agent_id"):
        return None, set()
    members = {n.get("id") for n in (pdef.get("nodes") or []) if n.get("id")}
    root = pdef["root_agent_id"]
    if not members or root not in members:
        return None, set()
    return root, members


# ── Ingress (PR8b3): autenticação de envelope + anti-replay + lookup por URN ──


async def get_entry_by_urn(urn: str) -> Optional[dict]:
    """Entry local pelo URN — a capability que o peer quer invocar."""
    if not urn:
        return None
    pool = _get_pool()
    async with pool.acquire() as con:
        r = await con.fetchrow("SELECT * FROM catalog_entries WHERE urn=$1", urn)
    return db_row_to_entry_dict(r) if r else None


async def verify_inbound_envelope(env: Envelope, signature: str) -> Optional[dict]:
    """Autentica o envelope contra o peer ATIVO de `env.origin_workspace`,
    verificando o HMAC contra os segredos válidos (atual + anterior na janela de
    rotação). Devolve a row do peer se válido, senão None — o caller responde 403
    indistinto (não revela se o peer existe vs assinatura inválida)."""
    if not signature:
        return None
    peer = await _peers.get_active_peer_by_workspace(env.origin_workspace)
    if not peer:
        return None
    for secret in _peers.peer_secrets(peer):
        if env.verify_hmac(secret, signature):
            return peer
    return None


def within_replay_window(created_at: str, now_utc: datetime, window_s: int = REPLAY_WINDOW_SECONDS) -> bool:
    """True se `created_at` (UTC naive 'YYYY-MM-DDTHH:MM:SS') está dentro de
    ±window_s de agora. Envelopes de federação DEVEM usar UTC (egress/PR8c)."""
    if not created_at:
        return False
    try:
        ts = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return False
    return abs((now_utc - ts).total_seconds()) <= window_s


async def check_and_record_nonce(nonce: str, peer_workspace: str) -> bool:
    """True se o nonce é NOVO (registrado agora); False se já visto (replay).
    Limpa nonces expirados no mesmo passo (bound de crescimento). Só deve ser
    chamado APÓS autenticar (senão um atacante enche a tabela sem assinatura)."""
    if not nonce:
        return False
    pool = _get_pool()
    async with pool.acquire() as con:
        await con.execute(
            f"DELETE FROM federation_nonces WHERE seen_at < now() - interval '{_NONCE_TTL_SECONDS} seconds'"
        )
        res = await con.execute(
            "INSERT INTO federation_nonces (nonce, peer_workspace) VALUES ($1, $2) "
            "ON CONFLICT (nonce) DO NOTHING",
            nonce, peer_workspace,
        )
    try:
        return int(res.split()[-1]) == 1  # "INSERT 0 1" inseriu; "INSERT 0 0" = replay
    except (ValueError, IndexError):
        return False
