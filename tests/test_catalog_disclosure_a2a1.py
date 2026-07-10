"""Guard-rails do achado A2A-1 (bateria E2E #4 "Atlas + A2A", 2026-07-09).

Sintoma 1 — os modais de catalog_detail.html vivem FORA do template
x-if="!loading && entry": o Alpine avalia as expressões deles no load da
página com `entry` ainda null. Um acesso cru (`entry.kind`) lança
TypeError, e um erro numa expressão reativa aborta a fila de efeitos — na
prática o modal de Divulgação nunca abria. Regra pinada aqui: na região
dos modais, todo acesso a `entry`/`currentExecution` usa optional
chaining (`entry?.kind`) ou um getter null-safe do componente (`capSealed`).

Sintoma 2 — a aba prometia "R6.3 é obrigatória para Root aprovar", mas o
Root aprovava sem disclosure (Arca v1.0.0 e Hélios v1.0.0 na VPS).
Política implementada: warning NÃO-bloqueante — aviso âmbar no modal de
aprovação (detalhe + fila), banner permanente na entry, e
`disclosure_warning` na resposta do decide (coberto por pytest de rota em
test_catalog_api.py). Estes testes pinam os marcadores da política na UI.

Se um teste aqui falhar:
- Novo binding num modal de catalog_detail.html? Use `entry?.prop` (nunca
  `entry.prop`) ou exponha um getter null-safe no componente.
- Mudou a política R6.3? Atualize JUNTOS: textos da aba/help, warnings do
  decide (2 páginas), banner da entry e o backend decide_submission.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGES = REPO_ROOT / "app" / "templates" / "pages"
DETAIL = PAGES / "catalog_detail.html"
QUEUE = PAGES / "catalog_queue.html"
CURL_PARTIAL = REPO_ROOT / "app" / "templates" / "partials" / "curl_auth_modal.html"
HELP_JS = REPO_ROOT / "app" / "static" / "js" / "help-content.js"

# Property access cru (`entry.kind`). NÃO casa `entry?.kind` (o `?` entre o
# nome e o ponto quebra o casamento) nem prosa tipo "Selecione a entry..."
# (o padrão exige um identificador logo após o ponto).
UNGUARDED = re.compile(r"\b(?:entry|currentExecution)\.[A-Za-z_$]")


def _modal_region(html: str) -> str:
    """Trecho do template entre o 1º modal e o fim do block content.

    Os modais são IRMÃOS do template x-if="entry" — é exatamente a região
    onde `entry` pode ser null durante a avaliação Alpine. O block scripts
    (JS, onde `this.entry` é protegido por ifs) fica fora do recorte.
    """
    start = html.index("═══ MODAL")
    end = html.index("{% endblock %}", start)
    return html[start:end]


class TestModalNullGuards:
    def test_detail_modal_region_has_no_unguarded_entry_access(self):
        region = _modal_region(DETAIL.read_text(encoding="utf-8"))
        hits = [
            f"(offset {i}): {line.strip()[:100]}"
            for i, line in enumerate(region.splitlines(), start=1)
            if UNGUARDED.search(line)
        ]
        assert not hits, (
            "Acesso cru a entry./currentExecution. em modal fora do "
            'x-if="entry" — Alpine avalia com entry=null no load (A2A-1). '
            "Use optional chaining (entry?.) ou getter null-safe:\n"
            + "\n".join(hits)
        )

    def test_curl_auth_partial_has_no_unguarded_entry_access(self):
        """O partial do cURL é incluído na região dos modais — mesma regra."""
        content = CURL_PARTIAL.read_text(encoding="utf-8")
        assert not UNGUARDED.search(content), (
            "curl_auth_modal.html com acesso cru a entry./currentExecution."
        )

    def test_run_modal_uses_optional_chaining(self):
        """Pin positivo: o modal Executar (origem do TypeError) usa entry?.kind."""
        region = _modal_region(DETAIL.read_text(encoding="utf-8"))
        assert "entry?.kind" in region

    def test_cap_sealed_getter_is_null_safe(self):
        """O modal de capability decide leitura/edição via getter capSealed —
        precisa existir e coagir para boolean (nunca undefined: `:disabled`
        com undefined vira atributo PRESENTE no Alpine 3 → botão morto)."""
        content = DETAIL.read_text(encoding="utf-8")
        assert "get capSealed()" in content
        assert "!!this.entry" in content


class TestDisclosurePolicyMarkers:
    def test_no_page_promises_blocking_approval(self):
        """A política é warning não-bloqueante — nenhum texto pode prometer
        que o Root é IMPEDIDO de aprovar sem disclosure."""
        detail = DETAIL.read_text(encoding="utf-8")
        assert "obrigatória para Root aprovar" not in detail
        help_content = HELP_JS.read_text(encoding="utf-8")
        assert "R6.3) é obrigatória" not in help_content

    def test_detail_has_policy_testids(self):
        content = DETAIL.read_text(encoding="utf-8")
        for testid in (
            'data-testid="entry-no-disclosure-banner"',
            'data-testid="decide-disclosure-warning"',
            'data-testid="capability-sealed-notice"',
            'data-testid="capability-modal"',
            'data-testid="open-capability-modal"',
            'data-testid="capability-declare-now"',
        ):
            assert testid in content, f"catalog_detail.html sem {testid}"

    def test_queue_has_policy_testids(self):
        content = QUEUE.read_text(encoding="utf-8")
        for testid in (
            'data-testid="queue-no-disclosure"',
            'data-testid="decide-disclosure-warning"',
        ):
            assert testid in content, f"catalog_queue.html sem {testid}"

    def test_capability_modal_opens_in_any_status(self):
        """Header abre o modal para canMutate em QUALQUER status — em entry
        publicada ele explica o draft-only (409) em vez de sumir/quebrar."""
        content = DETAIL.read_text(encoding="utf-8")
        assert re.search(
            r'x-show="canMutate" @click="openCapabilityEdit\(\)"', content
        ), "botão do header deve abrir o modal para canMutate sem exigir draft"

    def test_decide_frontends_surface_backend_warning(self):
        """As duas telas de decisão exibem o disclosure_warning do backend."""
        for page in (DETAIL, QUEUE):
            content = page.read_text(encoding="utf-8")
            assert "disclosure_warning" in content, (
                f"{page.name} não exibe o warning do POST /decide"
            )
