"""Hint de bloqueio do wizard de publicação (66.5.2) — achado E2E 2026-07-22.

No cadastro de plataforma externa (GPT do ChatGPT), o botão "Próximo" do passo
Disclosure ficava `disabled` SEM dizer o motivo: marcar "Persiste input do
consumer" passa a exigir Retenção(dias), mas a UI só mostrava o botão morto —
foi preciso ler o `canAdvance()` no código para descobrir o porquê.

Fix: `advanceBlockReason()` devolve o motivo textual (string vazia = pode
avançar), renderizado ao lado do botão + no title. O RISCO é o hint DIVERGIR
do gate (mentir): este teste sela que os dois vivem no mesmo template e cobrem
os MESMOS predicados, por source (o template é Alpine JS — pytest não executa;
padrão de test_gold_split_probe/test_settings). Se um predicado entrar no
canAdvance sem entrar no reason, o teste falha.
"""

import re
from pathlib import Path

SRC = Path("app/templates/pages/catalog_publish.html").read_text(encoding="utf-8")


def _body(fn_name: str) -> str:
    # Corpo do método Alpine `nome() { ... }` até o fechamento no nível do objeto
    # (heurística: até a próxima linha "        }," na indentação do método).
    i = SRC.index(f"{fn_name}() {{")
    j = SRC.index("\n        },", i)
    return SRC[i:j]


def test_o_hint_existe_e_esta_ligado_ao_botao():
    assert "advanceBlockReason()" in SRC
    assert 'data-testid="pub-next-reason"' in SRC
    # title do botão também usa o motivo (acessível ao hover).
    assert ':title="advanceBlockReason()"' in SRC


def test_paridade_predicados_canAdvance_x_reason():
    can = _body("canAdvance")
    reason = _body("advanceBlockReason")
    # Cada gate do canAdvance precisa ter um motivo correspondente no reason —
    # senão o botão trava sem explicar (o bug original).
    predicados = [
        "form.artifact_id",            # step 1 interno
        "form.name",                   # step 2
        r"\.test\(this\.form\.version\)",  # step 2 semver
        r"external\.vendor",           # step 3 externo
        "capability.calls_external_apis",  # step 3 URLs
        "capability.stores_input",     # step 3 retenção
    ]
    for p in predicados:
        assert re.search(p, can), f"predicado sumiu do canAdvance: {p}"
        assert re.search(p, reason), (
            f"predicado {p!r} está no canAdvance mas NÃO no advanceBlockReason — "
            "o botão travaria sem explicar o motivo (regressão do achado)."
        )


def test_reason_vazio_quando_pode_avancar():
    # Todos os ramos que 'return' cedo devem devolver STRING (motivo) e o
    # caminho feliz devolve '' — nunca undefined (x-show quebraria).
    reason = _body("advanceBlockReason")
    assert "return ''" in reason
    # external_platform e recipe no passo 1 não têm gate → '' explícito.
    assert "return ''" in reason
