"""Lista de Skills 66.3.0 — paginação, filtro por usuário, "em uso" e
painel Governança & Autoria; skills passam a AUDITAR created/updated.

Espelha a rodada 66.2.0 da tela de agentes: autoria via helper genérico
(app/core/authorship.py, whitelist), enriquecimento OPT-IN (?enriched=1),
tudo batelado e best-effort. Novidade estrutural: skills não auditavam NADA
— autoria só existe do 66.3.0 em diante (skills antigas mostram "—").
"""
from __future__ import annotations

from pathlib import Path

import pytest

_PAGE = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "skills.html"


class FakeRepo:
    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]

    async def find_all(self, limit=100, offset=0, **f):
        out = [r for r in self.rows if all(r.get(k) == v for k, v in f.items())]
        return out[:limit]

    async def find_by_id(self, i):
        return next((r for r in self.rows if r.get("id") == i), None)

    async def count(self, **f):
        return len([r for r in self.rows if all(r.get(k) == v for k, v in f.items())])

    async def create(self, row):
        self.rows.append(dict(row))

    async def update(self, i, patch):
        for r in self.rows:
            if r.get("id") == i:
                r.update(patch)
                return dict(r)
        return None


class TestWhitelistEHelper:
    def test_whitelist_de_skills(self):
        import app.routes.skills as S
        assert set(S._SKILL_CHANGE_ACTIONS) == {"created", "updated"}

    def test_helper_generico_compartilhado(self):
        # agents e skills usam o MESMO helper (authorship.py) — a regra da
        # whitelist vive num lugar só.
        import inspect
        import app.core.authorship as AU
        src = inspect.getsource(AU.audit_entity_authorship)
        assert "action = ANY($2::text[])" in src
        assert "entity_type=$2" in src or "entity_type=$3" in src


_RAW_DECL = (
    "---\nid: urn:skill:t:subagent:x\nversion: 0.1.0\nkind: subagent\n"
    "owner: t\nstability: alpha\nexecution_mode: declarative\n---\n"
    "# Skill X\n\n## Purpose\np\n\n## API Bindings\nb\n"
)


class TestListEnriched:
    @pytest.mark.asyncio
    async def test_enriched_junta_autoria_e_uso(self, monkeypatch):
        import app.routes.skills as S
        import app.core.database as DB
        monkeypatch.setattr(S, "skills_repo", FakeRepo([
            {"id": "s1", "name": "Skill X", "kind": "subagent", "raw_content": _RAW_DECL},
            {"id": "s2", "name": "Skill órfã", "kind": "subagent"},
        ]))
        monkeypatch.setattr(DB, "agents_repo", FakeRepo([
            {"id": "a1", "name": "Agente 1", "status": "active", "skill_id": "s1"},
            {"id": "a2", "name": "Agente 2", "status": "inactive", "skill_id": "s1"},
            {"id": "a3", "name": "Sem skill", "status": "active", "skill_id": None},
        ]))

        async def _auth(entity_type, ids, actions):
            assert entity_type == "skill" and actions == S._SKILL_CHANGE_ACTIONS
            return {"s1": {"created_by_name": "Ana", "updated_by_name": "Beto"}}
        import app.core.authorship as AU
        monkeypatch.setattr(AU, "audit_entity_authorship", _auth)
        r = await S.list_skills(enriched=True)
        by = {s["id"]: s for s in r["skills"]}
        assert by["s1"]["in_use"] is True and len(by["s1"]["used_by"]) == 2
        assert by["s1"]["used_by"][0]["name"] == "Agente 1"
        assert by["s1"]["created_by_name"] == "Ana"
        assert by["s2"]["in_use"] is False and by["s2"]["used_by"] == []
        # verdade PARSEADA no payload (revisão: a UI não pode duplicar a gramática)
        assert by["s1"]["execution_mode"] == "declarative"
        assert "Purpose" in by["s1"]["declared_sections"]
        assert "API Bindings" in by["s1"]["declared_sections"]
        assert "Workflow" in by["s1"]["missing_required"]

    @pytest.mark.asyncio
    async def test_falha_no_lookup_de_agentes_nao_vira_sem_uso(self, monkeypatch):
        # revisão: falha transitória virava badge "SEM USO" confiante em TODAS
        # as skills — indisponível tem que deixar as chaves AUSENTES.
        import app.routes.skills as S
        import app.core.database as DB
        monkeypatch.setattr(S, "skills_repo", FakeRepo([
            {"id": "s1", "name": "Skill X", "kind": "subagent", "raw_content": _RAW_DECL},
        ]))

        class Boom:
            async def count(self):
                raise RuntimeError("db down")

            async def find_all(self, **k):
                raise RuntimeError("db down")
        monkeypatch.setattr(DB, "agents_repo", Boom())

        async def _auth(entity_type, ids, actions):
            return {}
        import app.core.authorship as AU
        monkeypatch.setattr(AU, "audit_entity_authorship", _auth)
        r = await S.list_skills(enriched=True)
        s = r["skills"][0]
        assert "used_by" not in s and "in_use" not in s   # neutro, não "SEM USO"
        assert s["execution_mode"] == "declarative"        # parse independe do lookup

    def test_extract_section_names_segue_a_gramatica_do_parser(self):
        # revisão: a regex client-side aceitava '##Inputs', '## INPUTS' e
        # '### Inputs' — o helper público espelha o parser (caso e âncora).
        from app.skill_parser.parser import extract_section_names
        raw = ("---\nid: u\n---\n# N\n\n## Inputs\nx\n\n##Grudado\ny\n\n"
               "### Inputs Sub\nz\n\n## INPUTS\nw\n")
        names = extract_section_names(raw)
        assert "Inputs" in names
        assert "INPUTS" in names            # nome EXATO distinto (case-sensitive)
        assert "Grudado" not in names       # '##' sem espaço não é seção
        assert all(not n.startswith("Inputs Sub") for n in names)  # H3 não é seção

    @pytest.mark.asyncio
    async def test_sem_enriched_payload_intacto(self, monkeypatch):
        import app.routes.skills as S
        monkeypatch.setattr(S, "skills_repo", FakeRepo([
            {"id": "s1", "name": "Skill X", "kind": "subagent"},
        ]))
        r = await S.list_skills()
        assert "in_use" not in r["skills"][0]
        assert "created_by_name" not in r["skills"][0]


class TestSkillsAuditam:
    @pytest.mark.asyncio
    async def test_update_grava_action_updated(self, monkeypatch):
        import app.routes.skills as S
        audit = FakeRepo([])
        monkeypatch.setattr(S, "audit_repo", audit)
        monkeypatch.setattr(S, "skills_repo", FakeRepo([
            {"id": "s1", "name": "Skill X", "version": "0.1.0",
             "raw_content": "---\nid: urn:skill:t:subagent:x\nversion: 0.1.0\n"
                            "kind: subagent\nowner: t\nstability: alpha\n---\n"
                            "## Purpose\nResponder.\n"},
        ]))

        async def _noop(*a, **k):
            return None
        import app.core.revisions as REV
        monkeypatch.setattr(REV, "safe_backfill", _noop)
        monkeypatch.setattr(REV, "safe_record", _noop)

        async def _warn(parsed):
            return []
        monkeypatch.setattr(S, "_warn_unknown_evidence_sources", _warn)
        await S._persist_skill_update(
            "s1",
            "---\nid: urn:skill:t:subagent:x\nversion: 0.1.0\nkind: subagent\n"
            "owner: t\nstability: alpha\n---\n## Purpose\nResponder melhor.\n",
            None)
        acts = [r for r in audit.rows if r.get("action") == "updated"]
        assert len(acts) == 1
        assert acts[0]["entity_type"] == "skill" and acts[0]["entity_id"] == "s1"

    def test_creates_auditam_estruturalmente(self):
        # os DOIS caminhos de criação (raw + manual) auditam 'created'; o miolo
        # compartilhado (PUT + rollback) audita 'updated'. Tudo via _audit_skill
        # (BEST-EFFORT: a skill já persistiu — falha de auditoria não vira 500).
        src = (Path(__file__).resolve().parent.parent / "app" / "routes" / "skills.py").read_text(encoding="utf-8")
        assert src.count('_audit_skill("created"') == 2
        assert src.count('_audit_skill("updated"') == 1
        assert "except Exception" in src[src.index("async def _audit_skill"):src.index("async def _audit_skill") + 900]


class TestTemplate:
    def test_filtro_usuario_e_paginacao(self):
        html = _PAGE.read_text(encoding="utf-8")
        for t in ("skills-filter-user", "skills-pagination", "skills-pagesize"):
            assert f'data-testid="{t}"' in html, t
        assert "{ v: 10, l: '10' }" in html and "{ v: 20, l: '20' }" in html
        assert "{ v: 50, l: '50' }" in html and "{ v: 0, l: 'Todos' }" in html
        assert "get pagedSkills()" in html and "get distinctUsers()" in html
        assert "Math.min(this.skPage, this.skPageCount)" in html  # clamp
        assert "enriched=1&limit=500" in html
        assert "this.searchText.trim().toLowerCase()" in html      # needle com trim
        assert "this.filterUser = ''" in html                      # reset fantasma

    def test_linha_mostra_uso_usuario_declarativa(self):
        html = _PAGE.read_text(encoding="utf-8")
        assert "EM USO (" in html and ">SEM USO<" in html
        assert "skill.updated_by_name || skill.created_by_name" in html
        assert ">DECLARATIVA<" in html and "isDeclarative(" in html
        # revisão: a verdade vem do payload parseado, nunca de regex client-side
        assert "=== 'declarative'" in html
        assert "execution_mode:\\s*declarative" not in html
        assert "declared_sections" in html and "missing_required" in html
        # canon inclui as seções que a versão anterior omitia
        assert "'Workflow'" in html and "'API Bindings'" in html
        # serverTotal + exclusão sem pulo de página (padrões da rodada de agents)
        assert "serverTotal" in html and "no servidor" in html
        i_del = html.index("async deleteSkill(")
        assert "this.filtered = this.filtered.filter" in html[i_del:i_del + 900]

    def test_painel_governanca_uso_secoes(self):
        html = _PAGE.read_text(encoding="utf-8")
        for t in ("skill-preview-governance", "skill-preview-usedby", "skill-preview-sections"):
            assert f'data-testid="{t}"' in html, t
        for campo in ("Criado por", "Última alteração", "Modo de execução",
                      "Em uso por", "Seções declaradas"):
            assert campo in html, campo
        assert "skillSections(" in html
