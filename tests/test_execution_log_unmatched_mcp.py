"""Regressão pra 'Sem ferramentas MCP' ambíguo no execution_log.

Cenário real (user, 2026-05-28): agente _Qresearch_ vinculado a skill
"Pesquisa na Internet via Tavily" rodou e retornou cards com links
(IBM, Microsoft, Splunk, Google Cloud) — mas o painel mostrava
"Sem ferramentas MCP" e 0 MCP TOOLS. O LLM (gpt-oss-120b) alucinou
os resultados a partir de conhecimento de treino, porque a tool Tavily
declarada no SKILL.md NÃO bateu com nenhum registro em /tools.

Antes deste fix, a UI mostrava "Sem ferramentas MCP" igualmente em
2 cenários muito diferentes:
- Skill não declara MCP (correto, by design)
- Skill declara tools órfãs (alucinação iminente, perigoso)

Fix expõe `mcp_tools_unmatched` no execution_log com warning explícito.
"""
from __future__ import annotations

import pytest

from app.agents.engine import _build_execution_log


def _base_args():
    """Args mínimos pra invocar _build_execution_log sem KeyError."""
    return dict(
        agent={"name": "test", "kind": "subagent", "llm_provider": "azure", "model": "gpt-4o", "version": "1.0.0"},
        skill_data={"_execution_mode": "fast"},
        skill_detail={},
        mcp_tools_detail=[],
        transitions=[],
        evidence_count=0,
        evidence_sources=[],
        evidence_score=0.0,
        duration=100.0,
        final_state="Recommend",
    )


class TestMcpToolsLogStates:
    def test_no_bindings_declared_shows_neutral_info(self):
        """Skill sem ## Tool Bindings → 'Sem ferramentas MCP' neutro (info).
        Comportamento padrão pra skills puramente conversacionais."""
        log = _build_execution_log(**_base_args())
        tools_lines = [r for r in log if r["cat"] == "tools"]
        assert len(tools_lines) == 1
        assert "Sem ferramentas MCP" in tools_lines[0]["title"]
        assert tools_lines[0]["level"] == "info"

    def test_all_bindings_resolved_shows_success(self):
        """N declared, N resolved → 'N ferramenta(s) MCP vinculada(s)' info,
        sem warning de unmatched."""
        args = _base_args()
        args["mcp_tools_detail"] = [
            {"name": "tavily_search", "server": "tavily", "ops": ["search"]},
            {"name": "tavily_extract", "server": "tavily", "ops": ["extract"]},
        ]
        args["mcp_tools_declared_count"] = 2
        args["mcp_tools_unmatched"] = []
        log = _build_execution_log(**args)
        tools_lines = [r for r in log if r["cat"] == "tools"]
        # 1 header + 2 detalhes por tool
        assert len(tools_lines) == 3
        # Header cita "2 de 2"
        assert "2 de 2" in tools_lines[0]["detail"]
        # Sem linhas de warning
        assert not any(r["level"] == "warning" for r in tools_lines)

    def test_all_bindings_unmatched_shows_warning_with_alucinacao(self):
        """Cenário do user — N declared, 0 resolved. Warning explícito sobre
        risco de alucinação (LLM voa solo)."""
        args = _base_args()
        args["mcp_tools_detail"] = []
        args["mcp_tools_declared_count"] = 1
        args["mcp_tools_unmatched"] = ["tavily_search"]
        log = _build_execution_log(**args)
        tools_lines = [r for r in log if r["cat"] == "tools"]
        # Warning na linha principal
        warn_lines = [r for r in tools_lines if r["level"] == "warning"]
        assert warn_lines, "Tools unmatched DEVEM gerar pelo menos 1 warning"
        # Texto cita o nome da tool órfã
        all_text = " ".join(r.get("title", "") + " " + r.get("detail", "") for r in warn_lines)
        assert "tavily_search" in all_text
        # Texto cita alucinação como risco
        assert "alucinação" in all_text.lower()
        # Não pode ter o "Sem ferramentas MCP" neutro — esse cenário é DIFERENTE
        neutral = [r for r in tools_lines if r["title"] == "Sem ferramentas MCP"]
        assert not neutral, (
            "Skill com tools declaradas mas não resolvidas NÃO pode mostrar "
            "'Sem ferramentas MCP' neutro — isso esconde o bug"
        )

    def test_partial_match_shows_both_resolved_and_warning(self):
        """Cenário misto: 3 declared, 1 resolved, 2 unmatched.
        Mostra a resolvida normalmente + warning sobre as órfãs."""
        args = _base_args()
        args["mcp_tools_detail"] = [
            {"name": "tavily_search", "server": "tavily", "ops": ["search"]},
        ]
        args["mcp_tools_declared_count"] = 3
        args["mcp_tools_unmatched"] = ["tavily_extract", "tavily_crawl"]
        log = _build_execution_log(**args)
        tools_lines = [r for r in log if r["cat"] == "tools"]
        # Header das resolvidas + detalhe + linha de warning das órfãs
        assert any("1 ferramenta(s) MCP vinculada(s)" in r["title"] for r in tools_lines)
        warn = [r for r in tools_lines if r["level"] == "warning"]
        assert warn
        assert "tavily_extract" in warn[0].get("detail", "")
        assert "tavily_crawl" in warn[0].get("detail", "")

    def test_warning_mentions_tools_registry(self):
        """Mensagem do warning ensina o user a investigar — cita /tools.
        Sem isso, user vê 'tool não resolve' e não sabe o que fazer."""
        args = _base_args()
        args["mcp_tools_declared_count"] = 1
        args["mcp_tools_unmatched"] = ["mystery_tool"]
        log = _build_execution_log(**args)
        warn = [r for r in log if r["level"] == "warning"]
        text = " ".join(r.get("detail", "") for r in warn)
        assert "/tools" in text or "Tools Registry" in text
