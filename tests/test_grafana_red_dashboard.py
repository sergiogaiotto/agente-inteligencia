"""Dashboard RED do Grafana (35.13.0, médios do roadmap) + fix do footgun de
provisioning: dashboards moravam em /var/lib/grafana/dashboards, SOMBREADO
pelo volume nomeado grafana_data — JSON novo baked na imagem NUNCA aparecia
em rebuild (Docker só popula volume nomeado no 1º uso). Agora vivem em
/etc/grafana/dashboards (não-sombreado) e o provider aponta pra lá.

Decisão registrada: OTel MeterProvider NÃO entra — duplicaria as séries do
prometheus-client (fonte dos alerts.yml) com convenção de nomes OTel,
quebrando alertas e continuidade. Traces=OTel→Tempo; métricas=prometheus.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def _dash():
    return json.loads(Path("infra/grafana/dashboards/red.json").read_text(encoding="utf-8"))


class TestRedDashboard:
    def test_json_valido_com_uid_estavel(self):
        d = _dash()
        assert d["uid"] == "agente-red" and d["id"] is None
        assert len(d["panels"]) == 8

    def test_todas_as_queries_usam_series_reais(self):
        """Toda expr referencia métricas que app/core/metrics.py DE FATO expõe
        — um rename de métrica quebra este teste antes de quebrar o dashboard."""
        metrics_src = Path("app/core/metrics.py").read_text(encoding="utf-8")
        d = _dash()
        exprs = [t["expr"] for p in d["panels"] for t in p.get("targets", [])]
        assert exprs
        for expr in exprs:
            for serie in set(re.findall(r"maestro_[a-z_]+", expr)):
                base = serie.replace("_bucket", "")
                assert base in metrics_src, f"série {serie} não existe em metrics.py"

    def test_datasource_uid_provisionado(self):
        d = _dash()
        ds = Path("infra/grafana/provisioning/datasources/datasources.yaml").read_text(encoding="utf-8")
        assert "uid: prometheus" in ds
        for p in d["panels"]:
            assert p["datasource"]["uid"] == "prometheus"

    def test_footgun_do_volume_corrigido(self):
        dockerfile = Path("infra/grafana/Dockerfile").read_text(encoding="utf-8")
        provider = Path("infra/grafana/provisioning/dashboards/dashboards.yaml").read_text(encoding="utf-8")
        # dashboards fora do path sombreado pelo volume grafana_data
        assert "COPY dashboards /etc/grafana/dashboards" in dockerfile
        assert "COPY dashboards /var/lib/grafana/dashboards" not in dockerfile
        assert "path: /etc/grafana/dashboards" in provider

    def test_paleta_sem_roxo(self):
        raw = Path("infra/grafana/dashboards/red.json").read_text(encoding="utf-8").lower()
        assert "purple" not in raw and "violet" not in raw
