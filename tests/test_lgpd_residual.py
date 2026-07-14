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
async def test_forget_apaga_turns_do_titular(monkeypatch):
    from app.core import retention
    con = _ForgetCon()  # sem sessão mista (SELECT DISTINCT → [])
    monkeypatch.setattr("app.core.database._get_pool", lambda: _Pool(con))
    out = await retention.forget_customer("h-bob")
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
    """Achados do review (#614) + auditoria #4: na sessão mista o titular deixou
    PII em 5 tabelas por interaction_id (api_call_logs/tool_calls/binding_
    executions/verifier_jobs/evidences — verifications.turn_id é NULL no runtime,
    então scrub por interaction_id) + title/trace_data do master reusado."""
    from app.core import retention
    con = _MixedCon()
    monkeypatch.setattr("app.core.database._get_pool", lambda: _Pool(con))
    await retention.forget_customer("h-bob")
    # over-delete deliberado das 5 tabelas por interaction_id da sessão mista
    for tbl in ("api_call_logs", "tool_calls", "binding_executions",
                "verifier_jobs", "evidences"):
        hits = con.sql("DELETE FROM " + tbl + " WHERE interaction_id = ANY($1)")
        assert hits and hits[0][1][0] == ["int-de-alice"], f"{tbl} não alcançada"
    # verifications scrubadas por interaction_id (NÃO por turn_id, que é NULL)
    uv = con.sql("UPDATE verifications SET")
    assert uv and "interaction_id = ANY($1)" in uv[0][0]
    # title (mensagem crua) + trace_data do master reusado scrubados
    assert con.sql("UPDATE interactions SET title")


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


# ───────────────── Auditoria #4: fixes do estado integrado 35.15.1 ─────────────

def test_declarativo_de_agente_nasce_com_dono():
    """K (auditoria #4): o 3º branch (declarativo) de /agents/{id}/invoke seta o
    ContextVar de dono ANTES de execute_declarative, que o lê na criação."""
    ag = Path("app/routes/agents.py").read_text(encoding="utf-8")
    assert "set_interaction_owner_for_creation(_caller.get(\"id\"))" in ag
    de = Path("app/agents/declarative_engine.py").read_text(encoding="utf-8")
    assert "interaction_owner_for_creation()" in de
    assert '**({"owner_user_id": _downer} if _downer else {})' in de


def test_worker_reherda_hash_em_runtime():
    """M (auditoria #4): TOCTOU — o worker re-resolve o customer_hash da sessão
    quando o job nasceu NULL (herança no aceite perdeu a corrida) e PERSISTE."""
    src = Path("app/core/invoke_jobs.py").read_text(encoding="utf-8")
    assert "customer_hash_of_interaction(req.get(\"session_id\"))" in src
    assert "UPDATE invoke_jobs SET customer_hash = $1" in src
    assert "customer_hash=_job_chash" in src  # passado ao engine


def test_unlink_roda_em_thread_e_forget_tem_limit():
    """L (auditoria #4): I/O de filesystem fora do event loop + LIMIT no forget."""
    ret = Path("app/core/retention.py").read_text(encoding="utf-8")
    assert "await asyncio.to_thread(_unlink_batch)" in ret
    # ramo por titular (forget) agora tem LIMIT como o ramo por idade
    assert "WHERE customer_hash = $1 " in ret and "LIMIT $2" in ret


def test_endpoint_forget_expoe_contadores():
    """N (auditoria #4): /privacy/forget devolve E audita turns/jobs/files."""
    pv = Path("app/routes/privacy.py").read_text(encoding="utf-8")
    for k in ("turns_deleted", "invoke_jobs_deleted", "files_deleted"):
        assert pv.count(k) >= 2, f"{k} deve estar no audit E na resposta"
