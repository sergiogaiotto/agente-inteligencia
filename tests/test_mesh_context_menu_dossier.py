"""Fase 1 do Fluxo de agentes (QA E2E): Menu de Regência + Dossiê do Agente.

Varredura de template (padrão do projeto — ver test_cond_decisions_ui): garante
a presença e as amarras das peças; o comportamento Alpine em runtime é
verificado no smoke de browser real.

O pedido: botão direito nos nós/conexões deixava o menu NATIVO do browser
(inútil no contexto); o painel direito não mostrava a Skill; skill/prompt não
expandiam para leitura; e o cursor não indicava clicáveis.
"""
from pathlib import Path

SRC = (Path(__file__).parent.parent / "app" / "templates" / "pages"
       / "mesh_flow.html").read_text(encoding="utf-8")


class TestMenuDeRegencia:
    def test_menu_existe_e_fecha_por_todos_os_caminhos(self):
        assert 'data-testid="mesh-ctxmenu"' in SRC
        i = SRC.index('data-testid="mesh-ctxmenu"')
        bloco = SRC[i - 400: i + 200]
        assert '@click.away="ctxMenu=null"' in bloco
        assert '@keydown.escape.window="ctxMenu=null"' in bloco
        # menu é fixed e NÃO acompanha pan/zoom → pointerdown do canvas fecha
        assert "onCanvasDown(ev) { this.ctxMenu = null;" in SRC

    def test_no_real_ganha_menu_e_lens_start_mantem_o_seu(self):
        i = SRC.index("onNodeContext(ev, n)")
        fn = SRC[i: i + 900]
        assert "__lens_start__" in fn and "this.startMenu =" in fn   # branch antigo intacto
        assert "!n.synthetic" in fn and "kind: 'node'" in fn         # branch novo

    def test_botao_direito_nao_inicia_drag_fantasma(self):
        """onNodeDown sem guard de botão fazia o nó GRUDAR no mouse ao abrir o menu."""
        i = SRC.index("onNodeDown(ev, n)")
        assert "ev.button !== 0" in SRC[i: i + 120]

    def test_aresta_usa_a_hitbox_gorda_por_data_eid(self):
        assert '@contextmenu.prevent="onEdgeContext($event)"' in SRC
        i = SRC.index("onEdgeContext(ev)")
        assert "data-eid" in SRC[i: i + 300]

    def test_acoes_reusam_funcoes_existentes(self):
        """Nada de segundo caminho de DELETE/entry: o menu delega ao que já existe."""
        i = SRC.index('data-testid="mesh-ctxmenu"')
        menu = SRC[i: i + 4200]
        assert "setEntry(ctxMenu.node.id)" in menu
        assert "editEdge(ctxMenu.edgeId)" in menu
        assert "deleteConn()" in menu
        assert "toggleIsolate(ctxMenu.node.id)" in menu

    def test_menu_nao_estoura_a_viewport(self):
        assert "window.innerWidth-270" in SRC and "window.innerHeight-320" in SRC


class TestDossie:
    def test_secoes_e_estado(self):
        assert 'data-testid="mesh-dossier"' in SRC
        assert "dossier: { skill: true, prompt: true, runs: false }" in SRC

    def test_skill_carrega_em_cascata_do_agente(self):
        i = SRC.index("loadAgentDetail(agentId)")
        fn = SRC[i: i + 700]
        assert "this.loadSkillDetail(d.skill_id)" in fn

    def test_loads_tem_guarda_de_selecao_corrente(self):
        """Resposta atrasada de um nó anterior não pode sobrescrever o dossiê."""
        for fn_name in ("loadSkillDetail(skillId)", "loadInvocations(agentId)"):
            i = SRC.index(fn_name)
            fn = SRC[i: i + 700]
            assert "this.selected" in fn and "===" in fn, f"{fn_name} sem guarda"

    def test_select_node_zera_o_dossie_anterior(self):
        i = SRC.index("selectNode(n) {")
        fn = SRC[i: i + 700]
        for campo in ("skillDetail", "invocations", "docPanel"):
            assert campo in fn, f"selectNode não zera {campo}"

    def test_leitor_expandido_sanitiza_markdown(self):
        """raw_content é conteúdo de usuário: x-html SÓ via _md (DOMPurify)."""
        assert 'data-testid="mesh-doc-panel"' in SRC
        assert 'x-html="_md(docPanel.content)"' in SRC
        i = SRC.index("_md(text)")
        fn = SRC[i: i + 900]
        assert "DOMPurify.sanitize" in fn
        assert "ALLOWED_TAGS" in fn

    def test_invocacoes_usam_o_shape_real_da_rota(self):
        """A rota devolve `state` (não final_state) e `title`/`created_at`."""
        i = SRC.index('data-testid="mesh-dossier"')
        bloco = SRC[i: i + 9000]
        assert "iv.state === 'Recommend'" in bloco
        assert "iv.final_state" not in bloco
        assert "tzTime(iv.created_at)" in bloco   # NUNCA fatiar ISO na mão


class TestIsolarVizinhanca:
    def test_toggle_usa_view_edges_e_ignora_start(self):
        i = SRC.index("toggleIsolate(id)")
        fn = SRC[i: i + 600]
        assert "this.viewEdges" in fn, "edges cru ignora a lente do pipeline"
        assert "e.type === 'start'" in fn

    def test_dim_aplica_em_nos_e_arestas(self):
        assert "this.dimSet && !n.synthetic && !this.dimSet.has(n.id)" in SRC
        assert "this.dimSet.has(e.source) && this.dimSet.has(e.target)" in SRC

    def test_pill_de_saida_visivel(self):
        """Sem indicador, o usuário acha que o grafo 'sumiu'."""
        assert 'data-testid="mesh-isolate-pill"' in SRC
        i = SRC.index('data-testid="mesh-isolate-pill"')
        assert "clearIsolate()" in SRC[i - 300: i + 300]

    def test_troca_de_lente_limpa_isolamento_e_menu(self):
        i = SRC.index("selectPipeline(p) {")
        fn = SRC[i: i + 400]
        assert "clearIsolate()" in fn and "ctxMenu = null" in fn
