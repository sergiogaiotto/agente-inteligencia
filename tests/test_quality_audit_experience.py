"""Experiência de Auditoria na página Qualidade (PR4 do arco, 25.0.0).

- GET /dashboard/verifications: filtros agent_id/pipeline_id/with_claims_only,
  colunas novas no SELECT e nomes humanos do dono;
- GET /dashboard/verifications/stats: breakdowns by_agent/by_pipeline;
- GET /dashboard/verifications/claims: explorador de alucinações;
- POST /dashboard/verifications/{id}/rejudge: A/B de juízes (root/admin);
- GET /dashboard/verifications/export: CSV/JSONL p/ compliance;
- UI: painéis/filtros/re-julgar em quality.html + card-resumo em
  observability.html.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest


class _Con:
    def __init__(self, sink: dict, results: dict):
        self.sink = sink
        self.results = results

    def _log(self, kind, sql, args):
        self.sink.setdefault("queries", []).append((kind, sql, args))

    async def fetchval(self, sql, *a):
        self._log("fetchval", sql, a)
        return self.results.get("count", 0)

    async def fetchrow(self, sql, *a):
        self._log("fetchrow", sql, a)
        return self.results.get("row")

    async def fetch(self, sql, *a):
        self._log("fetch", sql, a)
        for key, val in self.results.items():
            if key.startswith("fetch:") and key[len("fetch:"):] in sql:
                return val
        return self.results.get("fetch_default", [])

    async def execute(self, sql, *a):
        self._log("execute", sql, a)
        return "UPDATE 0"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, sink: dict, results: dict):
        self.sink = sink
        self.results = results

    def acquire(self):
        return _Con(self.sink, self.results)


def _patch_pool(monkeypatch, sink: dict, results: dict):
    monkeypatch.setattr(
        "app.core.database._get_pool", lambda: _Pool(sink, results)
    )


# ─── Lista: filtros + colunas novas + nomes do dono ─────────────────

class TestListVerificationsAudit:
    @pytest.mark.asyncio
    async def test_filtros_por_dono_e_claims(self, monkeypatch):
        sink: dict = {}
        _patch_pool(monkeypatch, sink, {"count": 0, "fetch_default": []})
        from app.routes.dashboard import list_verifications
        out = await list_verifications(
            agent_id="ag-1", pipeline_id="pl-1", with_claims_only=True
        )
        assert out["items"] == []
        sqls = " ".join(q[1] for q in sink["queries"])
        assert "agent_id = $" in sqls
        assert "pipeline_id = $" in sqls
        assert "unsupported_claims != '[]'" in sqls

    @pytest.mark.asyncio
    async def test_select_inclui_colunas_novas_e_nomes(self, monkeypatch):
        sink: dict = {}
        row = {
            "id": "v1", "turn_id": None, "interaction_id": "i1",
            "agent_id": "ag-1", "pipeline_id": "pl-1",
            "question_redacted": "pergunta [CPF]", "draft_redacted": "resposta",
            "factuality_score": 4.0, "factuality_reason": "ok",
            "completeness_score": 5.0, "completeness_reason": "ok",
            "tone_score": 5.0, "tone_reason": "ok",
            "safety_score": 1.0, "safety_reason": "ok",
            "contract_compliant": True, "contract_errors": "[]",
            "contract_retried": True, "contract_original_errors": '["campo x"]',
            "ok": True, "confidence": 0.9, "unsupported_claims": "[]",
            "judge_model": "gpt-4o", "profile": "rigorous",
            "duration_ms": 1000, "created_at": datetime(2026, 7, 4, 12, 0),
        }
        results = {
            "count": 1,
            "fetch:FROM verifications": [row],
            "fetch:FROM agents": [{"id": "ag-1", "name": "Analista"}],
            "fetch:FROM pipelines": [{"id": "pl-1", "name": "Aurora"}],
        }
        _patch_pool(monkeypatch, sink, results)
        from app.routes.dashboard import list_verifications
        out = await list_verifications()
        item = out["items"][0]
        assert item["question_redacted"] == "pergunta [CPF]"
        assert item["contract_retried"] is True
        assert item["contract_original_errors"] == ["campo x"]
        assert item["agent_name"] == "Analista"
        assert item["pipeline_name"] == "Aurora"


# ─── Stats: breakdowns por dono ─────────────────────────────────────

class TestStatsByOwner:
    @pytest.mark.asyncio
    async def test_stats_traz_by_agent_e_by_pipeline(self, monkeypatch):
        sink: dict = {}
        results = {
            "row": {"total": 3, "ok_count": 2},
            "fetch:GROUP BY v.agent_id": [
                {"agent_id": "ag-1", "agent_name": "Analista", "n": 2,
                 "ok_count": 2, "avg_factuality": 4.25, "avg_completeness": 5.0,
                 "avg_tone": 5.0, "with_unsupported": 0},
            ],
            "fetch:GROUP BY v.pipeline_id": [
                {"pipeline_id": "pl-1", "pipeline_name": "Aurora", "n": 1,
                 "ok_count": 1, "avg_factuality": 4.0, "avg_completeness": 5.0,
                 "avg_tone": 5.0, "with_unsupported": 1},
            ],
            "fetch_default": [],
        }
        _patch_pool(monkeypatch, sink, results)
        from app.routes.dashboard import verifications_stats
        out = await verifications_stats(window="7d")
        assert out["by_agent"][0]["agent_name"] == "Analista"
        assert out["by_pipeline"][0]["with_unsupported"] == 1
        # threshold do juiz exposto p/ a UI (card "Atenção" da Observabilidade)
        assert isinstance(out["factuality_threshold"], float)
        sqls = " ".join(q[1] for q in sink["queries"])
        assert "v.agent_id IS NOT NULL" in sqls
        assert "v.pipeline_id IS NOT NULL" in sqls

    @pytest.mark.asyncio
    async def test_rejudge_fica_fora_dos_agregados(self, monkeypatch):
        """Finding HIGH da revisão 25.0.0: re-julgamentos (sem evidências
        re-anexadas) poluiriam n/ok%/alucinações do agente e o card da
        Observabilidade — induzindo o operador a 'corrigir' o que está certo."""
        sink: dict = {}
        _patch_pool(monkeypatch, sink, {"row": {"total": 0}, "fetch_default": []})
        from app.routes.dashboard import verifications_stats, verification_claims
        await verifications_stats(window="7d")
        fetchrow_sql = next(q[1] for q in sink["queries"] if q[0] == "fetchrow")
        assert "profile IS DISTINCT FROM 'rejudge'" in fetchrow_sql
        agg_sqls = " ".join(q[1] for q in sink["queries"] if "GROUP BY v." in q[1])
        assert agg_sqls.count("v.profile IS DISTINCT FROM 'rejudge'") == 2
        # explorador de claims também exclui
        sink2: dict = {}
        _patch_pool(monkeypatch, sink2, {"fetch_default": []})
        await verification_claims(window="7d")
        assert "v.profile IS DISTINCT FROM 'rejudge'" in sink2["queries"][0][1]


# ─── Explorador de alucinações ──────────────────────────────────────

class TestClaimsExplorer:
    @pytest.mark.asyncio
    async def test_achata_claims_com_dono(self, monkeypatch):
        sink: dict = {}
        results = {"fetch_default": [
            {"id": "v1", "agent_id": "ag-1", "agent_name": "Analista",
             "pipeline_id": None, "interaction_id": "i1",
             "unsupported_claims": '["limite de R$ 99.999", "cliente VIP"]',
             "created_at": datetime(2026, 7, 4, 12, 0)},
        ]}
        _patch_pool(monkeypatch, sink, results)
        from app.routes.dashboard import verification_claims
        out = await verification_claims(window="7d", limit=10)
        assert len(out["claims"]) == 2
        assert out["claims"][0]["claim"] == "limite de R$ 99.999"
        assert out["claims"][0]["agent_name"] == "Analista"


# ─── Re-julgar (A/B de juízes) ──────────────────────────────────────

def _patch_v2_on(monkeypatch, enabled: bool = True):
    from types import SimpleNamespace
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: SimpleNamespace(
            verifier_v2_enabled=enabled, verifier_factuality_threshold=3.0
        ),
    )


class TestRejudge:
    @pytest.mark.asyncio
    async def test_role_comum_recebe_403(self):
        from fastapi import HTTPException
        from app.routes.dashboard import rejudge_verification
        with pytest.raises(HTTPException) as ei:
            await rejudge_verification("v1", user={"role": "comum"})
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_v2_desligado_da_409(self, monkeypatch):
        """Finding da revisão: com v2 OFF o verify cai no _LegacyVerifier,
        que devolve veredito ENLATADO sem chamar juiz e sem persistir — a UI
        mentiria 'Re-julgado'. Fail-fast com instrução acionável."""
        from fastapi import HTTPException
        _patch_v2_on(monkeypatch, enabled=False)
        from app.routes.dashboard import rejudge_verification
        with pytest.raises(HTTPException) as ei:
            await rejudge_verification("v1", user={"role": "root"})
        assert ei.value.status_code == 409
        assert "VERIFIER_V2_ENABLED" in ei.value.detail

    @pytest.mark.asyncio
    async def test_rejudge_de_rejudge_da_409(self, monkeypatch):
        from fastapi import HTTPException
        _patch_v2_on(monkeypatch)
        sink: dict = {}
        _patch_pool(monkeypatch, sink, {"row": {
            "id": "v2", "draft_redacted": "resp", "question_redacted": "q",
            "interaction_id": "i1", "agent_id": "ag-1", "pipeline_id": None,
            "judge_model": "gpt-4o", "ok": True, "confidence": 0.9,
            "profile": "rejudge",
            "factuality_score": None, "completeness_score": 5.0,
            "tone_score": 5.0, "safety_score": 1.0,
        }})
        from app.routes.dashboard import rejudge_verification
        with pytest.raises(HTTPException) as ei:
            await rejudge_verification("v2", user={"role": "root"})
        assert ei.value.status_code == 409
        assert "ORIGINAL" in ei.value.detail

    @pytest.mark.asyncio
    async def test_sem_payload_persistido_da_400(self, monkeypatch):
        from fastapi import HTTPException
        _patch_v2_on(monkeypatch)
        sink: dict = {}
        _patch_pool(monkeypatch, sink, {"row": {
            "id": "v1", "draft_redacted": None, "question_redacted": None,
            "interaction_id": "i1", "agent_id": None, "pipeline_id": None,
            "judge_model": "x", "ok": True, "confidence": 0.5, "profile": "standard",
            "factuality_score": None, "completeness_score": None,
            "tone_score": None, "safety_score": None,
        }})
        from app.routes.dashboard import rejudge_verification
        with pytest.raises(HTTPException) as ei:
            await rejudge_verification("v1", user={"role": "root"})
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_rejudge_persiste_com_profile_rejudge(self, monkeypatch):
        _patch_v2_on(monkeypatch)
        sink: dict = {}
        _patch_pool(monkeypatch, sink, {"row": {
            "id": "v1", "draft_redacted": "resposta julgada",
            "question_redacted": "pergunta", "interaction_id": "i1",
            "agent_id": "ag-1", "pipeline_id": "pl-1",
            "judge_model": "gpt-4o", "ok": True, "confidence": 0.9, "profile": "rigorous",
            "factuality_score": 4.0, "completeness_score": 5.0,
            "tone_score": 5.0, "safety_score": 1.0,
        }})
        captured: dict = {}

        from app.verifier.runtime import VerificationResult
        import app.verifier as vpkg

        async def fake_verify(**kw):
            captured.update(kw)
            return VerificationResult(
                ok=True, confidence=0.7, judge_model="sabia-4",
                dimensions={"completeness": {"score": 4, "reason": "ok"}},
            )
        monkeypatch.setattr(vpkg.verifier, "verify", fake_verify)

        from app.routes.dashboard import rejudge_verification
        out = await rejudge_verification("v1", user={"role": "admin"})
        assert captured["profile"] == "rejudge"
        assert captured["persist"] is True
        assert captured["agent_id"] == "ag-1"
        assert captured["pipeline_id"] == "pl-1"
        assert out["original"]["judge_model"] == "gpt-4o"
        assert out["rejudged"]["judge_model"] == "sabia-4"


# ─── Export ─────────────────────────────────────────────────────────

class TestExport:
    _ROW = {
        "id": "v1", "created_at": datetime(2026, 7, 4, 12, 0),
        "interaction_id": "i1", "agent_id": "ag-1", "pipeline_id": None,
        "ok": True, "confidence": 0.9, "factuality_score": 4.0,
        "completeness_score": 5.0, "tone_score": 5.0, "safety_score": 1.0,
        "contract_compliant": True, "contract_retried": False,
        "unsupported_claims": "[]", "judge_model": "gpt-4o",
        "profile": "rigorous", "duration_ms": 900,
        "question_redacted": "pergunta", "draft_redacted": "resposta",
    }

    @pytest.mark.asyncio
    async def test_csv_com_header_bom_e_dados(self, monkeypatch):
        sink: dict = {}
        _patch_pool(monkeypatch, sink, {"fetch_default": [self._ROW]})
        from app.routes.dashboard import export_verifications
        resp = await export_verifications(format="csv", user={"role": "comum"})
        body = resp.body.decode("utf-8")
        assert resp.media_type.startswith("text/csv")
        # BOM UTF-8: sem ele o Excel abre acentos pt-BR como mojibake
        assert body.startswith("﻿")
        assert "question_redacted" in body.splitlines()[0]
        assert "gpt-4o" in body

    @pytest.mark.asyncio
    async def test_csv_neutraliza_formula_injection(self, monkeypatch):
        # célula começando com "=" viraria FÓRMULA no Excel/Sheets
        row = dict(self._ROW, draft_redacted='=HYPERLINK("http://mal.example")')
        sink: dict = {}
        _patch_pool(monkeypatch, sink, {"fetch_default": [row]})
        from app.routes.dashboard import export_verifications
        resp = await export_verifications(format="csv", user={"role": "comum"})
        body = resp.body.decode("utf-8")
        assert "'=HYPERLINK" in body

    @pytest.mark.asyncio
    async def test_export_honra_filtros_da_lista(self, monkeypatch):
        sink: dict = {}
        _patch_pool(monkeypatch, sink, {"fetch_default": []})
        from app.routes.dashboard import export_verifications
        await export_verifications(
            format="jsonl", window="7d", ok_only=True, min_factuality=3.0,
            profile="rigorous", interaction_id="i1", with_claims_only=True,
            user={"role": "comum"},
        )
        sql = sink["queries"][0][1]
        for frag in ("ok = TRUE", "factuality_score >= $", "profile = $",
                     "interaction_id = $", "unsupported_claims != '[]'",
                     "created_at > now() - interval '7 days'"):
            assert frag in sql, f"export ignorou filtro: {frag}"

    @pytest.mark.asyncio
    async def test_jsonl(self, monkeypatch):
        sink: dict = {}
        _patch_pool(monkeypatch, sink, {"fetch_default": [self._ROW]})
        from app.routes.dashboard import export_verifications
        resp = await export_verifications(format="jsonl", user={"role": "comum"})
        assert resp.media_type == "application/x-ndjson"
        assert b'"judge_model": "gpt-4o"' in resp.body


# ─── UI (invariantes de template) ───────────────────────────────────

class TestQualityUi:
    def test_quality_tem_paineis_e_filtros_novos(self):
        src = Path("app/templates/pages/quality.html").read_text(encoding="utf-8")
        for marker in ("quality-by-agent", "quality-by-pipeline",
                       "quality-filter-agent", "quality-filter-pipeline",
                       "quality-claims-explorer", "quality-export-csv",
                       "question_redacted", "draft_redacted"):
            assert marker in src, f"quality.html sem marcador: {marker}"

    def test_rejudge_gated_por_role_no_template(self):
        src = Path("app/templates/pages/quality.html").read_text(encoding="utf-8")
        idx = src.index("quality-rejudge-btn")
        gate = src.rfind("{% if user_role", 0, idx)
        assert gate != -1 and "admin" in src[gate:idx], (
            "botão Re-julgar deve estar dentro de gate root/admin"
        )

    def test_observability_tem_card_resumo_auditoria(self):
        src = Path("app/templates/pages/observability.html").read_text(encoding="utf-8")
        assert "obs-audit-card" in src
        assert "Abrir Qualidade" in src
        assert "verifications/stats?window=24h" in src
        # guard de amostra pequena + threshold real (não inventado na UI) +
        # deep-link filtrado por agente
        assert "a.n >= 3" in src
        assert "factuality_threshold" in src
        assert "/quality?agent_id=" in src

    def test_paineis_por_dono_clicam_para_filtrar(self):
        src = Path("app/templates/pages/quality.html").read_text(encoding="utf-8")
        blk_a = src[src.index("quality-by-agent"):src.index("quality-by-pipeline")]
        assert "filterAgentId" in blk_a and "load()" in blk_a
        blk_p = src[src.index("quality-by-pipeline"):src.index("quality-claims-explorer")]
        assert "filterPipelineId" in blk_p and "load()" in blk_p

    def test_export_url_espelha_todos_os_filtros(self):
        src = Path("app/templates/pages/quality.html").read_text(encoding="utf-8")
        body = src[src.index("exportUrl(fmt)"):src.index("viewClaim(c) {")]
        for f in ("agent_id", "pipeline_id", "ok_only", "min_factuality",
                  "profile", "interaction_id", "with_claims_only", "window"):
            assert f in body, f"exportUrl sem filtro: {f}"

    def test_glifo_de_alucinacao_consistente(self):
        src = Path("app/templates/pages/quality.html").read_text(encoding="utf-8")
        assert "⚠ c/ alucinação" not in src
        assert "⚑ c/ alucinação" in src
