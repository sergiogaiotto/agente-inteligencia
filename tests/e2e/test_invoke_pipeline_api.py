"""E2E do CONTRATO do invoke selado: POST /api/v1/pipelines/{id}/invoke.

Diferente de test_journey_invocar_pipeline.py (UI/Playwright, que PULA sem
pipeline pronto), este é um teste de API (httpx) AUTOSSUFICIENTE: semeia o
próprio ambiente (pipeline com A→B selado + um agente C FORA do pipeline),
exercita o contrato HTTP ponta-a-ponta e LIMPA tudo no fim.

Cobre:
- AUTH (contrato externo): 401 sem credencial; 200 via cookie de sessão;
  200 via header X-API-Key (o que o modal de cURL expõe).
- Erros determinísticos (sem LLM): 404 (id inexistente), 400 (sem message),
  422 (pipeline sem raiz), 409 (aposentado).
- SELO + execução: o agente FORA do pipeline (C) NÃO completa, mesmo havendo
  aresta B→C; o caminho feliz é best-effort (gated por saúde do LLM, `slow`).

Pré-requisitos (ver tests/e2e/conftest.py): app de pé + usuário E2E semeado
(`docker exec agente_app python scripts/seed_e2e_user.py`). Sem isso, PULA.
"""
from __future__ import annotations

import json
import os
import uuid

import httpx
import pytest

pytestmark = [pytest.mark.e2e]

BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:7000").rstrip("/")

# Prompt ESPECÍFICO (>50 chars, sem marcador genérico) para o agente NÃO ser
# detectado como pass-through (que pularia o LLM). Determinístico e barato.
_PROMPT = "Responda exclusivamente com a palavra OK em maiúsculas, sem pontuação nem texto adicional."


def _id_of(payload: dict) -> str | None:
    """Extrai o id de uma resposta de criação, tolerante a wrappers."""
    if not isinstance(payload, dict):
        return None
    if payload.get("id"):
        return payload["id"]
    for k in ("agent", "pipeline", "connection", "entry"):
        v = payload.get(k)
        if isinstance(v, dict) and v.get("id"):
            return v["id"]
    return None


def _create_agent(api, name: str) -> str:
    r = api.post("/api/v1/agents", json={
        "name": name, "kind": "subagent", "task_type": "reasoning",
        "system_prompt": _PROMPT, "temperature": 0.0,
    })
    assert r.status_code in (200, 201), f"criar agente falhou: {r.status_code} {r.text}"
    aid = _id_of(r.json())
    assert aid, f"sem id no agente criado: {r.text}"
    return aid


def _create_connection(api, src: str, tgt: str) -> str | None:
    r = api.post("/api/v1/mesh/connections", json={
        "source_agent_id": src, "target_agent_id": tgt, "connection_type": "sequential",
    })
    assert r.status_code in (200, 201), f"criar conexão falhou: {r.status_code} {r.text}"
    return _id_of(r.json())


def _llm_chat_ready() -> bool:
    """True se o papel de chat usado pelos agentes (reasoning) responde — para
    decidir se o caminho feliz roda ou pula (não testamos qualidade do LLM)."""
    try:
        d = httpx.get(f"{BASE_URL}/api/v1/llm/health", timeout=20.0).json()
    except Exception:
        return False
    chat = d.get("chat") or {}
    info = chat.get("reasoning") or {}
    return bool(info.get("ok")) or bool(d.get("all_ok"))


@pytest.fixture
def pipeline_env(api):
    """Cria A→B (membros, selados) + C (fora) e a aresta B→C; entry=A.

    C + a aresta B→C existem DE PROPÓSITO: provam o selo — o invoke é delimitado
    a {A,B}, então C nunca completa, apesar da aresta saindo do subgrafo.
    """
    sfx = uuid.uuid4().hex[:8]
    agents = {
        "a": _create_agent(api, f"E2E-Invoke-A-{sfx}"),
        "b": _create_agent(api, f"E2E-Invoke-B-{sfx}"),
        "c": _create_agent(api, f"E2E-Invoke-C-fora-{sfx}"),
    }
    conns: list[str] = []
    pid = None
    try:
        r = api.post("/api/v1/pipelines", json={"name": f"E2E Invoke {sfx}", "domain": "e2e"})
        assert r.status_code in (200, 201), f"criar pipeline falhou: {r.status_code} {r.text}"
        pid = _id_of(r.json())
        assert pid, f"sem id no pipeline: {r.text}"

        for k in ("a", "b"):
            rr = api.post(f"/api/v1/pipelines/{pid}/agents", json={"agent_id": agents[k]})
            assert rr.status_code in (200, 201), f"add membro {k} falhou: {rr.text}"

        cab = _create_connection(api, agents["a"], agents["b"])     # A→B (intra)
        cbc = _create_connection(api, agents["b"], agents["c"])     # B→C (sai do selo)
        conns = [c for c in (cab, cbc) if c]

        re_ = api.post(f"/api/v1/pipelines/{pid}/entry", json={"agent_id": agents["a"]})
        assert re_.status_code in (200, 201), f"set entry falhou: {re_.text}"

        yield {"pid": pid, **agents}
    finally:
        # teardown: conexões → pipeline → agentes (ordem evita FK pendente)
        for cid in conns:
            api.delete(f"/api/v1/mesh/connections/{cid}")
        if pid:
            api.delete(f"/api/v1/pipelines/{pid}")
        for aid in agents.values():
            api.delete(f"/api/v1/agents/{aid}")


# ───────────────────────────── AUTH ─────────────────────────────

def test_invoke_requires_auth(pipeline_env):
    """Sem cookie nem X-API-Key → 401 (não dispara execução nem gasta LLM)."""
    pid = pipeline_env["pid"]
    with httpx.Client(base_url=BASE_URL, timeout=20.0) as anon:  # sem credencial
        r = anon.post(f"/api/v1/pipelines/{pid}/invoke", json={"message": "ping"})
    assert r.status_code == 401, f"esperava 401 sem auth, veio {r.status_code}: {r.text}"


def test_invoke_with_api_key_authorizes(api, pipeline_env):
    """Contrato externo (o que o modal de cURL expõe): X-API-Key autoriza igual ao
    cookie. Provamos isso SEM disparar execução (independe de LLM): só o header,
    sem cookie. Uma key VÁLIDA passa pela auth (cai em 404/400 conforme o caso);
    sem a key (ou com key inválida) seria 401.
    """
    pid = pipeline_env["pid"]
    rk = api.post("/api/v1/api-keys", json={"name": f"e2e-invoke-{uuid.uuid4().hex[:6]}"})
    assert rk.status_code in (200, 201), f"criar api-key falhou: {rk.text}"
    key = rk.json().get("key")
    key_id = rk.json().get("id")
    assert key and key.startswith("ag_live_"), f"plaintext da key ausente: {rk.text}"
    try:
        with httpx.Client(base_url=BASE_URL, timeout=30.0) as ext:  # sem cookie, só a key
            # (1) key válida + pipeline inexistente → 404 (passou pela auth; sem key seria 401)
            r404 = ext.post(f"/api/v1/pipelines/{uuid.uuid4()}/invoke",
                            headers={"X-API-Key": key}, json={"message": "x"})
            assert r404.status_code == 404, f"esperava 404 (auth ok), veio {r404.status_code}: {r404.text}"
            # (2) key válida + mensagem vazia no pipeline real → 400 (auth ok, parou na validação)
            r400 = ext.post(f"/api/v1/pipelines/{pid}/invoke",
                            headers={"X-API-Key": key}, json={"message": "   "})
            assert r400.status_code == 400, f"esperava 400 (auth ok), veio {r400.status_code}: {r400.text}"
            # (3) prova negativa: key INVÁLIDA continua 401 (não vaza)
            rbad = ext.post(f"/api/v1/pipelines/{pid}/invoke",
                            headers={"X-API-Key": "ag_live_invalida-000000000000000000000000"},
                            json={"message": "x"})
            assert rbad.status_code == 401, f"key inválida deveria dar 401, veio {rbad.status_code}: {rbad.text}"
    finally:
        if key_id:
            api.delete(f"/api/v1/api-keys/{key_id}")


# ─────────────────────── erros determinísticos ───────────────────────

def test_invoke_404_unknown_pipeline(api):
    r = api.post(f"/api/v1/pipelines/{uuid.uuid4()}/invoke", json={"message": "x"})
    assert r.status_code == 404, r.text


def test_invoke_400_empty_message(api, pipeline_env):
    r = api.post(f"/api/v1/pipelines/{pipeline_env['pid']}/invoke", json={"message": "   "})
    assert r.status_code == 400, r.text


def test_invoke_422_pipeline_without_root(api):
    """Pipeline VAZIO (sem agentes) → raiz não resolvível → 422."""
    sfx = uuid.uuid4().hex[:6]
    r = api.post("/api/v1/pipelines", json={"name": f"E2E vazio {sfx}"})
    pid = _id_of(r.json())
    try:
        rr = api.post(f"/api/v1/pipelines/{pid}/invoke", json={"message": "x"})
        assert rr.status_code == 422, rr.text
    finally:
        api.delete(f"/api/v1/pipelines/{pid}")


def test_invoke_409_when_aposentado(api):
    """Pipeline aposentado → 409 (o gate de status roda antes de resolver o grafo)."""
    sfx = uuid.uuid4().hex[:6]
    r = api.post("/api/v1/pipelines", json={"name": f"E2E aposentado {sfx}"})
    pid = _id_of(r.json())
    try:
        # segue os next_states até 'aposentado' (rascunho→publicado→aposentado)
        for target in ("publicado", "aposentado"):
            api.post(f"/api/v1/pipelines/{pid}/status", json={"status": target})
        cur = api.get(f"/api/v1/pipelines/{pid}").json().get("status")
        if cur != "aposentado":
            pytest.skip(f"não consegui levar o pipeline a 'aposentado' (ficou em {cur!r}).")
        rr = api.post(f"/api/v1/pipelines/{pid}/invoke", json={"message": "x"})
        assert rr.status_code == 409, rr.text
    finally:
        api.delete(f"/api/v1/pipelines/{pid}")


# ───────────────── caminho feliz + selo (best-effort, LLM) ─────────────────

@pytest.mark.slow
def test_invoke_happy_is_sealed_to_members(api, pipeline_env):
    """Invoca selado e confere o CONTRATO + o SELO.

    - 200 com as chaves do contrato (output/final_state/completed_agents/
      pipeline_steps/interaction_id).
    - SELO: nenhum step COMPLETO é o agente C (fora do pipeline), mesmo com a
      aresta B→C; os steps completos ⊆ {A,B}.
    Não asserimos o TEXTO do LLM (flaky). Se o chat não estiver saudável, PULA.
    """
    if not _llm_chat_ready():
        pytest.skip("Papel de chat (reasoning) indisponível — caminho feliz não roda neste ambiente.")

    pid, a_id, b_id, c_id = (pipeline_env["pid"], pipeline_env["a"],
                             pipeline_env["b"], pipeline_env["c"])
    r = api.post(f"/api/v1/pipelines/{pid}/invoke", json={"message": "ping"})
    assert r.status_code == 200, f"invoke falhou: {r.status_code}: {r.text}"
    body = r.json()

    for k in ("pipeline_id", "output", "final_state", "completed_agents",
              "pipeline_steps", "interaction_id"):
        assert k in body, f"chave '{k}' ausente no contrato: {list(body)}"
    assert body["pipeline_id"] == pid
    assert isinstance(body["pipeline_steps"], list)

    steps = body["pipeline_steps"]
    completed = {s.get("agent_id") for s in steps if s.get("status") == "completed"}
    # SELO: o agente FORA do pipeline jamais completa
    assert c_id not in completed, f"VAZOU O SELO: agente externo {c_id} completou. steps={json.dumps(steps)}"
    # tudo que completou pertence ao pipeline {A,B}
    assert completed.issubset({a_id, b_id}), f"steps completos fora dos membros: {completed - {a_id, b_id}}"
    assert body["completed_agents"] <= 2
