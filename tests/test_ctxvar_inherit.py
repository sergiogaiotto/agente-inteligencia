"""Regressão do BLOCKER da auditoria de estado-integrado (35.14.5).

O set INCONDICIONAL do ContextVar de criação (35.14.4, M2 do #610) em
`execute_interaction` zerava o owner/customer_hash que `execute_pipeline`
propaga às filhas — master E filhas nasciam com owner=NULL e customer_hash=NULL
(regressão do IDOR #595 e do LGPD-2 #601 em TODO invoke de pipeline, o caminho
principal). O fix (35.14.5): um passo ANINHADO (`inherit_creation_context=True`,
só execute_pipeline o passa) NÃO reseta o ContextVar — herda o do pai.

Por que o teste antigo (`test_execute_pipeline_seta_o_contexto`) não pegou: ele
MOCKAVA execute_interaction e media o ContextVar no INSTANTE da chamada (ainda =
dono, pois quem zerava era o corpo real da filha, jamais executado). Falsa
confiança. Estes exercitam o `execute_interaction` REAL até logo após o bloco de
set (aborta em `_topo_agent` mockado → ValueError), medindo o efeito colateral
verdadeiro sobre o ContextVar.
"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock


@pytest.fixture(autouse=True)
def _limpa_contexto():
    # Os ContextVars de criação persistem na task; zera antes/depois p/ não
    # vazar entre casos (estado global entre testes é footgun recorrente).
    from app.core.interaction_access import (
        set_interaction_owner_for_creation,
        set_interaction_customer_hash_for_creation)
    set_interaction_owner_for_creation(None)
    set_interaction_customer_hash_for_creation(None)
    yield
    set_interaction_owner_for_creation(None)
    set_interaction_customer_hash_for_creation(None)


@pytest.mark.asyncio
async def test_filha_aninhada_nao_zera_o_contexto(monkeypatch):
    """inherit_creation_context=True: a filha NÃO toca o ContextVar — herda o
    dono/customer_hash que execute_pipeline setou. Sem isto (35.14.4),
    master+filhas nasciam órfãs (IDOR) e sem pivô LGPD."""
    import app.agents.engine as eng
    from app.core.interaction_access import (
        set_interaction_owner_for_creation, interaction_owner_for_creation,
        set_interaction_customer_hash_for_creation,
        interaction_customer_hash_for_creation)

    # Simula o set do execute_pipeline (pai) antes de chamar a filha.
    set_interaction_owner_for_creation("dono-123")
    set_interaction_customer_hash_for_creation("hash-abc")

    # Aborta logo após o bloco de set (agente inexistente → ValueError).
    monkeypatch.setattr(eng, "_topo_agent", AsyncMock(return_value=None))

    with pytest.raises(ValueError):
        await eng.execute_interaction(
            agent_id="X", user_input="oi",
            inherit_creation_context=True,  # filha aninhada: herda, não reseta
        )

    # O ContextVar do pai SOBREVIVEU — run_intake leria o dono/hash corretos.
    assert interaction_owner_for_creation() == "dono-123"
    assert interaction_customer_hash_for_creation() == "hash-abc"


@pytest.mark.asyncio
async def test_toplevel_reseta_o_contexto_herdado(monkeypatch):
    """Chamada TOP-LEVEL (default False): o set incondicional (M2, 35.14.4)
    segue valendo — None limpa a herança de uma operação anterior na MESMA task
    (harness/batch/A2A). A correção do blocker não pode reintroduzir a herança."""
    import app.agents.engine as eng
    from app.core.interaction_access import (
        set_interaction_owner_for_creation, interaction_owner_for_creation,
        set_interaction_customer_hash_for_creation,
        interaction_customer_hash_for_creation)

    # Estado "herdado" de uma operação anterior na mesma task.
    set_interaction_owner_for_creation("dono-anterior")
    set_interaction_customer_hash_for_creation("hash-anterior")

    monkeypatch.setattr(eng, "_topo_agent", AsyncMock(return_value=None))

    with pytest.raises(ValueError):
        await eng.execute_interaction(
            agent_id="X", user_input="oi",
            # sem inherit_creation_context (default False) → top-level → reseta
            owner_user_id=None, customer_ref=None,
        )

    # Top-level SEM dono/customer → o ContextVar foi ZERADO (não herda).
    assert interaction_owner_for_creation() is None
    assert interaction_customer_hash_for_creation() is None


@pytest.mark.asyncio
async def test_toplevel_com_dono_seta_o_dono(monkeypatch):
    """Top-level COM dono explícito (invoke de agente avulso por rota) carimba
    o dono no ContextVar — o caminho single-agent segue nascendo com dono."""
    import app.agents.engine as eng
    from app.core.interaction_access import interaction_owner_for_creation

    monkeypatch.setattr(eng, "_topo_agent", AsyncMock(return_value=None))
    with pytest.raises(ValueError):
        await eng.execute_interaction(
            agent_id="X", user_input="oi", owner_user_id="u-single")
    assert interaction_owner_for_creation() == "u-single"


def test_call_site_da_filha_usa_inherit():
    """A chamada de execute_interaction DENTRO de execute_pipeline passa
    inherit_creation_context=True, e o guard existe — guarda contra remoção
    acidental (o que reabriria o blocker silenciosamente)."""
    eng = Path("app/agents/engine.py").read_text(encoding="utf-8")
    assert "if not inherit_creation_context:" in eng
    # exatamente 1 CALL SITE aninhado passa o flag (vírgula colada distingue do
    # comentário, que cita `inherit_creation_context=True` entre crases).
    assert eng.count("inherit_creation_context=True,") == 1
