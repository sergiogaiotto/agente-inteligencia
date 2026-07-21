"""Paridade PARAMETER_UI_KEYS ↔ SettingsSave (66.4.2) — achado E2E 2026-07-21.

Classe de bug (2ª encarnação; a 1ª foi o _UI_TO_ENV_MAP no #700): a aba
Parâmetros lista as chaves de PARAMETER_UI_KEYS e salva via PUT /settings,
cujo corpo é o pydantic SettingsSave com extra='ignore' — chave listada na
aba mas SEM campo no modelo é DESCARTADA em silêncio: o toast diz
"Parâmetros salvos — já valem" e nada persiste. Foi exatamente o caso de
`verifier_signals_drive_fsm` (#686 adicionou config + _UI_TO_ENV_MAP +
PARAMETER_UI_KEYS + aba, esqueceu o campo): a flag ficou IMPOSSÍVEL de
ligar pela plataforma por 13 versões — e a validação do salto de accuracy
adversarial nunca aconteceu de verdade.

Este teste sela a paridade nos DOIS elos da corrente de persistência:
cada chave da aba precisa (1) de campo no SettingsSave (senão o PUT dropa)
e (2) de entrada no _UI_TO_ENV_MAP (senão persiste mas nunca aplica — #700).
"""

from app.core.config import PARAMETER_UI_KEYS, _UI_TO_ENV_MAP
from app.routes.dashboard import SettingsSave


def test_toda_chave_da_aba_parametros_tem_campo_no_settingssave():
    fields = set(SettingsSave.model_fields.keys())
    missing = [k for k in PARAMETER_UI_KEYS if k not in fields]
    assert not missing, (
        f"Chaves da aba Parâmetros SEM campo no SettingsSave {missing} — "
        "o PUT /settings descarta o valor em silêncio (pydantic extra=ignore) "
        "e o toast 'Parâmetros salvos' mente. Adicione o campo Optional no "
        "modelo (routes/dashboard.py)."
    )


def test_toda_chave_da_aba_parametros_esta_no_ui_to_env_map():
    missing = [k for k in PARAMETER_UI_KEYS if k not in _UI_TO_ENV_MAP]
    assert not missing, (
        f"Chaves da aba Parâmetros fora do _UI_TO_ENV_MAP {missing} — "
        "persistem no banco mas nunca aplicam em runtime (lição #700)."
    )
