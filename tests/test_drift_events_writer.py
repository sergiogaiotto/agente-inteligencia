"""33.11.0 — writer de `drift_events` (o PRODUTOR que faltava; a tabela era morta).

Cobre a comparação release-over-release por `gold_hash`: severidade orientada
pela direção da métrica, guarda de ruído, incomparáveis puladas, e os curtos-
circuitos (sem gold_hash / sem baseline).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.harness import evaluator


class _FakeEvalRepo:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[dict] = []

    async def find_all(self, **kw):
        self.calls.append(kw)
        return self.rows


class _FakeDriftRepo:
    def __init__(self):
        self.created: list[dict] = []

    async def create(self, data):
        self.created.append(data)


def _patch(monkeypatch, baseline_row):
    ev = _FakeEvalRepo([baseline_row] if baseline_row is not None else [])
    dr = _FakeDriftRepo()
    monkeypatch.setattr(evaluator, "eval_runs_repo", ev)
    monkeypatch.setattr(evaluator, "drift_repo", dr)
    return ev, dr


class TestDriftWriter:
    @pytest.mark.asyncio
    async def test_regressao_higher_better_vira_critical(self, monkeypatch):
        _, dr = _patch(monkeypatch, {"accuracy": 0.90})
        n = await evaluator._write_drift_events(
            "rel-1", "h1", {"accuracy": 0.70}, regression_pct_threshold=10.0,
        )
        assert n == 1
        e = dr.created[0]
        assert e["metric_name"] == "accuracy"
        assert e["severity"] == "critical"          # queda ~22% > 10%
        assert e["baseline_value"] == 0.9
        assert e["current_value"] == 0.7
        assert e["magnitude"] == -0.2               # delta cru (cur - base)
        assert e["detection_method"] == "harness_baseline_delta"
        assert e["release_id"] == "rel-1"

    @pytest.mark.asyncio
    async def test_evento_registra_o_alvo(self, monkeypatch):
        """35.1.0: o baseline já era filtrado por alvo — agora o EVENTO também
        declara de quem é o drift (agent_id XOR pipeline_id)."""
        ev, dr = _patch(monkeypatch, {"accuracy": 0.90})
        await evaluator._write_drift_events(
            "rel-1", "h1", {"accuracy": 0.70}, regression_pct_threshold=10.0,
            pipeline_id="p1",
        )
        assert dr.created[0]["pipeline_id"] == "p1"
        assert dr.created[0]["agent_id"] is None
        # e o baseline foi buscado pelo MESMO alvo
        assert ev.calls[0].get("pipeline_id") == "p1"

    def test_colunas_no_schema_e_migracao(self):
        from app.core.database import SCHEMA, _IDEMPOTENT_MIGRATIONS
        assert "agent_id TEXT" in SCHEMA.split("drift_events")[1].split(";")[0]
        migs = "\n".join(_IDEMPOTENT_MIGRATIONS)
        assert "ALTER TABLE drift_events ADD COLUMN IF NOT EXISTS agent_id TEXT" in migs
        assert "ALTER TABLE drift_events ADD COLUMN IF NOT EXISTS pipeline_id TEXT" in migs

    def test_endpoint_filtra_por_alvo(self):
        src = Path("app/routes/dashboard.py").read_text(encoding="utf-8")
        assert 'if agent_id: f["agent_id"] = agent_id' in src
        assert 'if pipeline_id: f["pipeline_id"] = pipeline_id' in src

    @pytest.mark.asyncio
    async def test_regressao_pequena_vira_warning(self, monkeypatch):
        _, dr = _patch(monkeypatch, {"accuracy": 0.80})
        await evaluator._write_drift_events(
            "rel-1", "h1", {"accuracy": 0.76}, regression_pct_threshold=25.0,
        )
        assert dr.created[0]["severity"] == "warning"  # 5% < 25%

    @pytest.mark.asyncio
    async def test_melhora_vira_info(self, monkeypatch):
        _, dr = _patch(monkeypatch, {"accuracy": 0.70})
        await evaluator._write_drift_events(
            "rel-1", "h1", {"accuracy": 0.90}, regression_pct_threshold=10.0,
        )
        assert dr.created[0]["severity"] == "info"
        assert dr.created[0]["magnitude"] == 0.2

    @pytest.mark.asyncio
    async def test_dentro_do_ruido_nao_registra(self, monkeypatch):
        _, dr = _patch(monkeypatch, {"accuracy": 0.900})
        n = await evaluator._write_drift_events(
            "rel-1", "h1", {"accuracy": 0.898}, regression_pct_threshold=10.0,
        )
        assert n == 0 and dr.created == []  # <1% = ruído

    @pytest.mark.asyncio
    async def test_metrica_lower_better_direcao_correta(self, monkeypatch):
        # hallucination_rate SOBE = pior (lower_is_better).
        _, dr = _patch(monkeypatch, {"hallucination_rate": 0.05})
        await evaluator._write_drift_events(
            "rel-1", "h1", {"hallucination_rate": 0.10}, regression_pct_threshold=200.0,
        )
        e = dr.created[0]
        assert e["metric_name"] == "hallucination_rate"
        assert e["severity"] == "warning"     # +100% adverso, < 200%
        assert e["magnitude"] == 0.05         # subiu (delta positivo)

    @pytest.mark.asyncio
    async def test_incomparavel_pulada(self, monkeypatch):
        # baseline sem a dimensão (None) → métrica pulada, sem evento.
        _, dr = _patch(monkeypatch, {"accuracy": 0.9, "avg_factuality": None})
        await evaluator._write_drift_events(
            "rel-1", "h1", {"avg_factuality": 4.0}, regression_pct_threshold=10.0,
        )
        assert all(e["metric_name"] != "avg_factuality" for e in dr.created)

    @pytest.mark.asyncio
    async def test_sem_gold_hash_curto_circuita(self, monkeypatch):
        ev, dr = _patch(monkeypatch, {"accuracy": 0.9})
        n = await evaluator._write_drift_events("rel-1", "", {"accuracy": 0.1}, 10.0)
        assert n == 0
        assert ev.calls == []  # nem consulta o baseline

    @pytest.mark.asyncio
    async def test_sem_baseline_zero_eventos(self, monkeypatch):
        _, dr = _patch(monkeypatch, None)  # find_all → []
        n = await evaluator._write_drift_events("rel-1", "h1", {"accuracy": 0.1}, 10.0)
        assert n == 0 and dr.created == []

    def test_run_evaluation_chama_o_writer(self):
        src = Path("app/harness/evaluator.py").read_text(encoding="utf-8")
        assert "await _write_drift_events(" in src
        assert "gold_hash=gold_hash" in src


class TestDriftFrasesProva:
    """36.6.0 — routing_phrase_pass_rate no drift, com guarda de HASH própria:
    pass-rate só se compara quando o CONJUNTO de frases é o mesmo
    (routing_phrases_hash igual nos dois lados). Métrica derivada
    (passed/total), fora do loop genérico de _DRIFT_METRICS."""

    _BASE = {
        "routing_phrases_total": 4, "routing_phrases_passed": 4,
        "routing_phrases_hash": "h-frases",
    }
    _CUR = {"routing_phrase_pass_rate": 0.5, "routing_phrases_hash": "h-frases"}

    @pytest.mark.asyncio
    async def test_mesmo_hash_queda_vira_critical(self, monkeypatch):
        _, dr = _patch(monkeypatch, dict(self._BASE))
        n = await evaluator._write_drift_events(
            "rel-1", "h1", dict(self._CUR), regression_pct_threshold=10.0,
            pipeline_id="p1",
        )
        assert n == 1
        e = dr.created[0]
        assert e["metric_name"] == "routing_phrase_pass_rate"
        assert e["severity"] == "critical"       # 1.0 → 0.5 = queda 50% > 10%
        assert e["baseline_value"] == 1.0 and e["current_value"] == 0.5
        assert e["pipeline_id"] == "p1"

    @pytest.mark.asyncio
    async def test_hash_diferente_nao_compara(self, monkeypatch):
        _, dr = _patch(monkeypatch, dict(self._BASE))
        n = await evaluator._write_drift_events(
            "rel-1", "h1",
            dict(self._CUR, routing_phrases_hash="OUTRO-conjunto"),
            regression_pct_threshold=10.0,
        )
        assert n == 0 and dr.created == []

    @pytest.mark.asyncio
    async def test_baseline_sem_frases_nao_compara(self, monkeypatch):
        # baseline pré-36.5.0 (colunas NULL) ou modo agente → sem referência.
        _, dr = _patch(monkeypatch, {"routing_phrases_hash": "h-frases"})
        n = await evaluator._write_drift_events(
            "rel-1", "h1", dict(self._CUR), regression_pct_threshold=10.0,
        )
        assert n == 0

    @pytest.mark.asyncio
    async def test_run_atual_sem_frases_nao_compara(self, monkeypatch):
        _, dr = _patch(monkeypatch, dict(self._BASE))
        n = await evaluator._write_drift_events(
            "rel-1", "h1",
            {"routing_phrase_pass_rate": None, "routing_phrases_hash": None},
            regression_pct_threshold=10.0,
        )
        assert n == 0

    @pytest.mark.asyncio
    async def test_melhora_vira_info(self, monkeypatch):
        _, dr = _patch(monkeypatch, dict(self._BASE, routing_phrases_passed=2))
        await evaluator._write_drift_events(
            "rel-1", "h1", dict(self._CUR, routing_phrase_pass_rate=1.0),
            regression_pct_threshold=10.0,
        )
        assert dr.created[0]["severity"] == "info"  # 0.5 → 1.0


class TestPhrasesPassRate:
    def test_derivacao(self):
        assert evaluator._phrases_pass_rate(4, 3) == 0.75
        assert evaluator._phrases_pass_rate(0, 0) is None   # avaliou, sem frases
        assert evaluator._phrases_pass_rate(None, None) is None  # N/A
        assert evaluator._phrases_pass_rate("x", 1) is None  # lixo → None
        assert evaluator._phrases_pass_rate(4, "x") is None  # lixo em passed idem
