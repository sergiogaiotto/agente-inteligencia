"""Regressão pro hotfix do NameError em _build_result.

Bug introduzido em PR #169 e disparado por user 2026-05-28:

    Erro na execução: name 'mcp_tools_declared_count' is not defined
    [req_e32db9111396]

Causa: PR #169 adicionou as variáveis mcp_tools_declared_count e
mcp_tools_unmatched no escopo de execute_agent E referenciou elas dentro
de _build_result (função SEPARADA). Mas esqueceu de adicioná-las à
assinatura de _build_result — quando o caminho normal rodava (com tool
Tavily real chamada — confirmado pelo consumo de crédito reportado pelo
user), `_build_result` tentava ler as variáveis e estourava NameError
no fim, depois do tool já ter sido invocado e gerado custo.

Skills com tools resolvidas (sintoma da Tavily) caíam neste caminho.
Skills sem MCP nem chegavam — fim antecipado via missing_reason.

Hotfix: adiciona os parâmetros ao _build_result com defaults seguros
(0, None→[]) e propaga do execute_agent quando disponível.
"""
from __future__ import annotations

import inspect

from app.agents.engine import _build_result


def test_build_result_signature_has_mcp_declared_count():
    """Assinatura DEVE incluir mcp_tools_declared_count — senão NameError
    no corpo da função quando ela referencia a variável."""
    sig = inspect.signature(_build_result)
    assert "mcp_tools_declared_count" in sig.parameters
    # Default sensato (0) — _build_execution_log lida com 0 graciosamente
    param = sig.parameters["mcp_tools_declared_count"]
    assert param.default == 0


def test_build_result_signature_has_mcp_unmatched():
    """Mesmo pra mcp_tools_unmatched — bug do gêmeo."""
    sig = inspect.signature(_build_result)
    assert "mcp_tools_unmatched" in sig.parameters
    param = sig.parameters["mcp_tools_unmatched"]
    # Default None (tratado como [] no corpo via `or []`)
    assert param.default is None


def test_build_result_signature_has_mcp_detail():
    """Não-regressão do parâmetro já existente — garante que o hotfix não
    removeu acidentalmente parâmetros antigos."""
    sig = inspect.signature(_build_result)
    assert "mcp_tools_detail" in sig.parameters
