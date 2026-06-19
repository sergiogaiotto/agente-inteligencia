"""Semeia (idempotente) o usuário usado pelos testes E2E de interface.

Rode DENTRO do container (tem acesso ao Postgres e às deps do app):

    docker exec agente_app python scripts/seed_e2e_user.py

Cria — ou atualiza a senha de — um usuário root com as credenciais que o suite
E2E espera. Necessário só quando o banco JÁ tem usuários (nesse caso o suite
não consegue criar via /api/v1/users sem um root autenticado). Em banco vazio,
o próprio suite cria o root pelo fluxo de setup e este script é dispensável.

Credenciais (mesmos defaults do tests/e2e/conftest.py; sobrescreva por env):
    E2E_USERNAME       (e2e_admin)
    E2E_PASSWORD       (e2e-pass-1234)
    E2E_DISPLAY_NAME   (E2E Admin)
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

# Permite rodar como `python scripts/seed_e2e_user.py` (sys.path[0] vira a pasta
# scripts/, não a raiz do projeto). Insere a raiz (pai de scripts/) no path para
# `import app...` funcionar independentemente do diretório de invocação.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

USERNAME = os.environ.get("E2E_USERNAME", "e2e_admin")
PASSWORD = os.environ.get("E2E_PASSWORD", "e2e-pass-1234")
DISPLAY = os.environ.get("E2E_DISPLAY_NAME", "E2E Admin")


async def main() -> int:
    from app.core.auth import hash_password
    from app.core.database import close_db, init_db, users_repo

    # Standalone: o pool só é aberto no lifespan do app; aqui inicializamos por
    # conta própria (idempotente — também aplica schema, sem efeito se já existe).
    await init_db()
    try:
        existing = await users_repo.find_all(limit=1000)
        match = next((u for u in existing if u["username"] == USERNAME), None)

        if match:
            await users_repo.update(match["id"], {"password_hash": hash_password(PASSWORD)})
            print(f"[seed] senha do usuário E2E '{USERNAME}' atualizada (id={match['id']}).")
            return 0

        uid = str(uuid.uuid4())
        await users_repo.create(
            {
                "id": uid,
                "username": USERNAME,
                "password_hash": hash_password(PASSWORD),
                "display_name": DISPLAY,
                "email": "",
                "role": "root",
                "domains": "[]",
            }
        )
        print(f"[seed] usuário E2E '{USERNAME}' criado como root (id={uid}).")
        return 0
    finally:
        await close_db()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
