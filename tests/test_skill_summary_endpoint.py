"""GET /api/v1/skills/{id} agora devolve `summary` com metadata parsed.

UI da step Revisão do agent_form consome esse summary pra mostrar Detalhes
da Skill (URN, execution_mode, threshold da evidence policy, contagens de
bindings, seções preenchidas). Sem parsear o YAML no frontend.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import skills_repo
from app.routes.skills import router as skills_router


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(skills_router)
    return TestClient(app)


_SKILL_WITH_THRESHOLD = """---
id: urn:skill:fin:subagent:consultar-kb
version: 0.1.0
kind: subagent
owner: equipe-ia
stability: alpha
---

# Consulta KB

## Purpose
Consulta o KB.

## Activation Criteria
Sempre.

## Inputs
{}

## Workflow
1. busca

## Tool Bindings
(Nenhuma ferramenta MCP foi selecionada para esta skill. Esta seção DEVE permanecer com a declaração abaixo — NÃO invente nomes de tools.)

_Esta skill não usa ferramentas MCP. Recursos disponíveis: RAG (Evidence Policy)._

## Output Contract
{}

## Failure Modes
- timeout

## Evidence Policy

```yaml
sources:
  - ks-abc-123
min_relevance: 0.15
max_age_days: 90
cite_sources: true
```

## Guardrails
- PII

## Execution Profile
mode: standard
"""


_SKILL_LEGACY_NO_EVIDENCE_POLICY = """---
id: urn:skill:ops:subagent:simple
version: 0.1.0
kind: subagent
owner: x
stability: alpha
---

# Simple

## Purpose
x

## Activation Criteria
x

## Inputs
{}

## Workflow
1. x

## Tool Bindings
- `tool-1` (Search) — busca

## Output Contract
{}

## Failure Modes
- x
"""


class TestSkillSummary:
    def test_endpoint_returns_summary_with_threshold(self, monkeypatch):
        """Skill com min_relevance no Evidence Policy → summary expõe o valor."""
        async def fake_find(_id):
            return {"id": _id, "name": "Consulta KB", "raw_content": _SKILL_WITH_THRESHOLD}
        monkeypatch.setattr(skills_repo, "find_by_id", fake_find)

        r = _make_client().get("/api/v1/skills/skill-1")
        assert r.status_code == 200
        body = r.json()
        assert "summary" in body
        s = body["summary"]
        assert s["urn"] == "urn:skill:fin:subagent:consultar-kb"
        assert s["execution_mode"] in ("standard", "fast", "rigorous", "declarative")
        # Evidence policy parsed (todos os campos opcionais)
        ev = s["evidence_policy_parsed"]
        assert ev["sources"] == ["ks-abc-123"]
        assert ev["min_relevance"] == 0.15
        assert ev["max_age_days"] == 90
        assert ev["cite_sources"] is True
        # Bindings: skill declarou "Sem MCP" explicitamente
        assert s["tool_bindings_explicit_none"] is True
        assert s["tool_bindings_count"] == 0

    def test_endpoint_legacy_skill_no_evidence_policy(self, monkeypatch):
        """Skill sem ## Evidence Policy → summary tem evidence_policy_parsed
        vazio (UI mostra default 0.30). Tool count detecta o item da skill."""
        async def fake_find(_id):
            return {"id": _id, "name": "Simple", "raw_content": _SKILL_LEGACY_NO_EVIDENCE_POLICY}
        monkeypatch.setattr(skills_repo, "find_by_id", fake_find)

        r = _make_client().get("/api/v1/skills/skill-2")
        assert r.status_code == 200
        body = r.json()
        s = body["summary"]
        # evidence_policy_parsed vazio quando skill não declara
        assert s["evidence_policy_parsed"].get("min_relevance") is None
        # Tool count detecta a única tool listada com "- `tool-1`"
        assert s["tool_bindings_count"] == 1
        assert s["tool_bindings_explicit_none"] is False

    def test_endpoint_404_when_skill_missing(self, monkeypatch):
        async def fake_find(_id):
            return None
        monkeypatch.setattr(skills_repo, "find_by_id", fake_find)
        r = _make_client().get("/api/v1/skills/nope")
        assert r.status_code == 404

    def test_endpoint_resilient_to_broken_raw_content(self, monkeypatch):
        """raw_content que faz o parser explodir não derruba o GET — summary
        fica ausente, UI esconde Detalhes da Skill."""
        async def fake_find(_id):
            # raw_content que pode confundir o parser
            return {"id": _id, "name": "Quebrada", "raw_content": "##\nincomplete\n```yaml\n[malformed:"}
        monkeypatch.setattr(skills_repo, "find_by_id", fake_find)
        r = _make_client().get("/api/v1/skills/skill-broken")
        assert r.status_code == 200
        # Parser é defensivo — pode devolver summary parcial ou nenhum,
        # mas NÃO pode quebrar 500.
        body = r.json()
        # Pode ou não ter summary; o importante é GET ter retornado 200
        if "summary" in body:
            # Se veio, é dict válido (não None)
            assert isinstance(body["summary"], dict)


class TestAgentFormReviewPanel:
    """Smoke estrutural do HTML do step Revisão — UI mostra Detalhes da Skill."""

    @pytest.fixture(scope="class")
    def html(self):
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "agent_form.html"
        return path.read_text(encoding="utf-8")

    def test_skill_summary_state_in_alpine(self, html):
        assert "skillSummary: null" in html or "skillSummary:null" in html

    def test_load_skill_summary_method_defined(self, html):
        assert "async loadSkillSummary()" in html or "loadSkillSummary()" in html

    def test_details_panel_renders_threshold_with_source(self, html):
        """Painel mostra 'min_relevance.toFixed(2) + (skill)' quando declarado,
        senão '0.30 (default do engine)' — espelha a lógica do PR #163."""
        assert "min_relevance" in html
        assert "(default do engine)" in html
        assert "(skill)" in html

    def test_details_panel_renders_bindings_counts(self, html):
        """Painel cobre tool/api/tables counts."""
        assert "tool_bindings_count" in html
        assert "api_bindings_count" in html
        assert "data_tables_count" in html

    def test_details_panel_only_when_skill_linked(self, html):
        """Painel envolto em x-if='form.skill_id && skillSummary' — esconde
        quando user não vinculou skill ou GET falhou."""
        assert 'x-if="form.skill_id && skillSummary"' in html
