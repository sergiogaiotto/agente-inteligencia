"""Incidente Aurora 2026-07-02: fallback LLM morria com 400 de PARÂMETRO.

Cenário real (Workspace): gpt-oss-20b fora do ar → cadeia de resiliência caiu
para azure/gpt-4o, MAS reenviou o ``reasoning_effort`` do agente (válido no
gpt-oss) e a Azure respondeu 400 "Unrecognized request argument supplied:
reasoning_effort" — erro não-de-alcance propagava e derrubava a interação
inteira, com o cliente vendo "erro técnico". De quebra, a cadeia queimava ~60s
de retries no hub morto antes de tentar o fallback.

Três camadas corrigem isso:
1. get_provider só envia reasoning_effort quando o MODELO de destino aceita
   (gate por modelo, não por provider) — ver model_supports_reasoning_effort;
2. _run_llm_chain re-tenta o MESMO candidato sem o parâmetro quando o provider
   rejeita um argumento (is_llm_param_rejection) — cinto-e-suspensório para
   params futuros;
3. cache in-process de providers FORA (TTL curto) reordena a cadeia para não
   re-pagar o timeout de um hub sabidamente morto.
"""
from __future__ import annotations

import time as _time

import httpx
import openai
import pytest

from app.agents import engine
from app.core.llm_providers import (
    get_provider,
    is_llm_param_rejection,
    model_supports_reasoning_effort,
)


@pytest.fixture(autouse=True)
def _clear_llm_down_cache():
    """O cache de providers FORA reordena a cadeia — limpar entre testes para
    ordem determinística (e não vazar estado para outros arquivos)."""
    engine._llm_down_at.clear()
    yield
    engine._llm_down_at.clear()


def _req() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _openai_conn() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=_req())


def _azure_param_400() -> openai.BadRequestError:
    resp = httpx.Response(400, request=_req())
    return openai.BadRequestError(
        "Error code: 400 - {'error': {'message': 'Unrecognized request "
        "argument supplied: reasoning_effort', 'type': 'invalid_request_error',"
        " 'param': None, 'code': None}}",
        response=resp,
        body=None,
    )


class _FakeMsg:
    def __init__(self, content):
        self.content = content


# ═══════════════════════════════════════════════════════════════════
# Detector de rejeição de parâmetro
# ═══════════════════════════════════════════════════════════════════
class TestIsLlmParamRejection:
    def test_azure_unrecognized_argument(self):
        assert is_llm_param_rejection(_azure_param_400()) is True

    def test_unsupported_parameter(self):
        assert is_llm_param_rejection(Exception(
            "Error code: 400 - {'error': {'message': \"Unsupported parameter: "
            "'reasoning_effort' is not supported with this model.\"}}"
        )) is True

    def test_extra_inputs_pydantic_vllm(self):
        # Servidor OpenAI-compatible com validação estrita (vLLM extra=forbid)
        # rejeita campo desconhecido com a mensagem pydantic — também é
        # rejeição de parâmetro, não de conteúdo.
        assert is_llm_param_rejection(RuntimeError(
            "gpt-oss-120b HTTP 400: Extra inputs are not permitted"
        )) is True

    def test_connection_error_nao_e(self):
        assert is_llm_param_rejection(_openai_conn()) is False

    def test_400_de_conteudo_nao_e(self):
        # 400 por content policy NÃO é rejeição de parâmetro — retry sem
        # params não resolveria; deve propagar como hoje.
        assert is_llm_param_rejection(Exception(
            "Error code: 400 - content management policy violation"
        )) is False


# ═══════════════════════════════════════════════════════════════════
# Matriz de suporte a reasoning_effort (gate por MODELO)
# ═══════════════════════════════════════════════════════════════════
class TestModelSupportsReasoningEffort:
    @pytest.mark.parametrize("provider,model,expected", [
        ("azure", "gpt-4o", False),          # o caso do incidente
        ("azure", "gpt-4.1", False),
        ("azure", "o3-mini", True),
        ("azure", "o1", True),
        ("azure", "gpt-5", True),
        ("openai_public", "gpt-4o", False),
        ("openai_public", "o4-mini", True),
        ("gpt-oss-20b", "openai/gpt-oss-20b", True),
        ("gpt-oss-120b", None, True),        # hub aceita p/ qualquer modelo
        ("azure", None, False),              # sem modelo → não dá pra afirmar
        ("maritaca", "sabia-3", False),
        ("ollama", "llama3", False),
    ])
    def test_matrix(self, provider, model, expected):
        assert model_supports_reasoning_effort(provider, model) is expected


class TestGetProviderReasoningEffortGate:
    def test_azure_gpt4o_descarta(self):
        p = get_provider("azure", model="gpt-4o", temperature=0.3, reasoning_effort="low")
        assert p.reasoning_effort is None

    def test_azure_o3_mantem(self):
        p = get_provider("azure", model="o3-mini", temperature=0.3, reasoning_effort="high")
        assert p.reasoning_effort == "high"

    def test_gpt_oss_mantem(self):
        p = get_provider("gpt-oss-20b", model="openai/gpt-oss-20b", reasoning_effort="low")
        assert p.reasoning_effort == "low"

    def test_maritaca_nao_recebe_kwarg(self):
        # Não pode dar TypeError de kwarg inesperado no construtor.
        p = get_provider("maritaca", model="sabia-3", reasoning_effort="low")
        assert getattr(p, "reasoning_effort", None) is None


# ═══════════════════════════════════════════════════════════════════
# Cadeia: retry do MESMO candidato sem o parâmetro rejeitado
# ═══════════════════════════════════════════════════════════════════
class TestChainParamRejectionRetry:
    @pytest.mark.asyncio
    async def test_retry_mesmo_candidato_sem_param(self):
        agent = {"reasoning_effort": "low"}
        calls = []

        async def run_attempt(p, m):
            calls.append((p, m, agent.get("reasoning_effort")))
            if len(calls) == 1:
                raise _azure_param_400()
            return {"messages": [_FakeMsg("OK sem effort")]}

        result, attempted = await engine._run_llm_chain(
            [("azure", "gpt-4o")], agent, run_attempt, "a1"
        )
        assert result is not None
        assert result["messages"][0].content == "OK sem effort"
        # 2ª tentativa foi no MESMO candidato, já sem o parâmetro
        assert calls == [("azure", "gpt-4o", "low"), ("azure", "gpt-4o", None)]
        assert attempted == ["azure/gpt-4o"]
        assert agent["reasoning_effort"] is None

    @pytest.mark.asyncio
    async def test_replay_do_incidente_aurora(self):
        # gpt-oss morto → fallback azure rejeita reasoning_effort → retry sem
        # o param responde. Antes: 400 propagava e o cliente via erro técnico.
        agent = {"reasoning_effort": "low"}
        calls = []

        async def run_attempt(p, m):
            calls.append((p, m, agent.get("reasoning_effort")))
            if p.startswith("gpt-oss"):
                raise _openai_conn()
            if agent.get("reasoning_effort"):
                raise _azure_param_400()
            return {"messages": [_FakeMsg("RESPOSTA REAL")]}

        result, attempted = await engine._run_llm_chain(
            [("gpt-oss-20b", "openai/gpt-oss-20b"), ("azure", "gpt-4o")],
            agent, run_attempt, "a1",
        )
        assert result is not None
        assert result["messages"][0].content == "RESPOSTA REAL"
        assert attempted == ["gpt-oss-20b/openai/gpt-oss-20b", "azure/gpt-4o"]
        assert agent["llm_provider"] == "azure" and agent["model"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_param_400_sem_reasoning_effort_propaga(self):
        # Agente NÃO tem reasoning_effort → não há o que despir; propaga como
        # qualquer 400 (comportamento de hoje).
        agent = {}

        async def run_attempt(p, m):
            raise _azure_param_400()

        with pytest.raises(openai.BadRequestError):
            await engine._run_llm_chain([("azure", "gpt-4o")], agent, run_attempt, "a1")

    @pytest.mark.asyncio
    async def test_retry_tambem_400_propaga(self):
        agent = {"reasoning_effort": "low"}

        async def run_attempt(p, m):
            raise _azure_param_400()

        with pytest.raises(openai.BadRequestError):
            await engine._run_llm_chain([("azure", "gpt-4o")], agent, run_attempt, "a1")

    @pytest.mark.asyncio
    async def test_retry_inacessivel_segue_cadeia(self):
        # 400 de param no 1º candidato; retry sem param dá Connection error →
        # cadeia segue para o próximo candidato normalmente.
        agent = {"reasoning_effort": "low"}
        calls = []

        async def run_attempt(p, m):
            calls.append(p)
            if p == "azure" and len(calls) == 1:
                raise _azure_param_400()
            if p == "azure":
                raise _openai_conn()
            return {"messages": [_FakeMsg("VIA OSS")]}

        result, attempted = await engine._run_llm_chain(
            [("azure", "gpt-4o"), ("gpt-oss-120b", "x")], agent, run_attempt, "a1"
        )
        assert result is not None
        assert result["messages"][0].content == "VIA OSS"
        assert attempted == ["azure/gpt-4o", "gpt-oss-120b/x"]


# ═══════════════════════════════════════════════════════════════════
# Cache de providers FORA — cadeia não re-paga timeout do hub morto
# ═══════════════════════════════════════════════════════════════════
class TestChainDownCache:
    @pytest.mark.asyncio
    async def test_provider_marcado_fora_vai_pro_fim(self):
        # gpt-oss falhou há segundos → a cadeia tenta o fallback PRIMEIRO,
        # sem re-pagar o timeout do hub morto.
        engine._mark_llm_down("gpt-oss-20b")
        agent = {}
        calls = []

        async def run_attempt(p, m):
            calls.append((p, m))
            return {"messages": [_FakeMsg("OK")]}

        result, attempted = await engine._run_llm_chain(
            [("gpt-oss-20b", "openai/gpt-oss-20b"), ("azure", "gpt-4o")],
            agent, run_attempt, "a1",
        )
        assert result is not None
        assert calls[0] == ("azure", "gpt-4o")
        assert attempted[0] == "azure/gpt-4o"

    @pytest.mark.asyncio
    async def test_falha_de_alcance_marca_fora(self):
        agent = {}

        async def run_attempt(p, m):
            if p.startswith("gpt-oss"):
                raise _openai_conn()
            return {"messages": [_FakeMsg("OK")]}

        await engine._run_llm_chain(
            [("gpt-oss-20b", "x"), ("azure", "gpt-4o")], agent, run_attempt, "a1"
        )
        assert engine._llm_marked_down("gpt-oss-20b") is True
        # o que respondeu fica/volta a ser considerado vivo
        assert engine._llm_marked_down("azure") is False

    @pytest.mark.asyncio
    async def test_sucesso_limpa_marca(self):
        engine._mark_llm_down("azure")
        agent = {}

        async def run_attempt(p, m):
            return {"messages": [_FakeMsg("OK")]}

        await engine._run_llm_chain([("azure", "gpt-4o")], agent, run_attempt, "a1")
        assert engine._llm_marked_down("azure") is False

    @pytest.mark.asyncio
    async def test_todos_marcados_mantem_ordem_original(self):
        engine._mark_llm_down("gpt-oss-20b")
        engine._mark_llm_down("azure")
        agent = {}
        calls = []

        async def run_attempt(p, m):
            calls.append(p)
            return {"messages": [_FakeMsg("OK")]}

        await engine._run_llm_chain(
            [("gpt-oss-20b", "x"), ("azure", "gpt-4o")], agent, run_attempt, "a1"
        )
        assert calls[0] == "gpt-oss-20b"

    def test_ttl_expira_marca(self):
        engine._llm_down_at["velho"] = _time.monotonic() - (engine._LLM_DOWN_TTL_SECONDS + 10)
        assert engine._llm_marked_down("velho") is False
        assert "velho" not in engine._llm_down_at
