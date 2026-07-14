"""Projeção de verbosidade da resposta de invoke (server-side, sem custo).

Três níveis sobre o MESMO `result` do engine — NÃO muda execução, custo ou
tokens; só projeta o que SAI na resposta da API:

- ``full``    : payload completo (debug). Tudo: trace, transitions, cost_usd,
                sql_rendered, modelos, ids. Default de sessão/Workspace.
- ``summary`` : resposta + narrativa por step (``status_message`` = o
                ``processing_message`` do agente) + status + contadores. Sem
                trace/transitions/custo/SQL. Default de chamada via X-API-Key.
- ``minimal`` : só a resposta final + status + interaction_id.

A resolução do default é CIENTE DE AUTH: sessão (cookie) → ``full``; integração
(X-API-Key) → o configurado em ``platform_settings.api_invoke_default_verbosity``
(semente ``summary``). Explícito (body/query) sempre vence.

Motivação: o teste E2E como usuário (2026-06-23) recebeu ~29 KB de debug ao
chamar o endpoint — inadequado p/ uma UI externa, e ainda expõe o SQL renderado
e o custo por chamada. Ver ``docs/backlog-teste-e2e.md``.
"""
from __future__ import annotations

import json
from typing import Optional

VERBOSITY_LEVELS = ("full", "summary", "minimal")
DEFAULT_VERBOSITY = "full"
#: Versão do ENVELOPE de resposta do invoke (contrato externo). Presente em TODAS
#: as verbosidades para o cliente detectar mudanças sem inspecionar as chaves.
SCHEMA_VERSION = "1"


def _output_data(output) -> tuple:
    """Devolve (data, output_is_json).

    O `output` do invoke é uma STRING (prosa OU JSON serializado — ex.: agente
    declarativo devolve a resposta da API via json.dumps). Isso força o consumidor
    a JSON.parse duas vezes E adivinhar se é JSON. Aqui pré-parseamos: quando o
    output É um JSON (objeto/lista), `data` traz o objeto pronto e `output_is_json`
    = True; senão `data`=None. `output` (string) é preservado p/ retrocompat.
    """
    if isinstance(output, (dict, list)):
        return output, True
    if isinstance(output, str):
        s = output.strip()
        if s and s[0] in "{[":
            try:
                parsed = json.loads(s)
                if isinstance(parsed, (dict, list)):
                    return parsed, True
            except Exception:
                pass
    return None, False
#: default semeado p/ chamadas via X-API-Key (sobrescrevível em platform_settings)
API_KEY_DEFAULT_VERBOSITY = "summary"


def normalize_verbosity(value: Optional[str], *, fallback: str = DEFAULT_VERBOSITY) -> str:
    """Normaliza p/ um nível conhecido; valor inválido/ausente → ``fallback``."""
    v = (value or "").strip().lower()
    return v if v in VERBOSITY_LEVELS else fallback


def resolve_verbosity(
    explicit: Optional[str],
    *,
    is_api_key: bool,
    api_default: str = API_KEY_DEFAULT_VERBOSITY,
) -> str:
    """Decide a verbosidade efetiva.

    Precedência: explícito (body/query) > default por auth. Sessão → ``full``;
    integração (X-API-Key) → ``api_default`` (lido de platform_settings pela rota).
    Função PURA (o I/O de settings fica na rota) → trivialmente testável.

    Importante: um explícito INVÁLIDO (typo, ex.: ``?verbosity=summry``) vindo de
    X-API-Key NÃO pode escalar p/ ``full`` — isso vazaria o payload de debug que a
    feature existe pra suprimir. Por isso o explícito normaliza contra o MESMO
    fallback ciente de auth (achado HIGH da revisão adversarial).
    """
    fallback = (
        normalize_verbosity(api_default, fallback=API_KEY_DEFAULT_VERBOSITY)
        if is_api_key else DEFAULT_VERBOSITY
    )
    if explicit and explicit.strip():
        return normalize_verbosity(explicit, fallback=fallback)
    return fallback


def _summary_step(step: dict) -> dict:
    """Step enxuto p/ UI: identidade humana + status + narrativa + saída.

    Omite de propósito: ``agent_id``/``agent_model`` (ids internos), ``trace``,
    ``transitions``, ``cost_usd``, ``tokens_used``, ``evidence_score`` e o SQL
    renderado que vive no trace. ``status_message`` é o ``processing_message`` do
    agente (a narrativa 💬), que o engine passou a expor por step.
    """
    out = {
        "agent_name": step.get("agent_name", ""),
        "agent_kind": step.get("agent_kind", ""),
        "status": step.get("status", ""),
        "status_message": step.get("status_message", ""),
        "output": step.get("output", ""),
    }
    # Step que falhou (engine: {status:'error', error:...}). Sem isto o consumidor
    # via API veria status='error' com output vazio e ZERO explicação — preserva o
    # motivo (o final_state do pipeline é 'completed' mesmo com step em erro).
    if step.get("error"):
        out["error"] = step["error"]
    return out


def project_pipeline_result(result: dict, verbosity: str) -> dict:
    """Projeta a resposta de ``POST /pipelines/{id}/invoke`` conforme a verbosidade.

    ``result`` é o dict JÁ montado pela rota no formato ``full``. ``full`` é
    devolvido VERBATIM (retrocompatível). ``summary``/``minimal`` recortam.
    """
    v = normalize_verbosity(verbosity)
    data, output_is_json = _output_data(result.get("output"))
    if v == "full":
        # full é retrocompatível (todo o payload legado preservado) + campos
        # ADITIVOS do envelope. Cópia rasa (não muta o dict do chamador); full é
        # o default de DEBUG/sessão, não o caminho de alto volume (X-API-Key →
        # summary), então o custo da cópia é irrelevante.
        return {
            **result,
            "schema_version": result.get("schema_version", SCHEMA_VERSION),
            "verbosity": result.get("verbosity", "full"),
            "data": result.get("data", data),
            "output_is_json": result.get("output_is_json", output_is_json),
        }

    base = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_id": result.get("pipeline_id"),
        "interaction_id": result.get("interaction_id"),
        "status": result.get("status", "completed"),
        "output": result.get("output", ""),
        "data": data,
        "output_is_json": output_is_json,
        "verbosity": v,
    }
    if v == "minimal":
        return base

    # summary: + contadores e a narrativa por step (sem trace/custo/SQL)
    base.update({
        "final_state": result.get("final_state"),
        "output_agent": result.get("output_agent"),  # Pacote B (35.0.0): quem respondeu
        "total_agents": result.get("total_agents", 0),
        "completed_agents": result.get("completed_agents", 0),
        "duration_ms": result.get("duration_ms"),
        "steps": [_summary_step(s) for s in (result.get("pipeline_steps") or [])],
    })
    return base
