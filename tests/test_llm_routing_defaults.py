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
    def test_tem_todas_as_chaves(self):
        """5 task types + multimodal_fallback. Falta de qualquer um faz UI
        mostrar 'carregando...' indefinidamente em agent_form.

        skill_generation foi adicionado em 2026-05-29 — separado de reasoning
        após gpt-oss-120b errar 4x consecutivas o mesmo bug Context7."""
        for key in ("tool_calling", "reasoning", "instruct", "classification",
                    "skill_generation", "multimodal_fallback"):
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

    def test_skill_generation_fallback_ultimo_recurso_azure_gpt4o(self):
        """skill_generation roda o Wizard de criar/alterar SKILL.md. O default
        EFETIVO segue o Modelo Primário global (ver global_primary_routing +
        load_routing). O valor em DEFAULT_ROUTING é só o ÚLTIMO recurso, usado
        quando nenhum Modelo Primário está configurado em platform_settings."""
        assert DEFAULT_ROUTING["skill_generation"] == "azure/gpt-4o"

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
    def _client(self, monkeypatch, global_model=None):
        """Mock load_routing pra evitar dependência de banco. Endpoint deve
        retornar DEFAULT_ROUTING quando load_routing retorna mesma config
        (caso 'ambiente novo sem nada salvo').

        global_model: simula o Modelo Primário global. None = não configurado
        (defaults batem com DEFAULT_ROUTING)."""
        async def fake_load_routing():
            return dict(DEFAULT_ROUTING)
        monkeypatch.setattr("app.llm_routing.load_routing", fake_load_routing)
        monkeypatch.setattr("app.llm_routing.global_primary_routing", lambda: global_model)
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
        # skill_generation: default é o modelo global da plataforma — descrição
        # NÃO deve fixar um modelo específico (azure/gpt-4o), e sim apontar pro
        # Modelo Primário (operador troca via UI).
        sg = descs["skill_generation"]
        assert "global" in sg.lower(), f"skill_generation não menciona modelo global: {sg!r}"
        assert "GPT-4o" not in sg, f"skill_generation ainda fixa Azure GPT-4o: {sg!r}"
        # Mensagem amigável: descreve o modo de uso, sem histórico do incidente
        assert "Context7" not in sg
        assert "4x" not in sg

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

    def test_endpoint_defaults_skill_generation_usa_modelo_global(self, monkeypatch):
        """Quando há Modelo Primário configurado, o `defaults` que vai pra UI
        (botão "padrões recomendados") aponta skill_generation pro modelo global
        — não pro hardcoded azure/gpt-4o."""
        c = self._client(monkeypatch, global_model="gpt-oss-120b/openai/gpt-oss-120b")
        body = c.get("/api/v1/dashboard/llm-routing").json()
        assert body["defaults"]["skill_generation"] == "gpt-oss-120b/openai/gpt-oss-120b"
        # Outros defaults seguem inalterados
        assert body["defaults"]["reasoning"] == DEFAULT_ROUTING["reasoning"]


# ─── Default global de skill_generation (modelo primário) ──────────


class TestSkillGenerationGlobalDefault:
    """skill_generation: "sempre usar o modelo global como default, permitindo
    override do usuário". O modelo global é o Modelo Primário da plataforma
    (primary_provider/primary_model)."""

    def _patch_settings(self, monkeypatch, provider, model):
        from types import SimpleNamespace
        monkeypatch.setattr(
            "app.core.config.get_settings",
            lambda: SimpleNamespace(primary_provider=provider, primary_model=model),
        )

    def test_global_primary_routing_formata_provider_model(self, monkeypatch):
        from app.llm_routing import global_primary_routing
        self._patch_settings(monkeypatch, "azure", "gpt-4o")
        assert global_primary_routing() == "azure/gpt-4o"

    def test_global_primary_routing_none_quando_nao_configurado(self, monkeypatch):
        from app.llm_routing import global_primary_routing
        self._patch_settings(monkeypatch, "", "")
        assert global_primary_routing() is None

    def test_load_routing_skill_generation_segue_modelo_global(self, monkeypatch):
        """Sem override explícito do operador, skill_generation resolve pro
        Modelo Primário global (não pro hardcoded azure/gpt-4o)."""
        import asyncio
        import app.llm_routing as lr

        async def fake_get_all():
            return {}  # operador não salvou nenhum llm_routing.*
        monkeypatch.setattr("app.core.database.settings_store.get_all", fake_get_all)
        monkeypatch.setattr(lr, "global_primary_routing", lambda: "gpt-oss-120b/openai/gpt-oss-120b")
        lr._routing_cache_at = 0.0  # força reload (ignora cache de testes anteriores)

        routing = asyncio.run(lr.load_routing())
        assert routing["skill_generation"] == "gpt-oss-120b/openai/gpt-oss-120b"

    def test_load_routing_respeita_override_explicito_do_operador(self, monkeypatch):
        """Quando o operador SALVOU skill_generation no /settings, esse valor
        vence o modelo global — usuário sempre pode definir o modelo de uso."""
        import asyncio
        import app.llm_routing as lr

        async def fake_get_all():
            return {"llm_routing.skill_generation": "openai/gpt-4.1"}
        monkeypatch.setattr("app.core.database.settings_store.get_all", fake_get_all)
        monkeypatch.setattr(lr, "global_primary_routing", lambda: "gpt-oss-120b/openai/gpt-oss-120b")
        lr._routing_cache_at = 0.0

        routing = asyncio.run(lr.load_routing())
        assert routing["skill_generation"] == "openai/gpt-4.1"

    def test_load_routing_ultimo_recurso_quando_sem_modelo_global(self, monkeypatch):
        """Sem override E sem Modelo Primário → cai no hardcoded de DEFAULT_ROUTING."""
        import asyncio
        import app.llm_routing as lr

        async def fake_get_all():
            return {}
        monkeypatch.setattr("app.core.database.settings_store.get_all", fake_get_all)
        monkeypatch.setattr(lr, "global_primary_routing", lambda: None)
        lr._routing_cache_at = 0.0

        routing = asyncio.run(lr.load_routing())
        assert routing["skill_generation"] == DEFAULT_ROUTING["skill_generation"]
