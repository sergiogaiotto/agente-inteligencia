"""Cockpit OPA Fase B (63.0.0) — núcleo `opa_policies` (edição/versão/re-push).

Valida: guard do `package` declarado (impede bypass por namespace novo), o
versionamento append-only (vigente = maior version), e o re-push no boot só da
vigente de pacotes com override. Push ao OPA e o repo são mockados.
"""
from __future__ import annotations

import asyncpg
import pytest

from app.core import opa_policies as P


class _FakeRepo:
    def __init__(self, rows=None):
        self.rows = [dict(r) for r in (rows or [])]

    async def find_all(self, limit=100, offset=0, **f):
        out = [r for r in self.rows if all(r.get(k) == v for k, v in f.items())]
        return out[:limit]

    async def create(self, row):
        self.rows.append(dict(row))
        return row.get("id")


class TestHelpers:
    def test_pkg_from_id(self):
        assert P.pkg_from_id("policies/interaction.rego") == "interaction"
        assert P.pkg_from_id("tool_invocation") == "tool_invocation"

    def test_policy_id_for_reusa_o_id_baked(self):
        assert P.policy_id_for("interaction") == "policies/interaction.rego"

    def test_read_baked(self):
        raw = P.read_baked("interaction")
        assert raw and "package interaction" in raw
        assert P.read_baked("naoexiste") is None

    def test_validate_package_decl(self):
        assert P.validate_package_decl("interaction", "package interaction\nallow := true") is None
        assert P.validate_package_decl("interaction", "package interaction # nota\n") is None
        # pacote errado no corpo → erro (impede bypass por namespace)
        err = P.validate_package_decl("interaction", "package tool_invocation\n")
        assert err and "package interaction" in err
        # prefixo NÃO conta (interaction ≠ interaction_v2)
        assert P.validate_package_decl("interaction", "package interaction_v2\n") is not None
        # sub-pacote PONTUADO NÃO conta (interaction ≠ interaction.v2) — era o furo
        # do \b: substituiria o doc por data.interaction.v2 = deny-all silencioso.
        assert P.validate_package_decl("interaction", "package interaction.v2\nallow := true") is not None
        assert P.validate_package_decl("interaction", "package interaction.sub\n") is not None


class TestVersioning:
    @pytest.mark.asyncio
    async def test_save_version_incrementa(self, monkeypatch):
        repo = _FakeRepo([])
        monkeypatch.setattr(P, "governance_policy_repo", repo)
        v1 = await P.save_version("interaction", "package interaction", "n1", "gov")
        v2 = await P.save_version("interaction", "package interaction\nx", "n2", "gov")
        assert (v1, v2) == (1, 2) and len(repo.rows) == 2

    @pytest.mark.asyncio
    async def test_current_version_e_a_maior(self, monkeypatch):
        repo = _FakeRepo([
            {"package": "interaction", "version": 1, "rego": "a"},
            {"package": "interaction", "version": 3, "rego": "c"},
            {"package": "interaction", "version": 2, "rego": "b"},
            {"package": "tool_invocation", "version": 5, "rego": "x"},
        ])
        monkeypatch.setattr(P, "governance_policy_repo", repo)
        cur = await P.current_version("interaction")
        assert cur["version"] == 3 and cur["rego"] == "c"
        assert (await P.current_version("evidence")) is None


class _RaceRepo:
    """Simula a corrida: o 1º create colide no UNIQUE(package,version)."""
    def __init__(self):
        self.rows, self.attempts = [], 0

    async def find_all(self, limit=100, offset=0, **f):
        return [r for r in self.rows if all(r.get(k) == v for k, v in f.items())][:limit]

    async def create(self, row):
        self.attempts += 1
        if self.attempts == 1:
            raise asyncpg.exceptions.UniqueViolationError("dup version")
        self.rows.append(dict(row))
        return row.get("id")


class TestSaveVersionRace:
    @pytest.mark.asyncio
    async def test_retry_na_colisao_de_versao(self, monkeypatch):
        repo = _RaceRepo()
        monkeypatch.setattr(P, "governance_policy_repo", repo)
        ver = await P.save_version("interaction", "package interaction", "n", "gov")
        assert ver == 1 and repo.attempts == 2 and len(repo.rows) == 1


class TestRevertOpa:
    @pytest.mark.asyncio
    async def test_sem_prev_usa_baked(self, monkeypatch):
        pushed = []

        async def _push(pid, rego):
            pushed.append((pid, rego))
            return {"ok": True, "kind": "ok", "error": None}
        monkeypatch.setattr(P.opa_client, "push_policy", _push)
        await P.revert_opa("interaction", None)
        assert pushed and pushed[0][0] == "policies/interaction.rego" and "package interaction" in pushed[0][1]

    @pytest.mark.asyncio
    async def test_com_prev_reempurra_o_prev(self, monkeypatch):
        pushed = []

        async def _push(pid, rego):
            pushed.append(rego)
            return {"ok": True, "kind": "ok", "error": None}
        monkeypatch.setattr(P.opa_client, "push_policy", _push)
        await P.revert_opa("interaction", "package interaction\n# PREV")
        assert pushed and "# PREV" in pushed[0]


class TestValidateAndPush:
    @pytest.mark.asyncio
    async def test_pacote_errado_kind_invalid(self):
        res = await P.validate_and_push("interaction", "package tool_invocation\n")
        assert res["ok"] is False and res["kind"] == "invalid"

    @pytest.mark.asyncio
    async def test_pacote_errado_nem_chega_no_opa(self, monkeypatch):
        called = []

        async def _push(pid, rego):
            called.append(pid)
            return {"ok": True, "error": None}
        monkeypatch.setattr(P.opa_client, "push_policy", _push)
        res = await P.validate_and_push("interaction", "package tool_invocation\n")
        assert res["ok"] is False and not called

    @pytest.mark.asyncio
    async def test_ok_empurra_com_o_id_baked(self, monkeypatch):
        seen = {}

        async def _push(pid, rego):
            seen["pid"] = pid
            return {"ok": True, "error": None}
        monkeypatch.setattr(P.opa_client, "push_policy", _push)
        res = await P.validate_and_push("interaction", "package interaction\nallow := true")
        assert res["ok"] is True and seen["pid"] == "policies/interaction.rego"


class TestEvidenceAllows:
    """Evidence ACL (64.0.0): 'no read up' via evidence.rego."""
    _RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3, "secret": 3}

    def _fake_sim(self):
        async def _sim(pkg, rule="allow", input_doc=None):
            c = self._RANK.get((input_doc["user"]["clearance"]), -1)
            e = self._RANK.get((input_doc["evidence"]["confidentiality"]), 99)
            return {"allow": c >= e, "source": "opa"}
        return _sim

    @pytest.mark.asyncio
    async def test_no_read_up(self, monkeypatch):
        monkeypatch.setattr(P.opa_client, "simulate", self._fake_sim())
        assert await P.evidence_allows("confidential", "internal") is True    # 2>=1
        assert await P.evidence_allows("internal", "confidential") is False   # 1<2
        assert await P.evidence_allows("restricted", "secret") is True        # 3>=3 (aliases)
        assert await P.evidence_allows(None, None) is True                    # internal>=internal (defaults)
        assert await P.evidence_allows("public", "internal") is False         # 0<1

    @pytest.mark.asyncio
    async def test_opa_fora_segue_failsafe(self, monkeypatch):
        import types

        async def _sim(pkg, rule="allow", input_doc=None):
            return {"allow": None, "source": "error"}
        monkeypatch.setattr(P.opa_client, "simulate", _sim)
        monkeypatch.setattr(P, "get_settings", lambda: types.SimpleNamespace(opa_failsafe_open=True))
        assert await P.evidence_allows("public", "secret") is True   # failsafe aberto → allow
        monkeypatch.setattr(P, "get_settings", lambda: types.SimpleNamespace(opa_failsafe_open=False))
        assert await P.evidence_allows("public", "secret") is False  # failsafe fechado → deny


class TestRepushOnBoot:
    @pytest.mark.asyncio
    async def test_reempurra_so_a_vigente_de_pacotes_com_override(self, monkeypatch):
        repo = _FakeRepo([
            {"package": "interaction", "version": 2, "rego": "package interaction v2"},
            {"package": "interaction", "version": 1, "rego": "old"},
        ])
        monkeypatch.setattr(P, "governance_policy_repo", repo)
        pushed = []

        async def _push(pid, rego):
            pushed.append((pid, rego))
            return {"ok": True, "error": None}
        monkeypatch.setattr(P.opa_client, "push_policy", _push)
        rp = await P.repush_policies_on_boot()
        assert rp["pushed"] == ["interaction@v2"]  # só a vigente; tool/evidence sem override
        assert pushed == [("policies/interaction.rego", "package interaction v2")]

    @pytest.mark.asyncio
    async def test_erro_de_push_vai_pra_errors(self, monkeypatch):
        repo = _FakeRepo([{"package": "interaction", "version": 1, "rego": "x"}])
        monkeypatch.setattr(P, "governance_policy_repo", repo)

        async def _push(pid, rego):
            return {"ok": False, "error": "opa down"}
        monkeypatch.setattr(P.opa_client, "push_policy", _push)
        rp = await P.repush_policies_on_boot()
        assert rp["pushed"] == [] and any("opa down" in e for e in rp["errors"])
