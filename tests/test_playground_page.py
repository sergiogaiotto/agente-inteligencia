"""Playground — console de API tipo AI Studio (submenu de AI Mesh).

Testa um pipeline COMO O APP VERIA: roda o endpoint real via X-API-Key (omitindo
o cookie → o servidor projeta a resposta de integração), com streaming ao vivo e o
código pronto (curl/Python/JS). Reusa a estação de chave + verbosidade + streaming.

Convenção: varredura de template (sem harness de DOM).
"""
from pathlib import Path

from app.routes.frontend import PAGES
from app.routes import frontend as fe

BASE = Path("app/templates/layouts/base.html")
PG = Path("app/templates/pages/mesh_playground.html")


def test_pagina_registrada_e_no_nav():
    assert PAGES.get("/mesh/playground", {}).get("template") == "pages/mesh_playground.html"
    assert PAGES["/mesh/playground"]["section"] == "mesh"
    assert hasattr(fe, "pg_mesh_playground")
    base = BASE.read_text(encoding="utf-8")
    assert 'href="/mesh/playground"' in base           # link no submenu AI Mesh
    # _pagePathMap (mapa rota→key da Ajuda): playground tem ajuda PRÓPRIA (era 'mesh',
    # mostrava a ajuda do Fluxo — corrigido ao adicionar o entry/botão "?" do Playground).
    assert "'/mesh/playground': 'playground'" in base


def test_bloco_ai_mesh_fecha_certo():
    """Regressão: contagem TOTAL de <div> balanceada NÃO pega mis-nesting. O bloco
    do AI Mesh (tour-nav-mesh) tinha um </div> a mais que fechava o submenu cedo e
    escondia o resto da sidebar SÓ na página do Playground. Trava o balanço do BLOCO.
    (perdido 2x no squash-drop do #436 — por isso o teste do bloco, não só o total.)"""
    import re
    base = BASE.read_text(encoding="utf-8")
    a = base.rfind("<div", 0, base.index('id="tour-nav-mesh"'))
    b = base.rfind("<div", 0, base.index('id="tour-nav-tools"'))
    block = base[a:b]
    opens = len(re.findall(r"<div(?:\s|>)", block))
    closes = len(re.findall(r"</div>", block))
    assert opens == closes, f"bloco AI Mesh desbalanceado: {opens} abrem vs {closes} fecham (fecha o nav cedo)"
    # e os itens DEPOIS do AI Mesh continuam no nav
    assert base.index('href="/mesh/playground"') < base.index("Ferramentas")
    assert 'href="/mcp"' in base and 'href="/settings"' in base


def test_console_roda_como_integracao():
    src = PG.read_text(encoding="utf-8")
    assert "playgroundPage()" in src
    # fidelidade: roda o /invoke/stream via X-API-Key OMITINDO o cookie
    assert "/invoke/stream" in src
    assert "credentials: 'omit'" in src
    assert "'X-API-Key'" in src
    # reusa a estação de chave (gerar e embutir)
    assert "...curlAuthStation()" in src
    assert "generateAndEmbed()" in src


def test_console_tem_streaming_e_resposta():
    src = PG.read_text(encoding="utf-8")
    assert 'data-testid="pg-live"' in src   # passo-a-passo ao vivo
    assert "_ev(" in src                    # parser SSE
    assert "outCards()" in src              # resposta elegante (cartões)
    assert 'data-testid="pg-result"' in src


def test_console_tem_codegen_3_linguagens():
    src = PG.read_text(encoding="utf-8")
    assert "snippet()" in src
    # curl + Python (requests) + JS (fetch)
    assert "import requests" in src
    assert "await fetch(" in src
    assert "-X POST" in src   # curl (montado por shell em _cgCurl)
    # abas de linguagem
    assert "LANGS:" in src


def test_codegen_multi_sdk_e_streaming():
    """Feature 3: codegen para +SDKs (go/php/ruby/csharp/java/httpx/axios) e a
    variante STREAMING (consumir o SSE de /invoke/stream) — frontend-only."""
    src = PG.read_text(encoding="utf-8")
    # spec único + 1 formatador por linguagem
    assert "_reqSpec()" in src
    for lang in ("'py-httpx'", "'node-axios'", "'go'", "'php'", "'ruby'", "'csharp'", "'java'"):
        assert lang in src, f"falta a linguagem {lang} no dispatch/LANGS"
    # formatadores idiomáticos presentes
    assert "_cgGo(" in src and "net/http" in src
    assert "_cgPhp(" in src and "curl_init(" in src
    assert "_cgRuby(" in src and "Net::HTTP" in src
    assert "_cgCsharp(" in src and "HttpClient" in src
    assert "_cgJava(" in src and "BodyPublishers.ofString" in src
    assert "_cgHttpx(" in src and "httpx.stream(" in src
    assert "_cgAxios(" in src and 'responseType: "stream"' in src
    # toggle sync|streaming + consumo de SSE (o diferencial)
    assert "codeMode" in src and 'data-testid="pg-code-mode"' in src
    assert "/invoke/stream" in src                        # endpoint de streaming no spec
    assert "text/event-stream" in src                     # header Accept nos snippets de stream
    assert "'-N '" in src                                 # flag de streaming do curl
    assert "getReader()" in src                           # JS fetch stream
    assert "iter_lines" in src                            # Python stream
    assert "BodyHandlers.ofLines" in src                  # Java stream


def test_curl_tem_opcoes_de_notacao_por_shell():
    """Quando curl é escolhido, aparecem as opções de NOTAÇÃO (Bash/PowerShell/CMD).
    A sintaxe do curl muda por shell: continuação de linha + aspas/escape. No
    PowerShell `curl` é alias de Invoke-WebRequest → o snippet usa `curl.exe`."""
    src = PG.read_text(encoding="utf-8")
    # estado + toggle visível só no curl
    assert "curlShell" in src
    assert 'data-testid="pg-curl-shell"' in src
    assert 'x-show="lang === \'curl\'"' in src
    # os três alvos
    assert "Bash (Linux/macOS)" in src and "PowerShell" in src and "CMD (Windows)" in src
    assert "curlShell='bash'" in src and "curlShell='powershell'" in src and "curlShell='cmd'" in src
    # mecânica por shell: curl.exe (PS) + escaping próprio (_psq dobra a aspa simples)
    assert "curl.exe" in src
    assert "_psq(" in src and "s.replace(/'/g, \"''\")" in src


def test_console_tem_abas_tempo_e_trace():
    src = PG.read_text(encoding="utf-8")
    # abas novas
    assert 'data-testid="pg-tab-tempo"' in src and 'data-testid="pg-tab-trace"' in src
    assert 'data-testid="pg-tempo"' in src and 'data-testid="pg-trace"' in src
    # Tempo: waterfall do timing do stream + totais
    assert "get waterfall()" in src and "performance.now()" in src
    assert "get totalCost()" in src
    # Trace: lê o trace da resposta FULL (custo/sql/evidência) — só Debug
    assert "get traceItems()" in src
    assert "sql_rendered" in src and "evidence_score" in src
    # custo/SQL só em Debug (fullSteps = pipeline_steps, presente só no full)
    assert "get fullSteps()" in src
    assert "só aparece em <strong>Debug</strong>" in src


def test_trace_recolhe_expande_com_tooltips():
    src = PG.read_text(encoding="utf-8")
    # recolher/expandir por agente
    assert "expanded[i] = !expanded[i]" in src
    assert 'x-show="expanded[i]"' in src
    # tooltips de avaliação (title=) nos termos que precisam de explicação
    assert "Pontuação de evidência" in src
    assert "máquina de decisão" in src


def test_console_tem_aba_http_e_mapa_de_erros():
    src = PG.read_text(encoding="utf-8")
    assert 'data-testid="pg-tab-http"' in src and 'data-testid="pg-http"' in src
    # status + rate-limit lidos dos headers REAIS da resposta (o stream escreve no sink)
    assert "X-RateLimit-Remaining" in src and "http = {" in src
    # mapa de erros: 401/400/404 simuláveis + 409/422/429 na referência
    assert "ERRORS:" in src and "async testError(code)" in src
    for c in ("401", "400", "404", "409", "422", "429"):
        assert c in src
    assert "testError(e.code)" in src
    # Regressão (bug do "testar" que nunca disparava): o :disabled do botão precisa
    # ser um BOOLEAN estrito. `errTests[e.code] && errTests[e.code].loading` retorna
    # `undefined` quando não há entrada — e o Alpine 3 renderiza um valor `undefined`
    # de atributo booleano como PRESENTE (botão fica disabled p/ sempre, clique no-op).
    # O `!!(...)` força false no estado ocioso. Confirmado em browser real (Playwright).
    assert "!!(errTests[e.code] && errTests[e.code].loading)" in src


def test_console_tem_historico_repl():
    src = PG.read_text(encoding="utf-8")
    assert 'data-testid="pg-history"' in src
    assert "_pushHistory()" in src
    assert "restore(h)" in src and "re-rodar" in src and "clearHistory()" in src
    # REPL persiste no navegador (sobrevive ao reload)
    assert "localStorage.setItem('pg_history'" in src and "_loadHistory()" in src


def test_historico_persiste_no_servidor():
    """Feature 1: o histórico agora é PERSISTIDO no servidor (por-usuário), com o
    localStorage como cache offline. A página chama o CRUD de /playground/runs."""
    src = PG.read_text(encoding="utf-8")
    # POST otimista + GET no load + DELETE (tudo / por item)
    assert "api.post('/api/v1/playground/runs'" in src
    assert "api.get('/api/v1/playground/runs" in src
    assert "api.del('/api/v1/playground/runs'" in src           # limpar tudo
    assert "api.del('/api/v1/playground/runs/'" in src          # remover um
    # métodos novos do ciclo servidor-backed
    assert "_persistRun(" in src and "_mapRun(" in src and "removeRun(h)" in src
    # cache offline preservado (sobrevive offline) + tz-correto no carimbo do servidor
    assert "localStorage.setItem('pg_history'" in src
    assert "window.tzTime(r.created_at)" in src
    # init carrega do servidor ao abrir (await: GET resolve antes de qualquer push)
    assert "await this._loadHistory()" in src
    # carimbo otimista também via tzTime (não toLocaleTimeString().slice → '3:05:' em en-US)
    assert "window.tzTime(new Date().toISOString())" in src
    # x-for keyed numa chave ESTÁVEL (não muda na reconciliação id local→servidor)
    assert ':key="h.key"' in src


def test_historico_restaura_thread_completa():
    """Clicar numa linha restaura a EXECUÇÃO inteira (Resposta/Tempo/Trace/HTTP) sem
    re-rodar: thread em memória (sessão) ou GET /runs/{id} (servidor/outra máquina)."""
    src = PG.read_text(encoding="utf-8")
    # a thread (result+timings+http) é empurrada no push e enviada no POST
    assert "thread: { result: this.result, timings: this.timings, http: this.http }" in src
    assert "duration_ms: e.totalMs, thread: e.thread" in src
    # restore: usa a thread em memória OU busca no servidor; reidrata os painéis
    assert "async restore(h)" in src
    assert "api.get('/api/v1/playground/runs/' + h.id)" in src
    assert "this.result = thread.result" in src and "this.timings = thread.timings" in src and "this.http = thread.http" in src
    # "re-rodar" só aplica a requisição (não busca a thread); restore != re-rodar
    assert "_applyRequest(h)" in src
    assert "_applyRequest(h); run()" in src
    # localStorage segue LEVE: a thread (grande) é removida antes de serializar
    assert "this.history.map(({ thread, ...c }) => c)" in src
    # restore sai do modo comparar (senão escreveria nos painéis escondidos por !compareMode)
    assert "this.compareMode = false; this.tab = 'resp'" in src


def test_compara_dois_pipelines_lado_a_lado():
    """Feature 2: comparar A/B — mesma entrada, 2 execuções reais lado a lado,
    com deltas (tempo/custo/tamanho/igualdade). Reusa o /invoke/stream (sem backend)."""
    src = PG.read_text(encoding="utf-8")
    # toggle + 2º destino + 2 modos (2 pipelines | mesmo pipeline 2 detalhes)
    assert 'data-testid="pg-compare-toggle"' in src
    assert 'data-testid="pg-pipeline-b"' in src
    assert "compareMode" in src and "compareKind" in src and "verbosityB" in src
    # núcleo de streaming reaproveitável (sink-aware) + slots A/B
    assert "async _stream(pipelineId, verbosity, sink)" in src
    assert "_ev(buf.slice(0, i), sink)" in src       # parser SSE escreve no sink
    assert "async runCompare()" in src and "_runSlot(" in src
    assert "Promise.all([this._runSlot(this.cmp.A), this._runSlot(this.cmp.B)])" in src
    # 2 execuções reais = 2× custo (avisado) — não é projeção client-side
    assert "2× custo de LLM" in src
    # painel 2 colunas + deltas + helpers por-bucket
    assert 'data-testid="pg-compare"' in src and 'data-testid="pg-deltas"' in src
    assert "[cmp.A, cmp.B]" in src
    assert "get deltas()" in src and "sameOutput" in src
    assert "_outCards(slot)" in src and "_totalMs(slot)" in src and "_totalCost(slot)" in src
    # botão despacha por modo; disponibilidade via canRun
    assert "compareMode ? runCompare() : run()" in src
    assert "get canRun()" in src
    # guarda contra comparar A com A (gasta 2× LLM por um delta de ruído)
    assert "this.pipelineB !== this.selectedId" in src
    assert "get compareDegenerate()" in src and 'data-testid="pg-compare-degenerate"' in src
    # badge do slot mostra o rótulo amigável (Deploy/Debug/Só resposta), não a chave crua
    assert "vName(slot.verbosity)" in src


def test_anexos_no_playground():
    """O Playground aceita anexos (como o app real): upload via /workspace/upload e
    envio no corpo do invoke — o engine roteia cada arquivo aos agentes que aceitam."""
    src = PG.read_text(encoding="utf-8")
    # estado + UI de anexos (input file + chips + remover)
    assert "attachments: [], uploading: false" in src
    assert 'data-testid="pg-attach"' in src and 'data-testid="pg-attachments"' in src
    assert 'x-ref="pgFiles"' in src and "uploadFiles($event.target.files)" in src
    assert "attachments.splice(i,1)" in src
    # upload reusa o /workspace/upload (cookie); o invoke segue fiel (X-API-Key)
    assert "async uploadFiles(fileList)" in src
    assert "/api/v1/workspace/upload" in src
    # anexos vão no CORPO do invoke quando presentes
    assert "if (this.attachments.length) _body.attachments = this.attachments" in src
    # aviso honesto de tipos suportados
    assert "Imagens vão a agentes multimodais; documentos viram texto" in src


def test_helper_inputs_esperados_e_template():
    """Helper inline: descobre os inputs esperados do pipeline (agente-raiz) e gera
    um template de payload — em vez de adivinhar o que mandar na Mensagem."""
    src = PG.read_text(encoding="utf-8")
    # dois botões ao lado da Mensagem + painel de inputs
    assert 'data-testid="pg-inputs"' in src and 'data-testid="pg-template"' in src
    assert 'data-testid="pg-inputs-panel"' in src
    assert "verInputs()" in src and "inserirTemplate()" in src
    # introspecção via o endpoint do pipeline (resolve a raiz no backend)
    assert "/api/v1/pipelines/' + this.selectedId + '/inputs-schema'" in src
    assert "get inputFields()" in src and "_buildTemplate()" in src
    # reset do helper centralizado: vale p/ @change do select E p/ restore/re-rodar
    # (troca programática de selectedId não dispara o @change → painel ficaria preso)
    assert "_resetInputsHelper()" in src
    assert "this.verbosity = h.verbosity; this._resetInputsHelper()" in src
    # guards defensivos: required/properties malformados não podem quebrar o getter
    assert "Array.isArray(isch.required)" in src


def test_form_de_args_estruturados():
    """D3: o painel de inputs vira FORMULÁRIO tipado (1 widget por campo do
    ## Inputs do agente-raiz). Os valores viram o objeto `args` do invoke
    (validado no servidor), sem JSON na mão. Validação no cliente espelha o 422."""
    src = PG.read_text(encoding="utf-8")
    # estado + form com widgets por campo
    assert "argValues: {}" in src
    assert 'data-testid="pg-args-form"' in src and 'data-testid="pg-arg-field"' in src
    # widget escolhe por tipo: enum→select, boolean→select, number→input number
    assert "f.enum && f.enum.length" in src
    assert "f.type === 'boolean'" in src
    assert "f.type === 'integer' || f.type === 'number'" in src
    # inputFields expõe enum p/ o dropdown
    assert "enum: Array.isArray(s.enum) ? s.enum : null" in src
    # payload pruned/coagido + getters de validação
    assert "get argsPayload()" in src and "get hasArgs()" in src
    assert "get argIssues()" in src and "get hasArgErrors()" in src and "argFieldError(f)" in src
    # args vão no corpo do invoke E no codegen (via bodyObj)
    assert "if (this.hasArgs) _body.args = this.argsPayload" in src
    assert "{ message: this.message, args: a, verbosity: this.verbosity }" in src
    # run gateado: texto OU args, e args inválidos travam (sem footgun de boolean)
    assert "!this.message.trim() && !this.hasArgs" in src
    assert "if (this.hasArgErrors) return false" in src
    # "inserir template" preenche o FORM (não joga JSON na mensagem) + restore reidrata args
    assert "this.argValues = JSON.parse(tpl)" in src
    assert "this.argValues = h.argValues || {}" in src


def test_layout_lado_a_lado():
    src = PG.read_text(encoding="utf-8")
    assert "lg:grid-cols-2" in src   # builder | resposta lado a lado
