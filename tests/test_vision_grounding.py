"""Grounded-by-default × visão: um anexo de IMAGEM enviado a um modelo
multimodal CONTA como evidência (a imagem é a fonte) — então o SA de visão não
é recusado por "falta de evidência".

Regressão (2026-06-07): o #310 parou de injetar o texto "ImageSize" no
attachment_context; com isso `_has_attach_grounding = bool(attachment_context)`
ficou False p/ imagem e o `_grounding_guard` (#301) recusava todo SA de imagem
(evidence_insufficient). Fix: `_image_is_grounding` conta a imagem qdo o modelo
é multimodal.
"""
from __future__ import annotations


_IMG = {"name": "x.png", "type": "image/png"}
_DOC = {"name": "a.pdf", "type": "application/pdf"}


class TestImageIsGrounding:
    def test_image_plus_multimodal_is_grounding(self):
        from app.agents.engine import _image_is_grounding
        assert _image_is_grounding([_IMG], "azure", "gpt-4o") is True

    def test_image_plus_text_only_is_not_grounding(self):
        """text-only descarta a imagem → não é grounding (recusa correta)."""
        from app.agents.engine import _image_is_grounding
        assert _image_is_grounding([_IMG], "gpt-oss-120b", "openai/gpt-oss-120b") is False

    def test_document_is_not_image_grounding(self):
        from app.agents.engine import _image_is_grounding
        assert _image_is_grounding([_DOC], "azure", "gpt-4o") is False

    def test_empty_is_not_grounding(self):
        from app.agents.engine import _image_is_grounding
        assert _image_is_grounding([], "azure", "gpt-4o") is False
        assert _image_is_grounding(None, "azure", "gpt-4o") is False


class TestGroundingGuardWithImage:
    """Integração com o guard: imagem+multimodal NÃO recusa; imagem+text-only recusa."""

    def _refuse(self, attachments, provider, model):
        from app.agents.engine import _grounding_guard, _image_is_grounding
        has_img = _image_is_grounding(attachments, provider, model)
        refuse, _reason = _grounding_guard(
            strict=True,
            allow_general_knowledge=False,
            has_evidences=False,
            has_attachments=has_img,
            has_pipeline_context=False,
            has_tool_output=False,
        )
        return refuse

    def test_image_to_vision_model_is_not_refused(self):
        assert self._refuse([_IMG], "azure", "gpt-4o") is False

    def test_image_to_text_only_model_is_refused(self):
        assert self._refuse([_IMG], "gpt-oss-120b", "openai/gpt-oss-120b") is True


class TestWiringSourceSmoke:
    def test_engine_counts_image_grounding(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "agents" / "engine.py").read_text(encoding="utf-8")
        assert "_image_is_grounding(" in src
        assert "_has_attach_grounding = bool(attachment_context) or _has_image_grounding" in src
