"""DLP pré-LLM (65.0.0) — wire da flag `dlp_redact_before_llm` no engine.

Achado HIGH da revisão adversarial do 64.2.0: a flag existia em config/UI mas
não tinha NENHUM consumidor no runtime. Agora, com dlp_enabled +
dlp_redact_before_llm, o conteúdo do turno que sai ao provedor LLM (mensagem +
anexos + evidências) e a pergunta passada ao juiz vão com PII redigida.

Garantia de não-impacto: o gate exige as DUAS flags; com qualquer uma OFF
(default de dlp_redact_before_llm = False) o caminho é byte-idêntico ao
anterior — coberto aqui + guarda estrutural contra revert do wire.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_ENGINE = Path(__file__).resolve().parent.parent / "app" / "agents" / "engine.py"


class _S:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class TestGate:
    def test_exige_as_duas_flags(self):
        from app.agents.engine import _pre_llm_dlp_enabled
        assert _pre_llm_dlp_enabled(_S(dlp_enabled=True, dlp_redact_before_llm=True)) is True
        assert _pre_llm_dlp_enabled(_S(dlp_enabled=True, dlp_redact_before_llm=False)) is False
        assert _pre_llm_dlp_enabled(_S(dlp_enabled=False, dlp_redact_before_llm=True)) is False
        assert _pre_llm_dlp_enabled(_S(dlp_enabled=False, dlp_redact_before_llm=False)) is False

    def test_atributo_ausente_conta_como_off(self):
        # settings sem o atributo (fakes/versões antigas) → comportamento
        # pré-existente, nunca redação surpresa.
        from app.agents.engine import _pre_llm_dlp_enabled
        assert _pre_llm_dlp_enabled(_S()) is False
        assert _pre_llm_dlp_enabled(_S(dlp_enabled=True)) is False


@dataclass
class _Ev:
    snippet_text: str = ""
    relevance_score: float = 0.5
    metadata: dict = field(default_factory=dict)


class TestRedactEvidences:
    def test_redige_dataclass_in_place_e_conta(self):
        from app.agents.engine import _redact_evidences_for_llm
        evs = [_Ev(snippet_text="Cliente CPF 123.456.789-00, e-mail ana@ex.com")]
        n = _redact_evidences_for_llm(evs)
        assert n == 2  # telemetria honesta: CPF + EMAIL contados na fonte
        assert "[CPF]" in evs[0].snippet_text and "[EMAIL]" in evs[0].snippet_text
        assert "123.456.789-00" not in evs[0].snippet_text
        assert "ana@ex.com" not in evs[0].snippet_text

    def test_redige_dict_e_tolera_vazios(self):
        from app.agents.engine import _redact_evidences_for_llm
        evs = [{"snippet_text": "fone (11) 99999-8888"}, {"snippet_text": ""}, {"outro": 1}]
        assert _redact_evidences_for_llm(evs) == 1
        assert evs[0]["snippet_text"] == "fone [PHONE]"
        assert evs[1]["snippet_text"] == ""

    def test_sem_pii_texto_intacto(self):
        from app.agents.engine import _redact_evidences_for_llm
        evs = [_Ev(snippet_text="Garantia de 12 meses para o produto X.")]
        assert _redact_evidences_for_llm(evs) == 0
        assert evs[0].snippet_text == "Garantia de 12 meses para o produto X."

    def test_lista_vazia_e_none(self):
        from app.agents.engine import _redact_evidences_for_llm
        assert _redact_evidences_for_llm([]) == 0
        assert _redact_evidences_for_llm(None) == 0


class TestWireEstrutural:
    """Guarda contra revert: o wire no fluxo do execute_interaction precisa
    existir (mesma classe da guarda de call sites do retriever no Evidence ACL)."""

    def test_engine_tem_o_wire_completo(self):
        src = _ENGINE.read_text(encoding="utf-8")
        # gate decidido 1x por interação
        assert "_dlp_pre_llm = _pre_llm_dlp_enabled(_pg_settings)" in src
        # evidências redigidas na fonte ANTES do rerank (o reranker default é
        # chamada LLM real — achado HIGH), com contagem p/ telemetria
        assert "_pii_ev_n = _redact_evidences_for_llm(evidences)" in src
        # a query do rerank também sai redigida (os DOIS lados consistentes)
        assert "_rerank_query = _dlp_redact_q(_search_query)" in src
        assert "reranker.rerank(_rerank_query" in src
        # histórico injetado: cinto-e-suspensório sobre as colunas *_redacted
        assert "_hm.content = _dlp_redact_h(_hm.content)" in src
        # turno redigido antes do provedor + observabilidade honesta
        assert 'ctx.metadata["dlp_pre_llm"]' in src
        assert '"redactions": _pii_ev_n + _pii_n' in src
        # a pergunta do juiz é REDIGIDA (pin da linha — sem ele um refactor
        # apagaria só esta linha com a suíte verde) e chega redigida em TODOS
        # os call sites (4) — nenhum verify/dispatch volta a passar cru.
        assert "_vf_question = _dlp_redact(user_input)" in src
        assert src.count("user_question=_vf_question") == 4
        assert "user_question=user_input" not in src

    def test_guard_ve_texto_cru(self):
        # a redação NÃO pode acontecer antes do prompt guard (mascarar PII não
        # pode reduzir a detecção de injeção): o detect roda sobre user_input e
        # a redação só roda depois, sobre enriched_input.
        src = _ENGINE.read_text(encoding="utf-8")
        i_guard = src.index("_pg_detect(")
        i_redact = src.index("enriched_input = _dlp_redact(enriched_input)")
        assert i_guard < i_redact

    def test_flag_off_nao_toca_enriched_input(self):
        # CONTAINMENT por indentação (achado de revisão: ordem textual passava
        # com a redação movida pra fora do if): a redação do turno vive DENTRO
        # do `if _dlp_pre_llm:` — 4 espaços no if (nível da função), 8 no corpo.
        # Se alguém mover a linha pra fora do gate, a indentação muda e este
        # teste quebra — protege o requisito nº1 (OFF byte-idêntico).
        src = _ENGINE.read_text(encoding="utf-8")
        assert "\n    if _dlp_pre_llm:\n        from app.core.dlp import count_pii" in src
        assert "\n        enriched_input = _dlp_redact(enriched_input)\n" in src
        assert "\n    enriched_input = _dlp_redact" not in src  # nunca no nível da função
        # mesma contenção nos outros dois pontos do wire:
        assert "\n        if _dlp_pre_llm and evidences:\n" in src          # rerank (dentro do else de evidência)
        assert "\n    if _dlp_pre_llm and history_messages:\n" in src       # histórico (nível da função, gated)

    def test_workspace_writers_redigem_colunas_redacted(self):
        # Achado MEDIUM: workspace gravava texto CRU em *_redacted → o
        # histórico injetado no prompt carregava PII crua. Guarda de call site:
        # TODO write dessas colunas em app/ precisa passar por *_redact(...).
        import re
        app_dir = Path(__file__).resolve().parent.parent / "app"
        bad = []
        for py in app_dir.rglob("*.py"):
            txt = py.read_text(encoding="utf-8")
            for m in re.finditer(r'"(?:user|output)_text_redacted":\s*([^,\n]+)', txt):
                val = m.group(1).strip()
                if "_redact(" not in val:
                    bad.append(f"{py.name}: {m.group(0)}")
        assert not bad, f"write cru em coluna *_redacted (vaza PII pro histórico/UI): {bad}"


class TestTextosHonestos:
    def test_docstring_dlp_atualizada(self):
        dlp = (Path(__file__).resolve().parent.parent / "app" / "core" / "dlp.py").read_text(encoding="utf-8")
        assert "TRÊS pontos" in dlp
        assert "Não redacta antes do LLM por padrão" not in dlp

    def test_docs_configuracoes_afirmam_o_wire(self):
        doc = (Path(__file__).resolve().parent.parent / "docs" / "configuracoes-plataforma.md").read_text(encoding="utf-8")
        assert "envia ao provedor LLM" in doc
