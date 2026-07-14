"""Forget de SESSÃO MISTA contra Postgres REAL — fecha a classe "mock esconde".

O blocker da auditoria #5 (DELETE FROM evidences WHERE interaction_id, coluna
inexistente → UndefinedColumnError → rollback → PII sobrevivia) passou por TODA
a suíte unit porque as FakeCons só registram SQL, sem validar contra o schema.
Este teste exercita forget_customer com DADOS REAIS numa transação que dá
rollback no teardown — o Postgres valida colunas/tipos de verdade.

Cenário: sessão MISTA (call-center) — interação do cliente A (first-writer-wins)
com um turno posterior do cliente B. forget(B) deve apagar o rastro de B
(turno, evidências, telemetria) SEM destruir a conversa/interação de A.
"""
import pytest

from app.core.retention import forget_customer, hash_customer_ref, _SCRUB


class _ConCtx:
    def __init__(self, con):
        self._con = con

    async def __aenter__(self):
        return self._con

    async def __aexit__(self, *a):
        return False


class _PoolWrap:
    """Faz forget_customer rodar na MESMA connection do db_tx (rollback no fim)."""
    def __init__(self, con):
        self._con = con

    def acquire(self):
        return _ConCtx(self._con)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_sessao_mista_no_postgres_real(db_tx, monkeypatch):
    con = db_tx
    monkeypatch.setattr("app.core.database._get_pool", lambda: _PoolWrap(con))

    hA = hash_customer_ref("cliente-A")
    hB = hash_customer_ref("cliente-B")

    # Interação de A (customer_hash=A, first-writer-wins); title = mensagem CRUA
    await con.execute(
        "INSERT INTO interactions (id, title, customer_hash, owner_user_id, state) "
        "VALUES ('itx-mix', 'pergunta CRUA de B', $1, 'userA', 'Done')", hA)
    # Turno de A e turno de B na MESMA interação (sessão mista, pivô por-turno)
    await con.execute(
        "INSERT INTO turns (id, turn_number, user_text_redacted, interaction_id, "
        "customer_hash) VALUES ('t-a', 1, 'texto de A', 'itx-mix', $1)", hA)
    await con.execute(
        "INSERT INTO turns (id, turn_number, user_text_redacted, interaction_id, "
        "customer_hash) VALUES ('t-b', 2, 'texto CRU de B', 'itx-mix', $1)", hB)
    # evidências do turno de B (só turn_id — o BLOCKER: por interaction_id 500ava)
    await con.execute(
        "INSERT INTO evidences (id, snippet_text, turn_id) "
        "VALUES ('ev-b', 'snippet com PII de B', 't-b')")
    # telemetria por interaction_id (over-delete deliberado da sessão mista)
    await con.execute(
        "INSERT INTO api_call_logs (id, method, url, interaction_id, request_body) "
        "VALUES ('acl', 'GET', 'http://x', 'itx-mix', 'corpo com PII de B')")
    await con.execute(
        "INSERT INTO verifications (id, interaction_id, question_redacted, "
        "draft_redacted) VALUES ('vf', 'itx-mix', 'pergunta de B', 'resposta a B')")

    # ── ESQUECER o cliente B ──
    res = await forget_customer(hB)

    # turno de B some; turno de A e a INTERAÇÃO (de A) FICAM vivos
    assert await con.fetchval("SELECT count(*) FROM turns WHERE id='t-b'") == 0
    assert await con.fetchval("SELECT count(*) FROM turns WHERE id='t-a'") == 1
    assert await con.fetchval("SELECT count(*) FROM interactions WHERE id='itx-mix'") == 1
    # evidências de B apagadas POR turn_id (prova o fix do blocker — não 500ou)
    assert await con.fetchval("SELECT count(*) FROM evidences WHERE id='ev-b'") == 0
    # telemetria por interaction_id apagada (over-delete); verifications scrubada;
    # title (mensagem crua) scrubado
    assert await con.fetchval("SELECT count(*) FROM api_call_logs WHERE id='acl'") == 0
    assert await con.fetchval(
        "SELECT question_redacted FROM verifications WHERE id='vf'") == _SCRUB
    assert await con.fetchval("SELECT title FROM interactions WHERE id='itx-mix'") == _SCRUB
    assert res["turns_deleted"] >= 1

    # ── ESQUECER o cliente A: a interação inteira some (cascade leva t-a) ──
    res_a = await forget_customer(hA)
    assert await con.fetchval("SELECT count(*) FROM interactions WHERE id='itx-mix'") == 0
    assert await con.fetchval("SELECT count(*) FROM turns WHERE id='t-a'") == 0
    assert res_a["deleted"] >= 1
