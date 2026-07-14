"""Bug user (2026-06-01): erro 400 OpenAI ao invocar `_Categorizar Imagem`
com imagem da Av. Paulista (MASP).

Erro: `Invalid 'response_format.json_schema.name': string does not match
pattern '^[a-zA-Z0-9_-]+$'`

Workflow paralelo identificou 3 pontos a corrigir:

1. `engine.py:_build_response_format` e `verifier/runtime.py` montavam o
   `name` direto de `schema.get("title")` (sem sanitizar). O title da
   skill era "Saida da Categorizar Imagem" — espaços violam o regex.

2. `llm_routing.resolve_llm_for_task` caía sempre no `multimodal_fallback`
   hardcoded quando havia imagem, ignorando o Modelo Primário da plataforma
   mesmo quando este já era multimodal. UX confusa.

3. Linter do SKILL.md não avisava quando o `title` continha chars que
   viraram lixo em runtime — problema só aparecia ao executar.

Este arquivo cobre as 3 frentes.
"""
from __future__ import annotations


import pytest

from app.core.text_utils import sanitize_schema_name, schema_name_is_valid


# ─── sanitize_schema_name (helper isolado) ──────────────────────────


class TestSanitizeSchemaName:
    def test_spaces_become_underscore(self):
        assert sanitize_schema_name("Saida da Categorizar Imagem") == "Saida_da_Categorizar_Imagem"

    def test_accented_chars_become_underscore(self):
        out = sanitize_schema_name("Análise Crédito (PF)")
        assert out == "An_lise_Cr_dito_PF"

    def test_already_valid_passes_through(self):
        assert sanitize_schema_name("SkillOutput_v2") == "SkillOutput_v2"

    def test_only_hyphens_and_alphanum_kept(self):
        assert sanitize_schema_name("my-skill-v3") == "my-skill-v3"

    def test_empty_input_returns_fallback(self):
        assert sanitize_schema_name("") == "SkillOutput"
        assert sanitize_schema_name(None) == "SkillOutput"

    def test_only_invalid_chars_returns_fallback(self):
        """String com nada além de chars proibidos vira só underscores
        que são strip()-ados — cai no fallback."""
        assert sanitize_schema_name("   !!!   ", fallback="X") == "X"

    def test_collapses_repeated_underscores(self):
        """Múltiplos espaços/caracteres viram 1 underscore, não vários."""
        assert sanitize_schema_name("a   b    c") == "a_b_c"

    def test_strips_leading_trailing_underscores(self):
        """Resultado não termina/começa em `_` se a entrada começa/termina
        com caractere inválido."""
        assert sanitize_schema_name("  hello  ") == "hello"

    def test_truncates_to_max_len(self):
        assert sanitize_schema_name("x" * 100, max_len=10) == "xxxxxxxxxx"

    def test_custom_fallback(self):
        assert sanitize_schema_name(None, fallback="CorrectedOutput") == "CorrectedOutput"


class TestSchemaNameIsValid:
    def test_valid_strings(self):
        assert schema_name_is_valid("SkillOutput")
        assert schema_name_is_valid("my-skill_v3")
        assert schema_name_is_valid("a")
        assert schema_name_is_valid("123")

    def test_invalid_strings(self):
        assert not schema_name_is_valid("Saida da Categorizar Imagem")
        assert not schema_name_is_valid("Análise")
        assert not schema_name_is_valid("foo.bar")
        assert not schema_name_is_valid("")
        assert not schema_name_is_valid(None)


# ─── engine.py: _build_response_format usa sanitize ─────────────────


class TestEngineUsesSanitizedName:
    def test_engine_imports_helper(self):
        """Confirma que engine.py importa sanitize_schema_name (smoke do source).

        Aceita import único OU multilinha (after PR de strict-mode coercion
        passou a importar coerce_to_openai_strict_schema junto).
        """
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / "app" / "agents" / "engine.py"
        src = path.read_text(encoding="utf-8")
        assert "from app.core.text_utils import" in src
        assert "sanitize_schema_name" in src
        assert "sanitize_schema_name(schema.get(\"title\")" in src


class TestVerifierUsesSanitizedName:
    def test_verifier_imports_helper(self):
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / "app" / "verifier" / "runtime.py"
        src = path.read_text(encoding="utf-8")
        assert "from app.core.text_utils import" in src
        assert "sanitize_schema_name" in src
        assert "sanitize_schema_name(" in src


# ─── llm_routing: preferir primary_model multimodal ─────────────────


class TestResolveLlmForTaskMultimodal:
    @pytest.mark.asyncio
    async def test_primary_model_multimodal_preferred_over_fallback(self, monkeypatch):
        """Se Modelo Primário=gpt-4.1 (multimodal) e task=tool_calling com
        imagem, rota usa primary, não o multimodal_fallback default."""
        from app import llm_routing

        async def fake_load_routing():
            return {
                "tool_calling": "openai_public/gpt-oss-120b",   # text-only
                "multimodal_fallback": "azure/gpt-4o",
            }

        def fake_primary():
            # Operador configurou gpt-4.1 (que está em MULTIMODAL_MODELS)
            return "openai_public/gpt-4.1"

        monkeypatch.setattr(llm_routing, "load_routing", fake_load_routing)
        monkeypatch.setattr(llm_routing, "global_primary_routing", fake_primary)

        provider, model = await llm_routing.resolve_llm_for_task(
            "tool_calling", has_image=True
        )
        # Esperava ir pra azure/gpt-4o (fallback hardcoded); agora vai pro primary
        assert (provider, model) == ("openai_public", "gpt-4.1")

    @pytest.mark.asyncio
    async def test_primary_text_only_still_falls_back(self, monkeypatch):
        """Se primary=gpt-oss-120b (text-only), routing continua usando
        o multimodal_fallback. Sem regressão do behavior antigo."""
        from app import llm_routing

        async def fake_load_routing():
            return {
                "tool_calling": "openai_public/gpt-oss-120b",
                "multimodal_fallback": "azure/gpt-4o",
            }

        def fake_primary():
            return "openai_public/gpt-oss-120b"  # text-only

        monkeypatch.setattr(llm_routing, "load_routing", fake_load_routing)
        monkeypatch.setattr(llm_routing, "global_primary_routing", fake_primary)

        provider, model = await llm_routing.resolve_llm_for_task(
            "tool_calling", has_image=True
        )
        assert (provider, model) == ("azure", "gpt-4o")

    @pytest.mark.asyncio
    async def test_no_image_uses_resolved_normally(self, monkeypatch):
        """Sem imagem, routing usa o que está em routing[task_type], sem
        considerar primary nem fallback."""
        from app import llm_routing

        async def fake_load_routing():
            return {
                "tool_calling": "openai_public/gpt-oss-120b",
                "multimodal_fallback": "azure/gpt-4o",
            }

        def fake_primary():
            return "openai_public/gpt-4.1"

        monkeypatch.setattr(llm_routing, "load_routing", fake_load_routing)
        monkeypatch.setattr(llm_routing, "global_primary_routing", fake_primary)

        provider, model = await llm_routing.resolve_llm_for_task(
            "tool_calling", has_image=False
        )
        assert (provider, model) == ("openai_public", "gpt-oss-120b")

    @pytest.mark.asyncio
    async def test_no_primary_configured_falls_back(self, monkeypatch):
        """global_primary_routing()=None → cai no fallback (sem crash)."""
        from app import llm_routing

        async def fake_load_routing():
            return {
                "tool_calling": "openai_public/gpt-oss-120b",
                "multimodal_fallback": "azure/gpt-4o",
            }

        monkeypatch.setattr(llm_routing, "load_routing", fake_load_routing)
        monkeypatch.setattr(llm_routing, "global_primary_routing", lambda: None)

        provider, model = await llm_routing.resolve_llm_for_task(
            "tool_calling", has_image=True
        )
        assert (provider, model) == ("azure", "gpt-4o")


# ─── linter: warning quando title tem chars inválidos ───────────────


class _StubParsed:
    def __init__(self, output_contract: str = "", **kwargs):
        self.execution_mode = kwargs.get("execution_mode", "")
        self.api_bindings_parsed = kwargs.get("api_bindings_parsed", [])
        self.output_contract = output_contract


class TestLinterOutputContractTitle:
    def test_title_with_spaces_emits_warning(self):
        """O caso real do user: title='Saida da Categorizar Imagem'."""
        from app.skill_parser.linter import lint_skill
        oc = '```json\n{"title": "Saida da Categorizar Imagem", "type": "object"}\n```'
        issues = lint_skill(_StubParsed(output_contract=oc))
        codes = [i["code"] for i in issues]
        assert "output_contract_title_invalid_chars" in codes
        # Mensagem cita o title bruto e o sanitizado
        msg = next(i["message"] for i in issues if i["code"] == "output_contract_title_invalid_chars")
        assert "Saida da Categorizar Imagem" in msg
        assert "Saida_da_Categorizar_Imagem" in msg

    def test_valid_title_no_warning(self):
        """title='SkillOutput_v2' (canônico) não emite warning."""
        from app.skill_parser.linter import lint_skill
        oc = '```json\n{"title": "SkillOutput_v2", "type": "object"}\n```'
        issues = lint_skill(_StubParsed(output_contract=oc))
        codes = [i["code"] for i in issues]
        assert "output_contract_title_invalid_chars" not in codes

    def test_no_title_no_warning(self):
        """Sem `title` no schema → engine usa fallback 'SkillOutput' (válido),
        linter não precisa avisar."""
        from app.skill_parser.linter import lint_skill
        oc = '```json\n{"type": "object", "properties": {}}\n```'
        issues = lint_skill(_StubParsed(output_contract=oc))
        codes = [i["code"] for i in issues]
        assert "output_contract_title_invalid_chars" not in codes

    def test_malformed_json_no_warning(self):
        """JSON quebrado: best-effort silencioso, sem warning específico
        (outros checks do linter já cobrem schema inválido)."""
        from app.skill_parser.linter import lint_skill
        oc = '```json\n{ this is broken }\n```'
        issues = lint_skill(_StubParsed(output_contract=oc))
        codes = [i["code"] for i in issues]
        assert "output_contract_title_invalid_chars" not in codes
