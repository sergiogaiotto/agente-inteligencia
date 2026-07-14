"""Testes de prompted_tools + capability detection (PR canônico de tool strategy).

Cobre:
- llm_capabilities: lookup case-insensitive, fallback conservador (unknown=False)
- prompted_tools: build do system prompt, parser tolerante (válido + malformado),
  strip_tool_calls, format_tool_result_message
- Estratégia escolhida pelo engine via _choose_tool_strategy
"""

from __future__ import annotations

import logging


from app.agents.prompted_tools import (
    build_prompted_tools_system,
    parse_tool_calls,
    strip_tool_calls,
    format_tool_result_message,
)
from app.core.llm_capabilities import (
    CAPABILITIES,
    get_capabilities,
    supports_native_tools,
    supports_prompted_tools,
    get_max_tools,
)


# ═════════════════════════════════════════════════════════════════
# llm_capabilities
# ═════════════════════════════════════════════════════════════════


class TestCapabilities:
    def test_azure_gpt_4o_native(self):
        assert supports_native_tools("azure", "gpt-4o") is True
        assert get_max_tools("azure", "gpt-4o") == 128

    def test_lookup_case_insensitive(self):
        assert supports_native_tools("AZURE", "GPT-4o") is True
        assert supports_native_tools("  azure  ", "gpt-4o") is True

    def test_maritaca_sabia_3_sem_native(self):
        # Sabia-3 legacy, sem function calling nativo
        assert supports_native_tools("maritaca", "sabia-3") is False
        # Mas suporta prompted (modelo segue instrução)
        assert supports_prompted_tools("maritaca", "sabia-3") is True

    def test_ollama_gemma_minusculo(self):
        # Gemma 2B falha em prompted estrito também
        assert supports_native_tools("ollama", "gemma") is False
        assert supports_prompted_tools("ollama", "gemma") is False

    def test_gpt_oss_native(self):
        assert supports_native_tools("gpt-oss-120b", "openai/gpt-oss-120b") is True
        assert supports_native_tools("gpt-oss-20b", "openai/gpt-oss-20b") is True

    def test_modelo_desconhecido_assume_no_native(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert supports_native_tools("foo", "bar") is False
        assert any("modelo desconhecido" in r.message for r in caplog.records)

    def test_modelo_desconhecido_assume_prompted(self):
        # Conservador: tenta prompted mesmo sem capability map
        assert supports_prompted_tools("foo", "bar") is True

    def test_get_capabilities_unknown_returns_none(self):
        assert get_capabilities("foo", "bar") is None

    def test_capabilities_estrutura(self):
        # Todas entries têm os 3 campos esperados
        for key, caps in CAPABILITIES.items():
            assert "native_tools" in caps, f"{key} sem native_tools"
            assert "max_tools" in caps, f"{key} sem max_tools"
            assert "prompted_ok" in caps, f"{key} sem prompted_ok"


# ═════════════════════════════════════════════════════════════════
# prompted_tools — build do system prompt
# ═════════════════════════════════════════════════════════════════


class TestBuildSystem:
    def test_inclui_nome_da_tool(self):
        tools = [{"type": "function", "function": {
            "name": "get_weather",
            "description": "Clima atual",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }}]
        out = build_prompted_tools_system(tools)
        assert "get_weather" in out
        assert "Clima atual" in out

    def test_inclui_schema_dos_params(self):
        tools = [{"type": "function", "function": {
            "name": "x", "description": "y",
            "parameters": {"type": "object", "properties": {"foo": {"type": "string"}}},
        }}]
        out = build_prompted_tools_system(tools)
        assert "foo" in out
        assert "string" in out

    def test_vazio_retorna_string_vazia(self):
        assert build_prompted_tools_system([]) == ""

    def test_multiplas_tools(self):
        tools = [
            {"type": "function", "function": {"name": "a", "description": "A", "parameters": {}}},
            {"type": "function", "function": {"name": "b", "description": "B", "parameters": {}}},
        ]
        out = build_prompted_tools_system(tools)
        assert "### a" in out
        assert "### b" in out

    def test_inclui_formato_xml_tool_call(self):
        # Instrução para o modelo gerar <tool_call>
        out = build_prompted_tools_system([{
            "function": {"name": "x", "description": "y", "parameters": {}}
        }])
        assert "<tool_call>" in out
        assert "</tool_call>" in out


# ═════════════════════════════════════════════════════════════════
# prompted_tools — parse_tool_calls
# ═════════════════════════════════════════════════════════════════


class TestParseToolCalls:
    def test_parse_unico_tool_call(self):
        text = 'Vou consultar.\n<tool_call>{"name": "x", "arguments": {"a": 1}}</tool_call>\nAguarde.'
        out = parse_tool_calls(text)
        assert len(out) == 1
        assert out[0]["name"] == "x"
        assert out[0]["arguments"] == {"a": 1}

    def test_parse_multiplos_tool_calls(self):
        text = """
        <tool_call>{"name": "a", "arguments": {}}</tool_call>
        meio
        <tool_call>{"name": "b", "arguments": {"x": 2}}</tool_call>
        """
        out = parse_tool_calls(text)
        assert len(out) == 2
        assert [c["name"] for c in out] == ["a", "b"]

    def test_malformado_eh_descartado(self):
        text = '<tool_call>{name: invalid}</tool_call><tool_call>{"name":"ok","arguments":{}}</tool_call>'
        out = parse_tool_calls(text)
        # Primeiro descartado (JSON inválido), segundo OK
        assert len(out) == 1
        assert out[0]["name"] == "ok"

    def test_aspas_simples_toleradas(self):
        # Modelos às vezes geram com aspas simples — parser tenta corrigir
        text = "<tool_call>{'name': 'x', 'arguments': {}}</tool_call>"
        out = parse_tool_calls(text)
        assert len(out) == 1
        assert out[0]["name"] == "x"

    def test_args_como_string_string_json(self):
        # Alguns modelos geram arguments como string JSON em vez de dict
        text = '<tool_call>{"name": "x", "arguments": "{\\"foo\\": 1}"}</tool_call>'
        out = parse_tool_calls(text)
        assert len(out) == 1
        assert out[0]["arguments"] == {"foo": 1}

    def test_sem_blocos_retorna_lista_vazia(self):
        assert parse_tool_calls("texto sem tool_calls") == []
        assert parse_tool_calls("") == []
        assert parse_tool_calls(None) == []

    def test_alias_args_em_vez_de_arguments(self):
        # Tolerância a key alternativa
        text = '<tool_call>{"name": "x", "args": {"foo": 1}}</tool_call>'
        out = parse_tool_calls(text)
        assert len(out) == 1
        assert out[0]["arguments"] == {"foo": 1}

    def test_name_vazio_descarta(self):
        text = '<tool_call>{"name": "", "arguments": {}}</tool_call>'
        assert parse_tool_calls(text) == []

    def test_dict_sem_name_descarta(self):
        text = '<tool_call>{"arguments": {}}</tool_call>'
        assert parse_tool_calls(text) == []

    def test_tool_call_multiline(self):
        # Modelo pode quebrar linha dentro do JSON
        text = """<tool_call>{
            "name": "x",
            "arguments": {
                "city": "Sao Paulo"
            }
        }</tool_call>"""
        out = parse_tool_calls(text)
        assert len(out) == 1
        assert out[0]["arguments"]["city"] == "Sao Paulo"


# ═════════════════════════════════════════════════════════════════
# strip_tool_calls
# ═════════════════════════════════════════════════════════════════


class TestStripToolCalls:
    def test_strip_um_bloco(self):
        text = "antes <tool_call>{}</tool_call> depois"
        out = strip_tool_calls(text)
        assert out == "antes  depois"

    def test_strip_multiplos_blocos(self):
        text = "a<tool_call>{}</tool_call>b<tool_call>{}</tool_call>c"
        out = strip_tool_calls(text)
        assert "<tool_call>" not in out
        assert "abc" in out.replace(" ", "")

    def test_strip_texto_sem_blocos_inalterado(self):
        assert strip_tool_calls("texto puro") == "texto puro"

    def test_strip_vazio(self):
        assert strip_tool_calls("") == ""
        assert strip_tool_calls(None) == ""


# ═════════════════════════════════════════════════════════════════
# format_tool_result_message
# ═════════════════════════════════════════════════════════════════


class TestFormatResult:
    def test_format_dict(self):
        out = format_tool_result_message("get_weather", {"temp": 25, "city": "SP"})
        assert "<tool_result" in out
        assert 'tool="get_weather"' in out
        assert "temp" in out
        assert "25" in out

    def test_format_string(self):
        out = format_tool_result_message("x", "resultado em texto")
        assert "resultado em texto" in out

    def test_format_lista(self):
        out = format_tool_result_message("x", [1, 2, 3])
        assert "1" in out
        assert "2" in out
