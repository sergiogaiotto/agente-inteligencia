"""C5 — "Publicar no Catálogo" usa modal in-app (não mais prompt() nativo).

Achado do teste E2E (2026-06-23): o botão "Publicar no Catálogo" do Estúdio de
Pipelines pedia a versão via ``prompt()`` do navegador — fácil de cancelar ou
deixar vazio e **abortar em silêncio**, sem validação de semver visível.

A correção troca o ``prompt()`` por um modal Alpine (espelha o ``runModal`` da
mesma página): campo de versão com validação semver visível, botão desabilitado
enquanto inválido, e erro inline. A validação espelha o backend
(``make_urn`` / ``PipelinePublishRequest``: ``^[0-9]+\\.[0-9]+\\.[0-9]+$``).

Convenção do projeto: não há harness de DOM/Alpine — varredura de template
(mesmo padrão de test_mesh_flow_detail_panel_click.py).
"""
from __future__ import annotations

from pathlib import Path

MESH_FLOW = Path("app/templates/pages/mesh_flow.html")


def _src() -> str:
    return MESH_FLOW.read_text(encoding="utf-8")


def _publish_handler_block(src: str) -> str:
    """Recorta o corpo da função publishToCatalog (sem comentários/markup ao redor).

    Anota na DEFINIÇÃO (``async publishToCatalog()``) — não no ``@click`` do botão
    nem no comentário do modal, que legitimamente mencionam a palavra prompt(). O
    fim é a PRÓXIMA definição de método (``get domainOptions()``), âncora de código
    mais estável que um comentário de seção.
    """
    start = src.index("async publishToCatalog()")
    end = src.index("get domainOptions()", start)
    return src[start:end]


def _open_button_tag(src: str) -> str:
    """Recorta a tag <button> de abrir o modal (em torno do data-testid)."""
    anchor = src.index('data-testid="pipeline-publish-open"')
    open_lt = src.rindex("<button", 0, anchor)
    close_gt = src.index(">", anchor)
    return src[open_lt:close_gt]


def test_publicacao_nao_usa_prompt_nativo_para_versao():
    src = _src()
    block = _publish_handler_block(src)
    # A regressão a travar: nada de prompt() na publicação no Catálogo.
    assert "prompt(" not in block, (
        "publishToCatalog não deve mais usar prompt() nativo — use o modal in-app "
        "(catalogModal) com validação de versão visível"
    )
    # O texto antigo do prompt não pode reaparecer em lugar nenhum da página.
    assert "Versão da publicação no Catálogo (semver" not in src
    # A versão agora vem do estado do modal, não de um prompt().
    assert "this.catalogModal.version" in block


def test_botao_abre_modal_em_vez_de_publicar_direto():
    src = _src()
    tag = _open_button_tag(src)
    # o botão ABRE o modal — e não pode voltar a publicar direto (regressão parcial).
    assert "openCatalogModal()" in tag
    assert "publishToCatalog(" not in tag, (
        "o botão de abrir não deve publicar direto — só abrir o modal (senão volta o "
        "atalho que pulava a validação de versão)"
    )
    # estado legado removido (a busy-flag virou parte do catalogModal)
    assert "publishingCatalog" not in src


def test_modal_tem_campo_versao_validacao_e_submit():
    src = _src()
    # campo de versão + submit + erro inline, todos com data-testid (convenção E2E)
    assert 'data-testid="pipeline-publish-version"' in src
    assert 'data-testid="pipeline-publish-submit"' in src
    assert 'data-testid="pipeline-publish-error"' in src
    # o modal existe e fecha pelos dois caminhos: clicar fora e ESC (bindings exatos,
    # não só o nome da função — que poderia casar com markup não-relacionado).
    assert 'x-show="catalogModal"' in src
    assert '@click.self="closeCatalogModal()"' in src
    assert '@keydown.escape.prevent="closeCatalogModal()"' in src


def test_handler_valida_versao_e_mostra_erro_sem_abortar_em_silencio():
    """O coração do C5: o handler valida a versão e mostra erro INLINE (em vez de
    abortar calado como o prompt() cancelado), e fecha o modal no sucesso sem
    redirect."""
    block = _publish_handler_block(_src())
    # gate de validade antes do POST
    assert "!this.catalogVersionValid" in block
    # erro vai pro estado do modal (renderizado inline), não some num toast efêmero
    assert "this.catalogModal.error" in block
    # sucesso sem entry.id ainda encerra o modal (não fica preso aberto)
    assert "this.closeCatalogModal()" in block


def test_validacao_semver_espelha_o_backend():
    src = _src()
    # getter de validação com a MESMA regra do backend (make_urn / PipelinePublishRequest)
    assert "catalogVersionValid" in src
    assert r"/^[0-9]+\.[0-9]+\.[0-9]+$/" in src
    # submit desabilitado enquanto a versão não for semver válido
    assert "catalogModal.busy || !catalogVersionValid" in src
