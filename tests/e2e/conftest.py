"""Fixtures para os testes E2E de interface (Playwright).

Estes testes dirigem o app REAL num browser headless — não mockam nada. São a
camada que faltava na pirâmide: unit + integração (Postgres real) já cobrem o
backend; aqui validamos que as TELAS carregam e que as jornadas do usuário
funcionam ponta-a-ponta (login → criar agente → publicar no catálogo → invocar
pipeline), incluindo o JavaScript (Alpine.js).

Pré-requisitos para rodar:
    1. App de pé:  docker compose up -d app   (porta 7000 por padrão)
    2. Browser:    python -m playwright install chromium
    3. Usuário E2E semeado (só na 1ª vez, se o banco já tiver usuários):
           docker exec agente_app python scripts/seed_e2e_user.py

    pytest tests/e2e -m e2e

Config por env (defaults entre parênteses):
    E2E_BASE_URL   (http://localhost:7000)
    E2E_USERNAME   (e2e_admin)
    E2E_PASSWORD   (e2e-pass-1234)
    E2E_DISPLAY_NAME (E2E Admin)

Bootstrap é 100% via HTTP — o Postgres NÃO é publicado no host (docker-compose
não mapeia 5432), então não dá para semear direto no banco a partir do runner.
Em ambiente NOVO (sem usuários) o suite cria o root pelo fluxo real de setup.
Em ambiente com usuários, se as credenciais E2E não existirem, as jornadas que
exigem login PULAM com instrução de rodar o seed.
"""
from __future__ import annotations

import os
import time

import httpx
import pytest

BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:7000").rstrip("/")
E2E_USERNAME = os.environ.get("E2E_USERNAME", "e2e_admin")
E2E_PASSWORD = os.environ.get("E2E_PASSWORD", "e2e-pass-1234")
E2E_DISPLAY = os.environ.get("E2E_DISPLAY_NAME", "E2E Admin")


def _app_reachable() -> bool:
    try:
        r = httpx.get(f"{BASE_URL}/api/health", timeout=4.0)
        return r.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def _skip_if_app_down():
    """Pula TODOS os testes E2E se o app não estiver respondendo.

    Mantém a suíte unit/CI verde mesmo sem app de pé — os E2E simplesmente não
    rodam (skip), em vez de falhar. Mesmo espírito do conftest de integração.
    """
    if not _app_reachable():
        pytest.skip(
            f"App E2E inacessível em {BASE_URL}/api/health — suba com "
            "`docker compose up -d app` ou ajuste E2E_BASE_URL."
        )


@pytest.fixture(scope="session")
def base_url() -> str:
    """Sobrescreve o base_url do pytest-playwright/pytest-base-url.

    Faz `page.goto('/agents')` resolver para BASE_URL + '/agents'.
    """
    return BASE_URL


@pytest.fixture(scope="session")
def e2e_auth() -> dict:
    """Garante um usuário de teste e devolve a sessão (cookie user_id).

    Retorna dict com: user_id, username, password, available (bool).
    `available=False` sinaliza que não há credenciais válidas — as fixtures que
    dependem de login pulam com mensagem orientando rodar o seed.
    """
    info = {
        "user_id": None,
        "session_cookie": None,
        "username": E2E_USERNAME,
        "password": E2E_PASSWORD,
        "available": False,
    }
    with httpx.Client(base_url=BASE_URL, timeout=15.0) as c:
        has_users = True
        try:
            has_users = bool(c.get("/api/v1/users/check-setup").json().get("has_users"))
        except Exception:
            pass

        # Ambiente NOVO: cria o root pelo endpoint de setup (não exige auth quando
        # o sistema ainda não tem usuários) — exercita o fluxo real de 1º acesso.
        if not has_users:
            try:
                c.post(
                    "/api/v1/users",
                    json={
                        "username": E2E_USERNAME,
                        "password": E2E_PASSWORD,
                        "display_name": E2E_DISPLAY,
                        "role": "root",
                    },
                )
            except Exception:
                pass

        try:
            r = c.post(
                "/api/v1/users/login",
                json={"username": E2E_USERNAME, "password": E2E_PASSWORD},
            )
            if r.status_code == 200:
                uid = (r.json().get("user") or {}).get("id")
                # Cookie de sessão ASSINADO emitido pelo login (não o UUID cru):
                # é ele que deve ser reenviado nas requisições autenticadas.
                session_cookie = c.cookies.get("user_id")
                if uid and session_cookie:
                    info["user_id"] = uid
                    info["session_cookie"] = session_cookie
                    info["available"] = True
        except Exception:
            pass

    return info


def _require_auth(e2e_auth: dict) -> None:
    if not e2e_auth["available"]:
        pytest.skip(
            f"Sem credenciais E2E válidas para '{E2E_USERNAME}'. Rode o seed: "
            "`docker exec agente_app python scripts/seed_e2e_user.py` "
            "ou exporte E2E_USERNAME/E2E_PASSWORD de um usuário existente."
        )


class _RetryingClient:
    """httpx.Client com retry em HTTP 429 (respeita `retry_after`).

    O app tem rate limit (ex.: 60 req/60s no bucket 'api'); rodar a suíte E2E
    inteira gera rajadas que estouram o limite no setup/cleanup. Aqui esperamos
    a janela e re-tentamos — sem isso o teste falha por motivo alheio à feature.
    Demais métodos/atributos do httpx.Client são delegados via __getattr__.
    """

    def __init__(self, **kw):
        self._c = httpx.Client(**kw)

    def __getattr__(self, name):
        return getattr(self._c, name)

    def _request(self, method: str, url: str, **kw):
        resp = None
        for _ in range(6):
            resp = self._c.request(method, url, **kw)
            if resp.status_code != 429:
                return resp
            wait = 2
            try:
                wait = min(int(resp.json().get("retry_after", 2)) + 1, 20)
            except Exception:
                pass
            time.sleep(wait)
        return resp

    def get(self, url, **kw):
        return self._request("GET", url, **kw)

    def post(self, url, **kw):
        return self._request("POST", url, **kw)

    def put(self, url, **kw):
        return self._request("PUT", url, **kw)

    def delete(self, url, **kw):
        return self._request("DELETE", url, **kw)

    def close(self):
        self._c.close()


@pytest.fixture
def api(e2e_auth: dict):
    """Client autenticado (cookie user_id) p/ setup/cleanup, com retry em 429."""
    _require_auth(e2e_auth)
    c = _RetryingClient(
        base_url=BASE_URL,
        timeout=30.0,
        cookies={"user_id": e2e_auth["session_cookie"]},
    )
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def authed_page(browser, e2e_auth: dict):
    """Página Playwright já autenticada (cookie user_id injetado no contexto)."""
    _require_auth(e2e_auth)
    context = browser.new_context(base_url=BASE_URL)
    context.add_cookies(
        [{"name": "user_id", "value": e2e_auth["session_cookie"], "url": BASE_URL}]
    )
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()
