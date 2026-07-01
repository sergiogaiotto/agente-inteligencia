"""Política mínima de senha (SKILL.md §1 / CWE-521)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.schemas import UserCreate, UserUpdate


def test_create_requires_min_length():
    with pytest.raises(ValidationError):
        UserCreate(username="a", password="short")   # 5 chars
    u = UserCreate(username="a", password="senha-forte-123")
    assert u.password == "senha-forte-123"


def test_update_password_min_length_when_provided():
    with pytest.raises(ValidationError):
        UserUpdate(password="1234567")   # 7 chars
    ok = UserUpdate(password="12345678")  # 8 chars
    assert ok.password == "12345678"


def test_update_without_password_is_allowed():
    # None e "" significam "não alterar" — não devem falhar a validação.
    assert UserUpdate(display_name="x").password is None
    assert UserUpdate(password="").password == ""
