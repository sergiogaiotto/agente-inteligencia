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
from typing import Optional

from app.catalog.pipeline_defs import get_pipeline_def
from app.catalog.queries import db_row_to_entry_dict, get_disclosure
from app.core.database import _get_pool
from app.core.federation_identity import local_workspace

logger = logging.getLogger(__name__)

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
