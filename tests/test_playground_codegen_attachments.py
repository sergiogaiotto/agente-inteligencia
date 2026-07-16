"""Playground — o código gerado agora ensina a enviar ANEXO pela API.

QA E2E 2026-07-16: dúvida do usuário "como o usuário informa o arquivo que quer
enviar na API?". Achado: a UI anexava (via /workspace/upload) e a EXECUÇÃO
mandava o arquivo, mas o CÓDIGO GERADO (curl/Python/…) só tinha message/args/
verbosity — o `bodyObj` que alimenta o codegen ignorava os anexos. Um dev que
copiava o curl não descobria a API de anexo nem o formato.

Fix: com arquivo(s) anexado(s), o `bodyObj` passa a incluir um bloco
`attachments` (contrato real: {filename, content_type, content_base64}), com o
base64 como PLACEHOLDER curto (o snippet não cola o blob de MBs) + um aviso
âmbar explicando como produzir o base64. Vale para TODA linguagem, porque todas
serializam o mesmo bodyObj.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def html() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "templates"
            / "pages" / "mesh_playground.html").read_text(encoding="utf-8")


class TestBodyObjInclui:
    def test_attachments_entram_no_bodyobj(self, html):
        i = html.index("get bodyObj() {")
        fn = html[i: i + 1300]
        assert "this.attachments.length" in fn
        assert "base.attachments = this.attachments.map" in fn

    def test_contrato_base64_correto(self, html):
        i = html.index("get bodyObj() {")
        fn = html[i: i + 1300]
        for campo in ("filename", "content_type", "content_base64"):
            assert campo in fn, f"contrato de anexo sem {campo}"

    def test_placeholder_nao_cola_blob(self, html):
        """O snippet não pode carregar um base64 de MBs — placeholder curto."""
        i = html.index("get bodyObj() {")
        fn = html[i: i + 1300]
        assert "<BASE64_DO_ARQUIVO>" in fn

    def test_bodyobj_e_so_codegen_execucao_e_separada(self, html):
        """A execução real monta `_body.attachments = this.attachments` (o
        descriptor de verdade); bodyObj com placeholder NÃO pode vazar pro
        envio, senão o Playground mandaria '<BASE64…>' em vez do arquivo."""
        assert "_body.attachments = this.attachments" in html
        # bodyObj só é consumido pelo _reqSpec/_pyDict (codegen)
        assert "body: JSON.stringify(this.bodyObj)" in html


class TestAvisoDeAnexo:
    def test_aviso_so_na_chamada_unica_com_anexo(self, html):
        i = html.index('data-testid="pg-code-attach-note"')
        bloco = html[i - 200: i + 120]
        assert "recipe==='single' && attachments.length" in bloco
        assert "x-cloak" in bloco   # não pisca no load

    def test_aviso_ensina_a_produzir_o_base64(self, html):
        i = html.index("codeAttachmentNote() {")
        fn = html[i: i + 700]
        assert "base64.b64encode" in fn          # dica Python real
        assert "5 arquivos" in fn or "5 arquivos × 10 MB" in fn
        assert "<BASE64_DO_ARQUIVO>" in fn       # aponta pro placeholder do snippet
