"""Provedores de LLM — Azure OpenAI (primário), Maritaca, Ollama.

Cada provider expõe `generate(messages) -> dict` e `get_langchain_llm()`.

Histórico:
- Onda 4b introduziu LiteLLM como gateway intermediário entre app e providers.
- Removido depois (gateway era opt-in e nunca chegou a virar default — overhead
  de container + RAM no VPS não compensava o roteamento simples por prefixo
  que `get_provider()` já entrega nativamente).
- OpenAI público também foi removido (Onda 7 Wave 5): provider 'openai' é
  alias de Azure pra retrocompat de agentes legacy.
"""

import logging
import re
import httpx
from abc import ABC, abstractmethod
from langchain_openai import ChatOpenAI, AzureChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from app.core.config import get_settings


# Modelos de reasoning da OpenAI (o1/o3/o4...) REJEITAM temperature != 1 (HTTP 400).
# Para esses, omitimos temperature do request. gpt-oss-* e gpt-4o NÃO casam (aceitam temp).
_REASONING_ONLY_RE = re.compile(r"^(o1|o3|o4)(\b|-|$)", re.IGNORECASE)

# Modelos da família OpenAI/Azure que ACEITAM o campo reasoning_effort no body.
# gpt-4o/gpt-4/gpt-35 NÃO aceitam — Azure devolve 400 "Unrecognized request
# argument supplied: reasoning_effort" (incidente do fallback Aurora, 2026-07-02).
_REASONING_EFFORT_RE = re.compile(r"^(o1|o3|o4|gpt-5)(\b|-|$)", re.IGNORECASE)


def _is_reasoning_only_model(model) -> bool:
    return bool(model and _REASONING_ONLY_RE.match(str(model).strip()))


def model_supports_reasoning_effort(provider_name, model) -> bool:
    """True quando o par provider/model aceita ``reasoning_effort`` no request.

    - hub gpt-oss (20b/120b): aceita para qualquer modelo servido lá (validado
      ao vivo contra o endpoint real);
    - Azure/OpenAI: SÓ modelos de raciocínio (o1/o3/o4/gpt-5). Enviar para
      gpt-4o & cia = 400 invalid_request_error — o que matava a cadeia de
      resiliência quando o fallback do gpt-oss caía na Azure.
    - demais providers (maritaca/ollama): não conhecem o parâmetro.
    """
    p = (provider_name or "").strip().lower()
    if p in ("gpt-oss-20b", "gpt-oss-120b"):
        return True
    if p in ("azure", "openai", "openai_public"):
        return bool(model and _REASONING_EFFORT_RE.match(str(model).strip()))
    return False


def is_llm_param_rejection(exc: BaseException) -> bool:
    """True quando o provider rejeitou um PARÂMETRO do request (não o conteúdo).

    Ex.: Azure gpt-4o com ``reasoning_effort`` → 400 invalid_request_error
    "Unrecognized request argument supplied". A cadeia de resiliência usa este
    detector para re-tentar o MESMO candidato sem os parâmetros opcionais, em
    vez de propagar o 400 e derrubar a interação inteira.
    """
    s = str(exc or "").lower()
    return any(m in s for m in (
        "unrecognized request argument",
        "unsupported parameter",
        "unknown parameter",
        "unsupported_value",
        # Servidores OpenAI-compatible com validação pydantic estrita (vLLM
        # extra=forbid & cia) rejeitam campo desconhecido com esta mensagem.
        "extra inputs are not permitted",
    ))


def _openai_chat_kwargs(temperature, model, reasoning_effort) -> dict:
    """kwargs compartilhados p/ ChatOpenAI/AzureChatOpenAI (toda a família OpenAI):

    - ``reasoning_effort`` (low|medium|high) → vai via ``model_kwargs`` e vira campo
      top-level no body OpenAI-compatible (gpt-oss e reasoning models da OpenAI).
      Só quando setado; ausente = comportamento de hoje (default do modelo).
    - ``temperature``: modelos reasoning-only (o1/o3/o4) SÓ aceitam temperature=1 e
      dão HTTP 400 com outro valor. OMITIR o kwarg NÃO resolve — ChatOpenAI/
      AzureChatOpenAI têm default 0.7 e mandam temperature no body de qualquer jeito
      (e p/ Azure o validador interno do langchain nem dispara, pois o modelo chega
      via azure_deployment). Por isso FORÇAMOS temperature=1.0 nesses casos.
    """
    kw = {"temperature": 1.0 if _is_reasoning_only_model(model) else temperature}
    if reasoning_effort:
        kw["model_kwargs"] = {"reasoning_effort": reasoning_effort}
    return kw

logger = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    """Provider curto-circuitado pelo circuit-breaker (circuito ABERTO).

    Tratado como INALCANÇÁVEL em toda a plataforma (``is_llm_unreachable`` o
    reconhece) — do ponto de vista do caller, o provider "não responde agora",
    então a cadeia de resiliência cai no fallback/mensagem acionável como se
    fosse um ConnectError, sem pagar o timeout. Ver app/core/llm_breaker.py."""


def is_llm_unreachable(exc: BaseException, _depth: int = 0) -> bool:
    """True quando a falha é de ALCANCE do provider (rede/timeout/URL ausente),
    NÃO um request malformado. Detector canônico, compartilhado pelo wizard
    (``app/routes/wizard.py``) e pela cadeia de resiliência do runtime
    (``app/agents/engine.py``).

    Cobre os DOIS caminhos de chamada da plataforma:
    - path httpx direto (``provider.generate`` dos OpenAI-compatible: Maritaca,
      Ollama, GPT-OSS) → levanta ``httpx.ConnectError``/``TimeoutException``;
    - path LangChain (``provider.get_langchain_llm().ainvoke``, usado pelo
      harness em runtime) → o SDK ``openai`` envelopa a httpx em
      ``openai.APIConnectionError`` (cujo ``str()`` é "Connection error.") /
      ``openai.APITimeoutError``. ESTE é o caso do erro que o usuário viu no
      Workspace e que um detector só-httpx NÃO pegaria.

    Provider sem URL/key ("não configurado") também conta como inalcançável —
    do ponto de vista do agente, ele não responde. Já erros de request
    (401/404/429 → ``openai.APIStatusError``) retornam False de propósito: são
    erro de configuração/uso que deve aparecer pro operador corrigir, não
    disparar fallback silencioso que mascara o problema.

    Anda na cadeia ``__cause__`` (profundidade limitada) porque o LangChain às
    vezes re-levanta o erro do SDK encadeado.
    """
    # 0) circuito ABERTO pelo breaker — sintetizado, tratado como inalcançável.
    if isinstance(exc, LLMUnavailable):
        return True
    # 1) httpx direto (path .generate dos providers OpenAI-compatible)
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout,
                        httpx.ReadTimeout, httpx.PoolTimeout,
                        httpx.TimeoutException)):
        return True
    # 2) SDK openai (path LangChain) — SÓ conexão/timeout. NÃO APIStatusError
    #    (4xx/5xx): esses são request/serviço, não "não responde".
    try:
        import openai as _openai
        if isinstance(exc, (_openai.APIConnectionError, _openai.APITimeoutError)):
            return True
    except Exception:
        pass
    # 2b) HTTP 5xx do provider/gateway (66.5.1, achado F-5 do E2E 2026-07-21):
    #    502/503/504 (ex.: nginx do hub GPT-OSS fora do ar) É "não respondeu" —
    #    o contrato de contingência das Configurações ("quando o modelo do
    #    agente não responde, a plataforma cai pro Modelo Primário/Fallback")
    #    cobre exatamente este caso. Excluí-los deixava o 503 escapar da cadeia
    #    e o HTML cru do gateway virar draft ao usuário. 4xx segue False de
    #    propósito (401/404/429 = config/uso → mensagem acionável ao operador,
    #    sem fallback silencioso que mascare o problema).
    _sc = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None)
    if isinstance(_sc, int) and _sc >= 500:
        return True
    msg = str(exc).lower()
    # 3) provider sem URL/key configurada — inalcançável na prática.
    #    GPTOSSProvider.generate levanta "url não configurada"; Azure/OpenAI
    #    público levantam "não configurado" em get_langchain_llm(); Maritaca
    #    levanta "API key não configurada". O match SEM a última letra cobre
    #    os dois gêneros (configurado/configurada).
    if isinstance(exc, RuntimeError) and "não configurad" in msg:
        return True
    # 4) fallback por string — "Connection error." do SDK openai, caso o tipo
    #    escape (versões/wrappers diferentes do openai/langchain).
    if "connection error" in msg:
        return True
    # 5) cadeia de causas — LangChain pode encadear o erro original do SDK.
    if _depth < 4:
        cause = getattr(exc, "__cause__", None)
        if cause is not None and cause is not exc:
            return is_llm_unreachable(cause, _depth + 1)
    return False


def is_llm_auth_error(exc: BaseException) -> bool:
    """Detecta erro de AUTENTICAÇÃO do provider (401 — chave inválida/expirada).

    Distinto de "inacessível" (rede caiu): aqui o servidor RESPONDEU negando a
    credencial. Justifica fallback hospedado tanto quanto o unreachable — uma
    chave ruim em UM provider não deve derrubar o caller se há outro provider
    com credencial válida. Detector canônico (movido do wizard em 24.9.0);
    ``app/routes/wizard.py`` mantém wrapper fino.
    """
    sc = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if sc == 401:
        return True
    name = type(exc).__name__.lower()
    if "authenticationerror" in name or "permissiondenied" in name:
        return True
    s = str(exc).lower()
    return (
        "incorrect api key" in s
        or "invalid_api_key" in s
        or "invalid api key" in s
        or ("401" in s and ("api key" in s or "unauthorized" in s or "authentication" in s))
    )


# get_provider mapeia "openai" e "azure" pro MESMO AzureOpenAIProvider (alias
# histórico) — comparações de "provider distinto" precisam canonicalizar,
# senão um fallback openai→azure re-tenta o MESMO backend com a MESMA chave.
_PROVIDER_ALIASES = {"openai": "azure"}


def canonical_provider(name: str) -> str:
    n = (name or "").strip().lower()
    return _PROVIDER_ALIASES.get(n, n)


async def generate_with_hosted_fallback(
    messages: list[dict],
    provider_name: str,
    model: str | None,
    *,
    purpose: str,
    prov_kwargs: dict | None = None,
    gen_kwargs: dict | None = None,
) -> tuple[dict, str, str]:
    """Gera com o par dado; se INACESSÍVEL (rede/URL/timeout) ou 401, re-tenta
    UMA vez no fallback HOSPEDADO (`multimodal_fallback` do Roteamento LLM —
    o modelo "sempre disponível" da plataforma), desde que seja provider
    DIFERENTE do que falhou.

    Cadeia NEUTRA de core, para módulos que não podem depender de app/routes
    (verifier/juiz). O wizard tem a sua própria (`_wizard_llm_complete`, com
    UX de 503 acionável) e o runtime de agentes tem a `_run_llm_chain` por
    candidatos — esta é a versão mínima compartilhável.

    Returns (resp, used_provider, used_model). Sem fallback aplicável, a
    exceção do PRIMÁRIO propaga; se o fallback também falhar, propaga a dele.
    """
    from app.core.llm_breaker import breaker, note_short_circuit

    pk = dict(prov_kwargs or {})
    gk = dict(gen_kwargs or {})
    canon_primary = canonical_provider(provider_name)
    primary_auth = False
    primary_exc: Exception  # a exceção que propaga se não houver fallback aplicável

    # Primário — pula (sem pagar o timeout) se o breaker abriu o circuito dele.
    if await breaker.allow(canon_primary):
        try:
            llm = get_provider(provider_name, model=(model or None), **pk)
            resp = await llm.generate(messages, **gk)
            await breaker.record_success(canon_primary)
            return resp, provider_name, (model or getattr(llm, "model", "") or "")
        except Exception as exc:
            primary_auth = is_llm_auth_error(exc)
            if is_llm_unreachable(exc):
                await breaker.record_failure(canon_primary)
            if not (is_llm_unreachable(exc) or primary_auth):
                raise
            primary_exc = exc
    else:
        note_short_circuit(canon_primary, purpose)
        primary_exc = LLMUnavailable(f"circuit open: {provider_name}")

    # Fallback hospedado (multimodal_fallback do Roteamento LLM).
    try:
        from app.llm_routing import load_routing
        routing = await load_routing()
        target = (routing.get("multimodal_fallback") or "").strip()
    except Exception:
        target = ""
    if "/" not in target:
        raise primary_exc
    fb_provider, fb_model = target.split("/", 1)
    fb_provider = fb_provider.strip().lower()
    fb_model = fb_model.strip()
    if not fb_provider or canonical_provider(fb_provider) == canon_primary:
        raise primary_exc
    canon_fb = canonical_provider(fb_provider)
    logger.warning(
        "llm.fallback.hosted",
        extra={
            "event": "llm.fallback.hosted",
            "purpose": purpose,
            "failed_provider": provider_name,
            "failed_model": model,
            "failed_reason": "auth" if primary_auth else "unreachable",
            "fallback_provider": fb_provider,
            "fallback_model": fb_model,
        },
    )
    # Fallback também curto-circuitado → propaga o erro do primário (não há 3º alvo).
    if not await breaker.allow(canon_fb):
        note_short_circuit(canon_fb, purpose)
        raise primary_exc
    try:
        fb_llm = get_provider(fb_provider, model=(fb_model or None), **pk)
        resp = await fb_llm.generate(messages, **gk)
        await breaker.record_success(canon_fb)
        return resp, fb_provider, fb_model
    except Exception as fb_exc:
        if is_llm_unreachable(fb_exc):
            await breaker.record_failure(canon_fb)
        raise


class LLMProvider(ABC):
    """Interface base para provedores de LLM.

    Wave Structured Output (PR atual): generate() aceita kwarg opcional
    `response_format` no formato OpenAI:
        {"type": "json_schema", "json_schema": {"name": "X", "schema": {...}, "strict": true}}
        ou simples {"type": "json_object"}
    Providers OpenAI-compatible (Azure, Maritaca, GPT-OSS) propagam direto.
    Ollama traduz pro formato nativo (format="json"). Cada provider que não
    suportar ignora silenciosamente (cai no fallback de injetar JSON Schema
    no prompt do system, igual antes).
    """

    # Flag de classe — providers que suportam structured output marcam True.
    # Engine consulta antes de tentar passar response_format pra decidir entre
    # "garantia construtiva" (provider força JSON) vs "prompt + jsonschema
    # validate depois" (gambiarra de retrocompat). Default False = conservador.
    supports_structured_output: bool = False

    @abstractmethod
    async def generate(self, messages: list[dict], **kwargs) -> dict:
        ...

    @abstractmethod
    def get_langchain_llm(self):
        ...


# ───────────────────────────────────────────────────────────────
# Azure OpenAI
# ───────────────────────────────────────────────────────────────
class AzureOpenAIProvider(LLMProvider):
    """Provedor primário — Azure OpenAI Service.

    Diferenças vs OpenAI público:
    - URL única por deployment: <endpoint>/openai/deployments/<deployment>
    - api_version obrigatória (ex: 2024-02-15-preview)
    - `model` no factory é interpretado como `azure_deployment` quando
      passado; do contrário usa AZURE_OPENAI_CHAT_DEPLOYMENT do env.

    Wave Structured Output: aceita response_format via LangChain `bind(...)`.
    Funciona em api_version >= 2024-08-01-preview pra json_schema; versões
    mais antigas só suportam json_object. Em ambos casos, propagar o kwarg
    funciona — Azure rejeita com 400 se incompatível, e o caller faz fallback.
    """

    supports_structured_output = True

    def __init__(self, model: str | None = None, temperature: float = 0.7, reasoning_effort: str | None = None):
        settings = get_settings()
        self.deployment = model or settings.azure_openai_chat_deployment
        self.model = self.deployment
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.endpoint = settings.azure_openai_endpoint
        self.api_key = settings.azure_openai_api_key
        self.api_version = settings.azure_openai_api_version
        if not self.endpoint or not self.api_key:
            self._llm = None
            return
        self._llm = AzureChatOpenAI(
            azure_endpoint=self.endpoint,
            azure_deployment=self.deployment,
            api_version=self.api_version,
            api_key=self.api_key,
            **_openai_chat_kwargs(self.temperature, self.model, self.reasoning_effort),
        )

    def get_langchain_llm(self):
        if self._llm is None:
            raise RuntimeError(
                "Azure OpenAI não configurado. Defina AZURE_OPENAI_ENDPOINT e "
                "AZURE_OPENAI_API_KEY no .env."
            )
        return self._llm

    async def generate(self, messages: list[dict], **kwargs) -> dict:
        return await _generate_via_langchain(self, messages, **kwargs)


# ───────────────────────────────────────────────────────────────
# Maritaca AI — endpoint OpenAI-compatível
# ───────────────────────────────────────────────────────────────
class MaritacaProvider(LLMProvider):
    # Maritaca expõe endpoint OpenAI-compatible; response_format vai direto
    # no JSON do request. Comportamento confirmado em Sabia-3 (function calling
    # também funciona). Caso falhe, server devolve erro e provider propaga.
    supports_structured_output = True

    def __init__(self, model: str | None = None, temperature: float = 0.7):
        settings = get_settings()
        self.model = model or settings.maritaca_model
        self.api_key = settings.maritaca_api_key
        self.api_url = settings.maritaca_api_url
        self.temperature = temperature

    def get_langchain_llm(self):
        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=f"{self.api_url}/v1",
            temperature=self.temperature,
        )

    async def generate(self, messages: list[dict], **kwargs) -> dict:
        # Sem key, o header viraria "Bearer " (vazio) → httpx explode com
        # LocalProtocolError "Illegal header value", que NÃO é classificado
        # como inalcançável e escaparia das cadeias de fallback. Guard com
        # RuntimeError "não configurada" (is_llm_unreachable classifica).
        if not (self.api_key or "").strip():
            raise RuntimeError(
                "maritaca: API key não configurada. "
                "Configure em /settings → Plataforma."
            )
        # Path httpx direto preserva controle fino sobre headers/timeout.
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{self.api_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": self.temperature,
                    **kwargs,
                },
            )
            return _parse_openai_compatible_response(response, provider="maritaca", model=self.model)


# ───────────────────────────────────────────────────────────────
# Ollama — endpoint OpenAI-compatível (/v1/chat/completions)
# ───────────────────────────────────────────────────────────────
class OllamaProvider(LLMProvider):
    # Ollama via /v1/chat/completions é OpenAI-compatible: aceita
    # response_format mas não respeita "json_schema" — converte tudo pra
    # JSON-mode equivalente ao "json_object". Tradução acontece no generate().
    supports_structured_output = True

    def __init__(self, model: str | None = None, temperature: float = 0.7):
        settings = get_settings()
        self.model = model or settings.ollama_model
        self.api_url = settings.ollama_api_url.rstrip("/")
        self.api_key = settings.ollama_api_key or "ollama"
        self.temperature = temperature

    def get_langchain_llm(self):
        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=f"{self.api_url}/v1",
            temperature=self.temperature,
        )

    async def generate(self, messages: list[dict], **kwargs) -> dict:
        # Ollama nativo prefere `format: "json"` em vez de `response_format`.
        # Quando engine passa response_format={"type":"json_schema",...},
        # convertemos pra forma equivalente que o servidor entende.
        # Schema completo não é honrado (Ollama valida só "é JSON?"), mas
        # ainda assim ganha-se garantia de JSON parseável — ContractValidator
        # roda depois pra validar shape específico.
        rf = kwargs.pop("response_format", None)
        if rf and isinstance(rf, dict):
            rf_type = rf.get("type")
            if rf_type in ("json_schema", "json_object"):
                # Ollama OpenAI-compat aceita response_format={"type":"json_object"}
                # ou format:"json" no body — usamos response_format que é cross-compat
                kwargs["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{self.api_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": self.temperature,
                    **kwargs,
                },
            )
            return _parse_openai_compatible_response(response, provider="ollama", model=self.model)


# ───────────────────────────────────────────────────────────────
# GPT-OSS (open-weight 20b / 120b via endpoint OpenAI-compatible)
# ───────────────────────────────────────────────────────────────
class GPTOSSProvider(LLMProvider):
    """Provedor para gpt-oss-20b e gpt-oss-120b.

    Cada size tem URL/api_key/model próprias em platform_settings —
    permite que cada modelo seja servido por endpoint dedicado (ex: hub
    interno com 2 GPUs distintas atendendo o 20b vs 120b).

    'not-needed' é valor válido de api_key — o proxy autentica de outra
    forma (rede interna, mTLS, etc.). Continua mandando o header
    Authorization Bearer pra compatibilidade com o cliente OpenAI.

    Wave Structured Output: gpt-oss-2025 (20b e 120b) suporta
    response_format={"type":"json_schema",...} via runtime compatível
    OpenAI. Caso o servidor não suporte, devolve 400 e caller faz fallback.
    """

    supports_structured_output = True

    def __init__(self, size: str = "120b", model: str | None = None, temperature: float = 0.7, reasoning_effort: str | None = None):
        if size not in ("20b", "120b"):
            raise ValueError(f"GPTOSSProvider size deve ser '20b' ou '120b' (got: {size!r})")
        settings = get_settings()
        self.size = size
        self.reasoning_effort = reasoning_effort
        if size == "20b":
            self.api_url = (settings.oss20b_url or "").rstrip("/")
            self.api_key = settings.oss20b_api_key or "not-needed"
            self.model = model or settings.oss20b_model
        else:
            self.api_url = (settings.oss120b_url or "").rstrip("/")
            self.api_key = settings.oss120b_api_key or "not-needed"
            self.model = model or settings.oss120b_model
        self.temperature = temperature
        self.timeout = settings.llm_timeout_seconds

    def get_langchain_llm(self):
        if not self.api_url:
            return None
        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=self.api_url,
            timeout=self.timeout,
            **_openai_chat_kwargs(self.temperature, self.model, self.reasoning_effort),
        )

    async def generate(self, messages: list[dict], **kwargs) -> dict:
        if not self.api_url:
            raise RuntimeError(
                f"gpt-oss-{self.size}: URL não configurada. "
                f"Configure em /settings → Plataforma → GPT-OSS."
            )
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            **kwargs,
        }
        # reasoning_effort do construtor vale também no path httpx cru — antes
        # só get_langchain_llm() o repassava, então callers de .generate()
        # (wizard, verifier) pediam reasoning e o hub nunca recebia o campo.
        # Kwarg explícito do caller tem precedência sobre o do construtor.
        if self.reasoning_effort and "reasoning_effort" not in body:
            body["reasoning_effort"] = self.reasoning_effort
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.api_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            return _parse_openai_compatible_response(
                response, provider=f"gpt-oss-{self.size}", model=self.model,
            )


# ───────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────
async def _generate_via_langchain(provider, messages: list[dict], **kwargs) -> dict:
    """Path comum para providers que usam LangChain (Azure).

    Wave Structured Output: aceita response_format via .bind(...). LangChain
    AzureChatOpenAI propaga pro Azure como model_kwargs no request OpenAI.
    Funciona desde api_version 2024-08-01-preview pra "json_schema"; versões
    anteriores aceitam "json_object" (sem schema, só garante JSON).
    """
    llm = provider.get_langchain_llm()
    response_format = kwargs.pop("response_format", None)
    if response_format:
        # .bind(...) cria nova instância com kwargs adicionais que vão
        # parar no request final ao Azure. Não muta o singleton.
        llm = llm.bind(response_format=response_format)
    lc_messages = []
    for m in messages:
        if m["role"] == "system":
            lc_messages.append(SystemMessage(content=m["content"]))
        elif m["role"] == "user":
            lc_messages.append(HumanMessage(content=m["content"]))
        elif m["role"] == "assistant":
            lc_messages.append(AIMessage(content=m["content"]))

    response = await llm.ainvoke(lc_messages)

    return {
        "content": response.content,
        "model": provider.model,
        "usage": (response.response_metadata or {}).get("token_usage", {}),
    }


def _parse_openai_compatible_response(response, provider: str, model: str) -> dict:
    """Parse seguro de respostas OpenAI-compatíveis."""
    try:
        data = response.json()
    except Exception:
        raise RuntimeError(
            f"{provider}: resposta inválida (status {response.status_code}, "
            f"body[:200]={response.text[:200]!r})"
        )

    if response.status_code >= 400:
        err = data.get("error") if isinstance(data, dict) else None
        msg = err.get("message") if isinstance(err, dict) else (err or data.get("message") or response.text[:300])
        exc = RuntimeError(f"{provider} HTTP {response.status_code}: {msg}")
        # status_code na exceção: o fast-path do is_llm_auth_error (getattr)
        # pega QUALQUER 401 dos providers httpx-diretos, independente da
        # fraseologia do corpo (que varia por servidor/idioma).
        exc.status_code = response.status_code
        raise exc

    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        err = data.get("error") if isinstance(data, dict) else None
        msg = err.get("message") if isinstance(err, dict) else (err or data.get("message") or "campo 'choices' ausente")
        raise RuntimeError(f"{provider}: {msg} (model={model})")

    try:
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"{provider}: estrutura inesperada em choices[0].message.content ({e})")

    return {
        "content": content or "",
        "model": data.get("model") or model,
        "usage": data.get("usage", {}),
    }


# ───────────────────────────────────────────────────────────────
# OpenAI público (api.openai.com) — PR #194 (2026-05-29)
# Separado de "openai" (alias de Azure). Use quando quiser chamar
# api.openai.com diretamente.
# ───────────────────────────────────────────────────────────────
class OpenAIPublicProvider(LLMProvider):
    """Provedor OpenAI público (api.openai.com). NÃO confundir com 'openai'
    (alias histórico de Azure).

    Usa langchain_openai.ChatOpenAI sem azure_endpoint — vai pra api.openai.com
    por default. Aceita response_format via .bind() (mesmo path do Azure).

    Configuração mínima:
    - OPENAI_PUBLIC_API_KEY (chave começa com 'sk-')
    - OPENAI_PUBLIC_BASE_URL (default https://api.openai.com/v1)
    - OPENAI_PUBLIC_MODEL (default gpt-4o)
    """

    supports_structured_output = True

    def __init__(self, model: str | None = None, temperature: float = 0.7, reasoning_effort: str | None = None):
        settings = get_settings()
        self.model = model or settings.openai_public_model
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.base_url = settings.openai_public_base_url
        self.api_key = settings.openai_public_api_key
        if not self.api_key:
            self._llm = None
            return
        self._llm = ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            **_openai_chat_kwargs(self.temperature, self.model, self.reasoning_effort),
        )

    def get_langchain_llm(self):
        if self._llm is None:
            raise RuntimeError(
                "OpenAI público não configurado. Defina OPENAI_PUBLIC_API_KEY "
                "em /settings → Plataforma."
            )
        return self._llm

    async def generate(self, messages: list[dict], **kwargs) -> dict:
        return await _generate_via_langchain(self, messages, **kwargs)


def get_provider(provider_name: str = "azure", **kwargs) -> LLMProvider:
    """Factory de provedores. Default: azure (Azure OpenAI Service).

    'openai' é ALIAS de 'azure' (Onda 7 Wave 4) — toda chamada que vinha como
    "openai" resolve pra Azure usando azure_openai_api_key. Compatível com
    agentes legacy sem necessidade de migração.

    'openai_public' (PR #194) chama api.openai.com diretamente — usado quando
    operador quer comparar latência/qualidade do GPT-4o público vs Azure.
    """
    # reasoning_effort só vai quando o MODELO de destino aceita (gpt-oss; o1/o3/
    # o4/gpt-5 na Azure/OpenAI). Gate por provider era insuficiente: azure/gpt-4o
    # devolve 400 "Unrecognized request argument" — e num fallback de cadeia isso
    # derrubava a interação inteira. Para maritaca/ollama NÃO repassamos — evita
    # TypeError de kwarg inesperado no construtor.
    reasoning_effort = kwargs.pop("reasoning_effort", None)
    if reasoning_effort:
        if model_supports_reasoning_effort(provider_name, kwargs.get("model")):
            kwargs["reasoning_effort"] = reasoning_effort
        else:
            logger.info(
                "reasoning_effort descartado: modelo de destino não aceita o parâmetro",
                extra={
                    "event": "llm.reasoning_effort.stripped",
                    "provider": provider_name,
                    "model": kwargs.get("model"),
                    "reasoning_effort": reasoning_effort,
                },
            )

    providers = {
        "azure": AzureOpenAIProvider,
        "openai": AzureOpenAIProvider,  # alias histórico
        "openai_public": OpenAIPublicProvider,
        "maritaca": MaritacaProvider,
        "ollama": OllamaProvider,
    }
    # gpt-oss tem 2 sizes com URL/key próprias — distinguir via factory
    if provider_name == "gpt-oss-20b":
        return GPTOSSProvider(size="20b", **kwargs)
    if provider_name == "gpt-oss-120b":
        return GPTOSSProvider(size="120b", **kwargs)
    provider_class = providers.get(provider_name)
    if not provider_class:
        raise ValueError(f"Provedor '{provider_name}' não suportado. Use: {list(providers.keys()) + ['gpt-oss-20b', 'gpt-oss-120b']}")
    return provider_class(**kwargs)
