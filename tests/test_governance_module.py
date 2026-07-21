"""Módulo IA Responsável (56.0.0) + perfil de usuário "Governança".

Cobre: (1) o RBAC — governanca herda TODOS os poderes de Admin (onde admin é
permitido) e Root segue à parte; (2) a lógica da rota de governança (postura
computada das flags reais, contagem de eventos de segurança); (3) o registro
da página e o markup do módulo + o novo perfil nos dropdowns.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

_PAGES = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages"
_BASE = Path(__file__).resolve().parent.parent / "app" / "templates" / "layouts" / "base.html"


# ─── RBAC: Governança herda Admin ────────────────────────────────────────────
class TestRoleGovernancaHerdaAdmin:
    @pytest.mark.asyncio
    async def test_governanca_passa_onde_admin_passa(self, monkeypatch):
        import app.core.auth as A

        async def fake(_req):
            return {"role": "governanca"}
        monkeypatch.setattr(A, "require_user", fake)
        dep = A.require_role("root", "admin")
        user = await dep(None)
        assert user["role"] == "governanca"

    @pytest.mark.asyncio
    async def test_comum_barrado(self, monkeypatch):
        import app.core.auth as A

        async def fake(_req):
            return {"role": "comum"}
        monkeypatch.setattr(A, "require_user", fake)
        with pytest.raises(HTTPException) as e:
            await A.require_role("root", "admin")(None)
        assert e.value.status_code == 403

    @pytest.mark.asyncio
    async def test_governanca_NAO_ganha_rota_root_only(self, monkeypatch):
        # onde só root é permitido (sem admin), governanca não entra.
        import app.core.auth as A

        async def fake(_req):
            return {"role": "governanca"}
        monkeypatch.setattr(A, "require_user", fake)
        with pytest.raises(HTTPException) as e:
            await A.require_role("root")(None)
        assert e.value.status_code == 403

    def test_is_privileged_inclui_governanca(self):
        from app.routes.users import _is_privileged
        assert _is_privileged({"role": "governanca"})
        assert _is_privileged({"role": "admin"})
        assert not _is_privileged({"role": "comum"})


# ─── Rota de governança ──────────────────────────────────────────────────────
class FakeAudit:
    def __init__(self, rows):
        self.rows = rows

    async def find_all(self, limit=100, offset=0, **f):
        return self.rows[:limit]


class FakeSettings:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class TestPosture:
    def test_opa_desligado_derruba_seguranca(self, monkeypatch):
        import app.routes.governance as G
        fake = FakeSettings(prompt_guard_enabled=True, grounding_strict=True,
                            circuit_breaker_enabled=True, opa_enabled=False,
                            dlp_enabled=True, verifier_v2_enabled=False,
                            interactions_retention_days=0)
        monkeypatch.setattr(G, "get_settings", lambda: fake)
        _, pillars = G._posture()
        seg = next(p for p in pillars if p["pillar"] == "Segurança")
        assert seg["pct"] == 75  # 3 de 4 (OPA off)

    def test_score_e_media_dos_pilares(self, monkeypatch):
        import app.routes.governance as G
        score, pillars = G._posture()
        assert 0 <= score <= 100
        assert {p["pillar"] for p in pillars} == {
            "Privacidade", "Segurança", "Transparência", "Robustez", "Auditabilidade"}


class TestGovernanceEndpoints:
    @pytest.mark.asyncio
    async def test_summary_traz_pilares_e_capacidades(self):
        import app.routes.governance as G
        r = await G.governance_summary(user={"role": "root"})
        assert "posture_score" in r and "pillars" in r
        assert "opa_enabled" in r["capabilities"]

    @pytest.mark.asyncio
    async def test_security_events_conta_por_tipo(self, monkeypatch):
        import app.routes.governance as G
        monkeypatch.setattr(G, "audit_repo", FakeAudit([
            {"id": 3, "action": "prompt_injection_blocked", "actor": "x"},
            {"id": 2, "action": "state_transition:VerifyEvidence->Refuse", "actor": "y"},
            {"id": 1, "action": "state_transition:Draft->Escalate", "actor": "z"},
            {"id": 0, "action": "user_login", "actor": "w"},
        ]))
        r = await G.governance_security_events(limit=10, user={"role": "governanca"})
        assert r["counts"]["injecoes_bloqueadas"] == 1
        assert r["counts"]["recusas"] == 1
        assert r["counts"]["escalonamentos"] == 1
        assert len(r["events"]) == 3  # o user_login não é de segurança

    @pytest.mark.asyncio
    async def test_audit_ordena_recentes_primeiro(self, monkeypatch):
        import app.routes.governance as G
        monkeypatch.setattr(G, "audit_repo", FakeAudit([
            {"id": 1, "action": "a"}, {"id": 5, "action": "b"}, {"id": 3, "action": "c"},
        ]))
        r = await G.governance_audit(limit=10, user={"role": "admin"})
        assert [e["id"] for e in r["events"]] == [5, 3, 1]


# ─── Registro da página + markup ─────────────────────────────────────────────
class TestPageAndMarkup:
    def test_pagina_registrada(self):
        from app.routes.frontend import PAGES, _GOVERNANCE_ROLES
        assert "/ia-responsavel" in PAGES
        assert PAGES["/ia-responsavel"]["template"] == "pages/ia_responsavel.html"
        assert "governanca" in _GOVERNANCE_ROLES

    def test_template_modulo(self):
        html = (_PAGES / "ia_responsavel.html").read_text(encoding="utf-8")
        for t in ("ia-responsavel", "ir-posture", "ir-forget", "ir-audit", "ir-capabilities"):
            assert f'data-testid="{t}"' in html, t
        assert "function iaResponsavel()" in html
        assert "/api/v1/governance/summary" in html
        assert "/api/v1/privacy/forget" in html

    def test_nav_gated_para_governanca(self):
        base = _BASE.read_text(encoding="utf-8")
        assert 'href="/ia-responsavel"' in base
        assert "user_role in ['root', 'admin', 'governanca']" in base

    def test_perfil_governanca_no_dropdown(self):
        s = (_PAGES / "settings.html").read_text(encoding="utf-8")
        assert '<option value="governanca">Governança</option>' in s
        # e as abas gated passaram a incluir governanca
        assert "user_role in ['root', 'admin', 'governanca']" in s


# ─── Model / System cards (Fase 2) ───────────────────────────────────────────
class FakeRepo2:
    def __init__(self, rows):
        self.rows = rows

    async def find_all(self, limit=100, offset=0, **f):
        return self.rows[:limit]

    async def find_by_id(self, i):
        return next((r for r in self.rows if r.get("id") == i), None)


_SKILL_MD = (
    "---\nid: urn:skill:t:subagent:x\nversion: 0.1.0\nkind: subagent\n"
    "owner: t\nstability: alpha\n---\n## Purpose\nResponder sobre garantia.\n\n"
    "## Guardrails\nNao vazar dados de terceiros.\n"
)


class TestModelCards:
    def test_truthy(self):
        import app.routes.governance as G
        assert G._truthy(1) and G._truthy("1") and G._truthy(True) and G._truthy("true")
        assert not G._truthy(0) and not G._truthy("0") and not G._truthy(None)

    def test_risk_signals(self):
        import app.routes.governance as G
        sig = G._risk_signals({"allow_general_knowledge": 1, "require_evidence": 0, "kind": "subagent"}, None)
        blob = " ".join(x["label"] for x in sig)
        assert "conhecimento geral" in blob
        assert "Não exige evidência" in blob
        assert "Sem SKILL.md" in blob
        sig2 = G._risk_signals({"require_evidence": 1, "kind": "subagent"},
                               {"guardrails": "x", "evidence_policy": ""})
        assert any(x["level"] == "ok" for x in sig2)

    @pytest.mark.asyncio
    async def test_list_ordena_mais_arriscado_primeiro(self, monkeypatch):
        import app.routes.governance as G
        monkeypatch.setattr(G, "agents_repo", FakeRepo2([
            {"id": "a", "name": "Seguro", "kind": "subagent", "skill_id": "s1",
             "require_evidence": 1, "allow_general_knowledge": 0},
            {"id": "b", "name": "Arriscado", "kind": "subagent", "skill_id": None,
             "require_evidence": 0, "allow_general_knowledge": 1},
        ]))
        r = await G.model_cards_list(user={})
        assert r["cards"][0]["agent_id"] == "b"
        assert r["cards"][0]["warn_count"] >= 2

    @pytest.mark.asyncio
    async def test_detail_deriva_da_skill_real(self, monkeypatch):
        import app.routes.governance as G
        monkeypatch.setattr(G, "agents_repo", FakeRepo2([
            {"id": "a", "name": "Esp", "kind": "subagent", "skill_id": "s1",
             "require_evidence": 1, "allow_general_knowledge": 0,
             "llm_provider": "azure", "model": "gpt-4o"},
        ]))
        monkeypatch.setattr(G, "skills_repo", FakeRepo2([{"id": "s1", "raw_content": _SKILL_MD}]))
        r = await G.model_card_detail("a", user={})
        assert r["name"] == "Esp" and r["model"]["model"] == "gpt-4o"
        assert r["skill"] is not None
        assert "garantia" in r["skill"]["purpose"].lower()

    @pytest.mark.asyncio
    async def test_detail_404(self, monkeypatch):
        import app.routes.governance as G
        monkeypatch.setattr(G, "agents_repo", FakeRepo2([]))
        with pytest.raises(HTTPException) as e:
            await G.model_card_detail("nope", user={})
        assert e.value.status_code == 404

    def test_transparencia_conta_model_cards(self):
        import app.routes.governance as G
        _, pillars = G._posture()
        t = next(p for p in pillars if p["pillar"] == "Transparência")
        assert any("model card" in c["label"].lower() for c in t["checks"])

    def test_aba_model_cards_no_template(self):
        html = (_PAGES / "ia_responsavel.html").read_text(encoding="utf-8")
        assert "{ k: 'cards', l: 'Model cards' }" in html
        assert 'data-testid="ir-cards-list"' in html
        assert 'data-testid="ir-card-detail"' in html
        assert "async selectCard(" in html
        assert "/api/v1/governance/model-cards" in html


# ─── Registro de risco (Fase 2) ──────────────────────────────────────────────
class FakeRWRepo:
    def __init__(self, rows=None):
        self.rows = [dict(r) for r in (rows or [])]

    async def find_all(self, limit=100, offset=0, **f):
        out = [r for r in self.rows if all(r.get(k) == v for k, v in f.items())]
        return out[:limit]

    async def create(self, row):
        self.rows.append(dict(row))
        return row.get("id")

    async def update(self, i, patch):
        for r in self.rows:
            if r.get("id") == i:
                r.update(patch)
                return True
        return False

    async def delete(self, i):
        n = len(self.rows)
        self.rows = [r for r in self.rows if r.get("id") != i]
        return len(self.rows) < n


class TestRiskRegister:
    def test_suggested_tier(self):
        import app.routes.governance as G
        assert G._suggested_tier({"allow_general_knowledge": 1, "require_evidence": 0, "skill_id": None}) == "high"
        assert G._suggested_tier({"allow_general_knowledge": 1, "require_evidence": 1, "skill_id": "s"}) == "limited"
        assert G._suggested_tier({"allow_general_knowledge": 0, "require_evidence": 1, "skill_id": "s"}) == "minimal"

    @pytest.mark.asyncio
    async def test_register_merge_e_counts(self, monkeypatch):
        import app.routes.governance as G
        monkeypatch.setattr(G, "agents_repo", FakeRepo2([
            {"id": "a1", "name": "X", "kind": "subagent", "require_evidence": 1, "skill_id": "s", "allow_general_knowledge": 0},
            {"id": "a2", "name": "Y", "kind": "subagent", "require_evidence": 0, "skill_id": None, "allow_general_knowledge": 1},
        ]))
        monkeypatch.setattr(G, "governance_risk_repo", FakeRWRepo([
            {"id": "r1", "entity_type": "agent", "entity_id": "a1", "tier": "minimal", "rationale": "ok"},
        ]))
        r = await G.risk_register(user={})
        by = {it["entity_id"]: it for it in r["items"]}
        assert by["a1"]["tier"] == "minimal"
        assert by["a2"]["tier"] is None and by["a2"]["suggested_tier"] == "high"
        assert r["counts"]["minimal"] == 1 and r["counts"]["unclassified"] == 1

    @pytest.mark.asyncio
    async def test_classify_cria_e_audita(self, monkeypatch):
        import app.routes.governance as G
        gr, au = FakeRWRepo([]), FakeRWRepo([])
        monkeypatch.setattr(G, "governance_risk_repo", gr)
        monkeypatch.setattr(G, "audit_repo", au)
        r = await G.classify_risk("agent", "a9", G.RiskClassify(tier="high", rationale="decisão de crédito"), user={"username": "gov"})
        assert "salva" in r["message"].lower()
        assert gr.rows[0]["tier"] == "high" and gr.rows[0]["classified_by"] == "gov"
        assert any("risk_classified:high" in x["action"] for x in au.rows)

    @pytest.mark.asyncio
    async def test_classify_atualiza_sem_duplicar(self, monkeypatch):
        import app.routes.governance as G
        gr = FakeRWRepo([{"id": "r1", "entity_type": "agent", "entity_id": "a9", "tier": "minimal"}])
        monkeypatch.setattr(G, "governance_risk_repo", gr)
        monkeypatch.setattr(G, "audit_repo", FakeRWRepo([]))
        await G.classify_risk("agent", "a9", G.RiskClassify(tier="limited"), user={"username": "g"})
        assert len(gr.rows) == 1 and gr.rows[0]["tier"] == "limited"

    @pytest.mark.asyncio
    async def test_classify_valida_tier_e_entity(self, monkeypatch):
        import app.routes.governance as G
        monkeypatch.setattr(G, "governance_risk_repo", FakeRWRepo([]))
        monkeypatch.setattr(G, "audit_repo", FakeRWRepo([]))
        with pytest.raises(HTTPException) as e:
            await G.classify_risk("agent", "a", G.RiskClassify(tier="bogus"), user={})
        assert e.value.status_code == 422
        with pytest.raises(HTTPException) as e:
            await G.classify_risk("nope", "a", G.RiskClassify(tier="high"), user={})
        assert e.value.status_code == 422

    def test_aba_risco_template(self):
        html = (_PAGES / "ia_responsavel.html").read_text(encoding="utf-8")
        assert "{ k: 'risk', l: 'Risco' }" in html
        for t in ("ir-risk-counts", "ir-risk-list", "ir-risk-modal", "ir-risk-save"):
            assert f'data-testid="{t}"' in html, t
        assert "async loadRisk()" in html
        assert "/api/v1/governance/risk-register" in html


# ─── Guarda de injeção & DLP — configuração (Fase 2) ─────────────────────────
class FakeStore:
    def __init__(self):
        self.saved = {}

    async def set_many(self, d):
        self.saved.update(d)


class TestGuardConfig:
    @pytest.mark.asyncio
    async def test_get_traz_as_5_chaves(self):
        import app.routes.governance as G
        r = await G.guard_config_get(user={})
        for k in ("prompt_guard_enabled", "prompt_guard_block_threshold",
                  "prompt_guard_warn_threshold", "dlp_enabled", "dlp_redact_before_llm"):
            assert k in r

    @pytest.mark.asyncio
    async def test_put_persiste_e_aplica(self, monkeypatch):
        import app.routes.governance as G
        store = FakeStore()
        monkeypatch.setattr(G, "settings_store", store)

        async def _apply():
            return 3
        monkeypatch.setattr(G, "apply_settings_to_env", _apply)
        monkeypatch.setattr(G, "audit_repo", FakeRWRepo([]))
        r = await G.guard_config_put(G.GuardConfig(prompt_guard_enabled=False, dlp_enabled=True), user={"username": "gov"})
        assert r["env_applied"] == 3
        assert store.saved["prompt_guard_enabled"] == "False"

    @pytest.mark.asyncio
    async def test_put_422_threshold_fora_de_0_1(self, monkeypatch):
        import app.routes.governance as G
        monkeypatch.setattr(G, "settings_store", FakeStore())
        with pytest.raises(HTTPException) as e:
            await G.guard_config_put(G.GuardConfig(prompt_guard_block_threshold=1.5), user={})
        assert e.value.status_code == 422

    @pytest.mark.asyncio
    async def test_put_422_aviso_maior_que_bloqueio(self, monkeypatch):
        import app.routes.governance as G
        monkeypatch.setattr(G, "settings_store", FakeStore())
        with pytest.raises(HTTPException) as e:
            await G.guard_config_put(
                G.GuardConfig(prompt_guard_block_threshold=0.3, prompt_guard_warn_threshold=0.6), user={})
        assert e.value.status_code == 422

    def test_template_guard_config(self):
        html = (_PAGES / "ia_responsavel.html").read_text(encoding="utf-8")
        for t in ("ir-guard-config", "ir-guard-save"):
            assert f'data-testid="{t}"' in html, t
        assert "async loadGuard()" in html and "async saveGuard()" in html
        assert "get guardDirty()" in html
        assert "/api/v1/governance/guard-config" in html

    def test_chaves_no_ui_to_env_map(self):
        # sem isto o PUT persiste no banco mas NÃO aplica ao runtime (bug real
        # pego no smoke: apply_settings_to_env só aplica chaves do mapa).
        from app.core.config import _UI_TO_ENV_MAP
        for k in ("prompt_guard_enabled", "prompt_guard_block_threshold",
                  "prompt_guard_warn_threshold", "dlp_enabled", "dlp_redact_before_llm"):
            assert k in _UI_TO_ENV_MAP, k


# ─── Crosswalk de conformidade (Fase 3) ──────────────────────────────────────
class TestCrosswalk:
    def test_controls_entregues_sao_true(self):
        import app.routes.governance as G
        c = G._controls()
        for k in ("forget", "audit", "rbac", "model_cards", "risk_register",
                  "federation_guard", "evidence_policy"):
            assert c[k] is True

    @pytest.mark.asyncio
    async def test_5_frameworks_com_pct(self):
        import app.routes.governance as G
        r = await G.compliance_crosswalk(user={})
        assert {f["framework"] for f in r["frameworks"]} == {
            "EU AI Act", "NIST AI RMF", "ISO/IEC 42001", "LGPD", "OWASP LLM Top 10"}
        for f in r["frameworks"]:
            assert 0 <= f["pct"] <= 100 and f["covered"] <= f["total"]
        assert "control_labels" in r

    @pytest.mark.asyncio
    async def test_cobertura_reflete_controle_real(self, monkeypatch):
        import app.routes.governance as G
        base = dict(G._controls())
        base.update(prompt_guard=False, dlp=False, grounding=False, verifier=False, opa=False)
        monkeypatch.setattr(G, "_controls", lambda: base)
        r = await G.compliance_crosswalk(user={})
        owasp = next(f for f in r["frameworks"] if f["framework"] == "OWASP LLM Top 10")
        llm01 = next(x for x in owasp["requirements"] if x["requirement"].startswith("LLM01"))
        assert llm01["covered"] is False and llm01["satisfied_by"] == []
        lgpd = next(f for f in r["frameworks"] if f["framework"] == "LGPD")
        forget = next(x for x in lgpd["requirements"] if "esquecimento" in x["requirement"].lower())
        assert forget["covered"] is True

    def test_template_conformidade(self):
        html = (_PAGES / "ia_responsavel.html").read_text(encoding="utf-8")
        assert "{ k: 'crosswalk', l: 'Conformidade' }" in html
        assert 'data-testid="ir-crosswalk"' in html
        assert 'data-testid="ir-crosswalk-detail"' in html
        assert "async loadCrosswalk()" in html
        assert "/api/v1/governance/crosswalk" in html


# ─── Attestation + papéis DPO/AI Officer + relatório (Fase 3) ────────────────
class TestAttestation:
    @pytest.mark.asyncio
    async def test_officer_assign_list_remove(self, monkeypatch):
        import app.routes.governance as G
        off = FakeRWRepo([])
        monkeypatch.setattr(G, "governance_officer_repo", off)
        monkeypatch.setattr(G, "users_repo", FakeRepo2([{"id": "u1", "display_name": "Ana"}]))
        monkeypatch.setattr(G, "audit_repo", FakeRWRepo([]))
        r = await G.officer_assign(G.OfficerAssign(office="dpo", user_id="u1"), user={"username": "gov"})
        assert "id" in r and off.rows[0]["office"] == "dpo"
        lst = await G.officers_list(user={})
        assert lst["assigned"][0]["name"] == "Ana"
        assert any(o["office"] == "dpo" for o in lst["offices"])
        rm = await G.officer_remove(off.rows[0]["id"], user={})
        assert "removida" in rm["message"].lower()

    @pytest.mark.asyncio
    async def test_officer_422_e_409(self, monkeypatch):
        import app.routes.governance as G
        off = FakeRWRepo([{"id": "o1", "office": "dpo", "user_id": "u1"}])
        monkeypatch.setattr(G, "governance_officer_repo", off)
        monkeypatch.setattr(G, "audit_repo", FakeRWRepo([]))
        with pytest.raises(HTTPException) as e:
            await G.officer_assign(G.OfficerAssign(office="bogus", user_id="u1"), user={})
        assert e.value.status_code == 422
        with pytest.raises(HTTPException) as e:
            await G.officer_assign(G.OfficerAssign(office="dpo", user_id="u1"), user={})
        assert e.value.status_code == 409

    @pytest.mark.asyncio
    async def test_attestation_sign_e_lista(self, monkeypatch):
        import app.routes.governance as G
        att = FakeRWRepo([])
        monkeypatch.setattr(G, "governance_attestation_repo", att)
        monkeypatch.setattr(G, "audit_repo", FakeRWRepo([]))
        r = await G.attestation_sign(G.AttestationCreate(scope="platform", statement="pronto"), user={"username": "gov"})
        assert "id" in r and att.rows[0]["signed_by"] == "gov"
        lst = await G.attestations_list(user={})
        assert lst["attestations"][0]["statement"] == "pronto"

    @pytest.mark.asyncio
    async def test_attestation_422(self, monkeypatch):
        import app.routes.governance as G
        monkeypatch.setattr(G, "governance_attestation_repo", FakeRWRepo([]))
        monkeypatch.setattr(G, "audit_repo", FakeRWRepo([]))
        with pytest.raises(HTTPException) as e:
            await G.attestation_sign(G.AttestationCreate(scope="bogus", statement="x"), user={})
        assert e.value.status_code == 422
        with pytest.raises(HTTPException) as e:
            await G.attestation_sign(G.AttestationCreate(scope="platform", statement="  "), user={})
        assert e.value.status_code == 422

    @pytest.mark.asyncio
    async def test_report_consolida_tudo(self, monkeypatch):
        import app.routes.governance as G
        monkeypatch.setattr(G, "agents_repo", FakeRepo2([{"id": "a1", "require_evidence": 1, "skill_id": "s"}]))
        monkeypatch.setattr(G, "governance_risk_repo", FakeRWRepo([]))
        monkeypatch.setattr(G, "governance_officer_repo", FakeRWRepo([]))
        monkeypatch.setattr(G, "governance_attestation_repo", FakeRWRepo([]))
        monkeypatch.setattr(G, "users_repo", FakeRepo2([]))
        r = await G.compliance_report(user={})
        for k in ("generated_at", "posture_score", "pillars", "frameworks", "risk", "officers", "attestations"):
            assert k in r
        assert len(r["frameworks"]) == 5

    def test_template_prontidao(self):
        html = (_PAGES / "ia_responsavel.html").read_text(encoding="utf-8")
        assert "{ k: 'attest', l: 'Prontidão' }" in html
        for t in ("ir-officers", "ir-attest-sign", "ir-attest-save", "ir-report-export", "ir-attest-list"):
            assert f'data-testid="{t}"' in html, t
        assert "async signAttestation()" in html and "async exportReport()" in html
        assert "/api/v1/governance/report" in html


# ─── Policy-as-code (OPA) — cockpit read/simulate (62.0.0) ───────────────────
class TestOpaCockpit:
    def test_opa_keys_no_ui_to_env_map(self):
        # sem isto o PUT /opa/config persiste no banco mas NÃO aplica ao runtime
        # (apply_settings_to_env só varre o mapa) — mesma classe de bug do #700.
        from app.core.config import _UI_TO_ENV_MAP
        for k in ("opa_enabled", "opa_failsafe_open", "opa_timeout_seconds"):
            assert k in _UI_TO_ENV_MAP, k

    def test_opa_keys_nao_seladas(self):
        # NÃO-seladas: o .env segue como fallback de boot. Selá-las reverteria
        # silenciosamente uma implantação que liga o OPA / fecha o failsafe via
        # .env para os defaults (fail-closed→open) num upgrade = downgrade de
        # segurança silencioso (achado da revisão adversarial).
        from app.core.config import _NON_MODEL_UI_KEYS, _SEALED_ENV_VARS
        for k in ("opa_enabled", "opa_failsafe_open", "opa_timeout_seconds"):
            assert k in _NON_MODEL_UI_KEYS, k
        for env in ("OPA_ENABLED", "OPA_FAILSAFE_OPEN", "OPA_TIMEOUT_SECONDS"):
            assert env not in _SEALED_ENV_VARS, env

    @pytest.mark.asyncio
    async def test_status_flags_e_saude(self, monkeypatch):
        import app.routes.governance as G
        import app.core.opa_client as OC
        fake = FakeSettings(opa_enabled=True, opa_url="http://opa:8181",
                            opa_failsafe_open=False, opa_timeout_seconds=1.5)
        monkeypatch.setattr(G, "get_settings", lambda: fake)

        async def _health():
            return True
        monkeypatch.setattr(OC, "server_health", _health)
        r = await G.opa_status(user={})
        assert r["enabled"] is True and r["failsafe_open"] is False
        assert r["timeout_seconds"] == 1.5 and r["server_ok"] is True
        assert r["url"] == "http://opa:8181"

    @pytest.mark.asyncio
    async def test_policies_da_opa_marca_wired(self, monkeypatch):
        import app.routes.governance as G
        import app.core.opa_client as OC

        async def _list():
            return [
                {"id": "policies/interaction.rego", "raw": "package interaction"},
                {"id": "policies/evidence.rego", "raw": "package evidence"},
            ]
        monkeypatch.setattr(OC, "list_policies", _list)
        r = await G.opa_policies(user={})
        assert r["source"] == "opa"
        by = {p["package"]: p for p in r["policies"]}
        assert by["interaction"]["wired"] is True
        assert by["evidence"]["wired"] is False
        assert r["policies"][0]["package"] == "interaction"  # wired primeiro

    @pytest.mark.asyncio
    async def test_policies_fallback_disco(self, monkeypatch):
        import app.routes.governance as G
        import app.core.opa_client as OC

        async def _none():
            return None
        monkeypatch.setattr(OC, "list_policies", _none)
        r = await G.opa_policies(user={})
        assert r["source"] == "disk"
        pkgs = {p["package"] for p in r["policies"]}
        assert {"interaction", "tool_invocation", "evidence"} <= pkgs
        inter = next(p for p in r["policies"] if p["package"] == "interaction")
        assert "package interaction" in inter["raw"]  # lê o Rego real do disco

    @pytest.mark.asyncio
    async def test_simulate_valida_pacote(self):
        import app.routes.governance as G
        with pytest.raises(HTTPException) as e:
            await G.opa_simulate(G.OpaSimulate(package="bogus"), user={})
        assert e.value.status_code == 422

    @pytest.mark.asyncio
    async def test_simulate_chama_opa_e_traz_reasons(self, monkeypatch):
        import app.routes.governance as G
        import app.core.opa_client as OC
        calls = []

        async def _sim(package, rule="allow", input_doc=None):
            calls.append((package, rule))
            if rule == "allow":
                return {"allow": False, "result": False, "source": "opa", "duration_ms": 1}
            return {"allow": None, "result": ["user_inactive"], "source": "opa", "duration_ms": 1}
        monkeypatch.setattr(OC, "simulate", _sim)
        r = await G.opa_simulate(
            G.OpaSimulate(package="interaction", input={"user": {"status": "inactive"}}), user={})
        assert r["allow"] is False
        assert r["reasons"] == ["user_inactive"]
        assert ("interaction", "allow") in calls and ("interaction", "reasons") in calls

    @pytest.mark.asyncio
    async def test_simulate_tool_invocation_usa_reason_singular(self, monkeypatch):
        # tool_invocation.rego expõe `reason` (singular string); interaction expõe
        # `reasons` (set). Este ramo (governance.py) não estava coberto.
        import app.routes.governance as G
        import app.core.opa_client as OC
        calls = []

        async def _sim(package, rule="allow", input_doc=None):
            calls.append((package, rule))
            if rule == "allow":
                return {"allow": False, "result": False, "source": "opa", "duration_ms": 1}
            return {"allow": None, "result": "insufficient_role", "source": "opa", "duration_ms": 1}
        monkeypatch.setattr(OC, "simulate", _sim)
        r = await G.opa_simulate(G.OpaSimulate(
            package="tool_invocation",
            input={"tool": {"sensitivity": "high"}, "user": {"role": "operator"}}), user={})
        assert r["allow"] is False and r["reasons"] == "insufficient_role"
        assert ("tool_invocation", "reason") in calls
        assert ("tool_invocation", "reasons") not in calls

    @pytest.mark.asyncio
    async def test_decisions_parse_details(self, monkeypatch):
        import app.routes.governance as G
        import json as _j
        monkeypatch.setattr(G, "audit_repo", FakeAudit([
            {"id": 2, "action": "deny", "entity_id": "tool_invocation.allow",
             "details": _j.dumps({"package": "tool_invocation", "rule": "allow",
                                  "decision": {"source": "opa", "duration_ms": 3}})},
            {"id": 1, "action": "allow", "entity_id": "interaction.allow", "details": "{}"},
        ]))
        r = await G.opa_decisions(limit=10, user={})
        assert [d["id"] for d in r["decisions"]] == [2, 1]  # recentes primeiro
        assert r["decisions"][0]["package"] == "tool_invocation"
        assert r["decisions"][0]["source"] == "opa" and r["decisions"][0]["duration_ms"] == 3

    @pytest.mark.asyncio
    async def test_config_put_persiste_e_aplica(self, monkeypatch):
        import app.routes.governance as G
        store = FakeStore()
        monkeypatch.setattr(G, "settings_store", store)

        async def _apply():
            return 3
        monkeypatch.setattr(G, "apply_settings_to_env", _apply)
        monkeypatch.setattr(G, "audit_repo", FakeRWRepo([]))
        r = await G.opa_config_put(
            G.OpaConfig(opa_enabled=True, opa_failsafe_open=False, opa_timeout_seconds=2.5),
            user={"username": "gov"})
        assert r["env_applied"] == 3
        assert store.saved["opa_enabled"] == "True"
        assert store.saved["opa_failsafe_open"] == "False"
        assert store.saved["opa_timeout_seconds"] == "2.5"

    @pytest.mark.asyncio
    async def test_config_put_422_timeout(self, monkeypatch):
        import app.routes.governance as G
        monkeypatch.setattr(G, "settings_store", FakeStore())
        with pytest.raises(HTTPException) as e:
            await G.opa_config_put(G.OpaConfig(opa_timeout_seconds=99), user={})
        assert e.value.status_code == 422
        # limite inferior também barrado (0.1 <= t <= 30)
        with pytest.raises(HTTPException) as e2:
            await G.opa_config_put(G.OpaConfig(opa_timeout_seconds=0.01), user={})
        assert e2.value.status_code == 422

    @pytest.mark.asyncio
    async def test_config_put_nada_a_alterar(self, monkeypatch):
        import app.routes.governance as G
        monkeypatch.setattr(G, "settings_store", FakeStore())
        r = await G.opa_config_put(G.OpaConfig(), user={})
        assert r["env_applied"] == 0

    @pytest.mark.asyncio
    async def test_config_put_apply_falha_nao_quebra(self, monkeypatch):
        # se apply_settings_to_env explodir, o PUT ainda retorna (env_applied=0).
        import app.routes.governance as G
        monkeypatch.setattr(G, "settings_store", FakeStore())

        async def _boom():
            raise RuntimeError("db down")
        monkeypatch.setattr(G, "apply_settings_to_env", _boom)
        monkeypatch.setattr(G, "audit_repo", FakeRWRepo([]))
        r = await G.opa_config_put(G.OpaConfig(opa_enabled=True), user={"username": "x"})
        assert r["env_applied"] == 0

    @pytest.mark.asyncio
    async def test_decisions_details_malformado_nao_quebra(self, monkeypatch):
        # details não-JSON não derruba o log (campos ficam vazios).
        import app.routes.governance as G
        monkeypatch.setattr(G, "audit_repo", FakeAudit([
            {"id": 1, "action": "allow", "entity_id": "x", "details": "{nao-json"},
        ]))
        r = await G.opa_decisions(limit=5, user={})
        assert r["decisions"][0]["id"] == 1 and r["decisions"][0]["package"] == ""

    def test_dockerfile_copia_politicas_para_fallback(self):
        # o fallback de disco de GET /opa/policies precisa dos .rego DENTRO da
        # imagem do app; sem esta COPY o dir /app/infra/opa/policies fica vazio no
        # container (o dir só existe no host) e o fallback vira lista vazia.
        df = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(encoding="utf-8")
        assert "infra/opa/policies" in df

    def test_aba_policies_no_template(self):
        html = (_PAGES / "ia_responsavel.html").read_text(encoding="utf-8")
        assert "{ k: 'policies', l: 'Políticas' }" in html
        for t in ("ir-opa-config", "ir-opa-save", "ir-opa-simulator",
                  "ir-opa-simulate", "ir-opa-policies", "ir-opa-decisions"):
            assert f'data-testid="{t}"' in html, t
        assert "async loadOpa()" in html and "async saveOpa()" in html and "async runOpaSim()" in html
        assert "get opaDirty()" in html
        assert "/api/v1/governance/opa/status" in html
        assert "/api/v1/governance/opa/simulate" in html
        # o viewer OPA saiu do roadmap (foi entregue); sobra a Fase B (edição).
        assert "'Políticas (OPA)', f: 'Fase 2'" not in html


# ─── Edição persistente de políticas Rego (63.0.0 — cockpit Fase B) ──────────
class TestOpaPolicyEditing:
    def test_schema_tem_tabela_e_repo(self):
        from app.core.database import SCHEMA, governance_policy_repo
        assert "governance_policy_version" in SCHEMA
        assert governance_policy_repo.table == "governance_policy_version"

    @pytest.mark.asyncio
    async def test_edit_valida_salva_audita(self, monkeypatch):
        import app.routes.governance as G

        async def _vp(pkg, rego):
            return {"ok": True, "error": None}

        async def _sv(pkg, rego, note, who):
            return 4

        async def _snap(pkg):
            return "package interaction\n# prev"
        monkeypatch.setattr(G.opa_pol, "validate_and_push", _vp)
        monkeypatch.setattr(G.opa_pol, "save_version", _sv)
        monkeypatch.setattr(G.opa_pol, "opa_current_raw", _snap)
        au = FakeRWRepo([])
        monkeypatch.setattr(G, "audit_repo", au)
        r = await G.opa_policy_edit("interaction",
                                    G.OpaPolicyEdit(rego="package interaction\nallow := true"),
                                    user={"username": "gov"})
        assert r["version"] == 4 and r["package"] == "interaction"
        assert any("policy_edited:v4" in x["action"] for x in au.rows)

    @pytest.mark.asyncio
    async def test_edit_opa_fora_do_ar_503(self, monkeypatch):
        import app.routes.governance as G

        async def _vp(pkg, rego):
            return {"ok": False, "kind": "unreachable", "error": "ConnectError"}

        async def _snap(pkg):
            return None
        monkeypatch.setattr(G.opa_pol, "validate_and_push", _vp)
        monkeypatch.setattr(G.opa_pol, "opa_current_raw", _snap)
        with pytest.raises(HTTPException) as e:
            await G.opa_policy_edit("interaction", G.OpaPolicyEdit(rego="package interaction\nx"), user={})
        assert e.value.status_code == 503

    @pytest.mark.asyncio
    async def test_edit_falha_de_persistencia_compensa_e_500(self, monkeypatch):
        # save_version explode DEPOIS do push → OPA é revertido e devolve 500.
        import app.routes.governance as G
        reverted = {}

        async def _vp(pkg, rego):
            return {"ok": True, "kind": "ok", "error": None}

        async def _snap(pkg):
            return "package interaction\n# PREV"

        async def _sv(pkg, rego, note, who):
            raise RuntimeError("db down")

        async def _revert(pkg, prev):
            reverted["pkg"], reverted["prev"] = pkg, prev
        monkeypatch.setattr(G.opa_pol, "validate_and_push", _vp)
        monkeypatch.setattr(G.opa_pol, "opa_current_raw", _snap)
        monkeypatch.setattr(G.opa_pol, "save_version", _sv)
        monkeypatch.setattr(G.opa_pol, "revert_opa", _revert)
        monkeypatch.setattr(G, "audit_repo", FakeRWRepo([]))
        with pytest.raises(HTTPException) as e:
            await G.opa_policy_edit("interaction", G.OpaPolicyEdit(rego="package interaction\nallow := true"), user={})
        assert e.value.status_code == 500
        assert reverted["pkg"] == "interaction" and "# PREV" in reverted["prev"]  # OPA revertido ao snapshot

    @pytest.mark.asyncio
    async def test_edit_pacote_invalido_422(self):
        import app.routes.governance as G
        with pytest.raises(HTTPException) as e:
            await G.opa_policy_edit("bogus", G.OpaPolicyEdit(rego="x"), user={})
        assert e.value.status_code == 422

    @pytest.mark.asyncio
    async def test_edit_vazio_422(self):
        import app.routes.governance as G
        with pytest.raises(HTTPException) as e:
            await G.opa_policy_edit("interaction", G.OpaPolicyEdit(rego="   "), user={})
        assert e.value.status_code == 422

    @pytest.mark.asyncio
    async def test_edit_rejeitado_pelo_opa_422(self, monkeypatch):
        import app.routes.governance as G

        async def _vp(pkg, rego):
            return {"ok": False, "kind": "rejected", "error": "rego_parse_error"}

        async def _snap(pkg):
            return None
        monkeypatch.setattr(G.opa_pol, "validate_and_push", _vp)
        monkeypatch.setattr(G.opa_pol, "opa_current_raw", _snap)
        with pytest.raises(HTTPException) as e:
            await G.opa_policy_edit("interaction",
                                    G.OpaPolicyEdit(rego="package interaction\ninvalid("), user={})
        assert e.value.status_code == 422 and "rego_parse_error" in e.value.detail

    @pytest.mark.asyncio
    async def test_versions_lista_e_current(self, monkeypatch):
        import app.routes.governance as G

        async def _lv(pkg):
            return [
                {"version": 2, "note": "editado", "created_by": "gov", "created_at": "t2"},
                {"version": 1, "note": "seed", "created_by": "sys", "created_at": "t1"},
            ]
        monkeypatch.setattr(G.opa_pol, "list_versions", _lv)
        r = await G.opa_policy_versions("interaction", user={})
        assert r["current"] == 2 and len(r["versions"]) == 2

    @pytest.mark.asyncio
    async def test_rollback_cria_versao_nova_com_rego_alvo(self, monkeypatch):
        import app.routes.governance as G

        async def _lv(pkg):
            return [{"version": 3, "rego": "v3"}, {"version": 1, "rego": "v1"}]

        async def _vp(pkg, rego):
            return {"ok": True, "error": None}
        saved = {}

        async def _sv(pkg, rego, note, who):
            saved.update(rego=rego, note=note)
            return 4
        async def _snap(pkg):
            return None
        monkeypatch.setattr(G.opa_pol, "list_versions", _lv)
        monkeypatch.setattr(G.opa_pol, "validate_and_push", _vp)
        monkeypatch.setattr(G.opa_pol, "save_version", _sv)
        monkeypatch.setattr(G.opa_pol, "opa_current_raw", _snap)
        monkeypatch.setattr(G, "audit_repo", FakeRWRepo([]))
        r = await G.opa_policy_rollback("interaction", G.OpaRollback(version=1), user={"username": "g"})
        assert r["version"] == 4 and saved["rego"] == "v1" and "rollback de v1" in saved["note"]

    @pytest.mark.asyncio
    async def test_rollback_versao_inexistente_404(self, monkeypatch):
        import app.routes.governance as G

        async def _lv(pkg):
            return [{"version": 1, "rego": "v1"}]
        monkeypatch.setattr(G.opa_pol, "list_versions", _lv)
        with pytest.raises(HTTPException) as e:
            await G.opa_policy_rollback("interaction", G.OpaRollback(version=99), user={})
        assert e.value.status_code == 404

    @pytest.mark.asyncio
    async def test_restore_default_empurra_o_baked(self, monkeypatch):
        import app.routes.governance as G

        def _baked(pkg):
            return "package interaction\n# baked"

        async def _vp(pkg, rego):
            return {"ok": True, "error": None}
        saved = {}

        async def _sv(pkg, rego, note, who):
            saved["rego"] = rego
            return 5
        async def _snap(pkg):
            return None
        monkeypatch.setattr(G.opa_pol, "read_baked", _baked)
        monkeypatch.setattr(G.opa_pol, "validate_and_push", _vp)
        monkeypatch.setattr(G.opa_pol, "save_version", _sv)
        monkeypatch.setattr(G.opa_pol, "opa_current_raw", _snap)
        monkeypatch.setattr(G, "audit_repo", FakeRWRepo([]))
        r = await G.opa_policy_restore("interaction", user={"username": "g"})
        assert r["version"] == 5 and "baked" in saved["rego"]

    @pytest.mark.asyncio
    async def test_fallback_rotula_db_quando_vem_do_db(self, monkeypatch):
        # OPA fora, mas há override no DB → source='db' (não 'disk') e mostra o DB.
        import app.routes.governance as G
        import app.core.opa_client as OC

        async def _none():
            return None

        async def _cur(pkg):
            return {"version": 3, "rego": "package " + pkg} if pkg == "interaction" else None
        monkeypatch.setattr(OC, "list_policies", _none)
        monkeypatch.setattr(G.opa_pol, "current_version", _cur)
        r = await G.opa_policies(user={})
        assert r["source"] == "db"
        by = {p["package"]: p for p in r["policies"]}
        assert by["interaction"]["raw"] == "package interaction"

    def test_editor_no_template(self):
        html = (_PAGES / "ia_responsavel.html").read_text(encoding="utf-8")
        for t in ("ir-opa-editor", "ir-opa-policy-save", "ir-opa-history", "ir-opa-hist-toggle"):
            assert f'data-testid="{t}"' in html, t
        for fn in ("async savePolicy(", "async loadVersions(", "async rollbackPolicy(", "async restoreDefault("):
            assert fn in html, fn
        assert "/api/v1/governance/opa/policies/' + pkg" in html


# ─── Evidence ACL — "no read up" (64.0.0) ─────────────────────────────────────
class _FakeUserRepo:
    def __init__(self, rows):
        self.rows = {k: dict(v) for k, v in rows.items()}

    async def find_by_id(self, i):
        return self.rows.get(i)

    async def update(self, i, patch):
        self.rows[i].update(patch)
        return True

    async def create(self, row):
        self.rows[row["id"]] = dict(row)
        return row["id"]

    async def count(self):
        return len(self.rows)

    async def find_all(self, limit=1000, offset=0, **f):
        return list(self.rows.values())[:limit]


class TestEvidenceAcl:
    def test_flag_no_ui_to_env_map_e_nao_selada(self):
        from app.core.config import _UI_TO_ENV_MAP, _NON_MODEL_UI_KEYS, _SEALED_ENV_VARS
        assert _UI_TO_ENV_MAP.get("evidence_acl_enabled") == "EVIDENCE_ACL_ENABLED"
        assert "evidence_acl_enabled" in _NON_MODEL_UI_KEYS
        assert "EVIDENCE_ACL_ENABLED" not in _SEALED_ENV_VARS

    @pytest.mark.asyncio
    async def test_status_traz_evidence_acl(self, monkeypatch):
        import app.routes.governance as G
        import app.core.opa_client as OC
        fake = FakeSettings(opa_enabled=False, opa_url="", opa_failsafe_open=True,
                            opa_timeout_seconds=2.0, evidence_acl_enabled=True)
        monkeypatch.setattr(G, "get_settings", lambda: fake)

        async def _h():
            return True
        monkeypatch.setattr(OC, "server_health", _h)
        r = await G.opa_status(user={})
        assert r["evidence_acl_enabled"] is True

    @pytest.mark.asyncio
    async def test_status_expoe_erros_de_repush(self, monkeypatch):
        # sinal de drift: se o re-push no boot falhou, /opa/status mostra os erros
        # (o OPA pode estar servindo o baked enquanto o DB mostra a versão editada).
        import app.routes.governance as G
        import app.core.opa_client as OC
        import app.core.opa_policies as P
        monkeypatch.setattr(G, "get_settings", lambda: FakeSettings(
            opa_enabled=False, opa_url="", opa_failsafe_open=True, opa_timeout_seconds=2.0,
            evidence_acl_enabled=False))

        async def _h():
            return True
        monkeypatch.setattr(OC, "server_health", _h)
        monkeypatch.setattr(P, "_LAST_REPUSH", {"pushed": [], "errors": ["evidence: opa down"]})
        r = await G.opa_status(user={})
        assert r["policy_repush_errors"] == ["evidence: opa down"]

    @pytest.mark.asyncio
    async def test_config_put_liga_evidence_acl(self, monkeypatch):
        import app.routes.governance as G
        store = FakeStore()
        monkeypatch.setattr(G, "settings_store", store)

        async def _apply():
            return 1
        monkeypatch.setattr(G, "apply_settings_to_env", _apply)
        monkeypatch.setattr(G, "audit_repo", FakeRWRepo([]))
        await G.opa_config_put(G.OpaConfig(evidence_acl_enabled=True), user={"username": "gov"})
        assert store.saved["evidence_acl_enabled"] == "True"

    @pytest.mark.asyncio
    async def test_evidence_wired_reflete_a_flag(self, monkeypatch):
        import app.routes.governance as G
        import app.core.opa_client as OC

        async def _list():
            return [{"id": "policies/evidence.rego", "raw": "package evidence"},
                    {"id": "policies/interaction.rego", "raw": "package interaction"}]
        monkeypatch.setattr(OC, "list_policies", _list)
        monkeypatch.setattr(G, "get_settings", lambda: FakeSettings(evidence_acl_enabled=True))
        r = await G.opa_policies(user={})
        assert next(p for p in r["policies"] if p["package"] == "evidence")["wired"] is True
        monkeypatch.setattr(G, "get_settings", lambda: FakeSettings(evidence_acl_enabled=False))
        r2 = await G.opa_policies(user={})
        assert next(p for p in r2["policies"] if p["package"] == "evidence")["wired"] is False

    @pytest.mark.asyncio
    async def test_update_clearance_privilegiado(self, monkeypatch):
        import app.routes.users as U
        from app.models.schemas import UserUpdate
        repo = _FakeUserRepo({"u1": {"id": "u1", "role": "comum", "status": "active"}})
        monkeypatch.setattr(U, "users_repo", repo)

        async def _caller(req):
            return {"id": "a1", "role": "admin"}
        monkeypatch.setattr(U, "_get_caller", _caller)
        await U.update_user("u1", UserUpdate(clearance="Confidential"), request=None)
        assert repo.rows["u1"]["clearance"] == "confidential"  # normalizado

    @pytest.mark.asyncio
    async def test_update_clearance_comum_barrado_403(self, monkeypatch):
        import app.routes.users as U
        from app.models.schemas import UserUpdate
        repo = _FakeUserRepo({"u1": {"id": "u1", "role": "comum", "status": "active"}})
        monkeypatch.setattr(U, "users_repo", repo)

        async def _caller(req):
            return {"id": "u1", "role": "comum"}  # edita a si mesmo (anti auto-escalonamento)
        monkeypatch.setattr(U, "_get_caller", _caller)
        with pytest.raises(HTTPException) as e:
            await U.update_user("u1", UserUpdate(clearance="restricted"), request=None)
        assert e.value.status_code == 403
        assert "clearance" not in repo.rows["u1"]

    @pytest.mark.asyncio
    async def test_update_clearance_invalido_422(self, monkeypatch):
        import app.routes.users as U
        from app.models.schemas import UserUpdate
        repo = _FakeUserRepo({"u1": {"id": "u1", "role": "comum", "status": "active"}})
        monkeypatch.setattr(U, "users_repo", repo)

        async def _caller(req):
            return {"id": "a1", "role": "admin"}
        monkeypatch.setattr(U, "_get_caller", _caller)
        with pytest.raises(HTTPException) as e:
            await U.update_user("u1", UserUpdate(clearance="bogus"), request=None)
        assert e.value.status_code == 422

    @pytest.mark.asyncio
    async def test_create_clearance_normaliza(self, monkeypatch):
        import app.routes.users as U
        from app.models.schemas import UserCreate
        repo = _FakeUserRepo({"x": {"id": "x", "username": "outro", "role": "admin"}})
        monkeypatch.setattr(U, "users_repo", repo)

        async def _caller(req):
            return {"id": "a1", "role": "admin"}
        monkeypatch.setattr(U, "_get_caller", _caller)
        r = await U.create_user(
            UserCreate(username="novo", password="senha-bem-longa-123", clearance="Confidential"),
            request=None)
        assert repo.rows[r["id"]]["clearance"] == "confidential"  # normalizado

    @pytest.mark.asyncio
    async def test_create_clearance_invalido_422(self, monkeypatch):
        import app.routes.users as U
        from app.models.schemas import UserCreate
        repo = _FakeUserRepo({"x": {"id": "x", "username": "outro", "role": "admin"}})
        monkeypatch.setattr(U, "users_repo", repo)

        async def _caller(req):
            return {"id": "a1", "role": "admin"}
        monkeypatch.setattr(U, "_get_caller", _caller)
        with pytest.raises(HTTPException) as e:
            await U.create_user(
                UserCreate(username="novo", password="senha-bem-longa-123", clearance="bogus"),
                request=None)
        assert e.value.status_code == 422

    def test_ui_evidence_acl_e_clearance(self):
        ir = (_PAGES / "ia_responsavel.html").read_text(encoding="utf-8")
        assert 'data-testid="ir-opa-evidence-acl"' in ir and "opaCfg.evidence_acl" in ir
        st = (_PAGES / "settings.html").read_text(encoding="utf-8")
        assert 'data-testid="user-clearance"' in st and "userForm.clearance" in st

    def test_todos_call_sites_do_retriever_passam_clearance(self):
        # O Evidence ACL só vale se TODO call site de retriever.search passar
        # user_clearance. A revisão achou 2 bypasses (RAG binding direto + inspeção
        # de KB); esta guarda quebra se um novo call site esquecer o clearance.
        import re
        from pathlib import Path
        app_dir = Path(__file__).resolve().parent.parent / "app"
        bad = []
        for py in app_dir.rglob("*.py"):
            txt = py.read_text(encoding="utf-8")
            for m in re.finditer(r"await\s+_?retriever\.search\(", txt):
                window = txt[m.end():m.end() + 400]
                if "user_clearance" not in window:
                    bad.append(f"{py.name}:{txt[:m.start()].count(chr(10)) + 1}")
        assert not bad, f"retriever.search sem user_clearance (bypass do ACL): {bad}"


# ─── Melhorias de UI 64.2.0 (ordem das abas, textos, tooltips, ator) ─────────
class TestUiMelhorias6420:
    def test_ordem_das_abas_e_sem_roadmap(self):
        html = (_PAGES / "ia_responsavel.html").read_text(encoding="utf-8")
        order = [
            "{ k: 'cards', l: 'Model cards' }",
            "{ k: 'policies', l: 'Políticas' }",
            "{ k: 'security', l: 'Segurança' }",
            "{ k: 'guardrails', l: 'Guardrails' }",  # 66.0.0: cockpit após Segurança
            "{ k: 'attest', l: 'Prontidão' }",
            "{ k: 'crosswalk', l: 'Conformidade' }",
            "{ k: 'risk', l: 'Risco' }",
            "{ k: 'audit', l: 'Auditoria' }",
            "{ k: 'privacy', l: 'Privacidade & LGPD' }",
            "{ k: 'overview', l: 'Visão geral' }",
        ]
        idx = [html.index(t) for t in order]  # index() explode se algum sumir
        assert idx == sorted(idx), "abas fora da ordem definida com o usuário"
        assert "'roadmap'" not in html and "ir-roadmap" not in html
        assert "tab: 'cards'" in html  # aba inicial = primeira da nova ordem

    def test_textos_de_fase_e_headless_removidos(self):
        html = (_PAGES / "ia_responsavel.html").read_text(encoding="utf-8")
        for gone in (
            "traz à luz e será a casa",
            "Antes só existia via API",
            "Fase 2: fila completa de pedidos do titular",
            "Antes só ligava por env/DB",
            "Até aqui só ligava por env/DB",
            "Clique num framework.",
            "headless",
        ):
            assert gone not in html, gone

    def test_novos_textos_explicativos(self):
        html = (_PAGES / "ia_responsavel.html").read_text(encoding="utf-8")
        assert "estado <strong>real e atual</strong>" in html            # Visão geral
        assert "score de risco de prompt injection" in html              # Segurança
        assert "linguagem declarativa de políticas do OPA" in html       # Rego
        assert "Selecione um card abaixo" in html                        # Conformidade

    def test_tooltips_de_conformidade(self):
        html = (_PAGES / "ia_responsavel.html").read_text(encoding="utf-8")
        assert ':title="fwTip(f.framework)"' in html
        for fw in ("EU AI Act", "NIST AI RMF", "ISO/IEC 42001", "LGPD", "OWASP LLM Top 10"):
            assert f"'{fw}':" in html, fw
        assert "Controles que atendem este requisito" in html

    def test_auditoria_exibe_usuario(self):
        html = (_PAGES / "ia_responsavel.html").read_text(encoding="utf-8")
        assert "e.actor_name || e.actor || '—'" in html

    @pytest.mark.asyncio
    async def test_audit_resolve_actor_para_nome(self, monkeypatch):
        # a resolução é BATELADA (reusa _resolve_user_names do dashboard) —
        # N+1 sequencial foi achado de revisão adversarial.
        import app.routes.governance as G
        import app.routes.dashboard as D
        monkeypatch.setattr(G, "audit_repo", FakeAudit([
            {"id": 2, "action": "a", "actor": "u1"},   # user_id cru (fallback do repo)
            {"id": 1, "action": "b", "actor": "gov"},  # username explícito
            {"id": 0, "action": "c"},                  # sem actor
        ]))
        calls = []

        async def _names(ids):
            calls.append(sorted(ids))
            return {"u1": "Ana Souza"}
        monkeypatch.setattr(D, "_resolve_user_names", _names)
        r = await G.governance_audit(limit=10, user={"role": "root"})
        by = {e["id"]: e for e in r["events"]}
        assert by[2]["actor_name"] == "Ana Souza" and by[2]["actor"] == "u1"
        assert by[1]["actor_name"] == "gov"   # não-id: cai no valor cru
        assert by[0]["actor_name"] is None
        assert calls == [["gov", "u1"]]       # 1 chamada batelada, actors distintos

    @pytest.mark.asyncio
    async def test_audit_lookup_falha_nao_quebra(self, monkeypatch):
        import app.routes.governance as G
        import app.routes.dashboard as D

        async def _boom(ids):
            raise RuntimeError("db down")
        monkeypatch.setattr(G, "audit_repo", FakeAudit([
            {"id": 1, "action": "a", "actor": "u1"},
        ]))
        monkeypatch.setattr(D, "_resolve_user_names", _boom)
        r = await G.governance_audit(limit=5, user={})
        assert r["events"][0]["actor_name"] == "u1"  # cai no cru, nunca 500

    @pytest.mark.asyncio
    async def test_security_events_tambem_resolvem_actor(self, monkeypatch):
        import app.routes.governance as G
        import app.routes.dashboard as D
        monkeypatch.setattr(G, "audit_repo", FakeAudit([
            {"id": 1, "action": "prompt_injection_blocked", "actor": "u1"},
        ]))

        async def _names(ids):
            return {"u1": "ana"}
        monkeypatch.setattr(D, "_resolve_user_names", _names)
        r = await G.governance_security_events(limit=10, user={})
        assert r["events"][0]["actor_name"] == "ana"

    @pytest.mark.asyncio
    async def test_security_events_clampa_limit(self, monkeypatch):
        # sem clamp, limit=100000 na query string viraria até 1000 linhas com
        # resolução de actor por linha (achado da revisão adversarial).
        import app.routes.governance as G
        import app.routes.dashboard as D
        monkeypatch.setattr(G, "audit_repo", FakeAudit([
            {"id": i, "action": "prompt_injection_blocked", "actor": f"u{i}"}
            for i in range(300)
        ]))

        async def _names(ids):
            return {}
        monkeypatch.setattr(D, "_resolve_user_names", _names)
        r = await G.governance_security_events(limit=100000, user={})
        assert len(r["events"]) == 200  # clamp igual ao /audit

    def test_textos_honestos_sobre_runtime(self):
        # revisão adversarial do 64.2.0: a decisão do OPA NÃO é "apenas
        # aplicada" (guarda local é AND autoritativo; failsafe decide com o OPA
        # fora). Desde 65.0.0 dlp_redact_before_llm É aplicada pelo runtime —
        # o aviso de "não aplicada" precisa ter SUMIDO (senão a UI mente ao
        # contrário) e o texto pode afirmar o comportamento pré-LLM.
        html = (_PAGES / "ia_responsavel.html").read_text(encoding="utf-8")
        assert "opção ainda não aplicada pelo runtime" not in html
        assert "envia ao provedor LLM" in html
        assert "apenas aplica a decisão" not in html
        assert "a guarda de injeção continua autoritativa" in html
