"""Tradutor NL→Jinja (Fatia 4) — descrição em pt-BR → regra condicional.

DNA "IA sugere → sistema PROVA" (mesmo do Catálogo de Dados): o LLM propõe
uma expressão Jinja booleana usando SÓ as variáveis canônicas, e o backend
reconcilia o resultado contra `CONDITIONAL_VARS_META` (fonte única do engine).

Armadilha endereçada aqui (a "killer objection" do estudo): `meta.
find_undeclared_variables` sobre uma expressão NUA (`'pix' in output_lower`)
retorna VAZIO — o parser Jinja não enxerga variáveis fora de `{{ }}`. Sem
envolver a expressão, o "cofre" aprovaria QUALQUER coisa (selo sempre-verde),
pior que não validar. `validate_conditional_expression` envolve em `{{ }}`
antes de parsear — é o que faz as variáveis aparecerem para o set-diff.
"""
from __future__ import annotations


def build_suggest_messages(description: str, vars_meta: list[dict]) -> list[dict]:
    """Monta as mensagens do LLM. O vocabulário vem do `CONDITIONAL_VARS_META`
    vivo — nunca uma lista hardcoded que poderia divergir do runtime."""
    catalog = "\n".join(
        f"- {v['name']} ({v.get('type', '?')}): {v.get('desc', '')}" for v in vars_meta
    )
    system = (
        "Você converte uma descrição em português de QUANDO uma conexão entre "
        "agentes deve rodar em UMA expressão booleana Jinja2.\n\n"
        "REGRAS RÍGIDAS:\n"
        "1. Use SOMENTE estas variáveis (nada além delas):\n"
        f"{catalog}\n\n"
        "2. Para casar palavra em texto use `'palavra' in output_lower` (ou "
        "input_lower/text_all) — sempre minúsculas, NUNCA `==`.\n"
        "3. Combine com `and`/`or` e parênteses quando precisar.\n"
        "4. Responda APENAS com a expressão — sem markdown, sem aspas ao redor, "
        "sem explicação.\n\n"
        "Exemplos:\n"
        "- \"se mencionar pix ou ted na resposta\" => 'pix' in output_lower or 'ted' in output_lower\n"
        "- \"quando o usuário anexar um documento\" => has_document\n"
        "- \"se a decisão foi recusar\" => is_refuse\n"
        "- \"se a pergunta fala de limite e a resposta tem link\" => 'limite' in input_lower and contains_url"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": description},
    ]


def extract_expression(text: str) -> str:
    """Extrai a expressão da resposta do LLM, tolerante a cercas de código.

    NÃO remove aspas simples (são literais Jinja: `'pix' in ...`). Só tira
    cercas ``` e backticks de envoltório, e pega a 1ª linha significativa.
    """
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()[1:]  # descarta ``` ou ```jinja
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    for line in s.splitlines():
        line = line.strip().strip("`").strip()
        if line:
            return line
    return s.strip()


def validate_conditional_expression(expr: str, canonical: set[str]) -> dict:
    """Reconcilia a expressão gerada contra o vocabulário canônico.

    Returns {valid, used_vars, unknown_vars, error}. `valid` só é True quando
    a sintaxe parseia E todas as variáveis referenciadas existem.
    """
    expr = (expr or "").strip()
    if not expr:
        return {"valid": False, "error": "Expressão vazia.", "used_vars": [], "unknown_vars": []}

    from jinja2 import meta
    from jinja2.sandbox import SandboxedEnvironment

    env = SandboxedEnvironment()
    try:
        # {{ }} OBRIGATÓRIO: sem isso find_undeclared_variables devolve set()
        # vazio e o guardrail vira selo sempre-verde (a armadilha do estudo).
        ast = env.parse("{{ " + expr + " }}")
    except Exception as e:  # TemplateSyntaxError etc.
        return {"valid": False, "error": f"Sintaxe Jinja inválida: {e}", "used_vars": [], "unknown_vars": []}

    used = sorted(meta.find_undeclared_variables(ast))
    unknown = sorted(set(used) - set(canonical))
    valid = not unknown
    return {
        "valid": valid,
        "used_vars": used,
        "unknown_vars": unknown,
        "error": "" if valid else f"A IA usou variáveis que não existem: {', '.join(unknown)}.",
    }
