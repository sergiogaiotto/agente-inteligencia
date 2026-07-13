"""Pacote A (quick wins pós-revisão E2E Pulsar) — A1 e A2.

A1 — harness `_similarity_check`: antes era overlap de SUBSTRING contando
stopwords (30% atingível por "de/o/a/para"; "planos" casava "planosXYZ").
Agora: tokens de palavra inteira, stopwords pt-BR fora.

A2 — GET /skills/{id} summary: o "X de 9 seções" da UI contava a lista de
EXIBIÇÃO (6 obrigatórias + 3 opcionais, sem Activation Criteria). O summary
agora expõe required_sections_found/missing/total sobre as 7 REQUIRED_SECTIONS
reais do parser.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import skills_repo
from app.harness.evaluator import _similarity_check, _similarity_tokens
from app.routes.skills import router as skills_router
from app.skill_parser.parser import REQUIRED_SECTIONS


class TestSimilarityStopwords:
    def test_recusa_nao_passa_em_gabarito_rico(self):
        """O cenário do incidente: texto de recusa compartilhava stopwords
        suficientes com um expected_output rico e podia passar no 30%."""
        expected = (
            "Opções de upgrade de fibra da Pulsar: Pulsar Turbo 600 Mbps por "
            "R$ 119,90 e Pulsar Ultra 1 Gbps por R$ 159,90, com upgrade "
            "imediato e cobrança pró-rata para o cliente."
        )
        recusa = (
            "Desculpe, mas não há evidências disponíveis nas bases autorizadas "
            "para a sua solicitação. Recomendo entrar em contato com o setor "
            "responsável para obter as informações."
        )
        assert _similarity_check(recusa, expected) is False

    def test_resposta_legitima_continua_passando(self):
        expected = (
            "Opções de upgrade: Pulsar Turbo 600 Mbps R$ 119,90 e Pulsar "
            "Ultra 1 Gbps R$ 159,90, sem fidelidade no Ultra."
        )
        resposta = (
            "Para upgrade temos o Pulsar Turbo com 600 Mbps a R$ 119,90 por "
            "mês e o Pulsar Ultra de 1 Gbps a R$ 159,90 sem fidelidade."
        )
        assert _similarity_check(resposta, expected) is True

    def test_match_e_por_palavra_inteira_nao_substring(self):
        # antes: "plano" (expected) casava por substring em "aeroplanos"
        assert _similarity_check("aeroplanos decolam cedo", "plano contratado valor") is False

    def test_expected_vazio_ou_so_stopwords_passa(self):
        assert _similarity_check("qualquer coisa", "") is True
        assert _similarity_check("qualquer coisa", "de para com o a") is True

    def test_tokens_removem_stopwords_e_normalizam(self):
        toks = _similarity_tokens("O plano DE fibra é R$ 89,90 para o cliente")
        assert "plano" in toks and "fibra" in toks and "89" in toks
        assert "de" not in toks and "o" not in toks and "para" not in toks


_SKILL_MD_PARCIAL = """---
id: urn:skill:teste:subagent:cobertura-secoes
version: 0.1.0
kind: subagent
owner: equipe
stability: alpha
---

# Cobertura de Seções

## Purpose
Testa a contagem de obrigatórias.

## Workflow
1. faz algo

## Guardrails
- nada de inventar

## Evidence Policy
```yaml
sources:
  - abc
```
"""


class TestRequiredSectionsSummary:
    def _get(self, monkeypatch):
        monkeypatch.setattr(
            skills_repo, "find_by_id",
            AsyncMock(return_value={"id": "s1", "raw_content": _SKILL_MD_PARCIAL}),
        )
        app = FastAPI()
        app.include_router(skills_router)
        return TestClient(app).get("/api/v1/skills/s1")

    def test_summary_conta_obrigatorias_reais(self, monkeypatch):
        r = self._get(monkeypatch)
        assert r.status_code == 200
        summary = r.json()["summary"]
        assert summary["required_sections_total"] == len(REQUIRED_SECTIONS) == 7
        # preenchidas: Purpose e Workflow (Guardrails/Evidence Policy são opcionais)
        assert set(summary["required_sections_found"]) == {"Purpose", "Workflow"}
        assert set(summary["required_sections_missing"]) == {
            "Activation Criteria", "Inputs", "Tool Bindings",
            "Output Contract", "Failure Modes",
        }

    def test_lista_de_exibicao_preservada_para_compat(self, monkeypatch):
        summary = self._get(monkeypatch).json()["summary"]
        # a lista antiga continua existindo (consumers/UI antigos não quebram)
        assert "Guardrails" in summary["sections_with_content"]
        assert "Evidence Policy" in summary["sections_with_content"]

    def test_template_mostra_zero_de_sete(self):
        """Review adversarial: `[] || fallback` curto-circuita no array vazio
        (truthy em JS) e escondia a linha justamente no '0 de 7'. O x-show
        deve ser guiado por required_sections_total, não pelo length."""
        import pathlib
        html = pathlib.Path("app/templates/pages/agent_form.html").read_text(encoding="utf-8")
        assert 'x-show="skillSummary.required_sections_total ||' in html
        assert "de 9 seções" not in html


class TestSyncFrontmatterVersion:
    """A5 + review adversarial: o sync precisa tolerar BOM, linha em branco
    inicial e CRLF (o parser tolera; o sync silenciosamente não fazia nada
    e perpetuava a divergência que dizia corrigir)."""

    def _sync(self, raw):
        from app.routes.skills import _sync_frontmatter_version
        return _sync_frontmatter_version(raw, "9.9.9")

    def test_plain(self):
        out = self._sync("---\nversion: 0.1.0\nkind: subagent\n---\n# T\n")
        assert "version: 9.9.9" in out and "0.1.0" not in out

    def test_bom_e_linha_em_branco(self):
        out = self._sync("﻿\n---\nversion: 0.1.0\n---\n# T\n")
        assert "version: 9.9.9" in out

    def test_crlf_preserva_line_endings(self):
        out = self._sync("---\r\nversion: 0.1.0\r\nkind: subagent\r\n---\r\n# T\r\n")
        assert "version: 9.9.9\r\n" in out  # \r intacto — sem endings mistos

    def test_sem_frontmatter_intacto(self):
        raw = "# Sem frontmatter\n\n## Purpose\nX."
        assert self._sync(raw) == raw

    def test_sem_linha_version_intacto(self):
        raw = "---\nkind: subagent\n---\n# T\n"
        assert self._sync(raw) == raw
