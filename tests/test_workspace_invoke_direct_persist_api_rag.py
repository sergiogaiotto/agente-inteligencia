"""Bug 2 de 3 (2026-06-01): persistência de invocações API/RAG via slash.

A PR #243 implementou persistência apenas no caminho MCP de
`/api/v1/workspace/invoke-binding-direct`. Os caminhos API/tabular
(`_invoke_api_binding_direct`) e RAG (`_invoke_rag_binding_direct`)
não chamavam `_persist_invoke_turn`, então invocações desses tipos
sumiam ao recarregar a sessão pela sidebar.

User reportou: invocou Consultar CEP (binding_kind=api), viu a resposta,
mas ao reabrir a sessão estava vazia ("sessão excluída").

Este arquivo cobre a paridade: API/tabular/RAG agora persistem 1 turn
no mesmo formato que o MCP, e retornam `interaction_id` no response.
"""
from __future__ import annotations


from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_client():
    """TestClient com router de workspace e auth bypass."""
    from app.routes.workspace import router as ws_router
    from app.core.auth import require_user

    app = FastAPI()
    app.include_router(ws_router)

    async def fake_user():
        return {"id": "u1", "email": "test@local"}

    app.dependency_overrides[require_user] = fake_user
    return TestClient(app)


def _patch_repos(monkeypatch, *, existing_session=None, existing_turns=None):
    """Patcha interactions_repo + turns_repo capturando chamadas."""
    calls = {
        "interactions_create": [],
        "interactions_update": [],
        "turns_create": [],
        "interactions_find": [],
    }

    async def fake_int_find(sid):
        calls["interactions_find"].append(sid)
        return existing_session

    async def fake_int_create(row):
        calls["interactions_create"].append(row)
        return row

    async def fake_int_update(sid, patch):
        calls["interactions_update"].append((sid, patch))
        return True

    async def fake_turns_find_all(limit=500, **filters):
        return existing_turns or []

    async def fake_turns_create(row):
        calls["turns_create"].append(row)
        return row

    monkeypatch.setattr("app.routes.workspace.interactions_repo.find_by_id", fake_int_find)
    monkeypatch.setattr("app.routes.workspace.interactions_repo.create", fake_int_create)
    monkeypatch.setattr("app.routes.workspace.interactions_repo.update", fake_int_update)
    monkeypatch.setattr("app.routes.workspace.turns_repo.find_all", fake_turns_find_all)
    monkeypatch.setattr("app.routes.workspace.turns_repo.create", fake_turns_create)
    return calls


# ─── API binding (binding_kind="api") ───────────────────────────────


SKILL_DECLARATIVE = """---
id: urn:skill:tech:subagent:consultar-cep
version: 0.1.0
kind: subagent
owner: e
stability: alpha
execution_mode: declarative
---

# Consultar CEP

## Purpose
Consulta CEP.

## Inputs
```json
{"type":"object","required":["cep"],"properties":{"cep":{"type":"string"}}}
```

## Workflow
1. Execute o endpoint.

## API Bindings
```yaml
- id: ep-cep
  connector: c-brasilapi
  connector_id: c-brasilapi
  name: Consultar CEP
  method: GET
  path: /api/cep/v1/{cep}
```

## Output Contract
```json
{"type":"object"}
```

## Failure Modes
- Erro 4xx/5xx.

## Evidence Policy
Skill depende exclusivamente do endpoint Consultar CEP.

## Guardrails
- Sem PII.
"""


def _patch_api_db(monkeypatch):
    """Patcha as deps de agent/skill para o caminho API."""
    agent = {"id": "agent-1", "skill_id": "skill-1"}
    skill = {
        "id": "skill-1",
        "name": "Consultar CEP",
        "raw_content": SKILL_DECLARATIVE,
    }

    async def fake_agent_find(aid):
        return agent if aid == "agent-1" else None

    async def fake_skill_find(sid):
        return skill if sid == "skill-1" else None

    monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_agent_find)
    monkeypatch.setattr("app.core.database.skills_repo.find_by_id", fake_skill_find)


def _patch_declarative_engine(monkeypatch, decl_result):
    """Faz execute_declarative devolver um dict arbitrário."""
    async def fake_exec(*args, **kwargs):
        return decl_result
    monkeypatch.setattr("app.agents.declarative_engine.execute_declarative", fake_exec)


def _patch_declarative_engine_capturing(monkeypatch, decl_result):
    """Como _patch_declarative_engine, mas captura os kwargs recebidos pelo
    engine. Usado para provar que o route é dono da sessão (session_id +
    register_interaction=False) e não deixa o engine criar uma 2ª sessão."""
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured.clear()
        captured.update(kwargs)
        return decl_result

    monkeypatch.setattr("app.agents.declarative_engine.execute_declarative", fake_exec)
    return captured


def _api_payload(**overrides):
    # Convenção do handler (workspace.py:1068-1075): para binding_kind="api"
    # ou "tabular", binding_id deve ser igual a skill_id.
    base = {
        "agent_id": "agent-1",
        "skill_id": "skill-1",
        "binding_kind": "api",
        "binding_id": "skill-1",
        "params": {"cep": "13211740"},
    }
    base.update(overrides)
    return base


class TestApiBindingPersistence:
    def test_no_message_keeps_legacy_ephemeral(self, monkeypatch):
        """Sem `message`: comportamento legado (não persiste, interaction_id=None)."""
        _patch_api_db(monkeypatch)
        calls = _patch_repos(monkeypatch)
        _patch_declarative_engine(monkeypatch, {
            "context": {"resposta": {"cep": "13211740"}},
            "bindings_executed": [{"status": 200}],
            "errors": [],
            "final_state": "completed",
        })

        client = _make_client()
        r = client.post("/api/v1/workspace/invoke-binding-direct", json=_api_payload())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["interaction_id"] is None
        assert not calls["interactions_create"]
        assert not calls["turns_create"]

    def test_with_message_empty_session_creates_new(self, monkeypatch):
        """`message` + session_id vazio → cria sessão nova e grava 2 turns."""
        _patch_api_db(monkeypatch)
        calls = _patch_repos(monkeypatch, existing_session=None)
        _patch_declarative_engine(monkeypatch, {
            "context": {"resposta": {"cep": "13211740", "city": "Campinas"}},
            "bindings_executed": [{"status": 200}],
            "errors": [],
            "final_state": "completed",
        })

        client = _make_client()
        r = client.post(
            "/api/v1/workspace/invoke-binding-direct",
            json=_api_payload(message="🛠️ Consultar CEP · cep=13211740"),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        sid = body["interaction_id"]
        assert sid, "deveria retornar UUID novo"
        # Sessão criada com agent_id correto + title derivado da message
        assert len(calls["interactions_create"]) == 1
        created = calls["interactions_create"][0]
        assert created["agent_id"] == "agent-1"
        assert "Consultar CEP" in created["title"]
        # Dois turns: user + assistant (output em fenced JSON pois é objeto)
        assert len(calls["turns_create"]) == 2
        user_turn, assistant_turn = calls["turns_create"]
        assert user_turn["user_text_redacted"] == "🛠️ Consultar CEP · cep=13211740"
        assert "```json" in assistant_turn["output_text_redacted"]
        assert "Campinas" in assistant_turn["output_text_redacted"]

    def test_with_existing_session_appends_next_turn(self, monkeypatch):
        """session_id existente → adiciona turn N+1 (não recria sessão)."""
        _patch_api_db(monkeypatch)
        existing_session = {"id": "s-1", "agent_id": "agent-1", "title": "X"}
        existing_turns = [{"turn_number": 4}, {"turn_number": 5}]
        calls = _patch_repos(
            monkeypatch,
            existing_session=existing_session,
            existing_turns=existing_turns,
        )
        _patch_declarative_engine(monkeypatch, {
            "context": {"resposta": "string simples"},
            "bindings_executed": [{"status": 200}],
            "errors": [],
            "final_state": "completed",
        })

        client = _make_client()
        r = client.post(
            "/api/v1/workspace/invoke-binding-direct",
            json=_api_payload(session_id="s-1", message="🛠️ Outra invocação"),
        )
        assert r.status_code == 200, r.text
        assert r.json()["interaction_id"] == "s-1"
        assert not calls["interactions_create"]
        assert calls["interactions_update"]
        # Numeração segue o existente (max=5 → next=6,7)
        nums = [t["turn_number"] for t in calls["turns_create"]]
        assert nums == [6, 7]
        # Resposta-string fica sem fence (paridade com MCP)
        assert calls["turns_create"][1]["output_text_redacted"] == "string simples"


class TestApiBindingSingleSession:
    """Bug 2026-06-02: invocar uma SKILL API via slash criava DUAS sessões —
    a do route (título = mensagem) + uma órfã "<agent> (declarativo)" criada
    pelo engine (session_id="" → UUID novo), que aparecia VAZIA na sidebar.
    Além disso o Execution Log mostrava "?? ??" e respostas HTTP-200 com aviso
    de mapping (final_state="partial") viravam "Failed" vermelho.

    O route agora: (a) é dono do sid e passa register_interaction=False ao
    engine; (b) deriva o log do binding_id real; (c) alinha ok/cor ao
    final_state do engine (só `failed` pinta vermelho).
    """

    def test_route_owns_session_and_disables_engine_create(self, monkeypatch):
        """Route passa register_interaction=False + o MESMO sid ao engine."""
        _patch_api_db(monkeypatch)
        calls = _patch_repos(monkeypatch, existing_session=None)
        captured = _patch_declarative_engine_capturing(monkeypatch, {
            "context": {"resposta": {"cep": "13211740"}},
            "bindings_executed": [
                {"binding_id": "ep-cep", "status": 200, "latency_ms": 42, "attempts": 1},
            ],
            "errors": [],
            "final_state": "completed",
        })

        client = _make_client()
        r = client.post(
            "/api/v1/workspace/invoke-binding-direct",
            json=_api_payload(message="🛠️ Consultar CEP · cep=13211740"),
        )
        assert r.status_code == 200, r.text
        sid = r.json()["interaction_id"]
        assert sid
        # Engine instruído a NÃO criar sessão...
        assert captured.get("register_interaction") is False
        # ...e recebeu o MESMO sid que o route persistiu (1 sessão, logs ligados).
        assert captured.get("session_id") == sid
        # Exatamente 1 interaction criada (sem a sessão "(declarativo)" órfã).
        assert len(calls["interactions_create"]) == 1
        assert calls["interactions_create"][0]["id"] == sid

    def test_exec_log_uses_binding_id_not_question_marks(self, monkeypatch):
        """Execution Log usa binding_id real (ex.: 'ep-cep'), nunca '?? ??'."""
        _patch_api_db(monkeypatch)
        _patch_repos(monkeypatch, existing_session=None)
        _patch_declarative_engine(monkeypatch, {
            "context": {"resposta": {"cep": "13211740"}},
            "bindings_executed": [
                {"binding_id": "ep-cep", "status": 200, "latency_ms": 42, "attempts": 1},
            ],
            "errors": [],
            "final_state": "completed",
        })

        client = _make_client()
        r = client.post(
            "/api/v1/workspace/invoke-binding-direct",
            json=_api_payload(message="🛠️ Consultar CEP · cep=13211740"),
        )
        assert r.status_code == 200, r.text
        tr = r.json()["trace"]["trace"]
        titles = [e["title"] for e in tr["execution_log"]]
        assert "ep-cep" in titles
        assert not any("?" in t for t in titles), titles
        # api_tools reflete binding_id + latência reais
        assert tr["api_tools"][0]["binding_id"] == "ep-cep"
        assert tr["api_tools"][0]["latency_ms"] == 42

    def test_partial_renders_green_with_warning(self, monkeypatch):
        """HTTP-200 + aviso de mapping (final_state='partial') = sucesso
        PARCIAL: ok=True, header verde (LogAndClose), diag de aviso."""
        _patch_api_db(monkeypatch)
        _patch_repos(monkeypatch, existing_session=None)
        _patch_declarative_engine(monkeypatch, {
            "context": {"resposta": {"cep": "13211740", "city": "Campinas"}},
            "bindings_executed": [
                {"binding_id": "ep-cep", "status": 200, "latency_ms": 50, "attempts": 1},
            ],
            "errors": ["[ep-cep] output_mapping 'extra': caminho não encontrado"],
            "final_state": "partial",
        })

        client = _make_client()
        r = client.post(
            "/api/v1/workspace/invoke-binding-direct",
            json=_api_payload(message="🛠️ Consultar CEP · cep=13211740"),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True  # parcial NÃO é falha
        assert body["trace"]["final_state"] == "LogAndClose"  # header verde ✓
        diags = body["trace"]["trace"]["diagnostics"]
        assert any(d["level"] == "warning" for d in diags), diags
        assert any("parcial" in d["text"].lower() for d in diags), diags

    def test_failed_renders_red(self, monkeypatch):
        """final_state='failed' (nenhum 2xx) = falha: ok=False, header vermelho."""
        _patch_api_db(monkeypatch)
        _patch_repos(monkeypatch, existing_session=None)
        _patch_declarative_engine(monkeypatch, {
            "context": {},
            "bindings_executed": [
                {"binding_id": "ep-cep", "status": 500, "latency_ms": 30,
                 "attempts": 3, "error": "[ep-cep] HTTP 500 — resposta não-2xx"},
            ],
            "errors": ["[ep-cep] HTTP 500 — resposta não-2xx"],
            "final_state": "failed",
        })

        client = _make_client()
        r = client.post(
            "/api/v1/workspace/invoke-binding-direct",
            json=_api_payload(message="🛠️ Consultar CEP · cep=00000000"),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert body["trace"]["final_state"] == "Failed"  # header vermelho 🔺
        diags = body["trace"]["trace"]["diagnostics"]
        assert any(d["level"] == "danger" for d in diags), diags
        # log do binding com nível danger (status 500)
        el = body["trace"]["trace"]["execution_log"]
        assert any(e["level"] == "danger" for e in el), el


# ─── RAG binding (binding_kind="rag") ───────────────────────────────


SKILL_RAG = """---
id: urn:skill:tech:subagent:busca
version: 0.1.0
kind: subagent
owner: e
stability: alpha
---

# Busca RAG

## Purpose
Busca em base.

## Inputs
```json
{"type":"object","required":["query"],"properties":{"query":{"type":"string"}}}
```

## Workflow
1. Busca.

## Tool Bindings
_Não usa MCP._

## Output Contract
```json
{"type":"object"}
```

## Failure Modes
- Sem resultados.

## Evidence Policy
```yaml
sources:
  - src-1
```

## Guardrails
- Sem PII.
"""


def _patch_rag_db(monkeypatch):
    """Patcha agent/skill/source para o caminho RAG."""
    agent = {"id": "agent-1", "skill_id": "skill-rag"}
    skill = {
        "id": "skill-rag",
        "name": "Busca RAG",
        "raw_content": SKILL_RAG,
    }
    source = {
        "id": "src-1",
        "name": "Base Teste",
        "authorized": 1,
        "confidentiality": "internal",
    }

    async def fake_agent_find(aid):
        return agent if aid == "agent-1" else None

    async def fake_skill_find(sid):
        return skill if sid == "skill-rag" else None

    async def fake_knowledge_find(kid):
        return source if kid == "src-1" else None

    monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_agent_find)
    monkeypatch.setattr("app.core.database.skills_repo.find_by_id", fake_skill_find)
    monkeypatch.setattr("app.core.database.knowledge_repo.find_by_id", fake_knowledge_find)


def _patch_retriever(monkeypatch, results):
    """Faz retriever.search devolver uma lista de objetos com os fields esperados."""
    class _Hit:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    hits = [_Hit(**r) for r in results]

    async def fake_search(query, skill_evidence_policy=None, top_n=5, allowed_source_ids=None):
        return hits

    # retriever é importado dentro do helper — patch no símbolo real
    monkeypatch.setattr("app.evidence.runtime.retriever.search", fake_search)


def _rag_payload(**overrides):
    base = {
        "agent_id": "agent-1",
        "skill_id": "skill-rag",
        "binding_kind": "rag",
        "binding_id": "src-1",
        "params": {"query": "agentes de IA", "top_n": 3},
    }
    base.update(overrides)
    return base


class TestRagBindingPersistence:
    def test_rag_with_message_creates_new_session(self, monkeypatch):
        """RAG com `message` cria sessão e grava chunks como fenced JSON."""
        _patch_rag_db(monkeypatch)
        calls = _patch_repos(monkeypatch, existing_session=None)
        _patch_retriever(monkeypatch, [
            {
                "evidence_id": "ev-1",
                "snippet_text": "agentes autônomos…",
                "relevance_score": 0.95,
                "source_name": "Base Teste",
                "source_id": "src-1",
                "confidentiality": "internal",
            },
        ])

        client = _make_client()
        r = client.post(
            "/api/v1/workspace/invoke-binding-direct",
            json=_rag_payload(message="🔍 Busca RAG · query=agentes de IA"),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        sid = body["interaction_id"]
        assert sid, "RAG deveria persistir e devolver interaction_id"
        assert len(calls["interactions_create"]) == 1
        assert "Busca RAG" in calls["interactions_create"][0]["title"]
        # Resposta é dict → vai como fenced JSON
        assistant_turn = calls["turns_create"][1]
        assert "```json" in assistant_turn["output_text_redacted"]
        assert "agentes autônomos" in assistant_turn["output_text_redacted"]

    def test_rag_no_message_keeps_ephemeral(self, monkeypatch):
        """Sem `message`, RAG também segue legado (não persiste)."""
        _patch_rag_db(monkeypatch)
        calls = _patch_repos(monkeypatch)
        _patch_retriever(monkeypatch, [])

        client = _make_client()
        r = client.post("/api/v1/workspace/invoke-binding-direct", json=_rag_payload())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["interaction_id"] is None
        assert not calls["interactions_create"]
