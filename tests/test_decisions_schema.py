"""Contrato de Decisão — parser + validação (Cond-C, 35.18.0).

O agente DECLARA as decisões que anuncia (campo + enum fechado); a plataforma
valida a saída do LLM contra o enum de forma tolerante a acento/maiúscula e
devolve a grafia CANÔNICA — para a aresta comparar contra o valor declarado, não
contra o que o LLM digitou. Substitui o 'escalar=sim' in output_lower combinado
por telepatia.
"""
from app.skill_parser.decisions_schema import (
    extract_decisions_schema, validate_decision_value,
)

SKILL = """# Agente
## Purpose
Triagem.
## Decisions
```json
{ "escalar": ["sim", "não"], "severidade": ["baixa", "média", "alta"] }
```
## Guardrails
Nada.
"""


def test_extrai_campos_e_enums():
    s = extract_decisions_schema(SKILL)
    assert s == {"escalar": ["sim", "não"], "severidade": ["baixa", "média", "alta"]}


def test_ausente_ou_malformado_vira_none():
    assert extract_decisions_schema("") is None
    assert extract_decisions_schema("# Agente\nsem decisions") is None
    assert extract_decisions_schema("## Decisions\n```json\n{ nao é json }\n```") is None
    assert extract_decisions_schema("## Decisions\n```json\n[1,2,3]\n```") is None  # não-dict


def test_descarta_campo_invalido_preserva_validos():
    s = extract_decisions_schema(
        '## Decisions\n```json\n{"ok": ["a","b"], "cd cliente": ["x"], "vazio": [], "num": [1,2]}\n```')
    # campo com espaço, lista vazia e lista não-string são descartados
    assert s == {"ok": ["a", "b"]}


def test_valores_duplicados_por_norma_deduplicados():
    s = extract_decisions_schema('## Decisions\n```json\n{"f": ["Sim","sim","SIM","não","nao"]}\n```')
    # 'Sim'/'sim'/'SIM' colapsam; 'não'/'nao' colapsam — grafia do 1º preservada
    assert s == {"f": ["Sim", "não"]}


def test_validate_tolerante_a_acento_e_caixa():
    schema = {"escalar": ["sim", "não"]}
    # o LLM pode emitir 'SIM', 'Não', 'nao' — todos casam e voltam CANÔNICOS
    assert validate_decision_value(schema, "escalar", "SIM") == "sim"
    assert validate_decision_value(schema, "escalar", "Não") == "não"
    assert validate_decision_value(schema, "escalar", "nao") == "não"
    assert validate_decision_value(schema, "escalar", "  sim  ") == "sim"


def test_validate_fora_do_enum_ou_campo_inexistente():
    schema = {"escalar": ["sim", "não"]}
    assert validate_decision_value(schema, "escalar", "talvez") is None  # fora do enum
    assert validate_decision_value(schema, "inexistente", "sim") is None  # campo não existe
    assert validate_decision_value({}, "escalar", "sim") is None
