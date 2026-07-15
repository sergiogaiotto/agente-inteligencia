"""Contrato de Decisão — parser + validação + extração + diretiva (Cond-C, 35.19.0).

O agente DECLARA as decisões que anuncia (campo + enum fechado); a plataforma
injeta a instrução selada no prompt, extrai a linha `DECISAO:` da saída do LLM,
valida contra o enum de forma tolerante a acento/maiúscula e devolve a grafia
CANÔNICA — para a aresta comparar contra o valor declarado, não contra o que o
LLM digitou. Substitui o 'escalar=sim' in output_lower combinado por telepatia.
"""
from app.skill_parser.decisions_schema import (
    build_decisions_directive, extract_decision_line, extract_decisions_schema,
    has_decision_line, validate_decision_value,
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


# ── extração da linha DECISAO (35.19.0) ──────────────────────────────────────

SCHEMA = {"escalar": ["sim", "não"], "severidade": ["baixa", "média", "alta"]}


def test_extrai_linha_no_fim_da_resposta():
    out = "O cliente relata falha crítica no pagamento.\n\nDECISAO: escalar=sim; severidade=alta"
    assert extract_decision_line(out, SCHEMA) == {"escalar": "sim", "severidade": "alta"}


def test_tolerante_a_acento_caixa_e_markdown():
    # 'DECISÃO' acentuada, valores com caixa/acento trocados, prefixo markdown
    out = "Análise concluída.\n**Decisão: escalar=NAO, severidade=Média**"
    got = extract_decision_line(out, SCHEMA)
    # devolve a grafia CANÔNICA do schema, não a digitada pelo LLM
    assert got == {"escalar": "não", "severidade": "média"}


def test_sela_campo_e_valor_fora_do_contrato():
    out = "DECISAO: escalar=talvez; inventado=x; severidade=alta"
    # 'talvez' fora do enum e 'inventado' fora do schema são descartados
    assert extract_decision_line(out, SCHEMA) == {"severidade": "alta"}


def test_ultima_linha_vence_por_campo():
    # eco do formato no meio da resposta + decisão real no fim
    out = "Exemplo: DECISAO: escalar=não\n...análise...\nDECISAO: escalar=sim"
    assert extract_decision_line(out, SCHEMA) == {"escalar": "sim"}


def test_eco_do_shape_da_diretiva_nao_vira_decisao():
    # o LLM copiou a linha-modelo com <a|b> — nada valida contra o enum
    out = "DECISAO: escalar=<sim|não>; severidade=<baixa|média|alta>"
    assert extract_decision_line(out, SCHEMA) == {}


def test_sem_linha_sem_schema_sem_texto():
    assert extract_decision_line("resposta sem linha", SCHEMA) == {}
    assert extract_decision_line("DECISAO: escalar=sim", None) == {}
    assert extract_decision_line("", SCHEMA) == {}


def test_has_decision_line_marker_barato():
    assert has_decision_line("blá\ndecisão: escalar=sim")
    assert has_decision_line("DECISAO: x=y")
    assert not has_decision_line("resposta comum sem a linha")
    assert not has_decision_line("")
    # 'decisão' citada em prosa (sem os dois-pontos de linha) não dispara
    assert not has_decision_line("a decisão do comitê foi adiada")


# ── diretiva selada de prompt (35.19.0) ──────────────────────────────────────

def test_diretiva_lista_campos_valores_e_shape():
    d = build_decisions_directive(SCHEMA)
    assert "## Contrato de Decisão" in d
    assert "DECISAO: escalar=<sim|não>; severidade=<baixa|média|alta>" in d
    assert "- escalar: sim | não" in d
    assert "- severidade: baixa | média | alta" in d
