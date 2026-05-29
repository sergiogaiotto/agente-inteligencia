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
    _build_response_language_closing,
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

    def test_directive_preserves_urls_code_and_brand_names(self):
        """Diretiva preserva no original APENAS categorias seguras: URLs,
        código, marcas/produtos/pessoas. NÃO usar 'nomes próprios' genérico
        (regressão do bug 2026-05-28 _pesquisa: LLM enquadrava títulos de
        artigos como 'nomes próprios' e parava de traduzir)."""
        d = _build_response_language_directive("pt-BR")
        low = d.lower()
        # Categorias seguras citadas
        assert "url" in low
        assert "código" in low or "codigo" in low
        assert "marcas" in low or "produtos" in low or "pessoas" in low


class TestDirectiveCoversStructuredOutputFields:
    """Regressão do bug 2026-05-28 _pesquisa: Tavily retorna title/content em EN,
    LLM preenche Output Contract copiando crus em vez de traduzir.

    Fix: diretiva enumera campos JSON típicos pra ativar o gatilho lexical do
    modelo open-weight (gpt-oss-120b) e diferencia 'preservar nomes próprios'
    de 'preservar títulos de artigos' — antes o LLM confundia os dois.
    """

    def test_directive_lists_common_json_text_fields(self):
        """Modelos open-weight precisam ver os nomes dos campos explicitamente
        — sem isso, encaram a obrigação de tradução como genérica e copiam."""
        d = _build_response_language_directive("pt-BR")
        # Campos canônicos que aparecem em Output Contracts de skills de busca/RAG
        for field in ("title", "content", "snippet", "summary", "description"):
            assert f"`{field}`" in d, f"campo `{field}` faltando na diretiva"

    def test_directive_calls_out_titles_and_headlines_explicitly(self):
        """Bug observado: títulos de artigos do Tavily ficavam em EN porque
        LLM os tratava como 'nomes próprios'. Diretiva precisa desambiguar."""
        d = _build_response_language_directive("pt-BR")
        assert "títulos" in d.lower() or "titulos" in d.lower()

    def test_directive_restricts_preserve_list_to_safe_categories(self):
        """A permissão de 'preservar original' precisa ser RESTRITA — apenas
        URLs, código, IDs e nomes de marcas/produtos/pessoas. Antes era 'nomes
        próprios' genérico e o LLM enquadrava títulos de artigos aí."""
        d = _build_response_language_directive("pt-BR")
        low = d.lower()
        # As 4 categorias seguras precisam estar listadas
        assert "url" in low
        assert "código" in low or "codigo" in low
        assert "marcas" in low or "produtos" in low
        # E precisa diferenciar (ter uma palavra restritiva)
        assert "apenas" in low or "somente" in low

    def test_directive_explicitly_covers_json_structured_output(self):
        """Coverage do path estruturado — modelo precisa entender que a regra
        vale dentro de JSON, não só em prosa narrativa."""
        d = _build_response_language_directive("pt-BR")
        low = d.lower()
        assert "json" in low
        assert "campo" in low or "campos" in low


class TestBuildResponseLanguageClosing:
    """Reminder colado ao FIM do system prompt (estratégia sanduíche).

    Modelos open-weight grudam no que está mais perto da geração. Quando o
    prompt cresce com Output Contract + MCP Tools, a diretiva inicial perde
    força. O closing é um reminder curto, ancorado no final, antes do LLM
    começar a gerar.
    """

    def test_closing_uses_human_label(self):
        c = _build_response_language_closing("pt-BR")
        assert "português brasileiro" in c

    def test_closing_falls_back_to_raw_tag(self):
        c = _build_response_language_closing("xh-ZA")
        assert "xh-ZA" in c

    def test_closing_mentions_json_fields(self):
        """O ponto do reminder: reforçar especificamente a regra de campos JSON,
        que é onde o LLM mais escorrega."""
        c = _build_response_language_closing("pt-BR")
        assert "JSON" in c or "json" in c.lower()

    def test_closing_is_distinct_from_opening_directive(self):
        """Closing não pode ser cópia da diretiva — precisa ser texto novo pra
        funcionar como ancora separada. Se forem idênticas, modelo trata como
        repetição e ignora."""
        d = _build_response_language_directive("pt-BR")
        c = _build_response_language_closing("pt-BR")
        assert d != c
        # Marcadores diferentes pra serem percebidos como blocos distintos
        assert "[IDIOMA DA RESPOSTA]" in d
        assert "[LEMBRETE FINAL" in c

    def test_closing_is_short(self):
        """Reminder curto de propósito — é uma âncora, não uma instrução
        completa. Detalhe está na diretiva inicial."""
        c = _build_response_language_closing("pt-BR")
        # Liberal mas catches regressões absurdas
        assert len(c) < 400, f"closing ficou longo demais ({len(c)} chars)"


class TestSystemPromptSandwich:
    """Verifica que _build_system_prompt aplica o sanduíche — diretiva no topo
    + reminder no fim. Integração ponta-a-ponta do fix do bug do _pesquisa.
    """

    def _make_harness(self, *, skill: dict | None = None, mcp_tools: list | None = None):
        """Constrói DeepAgentHarness mínimo sem invocar provider real."""
        from unittest.mock import patch
        from app.agents.engine import DeepAgentHarness

        agent_cfg = {
            "id": "test-agent",
            "llm_provider": "openai",
            "model": "gpt-4o",
            "temperature": 0.0,
            "system_prompt": "Você é um pesquisador.",
            "response_language": "pt-BR",
            "_parsed_skill": skill or {},
        }
        with patch("app.agents.engine.get_provider") as mock_prov:
            mock_prov.return_value = type(
                "FakeProvider", (), {"supports_structured_output": False},
            )()
            return DeepAgentHarness(agent_cfg, mcp_tools=mcp_tools or [])

    def test_directive_appears_at_top(self):
        """Diretiva inicial precisa estar antes do system_prompt do agente."""
        h = self._make_harness()
        prompt = h._build_system_prompt()
        idx_directive = prompt.find("[IDIOMA DA RESPOSTA]")
        idx_system = prompt.find("Você é um pesquisador.")
        assert idx_directive >= 0, "diretiva ausente"
        assert idx_system >= 0
        assert idx_directive < idx_system, "diretiva deveria estar antes do system_prompt"

    def test_closing_appears_at_the_end(self):
        """Reminder precisa ser a ÚLTIMA âncora antes do LLM gerar — depois
        do Output Contract e do bloco de MCP Tools."""
        h = self._make_harness(
            skill={
                "output_contract": "```json\n{\"results\": []}\n```",
            },
            mcp_tools=[{"name": "tavily", "operations": ["search"], "mcp_server": "tavily"}],
        )
        prompt = h._build_system_prompt()
        idx_closing = prompt.find("[LEMBRETE FINAL")
        idx_output = prompt.find("## Output Contract")
        idx_tools = prompt.find("## Ferramentas Disponíveis (MCP)")
        assert idx_closing >= 0, "closing ausente"
        assert idx_output >= 0 and idx_closing > idx_output, "closing deveria vir depois do Output Contract"
        assert idx_tools >= 0 and idx_closing > idx_tools, "closing deveria vir depois do bloco MCP Tools"

    def test_sandwich_present_in_minimal_prompt(self):
        """Mesmo sem skill/tools, sanduíche existe — diretiva + closing."""
        h = self._make_harness()
        prompt = h._build_system_prompt()
        assert "[IDIOMA DA RESPOSTA]" in prompt
        assert "[LEMBRETE FINAL" in prompt
        # E na ordem certa
        assert prompt.index("[IDIOMA DA RESPOSTA]") < prompt.index("[LEMBRETE FINAL")

    def test_sandwich_uses_resolved_language_consistently(self):
        """Diretiva e closing devem usar o MESMO idioma — sem isso, modelo
        recebe sinal conflitante e fica oscilando."""
        from unittest.mock import patch
        from app.agents.engine import DeepAgentHarness

        agent_cfg = {
            "id": "test-agent",
            "llm_provider": "openai",
            "model": "gpt-4o",
            "temperature": 0.0,
            "system_prompt": "ok",
            "response_language": "es-ES",
            "_parsed_skill": {},
        }
        with patch("app.agents.engine.get_provider") as mock_prov:
            mock_prov.return_value = type(
                "FakeProvider", (), {"supports_structured_output": False},
            )()
            h = DeepAgentHarness(agent_cfg)
        prompt = h._build_system_prompt()
        # Ambas as âncoras com label espanhol
        assert prompt.count("espanhol") >= 2, "label do idioma deveria aparecer em ambas as âncoras"
