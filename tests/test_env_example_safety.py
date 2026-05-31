"""Guard-rail: .env.example não pode conter secrets reais nem perder a
disciplina de placeholders.

Justificativa: em 2026-05-30 (PR #220), um IDE/extensão local injetou uma
chave OpenAI real (`sk-proj-...`) no .env.example modificado no working
tree. O incidente foi pego no review antes do commit, mas mostrou que o
arquivo está exposto a esse tipo de regressão.

Este teste é um GUARD-RAIL: qualquer commit que tente subir uma key real
(OpenAI, GitHub, AWS, Slack, JWT) ou que abandone a disciplina de "campo
sensível = vazio ou placeholder explícito" quebra o CI antes de mergear.

Filosofia (alinhada a [[feedback-no-local-config-in-git]]): a plataforma
resolve configuração via UI Settings em runtime. .env.example é apenas
documentação de quais variáveis existem — nunca veículo de valores reais.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# ─── Padrões de secrets em texto claro ─────────────────────────


# Cada entrada: (nome, regex). Padrões intencionalmente conservadores para
# minimizar falso-positivo em comentários. Quando algum bater, o teste
# reporta nome + linha + prefixo do match.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # OpenAI: nova (project key sk-proj-) e legacy (sk-)
    ("OpenAI project key", re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI legacy key", re.compile(r"(?<!sk-proj)\bsk-[A-Za-z0-9]{32,}\b")),
    # GitHub PATs: ghp_ (classic), gho_ (oauth), ghu_ (user), ghs_ (server), ghr_ (refresh)
    ("GitHub PAT", re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}")),
    # AWS Access Key ID
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Slack tokens: xoxa (app), xoxb (bot), xoxp (user)
    ("Slack token", re.compile(r"xox[abp]-[A-Za-z0-9\-]{10,}")),
    # JWT — começo padrão eyJ + 2 segmentos base64url (sigla típica em service accounts)
    ("JWT", re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"
    )),
    # Anthropic API key (claro, futuro provider)
    ("Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
]


ENV_EXAMPLE = Path(".env.example")


# ─── Tests ─────────────────────────────────────────────────────


def test_env_example_exists_at_repo_root():
    """Pré-condição. Se o arquivo sumir, devs perdem a doc de variáveis."""
    assert ENV_EXAMPLE.exists(), (
        f"{ENV_EXAMPLE} não encontrado. É a referência canônica das env vars; "
        "se renomearam ou moveram, atualizar este teste antes."
    )


def test_env_example_has_no_real_secrets():
    """O arquivo não pode bater nenhum padrão conhecido de secret.

    Se você está vendo este teste falhar:
    1. Provavelmente um IDE/extensão preencheu valor real numa linha
       sensível. Volte para placeholder vazio ou `troque-isto-...`.
    2. Se for falso-positivo (ex.: uma string que casualmente bate `sk-...`
       em um exemplo de schema), reformule a string ou estreite o regex
       acima — não relaxe globalmente.
    3. **NUNCA** desabilite este teste sem avaliar se foi exfiltração real.
    """
    content = ENV_EXAMPLE.read_text(encoding="utf-8")
    issues: list[tuple[str, int, str]] = []
    for name, regex in SECRET_PATTERNS:
        for m in regex.finditer(content):
            line_no = content[: m.start()].count("\n") + 1
            preview = m.group()[:20] + "..."
            issues.append((name, line_no, preview))
    assert not issues, (
        f"Secret(s) real(is) detectado(s) em {ENV_EXAMPLE}. "
        f"Substitua por placeholder ('troque-...', vazio, '<your-key>'). "
        f"Achados: {issues}"
    )


def test_env_example_sensitive_lines_use_placeholders():
    """Linhas de credentials sensíveis devem ter valor vazio OU placeholder
    declarado e reconhecível — nunca um valor concreto, mesmo que não bata
    nos regex acima.

    Sufixos considerados sensíveis: _KEY, _SECRET, _PASSWORD, _TOKEN.
    Placeholders aceitos: vazio, ou prefixo `troque-`/`your-`/`<`/`REPLACE`/`TODO`.
    """
    placeholders_aceitos = ("troque-", "your-", "<", "REPLACE", "TODO")
    sensitive_suffixes = ("_KEY", "_SECRET", "_PASSWORD", "_TOKEN")
    issues: list[tuple[int, str, str]] = []

    for line_no, raw in enumerate(
        ENV_EXAMPLE.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key_up = key.strip().upper()
        value = value.strip()

        if not any(key_up.endswith(suf) for suf in sensitive_suffixes):
            continue
        if value == "":
            continue
        # Aceita inline comment depois do valor (ex: `KEY=val  # nota`)
        value_clean = value.split("#", 1)[0].strip()
        if any(value_clean.startswith(p) for p in placeholders_aceitos):
            continue

        issues.append((line_no, key_up, value_clean[:30]))

    assert not issues, (
        f"Credentials em {ENV_EXAMPLE} devem usar placeholder explícito ou ficar "
        f"vazias. Achados: {issues}"
    )


# Fake secrets concatenados em runtime para NÃO baterem em secret scanners
# (GitHub Push Protection, gitleaks, trufflehog). Strings curtas por si só
# não combinam nenhum padrão conhecido — só ficam parecidas com um secret
# real depois que o Python concatena na execução do teste.
_FAKE_PREFIX_PROJ = "sk-" + "proj-"
_FAKE_PREFIX_LEGACY = "sk-"
_FAKE_PREFIX_GH = "gh" + "p_"
_FAKE_PREFIX_AWS = "AK" + "IA"
_FAKE_PREFIX_SLACK = "xo" + "xb-"


@pytest.mark.parametrize(
    "fake_secret",
    [
        _FAKE_PREFIX_PROJ + "uftY9Kq8uWMGe6iMAorgC5KBPJf3HpehOiK9nDH" + "_83QPBxHFNvm",
        _FAKE_PREFIX_LEGACY + "1234567890abcdefghijklmnopqrstuvwxyz1234",
        _FAKE_PREFIX_GH + "1234567890abcdefghijklmnopqrstuvwxyz12",
        _FAKE_PREFIX_AWS + "IOSFODNN7EXAMPLE",
        _FAKE_PREFIX_SLACK + "1234567890-" + "abcdefghijklmnopqrstuvwx",
    ],
)
def test_regex_actually_catches_real_format_secrets(fake_secret, tmp_path):
    """Sanity: cada regex pega exemplos formatados como secrets reais.

    Garante que o guard-rail principal não é "regex que nunca encontra nada".
    Roda contra arquivo temporário — não toca em .env.example real.

    Os fake-secrets são montados via concat de strings (acima) para evitar
    que secret scanners (GitHub Push Protection, gitleaks etc) bloqueiem
    este arquivo pensando que são vazamentos reais. Lição aprendida no
    próprio PR deste guard-rail: a primeira versão tinha os fakes literais
    e o push foi bloqueado pelo Slack detector do GitHub.
    """
    probe = tmp_path / ".env.example"
    probe.write_text(f"OPENAI_API_KEY={fake_secret}\n", encoding="utf-8")
    matched = False
    for _, regex in SECRET_PATTERNS:
        if regex.search(probe.read_text()):
            matched = True
            break
    assert matched, (
        f"Nenhum padrão de SECRET_PATTERNS pegou o secret de exemplo {fake_secret[:25]}... "
        "Se o formato de algum provider mudou, atualizar o regex correspondente."
    )
