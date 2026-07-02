"""Cadeia de resiliência LLM em RUNTIME (engine.execute_interaction).

Contexto (pedido do operador): no Workspace, quando o modelo do agente não
responde ("⚠ Erro ao chamar LLM (gpt-oss-120b/...): Connection error."), a
plataforma deve tentar, em ordem:
  1) o modelo escolhido para o Agente;
  2) se não responder, o Modelo Primário da plataforma;
  3) se não responder, o Multimodal Fallback (modelo "sempre disponível").

Refinamento posterior: a NOTA visível no painel de Rastreabilidade é
parametrizável por um checkbox (Configurações → Roteamento LLM → Multimodal
Fallback); MAS o fallback é SEMPRE registrado em observabilidade (metadata) e
nos LOGs, independente do checkbox.

Estes testes cobrem as peças isoladas (convenção do projeto: não chamar
execute_interaction inteiro, que é pesado — cf. test_platform_primary_model.py):
- is_llm_unreachable: detector canônico (httpx + openai SDK + cadeia de causas).
- _runtime_llm_candidates: ordem + dedup + pré-filtro de provider sem config.
- _run_llm_chain: orquestração (1ª falha → 2ª; esgotamento; erro não-de-alcance
  propaga).
- fallback_show_in_trace / set_fallback_show_in_trace: persistência do checkbox.
- Endpoints GET/PUT /dashboard/llm-routing: leem/gravam o flag.
- Smoke de template: checkbox em settings.html + nota em workspace.html.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.agents import engine
from app.core.llm_providers import is_llm_unreachable


@pytest.fixture(autouse=True)
def _clear_llm_down_cache():
    """O cache in-process de providers FORA reordena a cadeia — limpar entre
    testes para que a ordem dos candidatos seja determinística."""
    engine._llm_down_at.clear()
    yield
    engine._llm_down_at.clear()


# ─── Fixtures de exceções ──────────────────────────────────────────

def _req() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _openai_conn() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=_req())


def _openai_timeout() -> openai.APITimeoutError:
    return openai.APITimeoutError(request=_req())


def _openai_status(status: int) -> openai.APIStatusError:
    resp = httpx.Response(status, request=_req())
    if status == 404:
        return openai.NotFoundError("model not found", response=resp, body=None)
    if status == 401:
        return openai.AuthenticationError("bad key", response=resp, body=None)
    if status == 429:
        return openai.RateLimitError("slow down", response=resp, body=None)
    raise AssertionError(status)


# ═══════════════════════════════════════════════════════════════════
# is_llm_unreachable — detector canônico
# ═══════════════════════════════════════════════════════════════════
class TestIsLlmUnreachable:
    @pytest.mark.parametrize("exc", [
        httpx.ConnectError("All connection attempts failed"),
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("slow"),
        httpx.PoolTimeout("pool"),
        httpx.TimeoutException("generic"),
    ])
    def test_httpx_e_inacessivel(self, exc):
        assert is_llm_unreachable(exc) is True

    def test_openai_connection_error_e_inacessivel(self):
        # ESTE é o erro real do Workspace (path LangChain → SDK openai).
        exc = _openai_conn()
        assert str(exc) == "Connection error."
        assert is_llm_unreachable(exc) is True

    def test_openai_timeout_e_inacessivel(self):
        assert is_llm_unreachable(_openai_timeout()) is True

    @pytest.mark.parametrize("status", [404, 401, 429])
    def test_openai_status_errors_nao_sao_inacessivel(self, status):
        # 4xx = request/config errado → operador precisa ver, NÃO mascarar com
        # fallback silencioso.
        assert is_llm_unreachable(_openai_status(status)) is False

    def test_url_nao_configurada_e_inacessivel(self):
        exc = RuntimeError("gpt-oss-120b: URL não configurada. Configure em /settings.")
        assert is_llm_unreachable(exc) is True

    def test_nao_configurado_e_inacessivel(self):
        assert is_llm_unreachable(RuntimeError("Azure não configurado")) is True

    def test_string_connection_error_e_inacessivel(self):
        # Caso o tipo escape (wrappers/versões diferentes), match por string.
        assert is_llm_unreachable(Exception("Connection error.")) is True

    def test_cadeia_de_causas_httpx_e_inacessivel(self):
        inner = httpx.ConnectError("boom")
        outer = RuntimeError("LangChain wrapped this")
        outer.__cause__ = inner
        assert is_llm_unreachable(outer) is True

    def test_cadeia_de_causas_openai_e_inacessivel(self):
        outer = RuntimeError("wrapped")
        outer.__cause__ = _openai_conn()
        assert is_llm_unreachable(outer) is True

    def test_cadeia_de_causas_nao_de_alcance_e_false(self):
        outer = RuntimeError("wrapped")
        outer.__cause__ = ValueError("totally different")
        assert is_llm_unreachable(outer) is False

    def test_ciclo_de_causa_nao_trava(self):
        # __cause__ apontando pra si mesmo não deve causar recursão infinita.
        exc = RuntimeError("self")
        exc.__cause__ = exc
        assert is_llm_unreachable(exc) is False

    @pytest.mark.parametrize("exc", [
        ValueError("Provedor 'x' não suportado"),
        KeyError("content"),
        Exception("falha genérica"),
        RuntimeError("sem credencial"),
    ])
    def test_outros_erros_nao_sao_inacessivel(self, exc):
        assert is_llm_unreachable(exc) is False


# ═══════════════════════════════════════════════════════════════════
# _runtime_llm_candidates — ordem + dedup + pré-filtro
# ═══════════════════════════════════════════════════════════════════
def _patch_routing(monkeypatch, fallback="azure/gpt-4o"):
    async def _fake():
        return {"multimodal_fallback": fallback}
    monkeypatch.setattr("app.llm_routing.load_routing", _fake)


def _all_configured(monkeypatch):
    monkeypatch.setattr(engine, "_resolve_provider_config", lambda p, s: ("cfg", None))


class TestRuntimeLlmCandidates:
    @pytest.mark.asyncio
    async def test_ordem_agente_primario_fallback(self, monkeypatch):
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _all_configured(monkeypatch)
        settings = SimpleNamespace(primary_provider="openai_public", primary_model="gpt-4.1")
        agent = {"llm_provider": "gpt-oss-120b", "model": "openai/gpt-oss-120b"}
        out = await engine._runtime_llm_candidates(agent, settings)
        assert out == [
            ("gpt-oss-120b", "openai/gpt-oss-120b"),
            ("openai_public", "gpt-4.1"),
            ("azure", "gpt-4o"),
        ]

    @pytest.mark.asyncio
    async def test_dedup_agente_igual_primario(self, monkeypatch):
        # Cenário do usuário: agente == primário == gpt-oss-120b. Colapsa pra
        # [gpt-oss-120b, fallback] — sem dobrar timeout no mesmo hub morto.
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _all_configured(monkeypatch)
        settings = SimpleNamespace(primary_provider="gpt-oss-120b", primary_model="openai/gpt-oss-120b")
        agent = {"llm_provider": "gpt-oss-120b", "model": "openai/gpt-oss-120b"}
        out = await engine._runtime_llm_candidates(agent, settings)
        assert out == [("gpt-oss-120b", "openai/gpt-oss-120b"), ("azure", "gpt-4o")]

    @pytest.mark.asyncio
    async def test_dedup_case_insensitive(self, monkeypatch):
        _patch_routing(monkeypatch, "Azure/GPT-4o")
        _all_configured(monkeypatch)
        settings = SimpleNamespace(primary_provider="AZURE", primary_model="gpt-4o")
        agent = {"llm_provider": "azure", "model": "gpt-4o"}
        out = await engine._runtime_llm_candidates(agent, settings)
        assert out == [("azure", "gpt-4o")]

    @pytest.mark.asyncio
    async def test_prefiltro_pula_contingencia_sem_config(self, monkeypatch):
        # Fallback aponta pra azure, mas azure não tem key → pré-filtra (não
        # "tenta" um provider que nunca responderia).
        _patch_routing(monkeypatch, "azure/gpt-4o")

        def _resolve(p, s):
            if p == "azure":
                return "", "API Key do Azure OpenAI não está configurada"
            return "cfg", None
        monkeypatch.setattr(engine, "_resolve_provider_config", _resolve)
        settings = SimpleNamespace(primary_provider="openai_public", primary_model="gpt-4.1")
        agent = {"llm_provider": "gpt-oss-120b", "model": "openai/gpt-oss-120b"}
        out = await engine._runtime_llm_candidates(agent, settings)
        assert out == [("gpt-oss-120b", "openai/gpt-oss-120b"), ("openai_public", "gpt-4.1")]

    @pytest.mark.asyncio
    async def test_indice_zero_sempre_admitido_mesmo_sem_config(self, monkeypatch):
        # O candidato do agente (índice 0) JÁ passou pelo gate do caller; entra
        # mesmo que _resolve diga "sem config". Contingências (índice>0) caem.
        _patch_routing(monkeypatch, "azure/gpt-4o")
        monkeypatch.setattr(engine, "_resolve_provider_config", lambda p, s: ("", "missing"))
        settings = SimpleNamespace(primary_provider="openai_public", primary_model="gpt-4.1")
        agent = {"llm_provider": "gpt-oss-120b", "model": "openai/gpt-oss-120b"}
        out = await engine._runtime_llm_candidates(agent, settings)
        assert out == [("gpt-oss-120b", "openai/gpt-oss-120b")]

    @pytest.mark.asyncio
    async def test_load_routing_falha_degrada_sem_fallback(self, monkeypatch):
        async def _boom():
            raise RuntimeError("db offline")
        monkeypatch.setattr("app.llm_routing.load_routing", _boom)
        _all_configured(monkeypatch)
        settings = SimpleNamespace(primary_provider="openai_public", primary_model="gpt-4.1")
        agent = {"llm_provider": "gpt-oss-120b", "model": "openai/gpt-oss-120b"}
        out = await engine._runtime_llm_candidates(agent, settings)
        # sem fallback (routing morto), mas mantém agente + primário
        assert out == [("gpt-oss-120b", "openai/gpt-oss-120b"), ("openai_public", "gpt-4.1")]


# ═══════════════════════════════════════════════════════════════════
# _run_llm_chain — orquestração da cadeia
# ═══════════════════════════════════════════════════════════════════
class _FakeMsg:
    def __init__(self, content):
        self.content = content


class TestRunLlmChain:
    @pytest.mark.asyncio
    async def test_primeiro_responde_nao_tenta_resto(self):
        agent = {}
        calls = []

        async def run_attempt(p, m):
            calls.append((p, m))
            return {"messages": [_FakeMsg("OK")]}

        result, attempted = await engine._run_llm_chain(
            [("gpt-oss-120b", "x"), ("azure", "gpt-4o")], agent, run_attempt, "a1"
        )
        assert result is not None
        assert calls == [("gpt-oss-120b", "x")]
        assert attempted == ["gpt-oss-120b/x"]
        assert agent["llm_provider"] == "gpt-oss-120b" and agent["model"] == "x"

    @pytest.mark.asyncio
    async def test_primeiro_inacessivel_segundo_responde(self):
        agent = {}
        calls = []

        async def run_attempt(p, m):
            calls.append((p, m))
            if len(calls) == 1:
                raise _openai_conn()  # 1ª tentativa: Connection error.
            return {"messages": [_FakeMsg("RECOVERED")]}

        result, attempted = await engine._run_llm_chain(
            [("gpt-oss-120b", "x"), ("azure", "gpt-4o")], agent, run_attempt, "a1"
        )
        assert result is not None
        assert result["messages"][0].content == "RECOVERED"
        assert calls == [("gpt-oss-120b", "x"), ("azure", "gpt-4o")]
        assert attempted == ["gpt-oss-120b/x", "azure/gpt-4o"]
        # agent reflete o candidato que respondeu (pra coleta de tokens)
        assert agent["llm_provider"] == "azure" and agent["model"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_todos_inacessiveis_result_none(self):
        agent = {}

        async def run_attempt(p, m):
            raise httpx.ConnectError("boom")

        result, attempted = await engine._run_llm_chain(
            [("gpt-oss-120b", "x"), ("azure", "gpt-4o")], agent, run_attempt, "a1"
        )
        assert result is None
        assert attempted == ["gpt-oss-120b/x", "azure/gpt-4o"]

    @pytest.mark.asyncio
    async def test_erro_nao_de_alcance_propaga(self):
        # 404 NÃO é "não responde" → propaga (caller mapeia pra mensagem
        # acionável "modelo não encontrado"), NÃO dispara fallback.
        agent = {}
        calls = []

        async def run_attempt(p, m):
            calls.append((p, m))
            raise _openai_status(404)

        with pytest.raises(openai.NotFoundError):
            await engine._run_llm_chain(
                [("gpt-oss-120b", "x"), ("azure", "gpt-4o")], agent, run_attempt, "a1"
            )
        # parou na 1ª — não tentou o fallback (erro de config, não de alcance)
        assert calls == [("gpt-oss-120b", "x")]

    @pytest.mark.asyncio
    async def test_candidato_unico_inacessivel(self):
        agent = {}

        async def run_attempt(p, m):
            raise _openai_conn()

        result, attempted = await engine._run_llm_chain(
            [("gpt-oss-120b", "x")], agent, run_attempt, "a1"
        )
        assert result is None
        assert attempted == ["gpt-oss-120b/x"]


# ═══════════════════════════════════════════════════════════════════
# fallback_show_in_trace / set_fallback_show_in_trace — persistência
# ═══════════════════════════════════════════════════════════════════
class _FakeStore:
    def __init__(self, data=None):
        self.data = dict(data or {})

    async def get(self, key, default=""):
        return self.data.get(key, default)

    async def set(self, key, value):
        self.data[key] = value


class TestFallbackShowInTraceSetting:
    @pytest.mark.asyncio
    async def test_default_true_quando_ausente(self, monkeypatch):
        monkeypatch.setattr("app.core.database.settings_store", _FakeStore())
        from app import llm_routing
        assert await llm_routing.fallback_show_in_trace() is True

    @pytest.mark.asyncio
    async def test_set_false_persiste_e_le_false(self, monkeypatch):
        store = _FakeStore()
        monkeypatch.setattr("app.core.database.settings_store", store)
        from app import llm_routing
        ret = await llm_routing.set_fallback_show_in_trace(False)
        assert ret is False
        assert store.data["llm_fallback.show_in_trace"] == "false"
        assert await llm_routing.fallback_show_in_trace() is False

    @pytest.mark.asyncio
    async def test_set_true_persiste_e_le_true(self, monkeypatch):
        store = _FakeStore({"llm_fallback.show_in_trace": "false"})
        monkeypatch.setattr("app.core.database.settings_store", store)
        from app import llm_routing
        await llm_routing.set_fallback_show_in_trace(True)
        assert store.data["llm_fallback.show_in_trace"] == "true"
        assert await llm_routing.fallback_show_in_trace() is True

    @pytest.mark.asyncio
    async def test_leitura_falha_degrada_para_default_true(self, monkeypatch):
        class _Boom:
            async def get(self, *a, **k):
                raise RuntimeError("db offline")
        monkeypatch.setattr("app.core.database.settings_store", _Boom())
        from app import llm_routing
        assert await llm_routing.fallback_show_in_trace() is True

    @pytest.mark.parametrize("raw,expected", [
        ("true", True), ("1", True), ("yes", True), ("on", True), ("TRUE", True),
        ("false", False), ("0", False), ("no", False), ("off", False), ("FALSE", False),
        ("", True), ("garbage", True),  # default True
    ])
    def test_coerce_bool(self, raw, expected):
        from app.llm_routing import _coerce_bool
        assert _coerce_bool(raw, True) is expected


# ═══════════════════════════════════════════════════════════════════
# Endpoints GET/PUT /dashboard/llm-routing — leem/gravam o flag
# ═══════════════════════════════════════════════════════════════════
class TestDashboardEndpoints:
    @pytest.mark.asyncio
    async def test_get_retorna_show_in_trace(self, monkeypatch):
        async def _fake_load():
            return {"tool_calling": "a/b", "multimodal_fallback": "azure/gpt-4o"}

        async def _fake_show():
            return False
        monkeypatch.setattr("app.llm_routing.load_routing", _fake_load)
        monkeypatch.setattr("app.llm_routing.fallback_show_in_trace", _fake_show)
        monkeypatch.setattr("app.llm_routing.global_primary_routing", lambda: None)
        from app.routes.dashboard import get_llm_routing
        out = await get_llm_routing()
        assert out["fallback_show_in_trace"] is False
        assert "routing" in out and "defaults" in out

    @pytest.mark.asyncio
    async def test_put_persiste_show_in_trace(self, monkeypatch):
        saved = {}

        async def _fake_save(payload):
            return {"tool_calling": "a/b"}

        async def _fake_set(v):
            saved["v"] = v
            return v

        async def _fake_show():
            return saved.get("v", True)
        monkeypatch.setattr("app.llm_routing.save_routing", _fake_save)
        monkeypatch.setattr("app.llm_routing.set_fallback_show_in_trace", _fake_set)
        monkeypatch.setattr("app.llm_routing.fallback_show_in_trace", _fake_show)
        from app.routes.dashboard import put_llm_routing, LLMRoutingUpdate
        out = await put_llm_routing(LLMRoutingUpdate(fallback_show_in_trace=False))
        assert saved["v"] is False
        assert "fallback_show_in_trace" in out["updated"]
        assert out["fallback_show_in_trace"] is False

    @pytest.mark.asyncio
    async def test_put_so_o_flag_nao_da_400(self, monkeypatch):
        # Mudar SÓ o checkbox (sem tocar roteamento) é update válido.
        async def _fake_save(payload):
            return {}

        async def _fake_set(v):
            return v

        async def _fake_show():
            return True
        monkeypatch.setattr("app.llm_routing.save_routing", _fake_save)
        monkeypatch.setattr("app.llm_routing.set_fallback_show_in_trace", _fake_set)
        monkeypatch.setattr("app.llm_routing.fallback_show_in_trace", _fake_show)
        from app.routes.dashboard import put_llm_routing, LLMRoutingUpdate
        out = await put_llm_routing(LLMRoutingUpdate(fallback_show_in_trace=True))
        assert out["updated"] == ["fallback_show_in_trace"]

    @pytest.mark.asyncio
    async def test_put_vazio_da_400(self, monkeypatch):
        from app.routes.dashboard import put_llm_routing, LLMRoutingUpdate
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await put_llm_routing(LLMRoutingUpdate())
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_put_roteamento_e_flag_juntos(self, monkeypatch):
        saved = {}

        async def _fake_save(payload):
            saved["routing_payload"] = payload
            return {"tool_calling": "azure/gpt-4o"}

        async def _fake_set(v):
            saved["v"] = v
            return v

        async def _fake_show():
            return saved.get("v", True)
        monkeypatch.setattr("app.llm_routing.save_routing", _fake_save)
        monkeypatch.setattr("app.llm_routing.set_fallback_show_in_trace", _fake_set)
        monkeypatch.setattr("app.llm_routing.fallback_show_in_trace", _fake_show)
        from app.routes.dashboard import put_llm_routing, LLMRoutingUpdate
        out = await put_llm_routing(
            LLMRoutingUpdate(tool_calling="azure/gpt-4o", fallback_show_in_trace=False)
        )
        assert saved["routing_payload"] == {"tool_calling": "azure/gpt-4o"}
        assert saved["v"] is False
        assert set(out["updated"]) == {"tool_calling", "fallback_show_in_trace"}


# ═══════════════════════════════════════════════════════════════════
# LLMRoutingUpdate schema
# ═══════════════════════════════════════════════════════════════════
class TestLLMRoutingUpdateSchema:
    def test_aceita_fallback_show_in_trace_bool(self):
        from app.routes.dashboard import LLMRoutingUpdate
        u = LLMRoutingUpdate(fallback_show_in_trace=False)
        assert u.fallback_show_in_trace is False

    def test_default_none(self):
        from app.routes.dashboard import LLMRoutingUpdate
        u = LLMRoutingUpdate()
        assert u.fallback_show_in_trace is None


# ═══════════════════════════════════════════════════════════════════
# Smoke de template — checkbox + nota (red/rose/amber palette OK)
# ═══════════════════════════════════════════════════════════════════
class TestTemplateSmoke:
    def test_settings_tem_checkbox_show_in_trace(self):
        content = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
        assert "routingForm.fallback_show_in_trace" in content
        assert "Mostrar contingência na rastreabilidade" in content
        # checkbox dentro do bloco Multimodal Fallback (após o select)
        assert 'type="checkbox"' in content

    def test_settings_state_e_load_save_incluem_o_flag(self):
        content = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
        # estado inicial + load do GET + reset dos defaults
        assert "fallback_show_in_trace:true" in content.replace(" ", "")
        assert "r.fallback_show_in_trace" in content

    def test_workspace_tem_nota_de_contingencia(self):
        content = Path("app/templates/pages/workspace.html").read_text(encoding="utf-8")
        # gate pela flag show_in_trace
        assert "llm_fallback?.show_in_trace" in content
        assert "Resposta por contingência" in content
        assert "Todos os modelos indisponíveis" in content
        assert "Cadeia tentada" in content

    def test_templates_sem_roxo_nas_areas_novas(self):
        # Guard local: minhas adições não introduzem violet/fuchsia/purple.
        import re
        pat = re.compile(r"\b(violet|fuchsia|purple)-\d")
        for f in ("app/templates/pages/settings.html", "app/templates/pages/workspace.html"):
            assert not pat.search(Path(f).read_text(encoding="utf-8")), f"roxo em {f}"
