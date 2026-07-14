"""Arco LGPD residual (35.15.0) — as 3 lacunas que a auditoria #3 deixou para
decisão do dono (D/E/G), todas escolhidas para implementação:

- D: customer_hash POR-TURNO → forget alcança cada titular de uma sessão mista.
- E: job async de follow-up sem customer_ref HERDA o pivô da sessão reusada.
- G: forget/retenção apagam o BINÁRIO em data/uploads (elo arquivo→titular).
"""
import pytest
from pathlib import Path


# ─────────────────────────── D: pivô por-turno ────────────────────────────────

def test_turn_fragment_reflete_o_contextvar():
    from app.core import interaction_access as ia
    ia.set_interaction_customer_hash_for_creation("hABC")
    assert ia.turn_customer_hash_fragment() == {"customer_hash": "hABC"}
    ia.set_interaction_customer_hash_for_creation(None)
    assert ia.turn_customer_hash_fragment() == {}


def test_todos_os_turnos_carimbam_o_pivo():
    """Os 5 sites de criação de turno (FSM in/out, declarativo in/out, step de
    pipeline) incluem o fragmento por-turno — guarda contra um site esquecido."""
    fsm = Path("app/agents/state_machine.py").read_text(encoding="utf-8")
    eng = Path("app/agents/engine.py").read_text(encoding="utf-8")
    assert fsm.count("turn_customer_hash_fragment()") == 2   # entrada + saída
    assert eng.count("turn_customer_hash_fragment()") == 3   # decl in/out + step


class _ForgetCon:
    """Con que registra SQL e devolve rowcounts plausíveis (asyncpg-like)."""
    def __init__(self):
        self.calls = []

    async def fetch(self, sql, *a):
        self.calls.append((sql, a))
        return []  # sem interactions/arquivos → foca no caminho turn-level

    async def execute(self, sql, *a):
        self.calls.append((sql, a))
        if "DELETE FROM turns" in sql:
            return "DELETE 4"
        if "UPDATE verifications" in sql:
            return "UPDATE 2"
        return "DELETE 0"

    def sql(self, frag):
        return [c for c in self.calls if frag in c[0]]

    def transaction(self):
        con = self

        class _Tx:
            async def __aenter__(self):
                return con

            async def __aexit__(self, *a):
                return False
        return _Tx()


class _Pool:
    def __init__(self, con):
        self._con = con

    def acquire(self):
        con = self._con

        class _Ctx:
            async def __aenter__(self):
                return con

            async def __aexit__(self, *a):
                return False
        return _Ctx()


@pytest.mark.asyncio
async def test_forget_apaga_turns_do_titular_em_sessao_mista(monkeypatch):
    from app.core import retention
    con = _ForgetCon()
    monkeypatch.setattr("app.core.database._get_pool", lambda: _Pool(con))
    out = await retention.forget_customer("h-bob")
    # scrub das verifications por turn_id DESTE titular + delete dos turns
    scr = con.sql("WHERE turn_id IN (SELECT id FROM turns WHERE customer_hash")
    assert scr, "verifications dos turns do titular são scrubadas"
    dt = con.sql("DELETE FROM turns WHERE customer_hash")
    assert dt and dt[0][1][0] == "h-bob"
    assert out["turns_deleted"] == 4


class _MixedCon(_ForgetCon):
    """Sessão mista: o SELECT DISTINCT devolve a interaction do OUTRO titular."""
    async def fetch(self, sql, *a):
        self.calls.append((sql, a))
        if "SELECT DISTINCT interaction_id FROM turns" in sql:
            return [{"interaction_id": "int-de-alice"}]
        return []


@pytest.mark.asyncio
async def test_forget_sessao_mista_alcanca_pii_fora_de_turns(monkeypatch):
    """Achado do review adversarial do arco: o invoke do titular na sessão mista
    também gravou tool_calls/api_call_logs/binding_executions (PII crua, tabelas
    SEM turn_id) e evidences (com turn_id). O caminho turn-level alcança todas."""
    from app.core import retention
    con = _MixedCon()
    monkeypatch.setattr("app.core.database._get_pool", lambda: _Pool(con))
    await retention.forget_customer("h-bob")
    # evidences por turno (cirúrgico), ANTES do delete dos turns
    assert con.sql("DELETE FROM evidences WHERE turn_id IN")
    # PII sem turn_id → over-delete deliberado da interaction MISTA
    for tbl in ("api_call_logs", "tool_calls", "binding_executions"):
        hits = con.sql(f"DELETE FROM {tbl} WHERE interaction_id = ANY($1)")
        assert hits and hits[0][1][0] == ["int-de-alice"], f"{tbl} não alcançada"
    # a ordem importa: evidences (subselect de turns) vem antes do DELETE de turns
    idx_ev = next(i for i, c in enumerate(con.calls) if "DELETE FROM evidences" in c[0])
    idx_tn = next(i for i, c in enumerate(con.calls)
                  if "DELETE FROM turns WHERE customer_hash" in c[0])
    assert idx_ev < idx_tn


@pytest.mark.asyncio
async def test_unlink_preserva_linha_quando_falha(monkeypatch, tmp_path):
    """Achado do review: unlink que FALHA não pode perder a linha (único rastro
    do binário) — mantida para retry no próximo ciclo."""
    from app.core import retention
    from app.routes import workspace
    monkeypatch.setattr(workspace, "UPLOAD_DIR", tmp_path)
    ok = tmp_path / "ok.txt"
    ok.write_text("x")
    ruim = tmp_path / "ruim.txt"
    ruim.write_text("y")
    orig_unlink = Path.unlink

    def _boom(self, *a, **k):
        if self.name == "ruim.txt":
            raise OSError("em uso")
        return orig_unlink(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", _boom)
    con = _FileCon(["ok.txt", "ruim.txt"])
    removed = await retention._unlink_uploaded_files(con, customer_hash="h")
    assert removed == 1
    # o DELETE das linhas só leva o que foi removido/ausente — 'ruim' FICA
    dels = con.sql("DELETE FROM uploaded_files")
    assert dels and dels[0][1][0] == ["ok.txt"]


# ─────────────────────────── E: herança na sessão ─────────────────────────────

@pytest.mark.asyncio
async def test_customer_hash_of_interaction_le_a_coluna(monkeypatch):
    from app.core import interaction_access as ia

    class C:
        async def fetchval(self, sql, *a):
            assert "SELECT customer_hash FROM interactions" in sql
            return "h-da-sessao"
    monkeypatch.setattr("app.core.database._get_pool", lambda: _Pool(C()))
    assert await ia.customer_hash_of_interaction("s1") == "h-da-sessao"
    # sem id → None sem tocar o banco
    assert await ia.customer_hash_of_interaction(None) is None


def test_accept_async_herda_hash_da_sessao():
    rotas = Path("app/routes/pipelines.py").read_text(encoding="utf-8")
    assert "customer_hash_of_interaction(data.session_id)" in rotas
    assert "customer_hash=_effective_hash" in rotas


# ─────────────────────────── G: binário no disco ──────────────────────────────

class _FileCon:
    def __init__(self, disk_names):
        self._names = disk_names
        self.calls = []

    async def fetch(self, sql, *a):
        self.calls.append((sql, a))
        return [{"disk_name": n} for n in self._names]

    async def execute(self, sql, *a):
        self.calls.append((sql, a))
        return "DELETE 0"

    def sql(self, frag):
        return [c for c in self.calls if frag in c[0]]


@pytest.mark.asyncio
async def test_unlink_apaga_binario_e_linha(monkeypatch, tmp_path):
    from app.core import retention
    from app.routes import workspace
    # aponta UPLOAD_DIR para tmp e cria 2 arquivos
    monkeypatch.setattr(workspace, "UPLOAD_DIR", tmp_path)
    f1 = tmp_path / "aaa_doc.pdf"
    f2 = tmp_path / "bbb_nota.txt"
    f1.write_text("pii-do-titular")
    f2.write_text("mais-pii")
    con = _FileCon(["aaa_doc.pdf", "bbb_nota.txt"])
    removed = await retention._unlink_uploaded_files(con, customer_hash="h-titular")
    assert removed == 2
    assert not f1.exists() and not f2.exists()          # binários apagados
    assert con.sql("DELETE FROM uploaded_files")         # linha apagada


@pytest.mark.asyncio
async def test_unlink_anti_traversal(monkeypatch, tmp_path):
    """Um disk_name malicioso (path traversal) NÃO apaga fora de UPLOAD_DIR."""
    from app.core import retention
    from app.routes import workspace
    monkeypatch.setattr(workspace, "UPLOAD_DIR", tmp_path)
    outside = tmp_path.parent / "vitima.txt"
    outside.write_text("não apagar")
    con = _FileCon(["../vitima.txt"])
    removed = await retention._unlink_uploaded_files(con, customer_hash="h")
    # basename() neutraliza o traversal → procura tmp_path/vitima.txt (inexistente)
    assert removed == 0
    assert outside.exists()  # arquivo de fora intacto


def test_upload_registra_e_invoke_associa():
    ws = Path("app/routes/workspace.py").read_text(encoding="utf-8")
    assert "INSERT INTO uploaded_files (disk_name)" in ws
    rotas = Path("app/routes/pipelines.py").read_text(encoding="utf-8")
    assert "UPDATE uploaded_files SET customer_hash" in rotas
    ret = Path("app/core/retention.py").read_text(encoding="utf-8")
    # forget (por titular) e purga por idade ambos chamam o unlink
    assert "_unlink_uploaded_files(con, customer_hash=" in ret
    assert "_unlink_uploaded_files(con, older_than_days=" in ret
