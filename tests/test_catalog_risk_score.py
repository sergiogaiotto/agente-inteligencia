"""Testes dos helpers de risk score, compliance matcher e alertas.

Cobre: cálculo do score (limites, drivers, banding), cada regulação
(LGPD, GDPR, HIPAA, Marco Civil) com cenários positivos e negativos,
todos os alertas individualmente.

Todos os helpers são puros (sem I/O), então os testes são unit puros —
sem fixtures de DB, sem monkeypatch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.catalog.risk_score import (
    compute_alerts,
    compute_compliance,
    compute_risk_score,
)


# ─── Risk Score ─────────────────────────────────────────────────────


class TestRiskScore:
    def test_disclosure_none_retorna_zero(self):
        r = compute_risk_score(None)
        assert r["score"] == 0
        assert r["band"] == "low"
        assert r["breakdown"] == {}
        assert r["drivers"] == []

    def test_disclosure_vazia_retorna_zero(self):
        r = compute_risk_score({})
        assert r["score"] == 0
        assert r["band"] == "low"

    def test_entry_sem_flags_ativas_score_zero(self):
        r = compute_risk_score({
            "processes_pii": False,
            "calls_external_apis": False,
        })
        assert r["score"] == 0
        assert r["band"] == "low"

    def test_pii_sozinho_pesa_20(self):
        r = compute_risk_score({"processes_pii": True})
        assert r["score"] == 20
        assert r["band"] == "low"  # 20 ainda é low (boundary 30)
        assert "processes_pii" in r["breakdown"]

    def test_banding_low_medium_high(self):
        # 30 = ainda low (boundary inclusivo)
        assert compute_risk_score({"processes_pii": True, "calls_external_apis": True})["band"] == "low"  # 30
        # 35 = médio
        r = compute_risk_score({"processes_pii": True, "calls_external_apis": True, "trains_on_input": True})  # 40
        assert r["band"] == "medium"
        # 70 = alto (PII + financial + health = 60, + ext_apis = 70)
        r = compute_risk_score({
            "processes_pii": True, "processes_financial": True, "processes_health": True,
            "calls_external_apis": True,
        })
        assert r["score"] == 70
        assert r["band"] == "high"

    def test_score_capped_em_100(self):
        # Liga TUDO — soma teórica > 100
        r = compute_risk_score({
            "processes_pii": True, "processes_financial": True, "processes_health": True,
            "calls_external_apis": True, "accesses_internet": True, "trains_on_input": True,
            "stores_input": True, "writes_user_kb": True, "reads_user_kb": True,
            "storage_retention_days": None,  # sem retention = +5 penalty
        })
        assert r["score"] == 100  # clamped

    def test_storage_sem_retention_aplica_penalidade(self):
        sem = compute_risk_score({"processes_pii": True, "stores_input": True})
        com = compute_risk_score({"processes_pii": True, "stores_input": True, "storage_retention_days": 30})
        assert sem["score"] > com["score"]
        assert "__no_retention_penalty__" in sem["breakdown"]
        assert "__no_retention_penalty__" not in com["breakdown"]

    def test_storage_com_retention_longa_aplica_penalidade(self):
        """Retention > 365 dias = mesmo que sem retention."""
        r = compute_risk_score({"processes_pii": True, "stores_input": True, "storage_retention_days": 400})
        assert "__no_retention_penalty__" in r["breakdown"]

    def test_output_deterministico_reduz_score(self):
        sem_bonus = compute_risk_score({"processes_pii": True})
        com_bonus = compute_risk_score({"processes_pii": True, "output_is_deterministic": True})
        assert com_bonus["score"] < sem_bonus["score"]
        assert "__deterministic_bonus__" in com_bonus["breakdown"]

    def test_deterministico_nao_da_pontos_de_graca(self):
        """Sem nenhuma flag de risco, deterministic NÃO reduz score abaixo de 0."""
        r = compute_risk_score({"output_is_deterministic": True})
        assert r["score"] == 0
        # E bonus nem entra no breakdown porque não tinha risco
        assert "__deterministic_bonus__" not in r["breakdown"]

    def test_drivers_sao_top_3_flags_positivas(self):
        r = compute_risk_score({
            "processes_pii": True,        # 20
            "processes_health": True,     # 20
            "calls_external_apis": True,  # 10
            "reads_user_kb": True,        # 2 (não vai para top 3)
        })
        # Top 3 por peso — empate em 20 e 20 fica por ordem do dict (Python 3.7+)
        assert len(r["drivers"]) == 3
        assert "processes_pii" in r["drivers"]
        assert "processes_health" in r["drivers"]
        assert "calls_external_apis" in r["drivers"]
        assert "reads_user_kb" not in r["drivers"]


# ─── Compliance matcher ────────────────────────────────────────────


class TestComplianceMatcher:
    def test_disclosure_none_tudo_false_com_razao_clara(self):
        c = compute_compliance(None)
        for key in ("lgpd", "lgpd_sensitive", "gdpr", "hipaa", "marco_civil"):
            assert c[key]["applies"] is False
            assert "não declarada" in c[key]["reason"].lower()

    def test_lgpd_aplica_quando_processa_pii(self):
        c = compute_compliance({"processes_pii": True})
        assert c["lgpd"]["applies"] is True
        assert "PII" in c["lgpd"]["reason"]

    def test_lgpd_aplica_quando_processa_financeiro(self):
        c = compute_compliance({"processes_financial": True})
        assert c["lgpd"]["applies"] is True

    def test_lgpd_nao_aplica_sem_dados_pessoais(self):
        c = compute_compliance({
            "processes_pii": False, "processes_financial": False, "processes_health": False,
            "accesses_internet": True,  # internet sozinha não dispara LGPD
        })
        assert c["lgpd"]["applies"] is False

    def test_lgpd_sensitive_aplica_para_saude(self):
        c = compute_compliance({"processes_health": True})
        assert c["lgpd_sensitive"]["applies"] is True
        assert "art. 11" in c["lgpd_sensitive"]["reason"]

    def test_lgpd_sensitive_nao_aplica_para_pii_comum(self):
        """PII genérico (CPF, email) NÃO é sensível por si só — art. 11 é específico."""
        c = compute_compliance({"processes_pii": True, "processes_health": False})
        assert c["lgpd_sensitive"]["applies"] is False

    def test_gdpr_aplica_quando_residency_eu(self):
        c = compute_compliance({"data_residency": "EU", "processes_pii": True})
        assert c["gdpr"]["applies"] is True
        assert "EU" in c["gdpr"]["reason"]

    def test_gdpr_aplica_por_precaucao_quando_pii_sem_residency(self):
        """Cautela: PII sem residency → assume GDPR até comitê confirmar."""
        c = compute_compliance({"processes_pii": True, "data_residency": None})
        assert c["gdpr"]["applies"] is True
        assert "precaução" in c["gdpr"]["reason"]

    def test_gdpr_nao_aplica_residency_br_com_pii(self):
        c = compute_compliance({"processes_pii": True, "data_residency": "BR"})
        assert c["gdpr"]["applies"] is False

    def test_hipaa_aplica_saude_em_us(self):
        c = compute_compliance({"processes_health": True, "data_residency": "US"})
        assert c["hipaa"]["applies"] is True

    def test_hipaa_nao_aplica_saude_em_br(self):
        c = compute_compliance({"processes_health": True, "data_residency": "BR"})
        assert c["hipaa"]["applies"] is False

    def test_marco_civil_aplica_quando_acessa_internet(self):
        c = compute_compliance({"accesses_internet": True})
        assert c["marco_civil"]["applies"] is True
        assert "art. 13" in c["marco_civil"]["reason"]

    def test_marco_civil_nao_aplica_sem_internet(self):
        c = compute_compliance({"accesses_internet": False, "calls_external_apis": True})
        # APIs externas via connector NÃO disparam Marco Civil (são uso, não acesso aberto)
        assert c["marco_civil"]["applies"] is False


# ─── Alertas automáticos ───────────────────────────────────────────


class TestAlerts:
    def test_disclosure_none_gera_alerta_warning(self):
        alerts = compute_alerts({}, None)
        assert len(alerts) == 1
        assert alerts[0]["code"] == "disclosure_missing"
        assert alerts[0]["severity"] == "warning"

    def test_disclosure_completa_sem_riscos_nao_gera_alertas(self):
        alerts = compute_alerts({}, {
            "processes_pii": False, "processes_financial": False, "processes_health": False,
            "stores_input": False, "calls_external_apis": False, "accesses_internet": False,
            "trains_on_input": False, "output_is_deterministic": True,
        })
        assert alerts == []

    def test_treina_com_pii_alerta_danger(self):
        alerts = compute_alerts({}, {"trains_on_input": True, "processes_pii": True})
        codes = [a["code"] for a in alerts]
        assert "trains_on_sensitive" in codes
        # Severity danger
        ts = next(a for a in alerts if a["code"] == "trains_on_sensitive")
        assert ts["severity"] == "danger"

    def test_treina_com_saude_alerta_danger(self):
        alerts = compute_alerts({}, {"trains_on_input": True, "processes_health": True})
        assert any(a["code"] == "trains_on_sensitive" for a in alerts)

    def test_treina_sem_dados_sensiveis_nao_alerta(self):
        alerts = compute_alerts({}, {"trains_on_input": True, "processes_pii": False})
        codes = [a["code"] for a in alerts]
        assert "trains_on_sensitive" not in codes

    def test_pii_armazenada_sem_retention_alerta_danger(self):
        alerts = compute_alerts({}, {
            "processes_pii": True, "stores_input": True, "storage_retention_days": None,
        })
        assert any(a["code"] == "pii_stored_without_retention" for a in alerts)

    def test_pii_armazenada_com_retention_zero_alerta_danger(self):
        """retention=0 conta como 'sem definir' — não é 0 dias literal."""
        alerts = compute_alerts({}, {
            "processes_pii": True, "stores_input": True, "storage_retention_days": 0,
        })
        assert any(a["code"] == "pii_stored_without_retention" for a in alerts)

    def test_retention_longa_dados_sensiveis_warning(self):
        alerts = compute_alerts({}, {
            "processes_pii": True, "stores_input": True, "storage_retention_days": 730,
        })
        codes = [a["code"] for a in alerts]
        assert "long_retention_sensitive" in codes

    def test_apis_externas_sem_lista_warning(self):
        alerts = compute_alerts({}, {"calls_external_apis": True, "external_apis_list": []})
        assert any(a["code"] == "external_apis_undeclared" for a in alerts)

    def test_apis_externas_com_lista_nao_alerta(self):
        alerts = compute_alerts({}, {
            "calls_external_apis": True, "external_apis_list": ["https://stripe.com"],
        })
        codes = [a["code"] for a in alerts]
        assert "external_apis_undeclared" not in codes

    def test_pii_sem_residency_warning(self):
        alerts = compute_alerts({}, {"processes_pii": True, "data_residency": None})
        assert any(a["code"] == "pii_without_residency" for a in alerts)

    def test_internet_aberta_com_pii_warning(self):
        alerts = compute_alerts({}, {"accesses_internet": True, "processes_pii": True})
        assert any(a["code"] == "internet_with_sensitive" for a in alerts)

    def test_external_platform_sem_vendor_warning(self):
        alerts = compute_alerts(
            {"kind": "external_platform"},
            {"processes_pii": False},
            external_metadata={},
        )
        assert any(a["code"] == "external_platform_no_vendor" for a in alerts)

    def test_external_platform_vendor_sem_contrato_info(self):
        alerts = compute_alerts(
            {"kind": "external_platform"},
            {"processes_pii": False},
            external_metadata={"vendor": "ChatGPT", "contract_status": None},
        )
        codes = [a["code"] for a in alerts]
        assert "external_platform_no_contract" in codes
        evt = next(a for a in alerts if a["code"] == "external_platform_no_contract")
        assert evt["severity"] == "info"

    def test_entry_published_sem_invocacao_recente_info(self):
        last = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        alerts = compute_alerts(
            {"status": "published", "last_invoked_at": last},
            {"processes_pii": False},
        )
        codes = [a["code"] for a in alerts]
        assert "stale_entry" in codes

    def test_entry_published_invocacao_recente_nao_alerta_stale(self):
        last = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        alerts = compute_alerts(
            {"status": "published", "last_invoked_at": last},
            {"processes_pii": False},
        )
        codes = [a["code"] for a in alerts]
        assert "stale_entry" not in codes

    def test_alertas_ordenados_por_severidade(self):
        """Lista vem com danger primeiro, depois warning, depois info."""
        alerts = compute_alerts(
            {"kind": "external_platform"},
            {
                "processes_pii": True, "trains_on_input": True,  # → danger trains_on_sensitive
                "stores_input": True, "storage_retention_days": None,  # → danger pii_stored_without_retention
                "calls_external_apis": True, "external_apis_list": [],  # → warning external_apis_undeclared
            },
            external_metadata={"vendor": "Foo"},  # → info external_platform_no_contract
        )
        severities = [a["severity"] for a in alerts]
        # Cada nível agrupado
        danger_idx = [i for i, s in enumerate(severities) if s == "danger"]
        warning_idx = [i for i, s in enumerate(severities) if s == "warning"]
        info_idx = [i for i, s in enumerate(severities) if s == "info"]
        if danger_idx and warning_idx:
            assert max(danger_idx) < min(warning_idx)
        if warning_idx and info_idx:
            assert max(warning_idx) < min(info_idx)

    def test_alertas_resilientes_a_last_invoked_invalido(self):
        """last_invoked_at em formato bizarro NÃO derruba — só pula o check."""
        alerts = compute_alerts(
            {"status": "published", "last_invoked_at": "not-a-date-at-all"},
            {"processes_pii": False},
        )
        # Não levanta exception; lista contém os alertas válidos (vazio neste caso)
        assert isinstance(alerts, list)
