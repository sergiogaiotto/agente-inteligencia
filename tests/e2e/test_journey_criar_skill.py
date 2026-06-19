"""Jornada E2E: criar uma Skill pela UI e vê-la na lista.

Caminho real: /skills/new → cola um SKILL.md mínimo (frontmatter válido +
## Purpose) → "Criar Skill" → redirect p/ /skills → a skill aparece na lista.
Determinístico (o parser de SKILL.md é local, não chama LLM). Limpa no teardown.
"""
from __future__ import annotations

import uuid

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

pytestmark = pytest.mark.e2e


def _skill_md(slug: str, name: str) -> str:
    # Frontmatter mínimo que o parser exige (id/version/kind/owner/stability) +
    # um heading e ## Purpose. Seções faltantes viram apenas avisos (não bloqueiam).
    return (
        "---\n"
        f"id: urn:skill:test:util:{slug}\n"
        "version: 0.1.0\n"
        "kind: subagent\n"
        "owner: e2e-team\n"
        "stability: alpha\n"
        "---\n\n"
        f"# {name}\n\n"
        "## Purpose\n"
        "Skill descartável criada por teste E2E para validar o fluxo de criação.\n"
    )


def test_criar_skill_aparece_na_lista(authed_page, api):
    page = authed_page
    slug = f"e2e-{uuid.uuid4().hex[:8]}"
    name = f"E2E Skill {slug}"

    page.goto("/skills/new", wait_until="domcontentloaded")

    raw = page.get_by_test_id("skill-raw")
    expect(raw).to_be_visible(timeout=10_000)
    raw.fill(_skill_md(slug, name))

    save = page.get_by_test_id("skill-save")
    expect(save).to_be_enabled()
    save.click()

    # save() redireciona p/ /skills no sucesso
    page.wait_for_url("**/skills", timeout=30_000)
    expect(
        page.get_by_test_id("skill-row-name").filter(has_text=name)
    ).to_be_visible(timeout=30_000)

    # ── teardown: remove a skill criada ──
    try:
        r = api.get("/api/v1/skills")
        if r.status_code == 200:
            skills = r.json()
            if isinstance(skills, dict):
                skills = skills.get("skills", skills.get("items", []))
            for s in skills:
                if s.get("name") == name and s.get("id"):
                    api.delete(f"/api/v1/skills/{s['id']}")
    except Exception:
        pass
