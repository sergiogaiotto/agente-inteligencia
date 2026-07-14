"""Card "Anexos" no painel Rastreabilidade do Workspace (2026-06-01).

User pediu: _"quando o usuário interagir com qualquer tipo de imagem ou
arquivo, em Rastreabilidade > em Métricas deve ter a informação objetivo
usado... seja criativo"_.

## Fix

**Backend (`app/agents/engine.py`)** enriquece `attachment_meta` com
campos novos antes de persistir em `trace.attachments`:

- `category`: 'image' | 'document'
- `purpose`: string humanizada ("Análise visual", "Texto extraído", etc.)
- `routed_to`: token ('vision' | 'markdown_extracted' | 'inline_text' |
  'unprocessed') consumido pelo UI pra escolher cor do badge
- `extracted_chars`: len(content) — mostra quanto texto chegou ao LLM

Heurística do `purpose`/`routed_to`:
- type `image/*` → vision (modelo multimodal)
- texto presente + type `text/*` → inline (UTF-8 puro)
- texto presente + outro type → markdown_extracted (markitdown converteu)
- sem texto e não-image → unprocessed (fallback do upload)

**Frontend (`app/templates/pages/workspace.html`)** adiciona 7º card
"Anexos" no grid Métricas (col-span-2, indigo theme) + drilldown
expansível com ícone por tipo, tamanho legível, badge colorido por
purpose, chars extraídos quando aplicável.

Helpers: `formatBytes`, `attachmentIcon`, `attachmentBadgeClass`,
`attachmentBreakdown`.
"""
from __future__ import annotations

from pathlib import Path



def _workspace_html() -> str:
    p = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "workspace.html"
    return p.read_text(encoding="utf-8")


# ─── Backend: attachment_meta enriquecido ────────────────────────────


class TestAttachmentMetaEnrichment:
    """Garante que execute_interaction enriquece os meta de anexos
    com purpose/routed_to/category/extracted_chars antes de persistir
    em result.trace.attachments."""

    def test_engine_source_has_enrichment_logic(self):
        """Smoke do source: as 4 categorias de routed_to estão presentes
        no engine.py (vision/markdown_extracted/inline_text/unprocessed).
        Guard contra regressão de alguém remover um caminho."""
        src = (Path(__file__).resolve().parent.parent / "app" / "agents" / "engine.py").read_text(encoding="utf-8")
        for token in [
            '"vision"',
            '"markdown_extracted"',
            '"inline_text"',
            '"unprocessed"',
        ]:
            assert token in src, f"routed_to esperado não está em engine.py: {token}"

    def test_engine_source_has_purpose_strings(self):
        """Os textos humanos do `purpose` estão no source — UI lê esses
        valores diretamente, não traduz."""
        src = (Path(__file__).resolve().parent.parent / "app" / "agents" / "engine.py").read_text(encoding="utf-8")
        for label in [
            "Análise visual",
            "Texto inline",
            "Texto extraído (markitdown)",
            "Não processado",
        ]:
            assert label in src, f"purpose esperado não está em engine.py: {label}"

    def test_execute_interaction_passes_enriched_meta_to_build_result(self):
        """execute_interaction passa `attachment_meta` (enriquecido) como
        parâmetro `attachments` de _build_result — que então persiste no
        trace via `attachments or []`. Regressão guard contra alguém
        passar a versão raw."""
        src = (Path(__file__).resolve().parent.parent / "app" / "agents" / "engine.py").read_text(encoding="utf-8")
        # Pelo menos 2 chamadas de _build_result devem passar attachment_meta
        # (success path + um exception path)
        assert src.count("attachments=attachment_meta") >= 2
        # Dentro de _build_result, o campo trace.attachments lê o param local
        assert '"attachments": attachments or []' in src


# ─── Frontend: card "Anexos" + drilldown ─────────────────────────────


class TestAttachmentsCardUI:
    def test_card_present_in_metrics_grid(self):
        """7º card "Anexos" foi adicionado ao grid Métricas com col-span-2
        (linha final inteira), seguindo padrão visual dos outros cards
        clicáveis (evidências/MCP/API)."""
        src = _workspace_html()
        # Header do card
        assert '>Anexos<' in src
        # Card lê do trace.attachments
        assert "lastTrace.trace?.attachments?.length" in src
        # Card é clicável quando há anexos (metricExpanded === 'attachments')
        assert "metricExpanded === 'attachments'" in src

    def test_card_shows_breakdown_subtitle(self):
        """Card mostra contagem grande + breakdown sutil ('2 · 1 imagem
        · 1 doc') no subtítulo via helper attachmentBreakdown()."""
        src = _workspace_html()
        assert "attachmentBreakdown(lastTrace.trace?.attachments)" in src

    def test_drilldown_lists_each_attachment(self):
        """Drilldown expande lista detalhada — icon, nome, tamanho,
        badge de purpose e chars extraídos por anexo."""
        src = _workspace_html()
        # Iteração pela lista
        assert 'x-for="(att, i) in lastTrace.trace?.attachments || []"' in src
        # Renderiza helpers de display
        assert "attachmentIcon(att)" in src
        assert "formatBytes(att.size||0)" in src
        assert "attachmentBadgeClass(att.routed_to)" in src
        # Purpose vem direto do backend (já humanizado)
        assert 'x-text="att.purpose||\'—\'"' in src
        # Chars extraídos só aparecem quando > 0
        assert "x-show=\"(att.extracted_chars||0) > 0\"" in src


class TestAttachmentHelpersJS:
    def test_format_bytes_helper(self):
        """formatBytes converte bytes pra string humana (1.2 MB, 345 KB).
        Smoke do source pra garantir todos os tiers (B/KB/MB/GB)."""
        src = _workspace_html()
        assert "formatBytes(n)" in src
        # Tier B (< 1KB)
        assert "n + ' B'" in src
        # Tier KB
        assert "' KB'" in src
        # Tier MB
        assert "' MB'" in src
        # Tier GB
        assert "' GB'" in src

    def test_attachment_icon_helper(self):
        """attachmentIcon mapeia mime → emoji por tipo. Cobre os tipos
        comuns; default 📎 evita undefined no UI."""
        src = _workspace_html()
        assert "attachmentIcon(att)" in src
        # Casos cobertos
        for icon in ["🖼️", "📕", "📊", "📘", "🎵", "🎬", "📝", "📎"]:
            assert icon in src, f"icon faltando: {icon}"

    def test_attachment_badge_class_helper(self):
        """attachmentBadgeClass mapeia routed_to → tailwind class.
        Os 4 routed_to canônicos do backend devem estar mapeados pra
        cores distintas. Paleta evita violet/fuchsia/purple (proibido
        pelo guardrail test_platform_red_palette)."""
        src = _workspace_html()
        assert "attachmentBadgeClass(routed)" in src
        assert "'vision'" in src and "indigo-100" in src
        assert "'markdown_extracted'" in src and "emerald-100" in src
        assert "'inline_text'" in src and "sky-100" in src
        assert "'unprocessed'" in src and "amber-100" in src

    def test_attachment_breakdown_helper(self):
        """attachmentBreakdown gera subtítulo com tipos presentes
        ('1 imagem · 2 docs'). Não inclui categorias com zero."""
        src = _workspace_html()
        assert "attachmentBreakdown(atts)" in src
        # Singularização inteligente
        assert "imagem" in src and "imagens" in src
        assert " doc" in src and " docs" in src
