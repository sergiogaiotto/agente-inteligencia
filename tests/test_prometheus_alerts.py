"""Onda 6 — regras de alerta do Prometheus + fiação do Alertmanager.

O #565 deu as métricas RED; faltava o ALERTA. Estes testes garantem que as
regras existem, referenciam métricas REAIS (nome typo'd = alerta que nunca
dispara), e que Prometheus↔Alertmanager estão fiados. Sem ferramentas externas
(promtool) — parsing puro, roda no CI hermético.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_ALERTS = _ROOT / "infra" / "prometheus" / "alerts.yml"
_PROM = _ROOT / "infra" / "prometheus" / "prometheus.yml"
_AM = _ROOT / "infra" / "alertmanager" / "alertmanager.yml"
_METRICS = _ROOT / "app" / "core" / "metrics.py"

_EXPECTED_ALERTS = {
    "MaestroAppDown",
    "MaestroHighErrorRate",
    "MaestroHighLatencyP99",
    "MaestroLLMBreakerOpen",
    "MaestroRefusalSpike",
}

# Métricas do app referenciadas pelas regras (base, sem sufixo _bucket).
_REFERENCED_METRICS = {
    "maestro_invocations_total",
    "maestro_invocation_errors_total",
    "maestro_invocation_duration_seconds",
    "maestro_circuit_breaker_opens_total",
    "maestro_refusals_total",
}


class TestAlertRules:
    def test_arquivos_existem(self):
        for p in (_ALERTS, _PROM, _AM):
            assert p.exists(), f"faltando {p}"

    def test_todos_os_alertas_definidos(self):
        txt = _ALERTS.read_text(encoding="utf-8")
        for name in _EXPECTED_ALERTS:
            assert f"alert: {name}" in txt, f"regra ausente: {name}"

    def test_metricas_referenciadas_sao_reais(self):
        """Um nome de métrica typo'd numa regra = alerta que NUNCA dispara.
        Cada métrica citada nas regras tem de existir em app/core/metrics.py."""
        alerts_txt = _ALERTS.read_text(encoding="utf-8")
        metrics_src = _METRICS.read_text(encoding="utf-8")
        for m in _REFERENCED_METRICS:
            assert m in alerts_txt, f"regra não referencia {m}"
            assert f'"{m}"' in metrics_src, f"{m} citada na regra mas NÃO definida em metrics.py"

    def test_yaml_valido_e_estruturado(self):
        yaml = pytest.importorskip("yaml")
        doc = yaml.safe_load(_ALERTS.read_text(encoding="utf-8"))
        groups = doc.get("groups") or []
        assert groups, "alerts.yml sem groups"
        rules = [r for g in groups for r in (g.get("rules") or [])]
        names = {r.get("alert") for r in rules}
        assert _EXPECTED_ALERTS <= names
        for r in rules:
            assert r.get("expr"), f"{r.get('alert')} sem expr"
            assert (r.get("labels") or {}).get("severity") in ("critical", "warning"), \
                f"{r.get('alert')} sem severity válida"
            assert (r.get("annotations") or {}).get("summary"), f"{r.get('alert')} sem summary"

    def test_severidades(self):
        """AppDown é crítico (o resto warning) — sanity da classificação."""
        yaml = pytest.importorskip("yaml")
        doc = yaml.safe_load(_ALERTS.read_text(encoding="utf-8"))
        by_name = {r["alert"]: r for g in doc["groups"] for r in g["rules"]}
        assert by_name["MaestroAppDown"]["labels"]["severity"] == "critical"


class TestWiring:
    def test_prometheus_aponta_para_regras_e_alertmanager(self):
        txt = _PROM.read_text(encoding="utf-8")
        assert "rule_files:" in txt
        assert "/etc/prometheus/alerts.yml" in txt
        assert "alerting:" in txt
        assert "alertmanager:9093" in txt

    def test_prometheus_dockerfile_copia_regras(self):
        df = (_ROOT / "infra" / "prometheus" / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY alerts.yml" in df

    def test_alertmanager_route_tem_receiver_valido(self):
        yaml = pytest.importorskip("yaml")
        doc = yaml.safe_load(_AM.read_text(encoding="utf-8"))
        recv = (doc.get("route") or {}).get("receiver")
        assert recv, "route sem receiver"
        names = {r.get("name") for r in (doc.get("receivers") or [])}
        assert recv in names, f"receiver '{recv}' da route não existe em receivers {names}"

    def test_compose_tem_servico_alertmanager(self):
        compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        assert "alertmanager:" in compose
        assert "agente-alertmanager:local" in compose
        assert "alertmanager_data:" in compose
        assert re.search(r"profiles:\s*\[\"full\"\]", compose)  # sobe só no profile full
