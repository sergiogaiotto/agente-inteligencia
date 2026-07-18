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
