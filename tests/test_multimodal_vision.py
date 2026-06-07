"""Visão real: anexo de IMAGEM vai ao LLM como conteúdo multimodal (image_url)
quando o modelo resolvido é multimodal — e é DESCARTADO (com log) em modelo
text-only.

Regressão do bug "SA Imagem devolve {objects: []}": antes a imagem virava só o
texto "ImageSize: LxA" (markitdown) e nunca chegava ao LLM como pixels, então
até o gpt-4o (multimodal) respondia vazio. Decidido/diagnosticado em 2026-06-07.
"""
from __future__ import annotations

import base64
from pathlib import Path

# PNG 1x1 válido (transparente) — usado para montar anexos de imagem nos testes.
_PNG_1x1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_ROOT = Path(__file__).resolve().parent.parent


class TestAttachmentImageDataUrl:
    def test_image_with_base64_returns_data_url(self):
        from app.agents.engine import _attachment_image_data_url
        att = {"name": "x.png", "type": "image/png", "content_base64": _PNG_1x1_B64}
        url = _attachment_image_data_url(att)
        assert url == f"data:image/png;base64,{_PNG_1x1_B64}"

    def test_image_with_abs_path_reads_bytes(self, tmp_path):
        from app.agents.engine import _attachment_image_data_url
        p = tmp_path / "foto.jpg"
        p.write_bytes(base64.b64decode(_PNG_1x1_B64))
        att = {"name": "foto.jpg", "type": "image/jpeg", "abs_path": str(p)}
        url = _attachment_image_data_url(att)
        assert url is not None
        assert url.startswith("data:image/jpeg;base64,")
        # o base64 embutido decodifica de volta aos bytes do arquivo
        embedded = url.split(",", 1)[1]
        assert base64.b64decode(embedded) == base64.b64decode(_PNG_1x1_B64)

    def test_non_image_returns_none(self):
        from app.agents.engine import _attachment_image_data_url
        att = {"name": "doc.pdf", "type": "application/pdf", "content_base64": _PNG_1x1_B64}
        assert _attachment_image_data_url(att) is None

    def test_image_without_bytes_returns_none(self):
        from app.agents.engine import _attachment_image_data_url
        att = {"name": "x.png", "type": "image/png"}
        assert _attachment_image_data_url(att) is None


class TestBuildUserMessageContent:
    _IMG = {"name": "x.png", "type": "image/png", "content_base64": _PNG_1x1_B64}
    _DOC = {"name": "a.pdf", "type": "application/pdf", "content": "texto extraido"}

    def test_no_attachments_returns_plain_text(self):
        from app.agents.engine import _build_user_message_content
        assert _build_user_message_content("oi", None, "azure", "gpt-4o") == "oi"
        assert _build_user_message_content("oi", [], "azure", "gpt-4o") == "oi"

    def test_image_plus_multimodal_model_builds_parts(self):
        from app.agents.engine import _build_user_message_content
        content = _build_user_message_content("o que temos aqui", [self._IMG], "azure", "gpt-4o")
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "o que temos aqui"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_image_plus_text_only_model_drops_image(self):
        """gpt-oss-120b é text-only → imagem descartada, volta string (sem 400)."""
        from app.agents.engine import _build_user_message_content
        content = _build_user_message_content(
            "o que temos aqui", [self._IMG], "gpt-oss-120b", "openai/gpt-oss-120b"
        )
        assert content == "o que temos aqui"

    def test_document_attachment_keeps_plain_text(self):
        from app.agents.engine import _build_user_message_content
        content = _build_user_message_content("resuma", [self._DOC], "azure", "gpt-4o")
        assert content == "resuma"

    def test_image_multimodal_but_no_bytes_falls_back_to_text(self):
        from app.agents.engine import _build_user_message_content
        att = {"name": "x.png", "type": "image/png"}  # sem bytes
        content = _build_user_message_content("oi", [att], "azure", "gpt-4o")
        assert content == "oi"


class TestWiringSourceSmoke:
    def test_workspace_passes_abs_path(self):
        src = (_ROOT / "app" / "routes" / "workspace.py").read_text(encoding="utf-8")
        assert "abs_path" in src
        assert "UPLOAD_DIR / Path(att.get(\"path\"" in src

    def test_engine_uses_multimodal_builder(self):
        src = (_ROOT / "app" / "agents" / "engine.py").read_text(encoding="utf-8")
        assert "_build_user_message_content(" in src
        # imagem não é injetada como texto
        assert '_category != "image"' in src
