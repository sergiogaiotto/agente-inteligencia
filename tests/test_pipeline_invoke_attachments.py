"""Anexos no invoke de pipeline (fatia #3 da execução v2 + ramo base64 37.0.0).

Upload-ref (fluxo UI/modal): o backend mapeia a saída do /workspace/upload pra
forma que o engine consome ({name,type,size,content,abs_path}). Base64
(single-call p/ API, item 7 PR1): {filename, content_type?, content_base64}
decodificado pelo decoder COMUM (app/core/attachments — o mesmo do agent
invoke, com limites 5×10MB), fechando o drop silencioso de content_base64 —
imagem "aceita" que nunca chegava ao modelo. Violação → 422 nomeado; async →
422 acionável (base64 persistiria inteiro no job). Sem anexo → None.
"""
import base64 as b64mod
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
import app.catalog.pipeline_defs as pdefs
import app.agents.engine as engine
from app.routes import pipelines as pl_routes
from app.models.schemas import PipelineInvokeRequest

MESH_FLOW = Path("app/templates/pages/mesh_flow.html")


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client():
    app = FastAPI()
    app.include_router(pl_routes.router)
    app.dependency_overrides[pl_routes.require_user] = lambda: {"id": "u-test"}
    return TestClient(app, raise_server_exceptions=False)


def _stub(monkeypatch, capture):
    async def fake_exec(**k):
        capture.update(k)
        return {"status": "completed", "output": "ok", "pipeline_steps": [],
                "total_agents": 1, "completed_agents": 1, "interaction_id": "i", "duration_ms": 1}
    monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async({"id": "p1", "name": "P", "status": "publicado"}))
    monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": "r", "nodes": [{"id": "r"}], "edges": []}))
    monkeypatch.setattr(engine, "execute_pipeline", fake_exec)
    monkeypatch.setattr(db.audit_repo, "create", _async({}))


def test_schema_aceita_attachments():
    m = PipelineInvokeRequest(message="oi", attachments=[{"filename": "a.pdf"}])
    assert m.attachments == [{"filename": "a.pdf"}]
    assert PipelineInvokeRequest(message="oi").attachments is None


def test_invoke_mapeia_e_encaminha_anexo(monkeypatch):
    cap = {}
    _stub(monkeypatch, cap)
    r = _client().post("/api/v1/pipelines/p1/invoke", json={
        "message": "analise",
        "attachments": [{"filename": "a.pdf", "content_type": "application/pdf",
                         "size": 10, "text_content": "texto extraído", "path": "abc_a.pdf"}],
    })
    assert r.status_code == 200, r.text
    atts = cap["attachments"]
    assert len(atts) == 1
    a = atts[0]
    assert a["name"] == "a.pdf" and a["type"] == "application/pdf"
    assert a["content"] == "texto extraído" and a["size"] == 10
    assert a["abs_path"].endswith("abc_a.pdf")  # basename saneado sob UPLOAD_DIR


def test_invoke_sem_anexo_passa_none(monkeypatch):
    cap = {}
    _stub(monkeypatch, cap)
    r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "oi"})
    assert r.status_code == 200, r.text
    assert cap["attachments"] is None


def test_modal_tem_uploader():
    src = MESH_FLOW.read_text(encoding="utf-8")
    assert 'data-testid="pipeline-run-attach"' in src
    assert "uploadRunFiles(" in src
    assert "/api/v1/workspace/upload" in src
    # envia os anexos no invoke + estado inicial no modal
    assert "attachments: this.runModal.attachments" in src
    assert "attachments: [], uploading: false" in src


# ─── Ramo base64 (37.0.0 — item 7 PR1) ──────────────────────────────

# PNG 1×1 válido (mesma fixture de test_multimodal_vision)
_PNG_1x1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
    "2mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _b64(texto: str) -> str:
    return b64mod.b64encode(texto.encode()).decode()


def test_invoke_base64_texto_decodifica(monkeypatch):
    cap = {}
    _stub(monkeypatch, cap)
    r = _client().post("/api/v1/pipelines/p1/invoke", json={
        "message": "analise",
        "attachments": [{"filename": "nota.txt", "content_base64": _b64("linha um")}],
    })
    assert r.status_code == 200, r.text
    a = cap["attachments"][0]
    assert a["name"] == "nota.txt" and a["content"] == "linha um"
    assert "content_base64" not in a  # documento é textual, não dobra memória


def test_invoke_base64_imagem_carrega_content_base64(monkeypatch):
    """O fix do drop: imagem via API precisa chegar ao engine com o base64
    (senão o multimodal cai no caminho text-only — 'nenhuma imagem enviada')."""
    cap = {}
    _stub(monkeypatch, cap)
    r = _client().post("/api/v1/pipelines/p1/invoke", json={
        "message": "o que há na foto?",
        "attachments": [{"filename": "foto.png", "content_type": "image/png",
                         "content_base64": _PNG_1x1_B64}],
    })
    assert r.status_code == 200, r.text
    a = cap["attachments"][0]
    assert a["type"] == "image/png"
    assert a["content_base64"] == _PNG_1x1_B64


def test_invoke_misto_ref_e_base64(monkeypatch):
    cap = {}
    _stub(monkeypatch, cap)
    r = _client().post("/api/v1/pipelines/p1/invoke", json={
        "message": "analise",
        "attachments": [
            {"filename": "a.pdf", "text_content": "do upload", "path": "x_a.pdf"},
            {"filename": "b.txt", "content_base64": _b64("inline")},
        ],
    })
    assert r.status_code == 200, r.text
    names = [a["name"] for a in cap["attachments"]]
    assert names == ["a.pdf", "b.txt"]


def test_invoke_base64_invalido_422_nomeado(monkeypatch):
    cap = {}
    _stub(monkeypatch, cap)
    r = _client().post("/api/v1/pipelines/p1/invoke", json={
        "message": "x",
        "attachments": [{"filename": "z.bin", "content_base64": "@@não-é-base64@@"}],
    })
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "attachments_validation_failed"
    assert detail["rejected"][0]["kind"] == "invalid_base64"
    assert "attachments" not in cap  # nada executou


def test_invoke_base64_oversize_422(monkeypatch):
    cap = {}
    _stub(monkeypatch, cap)
    monkeypatch.setattr("app.core.attachments.MAX_ATTACHMENT_BYTES", 4)
    r = _client().post("/api/v1/pipelines/p1/invoke", json={
        "message": "x",
        "attachments": [{"filename": "n.txt", "content_base64": _b64("mais que 4 bytes")}],
    })
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["rejected"][0]["kind"] == "oversize"


def test_invoke_base64_overflow_422(monkeypatch):
    cap = {}
    _stub(monkeypatch, cap)
    atts = [{"filename": f"f{i}.txt", "content_base64": _b64("x")} for i in range(6)]
    r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "x", "attachments": atts})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["rejected"][0]["kind"] == "overflow"


def test_ref_text_content_inline_truncado_com_marcador(monkeypatch):
    """text_content injetado inline pela API é limitado (~50k) — e SEMPRE com
    o marcador explícito de truncamento (senão o modelo responde como se
    tivesse visto o documento inteiro — achado da revisão)."""
    cap = {}
    _stub(monkeypatch, cap)
    r = _client().post("/api/v1/pipelines/p1/invoke", json={
        "message": "x",
        "attachments": [{"filename": "gordo.txt", "text_content": "y" * 60_000}],
    })
    assert r.status_code == 200, r.text
    content = cap["attachments"][0]["content"]
    assert content.endswith("[...truncado em 50.000 caracteres]")
    assert len(content) < 51_000


def test_ref_payload_do_upload_passa_intacto(monkeypatch):
    """O payload legítimo do /workspace/upload (50k + marcador ≈ 50.04k) NÃO
    pode ser cortado — o slice seco decapitava o marcador (review)."""
    cap = {}
    _stub(monkeypatch, cap)
    upload_like = "y" * 50_000 + "\n\n[...truncado em 50.000 caracteres]"
    r = _client().post("/api/v1/pipelines/p1/invoke", json={
        "message": "x",
        "attachments": [{"filename": "doc.pdf", "text_content": upload_like, "path": "u_doc.pdf"}],
    })
    assert r.status_code == 200, r.text
    assert cap["attachments"][0]["content"] == upload_like


# ─── Normalização por item (achados da revisão — 500 → 422 nomeado) ──

def _post_atts(monkeypatch, atts):
    cap = {}
    _stub(monkeypatch, cap)
    return _client().post(
        "/api/v1/pipelines/p1/invoke", json={"message": "x", "attachments": atts}
    ), cap


def test_item_nao_dict_422(monkeypatch):
    r, cap = _post_atts(monkeypatch, ["sou-uma-string"])
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "attachments_validation_failed"
    assert "Anexo #0" in r.json()["detail"]["message"]


def test_content_base64_nao_string_422(monkeypatch):
    r, _ = _post_atts(monkeypatch, [{"filename": "x.png", "content_base64": 123}])
    assert r.status_code == 422, r.text
    assert "string base64" in r.json()["detail"]["message"]


def test_content_base64_vazio_422(monkeypatch):
    """'' e whitespace NÃO escorregam pro ramo ref como anexo oco (seria o
    drop silencioso de novo, e furaria o guard do async)."""
    for payload in ("", "   "):
        r, cap = _post_atts(
            monkeypatch, [{"filename": "foto.png", "content_base64": payload}]
        )
        assert r.status_code == 422, r.text
        assert "vazio" in r.json()["detail"]["message"]
        assert "attachments" not in cap


def test_text_content_nao_string_422(monkeypatch):
    r, _ = _post_atts(monkeypatch, [{"filename": "a.pdf", "text_content": 42}])
    assert r.status_code == 422, r.text
    assert "text_content" in r.json()["detail"]["message"]


def test_filename_nao_string_no_ramo_base64_nao_500(monkeypatch):
    """Decoder coage filename/content_type não-string (guess_type(int) dava
    TypeError → 500)."""
    r, cap = _post_atts(monkeypatch, [{"filename": 42, "content_base64": _b64("oi")}])
    assert r.status_code == 200, r.text
    assert cap["attachments"][0]["name"] == "42"


def test_fingerprint_distingue_conteudo_base64():
    """Idempotência: conteúdo base64 diferente ⇒ fingerprint diferente
    (35.14.2: campo relevante fora do hash = replay devolvendo a resposta de
    OUTRO documento quando o async aceitar base64). Item malformado não
    estoura (o fingerprint roda ANTES da validação no aceite async)."""
    f = lambda atts: pl_routes._request_fingerprint(
        "p1", PipelineInvokeRequest(message="x", attachments=atts)
    )
    a = f([{"filename": "n.txt", "content_base64": _b64("conteudo A")}])
    b = f([{"filename": "n.txt", "content_base64": _b64("conteudo B")}])
    ref = f([{"filename": "n.txt", "path": "u_n.txt"}])
    assert a != b and a != ref
    assert f(["malformado"])  # não levanta — identidade estável


def test_async_rejeita_base64_com_hint(monkeypatch):
    """No /invoke/async o ramo base64 → 422 acionável (o conteúdo persistiria
    inteiro em invoke_jobs.request_payload; storage/LGPD é a fatia seguinte)."""
    import asyncio
    from types import SimpleNamespace

    import pytest as _pytest
    from fastapi import HTTPException

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(pl_routes, "_guard_api_key_cost_budget", _noop)
    data = PipelineInvokeRequest(
        message="x",
        attachments=[{"filename": "f.png", "content_base64": _PNG_1x1_B64}],
    )
    req = SimpleNamespace(state=SimpleNamespace())
    with _pytest.raises(HTTPException) as exc:
        asyncio.get_event_loop().run_until_complete(
            pl_routes._finalize_invoke_inputs(
                "p1", {"id": "p1"}, "r", data, req, {"id": "u"}, "x",
                allow_base64=False,
            )
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "attachments_base64_not_supported_async"
