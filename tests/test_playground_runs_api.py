"""CRUD /api/v1/playground/runs — histórico do Playground por usuário (Feature 1).

Antes o histórico vivia só em localStorage (por-navegador). Agora é persistido no
servidor, escopado ao user autenticado. Testa: gravação escopada + created_at NAIVE
(armadilha asyncpg), listagem filtrada por user, e deleção que respeita o dono.

Padrão da casa: TestClient + dependency_overrides[require_user] + repo mockado (sem DB).
"""
import json
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
from app.routes import playground as pg


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client():
    app = FastAPI()
    app.include_router(pg.router)
    app.dependency_overrides[pg.require_user] = lambda: {"id": "u-test"}
    return TestClient(app, raise_server_exceptions=False)


def _client_noauth():
    """Sem override de require_user → exercita o contrato de auth (401)."""
    app = FastAPI()
    app.include_router(pg.router)
    return TestClient(app, raise_server_exceptions=False)


def test_sem_auth_401_em_todos_os_verbos():
    """Premissa da Feature 1 é escopo por-usuário: sem cookie/X-API-Key → 401
    (require_user corta antes de tocar o DB; nenhum mock necessário)."""
    c = _client_noauth()
    assert c.post("/api/v1/playground/runs", json={"message": "x"}).status_code == 401
    assert c.get("/api/v1/playground/runs").status_code == 401
    assert c.delete("/api/v1/playground/runs").status_code == 401
    assert c.delete("/api/v1/playground/runs/qualquer").status_code == 401


def test_post_run_poda_retem_recentes_apaga_antigas(monkeypatch):
    """Após gravar, _prune mantém só as _MAX_KEEP (50) mais recentes. find_all vem
    DESC (mais novas primeiro), então rows[50:] é o excedente ANTIGO — e a fresca
    (índice 0) sobrevive. Trava a direção da poda (regressão de ordenação mataria
    a recém-inserida)."""
    # 55 linhas, da mais nova (r0) à mais antiga (r54)
    rows = [{"id": f"r{i}"} for i in range(55)]
    monkeypatch.setattr(db.playground_runs_repo, "create", _async({}))
    monkeypatch.setattr(db.playground_runs_repo, "find_all", _async(rows))
    deleted = []

    async def fake_del(i):
        deleted.append(i)
        return True

    monkeypatch.setattr(db.playground_runs_repo, "delete", fake_del)
    r = _client().post("/api/v1/playground/runs", json={"message": "nova"})
    assert r.status_code == 201, r.text
    assert deleted == [f"r{i}" for i in range(50, 55)], "deve apagar só as 5 mais antigas"
    assert "r0" not in deleted, "a execução mais recente nunca é podada"


def test_post_run_grava_escopado_ao_user_com_created_naive(monkeypatch):
    """POST grava com user_id do autenticado e created_at NAIVE (coluna TIMESTAMP)."""
    captured = {}

    async def fake_create(row):
        captured.update(row)
        return row

    monkeypatch.setattr(db.playground_runs_repo, "create", fake_create)
    monkeypatch.setattr(db.playground_runs_repo, "find_all", _async([]))  # poda não acha nada
    monkeypatch.setattr(db.playground_runs_repo, "delete", _async(True))

    r = _client().post("/api/v1/playground/runs", json={
        "pipeline_id": "p1", "pipeline_name": "Suporte", "message": "oi",
        "verbosity": "summary", "status": "completed", "size_bytes": 3800, "duration_ms": 1200,
    })
    assert r.status_code == 201, r.text
    assert captured["user_id"] == "u-test"
    assert captured["pipeline_id"] == "p1"
    ca = captured["created_at"]
    assert isinstance(ca, datetime) and ca.tzinfo is None, "created_at precisa ser naive"
    body = r.json()
    assert body["id"] and body["pipeline_name"] == "Suporte" and body["size_bytes"] == 3800


def test_post_run_trunca_mensagem_longa(monkeypatch):
    captured = {}

    async def fake_create(row):
        captured.update(row)
        return row

    monkeypatch.setattr(db.playground_runs_repo, "create", fake_create)
    monkeypatch.setattr(db.playground_runs_repo, "find_all", _async([]))
    monkeypatch.setattr(db.playground_runs_repo, "delete", _async(True))
    r = _client().post("/api/v1/playground/runs", json={"message": "x" * 5000})
    assert r.status_code == 201, r.text
    assert len(captured["message"]) == 2000


def test_get_runs_lista_so_do_user(monkeypatch):
    seen = {}

    async def fake_find_all(**kw):
        seen.update(kw)
        return [{
            "id": "r1", "user_id": "u-test", "pipeline_id": "p1", "pipeline_name": "S",
            "message": "m", "verbosity": "summary", "status": "completed",
            "size_bytes": 100, "duration_ms": 50, "created_at": datetime(2026, 6, 28, 10, 0, 0),
        }]

    monkeypatch.setattr(db.playground_runs_repo, "find_all", fake_find_all)
    r = _client().get("/api/v1/playground/runs?limit=5")
    assert r.status_code == 200, r.text
    assert seen.get("user_id") == "u-test" and seen.get("limit") == 5
    runs = r.json()["runs"]
    assert len(runs) == 1 and runs[0]["created_at"].startswith("2026-06-28")


def test_get_runs_limit_invalido_422(monkeypatch):
    monkeypatch.setattr(db.playground_runs_repo, "find_all", _async([]))
    assert _client().get("/api/v1/playground/runs?limit=0").status_code == 422
    assert _client().get("/api/v1/playground/runs?limit=999").status_code == 422


def test_delete_run_de_outro_user_404(monkeypatch):
    """Deletar linha de OUTRO usuário → 404 (sem vazar existência)."""
    monkeypatch.setattr(db.playground_runs_repo, "find_by_id", _async({"id": "r1", "user_id": "outro"}))
    deleted = {"called": False}

    async def fake_del(i):
        deleted["called"] = True
        return True

    monkeypatch.setattr(db.playground_runs_repo, "delete", fake_del)
    r = _client().delete("/api/v1/playground/runs/r1")
    assert r.status_code == 404, r.text
    assert deleted["called"] is False, "não pode apagar linha de outro user"


def test_delete_run_do_dono_ok(monkeypatch):
    monkeypatch.setattr(db.playground_runs_repo, "find_by_id", _async({"id": "r1", "user_id": "u-test"}))
    deleted = {}

    async def fake_del(i):
        deleted["id"] = i
        return True

    monkeypatch.setattr(db.playground_runs_repo, "delete", fake_del)
    r = _client().delete("/api/v1/playground/runs/r1")
    assert r.status_code == 200, r.text
    assert deleted["id"] == "r1"


def test_clear_runs_apaga_todas_do_user(monkeypatch):
    monkeypatch.setattr(db.playground_runs_repo, "find_all", _async([{"id": "r1"}, {"id": "r2"}]))
    dels = []

    async def fake_del(i):
        dels.append(i)
        return True

    monkeypatch.setattr(db.playground_runs_repo, "delete", fake_del)
    r = _client().delete("/api/v1/playground/runs")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 2 and set(dels) == {"r1", "r2"}


# ── Thread completa (restaurar painéis ao clicar) ───────────────────────────────

def _stub_card_repos(monkeypatch):
    monkeypatch.setattr(db.playground_runs_repo, "create", _async({}))
    monkeypatch.setattr(db.playground_runs_repo, "find_all", _async([]))  # poda no-op
    monkeypatch.setattr(db.playground_runs_repo, "delete", _async(True))


def test_post_run_grava_thread_quando_cabe(monkeypatch):
    """POST com `thread` grava na tabela irmã (TEXT/json.dumps); has_thread=True."""
    _stub_card_repos(monkeypatch)
    tcap = {}

    async def fake_tcreate(row):
        tcap.update(row)
        return row

    monkeypatch.setattr(db.playground_threads_repo, "create", fake_tcreate)
    thread = {"result": {"output": "oi", "pipeline_steps": [{"agent_name": "A"}]},
              "timings": [{"start": 0, "end": 5}], "http": {"status": 200}}
    r = _client().post("/api/v1/playground/runs", json={"message": "m", "thread": thread})
    assert r.status_code == 201, r.text
    assert r.json()["has_thread"] is True
    assert tcap.get("id")  # mesma id da run (FK)
    assert json.loads(tcap["thread_json"])["result"]["output"] == "oi"


def test_post_run_thread_grande_so_grava_cartao(monkeypatch):
    """Thread acima do teto → só o cartão (has_thread=False, threads_repo não tocado)."""
    _stub_card_repos(monkeypatch)
    called = {"t": False}

    async def fake_tcreate(row):
        called["t"] = True
        return row

    monkeypatch.setattr(db.playground_threads_repo, "create", fake_tcreate)
    big = {"result": {"output": "x" * (pg._MAX_THREAD_BYTES + 10)}}
    r = _client().post("/api/v1/playground/runs", json={"message": "m", "thread": big})
    assert r.status_code == 201, r.text
    assert r.json()["has_thread"] is False
    assert called["t"] is False


def test_get_run_retorna_cartao_e_thread(monkeypatch):
    monkeypatch.setattr(db.playground_runs_repo, "find_by_id", _async({
        "id": "r1", "user_id": "u-test", "pipeline_id": "p1", "pipeline_name": "S",
        "message": "m", "verbosity": "summary", "status": "completed",
        "size_bytes": 100, "duration_ms": 50, "created_at": datetime(2026, 6, 28, 10, 0, 0),
    }))
    monkeypatch.setattr(db.playground_threads_repo, "find_by_id", _async(
        {"id": "r1", "thread_json": json.dumps({"result": {"output": "oi"}, "http": {"status": 200}})}))
    r = _client().get("/api/v1/playground/runs/r1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pipeline_name"] == "S"
    assert body["thread"]["result"]["output"] == "oi"
    assert body["thread"]["http"]["status"] == 200


def test_get_run_sem_thread_retorna_null(monkeypatch):
    monkeypatch.setattr(db.playground_runs_repo, "find_by_id",
                        _async({"id": "r1", "user_id": "u-test", "created_at": None}))
    monkeypatch.setattr(db.playground_threads_repo, "find_by_id", _async(None))
    r = _client().get("/api/v1/playground/runs/r1")
    assert r.status_code == 200, r.text
    assert r.json()["thread"] is None


def test_get_run_de_outro_user_404_sem_buscar_thread(monkeypatch):
    monkeypatch.setattr(db.playground_runs_repo, "find_by_id", _async({"id": "r1", "user_id": "outro"}))
    tcalled = {"c": False}

    async def fake_tfind(i):
        tcalled["c"] = True
        return None

    monkeypatch.setattr(db.playground_threads_repo, "find_by_id", fake_tfind)
    r = _client().get("/api/v1/playground/runs/r1")
    assert r.status_code == 404, r.text
    assert tcalled["c"] is False, "não pode buscar a thread de execução de outro user"
