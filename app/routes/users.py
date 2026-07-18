"""Rotas de Usuários e Domínios — CRUD com controle de acesso.

Auth hardening (Onda 1):
- bcrypt via passlib (`app.core.auth`). SHA256 legado validado e migrado
  no próximo login bem-sucedido (transparente para o usuário).
- Cookies: HttpOnly + SameSite + Secure (em prod via setting).
- CSRF token gerado em /me e /login; validação opt-in via setting.
"""
import json
import uuid
import logging
from fastapi import APIRouter, HTTPException, Request, Response
from app.models.schemas import UserCreate, UserUpdate, UserLogin, DomainCreate, DomainUpdate
from app.core.database import (
    users_repo, domains_repo, agents_repo, skills_repo, pipelines_repo,
    journeys_repo, catalog_entries_repo, car_repo,
)
from app.core.auth import (
    hash_password, verify_password, needs_rehash,
    make_csrf_token, cookie_kwargs, sign_session, read_session_uid,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["users"])
domains_router = APIRouter(prefix="/api/v1/domains", tags=["domains"])


async def _audit_session_event(action: str, actor: str | None, detail: dict) -> None:
    """Auditoria de login/logout (35.11.0) — best-effort: falha aqui NUNCA
    derruba o auth. O IP entra sozinho pela AuditRepository (contextvar)."""
    try:
        import json as _json
        from app.core.database import audit_repo
        await audit_repo.create({
            "entity_type": "session", "entity_id": actor or "anonymous",
            "action": action, "actor": actor,
            "details": _json.dumps(detail, ensure_ascii=False),
        })
    except Exception as e:
        logger.warning("audit de sessão falhou (%s): %s", action, str(e)[:150])


# ═══ Auth ═══
@router.post("/login")
async def login(data: UserLogin, response: Response):
    users = await users_repo.find_all(limit=1000)
    user = next((u for u in users if u["username"] == data.username), None)
    # Mensagem genérica e tempo aproximadamente constante para evitar
    # enumeração de usuários por timing/erro.
    if not user or not verify_password(data.password, user["password_hash"]):
        # Custo do verify_password de qualquer forma para mitigar timing
        if not user:
            verify_password(data.password, "$2b$12$" + "x" * 53)
        # Auditoria (35.11.0): NÃO grava o username tentado nem distingue
        # usuário-inexistente de senha-errada (anti-enumeração — o log de
        # segurança não pode virar oráculo). IP entra via AuditRepository.
        await _audit_session_event("login_failed", None, {"reason": "invalid_credentials"})
        raise HTTPException(401, "Credenciais inválidas")
    if user.get("status") != "active":
        await _audit_session_event("login_failed", user["id"], {"reason": "inactive_user"})
        raise HTTPException(403, "Usuário inativo")

    # Migração transparente: se hash legado SHA256, regrava em bcrypt.
    if needs_rehash(user["password_hash"]):
        try:
            await users_repo.update(user["id"], {"password_hash": hash_password(data.password)})
            logger.info(f"login: hash migrado para bcrypt user={user['id']}")
        except Exception as e:
            logger.warning(f"login: falha ao migrar hash user={user['id']}: {e}")

    # Cookies seguros + CSRF token (front pode ler csrf_token e mandar em header)
    # Cookie de sessão ASSINADO (não o UUID cru) — impede forja/impersonação.
    ck = cookie_kwargs()
    response.set_cookie("user_id", sign_session(user["id"]), **ck)
    csrf = make_csrf_token()
    # csrf_token NÃO é HttpOnly — front precisa ler para mandar no header
    response.set_cookie("csrf_token", csrf, **{**ck, "httponly": False})

    await _audit_session_event("login_success", user["id"], {"username": user["username"]})
    return {
        "user": {k: v for k, v in dict(user).items() if k != "password_hash"},
        "csrf_token": csrf,
    }


@router.post("/logout")
async def logout(request: Request, response: Response):
    # Auditoria (35.11.0): registra QUEM saiu (cookie assinado; None se já
    # expirado — ainda auditamos o evento). IP entra via AuditRepository.
    uid = None
    try:
        uid = read_session_uid(request)
    except Exception:
        pass
    await _audit_session_event("logout", uid, {})
    response.delete_cookie("user_id")
    response.delete_cookie("csrf_token")
    return {"message": "Logout realizado"}


@router.get("/me")
async def get_current_user(request: Request, response: Response):
    user_id = read_session_uid(request)
    if not user_id:
        return {"user": None}
    user = await users_repo.find_by_id(user_id)
    if not user:
        return {"user": None}
    # Renova CSRF token se ausente — útil quando o cookie expirou mas o user_id segue
    if not request.cookies.get("csrf_token"):
        csrf = make_csrf_token()
        response.set_cookie("csrf_token", csrf, **{**cookie_kwargs(), "httponly": False})
    return {"user": {k: v for k, v in dict(user).items() if k != "password_hash"}}


@router.get("/check-setup")
async def check_setup():
    """Verifica se o sistema já tem pelo menos um usuário Root."""
    count = await users_repo.count()
    return {"has_users": count > 0}


# ═══ CRUD ═══
@router.get("")
async def list_users(request: Request, role: str = None):
    # RBAC: a lista expõe PII (email/papel/domínios) de TODOS os usuários — só
    # root/admin. (A UI de Usuários já é gated; a observabilidade degrada p/ [].)
    caller = await _get_caller(request)
    if not caller:
        raise HTTPException(401, "Faça login primeiro")
    if not _is_privileged(caller):
        raise HTTPException(403, "Apenas Root/Admin podem listar usuários")
    users = await users_repo.find_all(limit=500)
    if role:
        users = [u for u in users if u.get("role") == role]
    return {"users": [{k: v for k, v in dict(u).items() if k != "password_hash"} for u in users]}


@router.get("/{user_id}")
async def get_user(user_id: str, request: Request):
    # RBAC: ver OUTRO usuário exige root/admin; o próprio pode ver a si mesmo.
    caller = await _get_caller(request)
    if not caller:
        raise HTTPException(401, "Faça login primeiro")
    if caller["id"] != user_id and not _is_privileged(caller):
        raise HTTPException(403, "Apenas Root/Admin podem ver outros usuários")
    u = await users_repo.find_by_id(user_id)
    if not u:
        raise HTTPException(404)
    return {k: v for k, v in dict(u).items() if k != "password_hash"}


@router.post("", status_code=201)
async def create_user(data: UserCreate, request: Request):
    # Check if first user (auto-root)
    count = await users_repo.count()
    if count == 0:
        data.role = "root"
    else:
        # RBAC: só root/admin criam usuários. Antes, um 'comum' criava um 'admin'
        # (nada barrava — só o papel 'root' era guardado) = escalonamento direto.
        caller = await _get_caller(request)
        if not caller:
            raise HTTPException(401, "Faça login primeiro")
        if not _is_privileged(caller):
            raise HTTPException(403, "Apenas Root/Admin podem criar usuários")
        # Não criar papel ACIMA do próprio: admin não cria root.
        if (data.role or "").lower() == "root" and (caller.get("role") or "").lower() != "root":
            raise HTTPException(403, "Apenas Root pode criar usuários Root")

    # Check unique username
    existing = await users_repo.find_all(limit=1000)
    if any(u["username"] == data.username for u in existing):
        raise HTTPException(409, "Username já existe")

    cl = (data.clearance or "internal").strip().lower()
    if cl not in _CLEARANCE_LEVELS:
        raise HTTPException(422, "clearance inválido (public|internal|confidential|restricted)")

    uid = str(uuid.uuid4())
    await users_repo.create({
        "id": uid,
        "username": data.username,
        "password_hash": hash_password(data.password),
        "display_name": data.display_name or data.username,
        "email": data.email or "",
        "role": data.role,
        "domains": data.domains or "[]",
        "clearance": cl,
    })
    return {"id": uid, "message": "Usuário criado", "role": data.role}


@router.put("/{user_id}")
async def update_user(user_id: str, data: UserUpdate, request: Request):
    target = await users_repo.find_by_id(user_id)
    if not target:
        raise HTTPException(404)

    caller = await _get_caller(request)
    if not caller:
        raise HTTPException(401)
    is_self = caller["id"] == user_id
    caller_role = (caller.get("role") or "").lower()

    # RBAC: editar OUTRO usuário exige root/admin. Antes, um 'comum' redefinia a
    # senha de QUALQUER não-root (só a senha de root era guardada) = takeover de
    # conta. O próprio usuário segue podendo editar a si mesmo (nome/email/senha).
    if not is_self and not _is_privileged(caller):
        raise HTTPException(403, "Apenas Root/Admin podem editar outros usuários")

    # Senha de Root: só o próprio Root altera.
    if target["role"] == "root" and data.password and not is_self:
        raise HTTPException(403, "Apenas o próprio Root pode alterar sua senha")
    # Admin não altera usuários Root.
    if caller_role == "admin" and target["role"] == "root":
        raise HTTPException(403, "Admin não pode alterar usuários Root")

    # Papel: só Root altera papéis, e NINGUÉM muda o próprio papel (nem auto-promoção
    # nem auto-rebaixamento do último root). Um pedido de role só é aplicado por root.
    role_change = data.role is not None and (data.role or "").lower() != (target.get("role") or "").lower()
    if role_change:
        if caller_role != "root":
            raise HTTPException(403, "Apenas Root altera papéis")
        if is_self:
            raise HTTPException(403, "Root não altera o próprio papel")

    upd = {}
    if data.display_name is not None:
        upd["display_name"] = data.display_name
    if data.email is not None:
        upd["email"] = data.email
    if role_change and caller_role == "root":
        upd["role"] = data.role
    if data.domains is not None:
        upd["domains"] = data.domains
    if data.password:
        upd["password_hash"] = hash_password(data.password)
    # Clearance (Evidence ACL, 64.0.0): controle de acesso a DADOS. Só privilegiado
    # define — impede auto-escalonamento (um 'comum' editando a si mesmo poderia se
    # dar clearance alto e ler tudo). Validado contra o vocabulário da evidence.rego.
    if data.clearance is not None:
        if not _is_privileged(caller):
            raise HTTPException(403, "Apenas Root/Admin/Governança definem clearance")
        cl = (data.clearance or "").strip().lower()
        if cl not in _CLEARANCE_LEVELS:
            raise HTTPException(422, "clearance inválido (public|internal|confidential|restricted)")
        upd["clearance"] = cl

    if upd:
        await users_repo.update(user_id, upd)
    return {"message": "Usuário atualizado"}


@router.delete("/{user_id}")
async def delete_user(user_id: str, request: Request):
    target = await users_repo.find_by_id(user_id)
    if not target:
        raise HTTPException(404)

    caller = await _get_caller(request)
    if not caller:
        raise HTTPException(401)

    # RBAC: só root/admin removem usuários (antes um 'comum' deletava não-roots).
    if not _is_privileged(caller):
        raise HTTPException(403, "Apenas Root/Admin podem remover usuários")
    if target["role"] == "root" and (caller.get("role") or "").lower() != "root":
        raise HTTPException(403, "Apenas Root pode remover Root")

    # Cannot delete self
    if caller["id"] == user_id:
        raise HTTPException(400, "Não é possível excluir a si mesmo")

    await users_repo.delete(user_id)
    return {"message": "Usuário removido"}


async def _get_caller(request: Request):
    uid = read_session_uid(request)
    if not uid:
        return None
    return await users_repo.find_by_id(uid)


_PRIVILEGED_ROLES = ("root", "admin", "governanca")
# Evidence ACL (64.0.0): níveis de clearance/confidencialidade — mesmo vocabulário
# da classificação das fontes (evidence.html: public|internal|confidential|restricted).
_CLEARANCE_LEVELS = ("public", "internal", "confidential", "restricted")


def _is_privileged(caller) -> bool:
    """True se o caller tem papel root/admin (case-insensitive). RBAC: só estes
    papéis administram usuários — fecha o escalonamento de privilégio e o takeover
    de conta por usuário 'comum' (achado crítico da auditoria de segurança)."""
    return bool(caller) and (caller.get("role") or "").lower() in _PRIVILEGED_ROLES


# ═══ Domains ═══
# Um domínio é carimbado por NOME (texto livre) em várias entidades. O "Raio-X"
# cruza essas referências; a caça a órfãos acha strings usadas mas não registradas.
_DOMAIN_INV_REPOS = [
    ("agentes", agents_repo),
    ("skills", skills_repo),
    ("pipelines", pipelines_repo),
    ("jornadas", journeys_repo),
    ("catalogo", catalog_entries_repo),
    ("car", car_repo),
]


def _sample_label(r: dict) -> str:
    return r.get("name") or r.get("skill_urn") or r.get("title") or r.get("id") or "—"


async def _domain_members(name: str) -> list[dict]:
    """Usuários cujo `domains` (lista JSON) inclui este domínio."""
    out = []
    for u in await users_repo.find_all(limit=500):
        try:
            doms = json.loads(u.get("domains") or "[]")
        except Exception:
            doms = []
        if isinstance(doms, list) and name in doms:
            out.append({"id": u.get("id"), "name": u.get("display_name") or u.get("username") or u.get("id")})
    return out


@domains_router.get("")
async def list_domains():
    return {"domains": await domains_repo.find_all(limit=200)}


@domains_router.get("/orphans")
async def domain_orphans():
    """Domínios-fantasma: strings de domínio citadas por ativos mas NÃO registradas."""
    registered = {(d.get("name") or "").strip().lower() for d in await domains_repo.find_all(limit=500)}
    usage: dict[str, dict] = {}
    for key, repo in _DOMAIN_INV_REPOS:
        for r in await repo.find_all(limit=1000):
            dv = (r.get("domain") or "").strip()
            if not dv or dv.lower() in registered:
                continue
            e = usage.setdefault(dv.lower(), {"name": dv, "used_by": {}, "total": 0})
            e["used_by"][key] = e["used_by"].get(key, 0) + 1
            e["total"] += 1
    return {"orphans": sorted(usage.values(), key=lambda x: -x["total"])}


@domains_router.get("/{domain_id}/inventory")
async def domain_inventory(domain_id: str):
    """Raio-X: contagem + amostra de tudo que cita o domínio, por categoria."""
    dom = await domains_repo.find_by_id(domain_id)
    if not dom:
        raise HTTPException(404)
    name = dom["name"]
    inv: dict[str, dict] = {}
    total = 0
    for key, repo in _DOMAIN_INV_REPOS:
        cnt = await repo.count(domain=name)
        rows = await repo.find_all(limit=6, domain=name)
        inv[key] = {"count": cnt, "sample": [{"id": r.get("id"), "name": _sample_label(r)} for r in rows]}
        total += cnt
    members = await _domain_members(name)
    inv["membros"] = {"count": len(members), "sample": members[:6]}
    total += len(members)
    return {"domain": dom, "inventory": inv, "total": total}


@domains_router.post("", status_code=201)
async def create_domain(data: DomainCreate, request: Request):
    # RBAC: domínios são estrutura de governança — só root/admin criam/editam/removem.
    if not _is_privileged(await _get_caller(request)):
        raise HTTPException(403, "Apenas Root/Admin podem criar domínios")
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(422, "Nome do domínio é obrigatório")
    existing = await domains_repo.find_all(limit=200)
    if any((d.get("name") or "").lower() == name.lower() for d in existing):
        raise HTTPException(409, "Domínio já existe")
    did = str(uuid.uuid4())
    await domains_repo.create({
        "id": did, "name": name, "description": data.description or "",
        "owner_user_id": data.owner_user_id or None, "color": data.color or None,
        "icon": data.icon or None, "status": data.status or "active",
    })
    return {"id": did, "message": "Domínio criado"}


@domains_router.put("/{domain_id}")
async def update_domain(domain_id: str, data: DomainUpdate, request: Request):
    if not _is_privileged(await _get_caller(request)):
        raise HTTPException(403, "Apenas Root/Admin podem editar domínios")
    dom = await domains_repo.find_by_id(domain_id)
    if not dom:
        raise HTTPException(404)
    patch: dict = {}
    if data.name is not None:
        nm = data.name.strip()
        if not nm:
            raise HTTPException(422, "Nome do domínio não pode ser vazio")
        others = await domains_repo.find_all(limit=500)
        if any(o.get("id") != domain_id and (o.get("name") or "").lower() == nm.lower() for o in others):
            raise HTTPException(409, "Já existe um domínio com esse nome")
        patch["name"] = nm
    for field in ("description", "owner_user_id", "color", "icon", "status"):
        val = getattr(data, field)
        if val is not None:
            patch[field] = val
    if patch:
        await domains_repo.update(domain_id, patch)
    return {"message": "Domínio atualizado"}


@domains_router.delete("/{domain_id}")
async def delete_domain(domain_id: str, request: Request):
    if not _is_privileged(await _get_caller(request)):
        raise HTTPException(403, "Apenas Root/Admin podem remover domínios")
    if not await domains_repo.delete(domain_id):
        raise HTTPException(404)
    return {"message": "Domínio removido"}
