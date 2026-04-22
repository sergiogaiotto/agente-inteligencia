"""Rotas de Usuários e Domínios — CRUD com controle de acesso."""
import uuid
import hashlib
import json
from fastapi import APIRouter, HTTPException, Request, Response
from app.models.schemas import UserCreate, UserUpdate, UserLogin, DomainCreate
from app.core.database import users_repo, domains_repo

router = APIRouter(prefix="/api/v1/users", tags=["users"])
domains_router = APIRouter(prefix="/api/v1/domains", tags=["domains"])


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


# ═══ Auth ═══
@router.post("/login")
async def login(data: UserLogin, response: Response):
    users = await users_repo.find_all(limit=1000)
    user = next((u for u in users if u["username"] == data.username), None)
    if not user:
        raise HTTPException(401, "Usuário não encontrado")
    if user["password_hash"] != hash_password(data.password):
        raise HTTPException(401, "Senha incorreta")
    if user.get("status") != "active":
        raise HTTPException(403, "Usuário inativo")
    # Set cookie
    response.set_cookie("user_id", user["id"], httponly=True, max_age=86400 * 7)
    return {"user": {k: v for k, v in dict(user).items() if k != "password_hash"}}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("user_id")
    return {"message": "Logout realizado"}


@router.get("/me")
async def get_current_user(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return {"user": None}
    user = await users_repo.find_by_id(user_id)
    if not user:
        return {"user": None}
    return {"user": {k: v for k, v in dict(user).items() if k != "password_hash"}}


@router.get("/check-setup")
async def check_setup():
    """Verifica se o sistema já tem pelo menos um usuário Root."""
    count = await users_repo.count()
    return {"has_users": count > 0}


# ═══ CRUD ═══
@router.get("")
async def list_users(role: str = None):
    users = await users_repo.find_all(limit=500)
    if role:
        users = [u for u in users if u.get("role") == role]
    return {"users": [{k: v for k, v in dict(u).items() if k != "password_hash"} for u in users]}


@router.get("/{user_id}")
async def get_user(user_id: str):
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
        # Validate caller permissions
        caller = await _get_caller(request)
        if not caller:
            raise HTTPException(401, "Faça login primeiro")
        if data.role == "root" and caller.get("role") != "root":
            raise HTTPException(403, "Apenas Root pode criar usuários Root")
        if caller.get("role") == "admin" and data.role == "root":
            raise HTTPException(403, "Admin não pode criar Root")

    # Check unique username
    existing = await users_repo.find_all(limit=1000)
    if any(u["username"] == data.username for u in existing):
        raise HTTPException(409, "Username já existe")

    uid = str(uuid.uuid4())
    await users_repo.create({
        "id": uid,
        "username": data.username,
        "password_hash": hash_password(data.password),
        "display_name": data.display_name or data.username,
        "email": data.email or "",
        "role": data.role,
        "domains": data.domains or "[]",
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

    # Root password: only self can change
    if target["role"] == "root" and data.password and caller["id"] != user_id:
        raise HTTPException(403, "Apenas o próprio Root pode alterar sua senha")

    # Admin cannot change Root users
    if caller["role"] == "admin" and target["role"] == "root":
        raise HTTPException(403, "Admin não pode alterar usuários Root")

    # Admin cannot promote to Root
    if caller["role"] == "admin" and data.role == "root":
        raise HTTPException(403, "Admin não pode promover a Root")

    upd = {}
    if data.display_name is not None:
        upd["display_name"] = data.display_name
    if data.email is not None:
        upd["email"] = data.email
    if data.role is not None and caller["role"] == "root":
        upd["role"] = data.role
    if data.domains is not None:
        upd["domains"] = data.domains
    if data.password:
        upd["password_hash"] = hash_password(data.password)

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

    if target["role"] == "root" and caller["role"] != "root":
        raise HTTPException(403, "Apenas Root pode remover Root")

    # Cannot delete self
    if caller["id"] == user_id:
        raise HTTPException(400, "Não é possível excluir a si mesmo")

    await users_repo.delete(user_id)
    return {"message": "Usuário removido"}


async def _get_caller(request: Request):
    uid = request.cookies.get("user_id")
    if not uid:
        return None
    return await users_repo.find_by_id(uid)


# ═══ Domains ═══
@domains_router.get("")
async def list_domains():
    return {"domains": await domains_repo.find_all(limit=200)}


@domains_router.post("", status_code=201)
async def create_domain(data: DomainCreate):
    existing = await domains_repo.find_all(limit=200)
    if any(d["name"].lower() == data.name.lower() for d in existing):
        raise HTTPException(409, "Domínio já existe")
    did = str(uuid.uuid4())
    await domains_repo.create({"id": did, "name": data.name, "description": data.description or ""})
    return {"id": did, "message": "Domínio criado"}


@domains_router.delete("/{domain_id}")
async def delete_domain(domain_id: str):
    if not await domains_repo.delete(domain_id):
        raise HTTPException(404)
    return {"message": "Domínio removido"}
