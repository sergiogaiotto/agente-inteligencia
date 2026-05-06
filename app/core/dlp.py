"""Redação de PII (Brasil-first) — proteção LLM06 Sensitive Information Disclosure.

Aplicado em DOIS pontos:
1. Antes da persistência em `turns.user_text_redacted` / `output_text_redacted`
   (a coluna já tinha o nome — agora cumpre o que promete).
2. Em `audit_log.details` quando o registro vem de input de usuário.

Não redacta antes do LLM por padrão (ver `dlp_redact_before_llm` em config).
Operadores que tratam dados sensíveis devem ativar essa flag — o trade-off
é o LLM perder contexto de identificadores reais.

Estratégia:
- Regex priorizando formatos com pontuação (CPF "123.456.789-00") sobre
  blocos de 11/14 dígitos — reduz falsos positivos com IDs internos.
- Luhn check para cartões (rejeita strings 16-dig que não passam).
- Substituição por placeholders fixos (`[CPF]`, `[EMAIL]`, etc) — preserva
  o tamanho aproximado da frase e mantém legibilidade do log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── Padrões ────────────────────────────────────────────────────

# CPF: ddd.ddd.ddd-dd (com pontuação obrigatória — evita FP com IDs)
_CPF_RE = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
# CNPJ: dd.ddd.ddd/dddd-dd
_CNPJ_RE = re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b")
# Email
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Telefone BR: (DD) 9999-9999 ou (DD) 99999-9999, com tolerância a espaços
# Aceita: 11 99999-9999 / (11)99999-9999 / 11999999999 (11 dígitos puros raramente — preferimos formato)
_PHONE_RE = re.compile(r"\(?\b(?:\d{2})\)?[\s-]?\d{4,5}-?\d{4}\b")
# Cartão: 13-19 dígitos com separadores opcionais (espaço/-) entre blocos de 4
_CARD_RE = re.compile(r"\b(?:\d{4}[\s-]?){3,4}\d{1,4}\b")
# CEP: ddddd-ddd
_CEP_RE = re.compile(r"\b\d{5}-\d{3}\b")


def _luhn_valid(digits: str) -> bool:
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    s = 0
    alt = False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        s += n
        alt = not alt
    return s % 10 == 0


def _redact_card(match: re.Match) -> str:
    raw = match.group(0)
    digits = re.sub(r"\D", "", raw)
    if _luhn_valid(digits):
        return "[CARD]"
    return raw  # falso positivo — preserva texto original


# ── API pública ────────────────────────────────────────────────


@dataclass
class RedactionStats:
    cpf: int = 0
    cnpj: int = 0
    email: int = 0
    phone: int = 0
    card: int = 0
    cep: int = 0

    @property
    def total(self) -> int:
        return self.cpf + self.cnpj + self.email + self.phone + self.card + self.cep


def count_pii(text: str) -> RedactionStats:
    """Conta ocorrências de PII sem alterar o texto."""
    if not text:
        return RedactionStats()
    return RedactionStats(
        cpf=len(_CPF_RE.findall(text)),
        cnpj=len(_CNPJ_RE.findall(text)),
        email=len(_EMAIL_RE.findall(text)),
        phone=len(_PHONE_RE.findall(text)),
        card=sum(1 for m in _CARD_RE.finditer(text) if _luhn_valid(re.sub(r"\D", "", m.group(0)))),
        cep=len(_CEP_RE.findall(text)),
    )


def redact(text: str) -> str:
    """Substitui PII por placeholders. Texto vazio passa direto."""
    if not text:
        return text
    text = _CPF_RE.sub("[CPF]", text)
    text = _CNPJ_RE.sub("[CNPJ]", text)
    text = _EMAIL_RE.sub("[EMAIL]", text)
    # Telefone vem antes de cartão para não disputar 4-dígit blocks repetidos
    text = _PHONE_RE.sub("[PHONE]", text)
    text = _CARD_RE.sub(_redact_card, text)
    text = _CEP_RE.sub("[CEP]", text)
    return text


def redact_for_persist(text: str) -> str:
    """Alias semântico — usado pelo state_machine ao gravar turnos."""
    return redact(text)


def redact_for_log(text: str) -> str:
    """Alias semântico — usado por audit_log/details que vem de entrada do usuário."""
    return redact(text)
