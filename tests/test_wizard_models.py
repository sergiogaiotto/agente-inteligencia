"""Testes do catálogo de modelos exposto em /api/v1/wizard/models.

Esse endpoint é consumido pelo frontend (settings.html → Roteamento LLM e
agent_form.html → seleção de modelo). Testes garantem o contrato: todos os
providers esperados aparecem e cada modelo traz {id, name, tier} no mínimo.

Pure unit — endpoint é hardcode, sem I/O.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.wizard import router as wizard_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(wizard_router)
    return TestClient(app)


class TestWizardModelsContract:
    def test_endpoint_responde_200(self):
        r = _client().get("/api/v1/wizard/models")
        assert r.status_code == 200

    def test_providers_esperados_presentes(self):
        """Roteamento LLM em /settings depende destes providers — qualquer
        remoção/renomeação aqui quebra o dropdown de seleção."""
        body = _client().get("/api/v1/wizard/models").json()
        for prov in ("azure", "openai", "maritaca", "ollama", "gpt-oss-120b", "gpt-oss-20b"):
            assert prov in body, f"provider '{prov}' ausente do catálogo"
            assert isinstance(body[prov], list), f"'{prov}' deveria ser lista"
            assert len(body[prov]) >= 1, f"'{prov}' deveria ter ao menos 1 modelo"

    def test_cada_modelo_tem_shape_minimo(self):
        body = _client().get("/api/v1/wizard/models").json()
        required_keys = ("id", "name", "tier")
        for prov, models in body.items():
            for m in models:
                for k in required_keys:
                    assert k in m, f"{prov}/{m.get('id','?')} sem campo '{k}'"
                assert isinstance(m["id"], str) and m["id"], f"{prov}: id vazio"
                assert isinstance(m["name"], str) and m["name"], f"{prov}: name vazio"

    def test_gpt_oss_120b_tem_modelo_aceito_pelo_hub(self):
        """ID do GPT-OSS-120B deve bater com o que o hub interno aceita
        (openai/gpt-oss-120b — formato OpenAI-compatible)."""
        body = _client().get("/api/v1/wizard/models").json()
        ids = [m["id"] for m in body["gpt-oss-120b"]]
        assert "openai/gpt-oss-120b" in ids

    def test_gpt_oss_20b_tem_modelo_aceito_pelo_hub(self):
        body = _client().get("/api/v1/wizard/models").json()
        ids = [m["id"] for m in body["gpt-oss-20b"]]
        assert "openai/gpt-oss-20b" in ids

    def test_gpt_oss_tier_open_weight(self):
        """GPT-OSS é sempre tier 'open-weight' — UI usa pra agrupar."""
        body = _client().get("/api/v1/wizard/models").json()
        for prov in ("gpt-oss-120b", "gpt-oss-20b"):
            for m in body[prov]:
                assert m["tier"] == "open-weight", f"{prov}/{m['id']} tier='{m['tier']}'"

    def test_azure_e_openai_compartilham_mesma_lista(self):
        """Azure deployment-based usa mesmos modelos do OpenAI público — qualquer
        divergência aqui sinaliza erro de mapeamento (LiteLLM/engine roteia ambos
        para o mesmo backend)."""
        body = _client().get("/api/v1/wizard/models").json()
        assert body["azure"] == body["openai"]

    def test_multimodal_flag_presente_em_todos(self):
        """Routing decide se input com imagem cai no multimodal_fallback —
        depende de cada modelo ter o flag explícito (não inferir)."""
        body = _client().get("/api/v1/wizard/models").json()
        for prov, models in body.items():
            for m in models:
                assert "multimodal" in m, f"{prov}/{m['id']} sem flag multimodal"
                assert isinstance(m["multimodal"], bool), f"{prov}/{m['id']} multimodal não-bool"

    def test_gpt_oss_nao_e_multimodal(self):
        """GPT-OSS open-weight atual não tem suporte oficial a image input.
        Routing nunca deve cair em GPT-OSS para multimodal_fallback."""
        body = _client().get("/api/v1/wizard/models").json()
        for prov in ("gpt-oss-120b", "gpt-oss-20b"):
            for m in body[prov]:
                assert m["multimodal"] is False
