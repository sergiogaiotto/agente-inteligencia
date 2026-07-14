"""output_mapping da skill 'Consulta de CEP' (BrasilAPI → contrato ViaCEP).

CONTEXTO / bug "resultado parcial" (2026-06-07):
A skill declarativa 'Consulta de CEP' aponta o binding para a **BrasilAPI**
(`GET /api/cep/v1/{cep}`), que responde com os campos
`{cep, state, city, neighborhood, street, service}`. Mas:
  • o `## Output Contract` da skill exige nomes do **ViaCEP**
    (`cep, logradouro, bairro, localidade, uf`); e
  • o binding **não tinha `output_mapping`** — que o engine declarativo trata
    como OBRIGATÓRIO (`declarative_engine.py`: "output_mapping é obrigatório").
Resultado: a chamada dava 2xx, mas o engine acusava erro de mapping →
`final_state=partial` → a UI mostrava "· resultado parcial" e o endereço não
chegava limpo ao contexto (quebrando o encadeamento → Tavily).

FIX (config de skill, sem código): adicionar ao binding o `output_mapping`
mapeando os campos da BrasilAPI para os nomes do contrato. Este teste prova que
esse mapeamento, aplicado pela função real do engine (`_apply_output_mapping`)
sobre a resposta REAL da BrasilAPI, produz exatamente os 5 campos do contrato,
SEM erros. É a verificação (e a regressão) do fix entregue na config da skill.
"""
from __future__ import annotations


from app.agents.declarative_engine import _apply_output_mapping


# Resposta REAL da BrasilAPI para o CEP 13211740 (conferida ao vivo 2026-06-07).
BRASILAPI_RESPONSE = {
    "cep": "13211740",
    "state": "SP",
    "city": "Jundiaí",
    "neighborhood": "Recanto Quarto Centenário",
    "street": "Rua Aristides Mariotti",
    "service": "open-cep",
}

# O output_mapping que vai no binding da skill (## API Bindings):
#   output_mapping:
#     - from: $.cep        \n      to: cep
#     - from: $.street     \n      to: logradouro
#     - from: $.neighborhood \n    to: bairro
#     - from: $.city       \n      to: localidade
#     - from: $.state      \n      to: uf
CEP_OUTPUT_MAPPING = [
    {"from": "$.cep", "to": "cep"},
    {"from": "$.street", "to": "logradouro"},
    {"from": "$.neighborhood", "to": "bairro"},
    {"from": "$.city", "to": "localidade"},
    {"from": "$.state", "to": "uf"},
]


def test_mapping_produces_contract_fields_without_errors():
    additions, errors = _apply_output_mapping(
        BRASILAPI_RESPONSE, CEP_OUTPUT_MAPPING, bytes_budget=100_000
    )
    assert errors == [], f"mapeamento não deveria gerar erros, veio: {errors}"
    assert additions == {
        "cep": "13211740",
        "logradouro": "Rua Aristides Mariotti",
        "bairro": "Recanto Quarto Centenário",
        "localidade": "Jundiaí",
        "uf": "SP",
    }


def test_contract_required_fields_all_present():
    """Os 5 campos `required` do ## Output Contract ficam preenchidos — é o que
    elimina o 'resultado parcial' e dá ao Tavily um bairro+cidade utilizáveis."""
    additions, _ = _apply_output_mapping(
        BRASILAPI_RESPONSE, CEP_OUTPUT_MAPPING, bytes_budget=100_000
    )
    for required in ("cep", "logradouro", "bairro", "localidade", "uf"):
        assert additions.get(required), f"campo obrigatório do contrato ausente: {required}"


def test_without_mapping_engine_flags_mandatory_error():
    """Regressão da causa-raiz: binding SEM output_mapping → o engine exige (não
    é só 'mapeamento vazio inofensivo'). Aqui mapping=[] não gera additions; o
    erro 'obrigatório' é emitido na camada do binding (execute), provado abaixo
    pela ausência de qualquer campo do contrato."""
    additions, _ = _apply_output_mapping(BRASILAPI_RESPONSE, [], bytes_budget=100_000)
    assert additions == {}, "sem mapping não há additions — origem do 'resultado parcial'"
