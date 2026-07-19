"""Contrato de Decisão — parser + validação + extração + diretiva (Cond-C, 35.19.0).

O agente DECLARA as decisões que anuncia (campo + enum fechado); a plataforma
injeta a instrução selada no prompt, extrai a linha `DECISAO:` da saída do LLM,
valida contra o enum de forma tolerante a acento/maiúscula e devolve a grafia
CANÔNICA — para a aresta comparar contra o valor declarado, não contra o que o
LLM digitou. Substitui o 'escalar=sim' in output_lower combinado por telepatia.
"""
from app.skill_parser.decisions_schema import (
    build_decision_line, build_decisions_directive, extract_decision_line,
    extract_decisions_schema, has_decision_line, is_decision_only,
    preserve_decision_line, strip_decision_line, validate_decision_value,
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


def test_diretiva_manda_nao_traduzir_a_linha():
    # colisão idioma×contrato (review 2026-07-15): a diretiva de idioma manda
    # traduzir TUDO — a diretiva do contrato precisa selar a linha como verbatim.
    d = build_decisions_directive(SCHEMA)
    assert "SEM traduzir" in d


# ── higiene do schema (review 2026-07-15) ────────────────────────────────────

def test_valor_com_separador_da_linha_e_rejeitado():
    # ';' ',' '=' são os separadores de `DECISAO: campo=valor; ...` — um valor
    # que os contém é IRREPRESENTÁVEL na linha e o contrato nasceria morto.
    s = extract_decisions_schema(
        '## Decisions\n```json\n{"parecer": ["sim, com ressalvas", "não"], "f": ["a=b", "x;y", "ok"]}\n```')
    assert s == {"parecer": ["não"], "f": ["ok"]}


def test_campo_acentuado_e_rejeitado():
    # \w unicode aceitaria 'situação', mas o lookup do parser é EXATO e a expr
    # da aresta digita o campo — identificador ASCII puro obrigatório.
    s = extract_decisions_schema(
        '## Decisions\n```json\n{"situação": ["x"], "ok": ["y"]}\n```')
    assert s == {"ok": ["y"]}


def test_campo_com_nome_de_metodo_de_dict_e_rejeitado():
    # `decision.items` no Jinja resolve o MÉTODO do dict, não o valor anunciado
    # — a regra nunca casaria (review pré-push 2026-07-15).
    s = extract_decisions_schema(
        '## Decisions\n```json\n{"items": ["poucos", "muitos"], "get": ["a"], "ok": ["y"]}\n```')
    assert s == {"ok": ["y"]}


# ── aliases de idioma no prefixo (review 2026-07-15) ─────────────────────────

def test_alias_decision_e_decision_es_casam():
    # defesa em profundidade: agente en-US/es pode traduzir o prefixo apesar da
    # diretiva verbatim — o ENUM continua selando os valores.
    assert has_decision_line("análise\nDECISION: escalar=sim")
    assert has_decision_line("análisis\nDECISIÓN: escalar=sim")
    assert extract_decision_line("DECISION: escalar=sim", SCHEMA) == {"escalar": "sim"}
    assert extract_decision_line("Decisión: severidade=alta", SCHEMA) == {"severidade": "alta"}


# ── build_decision_line (forma canônica) ─────────────────────────────────────

def test_build_decision_line_roundtrip():
    dec = {"escalar": "sim", "severidade": "alta"}
    line = build_decision_line(dec)
    assert line == "DECISAO: escalar=sim; severidade=alta"
    assert extract_decision_line(line, SCHEMA) == dec


# ── preserve_decision_line: corte no MEIO da linha (review 2026-07-15) ───────

ORIGINAL = "Análise do caso.\nDECISAO: escalar=sim; severidade=alta"


def test_preserve_corte_no_meio_do_corpo():
    # o caso MAIS provável: a linha é a última → overflow pequeno corta DENTRO
    # dela; o guard antigo (has_decision_line) devolvia campos amputados.
    truncated = "Análise do caso.\nDECISAO: escalar=sim; severida…"
    got = preserve_decision_line(ORIGINAL, truncated, SCHEMA)
    assert got.endswith("\nDECISAO: escalar=sim; severidade=alta")
    assert got.count("DECISAO") == 1  # a parcial saiu, só a canônica fica


def test_preserve_corte_no_meio_do_valor():
    truncated = "Análise do caso.\nDECISAO: escalar=sim; severidade=al…"
    got = preserve_decision_line(ORIGINAL, truncated, SCHEMA)
    assert got.endswith("\nDECISAO: escalar=sim; severidade=alta")
    assert got.count("DECISAO") == 1


def test_preserve_corte_no_meio_do_prefixo():
    # corte antes do ':' — não casa a regex da linha, mas o fragmento 'DECIS…'
    # não pode sobrar duplicado no texto final.
    truncated = "Análise do caso.\nDECIS…"
    got = preserve_decision_line(ORIGINAL, truncated, SCHEMA)
    assert got.endswith("\nDECISAO: escalar=sim; severidade=alta")
    assert "DECIS…" not in got


def test_preserve_nao_mexe_quando_extracao_igual():
    # a linha sobreviveu INTEIRA ao truncate → texto devolvido intacto
    truncated = "Análise…\nDECISAO: escalar=sim; severidade=alta"
    assert preserve_decision_line(ORIGINAL, truncated, SCHEMA) == truncated


def test_preserve_prosa_nao_e_confundida_com_prefixo():
    # 'Decisões…' NÃO é prefixo de nenhuma grafia da linha — prosa fica.
    truncated = "As Decisões…"
    got = preserve_decision_line(ORIGINAL, truncated, SCHEMA)
    assert got.startswith("As Decisões…")
    assert got.endswith("\nDECISAO: escalar=sim; severidade=alta")


def test_preserve_nao_deleta_heading_de_prosa_decision():
    # MAJOR do review pré-push (repro real): heading 'Decision: ...' é prosa
    # comum em respostas en-US — casa a regex (alias) mas NÃO valida par algum;
    # a remoção precisa do gate, senão o conteúdo some em silêncio.
    original = (
        "Analysis done.\nDecision: Approve the refund and notify billing.\n"
        "More details here.\nDECISAO: escalar=sim; severidade=alta"
    )
    truncated = (
        "Analysis done.\nDecision: Approve the refund and notify billing.\n"
        "More details here.\nDECISAO: escalar=sim; severida…"
    )
    got = preserve_decision_line(original, truncated, SCHEMA)
    assert "Decision: Approve the refund and notify billing." in got
    assert got.endswith("\nDECISAO: escalar=sim; severidade=alta")
    assert got.count("DECISAO:") == 1


def test_preserve_nao_remove_citacao_no_meio():
    # eco do formato no MEIO do texto (linha que casa a regex mas não é o rabo)
    # fica — remoção é trailing-only, simétrica ao strip de display.
    original = "DECISAO: escalar=<sim|não>\nAnálise do caso.\nDECISAO: escalar=sim; severidade=alta"
    truncated = "DECISAO: escalar=<sim|não>\nAnálise do caso.\nDECISAO: escalar=sim; severida…"
    got = preserve_decision_line(original, truncated, SCHEMA)
    assert got.startswith("DECISAO: escalar=<sim|não>\nAnálise do caso.")
    assert got.endswith("\nDECISAO: escalar=sim; severidade=alta")


def test_preserve_sem_schema_ou_sem_linha_no_original():
    assert preserve_decision_line("x", "y", None) == "y"
    assert preserve_decision_line("sem linha", "trunc…", SCHEMA) == "trunc…"


# ── strip_decision_line: resposta final SEM jargão de máquina ────────────────

def test_strip_remove_linha_final():
    txt = "Resposta ao cliente.\n\nDECISAO: escalar=sim; severidade=alta"
    assert strip_decision_line(txt, SCHEMA) == "Resposta ao cliente."


def test_strip_multiplas_linhas_finais():
    txt = "Resposta.\nDECISAO: escalar=sim\nDECISAO: severidade=alta"
    assert strip_decision_line(txt, SCHEMA) == "Resposta."


def test_strip_nao_toca_linha_no_meio():
    # citação do formato no meio do texto não é protocolo — fica.
    txt = "Exemplo: DECISAO: escalar=sim\nE a resposta continua."
    assert strip_decision_line(txt, SCHEMA) == txt


def test_strip_gate_duplo_sem_schema_e_sem_par_valido():
    # BLOCKER do review do plano: sem gate, prosa legítima 'Decisão: ...' de
    # agente legado seria amputada. Gate 1: sem schema → no-op.
    txt = "Análise concluída.\nDecisão: aprovado o crédito"
    assert strip_decision_line(txt, None) == txt
    # Gate 2: com schema, linha cujo corpo NÃO valida par algum → fica.
    assert strip_decision_line(txt, SCHEMA) == txt
    fora_do_enum = "Análise.\nDECISAO: escalar=talvez"
    assert strip_decision_line(fora_do_enum, SCHEMA) == fora_do_enum


def test_strip_nunca_esvazia():
    # resposta que é SÓ a linha (agente classificador) → devolve o original,
    # nunca string em branco na UI.
    txt = "DECISAO: escalar=sim"
    assert strip_decision_line(txt, SCHEMA) == txt


def test_strip_output_contract_json_volta_a_parsear():
    import json as _json
    txt = '{"resultado": "ok"}\nDECISAO: escalar=sim'
    got = strip_decision_line(txt, SCHEMA)
    assert _json.loads(got) == {"resultado": "ok"}


# ── is_decision_only: output que é SÓ protocolo (router terminal, #5) ─────────

def test_decision_only_uma_linha():
    # o caso do #5: router terminal cujo output é só a linha DECISAO.
    assert is_decision_only("DECISAO: escalar=sim", SCHEMA) is True


def test_decision_only_com_espacos_e_multiplas_linhas():
    txt = "\n  DECISAO: escalar=sim\n\nDECISAO: severidade=alta  \n"
    assert is_decision_only(txt, SCHEMA) is True


def test_decision_only_falso_quando_ha_prosa_antes():
    # prosa + linha final = resposta real → NÃO é "só decisão" (o strip cuida).
    assert is_decision_only("Resposta ao cliente.\nDECISAO: escalar=sim", SCHEMA) is False


def test_decision_only_falso_quando_ha_prosa_depois():
    # linha no MEIO (prosa após) também não é "só decisão".
    assert is_decision_only("DECISAO: escalar=sim\nE a resposta continua.", SCHEMA) is False


def test_decision_only_falso_sem_schema():
    assert is_decision_only("DECISAO: escalar=sim", None) is False


def test_decision_only_falso_linha_fora_do_enum():
    # valor fora do contrato não valida → não é protocolo reconhecido → prosa.
    assert is_decision_only("DECISAO: escalar=talvez", SCHEMA) is False


def test_decision_only_falso_texto_vazio():
    assert is_decision_only("", SCHEMA) is False
    assert is_decision_only("   \n  ", SCHEMA) is False


def test_decision_only_falso_citacao_do_formato_no_meio():
    # "Exemplo: DECISAO: ..." não casa a âncora ^ da linha → prosa, não protocolo.
    txt = "Exemplo: DECISAO: escalar=sim\nE a resposta continua."
    assert is_decision_only(txt, SCHEMA) is False


def test_valor_com_borda_stripavel_e_rejeitado():
    # extract_decision_line stripa "'`*_. da BORDA do valor emitido — um
    # canônico com esses chars nunca casaria (review pré-push do Cond-C.2).
    s = extract_decisions_schema(
        '## Decisions\n```json\n{"parecer": ["aprovado.", "*negado*", "ok"], "v": ["v1.2"]}\n```')
    # 'v1.2' fica (ponto INTERNO); os de borda caem
    assert s == {"parecer": ["ok"], "v": ["v1.2"]}
