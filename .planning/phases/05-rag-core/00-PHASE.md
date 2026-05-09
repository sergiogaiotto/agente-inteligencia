# Onda 6 — RAG Core (3 waves)

## Vision

Transformar o RAG de "infraestrutura presente sem caminho de uso" em **espinha dorsal de governança factual** da plataforma. A plataforma promete agentes que respondem com base em documentos autorizados, citáveis, auditáveis, governados por skill, com qualidade mensurável. Hoje 60% disso existe (Qdrant, embedder Azure, retrieval híbrido, Verifier multi-dim). Esta onda fecha os 40% que faltam.

## Decisões do usuário (em diálogo prévio)

1. **Embedding**: `text-embedding-3-small` (Azure) — mantém. Sem upgrade pra 3-large.
2. **Volume**: indeterminado — design deve escalar (sem otimização precoce, sem limites artificiais).
3. **Parsing de documentos**: usar [markitdown](https://github.com/microsoft/markitdown) da Microsoft com extras `[all]`. Aceita PDF, DOCX, PPTX, XLSX, HTML, MD, CSV, JSON, XML, EPUB, Outlook .msg, ZIPs (recursivo), YouTube transcripts, audio (transcrição), imagens (OCR + LLM caption opcional).
4. **Citações**: **opcionais** (não bloqueia resposta). Plataforma preserva throughput e performance — citação é hint no prompt, não constraint.
5. **Multi-tenant**: não. `confidentiality_label` continua como metadata mas sem enforcement por clearance.
6. **Escopo**: **Core** — Waves 1+2+3 (Tactical+Governance+Auditability). Métricas (Wave 4), confidentiality enforcement (Wave 5) e ops (Wave 6) ficam pra ondas próprias depois.

## Scope (3 waves)

### Wave 1 — Ingestão Operável (DESBLOQUEADOR)
Operador consegue colocar conteúdo na base sem tocar curl. Aceita qualquer formato via markitdown. Fontes mostram contagem de chunks, status, última ingestão.

- `01-PLAN-ingestion-backend.md` — markitdown integration + endpoints `ingest-file`, `ingest-url`, `stats`
- `02-PLAN-ingestion-ui.md` — modal de ingestão (texto/arquivo/URL) + cards com stats

### Wave 2 — Vínculo Skill ↔ Source (GOVERNANÇA)
Cada skill declara explicitamente quais fontes pode consultar; retriever respeita.

- `03-PLAN-skill-source-backend.md` — parser estruturado de evidence_policy + filtro no retriever
- `04-PLAN-skill-source-ui.md` — multi-select no skill editor + chips no workspace

### Wave 3 — Citações Rastreáveis (AUDITABILIDADE — opt-in)
Resposta do agente pode citar `[E1]`, `[E2]`. UI mostra como chips clicáveis com chunk text + fonte.

- `05-PLAN-citations-backend.md` — prompt opcional + post-process + persistência
- `06-PLAN-citations-ui.md` — chips clicáveis no workspace + Verifier ganha `citation_coverage`

## Out of scope (consciente)

- **Métricas de RAG por skill/source** (Wave 4 do plano original) — onda dedicada após este Core.
- **Confidentiality enforcement** (Wave 5) — multi-tenant fora do horizonte.
- **Re-embedding em massa, freshness tracker** (Wave 6) — quando volume justificar.
- **OCR via Tesseract local** — markitdown[all] cobre via outras vias quando necessário.
- **Web crawler com follow-links** — só ingestão URL-a-URL.

## Must-haves do Core inteiro

A onda só está completa quando, com 1 fonte registrada e ingerida com PDF + URL + texto:

- [ ] Operador consegue ingerir PDF, DOCX, URL e texto sem usar curl/Postman.
- [ ] Cada fonte mostra contagem de chunks indexados e timestamp da última ingestão.
- [ ] Skill declara fontes permitidas via multi-select no editor; salva no `## Evidence Policy`.
- [ ] Workspace de uma interação com agente vinculado à skill mostra "fontes em jogo" como chips.
- [ ] Retriever filtra Qdrant + BM25 pelas fontes permitidas pela skill ativa.
- [ ] Resposta do agente (quando skill tem `cite_sources: true` no evidence_policy) pode incluir `[E1]`.
- [ ] UI do `/workspace` renderiza `[E1]` como chip → modal mostra chunk + source name + relevance_score.
- [ ] Trace persiste `citations_used` para auditoria.

## Files touched (consolidado)

| Arquivo | Wave | Tipo |
|---------|------|------|
| `requirements.txt` | 1 | edit (+markitdown[all]) |
| `Dockerfile` | 1 | edit (+ffmpeg, exiftool) |
| `app/evidence/converters.py` | 1 | new (wrapper markitdown) |
| `app/evidence/ingest.py` | 1 | edit (extend com file/url) |
| `app/routes/dashboard.py` | 1 | edit (+3 endpoints) |
| `app/templates/pages/evidence.html` | 1 | edit (modal + stats) |
| `app/skill_parser/parser.py` | 2 | edit (parse evidence_policy estruturado) |
| `app/evidence/runtime.py` | 2 | edit (filtro source_ids) |
| `app/agents/engine.py` | 2,3 | edit (passa skill ao retriever; cite hint; pós-process) |
| `app/templates/pages/skill_form.html` | 2 | edit (multi-select fontes) |
| `app/templates/pages/workspace.html` | 2,3 | edit (chips de fonte; chips de citação) |
| `app/core/database.py` | 3 | edit (coluna citations em turns) |
| `app/verifier/multi_dim_judge.py` | 3 | edit (citation_coverage opcional) |

## Estimativa

- Wave 1: 2-3 dias (backend + UI)
- Wave 2: 2 dias (backend + UI)
- Wave 3: 2-3 dias (backend + UI + Verifier hook)
- **Total**: 6-8 dias trabalho focado, ~3-4 sessões

## Riscos

- **markitdown[all] pode pesar muito o image size** (libs PDF/audio). Mitigação: medir após install; se passar de +200MB, trocar pra extras específicos `[pdf,docx,pptx,xlsx]`.
- **markitdown precisa de ffmpeg/exiftool no SO**. Adicionar ao Dockerfile.
- **Embeddings cost**: cada ingest chama Azure embedding. Monitorar custo via dashboard. Sem rate-limit por agora (operador é único).
- **PDF rico** (tabelas, layouts complexos): markitdown faz best-effort. Se cair muito recall, considerar Wave futura "Azure Document Intelligence" (`markitdown[az-doc-intel]`).
