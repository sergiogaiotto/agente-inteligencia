"""Testes da execução real de recipes (Onda 4 / PR #67).

Cobre dois níveis:

1. **Endpoints REST** — POST /entries/{id}/execute, GET /executions/{id},
   GET /entries/{id}/executions. Mocks: get_recipe, create_execution,
   get_execution, list_executions_for_entry, execute_recipe (background).
   O background task é capturado mas NÃO roda de verdade — testa-se
   apenas que o endpoint retorna 202 e dispara a chamada certa.

2. **Executor** — `app.catalog.executor.execute_recipe()` chamado direto.
   Mocks: `_invoke_step` (evita carregar engine LLM), append_step_result,
   finalize_execution, record_invocation_cost, catalog_entries_repo.find_by_id.
   Cobre chain (output→input), skip-after-failure, target inválido (deleted,
   draft, kind errado, sem artifact), exception no engine, cost auto-wire.
"""

from __future__ import annotations

import uuid
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import require_user
from app.core.database import audit_repo, catalog_entries_repo, users_repo
from app.routes.catalog import router as catalog_router


# ═════════════════════════════════════════════════════════════════
# Parte 1 — Endpoints REST
# ═════════════════════════════════════════════════════════════════


def _client(user: dict) -> TestClient:
    app = FastAPI()
    app.include_router(catalog_router)
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app)


def _entry(
    *,
    entry_id: str = "rcp-1",
    kind: str = "recipe",
    status: str = "published",
    owner_user_id: str = "owner-1",
    visibility: str = "company",
    name: str = "Pipeline Fiscal",
) -> dict:
    return {
        "id": entry_id,
        "name": name,
        "kind": kind,
        "status": status,
        "version": "1.0.0",
        "owner_user_id": owner_user_id,
        "visibility": visibility,
        "visibility_scope": None,
        "artifact_type": None,
        "artifact_id": None,
        "tags": "[]",
        "adapter_config": "{}",
        "urn": f"urn:maestro:default:recipe:{entry_id}:1.0.0",
    }


@pytest.fixture
def exec_api_storage(monkeypatch):
    """Mock dos helpers usados pelos 3 endpoints REST de execução."""
    entries: dict[str, dict] = {}
    recipes: dict[str, dict] = {}
    executions: dict[str, dict] = {}
    audit_log: list[dict] = []
    bg_calls: list[dict] = []

    async def fake_find_by_id(eid):
        return dict(entries[eid]) if eid in entries else None

    monkeypatch.setattr(catalog_entries_repo, "find_by_id", fake_find_by_id)

    async def fake_users_find(uid):
        return None  # routes só checam por id em alguns caminhos

    monkeypatch.setattr(users_repo, "find_by_id", fake_users_find)

    async def fake_audit_create(data):
        audit_log.append(dict(data))
        return data

    monkeypatch.setattr(audit_repo, "create", fake_audit_create)

    async def fake_get_recipe(entry_id):
        return dict(recipes[entry_id]) if entry_id in recipes else None

    async def fake_create_execution(*, recipe_entry_id, consumer_user_id, input_text, is_sandbox=False):
        from datetime import datetime, timezone
        exec_id = str(uuid.uuid4())
        row = {
            "id": exec_id,
            "recipe_entry_id": recipe_entry_id,
            "consumer_user_id": consumer_user_id,
            "input": input_text,
            "steps_results": [],
            "status": "running",
            "total_cost_usd": 0.0,
            "total_latency_ms": 0,
            "error_message": None,
            "started_at": datetime.now(timezone.utc),
            "finished_at": None,
            "is_sandbox": is_sandbox,
        }
        executions[exec_id] = row
        return dict(row)

    async def fake_get_execution(execution_id, enrich=True):
        return dict(executions[execution_id]) if execution_id in executions else None

    async def fake_list_executions_for_entry(recipe_entry_id, *, limit=50, offset=0):
        rows = [
            dict(e) for e in executions.values()
            if e["recipe_entry_id"] == recipe_entry_id
        ]
        rows.sort(key=lambda r: r["started_at"], reverse=True)
        return rows[offset:offset + limit]

    monkeypatch.setattr("app.routes.catalog.get_recipe", fake_get_recipe)
    monkeypatch.setattr("app.routes.catalog.create_execution", fake_create_execution)
    monkeypatch.setattr("app.routes.catalog.get_execution", fake_get_execution)
    monkeypatch.setattr(
        "app.routes.catalog.list_executions_for_entry", fake_list_executions_for_entry
    )

    # Background task — captura mas não executa
    async def fake_execute_recipe(**kwargs):
        bg_calls.append(kwargs)

    monkeypatch.setattr("app.catalog.executor.execute_recipe", fake_execute_recipe)

    return {
        "entries": entries,
        "recipes": recipes,
        "executions": executions,
        "audit": audit_log,
        "bg_calls": bg_calls,
    }


def _seed_recipe(storage, *, entry_id="rcp-1", owner_id="owner-1", with_manifest=True, **entry_over):
    e = _entry(entry_id=entry_id, owner_user_id=owner_id, **entry_over)
    storage["entries"][entry_id] = e
    if with_manifest:
        storage["recipes"][entry_id] = {
            "entry_id": entry_id,
            "steps": [
                {"order": 1, "target_entry_id": "tgt-a", "notes": "passo 1"},
                {"order": 2, "target_entry_id": "tgt-b", "notes": "passo 2"},
            ],
        }
    return e


# ─── POST /entries/{id}/execute ───────────────────────────────────


class TestExecuteEndpoint:
    def test_404_entry_inexistente(self, exec_api_storage):
        c = _client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries/nope/execute", json={"input": "oi"})
        assert r.status_code == 404

    def test_404_nao_visivel(self, exec_api_storage):
        # Entry private de outro owner — comum não vê
        _seed_recipe(exec_api_storage, owner_id="other", visibility="private")
        c = _client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries/rcp-1/execute", json={"input": "oi"})
        assert r.status_code == 404

    def test_422_nao_recipe(self, exec_api_storage):
        _seed_recipe(exec_api_storage, kind="agent")
        c = _client({"id": "u1", "role": "root"})
        r = c.post("/api/v1/catalog/entries/rcp-1/execute", json={"input": "oi"})
        assert r.status_code == 422
        assert "recipe" in r.json()["detail"].lower()

    def test_409_recipe_em_draft(self, exec_api_storage):
        _seed_recipe(exec_api_storage, status="draft")
        c = _client({"id": "u1", "role": "root"})
        r = c.post("/api/v1/catalog/entries/rcp-1/execute", json={"input": "oi"})
        assert r.status_code == 409
        assert "published" in r.json()["detail"].lower()

    def test_422_recipe_sem_manifest(self, exec_api_storage):
        _seed_recipe(exec_api_storage, with_manifest=False)
        c = _client({"id": "u1", "role": "root"})
        r = c.post("/api/v1/catalog/entries/rcp-1/execute", json={"input": "oi"})
        assert r.status_code == 422
        assert "steps" in r.json()["detail"].lower()

    def test_202_caminho_feliz(self, exec_api_storage):
        _seed_recipe(exec_api_storage)
        c = _client({"id": "u1", "role": "root"})
        r = c.post("/api/v1/catalog/entries/rcp-1/execute", json={"input": "oi"})
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "running"
        assert body["step_count"] == 2
        assert body["recipe_entry_id"] == "rcp-1"
        assert body["execution_id"]
        # Background task disparado
        assert len(exec_api_storage["bg_calls"]) == 1
        bg = exec_api_storage["bg_calls"][0]
        assert bg["execution_id"] == body["execution_id"]
        assert bg["recipe_entry_id"] == "rcp-1"
        assert bg["user_input"] == "oi"
        assert len(bg["steps"]) == 2
        # Audit registrado
        kinds = [e["action"] for e in exec_api_storage["audit"]]
        assert "recipe_execution_started" in kinds

    def test_422_input_vazio(self, exec_api_storage):
        _seed_recipe(exec_api_storage)
        c = _client({"id": "u1", "role": "root"})
        r = c.post("/api/v1/catalog/entries/rcp-1/execute", json={"input": ""})
        assert r.status_code == 422  # Pydantic min_length=1


# ─── GET /executions/{id} ────────────────────────────────────────


class TestGetExecution:
    def _seed_execution(self, storage, *, consumer="u1", recipe_owner="owner-1"):
        _seed_recipe(storage, owner_id=recipe_owner)
        exec_id = "exec-test-1"
        storage["executions"][exec_id] = {
            "id": exec_id,
            "recipe_entry_id": "rcp-1",
            "consumer_user_id": consumer,
            "input": "hi",
            "steps_results": [{"order": 1, "status": "success", "output": "o1"}],
            "status": "completed",
            "total_cost_usd": 0,
            "total_latency_ms": 100,
            "error_message": None,
            "started_at": "2026-05-19T00:00:00Z",
            "finished_at": "2026-05-19T00:00:01Z",
        }
        return exec_id

    def test_404_inexistente(self, exec_api_storage):
        c = _client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/executions/nope")
        assert r.status_code == 404

    def test_404_outro_usuario_sem_relacao(self, exec_api_storage):
        eid = self._seed_execution(exec_api_storage, consumer="u-alice", recipe_owner="owner-bob")
        c = _client({"id": "u-carlos", "role": "comum"})
        r = c.get(f"/api/v1/catalog/executions/{eid}")
        assert r.status_code == 404

    def test_consumer_pode_ver(self, exec_api_storage):
        eid = self._seed_execution(exec_api_storage, consumer="u1", recipe_owner="other")
        c = _client({"id": "u1", "role": "comum"})
        r = c.get(f"/api/v1/catalog/executions/{eid}")
        assert r.status_code == 200
        assert r.json()["status"] == "completed"

    def test_owner_pode_ver(self, exec_api_storage):
        eid = self._seed_execution(exec_api_storage, consumer="someone", recipe_owner="u-owner")
        c = _client({"id": "u-owner", "role": "comum"})
        r = c.get(f"/api/v1/catalog/executions/{eid}")
        assert r.status_code == 200

    def test_root_pode_ver(self, exec_api_storage):
        eid = self._seed_execution(exec_api_storage, consumer="other", recipe_owner="other")
        c = _client({"id": "u-root", "role": "root"})
        r = c.get(f"/api/v1/catalog/executions/{eid}")
        assert r.status_code == 200


# ─── GET /entries/{id}/executions ────────────────────────────────


class TestListExecutions:
    def test_404_entry_inexistente(self, exec_api_storage):
        c = _client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/entries/nope/executions")
        assert r.status_code == 404

    def test_422_nao_recipe(self, exec_api_storage):
        _seed_recipe(exec_api_storage, kind="agent")
        c = _client({"id": "u1", "role": "root"})
        r = c.get("/api/v1/catalog/entries/rcp-1/executions")
        assert r.status_code == 422

    def test_lista_vazia(self, exec_api_storage):
        _seed_recipe(exec_api_storage)
        c = _client({"id": "u1", "role": "root"})
        r = c.get("/api/v1/catalog/entries/rcp-1/executions")
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["has_more"] is False

    def test_lista_paginada(self, exec_api_storage):
        _seed_recipe(exec_api_storage)
        # Seed 3 execuções
        from datetime import datetime, timezone, timedelta
        base = datetime.now(timezone.utc)
        for i in range(3):
            exec_api_storage["executions"][f"e-{i}"] = {
                "id": f"e-{i}",
                "recipe_entry_id": "rcp-1",
                "consumer_user_id": "u1",
                "input": f"in-{i}",
                "steps_results": [],
                "status": "completed",
                "total_cost_usd": 0,
                "total_latency_ms": 0,
                "error_message": None,
                "started_at": base - timedelta(minutes=i),
                "finished_at": None,
            }
        c = _client({"id": "u1", "role": "root"})
        r = c.get("/api/v1/catalog/entries/rcp-1/executions?limit=2")
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 2
        assert body["has_more"] is True


# ═════════════════════════════════════════════════════════════════
# Parte 2 — Executor direto
# ═════════════════════════════════════════════════════════════════


@pytest.fixture
def executor_storage(monkeypatch):
    """Mock do executor: catalog_entries (target lookups), append/finalize/cost
    e _invoke_step (não carrega engine LLM)."""
    entries: dict[str, dict] = {}
    appended: list[dict] = []  # ordem de chamadas a append_step_result
    finalized: list[dict] = []  # chamadas a finalize_execution
    cost_rows: list[dict] = []  # chamadas a record_invocation_cost
    invocations: list[dict] = []  # chamadas a _invoke_step

    async def fake_entries_find(eid):
        return dict(entries[eid]) if eid in entries else None

    monkeypatch.setattr(catalog_entries_repo, "find_by_id", fake_entries_find)

    async def fake_append(execution_id, step_result):
        appended.append({"execution_id": execution_id, **step_result})

    async def fake_finalize(execution_id, *, status, total_cost_usd, total_latency_ms, error_message=None):
        finalized.append({
            "execution_id": execution_id, "status": status,
            "total_cost_usd": total_cost_usd, "total_latency_ms": total_latency_ms,
            "error_message": error_message,
        })

    async def fake_record_cost(entry_id, **kwargs):
        cost_rows.append({"entry_id": entry_id, **kwargs})

    monkeypatch.setattr("app.catalog.executor.append_step_result", fake_append)
    monkeypatch.setattr("app.catalog.executor.finalize_execution", fake_finalize)
    monkeypatch.setattr("app.catalog.executor.record_invocation_cost", fake_record_cost)

    # _invoke_step injetável por teste — default: echo simples + tokens fictícios
    async def default_invoke(target_entry, current_input, consumer_user_id):
        return {
            "output": f"out:{target_entry['id']}:{current_input}",
            "duration_ms": 100,
            "tokens_input": 30,
            "tokens_output": 20,
            "tokens_total": 50,
            "provider": "azure",
            "model": "gpt-4o-mini",
            "interaction_id": f"int-{target_entry['id']}",
            "final_state": "Recommend",
        }

    invoke_fn = {"fn": default_invoke}

    async def dispatch_invoke(target_entry, current_input, consumer_user_id):
        result = await invoke_fn["fn"](target_entry, current_input, consumer_user_id)
        invocations.append({
            "target_id": target_entry["id"],
            "current_input": current_input,
        })
        return result

    monkeypatch.setattr("app.catalog.executor._invoke_step", dispatch_invoke)

    def set_invoke(fn):
        invoke_fn["fn"] = fn

    return {
        "entries": entries,
        "appended": appended,
        "finalized": finalized,
        "cost_rows": cost_rows,
        "invocations": invocations,
        "set_invoke": set_invoke,
    }


def _agent_entry(eid: str, name: str = None) -> dict:
    return {
        "id": eid,
        "name": name or f"Agent {eid}",
        "kind": "agent",
        "status": "published",
        "artifact_type": "agent",
        "artifact_id": f"art-{eid}",
        "owner_user_id": "owner",
    }


@pytest.mark.asyncio
async def test_executor_caminho_feliz_3_steps_chain(executor_storage):
    """3 steps OK: output[N-1] vira input[N], finalize=completed."""
    from app.catalog.executor import execute_recipe

    executor_storage["entries"]["a"] = _agent_entry("a")
    executor_storage["entries"]["b"] = _agent_entry("b")
    executor_storage["entries"]["c"] = _agent_entry("c")

    steps = [
        {"order": 1, "target_entry_id": "a", "notes": ""},
        {"order": 2, "target_entry_id": "b", "notes": ""},
        {"order": 3, "target_entry_id": "c", "notes": ""},
    ]
    await execute_recipe(
        execution_id="exec-1",
        recipe_entry_id="rcp-1",
        steps=steps,
        consumer_user={"id": "u1"},
        user_input="hi",
    )

    # 3 steps registrados, todos success, na ordem
    assert len(executor_storage["appended"]) == 3
    assert [s["status"] for s in executor_storage["appended"]] == ["success", "success", "success"]
    assert [s["target_entry_id"] for s in executor_storage["appended"]] == ["a", "b", "c"]

    # Chain: step 2 recebeu output do step 1
    invs = executor_storage["invocations"]
    assert invs[0]["current_input"] == "hi"
    assert invs[1]["current_input"] == "out:a:hi"
    assert invs[2]["current_input"] == "out:b:out:a:hi"

    # Finalize completed
    assert len(executor_storage["finalized"]) == 1
    assert executor_storage["finalized"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_executor_skip_apos_falha(executor_storage):
    """Step 2 falha → step 3 fica skipped; status final = partial."""
    from app.catalog.executor import execute_recipe

    executor_storage["entries"]["a"] = _agent_entry("a")
    executor_storage["entries"]["b"] = _agent_entry("b")
    executor_storage["entries"]["c"] = _agent_entry("c")

    async def flaky(target_entry, current_input, consumer_user_id):
        if target_entry["id"] == "b":
            raise RuntimeError("boom")
        return {
            "output": f"out:{target_entry['id']}",
            "duration_ms": 50,
            "tokens_input": 6,
            "tokens_output": 4,
            "tokens_total": 10,
            "provider": "azure",
            "model": "gpt-4o-mini",
            "interaction_id": None,
            "final_state": "Recommend",
        }

    executor_storage["set_invoke"](flaky)

    await execute_recipe(
        execution_id="exec-2",
        recipe_entry_id="rcp-1",
        steps=[
            {"order": 1, "target_entry_id": "a", "notes": ""},
            {"order": 2, "target_entry_id": "b", "notes": ""},
            {"order": 3, "target_entry_id": "c", "notes": ""},
        ],
        consumer_user={"id": "u1"},
        user_input="hi",
    )

    statuses = [s["status"] for s in executor_storage["appended"]]
    assert statuses == ["success", "error", "skipped"]
    assert "boom" in executor_storage["appended"][1]["error"]
    assert executor_storage["finalized"][0]["status"] == "partial"


@pytest.mark.asyncio
async def test_executor_target_inexistente(executor_storage):
    """Step 1 com target_entry_id que não existe → error + demais skipped."""
    from app.catalog.executor import execute_recipe

    executor_storage["entries"]["b"] = _agent_entry("b")

    await execute_recipe(
        execution_id="exec-3",
        recipe_entry_id="rcp-1",
        steps=[
            {"order": 1, "target_entry_id": "ghost", "notes": ""},
            {"order": 2, "target_entry_id": "b", "notes": ""},
        ],
        consumer_user={"id": "u1"},
        user_input="hi",
    )

    statuses = [s["status"] for s in executor_storage["appended"]]
    assert statuses == ["error", "skipped"]
    assert "não existe" in executor_storage["appended"][0]["error"]
    assert executor_storage["finalized"][0]["status"] == "partial"


@pytest.mark.asyncio
async def test_executor_target_em_draft(executor_storage):
    """Target em status='draft' (não published) → step error."""
    from app.catalog.executor import execute_recipe

    e = _agent_entry("a")
    e["status"] = "draft"
    executor_storage["entries"]["a"] = e

    await execute_recipe(
        execution_id="exec-4",
        recipe_entry_id="rcp-1",
        steps=[{"order": 1, "target_entry_id": "a", "notes": ""}],
        consumer_user={"id": "u1"},
        user_input="hi",
    )

    assert executor_storage["appended"][0]["status"] == "error"
    assert "draft" in executor_storage["appended"][0]["error"]
    assert executor_storage["finalized"][0]["status"] == "partial"


@pytest.mark.asyncio
async def test_executor_target_kind_skill_nao_executavel(executor_storage):
    """Target kind=skill (não agent) → error nesta onda."""
    from app.catalog.executor import execute_recipe

    e = _agent_entry("s")
    e["kind"] = "skill"
    executor_storage["entries"]["s"] = e

    await execute_recipe(
        execution_id="exec-5",
        recipe_entry_id="rcp-1",
        steps=[{"order": 1, "target_entry_id": "s", "notes": ""}],
        consumer_user={"id": "u1"},
        user_input="hi",
    )

    assert executor_storage["appended"][0]["status"] == "error"
    assert "skill" in executor_storage["appended"][0]["error"]


@pytest.mark.asyncio
async def test_executor_target_sem_artifact_id(executor_storage):
    e = _agent_entry("a")
    e["artifact_id"] = None
    executor_storage["entries"]["a"] = e

    from app.catalog.executor import execute_recipe
    await execute_recipe(
        execution_id="exec-6",
        recipe_entry_id="rcp-1",
        steps=[{"order": 1, "target_entry_id": "a", "notes": ""}],
        consumer_user={"id": "u1"},
        user_input="hi",
    )
    assert executor_storage["appended"][0]["status"] == "error"
    assert "artifact_id" in executor_storage["appended"][0]["error"]


@pytest.mark.asyncio
async def test_executor_cost_auto_wire(executor_storage):
    """Cada step success gera 1 row em catalog_costs (via record_invocation_cost)."""
    from app.catalog.executor import execute_recipe

    executor_storage["entries"]["a"] = _agent_entry("a")
    executor_storage["entries"]["b"] = _agent_entry("b")

    await execute_recipe(
        execution_id="exec-7",
        recipe_entry_id="rcp-1",
        steps=[
            {"order": 1, "target_entry_id": "a", "notes": ""},
            {"order": 2, "target_entry_id": "b", "notes": ""},
        ],
        consumer_user={"id": "u1", "domains": ["fiscal"]},
        user_input="hi",
    )
    # 2 rows de cost — uma por step success
    assert len(executor_storage["cost_rows"]) == 2
    entry_ids = [r["entry_id"] for r in executor_storage["cost_rows"]]
    assert entry_ids == ["a", "b"]
    # tokens_used vem do mock default (50)
    assert executor_storage["cost_rows"][0]["tokens_used"] == 50
    # consumer_department puxado do primeiro elemento de domains
    assert executor_storage["cost_rows"][0]["consumer_department"] == "fiscal"
    # PR #69: cost_usd agora é real, calculado de tokens × pricing
    # mock default usa azure/gpt-4o-mini: 30 in × 0.00015 + 20 out × 0.0006 = 0.0000165
    assert executor_storage["cost_rows"][0]["cost_usd"] > 0
    # step_results devem trazer tokens_input/output e provider/model
    success_steps = [s for s in executor_storage["appended"] if s["status"] == "success"]
    assert success_steps[0]["tokens_input"] == 30
    assert success_steps[0]["tokens_output"] == 20
    assert success_steps[0]["provider"] == "azure"
    assert success_steps[0]["model"] == "gpt-4o-mini"
    assert success_steps[0]["cost_usd"] > 0


@pytest.mark.asyncio
async def test_executor_steps_desordenados_sao_executados_em_ordem(executor_storage):
    """Defensivo: se vier {order:2, order:1}, executor reordena ascendente."""
    from app.catalog.executor import execute_recipe

    executor_storage["entries"]["a"] = _agent_entry("a", name="A")
    executor_storage["entries"]["b"] = _agent_entry("b", name="B")

    await execute_recipe(
        execution_id="exec-8",
        recipe_entry_id="rcp-1",
        steps=[
            {"order": 2, "target_entry_id": "b", "notes": ""},
            {"order": 1, "target_entry_id": "a", "notes": ""},
        ],
        consumer_user={"id": "u1"},
        user_input="hi",
    )
    # Primeiro executado = order 1 (target a), depois order 2 (target b)
    assert executor_storage["invocations"][0]["target_id"] == "a"
    assert executor_storage["invocations"][1]["target_id"] == "b"


# ═════════════════════════════════════════════════════════════════
# Parte 3 — Sandbox (Onda 4 PR #70)
# ═════════════════════════════════════════════════════════════════


class TestSandboxEndpoint:
    """POST /entries/{id}/sandbox — auth=owner|root, qualquer status, sem cost."""

    def test_404_entry_inexistente(self, exec_api_storage):
        c = _client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries/nope/sandbox", json={"input": "oi"})
        assert r.status_code == 404

    def test_403_nao_owner_nao_root(self, exec_api_storage):
        _seed_recipe(exec_api_storage, owner_id="alice", status="draft")
        c = _client({"id": "bob", "role": "comum"})
        r = c.post("/api/v1/catalog/entries/rcp-1/sandbox", json={"input": "oi"})
        assert r.status_code in (403, 404)  # 404 se nem ve a entry; 403 se ve mas nao muda

    def test_422_nao_recipe(self, exec_api_storage):
        _seed_recipe(exec_api_storage, kind="agent")
        c = _client({"id": "u1", "role": "root"})
        r = c.post("/api/v1/catalog/entries/rcp-1/sandbox", json={"input": "oi"})
        assert r.status_code == 422

    def test_422_sem_manifest(self, exec_api_storage):
        _seed_recipe(exec_api_storage, with_manifest=False)
        c = _client({"id": "u1", "role": "root"})
        r = c.post("/api/v1/catalog/entries/rcp-1/sandbox", json={"input": "oi"})
        assert r.status_code == 422

    def test_202_owner_em_draft(self, exec_api_storage):
        """Diferencial do sandbox: aceita draft (vs /execute que exige published)."""
        _seed_recipe(exec_api_storage, owner_id="u-owner", status="draft")
        c = _client({"id": "u-owner", "role": "comum"})
        r = c.post("/api/v1/catalog/entries/rcp-1/sandbox", json={"input": "oi"})
        assert r.status_code == 202
        body = r.json()
        assert body["is_sandbox"] is True
        assert body["status"] == "running"
        # Background task disparado com is_sandbox=True
        assert len(exec_api_storage["bg_calls"]) == 1
        assert exec_api_storage["bg_calls"][0]["is_sandbox"] is True
        # Audit do sandbox
        actions = [e["action"] for e in exec_api_storage["audit"]]
        assert "recipe_sandbox_started" in actions

    def test_202_root_em_qualquer_status(self, exec_api_storage):
        # Root pode em qualquer status, mesmo de entry de outro owner
        _seed_recipe(exec_api_storage, owner_id="alice", status="approved")
        c = _client({"id": "u-root", "role": "root"})
        r = c.post("/api/v1/catalog/entries/rcp-1/sandbox", json={"input": "oi"})
        assert r.status_code == 202

    def test_202_owner_em_published(self, exec_api_storage):
        """Sandbox também funciona em published (não obriga draft)."""
        _seed_recipe(exec_api_storage, owner_id="u-owner", status="published")
        c = _client({"id": "u-owner", "role": "comum"})
        r = c.post("/api/v1/catalog/entries/rcp-1/sandbox", json={"input": "oi"})
        assert r.status_code == 202


@pytest.mark.asyncio
async def test_executor_sandbox_nao_grava_cost(executor_storage):
    """is_sandbox=True não chama record_invocation_cost.
    step_results ainda contém cost_usd calculado (para drill-down)."""
    from app.catalog.executor import execute_recipe

    executor_storage["entries"]["a"] = _agent_entry("a")
    executor_storage["entries"]["b"] = _agent_entry("b")

    await execute_recipe(
        execution_id="exec-sb-1",
        recipe_entry_id="rcp-1",
        steps=[
            {"order": 1, "target_entry_id": "a", "notes": ""},
            {"order": 2, "target_entry_id": "b", "notes": ""},
        ],
        consumer_user={"id": "u1", "domains": ["fiscal"]},
        user_input="hi",
        is_sandbox=True,
    )
    # Zero rows em catalog_costs
    assert len(executor_storage["cost_rows"]) == 0
    # Mas step_results ainda têm cost_usd calculado (para drill-down)
    success_steps = [s for s in executor_storage["appended"] if s["status"] == "success"]
    assert len(success_steps) == 2
    assert success_steps[0]["cost_usd"] > 0
    assert success_steps[0]["tokens_input"] == 30


@pytest.mark.asyncio
async def test_executor_default_continua_gravando_cost(executor_storage):
    """Regressão: sem is_sandbox (default=False), cost_rows são gravados normalmente."""
    from app.catalog.executor import execute_recipe

    executor_storage["entries"]["a"] = _agent_entry("a")

    await execute_recipe(
        execution_id="exec-prod-1",
        recipe_entry_id="rcp-1",
        steps=[{"order": 1, "target_entry_id": "a", "notes": ""}],
        consumer_user={"id": "u1"},
        user_input="hi",
    )
    # Default chama record_invocation_cost
    assert len(executor_storage["cost_rows"]) == 1
