"""Resolução em cascata do idioma de resposta (agent > settings > fallback).

User reportou (2026-05-28): agente _Qresearch_ rodou pesquisa Tavily real
mas retornou resposta em INGLÊS porque o LLM (gpt-oss-120b) espelhou o
idioma dos resultados retornados pela busca. Sem instrução explícita de
idioma no system_prompt, modelos open-weight tendem a esse comportamento.

Fix arquitetural:
- settings.default_response_language (default pt-BR)
- agents.response_language (Optional, override por agente)
- Engine prepende diretiva no system_prompt em toda chamada LLM

Resolução em cascata: agent.response_language > settings.default > "pt-BR".
"""
from __future__ import annotations

import pytest

from app.agents.engine import (
    _build_response_language_directive,
    _resolve_response_language,
)


class _FakeSettings:
    def __init__(self, default_response_language: str = "pt-BR"):
        self.default_response_language = default_response_language


class TestResolveResponseLanguage:
    def test_agent_override_takes_precedence(self):
        """Agent define o idioma → ganha do default global."""
        settings = _FakeSettings(default_response_language="pt-BR")
        agent = {"response_language": "en-US"}
        assert _resolve_response_language(agent, settings) == "en-US"

    def test_falls_back_to_settings_when_agent_empty(self):
        """Agent sem override → herda do platform default."""
        settings = _FakeSettings(default_response_language="es-ES")
        agent = {"response_language": ""}
        assert _resolve_response_language(agent, settings) == "es-ES"

    def test_falls_back_to_settings_when_agent_none(self):
        """Campo NULL (Pydantic None) — comportamento idêntico a string vazia."""
        settings = _FakeSettings(default_response_language="fr-FR")
        agent = {"response_language": None}
        assert _resolve_response_language(agent, settings) == "fr-FR"

    def test_hard_fallback_when_settings_also_empty(self):
        """Settings corrompido (vazio) — engine ainda devolve pt-BR seguro."""
        settings = _FakeSettings(default_response_language="")
        agent = {"response_language": None}
        assert _resolve_response_language(agent, settings) == "pt-BR"

    def test_agent_strip_whitespace(self):
        """Resolve precisa lidar com whitespace defensivamente."""
        settings = _FakeSettings(default_response_language="pt-BR")
        agent = {"response_language": "  en-US  "}
        assert _resolve_response_language(agent, settings) == "en-US"

    def test_agent_dict_without_response_language_key(self):
        """Agent legacy sem o campo migrado — não estoura, herda settings."""
        settings = _FakeSettings(default_response_language="pt-BR")
        agent = {"name": "legacy"}  # sem response_language
        assert _resolve_response_language(agent, settings) == "pt-BR"

    def test_empty_agent_dict(self):
        settings = _FakeSettings(default_response_language="pt-BR")
        assert _resolve_response_language({}, settings) == "pt-BR"


class TestBuildResponseLanguageDirective:
    def test_directive_uses_human_label_for_known_tags(self):
        """pt-BR → texto humano 'português brasileiro' — modelos open-weight
        respondem melhor com nome da língua do que com tag técnica."""
        d = _build_response_language_directive("pt-BR")
        assert "português brasileiro" in d
        assert "IDIOMA DA RESPOSTA" in d

    def test_directive_falls_back_to_raw_tag_for_unknown(self):
        """Tag não mapeada (ex: 'xh-ZA' Xhosa) cai no próprio tag — instrução
        ainda existe mas LLM precisa inferir."""
        d = _build_response_language_directive("xh-ZA")
        assert "xh-ZA" in d

    def test_directive_instructs_translation_when_evidence_in_other_language(self):
        """Caso EXATO do user: Tavily retorna inglês, resposta deve sair em
        pt-BR — directive precisa instruir explicitamente."""
        d = _build_response_language_directive("pt-BR")
        # Cita os caminhos de origem do conteúdo em outras línguas
        assert "RAG" in d or "evidências" in d.lower()
        assert "tools MCP" in d or "tools" in d
        # Instrução de tradução
        assert "Traduza" in d or "traduzir" in d.lower() or "adapte" in d.lower()

    def test_directive_preserves_proper_nouns_and_urls(self):
        """Tradução pode estragar nomes próprios e URLs — directive instrui
        a preservar (UX importante pra pesquisa web)."""
        d = _build_response_language_directive("pt-BR")
        assert "nomes próprios" in d.lower() or "nomes proprios" in d.lower()
        assert "URLs" in d or "urls" in d.lower()
