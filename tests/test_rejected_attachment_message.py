"""UX: quando TODOS os anexos são podados na porta (nenhum agente da cadeia
aceita o tipo), o /chat e /chat/stream devolvem uma mensagem CLARA e acionável
em vez de rodar o agente/pipeline cego (que respondia "sem evidências").

Decidido com o usuário (2026-06-07): manter o opt-in (accepts_images/documents),
só melhorar o feedback. Vale p/ SA direto e p/ pipeline SR/AOBD.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


class TestRejectedAttachmentMessage:
    def test_image_message_is_clear_and_actionable(self):
        from app.routes.workspace import _rejected_attachments_message
        msg = _rejected_attachments_message([{"name": "foto.jpg", "kind": "image"}])
        assert msg.startswith("⚠")
        assert "foto.jpg" in msg
        assert "imagens" in msg
        assert "Editar Agente" in msg

    def test_document_kind_wording(self):
        from app.routes.workspace import _rejected_attachments_message
        msg = _rejected_attachments_message([{"name": "a.pdf", "kind": "document"}])
        assert "documentos" in msg
        assert "a.pdf" in msg

    def test_multiple_kinds_listed(self):
        from app.routes.workspace import _rejected_attachments_message
        msg = _rejected_attachments_message([
            {"name": "f.jpg", "kind": "image"},
            {"name": "d.pdf", "kind": "document"},
        ])
        assert "imagens" in msg and "documentos" in msg
        assert "f.jpg" in msg and "d.pdf" in msg

    def test_empty_falls_back_gracefully(self):
        from app.routes.workspace import _rejected_attachments_message
        msg = _rejected_attachments_message([])
        assert "o anexo" in msg  # nome-fallback, sem quebrar


class TestShortCircuitWiring:
    def test_chat_handlers_short_circuit_on_full_rejection(self):
        src = (_ROOT / "app" / "routes" / "workspace.py").read_text(encoding="utf-8")
        # helper usado nos dois caminhos
        assert src.count("_rejected_attachments_message(") >= 2
        # /chat (não-stream): retorna cedo com final_state Refuse
        assert "\"status\": \"rejected_attachments\"" in src
        # /chat/stream: não cria a task quando tudo rejeitado
        assert "if not _all_rejected:" in src
        assert "_all_rejected = bool(data.attachments) and not attachments" in src
