"""RBAC do CRUD de usuários (achado CRÍTICO da auditoria de segurança).

Antes, as rotas de usuários só validavam AUTENTICAÇÃO (middleware default-deny),
não o PAPEL — e as checagens inline só guardavam 'root'. Um usuário 'comum'
autenticado conseguia: criar admin (escalonamento), redefinir a senha de qualquer
não-root (takeover), deletar não-roots e listar a PII de todos. Estes testes
travam o fechamento por papel.
"""
import pytest
from fastapi import HTTPException

from app.routes import users as u
from app.models.schemas import UserCreate, UserUpdate, DomainCreate


class _Req:  # dummy — _get_caller é monkeypatchado
    pass


def _caller(monkeypatch, role="comum", uid="me"):
    async def _f(request):
        return {"id": uid, "role": role}
    monkeypatch.setattr(u, "_get_caller", _f)


# ── escalonamento de privilégio ──
@pytest.mark.asyncio
async def test_comum_nao_cria_admin(monkeypatch):
    _caller(monkeypatch, role="comum")
    async def _count(): return 1
    monkeypatch.setattr(u.users_repo, "count", _count)
    with pytest.raises(HTTPException) as e:
        await u.create_user(UserCreate(username="x", password="12345678", role="admin"), _Req())
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_nao_cria_root(monkeypatch):
    _caller(monkeypatch, role="admin")
    async def _count(): return 1
    monkeypatch.setattr(u.users_repo, "count", _count)
    with pytest.raises(HTTPException) as e:
        await u.create_user(UserCreate(username="x", password="12345678", role="root"), _Req())
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_sem_auto_promocao_no_update(monkeypatch):
    _caller(monkeypatch, role="comum", uid="me")
    async def _find(uid): return {"id": uid, "role": "comum"}
    monkeypatch.setattr(u.users_repo, "find_by_id", _find)
    with pytest.raises(HTTPException) as e:
        await u.update_user("me", UserUpdate(role="admin"), _Req())   # tenta se auto-promover
    assert e.value.status_code == 403


# ── takeover de conta ──
@pytest.mark.asyncio
async def test_comum_nao_reseta_senha_alheia(monkeypatch):
    _caller(monkeypatch, role="comum", uid="me")
    async def _find(uid): return {"id": uid, "role": "comum"}
    monkeypatch.setattr(u.users_repo, "find_by_id", _find)
    with pytest.raises(HTTPException) as e:
        await u.update_user("outro", UserUpdate(password="senhanova123"), _Req())
    assert e.value.status_code == 403


# ── vazamento de PII / remoção ──
@pytest.mark.asyncio
async def test_comum_nao_lista_usuarios(monkeypatch):
    _caller(monkeypatch, role="comum")
    with pytest.raises(HTTPException) as e:
        await u.list_users(_Req())
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_comum_nao_ve_outro_usuario(monkeypatch):
    _caller(monkeypatch, role="comum", uid="me")
    with pytest.raises(HTTPException) as e:
        await u.get_user("outro", _Req())
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_comum_nao_deleta(monkeypatch):
    _caller(monkeypatch, role="comum", uid="me")
    async def _find(uid): return {"id": uid, "role": "comum"}
    monkeypatch.setattr(u.users_repo, "find_by_id", _find)
    with pytest.raises(HTTPException) as e:
        await u.delete_user("outro", _Req())
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_comum_nao_cria_dominio(monkeypatch):
    _caller(monkeypatch, role="comum")
    with pytest.raises(HTTPException) as e:
        await u.create_domain(DomainCreate(name="x"), _Req())
    assert e.value.status_code == 403


# ── caminhos LEGÍTIMOS que precisam continuar funcionando ──
@pytest.mark.asyncio
async def test_comum_edita_a_si_mesmo(monkeypatch):
    _caller(monkeypatch, role="comum", uid="me")
    async def _find(uid): return {"id": uid, "role": "comum"}
    seen = {}
    async def _upd(uid, changes): seen.update(changes)
    monkeypatch.setattr(u.users_repo, "find_by_id", _find)
    monkeypatch.setattr(u.users_repo, "update", _upd)
    await u.update_user("me", UserUpdate(display_name="Novo Nome", password="minhasenha123"), _Req())
    assert seen.get("display_name") == "Novo Nome" and "password_hash" in seen
    assert "role" not in seen                     # 'comum' não muda o próprio papel


@pytest.mark.asyncio
async def test_ve_a_si_mesmo(monkeypatch):
    _caller(monkeypatch, role="comum", uid="me")
    async def _find(uid): return {"id": uid, "role": "comum", "username": "eu", "password_hash": "h"}
    monkeypatch.setattr(u.users_repo, "find_by_id", _find)
    out = await u.get_user("me", _Req())
    assert out["username"] == "eu" and "password_hash" not in out


@pytest.mark.asyncio
async def test_admin_cria_comum(monkeypatch):
    _caller(monkeypatch, role="admin")
    async def _count(): return 1
    async def _find_all(**k): return []
    seen = {}
    async def _create(row): seen.update(row)
    monkeypatch.setattr(u.users_repo, "count", _count)
    monkeypatch.setattr(u.users_repo, "find_all", _find_all)
    monkeypatch.setattr(u.users_repo, "create", _create)
    r = await u.create_user(UserCreate(username="novo", password="12345678", role="comum"), _Req())
    assert r["role"] == "comum" and seen["username"] == "novo"


@pytest.mark.asyncio
async def test_primeiro_usuario_autoroot_no_setup(monkeypatch):
    """Setup (count==0) segue criando root SEM exigir caller — 1º acesso intacto."""
    async def _count(): return 0
    async def _find_all(**k): return []
    seen = {}
    async def _create(row): seen.update(row)
    monkeypatch.setattr(u.users_repo, "count", _count)
    monkeypatch.setattr(u.users_repo, "find_all", _find_all)
    monkeypatch.setattr(u.users_repo, "create", _create)
    r = await u.create_user(UserCreate(username="root", password="12345678"), _Req())
    assert r["role"] == "root"
