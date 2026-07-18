"""Wizard de Skill: '+ Novo' domínio, espelhando o campo Domínio do Agente.

User pediu (2026-07-18): no wizard "Gerar SKILL.md" o campo Domínio era só um
combobox (datalist) — dava pra digitar um domínio novo, mas ele NÃO era
registrado no catálogo de governança (`POST /api/v1/domains`), então nascia
órfão (não voltava na lista, some para os outros wizards). O Agente já tinha o
"+ novo" que persiste. Aqui damos a mesma capacidade à Skill.
"""
from __future__ import annotations

from pathlib import Path

import pytest

PAGES = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages"


@pytest.fixture(scope="module")
def skill_html() -> str:
    return (PAGES / "skill_form.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def agent_html() -> str:
    return (PAGES / "agent_form.html").read_text(encoding="utf-8")


class TestBotaoNovoDominio:
    def test_botao_novo_existe(self, skill_html):
        assert 'data-testid="wizard-domain-new"' in skill_html
        assert "+ Novo" in skill_html

    def test_botao_so_aparece_com_dominio_inedito(self, skill_html):
        # evita botão no-op: só habilita quando o texto digitado ainda não existe.
        assert 'x-show="wizardDomainIsNew"' in skill_html

    def test_datalist_preservado(self, skill_html):
        # não regride a escolha por lista existente.
        assert 'list="wizard-domain-list"' in skill_html
        assert '<datalist id="wizard-domain-list">' in skill_html


class TestPersistenciaEspelhaAgente:
    def test_registra_via_post_domains(self, skill_html):
        # o novo domínio é PERSISTIDO no catálogo (não fica só na skill).
        assert "async addWizardDomain()" in skill_html
        assert "api.post('/api/v1/domains', { name })" in skill_html

    def test_recarrega_a_lista_apos_criar(self, skill_html):
        # reflete no datalist imediatamente (fica reutilizável).
        assert "this.wizardDomains = (await api.get('/api/v1/domains')).domains || []" in skill_html

    def test_getter_de_ineditismo_case_insensitive(self, skill_html):
        assert "get wizardDomainIsNew()" in skill_html
        assert ".toLowerCase()" in skill_html

    def test_paridade_de_endpoint_com_agente(self, skill_html, agent_html):
        # os dois wizards registram domínio no MESMO endpoint de governança.
        assert "api.post('/api/v1/domains'" in skill_html
        assert "api.post('/api/v1/domains'" in agent_html


class TestDegradacaoRBAC:
    def test_note_para_usuario_sem_permissao(self, skill_html):
        # 403 (comum) não é engolido em silêncio: avisa que vale só como rótulo.
        assert 'data-testid="wizard-domain-note"' in skill_html
        assert "wizardDomainNote" in skill_html
