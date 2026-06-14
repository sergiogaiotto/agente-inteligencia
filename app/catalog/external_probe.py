"""Probe de Plataforma Externa (PR1) — testa a conexão a uma IA de terceiro.

Reaproveita a guarda SSRF (`app/core/ssrf`), os helpers de auth
(`app/core/http_auth`) e a cifra de segredos (`app/core/crypto`). Toda chamada
outbound passa por `validate_public_url` + `follow_redirects=False` + timeout +
cap de tamanho de resposta (lê em streaming até o limite) — mesmo padrão de
`app/catalog/federation_egress._get_json`.

Dois modos (MVP):
- openai_chat: POST {base_url}{path} com {model, messages:[{role:user, content}]}
  (compatível com OpenAI/Azure/maioria dos vendors). Extrai resposta + tokens.
- http_ping: GET {base_url}{path} — só valida que o endpoint responde 2xx/3xx.

Sem efeitos colaterais persistentes — usado pelo "Testar conexão" (inline, PR1) e,
no PR2, pelo "Provar Capacidade" (com registro de execução em
catalog_recipe_executions). `run_probe` NUNCA levanta: devolve sempre um dict
normalizado com {ok, status, latency_ms, output, tokens_*, error, hint}.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import httpx

from app.core.http_auth import build_auth_headers
from app.core.ssrf import SSRFError, validate_public_url

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 30.0
_MAX_RESPONSE_BYTES = 1_000_000  # 1 MB — cap de resposta do vendor
_MAX_OUTPUT_CHARS = 5000

_DEFAULT_PATHS = {
    "openai_chat": "/v1/chat/completions",
    "http_ping": "/",
}


def default_path(mode: str) -> str:
    return _DEFAULT_PATHS.get(mode, "/")


def _truncate(s: str) -> str:
    if not s:
        return ""
    if len(s) <= _MAX_OUTPUT_CHARS:
        return s
    return s[:_MAX_OUTPUT_CHARS] + f"… [+{len(s) - _MAX_OUTPUT_CHARS} chars]"


async def _http_request(
    method: str,
    url: str,
    *,
    headers: dict,
    json_body: Optional[dict],
    timeout_s: float,
) -> tuple[int, bytes]:
    """Chamada HTTP crua com cap de tamanho (streaming). Isolada de propósito para
    facilitar o monkeypatch em testes. Levanta httpx.HTTPError em falha de
    transporte (caller trata)."""
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as client:
        async with client.stream(method, url, headers=headers, json=json_body) as resp:
            total = 0
            chunks: list[bytes] = []
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    break  # cap atingido — ignora o resto
                chunks.append(chunk)
            return resp.status_code, b"".join(chunks)


def _build_headers(config: dict, secret: str) -> dict:
    """Headers de auth a partir do probe config + segredo. `secret` pode vir em
    claro OU cifrado (enc::…) — build_auth_headers/decrypt_secret tratam ambos."""
    return build_auth_headers(
        {
            "auth_type": config.get("auth_type") or "none",
            "auth_header": config.get("auth_header") or "X-API-Key",
            "api_key": secret or "",
        }
    )


def _status_hint(status: int) -> Optional[str]:
    if status == 401:
        return "Auth falhou — confira auth_type e o segredo."
    if status == 403:
        return "Auth ok mas sem permissão — confira o escopo do token."
    if status == 404:
        return "Endpoint não encontrado — confira base_url + path."
    if status == 429:
        return "Rate limit do vendor — tente novamente em instantes."
    return None


async def run_probe(
    config: dict,
    *,
    secret: str = "",
    input_text: str = "",
    allow_http: bool = False,
) -> dict:
    """Executa um probe contra a plataforma externa.

    NUNCA levanta — devolve sempre um dict:
    {ok, status, latency_ms, output, tokens_input, tokens_output, error, hint,
     mode, url}. `secret` em claro ou cifrado. `input_text` sobrepõe test_prompt.
    """
    mode = (config.get("mode") or "openai_chat").strip()
    base = (config.get("base_url") or "").strip().rstrip("/")
    path = (config.get("path") or "").strip() or default_path(mode)
    if not path.startswith("/"):
        path = "/" + path
    url = base + path
    timeout_s = max(1.0, min(120.0, (config.get("timeout_ms") or 30000) / 1000))

    result: dict[str, Any] = {
        "ok": False,
        "status": 0,
        "latency_ms": 0.0,
        "output": "",
        "tokens_input": 0,
        "tokens_output": 0,
        "error": None,
        "hint": None,
        "mode": mode,
        "url": url,
    }

    # 1) Guarda SSRF — bloqueia loopback/privado/link-local/metadata cloud.
    try:
        validate_public_url(url, allow_http=allow_http)
    except SSRFError as e:
        result["error"] = f"URL bloqueada pela guarda SSRF: {e}"
        result["hint"] = "Use uma URL pública https (não-localhost, não-IP-privado)."
        return result

    headers = _build_headers(config, secret)
    prompt = input_text or config.get("test_prompt") or "Responda apenas: OK"

    if mode == "openai_chat":
        method: str = "POST"
        json_body: Optional[dict] = {
            "model": config.get("model") or "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
        }
    else:  # http_ping
        method, json_body = "GET", None

    start = time.monotonic()
    try:
        status, raw = await _http_request(
            method, url, headers=headers, json_body=json_body, timeout_s=timeout_s
        )
    except httpx.ConnectError:
        result["error"] = f"Não foi possível conectar a {base}"
        result["hint"] = "Verifique a URL e se o host está acessível."
        return result
    except httpx.TimeoutException:
        result["status"] = 408
        result["error"] = "Timeout"
        result["hint"] = "Aumente o timeout ou verifique a latência do vendor."
        return result
    except Exception as e:  # noqa: BLE001 — fail-soft; não vaza stack ao cliente
        logger.warning("run_probe: erro inesperado %s: %s", type(e).__name__, e)
        result["status"] = 500
        result["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return result

    result["latency_ms"] = round((time.monotonic() - start) * 1000, 2)
    result["status"] = status
    result["ok"] = 200 <= status < 400

    if mode == "openai_chat":
        try:
            data = json.loads(raw or b"{}")
        except (ValueError, TypeError):
            data = {}
        if isinstance(data, dict):
            choices = data.get("choices") or []
            if choices and isinstance(choices, list):
                msg = (choices[0] or {}).get("message") or {}
                result["output"] = _truncate(str(msg.get("content") or ""))
            usage = data.get("usage") or {}
            if isinstance(usage, dict):
                result["tokens_input"] = int(usage.get("prompt_tokens") or 0)
                result["tokens_output"] = int(usage.get("completion_tokens") or 0)
            err = data.get("error")
            if err and not result["ok"]:
                emsg = err.get("message") if isinstance(err, dict) else str(err)
                result["error"] = str(emsg)[:200]

    if not result["ok"] and not result["error"]:
        result["error"] = f"HTTP {status}"
        result["hint"] = _status_hint(status)
    return result
