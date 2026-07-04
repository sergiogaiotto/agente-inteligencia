"""Papel "judge" no Roteamento LLM — card "LLM como Juiz" (24.8.0).

O modelo do MultiDimJudge (Verifier §14.2) deixa de ser env-only
(VERIFIER_JUDGE_MODEL) e vira papel de roteamento de 1ª classe:
- rota `llm_routing.judge` salva na UI (Configurações → Roteamento LLM) VENCE;
- sem rota salva, o default honra a env legada (retrocompat de instalações
  que já configuravam o juiz por .env);
- MultiDimJudge resolve via resolve_llm_for_task("judge"), com fallback
  para a env quando o roteamento não puder ser lido (DB fora etc.);
- `judge` é papel de PLATAFORMA — agentes NÃO podem usá-lo como task_type.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.llm_routing as lr
from app.llm_routing import DEFAULT_ROUTING, TASK_TYPES


@pytest.fixture(autouse=True)
def _reset_routing_cache():
    """load_routing cacheia 30s — limpa antes/depois pra não vazar mocks
    de settings_store entre testes (e para outros arquivos da suíte)."""
    lr._routing_cache_at = 0.0
    yield
    lr._routing_cache_at = 0.0
    lr._routing_cache = {}


# ─── Catálogo ───────────────────────────────────────────────────────

class TestJudgeInCatalog:
    def test_judge_em_task_types_e_default_routing(self):
        assert "judge" in TASK_TYPES
        assert DEFAULT_ROUTING["judge"] == "azure/gpt-4o"

    def test_agente_nao_pode_usar_judge_como_task_type(self):
        # judge é papel de PLATAFORMA (verifier) — o schema de agente continua
        # restrito aos 4 papéis geradores; sem isso o form de agente ganharia
        # um task_type que roteia o agente pro modelo do juiz.
        from app.models.schemas import AgentCreate
        with pytest.raises(ValidationError):
            AgentCreate(name="xx", task_type="judge")


# ─── Retrocompat com a env VERIFIER_JUDGE_MODEL ─────────────────────

class TestJudgeEnvRetrocompat:
    def _patch(self, monkeypatch, store: dict, env_judge: str):
        async def fake_get_all():
            return store
        monkeypatch.setattr(
            "app.core.database.settings_store.get_all", fake_get_all
        )
        monkeypatch.setattr(
            "app.core.config.get_settings",
            lambda: SimpleNamespace(
                verifier_judge_model=env_judge,
                primary_provider="", primary_model="",
            ),
        )

    def test_sem_rota_na_ui_env_legada_vale(self, monkeypatch):
        self._patch(monkeypatch, {}, "openai_public/gpt-4.1")
        routing = asyncio.run(lr.load_routing())
        assert routing["judge"] == "openai_public/gpt-4.1"

    def test_rota_salva_na_ui_vence_a_env(self, monkeypatch):
        self._patch(
            monkeypatch,
            {"llm_routing.judge": "maritaca/sabia-4"},
            "openai_public/gpt-4.1",
        )
        routing = asyncio.run(lr.load_routing())
        assert routing["judge"] == "maritaca/sabia-4"

    def test_env_sem_barra_e_ignorada(self, monkeypatch):
        # valor malformado (sem provider/) não substitui o default seguro
        self._patch(monkeypatch, {}, "gpt-4o")
        routing = asyncio.run(lr.load_routing())
        assert routing["judge"] == DEFAULT_ROUTING["judge"]

    def test_default_hardcoded_honra_deployment_azure_customizado(self, monkeypatch):
        """Finding da revisão 24.9.0: com o default intocado ("azure/gpt-4o")
        e deployment Azure de nome customizado, o model explícito viraria
        azure_deployment literal → 404 em todo julgamento. O default efetivo
        deve seguir o deployment REAL configurado."""
        async def fake_get_all():
            return {}
        monkeypatch.setattr("app.core.database.settings_store.get_all", fake_get_all)
        monkeypatch.setattr(
            "app.core.config.get_settings",
            lambda: SimpleNamespace(
                verifier_judge_model="azure/gpt-4o",
                azure_openai_chat_deployment="meu-gpt4o",
                primary_provider="", primary_model="",
            ),
        )
        routing = asyncio.run(lr.load_routing())
        assert routing["judge"] == "azure/meu-gpt4o"

    def test_env_customizada_nao_e_tocada_pelo_deployment(self, monkeypatch):
        # Operador que SETOU a env explicitamente (≠ default) manda nela.
        async def fake_get_all():
            return {}
        monkeypatch.setattr("app.core.database.settings_store.get_all", fake_get_all)
        monkeypatch.setattr(
            "app.core.config.get_settings",
            lambda: SimpleNamespace(
                verifier_judge_model="azure/o3-mini",
                azure_openai_chat_deployment="meu-gpt4o",
                primary_provider="", primary_model="",
            ),
        )
        routing = asyncio.run(lr.load_routing())
        assert routing["judge"] == "azure/o3-mini"

    def test_resolve_llm_for_task_judge(self, monkeypatch):
        self._patch(monkeypatch, {"llm_routing.judge": "azure/o3-mini"}, "")
        provider, model = asyncio.run(lr.resolve_llm_for_task("judge"))
        assert (provider, model) == ("azure", "o3-mini")


# ─── MultiDimJudge resolve via roteamento ───────────────────────────

_JUDGE_JSON = (
    '{"factuality": {"score": 4, "reason": "ok"},'
    ' "completeness": {"score": 5, "reason": "ok"},'
    ' "tone_adherence": {"score": 5, "reason": "ok"},'
    ' "safety": {"score": 1, "reason": "ok"},'
    ' "unsupported_claims": []}'
)


class TestMultiDimJudgeUsesRouting:
    @pytest.mark.asyncio
    async def test_judge_usa_par_do_roteamento(self, monkeypatch):
        import app.verifier.multi_dim_judge as mdj
        captured = {}

        async def fake_resolve(task):
            captured["task"] = task
            return ("maritaca", "sabia-4")
        monkeypatch.setattr("app.llm_routing.resolve_llm_for_task", fake_resolve)

        class _P:
            async def generate(self, messages, **kw):
                return {"content": _JUDGE_JSON, "model": "sabia-4"}

        def fake_get_provider(name, **kw):
            captured["provider"] = name
            captured["model"] = kw.get("model")
            return _P()
        # 24.9.0: o juiz gera via generate_with_hosted_fallback (core) — o
        # factory a mockar vive em app.core.llm_providers.
        monkeypatch.setattr("app.core.llm_providers.get_provider", fake_get_provider)

        out = await mdj.MultiDimJudge().evaluate("draft", [], user_question="q")
        assert captured["task"] == "judge"
        assert (captured["provider"], captured["model"]) == ("maritaca", "sabia-4")
        assert out["dimensions"]["factuality"]["score"] == 4

    @pytest.mark.asyncio
    async def test_falha_no_roteamento_cai_na_env(self, monkeypatch):
        import app.verifier.multi_dim_judge as mdj

        async def boom(task):
            raise RuntimeError("db fora")
        monkeypatch.setattr("app.llm_routing.resolve_llm_for_task", boom)
        monkeypatch.setattr(
            mdj, "get_settings",
            lambda: SimpleNamespace(
                verifier_judge_model="openai_public/gpt-4.1",
                verifier_max_tokens=800,
            ),
        )
        captured = {}

        class _P:
            async def generate(self, messages, **kw):
                return {"content": _JUDGE_JSON}  # sem "model" → usa o id resolvido

        def fake_get_provider(name, **kw):
            captured["provider"] = name
            captured["model"] = kw.get("model")
            return _P()
        monkeypatch.setattr("app.core.llm_providers.get_provider", fake_get_provider)

        out = await mdj.MultiDimJudge().evaluate("draft", [])
        assert (captured["provider"], captured["model"]) == ("openai_public", "gpt-4.1")
        assert out["model"] == "openai_public/gpt-4.1"

    @pytest.mark.asyncio
    async def test_judge_inacessivel_cai_no_fallback_hospedado(self, monkeypatch):
        """24.9.0: juiz roteado (ex.: gpt-oss fora da VPN) inacessível → a
        cadeia do core re-tenta no multimodal_fallback; o judge_model reflete
        quem REALMENTE julgou."""
        import httpx
        import app.verifier.multi_dim_judge as mdj

        async def fake_resolve(task):
            return ("gpt-oss-120b", "openai/gpt-oss-120b")
        monkeypatch.setattr("app.llm_routing.resolve_llm_for_task", fake_resolve)

        async def fake_load_routing():
            return {"multimodal_fallback": "azure/gpt-4o"}
        monkeypatch.setattr("app.llm_routing.load_routing", fake_load_routing)

        class _Down:
            async def generate(self, messages, **kw):
                raise httpx.ConnectError("All connection attempts failed")

        class _Ok:
            async def generate(self, messages, **kw):
                return {"content": _JUDGE_JSON}

        monkeypatch.setattr(
            "app.core.llm_providers.get_provider",
            lambda name, **kw: _Down() if name == "gpt-oss-120b" else _Ok(),
        )

        out = await mdj.MultiDimJudge().evaluate("draft", [], user_question="q")
        assert out["model"] == "azure/gpt-4o"
        assert out["dimensions"]["completeness"]["score"] == 5


# ─── Endpoint + UI ──────────────────────────────────────────────────

class TestEndpointAndUi:
    def test_llm_routing_update_aceita_judge(self):
        from app.routes.dashboard import LLMRoutingUpdate
        assert LLMRoutingUpdate(judge="azure/gpt-4o").judge == "azure/gpt-4o"

    def test_endpoint_expoe_judge_em_task_types_e_descricao(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        async def fake_load_routing():
            return dict(DEFAULT_ROUTING)
        monkeypatch.setattr("app.llm_routing.load_routing", fake_load_routing)
        monkeypatch.setattr("app.llm_routing.global_primary_routing", lambda: None)
        from app.routes.dashboard import router as dashboard_router
        app = FastAPI()
        app.include_router(dashboard_router)
        body = TestClient(app).get("/api/v1/dashboard/llm-routing").json()
        assert "judge" in body["task_types"]
        assert "Juiz" in body["task_descriptions"]["judge"]

    def test_settings_ui_tem_card_llm_como_juiz(self):
        from pathlib import Path
        src = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
        assert "LLM como Juiz" in src
        assert 'x-model="routingForm.judge"' in src
        # aviso anti-autopreferência (juiz == modelo de papel gerador)
        assert "judgeSelfPreference" in src
        # valor herdado do ambiente fora do catálogo não vira select vazio
        assert "judgeUnlistedOption" in src
        # aba Roteamento LLM visível também pro admin: o {% if %} imediatamente
        # anterior ao botão deve incluir root E admin
        idx = src.index("settings-tab-routing")
        last_if = src.rfind("{% if", 0, idx)
        gate = src[last_if:idx]
        assert "root" in gate and "admin" in gate, (
            "botão Roteamento LLM não está gated para root+admin: " + gate[:120]
        )

    def test_save_routing_envia_so_o_delta(self):
        """Regressão do finding da revisão 24.8.0: saveRouting com o form
        INTEIRO congelava papéis herdados (judge da env VERIFIER_JUDGE_MODEL,
        skill_generation do Modelo Primário) como rota explícita em qualquer
        save de outro papel — matando a env em silêncio."""
        from pathlib import Path
        src = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
        assert "Object.entries(this.routingForm).filter(([k, v]) => v !== snap[k])" in src
        assert "api.put('/api/v1/dashboard/llm-routing', delta)" in src

    def test_saude_dos_modelos_tem_label_do_judge(self):
        """O chip 'Saúde dos Modelos' sonda TASK_TYPES — o papel novo precisa
        de label pt-BR (sem entrada, apareceria 'judge' cru entre labels)."""
        from pathlib import Path
        src = Path("app/templates/layouts/base.html").read_text(encoding="utf-8")
        assert "judge:'LLM como Juiz'" in src

    def test_get_defaults_judge_honra_env(self, monkeypatch):
        """`defaults.judge` do GET alimenta o botão 'Aplicar padrões' — deve
        refletir a env VERIFIER_JUDGE_MODEL (default efetivo), senão o reset
        sobrescreveria a env do operador com o hardcoded azure/gpt-4o."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        async def fake_load_routing():
            return dict(DEFAULT_ROUTING)
        monkeypatch.setattr("app.llm_routing.load_routing", fake_load_routing)
        monkeypatch.setattr("app.llm_routing.global_primary_routing", lambda: None)
        monkeypatch.setattr(
            "app.core.config.get_settings",
            lambda: SimpleNamespace(
                verifier_judge_model="maritaca/sabia-4",
                primary_provider="", primary_model="",
            ),
        )
        from app.routes.dashboard import router as dashboard_router
        app = FastAPI()
        app.include_router(dashboard_router)
        body = TestClient(app).get("/api/v1/dashboard/llm-routing").json()
        assert body["defaults"]["judge"] == "maritaca/sabia-4"


class TestPutRoleGate:
    """PUT /llm-routing muda o modelo de TODA a plataforma — gate por role
    REAL no backend (antes era só a aba escondida no template)."""

    @pytest.mark.asyncio
    async def test_role_comum_recebe_403(self):
        from fastapi import HTTPException
        from app.routes.dashboard import put_llm_routing, LLMRoutingUpdate
        with pytest.raises(HTTPException) as ei:
            await put_llm_routing(
                LLMRoutingUpdate(judge="azure/gpt-4o"), user={"role": "comum"}
            )
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", ["root", "admin", "Admin"])
    async def test_root_e_admin_passam(self, monkeypatch, role):
        saved = {}

        async def _fake_save(payload):
            saved["payload"] = payload
            return {**DEFAULT_ROUTING, **payload}

        async def _fake_show():
            return True
        monkeypatch.setattr("app.llm_routing.save_routing", _fake_save)
        monkeypatch.setattr("app.llm_routing.fallback_show_in_trace", _fake_show)
        from app.routes.dashboard import put_llm_routing, LLMRoutingUpdate
        out = await put_llm_routing(
            LLMRoutingUpdate(judge="azure/o3-mini"), user={"role": role}
        )
        assert saved["payload"] == {"judge": "azure/o3-mini"}
        assert "judge" in out["updated"]
