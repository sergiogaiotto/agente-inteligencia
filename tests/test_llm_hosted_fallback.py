"""generate_with_hosted_fallback — cadeia NEUTRA de resiliência do core (24.9.0).

Para módulos que não podem depender de app/routes (verifier/juiz): gera com o
par dado; INACESSÍVEL (rede/URL/timeout) ou 401 → re-tenta UMA vez no
`multimodal_fallback` do Roteamento LLM, desde que provider DIFERENTE.
Também cobre o detector canônico is_llm_auth_error (movido do wizard) e o
de-hardcode do _LegacyVerifier (get_provider("openai") → papel judge).
"""
from __future__ import annotations

import httpx
import pytest

from app.core import llm_providers as lp

_MSGS = [{"role": "user", "content": "oi"}]


def _patch_routing(monkeypatch, target: str = "azure/gpt-4o"):
    async def _fake_load_routing():
        return {"multimodal_fallback": target}
    monkeypatch.setattr("app.llm_routing.load_routing", _fake_load_routing)


class _Provider:
    def __init__(self, behavior, content="OK"):
        self.behavior = behavior
        self.content = content
        self.model = "modelo-default"
        self.gen_calls: list[dict] = []

    async def generate(self, messages, **kw):
        self.gen_calls.append(kw)
        if self.behavior == "connect":
            raise httpx.ConnectError("All connection attempts failed")
        if self.behavior == "auth":
            raise Exception("Error code: 401 - Incorrect API key provided")
        if self.behavior == "boom":
            raise RuntimeError("erro qualquer nao-de-alcance")
        return {"content": self.content, "model": self.content}


def _patch_providers(monkeypatch, behaviors: dict, captured: list | None = None,
                     instances: dict | None = None):
    def _fake(name, **kw):
        if captured is not None:
            captured.append((name, kw))
        p = _Provider(*behaviors[name]) if isinstance(behaviors[name], tuple) \
            else _Provider(behaviors[name])
        if instances is not None:
            instances[name] = p
        return p
    monkeypatch.setattr(lp, "get_provider", _fake)


class TestGenerateWithHostedFallback:
    @pytest.mark.asyncio
    async def test_primario_ok_nao_toca_fallback(self, monkeypatch):
        captured: list = []
        _patch_providers(monkeypatch, {"gpt-oss-120b": "ok"}, captured)
        resp, p, m = await lp.generate_with_hosted_fallback(
            _MSGS, "gpt-oss-120b", "openai/gpt-oss-120b", purpose="teste"
        )
        assert resp["content"] == "OK"
        assert (p, m) == ("gpt-oss-120b", "openai/gpt-oss-120b")
        assert [(n, kw.get("model")) for n, kw in captured] == [
            ("gpt-oss-120b", "openai/gpt-oss-120b")
        ]

    @pytest.mark.asyncio
    async def test_connect_error_cai_no_fallback(self, monkeypatch, caplog):
        import logging
        _patch_routing(monkeypatch)
        captured: list = []
        _patch_providers(
            monkeypatch, {"gpt-oss-120b": "connect", "azure": "ok"}, captured
        )
        with caplog.at_level(logging.WARNING, logger="app.core.llm_providers"):
            resp, p, m = await lp.generate_with_hosted_fallback(
                _MSGS, "gpt-oss-120b", "openai/gpt-oss-120b", purpose="teste"
            )
        assert (p, m) == ("azure", "gpt-4o")
        assert captured[-1][0] == "azure"
        # evento estruturado da contingência (convenção event= do repo)
        rec = next(r for r in caplog.records if getattr(r, "event", "") == "llm.fallback.hosted")
        assert rec.purpose == "teste"
        assert rec.failed_provider == "gpt-oss-120b"
        assert rec.failed_reason == "unreachable"
        assert rec.fallback_provider == "azure"

    @pytest.mark.asyncio
    async def test_kwargs_propagam_ao_primario_e_ao_fallback(self, monkeypatch):
        _patch_routing(monkeypatch)
        captured: list = []
        instances: dict = {}
        _patch_providers(
            monkeypatch, {"gpt-oss-120b": "connect", "azure": "ok"},
            captured, instances,
        )
        await lp.generate_with_hosted_fallback(
            _MSGS, "gpt-oss-120b", "m", purpose="teste",
            prov_kwargs={"temperature": 0.1},
            gen_kwargs={"max_tokens": 800},
        )
        # prov_kwargs no construtor das DUAS tentativas
        assert captured[0][1].get("temperature") == 0.1
        assert captured[1][1].get("temperature") == 0.1
        # gen_kwargs no generate das DUAS tentativas
        assert instances["gpt-oss-120b"].gen_calls[0].get("max_tokens") == 800
        assert instances["azure"].gen_calls[0].get("max_tokens") == 800

    @pytest.mark.asyncio
    async def test_fallback_tambem_falha_propaga_excecao_do_fallback(self, monkeypatch):
        # contrato da docstring: "se o fallback também falhar, propaga a dele"
        # (tipos distintos provam qual exceção saiu: primário=ConnectError,
        # fallback=RuntimeError)
        _patch_routing(monkeypatch)
        _patch_providers(monkeypatch, {"gpt-oss-120b": "connect", "azure": "boom"})
        with pytest.raises(RuntimeError):
            await lp.generate_with_hosted_fallback(
                _MSGS, "gpt-oss-120b", "m", purpose="teste"
            )

    @pytest.mark.asyncio
    async def test_alias_openai_azure_nao_re_tenta_o_mesmo_backend(self, monkeypatch):
        # "openai" é alias de "azure" no get_provider — fallback openai→azure
        # seria o MESMO backend com a MESMA chave; re-raise do primário.
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {"openai": "connect"})
        with pytest.raises(httpx.ConnectError):
            await lp.generate_with_hosted_fallback(
                _MSGS, "openai", "gpt-4o", purpose="teste"
            )

    @pytest.mark.asyncio
    async def test_401_tambem_cai_no_fallback(self, monkeypatch):
        _patch_routing(monkeypatch)
        _patch_providers(monkeypatch, {"openai_public": "auth", "azure": "ok"})
        resp, p, m = await lp.generate_with_hosted_fallback(
            _MSGS, "openai_public", "gpt-4.1", purpose="teste"
        )
        assert p == "azure"

    @pytest.mark.asyncio
    async def test_erro_nao_de_alcance_propaga_sem_fallback(self, monkeypatch):
        _patch_routing(monkeypatch)
        _patch_providers(monkeypatch, {"gpt-oss-120b": "boom"})
        with pytest.raises(RuntimeError):
            await lp.generate_with_hosted_fallback(
                _MSGS, "gpt-oss-120b", "m", purpose="teste"
            )

    @pytest.mark.asyncio
    async def test_fallback_mesmo_provider_re_raise_do_primario(self, monkeypatch):
        # multimodal_fallback aponta pro MESMO provider que falhou → sem
        # alternativa real; re-raise (não dobra o timeout no mesmo hub).
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {"azure": "connect"})
        with pytest.raises(httpx.ConnectError):
            await lp.generate_with_hosted_fallback(
                _MSGS, "azure", "gpt-4o", purpose="teste"
            )

    @pytest.mark.asyncio
    async def test_sem_multimodal_fallback_re_raise(self, monkeypatch):
        _patch_routing(monkeypatch, "")
        _patch_providers(monkeypatch, {"gpt-oss-120b": "connect"})
        with pytest.raises(httpx.ConnectError):
            await lp.generate_with_hosted_fallback(
                _MSGS, "gpt-oss-120b", "m", purpose="teste"
            )

    @pytest.mark.asyncio
    async def test_routing_fora_re_raise_do_primario(self, monkeypatch):
        async def _boom():
            raise RuntimeError("db fora")
        monkeypatch.setattr("app.llm_routing.load_routing", _boom)
        _patch_providers(monkeypatch, {"gpt-oss-120b": "connect"})
        with pytest.raises(httpx.ConnectError):
            await lp.generate_with_hosted_fallback(
                _MSGS, "gpt-oss-120b", "m", purpose="teste"
            )


class TestMaritacaSemKey:
    """Gap achado no smoke ao vivo (2026-07-04): Maritaca com key vazia
    mandava 'Authorization: Bearer ' → httpx LocalProtocolError 'Illegal
    header value', que NÃO era classificado como inalcançável → escapava de
    TODAS as cadeias de fallback. Guard no provider + detector cobre ambos
    os gêneros ('não configurado'/'não configurada')."""

    @pytest.mark.asyncio
    async def test_maritaca_sem_key_levanta_nao_configurada(self, monkeypatch):
        from types import SimpleNamespace
        monkeypatch.setattr(
            lp, "get_settings",
            lambda: SimpleNamespace(
                maritaca_model="sabia-4", maritaca_api_key="",
                maritaca_api_url="https://chat.maritaca.ai/api",
            ),
        )
        p = lp.MaritacaProvider()
        with pytest.raises(RuntimeError) as ei:
            await p.generate(_MSGS)
        assert "não configurada" in str(ei.value)
        # o detector canônico classifica como inalcançável → fallback dispara
        assert lp.is_llm_unreachable(ei.value) is True

    def test_detector_cobre_ambos_os_generos(self):
        assert lp.is_llm_unreachable(RuntimeError("API key não configurada")) is True
        assert lp.is_llm_unreachable(RuntimeError("provider não configurado")) is True


class TestIsLlmAuthErrorCanonico:
    @pytest.mark.parametrize("exc", [
        Exception("Error code: 401 - Incorrect API key provided: sk-x"),
        Exception("invalid_api_key"),
        Exception("HTTP 401 Unauthorized"),
        type("AuthenticationError", (Exception,), {})("bad key"),
    ])
    def test_detecta_auth(self, exc):
        assert lp.is_llm_auth_error(exc) is True

    def test_parse_openai_compatible_anexa_status_code(self):
        """401 dos providers httpx-diretos com corpo SEM as frases mágicas
        ('api key' etc.): o status_code anexado à exceção garante a detecção
        pelo fast-path getattr do is_llm_auth_error."""
        class _Resp:
            status_code = 401
            text = "{}"
            def json(self):
                return {"error": {"message": "credencial recusada pelo gateway"}}

        with pytest.raises(RuntimeError) as ei:
            lp._parse_openai_compatible_response(_Resp(), provider="gpt-oss-120b", model="m")
        assert getattr(ei.value, "status_code", None) == 401
        assert lp.is_llm_auth_error(ei.value) is True

    @pytest.mark.parametrize("exc", [
        httpx.ConnectError("All connection attempts failed"),
        RuntimeError("URL não configurada"),
        Exception("falha genérica 500"),
    ])
    def test_nao_auth(self, exc):
        assert lp.is_llm_auth_error(exc) is False

    def test_wrapper_do_wizard_delega_pro_canonico(self):
        from app.routes import wizard
        assert wizard._is_llm_auth_error(Exception("invalid_api_key")) is True
        assert wizard._is_llm_auth_error(RuntimeError("outro erro")) is False


class TestLegacyVerifierDeHardcode:
    @pytest.mark.asyncio
    async def test_legacy_usa_papel_judge_do_roteamento(self, monkeypatch):
        from app.verifier.runtime import _LegacyVerifier

        async def fake_resolve(task):
            assert task == "judge"
            return ("maritaca", "sabia-4")
        monkeypatch.setattr("app.llm_routing.resolve_llm_for_task", fake_resolve)

        captured: list = []

        class _Ok:
            async def generate(self, messages, **kw):
                return {"content": '{"ok": true, "confidence": 0.9, "issues": []}'}

        def _fake(name, **kw):
            captured.append((name, kw.get("model")))
            return _Ok()
        monkeypatch.setattr(lp, "get_provider", _fake)

        class _Ev:
            relevance_score = 0.8
            source_name = "s"
            snippet_text = "t"

        out = await _LegacyVerifier().verify("draft", [_Ev()])
        assert out.ok is True
        assert captured == [("maritaca", "sabia-4")]
