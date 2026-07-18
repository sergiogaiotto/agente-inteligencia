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
        for t in ("ia-responsavel", "ir-posture", "ir-forget", "ir-audit", "ir-capabilities", "ir-roadmap"):
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
