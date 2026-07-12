"""Cockpit da Conversa — o painel de avaliação big-picture à direita do chat.

Uma conversa (à esquerda) ganha, à direita, uma leitura de QUALIDADE, CONFIABILIDADE
e CUSTO à luz do TCO — insumo para o Business Plan. Os indicadores EVOLUEM a cada
turno da sessão (recorte turno|sessão), 100% client-side sobre o que o /invoke/stream
já entrega em verbosity FULL (custo/tokens/evidência/FSM/juiz por step). Tudo tem
tooltip (title=) + um "?" que abre um modal com a REGRA considerada e aplicada.

Convenção do módulo: varredura de template (sem harness de DOM) — igual aos demais
testes do Playground. A reatividade em browser real é coberta pelos testes e2e.
"""
from pathlib import Path

PG = Path("app/templates/pages/mesh_playground.html")


def _src() -> str:
    return PG.read_text(encoding="utf-8")


def test_cockpit_layout_split_chat_esquerda_cockpit_direita():
    src = _src()
    # o modo Conversa deixa de ser coluna única (mx-auto max-w-3xl) e vira split:
    # grid com chat à esquerda e o cockpit (aside) à direita
    assert "mx-auto max-w-3xl px-4 lg:px-6 py-5 space-y-3" not in src   # o container antigo saiu
    assert "lg:grid-cols-[minmax(0,1fr)_minmax(0,400px)]" in src
    assert 'data-testid="pg-chat-left"' in src
    assert 'data-testid="pg-cockpit"' in src
    assert "<aside" in src and "lg:sticky" in src


def test_cockpit_roda_o_chat_em_full_para_ter_telemetria():
    """Deploy/summary corta custo/tokens/juiz no servidor; o chat passa a rodar em
    FULL (chatVerbosity) só p/ alimentar o cockpit — é o Playground (dono, X-API-Key)."""
    src = _src()
    assert "chatVerbosity: 'full'" in src
    assert "this._stream(this.selectedId, this.chatVerbosity, turn" in src


def test_cockpit_evolui_por_escopo_turno_e_sessao():
    """A evolução acumula por recorte: 'Este turno' = última troca; 'Sessão' = todos
    os turnos da conversa. Ambos derivam de turn.result.pipeline_steps[]."""
    src = _src()
    assert "cockpitScope: 'sessao'" in src
    assert 'data-testid="pg-cockpit-scope"' in src
    assert "cockpitScope='turno'" in src and "cockpitScope='sessao'" in src
    # os acumuladores por escopo + a fonte (pipeline_steps por turno)
    assert "_scopeTurns()" in src and "get cockpitTurns()" in src
    assert "t.result.pipeline_steps" in src
    # vazio antes do 1º turno + estado ativo
    assert 'data-testid="pg-cockpit-empty"' in src
    assert "get cockpitActive()" in src


def test_cockpit_tres_indices_e_gate():
    src = _src()
    # índices Q/R/C (0..100) com fórmulas nos getters
    assert "get idxQ()" in src and "get idxR()" in src and "get idxC()" in src
    assert 'data-testid="pg-cockpit-triad"' in src
    assert "gaugeStyle(idxQ)" in src and "gaugeStyle(idxR)" in src and "gaugeStyle(idxC)" in src
    # veredito Go/No-Go derivado dos índices
    assert "get ckGate()" in src
    assert 'data-testid="pg-cockpit-gate"' in src
    for k in ("Apto para produção", "Apto com ressalvas", "Reprovar"):
        assert k in src


def test_cockpit_tres_lentes_qualidade_confiabilidade_custo():
    src = _src()
    for tid in ("pg-cockpit-quality", "pg-cockpit-reliability", "pg-cockpit-cost"):
        assert f'data-testid="{tid}"' in src, f"falta a lente {tid}"
    # Qualidade: dimensões do juiz + RAGAS heurístico honesto (com o que falta)
    assert "get ckFact()" in src and "get ckCompl()" in src and "get ckTone()" in src and "get ckSafety()" in src
    assert "get ckRagas()" in src and "compute_heuristic_ragas" in src
    assert "groundedness" in src and "context recall" in src   # o que NÃO é calculado
    # degrada com honestidade quando o juiz não rodou
    assert "get ckHasJudge()" in src and "não medida" in src
    # Confiabilidade: FSM, contrato, alucinação, fundamentação, cobertura
    assert "get ckFsmLabel()" in src and "get ckContractRate()" in src
    assert "get ckHallucinations()" in src and "get ckGroundedRate()" in src and "get ckCoverage()" in src


def test_cockpit_custo_qac_e_escada_tco():
    src = _src()
    # custo/tokens/latência do recorte
    assert "get ckCostUsd()" in src and "get ckTokens()" in src and "get ckMs()" in src
    # a métrica de CFO: custo por resposta CONFIÁVEL (custo ÷ aprovação)
    assert "get qacBRL()" in src and "get approvalRate()" in src
    # escada de TCO: inferência + juiz(est.) + SaaS + infra(não medido) → banda mês/ano
    assert "get tcoInfMonth()" in src and "get tcoJudgeMonth()" in src and "get tcoSaasMonth()" in src
    assert "get tcoBandMonth()" in src and "get tcoBandYear()" in src
    assert "não medido" in src   # infra self-hosted
    # premissas EDITÁVEIS (BP): volume, câmbio, meta, SaaS
    for tid in ("pg-tco-volume", "pg-tco-fx", "pg-tco-alvo", "pg-tco-saas"):
        assert f'data-testid="{tid}"' in src, f"falta a entrada de TCO {tid}"


def test_cockpit_tooltip_e_help_modal_por_regra():
    """Requisito: tooltip em TUDO + um "?" perto de cada info que abre um modal com a
    regra CONSIDERADA e APLICADA (fórmula + proveniência + ressalva)."""
    src = _src()
    # o "?" e o modal da regra
    assert "openRule(" in src and "get ruleView()" in src
    assert 'data-testid="pg-cockpit-rule"' in src
    assert "Regra considerada e aplicada" in src
    assert "Proveniência" in src
    # o dicionário de regras cobre os indicadores-chave
    assert "RULES:" in src
    for key in ("idxQ", "idxR", "idxC", "gate", "factuality", "ragas",
                "contract", "hallucination", "coverage", "qac", "tco", "tco_juiz", "tco_infra"):
        assert f"openRule('{key}')" in src, f'falta o "?" da regra {key}'
        assert f"{key}:" in src, f"falta a regra {key} em RULES"
    # tooltips (title=) presentes nos indicadores
    assert 'title="Regra do índice de Qualidade"' in src
    assert 'title="Custo por resposta CONFIÁVEL = custo ÷ taxa de aprovação"' in src


def test_cockpit_selos_de_proveniencia():
    """Honestidade: nenhum número sem dizer COMO foi obtido — selos medido/heurístico/
    projetado/amostrado/declarado, e as ressalvas de custo (juiz/self-hosted)."""
    src = _src()
    for cls in ("ck-prov-medido", "ck-prov-heuristico", "ck-prov-projetado",
                "ck-prov-amostrado"):
        assert cls in src, f"falta o selo {cls}"
    # ressalvas materiais do TCO que evitam falsa confiança no BP
    assert "custo do juiz" in src and "estimado" in src
    assert "self-hosted conta 0" in src
