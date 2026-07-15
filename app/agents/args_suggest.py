"""Tradutor NL→args (item 2 do plano, 38.0.0) — pedido em pt-BR → dict `args`.

DNA "IA sugere → sistema PROVA" (o mesmo do tradutor NL→Jinja das regras
condicionais, app/agents/conditional_suggest.py): o LLM propõe um objeto JSON
usando SÓ os campos do ## Inputs do agente-raiz (contrato SELADO quando o
pipeline está publicado), e o backend PROVA a sugestão com o MESMO resolvedor
do invoke (`_resolve_args`: defaults → coerção → validação de tipo/enum/
required/unknown_field com did-you-mean).

Diferença estrutural vs NL→Jinja: lá o vocabulário é FECHADO (variáveis
canônicas — a prova é quase completa); aqui os VALORES são abertos — a prova
pega tipo/enum/required/chave desconhecida, mas NÃO pega valor plausível-
porém-inventado. Por isso o prompt proíbe inventar (campo sem informação no
pedido fica FORA do JSON) e o endpoint nunca executa: humano/integrador
confirma antes do invoke.

Módulo stdlib-only (sem import de app/*, sem ciclos) — mesma regra do irmão.
"""
from __future__ import annotations

import json
import unicodedata


def _fmt_field(name: str, spec: dict, required: set) -> str:
    parts = [f"- {name} ({spec.get('type', '?')})"]
    if name in required:
        parts.append("[OBRIGATÓRIO]")
    enum = spec.get("enum")
    if isinstance(enum, list) and enum:
        parts.append("aceita EXATAMENTE um de: " + " | ".join(str(e) for e in enum))
    if "default" in spec:
        parts.append(f"[default: {spec['default']!r} — omita se o pedido não disser]")
    desc = (spec.get("description") or "").strip()
    if desc:
        parts.append(f": {desc}")
    return " ".join(parts)


def build_args_messages(
    description: str, schema: dict, partial_args: dict | None = None,
) -> list[dict]:
    """Monta as mensagens do LLM. O catálogo de campos vem do JSON Schema VIVO
    (## Inputs parseado / contrato selado) — nunca uma lista hardcoded que
    poderia divergir do runtime (mesma filosofia do CONDITIONAL_VARS_META)."""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    catalog = "\n".join(
        _fmt_field(n, s if isinstance(s, dict) else {}, required)
        for n, s in props.items()
    )
    system = (
        "Você extrai PARÂMETROS de um pedido em português para invocar um "
        "pipeline de agentes.\n\n"
        "REGRAS RÍGIDAS:\n"
        "1. Use SOMENTE estes campos (nada além deles):\n"
        f"{catalog}\n\n"
        "2. NUNCA invente valor: se o pedido NÃO contém a informação de um "
        "campo, deixe o campo FORA do objeto (não use null, não chute).\n"
        "3. Campo com valores aceitos (enum): use EXATAMENTE um dos valores "
        "listados.\n"
        "4. Responda APENAS com um objeto JSON — sem markdown, sem cercas, "
        "sem explicação.\n\n"
        "Exemplo: campos {cd_cliente (integer), segmento (string, aceita: "
        "varejo | premium)} e pedido \"consulta o limite do cliente 1031 do "
        "varejo\" => {\"cd_cliente\": 1031, \"segmento\": \"varejo\"}"
    )
    if partial_args:
        system += (
            "\n\nO integrador JÁ preencheu estes campos — não os inclua nem "
            "os contradiga: " + ", ".join(sorted(partial_args))
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": description},
    ]


def _balanced_object_end(s: str, start: int) -> int:
    """Índice EXCLUSIVO do fecho do objeto {...} balanceado iniciado em
    `start`, respeitando strings/escapes JSON; -1 se não fecha."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def extract_args_json(text: str) -> tuple[dict | None, str]:
    """Extrai o objeto JSON da resposta do LLM, tolerante a cercas de código
    (inclusive de LINHA ÚNICA — via textnorm) e a prosa em volta. A varredura
    balanceada tenta CADA '{' do texto (review 38.0.0: desistir no primeiro
    candidato matava respostas com '{exemplo}' em prosa antes do objeto real;
    e '{' dentro de aspas de prosa dessincronizava o rastreio de string —
    candidatos podres agora só falham o próprio parse). Retorna (obj, "") ou
    (None, erro)."""
    from app.agents.textnorm import strip_code_fences

    raw = (text or "").strip()
    if not raw:
        return None, "A IA respondeu vazio."
    for cand in (strip_code_fences(raw), raw):
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj, ""
        except json.JSONDecodeError:
            pass
    pos = raw.find("{")
    while pos >= 0:
        end = _balanced_object_end(raw, pos)
        if end > pos:
            try:
                obj = json.loads(raw[pos:end])
                if isinstance(obj, dict):
                    return obj, ""
            except json.JSONDecodeError:
                pass
        pos = raw.find("{", pos + 1)
    return None, "A IA não devolveu um objeto JSON de args — tente reformular o pedido."


def repair_suggested_args(args: dict, schema: dict) -> dict:
    """Repairs DETERMINÍSTICOS da SUGESTÃO do LLM, antes da prova (análogo ao
    repair_unquoted_literals/normalize_norm_literals do NL→Jinja):

    1. Poda `null`: o prompt manda OMITIR campo sem informação, mas LLMs
       adoram emitir null — null aqui significa "não extraí", não um valor
       (sem a poda, viraria type_mismatch ruidoso na prova).
    2. Enum por grafia: valor que não casa exatamente mas casa por
       casefold+sem-acento (régua do textnorm — a MESMA do runtime) vira o
       membro CANÔNICO do enum ('Varejo' → 'varejo', 'nao' → 'não').

    Idempotente. SÓ para valores vindos do LLM — o endpoint aplica ANTES de
    mesclar partial_args (valor humano não é reescrito). A coerção de TIPO
    ('1031' → 1031) e a rejeição de bool-vs-enum-numérico (True==1 em Python)
    ficam com a PROVA (_resolve_args), fonte única do invoke."""
    from app.agents.textnorm import norm

    props = schema.get("properties") or {}
    out: dict = {}
    for field, val in (args or {}).items():
        if val is None:
            continue  # poda: null = "não extraí"
        spec = props.get(field)
        enum = spec.get("enum") if isinstance(spec, dict) else None
        if isinstance(enum, list) and enum and val not in enum:
            try:
                match = next((e for e in enum if norm(e) == norm(val)), None)
            except Exception:
                match = None  # valor não-normalizável (dict/list) — prova decide
            if match is not None:
                val = match
        out[field] = val
    return out
