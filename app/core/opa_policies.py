"""Núcleo do editor de políticas OPA (63.0.0 — cockpit Fase B).

O DB (`governance_policy_version`) é a fonte VIVA das políticas; os `.rego` baked
na imagem são o SEED inicial. Cada save é uma versão nova (append-only) → histórico
+ rollback. A versão vigente (maior `version` por pacote) é empurrada ao OPA no
save e no boot (`repush_policies_on_boot`). Compartilhado entre as rotas de
governança (`app/routes/governance.py`) e o hook de lifespan (`app/main.py`).

Segurança: editar uma política é reescrever uma regra de acesso. Toda edição é
validada (o OPA COMPILA no push — Rego inválido não entra), auditada e versionada.
O `package` declarado no Rego é conferido contra o pacote alvo para impedir que
uma edição crie um namespace novo e deixe o antigo (bypass silencioso).
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Optional

import asyncpg

from app.core import opa_client
from app.core.config import get_settings
from app.core.database import governance_policy_repo

# Pacotes conhecidos e quais estão de fato ligados no PEP (evidence é dormente).
PACKAGES = ("interaction", "tool_invocation", "evidence")
WIRED = ("interaction", "tool_invocation")
POLICIES_DIR = Path(__file__).resolve().parent.parent.parent / "infra" / "opa" / "policies"


def pkg_from_id(pid: str) -> str:
    """OPA devolve id tipo "policies/interaction.rego" → extrai "interaction"."""
    base = (pid or "").rsplit("/", 1)[-1]
    return base[:-5] if base.endswith(".rego") else base


def policy_id_for(package: str) -> str:
    """Id do documento no OPA — o MESMO id do baked, para SUBSTITUIR (não duplicar)."""
    return f"policies/{package}.rego"


def read_baked(package: str) -> Optional[str]:
    """Lê o `.rego` baked do disco (o seed inicial). None se ausente."""
    try:
        return (POLICIES_DIR / f"{package}.rego").read_text(encoding="utf-8")
    except Exception:
        return None


async def list_versions(package: str) -> list[dict]:
    """Todas as versões salvas de um pacote, mais recente primeiro."""
    rows = await governance_policy_repo.find_all(package=package, limit=500)
    rows.sort(key=lambda r: r.get("version") or 0, reverse=True)
    return rows


async def current_version(package: str) -> Optional[dict]:
    """Versão vigente (maior version) do pacote no DB, ou None (sem override)."""
    rows = await list_versions(package)
    return rows[0] if rows else None


def validate_package_decl(package: str, rego: str) -> Optional[str]:
    """Confere que o Rego declara EXATAMENTE `package <package>` — nem prefixo
    (`interaction_v2`) nem sub-pacote pontuado (`interaction.v2`), que substituiriam
    o documento por um namespace diferente (`data.interaction.v2`) e deixariam o
    real (`data.interaction.allow`) indefinido = deny-all silencioso. Erro ou None.
    """
    # lookahead nega letra/dígito/_/. logo após o nome → casa só o pacote exato.
    if not re.search(rf"^\s*package\s+{re.escape(package)}(?![\w.])", rego, re.MULTILINE):
        return f"o Rego precisa declarar exatamente 'package {package}'"
    return None


async def validate_and_push(package: str, rego: str) -> dict:
    """Valida o pacote declarado + empurra ao OPA (que COMPILA). {ok, kind, error}
    com kind ∈ {"invalid","rejected","unreachable","ok"}."""
    perr = validate_package_decl(package, rego)
    if perr:
        return {"ok": False, "kind": "invalid", "error": perr}
    return await opa_client.push_policy(policy_id_for(package), rego)


async def save_version(package: str, rego: str, note: str, who: str) -> int:
    """Grava uma versão nova (append-only). Retorna o número da versão. Corrida
    de dois saves concorrentes calcularia o mesmo `version` → UNIQUE(package,version)
    barra o 2º; recomputa e tenta de novo (cada save vira uma versão distinta)."""
    for _ in range(6):
        rows = await list_versions(package)
        ver = (max((r.get("version") or 0) for r in rows) + 1) if rows else 1
        try:
            await governance_policy_repo.create({
                "id": str(uuid.uuid4()), "package": package, "version": ver,
                "rego": rego, "note": note or "", "created_by": who or "?",
            })
            return ver
        except asyncpg.exceptions.UniqueViolationError:
            continue  # outro save levou este número — recomputa
    raise RuntimeError(f"não foi possível alocar versão para {package} (corrida persistente)")


async def opa_current_raw(package: str) -> Optional[str]:
    """O Rego que o OPA serve AGORA para o pacote (snapshot, independe do DB)."""
    return await opa_client.get_policy(policy_id_for(package))


async def revert_opa(package: str, prev_raw: Optional[str]) -> None:
    """Re-empurra o estado anterior do OPA (ou o baked) — compensa uma persistência
    falha, para o OPA nunca ficar com uma mudança não registrada no DB/auditoria."""
    rego = prev_raw if prev_raw is not None else read_baked(package)
    if rego:
        await opa_client.push_policy(policy_id_for(package), rego)


# ── Evidence ACL (64.0.0): "no read up" via evidence.rego ─────────────────────
async def evidence_allows(clearance: Optional[str], confidentiality: Optional[str]) -> bool:
    """True se o usuário (clearance) pode ver a evidência (confidentiality), pela
    evidence.rego (rank[clearance] >= rank[confidentiality]). Avalia DIRETO no OPA —
    independe de opa_enabled (é o toggle `evidence_acl_enabled` quem liga o filtro no
    retriever). Defaults 'internal' (nível default das fontes). OPA fora do ar → segue
    o failsafe configurável (mesmo knob do gate de interação: opa_failsafe_open)."""
    # Normaliza (casing/espaço) — o rank[] da rego é lookup EXATO. Rótulo fora do
    # vocabulário (typo/legado) NÃO casa no rank → a rego mantém allow=false = oculta
    # a evidência (fail-closed conservador, coerente com um controle de sigilo).
    _cl = (str(clearance or "internal")).strip().lower() or "internal"
    _co = (str(confidentiality or "internal")).strip().lower() or "internal"
    d = await opa_client.simulate("evidence", "allow", {
        "user": {"clearance": _cl},
        "evidence": {"confidentiality": _co},
    })
    if d.get("source") == "error":
        return bool(get_settings().opa_failsafe_open)
    return bool(d.get("allow"))


async def repush_policies_on_boot() -> dict:
    """Re-empurra a vigente de cada pacote (DB) para o OPA. Best-effort: pacote
    sem override → mantém o baked; OPA fora do ar → registra erro e segue."""
    pushed, errors = [], []
    for package in PACKAGES:
        try:
            cur = await current_version(package)
        except Exception as e:
            errors.append(f"{package}: DB {type(e).__name__}")
            continue
        if not cur or not cur.get("rego"):
            continue  # sem override → OPA mantém o baked
        res = await opa_client.push_policy(policy_id_for(package), cur["rego"])
        if res.get("ok"):
            pushed.append(f"{package}@v{cur.get('version')}")
        else:
            errors.append(f"{package}: {res.get('error')}")
    return {"pushed": pushed, "errors": errors}
