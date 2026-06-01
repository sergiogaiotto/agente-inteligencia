"""Bug user 2026-06-01: agente _Análise de Texto respondeu que não conseguia
ler PDF/PPTX porque o arquivo estava em "formato binário".

User perguntou: "tem docling implementado, não deveria conseguir?"
Diagnóstico: docling NÃO está no projeto, mas markitdown SIM (já em
requirements.txt + app/evidence/converters.py:convert_bytes). O bug é que
os 2 handlers de attachment do chat NUNCA chamavam o conversor:

1. `app/routes/workspace.py` POST /upload (Vue/Alpine flow) — caía em
   `except UnicodeDecodeError` e gravava o placeholder
   "[Arquivo binário: ...]" como text_content. LLM recebia esse texto
   literalmente como input do attachment.

2. `app/routes/agents.py` _decode_attachments (API /invoke base64 flow)
   — caía em `except UnicodeDecodeError: pass` e o `content` ficava "",
   levando o engine a descartar o attachment silenciosamente.

Fix: tentar markitdown via convert_bytes antes de cair no placeholder /
descarte. Padrão de hardening da PR #250 aplicado: logger.warning com
extras quando markitdown falha (em vez de swallow).
"""
from __future__ import annotations

import base64
import io
import logging
from unittest.mock import patch

import pytest
from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient


# ─── workspace.py /upload ───────────────────────────────────────────


@pytest.fixture
def upload_client():
    from app.routes.workspace import router as ws_router
    from app.core.auth import require_user

    app = FastAPI()
    app.include_router(ws_router)

    async def fake_user():
        return {"id": "u1", "email": "test@local"}

    app.dependency_overrides[require_user] = fake_user
    return TestClient(app)


def _upload(client, content: bytes, filename: str, content_type: str):
    files = {"file": (filename, io.BytesIO(content), content_type)}
    return client.post("/api/v1/workspace/upload", files=files)


class TestWorkspaceUploadExtractsBinaryText:
    def test_utf8_text_passes_through_unchanged(self, upload_client):
        """Happy path original — texto UTF-8 não toca em markitdown."""
        r = _upload(upload_client, "olá mundo".encode("utf-8"), "x.txt", "text/plain")
        assert r.status_code == 200
        assert r.json()["text_content"] == "olá mundo"

    def test_binary_pdf_uses_markitdown(self, upload_client):
        """REGRESSÃO do bug: PDF binário agora vira texto via markitdown."""
        fake_pdf = b"\x25PDF-1.4 \xff\xfe\x80\x81 binary garbage"
        with patch("app.evidence.converters.convert_bytes") as mock_convert:
            mock_convert.return_value = "# Relatório\n\nConteúdo extraído do PDF."
            r = _upload(upload_client, fake_pdf, "relatorio.pdf", "application/pdf")

        assert r.status_code == 200
        body = r.json()
        assert "Conteúdo extraído do PDF" in body["text_content"]
        # markitdown foi de fato chamada com os args certos
        mock_convert.assert_called_once()
        args = mock_convert.call_args
        assert args[0][0] == fake_pdf
        assert args[0][1] == "relatorio.pdf"
        assert args[0][2] == "application/pdf"

    def test_binary_pptx_uses_markitdown(self, upload_client):
        """PPTX também deve passar pelo conversor."""
        fake_pptx = b"PK\x03\x04\xff\xfe\x80\x81 fake pptx bytes"
        with patch("app.evidence.converters.convert_bytes") as mock_convert:
            mock_convert.return_value = "Slide 1: Introdução\n\nSlide 2: Resultados"
            r = _upload(
                upload_client, fake_pptx, "apresentacao.pptx",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )

        assert r.status_code == 200
        assert "Slide 1: Introdução" in r.json()["text_content"]

    def test_markitdown_failure_falls_back_to_descriptive_placeholder(self, upload_client, caplog):
        """Quando markitdown falha (lib não instalada, formato exótico, etc),
        cai em placeholder descritivo (NÃO mais "[Arquivo binário]" simples)
        E loga warning para troubleshooting."""
        fake_binary = b"\x00\x01\x02\xff\xfe\x80\x81 weird bytes"
        with patch("app.evidence.converters.convert_bytes") as mock_convert:
            mock_convert.side_effect = RuntimeError("markitdown crashou")
            with caplog.at_level(logging.WARNING, logger="app.routes.workspace"):
                r = _upload(upload_client, fake_binary, "weird.xyz", "application/octet-stream")

        assert r.status_code == 200
        body = r.json()
        # Placeholder descritivo (não o legado "[Arquivo binário: ...]")
        assert "não convertido" in body["text_content"]
        assert "weird.xyz" in body["text_content"]
        # Padrão hardening: erro foi logado com contexto
        events = [getattr(rec, "event", None) for rec in caplog.records]
        assert "workspace.upload" in events
        rec = next(r for r in caplog.records if getattr(r, "event", None) == "workspace.upload")
        assert rec.attachment_name == "weird.xyz"
        assert rec.error_type == "RuntimeError"

    def test_truncation_applies_to_markitdown_output_too(self, upload_client):
        """Output do markitdown maior que 50k é truncado igual ao UTF-8."""
        fake_pdf = b"\x25PDF \xff\xfe\x80\x81 binary"
        with patch("app.evidence.converters.convert_bytes") as mock_convert:
            mock_convert.return_value = "x" * 60_000

            r = _upload(upload_client, fake_pdf, "big.pdf", "application/pdf")

        body = r.json()
        assert "[...truncado em 50.000 caracteres]" in body["text_content"]
        assert len(body["text_content"]) < 60_000


# ─── agents.py _decode_attachments ──────────────────────────────────


class TestDecodeAttachmentsExtractsBinary:
    def _make_attachment(self, content: bytes, filename: str, content_type: str = None):
        """Helper: cria objeto AttachmentInput simulado com content_base64."""
        class _Att:
            pass

        att = _Att()
        att.filename = filename
        att.content_type = content_type
        att.content_base64 = base64.b64encode(content).decode("ascii")
        return att

    def test_utf8_passes_through(self):
        from app.routes.agents import _decode_attachments
        att = self._make_attachment(b"texto utf8", "note.txt", "text/plain")
        accepted, rejected = _decode_attachments([att])
        assert len(accepted) == 1
        assert accepted[0]["content"] == "texto utf8"
        assert rejected == []

    def test_binary_pdf_uses_markitdown(self):
        """REGRESSÃO do bug irmão (/invoke base64): PDF binário agora
        vira texto via markitdown. Antes o `content` ficava "" e o
        engine descartava silenciosamente."""
        from app.routes.agents import _decode_attachments
        att = self._make_attachment(b"\x25PDF \xff\xfe\x80\x81 binary garbage", "doc.pdf", "application/pdf")

        with patch("app.evidence.converters.convert_bytes") as mock_convert:
            mock_convert.return_value = "Texto extraído do PDF"
            accepted, rejected = _decode_attachments([att])

        assert len(accepted) == 1
        assert accepted[0]["content"] == "Texto extraído do PDF"
        mock_convert.assert_called_once()

    def test_markitdown_failure_logs_warning_and_keeps_empty_content(self, caplog):
        """Conversor falha → mantém content="" (comportamento legado) mas
        loga warning com filename para diagnóstico."""
        from app.routes.agents import _decode_attachments
        att = self._make_attachment(b"\x00\x01\xff\xfe\x80\x81 bad", "x.bin", "application/octet-stream")

        with patch("app.evidence.converters.convert_bytes") as mock_convert:
            mock_convert.side_effect = RuntimeError("conv crashou")
            with caplog.at_level(logging.WARNING, logger="app.routes.agents"):
                accepted, rejected = _decode_attachments([att])

        # Content vazio (engine descarta) — mas agora há log para o operador
        assert len(accepted) == 1
        assert accepted[0]["content"] == ""
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agents.invoke_attachments" in events
        rec = next(r for r in caplog.records if getattr(r, "event", None) == "agents.invoke_attachments")
        assert rec.attachment_name == "x.bin"
        assert rec.error_type == "RuntimeError"

    def test_invalid_base64_still_rejected(self):
        """Base64 inválido continua sendo rejeitado antes de tentar conversão
        (não-regressão)."""
        from app.routes.agents import _decode_attachments

        class _Att:
            filename = "x.pdf"
            content_type = "application/pdf"
            content_base64 = "not valid base64 !!!"

        accepted, rejected = _decode_attachments([_Att()])
        assert accepted == []
        assert len(rejected) == 1
        assert rejected[0]["kind"] == "invalid_base64"
