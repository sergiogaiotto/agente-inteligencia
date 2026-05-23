"""Testes dos defaults do LLM Routing.

DEFAULT_ROUTING é o fallback que vaza para a UI quando o operador ainda não
configurou roteamento no /settings. Ambientes novos partem desses valores —
não é só código defensivo, é o preset que o produto sugere.

Mudanças aqui exigem:
- Atualizar app/routes/dashboard.py task_descriptions (mencionam o default)
- Atualizar app/templates/pages/agent_form.html (fallback hardcoded)
- Comunicar ao time de produto (preset visível na criação de agent)

Esses testes pegam regressão acidental nessas 3 frentes.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.llm_routing import DEFAULT_ROUTING, TASK_TYPES


# ─── DEFAULT_ROUTING — contrato direto ─────────────────────────────


class TestDefaultRoutingContract:
    def test_tem_todas_as_5_chaves(self):
        """4 task types + multimodal_fallback. Falta de qualquer um faz UI
        mostrar 'carregando...' indefinidamente em agent_form."""
        for key in ("tool_calling", "reasoning", "instruct", "classification", "multimodal_fallback"):
            assert key in DEFAULT_ROUTING, f"DEFAULT_ROUTING sem '{key}'"

    def test_task_types_alinhado_com_routing(self):
        """TASK_TYPES e DEFAULT_ROUTING precisam combinar — divergência aqui
        gera 'task sem default' em runtime."""
        for t in TASK_TYPES:
            assert t in DEFAULT_ROUTING, f"task_type '{t}' sem default"

    def test_tool_calling_default_gpt_oss_120b(self):
        """tool_calling demanda inferência complexa — 120B suporta tool_choice
        bem em testes manuais. Mudar requer validar function calling em prod."""
        assert DEFAULT_ROUTING["tool_calling"] == "gpt-oss-120b/openai/gpt-oss-120b"

    def test_reasoning_default_gpt_oss_120b(self):
        """Raciocínio PT-BR também usa 120B — soberania de dados em BR
        (sem trânsito EU/US como acontecia com Maritaca SaaS)."""
        assert DEFAULT_ROUTING["reasoning"] == "gpt-oss-120b/openai/gpt-oss-120b"

    def test_instruct_default_gpt_oss_20b(self):
        """Instruction following é volume alto — 20B é o sweet-spot custo/qualidade."""
        assert DEFAULT_ROUTING["instruct"] == "gpt-oss-20b/openai/gpt-oss-20b"

    def test_classification_default_gpt_oss_20b(self):
        """Classification é o caso mais simples — 20B basta com folga."""
        assert DEFAULT_ROUTING["classification"] == "gpt-oss-20b/openai/gpt-oss-20b"

    def test_multimodal_fallback_continua_azure_gpt4o(self):
        """GPT-OSS atual é text-only — multimodal segue em Azure GPT-4o.
        Mudar requer ter outro multimodal validado em produção."""
        assert DEFAULT_ROUTING["multimodal_fallback"] == "azure/gpt-4o"

    def test_todos_os_valores_no_formato_provider_model(self):
        """Cada valor é 'provider/model' ou 'provider/model/variant'. Validação
        leve — protege contra typo tipo 'gpt-oss-120b' (sem '/')."""
        for key, value in DEFAULT_ROUTING.items():
            assert "/" in value, f"{key}='{value}' não tem '/'"
            parts = value.split("/", 1)
            assert parts[0] and parts[1], f"{key}='{value}' tem parte vazia"


# ─── GET /api/v1/dashboard/llm-routing — integração ────────────────


class TestLLMRoutingEndpoint:
    def _client(self, monkeypatch):
        """Mock load_routing pra evitar dependência de banco. Endpoint deve
        retornar DEFAULT_ROUTING quando load_routing retorna mesma config
        (caso 'ambiente novo sem nada salvo')."""
        async def fake_load_routing():
            return dict(DEFAULT_ROUTING)
        monkeypatch.setattr("app.llm_routing.load_routing", fake_load_routing)
        from app.routes.dashboard import router as dashboard_router
        app = FastAPI()
        app.include_router(dashboard_router)
        return TestClient(app)

    def test_endpoint_retorna_defaults_quando_sem_config(self, monkeypatch):
        c = self._client(monkeypatch)
        r = c.get("/api/v1/dashboard/llm-routing")
        assert r.status_code == 200
        body = r.json()
        # Estrutura esperada
        for key in ("routing", "defaults", "task_types", "task_descriptions"):
            assert key in body
        # Defaults batendo com o contrato
        assert body["defaults"] == DEFAULT_ROUTING
        # Quando load_routing devolve === defaults, routing também é igual
        assert body["routing"] == DEFAULT_ROUTING

    def test_task_descriptions_mencionam_gpt_oss(self, monkeypatch):
        """Texto exibido em /settings → Roteamento LLM deve refletir o
        default real — operador que lê 'Default: Maritaca' quando o default
        mudou para GPT-OSS fica confuso. Test detecta drift entre texto e
        DEFAULT_ROUTING."""
        c = self._client(monkeypatch)
        body = c.get("/api/v1/dashboard/llm-routing").json()
        descs = body["task_descriptions"]
        # GPT-OSS deve aparecer nas descrições de tool_calling/reasoning/instruct/classification
        for task in ("tool_calling", "reasoning", "instruct", "classification"):
            assert "GPT-OSS" in descs[task], f"{task} descrição não menciona GPT-OSS: {descs[task]!r}"
        # Multimodal continua mencionando Azure GPT-4o
        assert "GPT-4o" in descs["multimodal_fallback"]

    def test_task_descriptions_nao_mencionam_modelo_obsoleto(self, monkeypatch):
        """Defesa: 'Maritaca Sabiá-4' e 'azure/gpt-4o' eram os defaults antigos
        para tool_calling/reasoning/instruct/classification — não devem mais
        aparecer como 'Default: ' nessas descrições. Regressão acidental aqui
        confunde o usuário (texto diz uma coisa, dropdown sugere outra)."""
        c = self._client(monkeypatch)
        body = c.get("/api/v1/dashboard/llm-routing").json()
        descs = body["task_descriptions"]
        for task in ("tool_calling", "reasoning", "instruct", "classification"):
            assert "Maritaca Sabiá-4" not in descs[task], f"{task} ainda menciona Maritaca como default"
            # Azure GPT-4o pode aparecer em multimodal_fallback (correto), mas
            # NÃO como default de tool_calling/reasoning/instruct/classification
            assert "Default: Azure" not in descs[task], f"{task} ainda menciona Azure como default principal"
