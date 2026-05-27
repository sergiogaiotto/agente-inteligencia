"""Onda Observabilidade RAG (2026-05-27): chunks de evidência no execution_log.

User reportou no Workspace: agente caiu em Refuse com 'evidence_insufficient'
(score 0.07), mas o log só dizia "3 evidência(s) encontrada(s) · Score: 0.07
· Fontes: Scirpts Rentab e Churn". Sem ver o conteúdo dos 3 chunks, era
impossível distinguir:

- "base não cobre o tema" (snippet irrelevante à pergunta)
- "embedder mal calibrado" (snippet bate mas score baixo)
- "chunking ruim" (snippet partido no meio)

Fix: `_build_execution_log` agora aceita `evidence_detail` e adiciona uma
linha por chunk com preview do texto, score e fonte. O `trace` também passa
a expor `evidence_detail` pro frontend mostrar/exportar (aba "Evidências"
no XLSX).
"""
from __future__ import annotations

import pytest

from app.agents.engine import _build_execution_log


def _base_args():
    """Args mínimos pra invocar _build_execution_log sem KeyError."""
    return dict(
        agent={"name": "test", "kind": "subagent", "llm_provider": "azure", "model": "gpt-4o", "version": "1.0.0"},
        skill_data={"_execution_mode": "standard"},
        skill_detail={},
        mcp_tools_detail=[],
        transitions=[],
        evidence_count=0,
        evidence_sources=[],
        evidence_score=0.0,
        duration=100.0,
        final_state="Recommend",
    )


class TestEvidenceDetailRendering:
    def test_no_evidence_detail_keeps_legacy_behavior(self):
        """Compat: chamadas sem evidence_detail continuam funcionando (default None)."""
        log = _build_execution_log(**_base_args())
        # Sem evidências, mostra "Sem evidências consultadas"
        ev_lines = [r for r in log if r["cat"] == "evidence"]
        assert len(ev_lines) == 1
        assert "Sem evidências" in ev_lines[0]["title"]

    def test_evidence_detail_adds_one_line_per_chunk(self):
        """Cenário do user: 3 chunks recuperados → 3 linhas extras no log."""
        args = _base_args()
        args["evidence_count"] = 3
        args["evidence_score"] = 0.07
        args["evidence_sources"] = ["Scirpts Rentab e Churn"]
        args["evidence_detail"] = [
            {"ordinal": 1, "score": 0.10, "source": "Scirpts Rentab e Churn",
             "knowledge_source_id": "ks-abc", "text_preview": "SELECT * FROM rentab WHERE ...",
             "text_full_len": 280},
            {"ordinal": 2, "score": 0.08, "source": "Scirpts Rentab e Churn",
             "knowledge_source_id": "ks-abc", "text_preview": "Script de churn mensal: ...",
             "text_full_len": 145},
            {"ordinal": 3, "score": 0.03, "source": "Scirpts Rentab e Churn",
             "knowledge_source_id": "ks-abc", "text_preview": "DROP TABLE rentab_old;",
             "text_full_len": 22},
        ]
        log = _build_execution_log(**args)
        ev_lines = [r for r in log if r["cat"] == "evidence"]
        # 1 header agregado + 3 chunks detalhados
        assert len(ev_lines) == 4
        # Header agregado primeiro
        assert "3 evidência" in ev_lines[0]["title"]
        # 3 chunks por score desc
        chunks = ev_lines[1:]
        assert "0.10" in chunks[0]["title"]
        assert "0.08" in chunks[1]["title"]
        assert "0.03" in chunks[2]["title"]

    def test_low_score_chunks_marked_warning(self):
        """Score < 0.3 marca como warning visualmente pro user identificar
        chunks que estão puxando o agregado pra baixo."""
        args = _base_args()
        args["evidence_count"] = 2
        args["evidence_score"] = 0.5
        args["evidence_detail"] = [
            {"ordinal": 1, "score": 0.85, "source": "S", "text_preview": "high", "text_full_len": 4},
            {"ordinal": 2, "score": 0.15, "source": "S", "text_preview": "low",  "text_full_len": 3},
        ]
        log = _build_execution_log(**args)
        chunks = [r for r in log if r["cat"] == "evidence" and r["icon"] == "📄"]
        assert len(chunks) == 2
        # Score alto → success; score baixo → warning
        high = next(c for c in chunks if "0.85" in c["title"])
        low = next(c for c in chunks if "0.15" in c["title"])
        assert high["level"] == "success"
        assert low["level"] == "warning"

    def test_preview_truncation_marker(self):
        """Quando text_full_len > 300 (cap de preview no log), aparece marca
        de truncamento — ajuda user a entender que tem mais conteúdo."""
        args = _base_args()
        args["evidence_count"] = 1
        args["evidence_score"] = 0.5
        args["evidence_detail"] = [
            {"ordinal": 1, "score": 0.5, "source": "S",
             "text_preview": "x" * 300, "text_full_len": 1200},
        ]
        log = _build_execution_log(**args)
        chunk = next(r for r in log if r["icon"] == "📄")
        assert "…" in chunk["detail"]

    def test_chunks_ordered_by_score_desc(self):
        """Ordenação por score desc independente da ordem original — ajuda
        user a focar nos mais relevantes primeiro."""
        args = _base_args()
        args["evidence_count"] = 3
        args["evidence_score"] = 0.5
        args["evidence_detail"] = [
            {"ordinal": 1, "score": 0.20, "source": "S", "text_preview": "a", "text_full_len": 1},
            {"ordinal": 2, "score": 0.90, "source": "S", "text_preview": "b", "text_full_len": 1},
            {"ordinal": 3, "score": 0.50, "source": "S", "text_preview": "c", "text_full_len": 1},
        ]
        log = _build_execution_log(**args)
        chunks = [r for r in log if r["icon"] == "📄"]
        scores = [float(c["title"].split("score ")[1].split(" ")[0]) for c in chunks]
        assert scores == sorted(scores, reverse=True)

    def test_newlines_in_preview_collapsed(self):
        """Texto com \\n vira espaço — log fica linear, sem quebrar visualmente."""
        args = _base_args()
        args["evidence_count"] = 1
        args["evidence_score"] = 0.5
        args["evidence_detail"] = [
            {"ordinal": 1, "score": 0.5, "source": "S",
             "text_preview": "line1\nline2\nline3", "text_full_len": 17},
        ]
        log = _build_execution_log(**args)
        chunk = next(r for r in log if r["icon"] == "📄")
        assert "\n" not in chunk["detail"]
        assert "line1 line2 line3" in chunk["detail"]
