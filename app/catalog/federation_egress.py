"""Federação A2A — lado CONSUMER/egress (PR8c).

Puxa o manifesto de um peer, registra as capabilities remotas como entries
federadas (read-only) no catálogo local, e INVOCA uma capability remota assinando
o envelope (HMAC com o segredo do peer) e fazendo POST no `/federation/invoke` dele.

Toda chamada outbound passa pela guarda SSRF (`app/core/ssrf.py`) + `follow_redirects=
False` + timeout + cap de tamanho de resposta (lê em streaming até o limite). Os
segredos do peer são decifrados no momento do uso (crypto.py), nunca em log.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

import httpx

from app.a2a.protocol import Envelope
from app.catalog.urn import VALID_KINDS, is_valid_urn, parse_urn
from app.core.crypto import decrypt_secret
from app.core.database import _get_pool, settings_store
from app.core.federation_identity import local_workspace
from app.core.ssrf import SSRFError, validate_public_url

logger = logging.getLogger(__name__)

_MANIFEST_PATH = "/.well-known/maestro-federation.json"
_INVOKE_PATH = "/api/v1/federation/invoke"
_TIMEOUT_S = 30.0
_MAX_RESPONSE_BYTES = 2_000_000  # 2 MB — cap de manifesto/resposta de peer
_MAX_CAPABILITIES = 2000         # cap de capabilities por manifesto (anti-bloat)
_MAX_ERROR_BODY_BYTES = 4096     # cap do corpo de ERRO lido só p/ extrair o detail
_MAX_ERROR_DETAIL_CHARS = 300    # cap do detail do peer repassado na mensagem


async def _dev_allow_http() -> bool:
    raw = (await settings_store.get("federation.dev_allow_http", "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


async def _peer_error_reason(resp) -> str:
    """Razão legível de um status não-200 do peer, incluindo o `detail` do corpo
    (cap de leitura + de tamanho). Sem isto, o 503 fail-closed do peer — que
    explica "MAESTRO_SECRET_KEY ausente" — virava um 502 mudo no consumer (A2A-2)."""
    body = b""
    try:
        async for chunk in resp.aiter_bytes():
            body += chunk
            if len(body) >= _MAX_ERROR_BODY_BYTES:
                break
    except Exception:  # corpo é best-effort; o status já basta p/ a mensagem
        pass
    detail = ""
    try:
        parsed = json.loads(body[:_MAX_ERROR_BODY_BYTES].decode("utf-8", "replace") or "{}")
        if isinstance(parsed, dict) and isinstance(parsed.get("detail"), str):
            detail = parsed["detail"].strip()
    except (ValueError, TypeError):
        pass
    base = f"peer respondeu HTTP {resp.status_code}"
    return f"{base}: {detail[:_MAX_ERROR_DETAIL_CHARS]}" if detail else base


async def _get_json(method: str, url: str, *, allow_http: bool, json_body: Optional[dict] = None) -> dict:
    """GET/POST com guarda SSRF + sem redirect + timeout + cap de tamanho (streaming).
    Levanta SSRFError/ValueError/httpx.HTTPError. Devolve o JSON parseado (dict)."""
    validate_public_url(url, allow_http=allow_http)
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=False) as client:
        async with client.stream(method, url, json=json_body) as resp:
            if resp.status_code != 200:
                raise ValueError(await _peer_error_reason(resp))
            total = 0
            chunks: list[bytes] = []
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    raise ValueError("resposta do peer excede o limite")
                chunks.append(chunk)
    try:
        data = json.loads(b"".join(chunks) or b"{}")
    except (ValueError, TypeError):
        raise ValueError("resposta do peer não é JSON válido")
    if not isinstance(data, dict):
        raise ValueError("resposta do peer não é um objeto")
    return data


async def pull_manifest(peer: dict) -> dict:
    """Puxa o manifesto well-known do peer. Valida o shape mínimo."""
    base = (peer.get("base_url") or "").rstrip("/")
    if not base:
        raise SSRFError("peer sem base_url — registre com base_url para sincronizar/invocar")
    data = await _get_json("GET", base + _MANIFEST_PATH, allow_http=await _dev_allow_http())
    if not isinstance(data.get("capabilities"), list):
        raise ValueError("manifesto inválido (sem 'capabilities')")
    return data


async def sync_remote_entries(peer: dict, owner_user_id: str) -> dict:
    """Puxa o manifesto e UPSERTA cada capability como entry federada (read-only).
    Pula URNs inválidos, kinds fora do CHECK, e capabilities do PRÓPRIO workspace."""
    manifest = await pull_manifest(peer)
    local_ws = await local_workspace()
    registered = 0
    skipped = 0
    pool = _get_pool()
    caps = manifest.get("capabilities", [])
    if len(caps) > _MAX_CAPABILITIES:
        logger.warning("sync: manifesto de %s tem %d capabilities — truncando em %d",
                       peer.get("workspace"), len(caps), _MAX_CAPABILITIES)
        caps = caps[:_MAX_CAPABILITIES]
    for cap in caps:
        urn = cap.get("urn")
        kind = cap.get("kind") or "pipeline"
        p = parse_urn(urn) if urn else None
        if not urn or not is_valid_urn(urn) or kind not in VALID_KINDS:
            skipped += 1
            continue
        if p and p["workspace"] == local_ws:  # não espelha a própria instância
            skipped += 1
            continue
        adapter_cfg = json.dumps({
            "remote": True, "peer_id": peer["id"], "peer_workspace": peer.get("workspace"),
            "fingerprint": cap.get("fingerprint"),
        })
        async with pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO catalog_entries
                  (id, urn, name, description, kind, version, status, visibility,
                   owner_user_id, adapter_type, adapter_config, federated, remote_urn, remote_peer_id)
                VALUES ($1, $2, $3, $4, $5, $6, 'published', 'company', $7, 'a2a', $8, TRUE, $2, $9)
                ON CONFLICT (urn) DO UPDATE SET
                  name = EXCLUDED.name, description = EXCLUDED.description,
                  version = EXCLUDED.version, adapter_config = EXCLUDED.adapter_config,
                  remote_peer_id = EXCLUDED.remote_peer_id, updated_at = now()
                -- só atualiza se a entry já pertence a ESTE peer — impede que um
                -- peer sequestre o URN federado de outro (integridade cross-peer).
                WHERE catalog_entries.remote_peer_id = EXCLUDED.remote_peer_id
                """,
                str(uuid.uuid4()), urn, cap.get("name") or urn, cap.get("description") or "",
                kind, cap.get("version") or "1.0.0", owner_user_id, adapter_cfg, peer["id"],
            )
        registered += 1
    return {"registered": registered, "skipped": skipped, "workspace": peer.get("workspace")}


async def invoke_remote(entry: dict, user_input: str, peer: dict) -> dict:
    """Invoca uma capability remota: assina o envelope com o segredo do peer e faz
    POST no /federation/invoke dele. Devolve o JSON de resposta do peer."""
    base = (peer.get("base_url") or "").rstrip("/")
    if not base:
        raise SSRFError("peer sem base_url")
    secret = decrypt_secret(peer.get("shared_secret") or "")
    if not secret:
        raise ValueError("peer sem segredo utilizável (rotacione/registre)")
    target_urn = entry.get("remote_urn") or entry.get("urn")
    env = Envelope(
        envelope_id=str(uuid.uuid4()),
        origin_workspace=await local_workspace(),
        target_skill_urn=target_urn,
        skill_ref=target_urn,
        context={"user_input": user_input},
        created_at=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),  # UTC p/ a janela do peer
    )
    sig = env.sign_hmac(secret)
    body = {"envelope": env.to_dict(), "signature": sig}
    return await _get_json("POST", base + _INVOKE_PATH, allow_http=await _dev_allow_http(), json_body=body)
