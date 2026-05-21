"""Testes do suporte plataforma a gpt-oss + Qwen3 embedding (Onda 4 plataforma).

Cobre:
- GPTOSSProvider (size 20b/120b) — URL/key/model corretas vs settings
- Factory get_provider() aceitando 'gpt-oss-20b' e 'gpt-oss-120b'
- Pricing entries para os 2 modelos (free tier = 0)
- _qwen3_base_url — extrai scheme://host do OSS_URL + concatena path
- _build_qwen3_embedder — guard quando OSS source não configurado
- _build_embedder — seletor Azure | Qwen3 baseado em settings
"""

from __future__ import annotations

import pytest

from app.core import config as _config
from app.core.llm_providers import GPTOSSProvider, get_provider
from app.core.llm_pricing import compute_cost, get_pricing
from app.evidence import embedder as _embedder
from app.evidence.embedder import _qwen3_base_url, Qwen3Embedder


@pytest.fixture
def fresh_settings(monkeypatch):
    """Limpa lru_cache de get_settings — tests podem patchar Settings fields
    e ainda assim get_settings() retorna instância nova."""
    _config.get_settings.cache_clear()
    yield
    _config.get_settings.cache_clear()


# ═════════════════════════════════════════════════════════════════
# GPTOSSProvider — instanciação direta
# ═════════════════════════════════════════════════════════════════


class TestGPTOSSProvider:
    def test_120b_le_settings_corretas(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("OSS120B_URL", "https://hub-gpus.claro.com.br/gpt120/v1")
        monkeypatch.setenv("OSS120B_MODEL", "openai/gpt-oss-120b")
        monkeypatch.setenv("OSS120B_API_KEY", "secret-120")
        p = GPTOSSProvider(size="120b")
        assert p.api_url == "https://hub-gpus.claro.com.br/gpt120/v1"
        assert p.api_key == "secret-120"
        assert p.model == "openai/gpt-oss-120b"
        assert p.size == "120b"

    def test_20b_le_settings_corretas(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("OSS20B_URL", "https://hub-gpus.claro.com.br/gpt20/v1")
        monkeypatch.setenv("OSS20B_API_KEY", "not-needed")
        p = GPTOSSProvider(size="20b")
        assert p.api_url == "https://hub-gpus.claro.com.br/gpt20/v1"
        assert p.api_key == "not-needed"
        assert p.size == "20b"

    def test_size_invalido_raise(self):
        with pytest.raises(ValueError, match="size"):
            GPTOSSProvider(size="13b")

    def test_url_vazia_get_langchain_retorna_none(self, fresh_settings):
        # Sem env vars setadas, oss120b_url default = ""
        p = GPTOSSProvider(size="120b")
        assert p.api_url == ""
        assert p.get_langchain_llm() is None

    @pytest.mark.asyncio
    async def test_url_vazia_generate_raise(self, fresh_settings):
        p = GPTOSSProvider(size="120b")
        with pytest.raises(RuntimeError, match="URL não configurada"):
            await p.generate([{"role": "user", "content": "hi"}])

    def test_api_key_default_not_needed(self, monkeypatch, fresh_settings):
        # API key vazia → "not-needed" (válido para proxy interno)
        monkeypatch.setenv("OSS120B_URL", "https://x.com/v1")
        monkeypatch.setenv("OSS120B_API_KEY", "")
        p = GPTOSSProvider(size="120b")
        assert p.api_key == "not-needed"

    def test_trailing_slash_da_url_normalizado(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("OSS120B_URL", "https://x.com/v1/")
        p = GPTOSSProvider(size="120b")
        assert p.api_url == "https://x.com/v1"  # sem trailing


# ═════════════════════════════════════════════════════════════════
# Factory get_provider()
# ═════════════════════════════════════════════════════════════════


class TestGetProvider:
    def test_factory_gpt_oss_20b(self):
        p = get_provider("gpt-oss-20b")
        assert isinstance(p, GPTOSSProvider)
        assert p.size == "20b"

    def test_factory_gpt_oss_120b(self):
        p = get_provider("gpt-oss-120b")
        assert isinstance(p, GPTOSSProvider)
        assert p.size == "120b"

    def test_factory_invalido_raise(self):
        with pytest.raises(ValueError, match="não suportado"):
            get_provider("gpt-oss-1tb")  # não existe

    def test_factory_existentes_intactos(self):
        # Regressão: providers antigos continuam funcionando
        from app.core.llm_providers import AzureOpenAIProvider, MaritacaProvider, OllamaProvider
        assert isinstance(get_provider("azure"), AzureOpenAIProvider)
        assert isinstance(get_provider("openai"), AzureOpenAIProvider)  # alias
        assert isinstance(get_provider("maritaca"), MaritacaProvider)
        assert isinstance(get_provider("ollama"), OllamaProvider)


# ═════════════════════════════════════════════════════════════════
# Pricing entries
# ═════════════════════════════════════════════════════════════════


class TestPricingGPTOSS:
    def test_pricing_120b_entry(self):
        # Endpoint interno = custo zero (ajustável se virar chargeback)
        cost = compute_cost("gpt-oss-120b", "openai/gpt-oss-120b", 1000, 500)
        assert cost == 0.0
        # Sem warning de "modelo desconhecido"
        p = get_pricing("gpt-oss-120b", "openai/gpt-oss-120b")
        assert p is not None

    def test_pricing_20b_entry(self):
        cost = compute_cost("gpt-oss-20b", "openai/gpt-oss-20b", 1000, 500)
        assert cost == 0.0
        assert get_pricing("gpt-oss-20b", "openai/gpt-oss-20b") is not None

    def test_pricing_alias_sem_prefixo_openai(self):
        # Caso UI armazene 'gpt-oss-20b' sem o prefixo 'openai/'
        assert get_pricing("gpt-oss-20b", "gpt-oss-20b") is not None
        assert get_pricing("gpt-oss-120b", "gpt-oss-120b") is not None


# ═════════════════════════════════════════════════════════════════
# Qwen3 embedder
# ═════════════════════════════════════════════════════════════════


class TestQwen3BaseURL:
    def test_extrai_scheme_e_host_do_oss(self):
        url = _qwen3_base_url("https://hub-gpus.claro.com.br/gpt120/v1", "qwen3/v1")
        assert url == "https://hub-gpus.claro.com.br/qwen3/v1"

    def test_path_normaliza_slashes(self):
        url = _qwen3_base_url("https://x.com/foo/v1", "/qwen3/v1/")
        assert url == "https://x.com/qwen3/v1"

    def test_url_vazia_retorna_string_vazia(self):
        assert _qwen3_base_url("", "qwen3/v1") == ""

    def test_url_invalida_retorna_string_vazia(self):
        # Sem scheme
        assert _qwen3_base_url("not-a-url", "qwen3/v1") == ""

    def test_qwen3_path_absoluto_usa_direto(self):
        """Se qwen3_path já é URL absoluta (operador colou a URL completa do
        hub), usa direto e ignora oss_url. Cobre o caso real reportado em prod:
        operador colava 'https://hub-gpus.claro.com.br/embed06b/v1' no campo
        Path e o backend montava 'https://<oss_host>/https://hub.../...'."""
        # Mesmo com oss_url presente, URL absoluta prevalece
        url = _qwen3_base_url(
            "https://hub-gpus.claro.com.br/gpt120/v1",
            "https://hub-gpus.claro.com.br/embed06b/v1",
        )
        assert url == "https://hub-gpus.claro.com.br/embed06b/v1"

    def test_qwen3_path_absoluto_funciona_sem_oss_url(self):
        """Path absoluto não depende do oss_url estar configurado."""
        url = _qwen3_base_url("", "https://hub-gpus.claro.com.br/embed06b/v1")
        assert url == "https://hub-gpus.claro.com.br/embed06b/v1"

    def test_qwen3_path_absoluto_normaliza_trailing_slash(self):
        url = _qwen3_base_url("", "https://hub.com/embed06b/v1/")
        assert url == "https://hub.com/embed06b/v1"

    def test_qwen3_path_http_tambem_aceito(self):
        """http:// também é aceito (não só https)."""
        url = _qwen3_base_url("", "http://internal-hub/embed/v1")
        assert url == "http://internal-hub/embed/v1"


class TestBuildQwen3Embedder:
    def test_oss_source_vazio_retorna_none(self, fresh_settings, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_SOURCE", "oss120b")
        monkeypatch.setenv("OSS120B_URL", "")
        from app.evidence.embedder import _build_qwen3_embedder
        assert _build_qwen3_embedder() is None

    def test_oss_120b_source_constroi_embedder(self, fresh_settings, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_SOURCE", "oss120b")
        monkeypatch.setenv("OSS120B_URL", "https://hub.com/gpt120/v1")
        monkeypatch.setenv("OSS120B_API_KEY", "secret")
        monkeypatch.setenv("QWEN3_PATH", "qwen3/v1")
        monkeypatch.setenv("QWEN3_MODEL", "Qwen/Qwen3-Embedding-0.6B")
        from app.evidence.embedder import _build_qwen3_embedder
        emb = _build_qwen3_embedder()
        assert emb is not None
        assert emb.base_url == "https://hub.com/qwen3/v1"
        assert emb.api_key == "secret"
        assert emb.model == "Qwen/Qwen3-Embedding-0.6B"

    def test_oss_20b_source_usa_url_do_20b(self, fresh_settings, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_SOURCE", "oss20b")
        monkeypatch.setenv("OSS20B_URL", "https://hub.com/gpt20/v1")
        monkeypatch.setenv("OSS20B_API_KEY", "key-20")
        # Path explícito — não depender do default global (que mudou para
        # 'embed06b/v1'). Mantém o teste estável a futuras mudanças de default.
        monkeypatch.setenv("QWEN3_PATH", "qwen3/v1")
        from app.evidence.embedder import _build_qwen3_embedder
        emb = _build_qwen3_embedder()
        assert emb is not None
        assert emb.base_url == "https://hub.com/qwen3/v1"
        assert emb.api_key == "key-20"

    def test_dimensions_propaga_do_settings(self, fresh_settings, monkeypatch):
        """qwen3_dimensions do settings chega como dimensions no Qwen3Embedder."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_SOURCE", "oss120b")
        monkeypatch.setenv("OSS120B_URL", "https://hub.com/gpt120/v1")
        monkeypatch.setenv("QWEN3_PATH", "qwen3/v1")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "768")
        from app.evidence.embedder import _build_qwen3_embedder
        emb = _build_qwen3_embedder()
        assert emb is not None
        assert emb.dimensions == 768

    def test_dimensions_zero_vira_none(self, fresh_settings, monkeypatch):
        """qwen3_dimensions=0 (default) é normalizado para None — não envia parâmetro."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_SOURCE", "oss120b")
        monkeypatch.setenv("OSS120B_URL", "https://hub.com/gpt120/v1")
        monkeypatch.setenv("QWEN3_PATH", "qwen3/v1")
        # Sem QWEN3_DIMENSIONS no env → usa default 0
        from app.evidence.embedder import _build_qwen3_embedder
        emb = _build_qwen3_embedder()
        assert emb is not None
        assert emb.dimensions is None


class TestSelectEmbedder:
    def test_default_seleciona_azure(self, fresh_settings, monkeypatch):
        # embedding_provider default = "azure" (sem env override)
        monkeypatch.setenv("EMBEDDING_PROVIDER", "azure")
        # Sem Azure configurado → retorna None mas o caminho foi tentado
        # Aqui só validamos que NÃO entra em qwen3
        monkeypatch.setattr(_embedder, "_build_qwen3_embedder", lambda: "QWEN3_INSTANCE")
        monkeypatch.setattr(_embedder, "_build_azure_embedder", lambda: "AZURE_INSTANCE")
        # Reset singleton para forçar rebuild
        _embedder._embedder = None
        result = _embedder._build_embedder()
        assert result == "AZURE_INSTANCE"

    def test_qwen3_setting_seleciona_qwen3(self, fresh_settings, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setattr(_embedder, "_build_qwen3_embedder", lambda: "QWEN3_INSTANCE")
        monkeypatch.setattr(_embedder, "_build_azure_embedder", lambda: "AZURE_INSTANCE")
        _embedder._embedder = None
        result = _embedder._build_embedder()
        assert result == "QWEN3_INSTANCE"


class TestQwen3EmbedderHTTP:
    @pytest.mark.asyncio
    async def test_aembed_documents_chama_endpoint_correto(self, monkeypatch):
        captured = {}

        class FakeResp:
            status_code = 200
            def json(self): return {"data": [
                {"embedding": [0.1, 0.2], "index": 0},
                {"embedding": [0.3, 0.4], "index": 1},
            ]}

        class FakeClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def post(self, url, **kw):
                captured["url"] = url
                captured["json"] = kw.get("json")
                captured["headers"] = kw.get("headers")
                return FakeResp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        emb = Qwen3Embedder(
            base_url="https://hub.com/qwen3/v1",
            api_key="not-needed",
            model="Qwen/Qwen3-Embedding-0.6B",
        )
        out = await emb.aembed_documents(["foo", "bar"])

        assert captured["url"] == "https://hub.com/qwen3/v1/embeddings"
        assert captured["json"]["model"] == "Qwen/Qwen3-Embedding-0.6B"
        assert captured["json"]["input"] == ["foo", "bar"]
        assert captured["headers"]["Authorization"] == "Bearer not-needed"
        # Sem dimensions explícito → parâmetro NÃO vai no payload (usa default do modelo)
        assert "dimensions" not in captured["json"]
        assert out == [[0.1, 0.2], [0.3, 0.4]]

    @pytest.mark.asyncio
    async def test_dimensions_setado_vai_no_payload(self, monkeypatch):
        """Quando o operador escolhe densidade no UI (config.qwen3_dimensions > 0),
        o Qwen3Embedder inclui 'dimensions' no payload do POST /embeddings, ativando
        o truncamento server-side (Matryoshka)."""
        captured = {}

        class FakeResp:
            status_code = 200
            def json(self):
                return {"data": [{"embedding": [0.1] * 768, "index": 0}]}

        class FakeClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def post(self, url, **kw):
                captured["json"] = kw.get("json")
                return FakeResp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        emb = Qwen3Embedder(
            base_url="https://hub.com/qwen3/v1",
            api_key="not-needed",
            model="Qwen/Qwen3-Embedding-0.6B",
            dimensions=768,
        )
        await emb.aembed_query("ping")
        assert captured["json"]["dimensions"] == 768

    @pytest.mark.asyncio
    async def test_dimensions_zero_nao_vai_no_payload(self, monkeypatch):
        """dimensions=0 (sentinel 'usa default do modelo') NUNCA aparece no payload.
        Defesa: hub pode rejeitar dimensions=0 com 400."""
        captured = {}

        class FakeResp:
            status_code = 200
            def json(self): return {"data": [{"embedding": [0.1], "index": 0}]}

        class FakeClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def post(self, url, **kw):
                captured["json"] = kw.get("json")
                return FakeResp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        emb = Qwen3Embedder(
            base_url="https://hub.com/qwen3/v1",
            api_key="not-needed",
            model="Qwen/Qwen3-Embedding-0.6B",
            dimensions=0,
        )
        await emb.aembed_query("ping")
        assert "dimensions" not in captured["json"]

    @pytest.mark.asyncio
    async def test_aembed_query_single(self, monkeypatch):
        class FakeResp:
            status_code = 200
            def json(self): return {"data": [{"embedding": [0.5, 0.6], "index": 0}]}

        class FakeClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def post(self, *a, **kw): return FakeResp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        emb = Qwen3Embedder(base_url="https://x.com/v1", api_key="k", model="m")
        out = await emb.aembed_query("foo")
        assert out == [0.5, 0.6]

    @pytest.mark.asyncio
    async def test_http_4xx_levanta(self, monkeypatch):
        class FakeResp:
            status_code = 500
            text = "internal error"
            def json(self): return {}

        class FakeClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def post(self, *a, **kw): return FakeResp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        emb = Qwen3Embedder(base_url="https://x.com/v1", api_key="k", model="m")
        with pytest.raises(RuntimeError, match="qwen3 HTTP 500"):
            await emb.aembed_query("foo")


# ═════════════════════════════════════════════════════════════════
# Endpoint /settings — schema com novos campos
# ═════════════════════════════════════════════════════════════════


def test_settings_save_aceita_novos_campos():
    """Sanity: SettingsSave Pydantic aceita os 11 campos novos sem erro."""
    from app.routes.dashboard import SettingsSave
    s = SettingsSave(
        oss120b_url="https://x.com/v1",
        oss120b_model="openai/gpt-oss-120b",
        oss120b_api_key="not-needed",
        oss20b_url="https://x.com/v1",
        oss20b_model="openai/gpt-oss-20b",
        oss20b_api_key="not-needed",
        llm_timeout_seconds=300,
        embedding_provider="qwen3",
        qwen3_source="oss120b",
        qwen3_path="qwen3/v1",
        qwen3_model="Qwen/Qwen3-Embedding-0.6B",
    )
    assert s.embedding_provider == "qwen3"
    assert s.qwen3_source == "oss120b"
    assert s.llm_timeout_seconds == 300
