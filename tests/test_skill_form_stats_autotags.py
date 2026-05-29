"""Smoke do skill_form.html — stats (chars/tokens/custo) + auto-tags.

User pediu (2026-05-29): adicionar informações de quantidade de tokens
e estimativas de uso/consumo logo abaixo do textarea + auto-preencher
campo Tags.

Testes JS reais exigiriam Node — não temos. Estes são smoke estáticos
que pegam regressão tipo "alguém apagou o getter skillStats" ou
"removeu o botão Sugerir tags". A verificação visual completa depende
de abrir o /skills/new no navegador.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


TEMPLATE = Path("app/templates/pages/skill_form.html")


@pytest.fixture(scope="module")
def html() -> str:
    assert TEMPLATE.exists()
    return TEMPLATE.read_text(encoding="utf-8")


# ───────────────────────────────────────────────────────────────
# Stats card (chars/tokens/custo)
# ───────────────────────────────────────────────────────────────


class TestSkillStatsBlock:
    def test_stats_getter_defined(self, html):
        """skillStats existe como getter reativo no Alpine."""
        assert "get skillStats()" in html

    def test_stats_block_has_chars_card(self, html):
        """Card de chars com label e value referenciando skillStats."""
        assert "skillStats.chars" in html
        assert "Caracteres" in html

    def test_stats_block_has_tokens_estimate(self, html):
        """Tokens estimados via heurística pt-BR."""
        assert "skillStats.tokens" in html
        assert "Tokens" in html

    def test_tokens_use_ptbr_heuristic(self, html):
        """Heurística pt-BR usa divisão por 3.2 (mais densidade que EN ~4)."""
        m = re.search(r"get skillStats\(\)\s*\{([\s\S]*?)\n\s+\},", html)
        assert m
        body = m.group(1)
        assert "/ 3.2" in body or "/3.2" in body, (
            "heurística de tokens não usa 3.2 — ajuste de calibração pt-BR"
        )

    def test_cost_per_call_card_present(self, html):
        assert "skillStats.costPerCall" in html
        assert "Custo/chamada" in html

    def test_cost_per_month_card_present(self, html):
        assert "skillStats.costPerMonth" in html
        assert "Custo / mês" in html

    def test_cost_uses_input_price_reference(self, html):
        """Preço de input definido como constante na função do getter."""
        m = re.search(r"get skillStats\(\)\s*\{([\s\S]*?)\n\s+\},", html)
        assert m
        body = m.group(1)
        assert "PRICE_PER_1M_INPUT" in body
        assert "0.15" in body, "preço de referência não usa GPT-4o-mini (US$0.15)"

    def test_token_count_warns_at_thresholds(self, html):
        """Tokens com cor rose >8000 e amber >4000 — sinaliza skill inchada."""
        assert "> 8000" in html or ">= 8000" in html or "> 8_000" in html
        assert "> 4000" in html or ">= 4000" in html

    def test_projection_calls_default_1000(self, html):
        """Volume default 1k/mês — operador conserva controle pra alternar."""
        m = re.search(r"skillStatsProjectionCalls\s*:\s*(\d+)", html)
        assert m
        assert int(m.group(1)) == 1000

    def test_volume_toggle_button_present(self, html):
        """Botão 'alternar volume' cicla entre 1k/10k/100k."""
        assert "alternar volume" in html
        # Deve mexer no state, não no getter
        assert "skillStatsProjectionCalls =" in html
        # Cobertura dos 3 volumes
        assert "1000" in html and "10000" in html and "100000" in html

    def test_disclaimer_about_limitations_present(self, html):
        """Footnote explicando que estimativa NÃO inclui output, RAG,
        tool results — evita interpretação errada do número."""
        low = html.lower()
        # Pelo menos 2 limitações citadas explicitamente
        mentions = sum(1 for w in ("output", "evidências", "tool results", "input do usuário") if w in low)
        assert mentions >= 2, "disclaimer da estimativa de custo é raso demais"


# ───────────────────────────────────────────────────────────────
# Auto-fill tags
# ───────────────────────────────────────────────────────────────


class TestAutoFillTags:
    def test_autofill_method_defined(self, html):
        assert "autoFillTags()" in html

    def test_button_to_trigger_autofill_present(self, html):
        assert "Sugerir tags" in html
        # Botão chama @click="autoFillTags()"
        assert '@click="autoFillTags()"' in html

    def test_button_disabled_when_no_content(self, html):
        """UX: botão fica disabled quando raw_content vazio (não há de onde
        extrair tags)."""
        # Procura disabled binding logo após autoFillTags
        idx = html.find("autoFillTags()")
        assert idx > 0
        chunk = html[idx:idx+500]
        assert ":disabled" in chunk

    def test_extracts_from_relevant_sections(self, html):
        """Heurística colhe de Purpose/Activation Criteria/Workflow/Tool Bindings."""
        m = re.search(r"autoFillTags\(\)\s*\{([\s\S]*?)\n\s+\},", html)
        assert m
        body = m.group(1)
        for sec in ("Purpose", "Activation Criteria", "Workflow", "Tool Bindings"):
            assert sec in body, f"seção {sec!r} não considerada pela heurística"

    def test_stopwords_filter_present(self, html):
        """Stopwords pt-BR + vocabulário da plataforma evita tags lixo."""
        assert "_TAG_STOPWORDS" in html
        # Subset essencial
        m = re.search(r"_TAG_STOPWORDS:\s*new Set\(\[([\s\S]*?)\]\)", html)
        assert m
        body = m.group(1)
        for sw in ("'de'", "'que'", "'para'"):
            assert sw in body
        # Vocabulário da plataforma também
        assert "'skill'" in body
        assert "'tool'" in body

    def test_strips_markdown_before_tokenize(self, html):
        """Não deve emitir asteriscos ou backticks como tag — markdown
        precisa ser limpado antes do split."""
        m = re.search(r"autoFillTags\(\)\s*\{([\s\S]*?)\n\s+\},", html)
        assert m
        body = m.group(1)
        # Limpeza de code fences + inline code + bold/italic
        assert "```" in body  # remove fenced
        assert "`" in body    # remove inline
        # Mais tokens vão pelo split — markdown removido antes

    def test_caps_at_seven_tags(self, html):
        """Top 7 — evita pollution de campo com 20+ tags."""
        m = re.search(r"autoFillTags\(\)\s*\{([\s\S]*?)\n\s+\},", html)
        assert m
        assert ">= 7" in m.group(1)

    def test_dedups_prefix_overlap(self, html):
        """singleton/singletons — não emite ambos."""
        m = re.search(r"autoFillTags\(\)\s*\{([\s\S]*?)\n\s+\},", html)
        assert m
        body = m.group(1)
        assert "startsWith" in body, "dedup de prefixo ausente"

    def test_writes_to_form_tags_as_json_array(self, html):
        """form.tags é input text com JSON — autoFill precisa serializar."""
        m = re.search(r"autoFillTags\(\)\s*\{([\s\S]*?)\n\s+\},", html)
        assert m
        body = m.group(1)
        assert "this.form.tags = JSON.stringify" in body

    def test_dica_message_visible_when_tags_empty(self, html):
        """Quando tags vazia, mensagem orienta a clicar em Sugerir tags."""
        assert "Sugerir tags" in html
        # Mensagem condicional
        assert 'x-show="!form.tags' in html or "x-show=\"!form.tags" in html


# ───────────────────────────────────────────────────────────────
# Posição relativa: stats embaixo do textarea, tags ainda depois
# ───────────────────────────────────────────────────────────────


class TestPositioning:
    def test_stats_block_appears_after_textarea(self, html):
        """Stats deve ficar IMEDIATAMENTE abaixo do textarea — não no
        cabeçalho nem no final do form."""
        idx_textarea = html.find('x-model="form.raw_content"')
        idx_stats = html.find("skillStats.chars")
        idx_tags = html.find('x-model="form.tags"')
        assert idx_textarea > 0 and idx_stats > 0 and idx_tags > 0
        assert idx_textarea < idx_stats < idx_tags, (
            "Stats não está entre textarea e tags — ordem visual quebrada"
        )

    def test_sugerir_tags_button_appears_near_tags_label(self, html):
        """Botão 'Sugerir tags' fica no header do campo Tags, não solto.
        Margem de 1500 chars pra acomodar tooltip + svg + classes Tailwind."""
        idx_tags_label = html.find(">Tags</label>")
        idx_button = html.find("Sugerir tags")
        assert idx_tags_label > 0 and idx_button > 0
        assert abs(idx_tags_label - idx_button) < 1500
