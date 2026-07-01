"""Hardening do upload do workspace — path traversal (CWE-22) + tamanho (CWE-400).

`_safe_upload_name` garante que o nome do cliente NUNCA controla o diretório de
destino (usa só o basename, neutraliza `..`/`:`). O containment resolvido é
verificado adicionalmente no handler.
"""
from __future__ import annotations

import os

import pytest

from app.routes.workspace import UPLOAD_DIR, _safe_upload_name

_TRAVERSAL = [
    "../../etc/passwd",
    "..\\..\\windows\\system32\\evil.dll",
    "/etc/shadow",
    "a/b/c.txt",
    "....//....//x",
    "../" * 12 + "etc/cron.d/x",
    "..",
    "foo/../../bar",
]


@pytest.mark.parametrize("filename", _TRAVERSAL)
def test_no_traversal_escapes_upload_dir(filename):
    name = _safe_upload_name(filename, "abcd1234")
    # sem separadores nem componentes de traversal no nome final
    assert "/" not in name and "\\" not in name
    assert ".." not in name
    assert name.startswith("abcd1234_")
    # o caminho resolvido fica DENTRO de UPLOAD_DIR
    base = UPLOAD_DIR.resolve()
    dest = (UPLOAD_DIR / name).resolve()
    assert str(dest).startswith(str(base) + os.sep)


def test_preserves_reasonable_name():
    assert _safe_upload_name("relatorio final.pdf", "id1") == "id1_relatorio_final.pdf"


def test_empty_and_none_filename():
    assert _safe_upload_name("", "id1") == "id1_upload"
    assert _safe_upload_name(None, "id1") == "id1_upload"


def test_long_name_truncated():
    name = _safe_upload_name("x" * 500 + ".txt", "id1")
    assert len(name) <= len("id1_") + 150
