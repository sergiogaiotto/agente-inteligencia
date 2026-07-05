"""Higiene de nome e cerca de código em skills (27.2.1).

Item #6 do teste E2E 2026-07-05:
- (6a) skill nomeada '## Evidence Policy': sem título H1, o parser adotava a
  primeira linha — um cabeçalho de seção — como `name`. Agora pula headings, e
  a rota rejeita (422) nomes que sejam cabeçalho/seção conhecida.
- (6b) o wizard devolvia o SKILL.md embrulhado na cerca ```markdown … ```. Agora
  descasca via helper compartilhado com o parser (strip_code_fence).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import skills_repo
from app.routes.skills import router as skills_router
from app.skill_parser.parser import parse_skill_md, strip_code_fence


# ─── strip_code_fence — helper compartilhado parser↔wizard (6b) ───
class TestStripCodeFence:
    def test_strips_markdown_fence(self):
        assert strip_code_fence("```markdown\n# Foo\n```") == "# Foo"

    def test_strips_bare_and_yaml_fence(self):
        assert strip_code_fence("```\nx\n```") == "x"
        assert strip_code_fence("```yaml\nk: v\n```") == "k: v"

    def test_noop_without_fence(self):
        assert strip_code_fence("# Foo\ntexto") == "# Foo\ntexto"

    def test_idempotent(self):
        once = strip_code_fence("```markdown\n# Foo\n```")
        assert strip_code_fence(once) == once


# ─── parser: um heading NÃO vira nome (6a) ───
class TestNameNotAHeading:
    def test_section_heading_not_adopted_as_name(self):
        parsed = parse_skill_md("## Evidence Policy\n\nalgum corpo\n")
        assert not parsed.name.startswith("#"), parsed.name
        assert parsed.name != "## Evidence Policy"

    def test_valid_h1_still_extracted(self):
        parsed = parse_skill_md("# Minha Skill\n\n## Purpose\nx\n")
        assert parsed.name == "Minha Skill"


# ─── rota: gate 422 pra nome que é cabeçalho/seção (6a) ───
def _client():
    app = FastAPI()
    app.include_router(skills_router)
    return TestClient(app, raise_server_exceptions=False)


_VALID_H1_MD = """---
id: urn:skill:geral:subagent:minha
version: 0.1.0
kind: subagent
owner: e
stability: alpha
---

# Minha Skill

## Purpose
Faz algo.

## Activation Criteria
Sempre.
"""

# Sem frontmatter e sem H1, a 1ª linha é o nome de uma SEÇÃO conhecida → o parser
# adota "Evidence Policy" como nome → o gate rejeita (422).
_SECTION_NAME_MD = "Evidence Policy\n\n## Purpose\nCorpo suficientemente longo aqui.\n"


def test_create_rejects_section_heading_name(monkeypatch):
    hits = {"n": 0}

    async def _should_not_create(_data):
        hits["n"] += 1

    monkeypatch.setattr(skills_repo, "create", _should_not_create)
    r = _client().post("/api/v1/skills", json={"raw_content": _SECTION_NAME_MD, "tags": "[]"})
    assert r.status_code == 422, r.text
    assert "H1" in r.json()["detail"]
    assert hits["n"] == 0  # nem chegou a persistir


def test_create_accepts_valid_h1(monkeypatch):
    async def _ok(_data):
        return None

    monkeypatch.setattr(skills_repo, "create", _ok)
    r = _client().post("/api/v1/skills", json={"raw_content": _VALID_H1_MD, "tags": "[]"})
    assert r.status_code == 201, r.text


# ─── wizard: o SKILL.md retornado sai SEM cerca (6b) ───
@pytest.mark.asyncio
async def test_wizard_skill_strips_fence(monkeypatch):
    from app.routes import wizard as _wiz

    fenced = (
        "```markdown\n---\nid: urn:skill:x:subagent:y\nversion: 0.1.0\n"
        "kind: subagent\nowner: e\nstability: alpha\n---\n\n# Minha Skill\n\n"
        "## Purpose\nx\n```"
    )

    async def _fake_complete(messages, provider, model, **k):
        return fenced, provider, model

    async def _fake_resolve(data, route):
        return ("gpt-oss-120b", "openai/gpt-oss-120b", "skill_generation")

    async def _fake_bindings(data):
        return {"mcp_tools": [], "rag_sources": [], "data_tables": [], "api_endpoints": []}

    monkeypatch.setattr(_wiz, "_wizard_llm_complete", _fake_complete)
    monkeypatch.setattr(_wiz, "_resolve_wizard_llm", _fake_resolve)
    monkeypatch.setattr(_wiz, "_resolve_bindings_for_prompt", _fake_bindings)

    out = await _wiz.wizard_skill(
        _wiz.WizardSkillRequest(description="x", kind="subagent", domain="d")
    )
    md = out["skill_md"]
    assert not md.lstrip().startswith("```"), md[:40]
    assert "```markdown" not in md
