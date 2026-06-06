"""Bug user (2026-06-06): perguntei "como cozinhar" (fora de escopo) e o AR tentou
ENCAMINHAR para o "FAQ Claro" — um agente que existe no catálogo mas NÃO está
cabeado no AI Mesh (sem aresta a partir do roteador). Resultado: 1/3 executados,
os SAs reais (Rentab/Retenção) pulados como skipped_conditional e o roteador
apontando pra um destino que o mesh nunca roteia (dead-end).

Causa-raiz: DRIFT entre prompt e mesh. O system_prompt do roteador prometia um
fallback em prosa ("Encaminhar ao FAQ Claro"), mas:
  - o Composer nunca validava esse alvo contra o AI Mesh;
  - o fallback era texto livre — nada impedia citar um agente inexistente/sem aresta.

Fix (PR1, prompt-only — sem mexer no motor):
  1) FALLBACK ESTRUTURADO: `mission.fallbackMode` com default 'graceful'. O else
     gracioso faz o agente RESPONDER que está fora do escopo e pedir reformulação,
     SEM citar agente algum (mata o drift na origem). 'custom' mantém prosa livre.
  2) TRAVA ANTI-DRIFT sempre no prompt gerado: "só encaminhe/delegue para os
     destinos listados; NUNCA invente nem cite agentes fora da lista".
  3) ALERTA DE DRIFT no dry-run: valida TODOS os alvos (regras E fallback custom)
     contra o mesh. Regra que nomeia agente real SEM aresta → badge "fora do mesh";
     fallback custom que nomeia agente real sem aresta → aviso âmbar. O sinal de
     "nomeia agente" espelha _output_names_target() do engine (fronteira de
     palavra, case/acento-insensível) — o MESMO sinal que o motor usa pra rodar.

Como nos demais smokes do Composer, Alpine/JS só roda no browser → travamos os
contratos ESTRUTURAIS no HTML. A lógica de runtime do _mentionsName foi validada
à parte (paridade com o engine); aqui garantimos que o código está presente/wired.
"""
from __future__ import annotations

from pathlib import Path

import pytest


_TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "app" / "templates" / "pages" / "agent_form.html"
)


@pytest.fixture(scope="module")
def html() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


# ─── Estado novo ─────────────────────────────────────────────────────


class TestFallbackModeState:
    def test_fallback_mode_default_graceful(self, html):
        """O default é o else gracioso (não o texto livre) — seguro por padrão."""
        assert "fallbackMode: 'graceful'," in html

    def test_mesh_downstream_ids_state_defined(self, html):
        """Cache dos alvos já cabeados (alimenta o alerta de drift em edição)."""
        assert "meshDownstreamIds: []," in html

    def test_open_composer_resets_fallback_mode(self, html):
        # openComposer abre limpo, com o modo gracioso por padrão.
        # 2026-06-06: o reset passou a incluir defaultAgent: '' (agente-padrão/else).
        assert (
            "this.mission = { statement: '', rules: [{ when: '', target: '' }], "
            "fallback: '', fallbackMode: 'graceful', defaultAgent: '', goldenRule: true };"
        ) in html

    def test_open_composer_loads_downstream_in_edit(self, html):
        # Em edição, carrega quais alvos já têm aresta (assíncrono, não bloqueia).
        assert "this.meshDownstreamIds = [];" in html
        assert "this._existingDownstreamIds(editId)" in html
        assert "this.meshDownstreamIds = Array.from(set);" in html

    def test_compose_with_ai_infers_fallback_mode(self, html):
        # IA trouxe prosa de fallback → custom; senão mantém o gracioso.
        assert "fallbackMode: d.fallback ? 'custom' : 'graceful'," in html


# ─── composeMissionPrompt: else gracioso + trava anti-drift ──────────


class TestGracefulElseGeneration:
    def test_graceful_flag_computed(self, html):
        assert "const graceful = (this.mission.fallbackMode || 'graceful') === 'graceful';" in html

    def test_branches_on_graceful(self, html):
        assert "if (graceful) {" in html
        # o texto custom do operador entra como COMPLEMENTO do else gracioso
        assert "if (fb) out.push(fb);" in html

    def test_router_graceful_does_not_name_agents(self, html):
        # else do ROTEADOR: responde fora-de-escopo e NÃO encaminha p/ ninguém
        assert "responda que o assunto está fora do seu escopo e peça uma reformulação" in html
        assert "NÃO encaminhe para nenhum agente nesse caso." in html

    def test_aobd_graceful_does_not_name_agents(self, html):
        # else do ORQUESTRADOR: fora do escopo de coordenação, sem delegar
        assert "responda que o pedido está fora do escopo de coordenação e peça uma reformulação" in html
        assert "NÃO delegue para nenhum agente nesse caso." in html


class TestAntiDriftGuardAlwaysEmitted:
    """A trava entra no prompt em AMBOS os modos (gracioso e custom) — é o que
    impede o LLM de citar um agente fora da lista (a raiz do bug FAQ Claro)."""

    def test_router_guard_line(self, html):
        assert (
            "Importante: só encaminhe para os destinos listados em Categorias; "
            "NUNCA invente nem cite agentes que não estejam nessa lista."
        ) in html

    def test_aobd_guard_line(self, html):
        assert (
            "Importante: só delegue aos agentes listados em Critérios de roteamento; "
            "NUNCA invente nem cite agentes que não estejam nessa lista."
        ) in html

    def test_guard_emitted_outside_graceful_branch(self, html):
        """A linha-trava do roteador é empurrada DEPOIS do if/else do fallback
        (sempre roda), não dentro do ramo gracioso."""
        idx_router_graceful = html.find("NÃO encaminhe para nenhum agente nesse caso.")
        idx_router_guard = html.find("só encaminhe para os destinos listados em Categorias")
        assert 0 < idx_router_graceful < idx_router_guard


# ─── Validação de drift: helpers + getters ───────────────────────────


class TestDriftHelpers:
    @pytest.mark.parametrize(
        "method",
        [
            "_isAgentWired(agentId) {",
            "_mentionsName(text, name) {",
            "get missionFallbackDrift() {",
        ],
    )
    def test_helpers_defined(self, html, method):
        assert method in html, f"helper {method!r} ausente no x-data()"

    def test_is_agent_wired_uses_downstream_cache(self, html):
        assert "return (this.meshDownstreamIds || []).includes(agentId);" in html

    def test_mentions_name_normalizes_accents(self, html):
        # espelha _output_names_target() do engine: NFKD + minúsculas
        assert ".normalize('NFKD')" in html

    def test_mentions_name_uses_word_boundary(self, html):
        # fronteira de palavra (não substring) — 'Rentab' não casa 'rentabilidade'
        assert r"new RegExp('\\b' + esc + '\\b').test(hay)" in html

    def test_mentions_name_short_name_guard(self, html):
        # nome < 3 chars não dispara (evita falso positivo)
        assert "if (n.length < 3) return false;" in html


class TestRuleDriftFlag:
    def test_checks_compute_unwired_flag(self, html):
        # alvo é agente real, em edição, SEM aresta → drift
        assert (
            "const unwired = this.isEdit && cls === 'agent' && !!agent "
            "&& !this._isAgentWired(agent.id);"
        ) in html

    def test_unwired_included_in_check_object(self, html):
        assert "inactive, kind, unwired };" in html


class TestFallbackDriftGetter:
    def test_only_in_edit_mode(self, html):
        # agente novo ainda não tem downstream → nada a validar
        assert "if (!this.isEdit) return [];" in html

    def test_only_for_custom_mode(self, html):
        # o gracioso, por construção, não nomeia agente algum
        assert "if ((this.mission.fallbackMode || 'graceful') !== 'custom') return [];" in html

    def test_flags_named_but_unwired_agents(self, html):
        assert "if (this._mentionsName(text, name) && !this._isAgentWired(a.id)) {" in html


# ─── UI: radios de fallback + alerta + badge ─────────────────────────


class TestFallbackModeUI:
    def test_radio_graceful_wired(self, html):
        assert 'value="graceful" x-model="mission.fallbackMode"' in html

    def test_radio_custom_wired(self, html):
        assert 'value="custom" x-model="mission.fallbackMode"' in html

    def test_custom_input_still_bound(self, html):
        # o input de texto livre continua existindo (modo custom)
        assert 'x-model="mission.fallback"' in html

    def test_placeholder_adapts_to_mode(self, html):
        assert ":placeholder=\"mission.fallbackMode === 'custom' ?" in html

    def test_anti_drift_explainer_shown(self, html):
        assert "trava anti-drift" in html


class TestDriftUIIndicators:
    def test_unwired_badge_rendered(self, html):
        assert 'x-show="chk.unwired"' in html
        assert ">fora do mesh</span>" in html

    def test_unwired_badge_uses_amber_not_purple(self, html):
        # âmbar = aviso (mesma paleta do projeto, sem roxo)
        idx = html.find('x-show="chk.unwired"')
        assert idx > 0
        window = html[idx: idx + 400]
        assert "bg-amber-100 text-amber-700" in window

    def test_fallback_drift_alert_rendered(self, html):
        assert 'x-if="missionFallbackDrift.length"' in html
        assert "missionFallbackDrift.map(a => a.name).join(', ')" in html


class TestPaletteConstraint:
    def test_no_purple_tones(self, html):
        # constraint do projeto: roxo proibido (usar vermelho/branco)
        for banned in ("violet", "fuchsia", "purple"):
            assert banned not in html, f"tom proibido {banned!r} no template"
