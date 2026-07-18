"""Regra do produto: NENHUMA resposta de API pode conter emoji ou símbolos
decorativos "similares". Testa o sanitizador determinístico e os pontos onde é
aplicado (prefixos da FSM sem emoji, instrução no prompt)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.text_sanitize import scrub_diagnostics, strip_emoji

_DECOR = "🎉✅❌🔺⚠▶◀●■◆→←↑↓✓✔ℹ🔒🔓🟢🟡🔴💬🧪🎼🖱️🚀🙂🤝⭐➤"


def _no_emoji(s: str) -> bool:
    return strip_emoji(s) == s


class TestStripEmoji:
    def test_remove_todos_os_decorativos(self):
        out = strip_emoji("x" + _DECOR + "y")
        assert out == "xy"
        for ch in _DECOR:
            assert ch not in strip_emoji(f"a {ch} b")

    def test_prefixos_fsm_limpos_exatamente(self):
        assert strip_emoji("🔺 Escalação") == "Escalação"
        assert strip_emoji("⚠ Recusa controlada: motivo") == "Recusa controlada: motivo"
        assert strip_emoji("Feito 🎉✅") == "Feito"
        assert strip_emoji("foo 🎉 bar") == "foo bar"

    def test_preserva_acentos_pontuacao_bullets(self):
        s = "Ação: não é possível — reveja.\n• um\n• dois"
        assert strip_emoji(s) == s          # nada a remover

    def test_preserva_indentacao_de_codigo(self):
        out = strip_emoji("resultado 🎉\n    def foo():\n        return 1")
        assert "🎉" not in out
        assert "\n    def foo():\n        return 1" in out   # indentação intacta

    def test_idempotente(self):
        once = strip_emoji("oi 😀 tudo bem ✅ →")
        assert strip_emoji(once) == once

    def test_none_e_vazio_passam(self):
        assert strip_emoji(None) is None
        assert strip_emoji("") == ""
        assert strip_emoji("sem nada aqui") == "sem nada aqui"


class TestScrubDiagnostics:
    def test_limpa_text_mantem_level(self):
        d = [{"level": "warning", "text": "⚠ atenção"}, {"level": "info", "text": "ok ✅"}]
        assert scrub_diagnostics(d) == [
            {"level": "warning", "text": "atenção"},
            {"level": "info", "text": "ok"},
        ]

    def test_entrada_malformada_passa_intacta(self):
        assert scrub_diagnostics("x") == "x"
        assert scrub_diagnostics([{"sem_text": 1}]) == [{"sem_text": 1}]


class TestFSMPrefixosSemEmoji:
    @pytest.mark.asyncio
    async def test_refuse_sem_emoji(self):
        from app.agents.state_machine import InteractionContext, InteractionStateMachine, State
        ctx = InteractionContext(current_state=State.REFUSE)   # interaction_id="" → sem DB
        sm = InteractionStateMachine(ctx)
        await sm.run_refuse("dado de terceiro")
        assert ctx.final_output.startswith("Recusa controlada:")
        assert _no_emoji(ctx.final_output)

    @pytest.mark.asyncio
    async def test_escalate_sem_emoji(self):
        from app.agents.state_machine import InteractionContext, InteractionStateMachine, State
        ctx = InteractionContext(current_state=State.ESCALATE)
        sm = InteractionStateMachine(ctx)
        await sm.run_escalate("falha regional")
        assert ctx.final_output.startswith("Escalação:")
        assert _no_emoji(ctx.final_output)


class TestAplicacaoNoEngine:
    """Guardas de fonte: os pontos de saída da API passam pelo sanitizador."""

    def _engine(self) -> str:
        return (Path(__file__).resolve().parent.parent / "app" / "agents" / "engine.py").read_text(encoding="utf-8")

    def test_output_do_envelope_sanitizado(self):
        src = self._engine()
        assert "output = strip_emoji(output)" in src          # invoke de agente
        assert "strip_emoji(final_output)" in src             # envelope do pipeline
        assert "strip_emoji(output_text)" in src              # caminho declarativo
        assert "scrub_diagnostics(diagnostics)" in src        # diagnósticos (agente)
        assert "scrub_diagnostics(all_diagnostics)" in src    # diagnósticos (pipeline)

    def test_prompt_proibe_emoji(self):
        from app.agents.engine import _build_response_language_directive
        assert "NUNCA use emoji" in _build_response_language_directive("pt-BR")

    def test_explain_sanitiza_answer(self):
        src = (Path(__file__).resolve().parent.parent / "app" / "routes" / "agents.py").read_text(encoding="utf-8")
        assert 'strip_emoji((content or "").strip())' in src
