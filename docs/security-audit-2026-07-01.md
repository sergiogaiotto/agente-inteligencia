# Auditoria de Segurança — 2026-07-01 (AppSec + LLM)

Aplicação do playbook `SKILL.md` (OWASP Top 10 / OWASP LLM Top 10 / ASVS / CWE /
LGPD) à plataforma Maestro. A auditoria foi conduzida por um enxame multi-agente
(11 domínios do SKILL.md), com **verificação adversarial** de cada achado (um
cético independente tentou refutar cada um lendo o código real). Só sobraram
achados confirmados/plausíveis.

- **Achados:** 56 (0 refutados) — **11 Critical, 13 High, 15 Medium, 15 Low, 2 Info**.
- **Método de prova:** cada correção tem teste positivo **e** negativo (unit) e,
  quando aplicável, validação **ao vivo** (Docker) — "prova, não promessa".
- **Versão da plataforma:** `24.2.1 → 24.5.0` (4 PRs de hardening merged).

---

## 1. Remediado nesta rodada (merged na `main`)

As 3 raízes estruturais que explicavam a maioria dos Criticals foram fechadas,
mais uma baseline de headers. Todas verificadas unit + ao vivo, sem regressão.

### PR #463 — Cookie de sessão assinado (`24.3.0`) · CWE-565/CWE-639
O cookie `user_id` era o **UUID cru** do usuário → qualquer um forjava
`Cookie: user_id=<uuid>` e virava aquele usuário (inclusive root). Agora carrega
um **token HMAC** (`itsdangerous`, chaveado por `secret_key`, com expiração).
Helpers centrais em `app/core/auth.py` (`sign_session`/`read_session_uid`) usados
nos 6 pontos de leitura. Fecha os achados **00, 01, 08, 10, 19, 20**.
> Prova ao vivo: o UUID cru real → `/me` `user:null` + 401; login emite token
> assinado → autentica. **Depende de um `secret_key` forte em produção — ver §2.**

### PR #464 — Default-deny de auth no data plane `/api/v1/*` (`24.4.0`) · CWE-306
~159 endpoints `/api/v1` não exigiam identidade no servidor (o "gate" era só o
redirect do frontend, contornável chamando a API direto). `ApiAuthMiddleware`
(`app/core/api_auth.py`) **nega por padrão** — exige sessão assinada ou
`X-API-Key`, salvo allowlist mínimo (login/logout/me/check-setup, bootstrap do 1º
usuário, ingress federado por assinatura de peer). **Mitiga** (fecha o acesso
anônimo de) os achados **02, 03, 04, 06, 09, 12, 13, 14, 15, 16, 17, 18, 19, 23,
26, 33** e o vetor de vazamento do achado **00**.

### PR #465 — Allowlist de runtime MCP stdio (`24.4.1`) · CWE-78
`POST /api/v1/tools/test|call` e `/tools/execute` passavam o `endpoint` do usuário
para `subprocess` (shell no Windows; qualquer binário como argv[0] no Linux) →
**RCE**. Agora `build_stdio_argv()` tokeniza **sem shell** e exige `argv[0]` ∈
allowlist de runtimes (npx/node/python/uvx/...). Fecha **07, 32** (defesa em
profundidade após o gate do #464).
> Prova ao vivo: payload `/bin/sh -c 'touch <marker>'` → "runtime não permitido";
> marker **não** criado no container.

### PR #466 — Baseline de headers de segurança no app (`24.5.0`) · SKILL.md §7
O app não emitia **nenhum** header de segurança (o Caddy só cobre parte e só em
prod). `SecurityHeadersMiddleware` garante baseline independente do proxy:
`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
`Permissions-Policy`, CSP (`frame-ancestors 'self'; object-src 'none'; base-uri
'self'`) e HSTS sob HTTPS. Fecha **37** e mitiga **51** (CSP parcial — falta
`script-src` com nonces).

---

## 2. ⚠️ AÇÃO DO USUÁRIO — CRÍTICO (não automatizável com segurança)

O container roda com **`APP_ENV=production` e `SECRET_KEY` = o placeholder público
do `.env.example`** (`troque-isto-por-uma-chave-aleatoria-de-64-caracteres`), e
`MAESTRO_SECRET_KEY` **não setado**. Consequências:

1. **Cookies assinados forjáveis:** o `secret_key` que assina as sessões (PR #463)
   é público (está no `.env.example` do repo). Um atacante que o conhece **forja
   tokens de sessão válidos** — reabrindo a impersonação. O #463 só é seguro com
   um `secret_key` real e secreto.
2. **Cifra at-rest previsível:** `secrets.py` deriva a chave Fernet do `secret_key`,
   e `crypto.py` usa `MAESTRO_SECRET_KEY` (ausente → fallback determinístico
   inseguro). Tokens de conector/segredos ficam cifrados com chave previsível.

**Por que não corrigi em código:** rotacionar `secret_key`/`MAESTRO_SECRET_KEY`
torna **ilegíveis os segredos já cifrados no banco** (tokens de MCP/conectores) e
invalida as sessões — é uma migração operacional com perda de dados se feita às
cegas. E um fail-fast que recuse subir com segredo placeholder **quebraria o boot
do ambiente atual**. Portanto é ação do operador:

```bash
# 1) Gere segredos fortes (64 hex chars cada):
python -c "import secrets; print('SECRET_KEY='+secrets.token_hex(32))"
python -c "import secrets; print('MAESTRO_SECRET_KEY='+secrets.token_hex(32))"
# 2) Grave-os no .env (NÃO commitar). Depois rebuild+up:
docker compose build app && docker compose up -d app
# 3) Efeitos esperados: todos re-logam (sessões invalidadas); conectores com
#    auth_token cifrado precisam ter o token RE-CADASTRADO na UI (a chave mudou).
```

Após o operador setar segredos reais, um **PR de fail-fast** (recusar boot em
produção com segredo placeholder/curto — SKILL.md §1/§8, achados 24/35/36) pode
ser mergeado com segurança. Recomendo também `APP_ENV=development` para o ambiente
local e `APP_ENV=production` só no deploy real, com `COOKIE_SECURE=true` em prod.

---

## 3. Backlog priorizado (próximos PRs)

Ordem sugerida por (severidade × explorabilidade × baixo risco de regressão):

1. **XSS — allowlist de esquema nos renderers de link markdown** (High; achados
   21, 22, 52). `workspace.html`/`tools.html`: bloquear `javascript:`/`data:`
   (reusar `marked+DOMPurify` já presente em `base.html`). Contido, testável.
2. **Gating por PAPEL (admin/root) + mascaramento de segredos** (High; achados
   04, 09, 14, 23). `GET /settings` mascarar valores; `PUT /settings`,
   `tools/execute`, `llm-routing`, CRUD de usuários → exigir papel. Requer
   confirmar o modelo de papéis (nav já esconde de não-root).
3. **SSRF** (High; achados 03, 23). Aplicar `app/core/ssrf.py` a
   `proxy`/`test-inline`/`extract-cookie` e às URLs de provider no `PUT /settings`.
4. **Path traversal + limite de upload** (High; achados 17, 16). `workspace/upload`:
   basename + containment; corte por tamanho.
5. **Fail-fast de segredo em prod + `cookie_secure`** (Medium; 24, 35, 36, 38) —
   **após** §2.
6. **Rate-limit** (Medium; 25, 27, 28, 29, 42). Precedência do bucket LLM;
   `X-Forwarded-For` só de proxy confiável; `--forwarded-allow-ips` restrito;
   eviction do `_MemoryLimiter`.
7. **Supply chain / CI** (Medium/Low/Info; 30, 31, 44, 46, 54, 55). `.dockerignore`;
   lockfile com hashes; `pip-audit` + SAST (CodeQL/Bandit) no CI; digests de imagem;
   defaults do Grafana (45).
8. **LGPD / logging** (Low; 47, 48). IP+actor no audit_log; eventos de login/logout;
   DLP nas linhas enviadas ao `/explain`; erro genérico ao cliente.
9. **CSRF** (Low; 39, 53) — exige o frontend enviar `X-CSRF-Token`; mitigado hoje
   por `SameSite=lax`.
10. **Diversos** (Low): política de senha (41); login O(1) por índice (43);
    prompt-guard pt-BR (34); `max_tokens`/allowlist de tool no LLM (49, 50);
    revogação de sessão server-side no logout (40).

---

## 4. Apêndice — todos os 56 achados

> Status: `RESOLVIDO #PR` = corrigido e provado; `MITIGADO #464 (gate)` = acesso
> anônimo fechado, mas resta hardening específico (SSRF/papel/mascaramento/etc.);
> `AÇÃO USUÁRIO` = ver §2; `BACKLOG` = ver §3.

| # | Sev | Domínio | Local | Achado | Status |
|---|-----|---------|-------|--------|--------|
| 00 | Critical | Autenticação e Sessão | app/routes/users.py:50 | Cookie de sessão "user_id" é UUID CRU sem assinatura/MAC → forja de sessão e impersonação total | RESOLVIDO #463 |
| 01 | Critical | Autorização / IDOR / R | app/routes/users.py:50 | Cookie de sessão 'user_id' é o UUID cru sem assinatura/MAC — forja trivial de qualquer sessão ( | RESOLVIDO #463 |
| 02 | Critical | Autorização / IDOR / R | app/routes/agents.py:483 | Router de agentes inteiro SEM autenticação — invoke/CRUD/delete expostos publicamente | MITIGADO #464 (gate) |
| 03 | Critical | Autorização / IDOR / R | app/routes/api_connectors.py:410 | POST /api/v1/api-connectors/proxy sem auth — SSRF autenticado com credenciais dos conectores +  | MITIGADO #464 (gate) |
| 04 | Critical | Autorização / IDOR / R | app/routes/dashboard.py:2278 | PUT /api/v1/settings sem auth — reconfiguração da plataforma (provider/rota/segredos LLM) por a | MITIGADO #464 (gate) |
| 05 | Critical | Autorização / IDOR / R | app/routes/users.py:201 | IDOR/RBAC no CRUD de usuários: caller identificado só pelo cookie forjável, e checagens contorn | BACKLOG |
| 06 | Critical | Autorização / IDOR / R | app/routes/api_connectors.py:163 | CRUD de api-connectors/endpoints sem auth — leitura/edição/exclusão de conectores de qualquer ' | MITIGADO #464 (gate) |
| 07 | Critical | Injeção (SQL / comando | app/routes/dashboard.py:1956 | RCE não autenticado: POST /api/v1/tools/test passa 'endpoint' do usuário direto para subprocess | RESOLVIDO #465 |
| 08 | Critical | Logging, Auditoria e P | app/core/auth.py:139 | Cookie de sessão 'user_id' é o UUID cru, sem assinatura/MAC — combinado com listagem de usuário | RESOLVIDO #463 |
| 09 | Critical | Segredos e Configuraçã | app/routes/dashboard.py:2272 | GET /api/v1/settings expõe TODOS os segredos de LLM em texto claro, sem autenticação | MITIGADO #464 (gate) |
| 10 | Critical | Transporte, CORS e Hea | app/core/auth.py:139 | Cookie de sessão 'user_id' é o UUID CRU sem assinatura/MAC — forjável e enumerável (auth bypass | RESOLVIDO #463 |
| 11 | High | Autenticação e Sessão | app/routes/users.py:107 | Endpoints que mudam usuários (create/update/delete) sem Depends(require_user) e sem controle de | BACKLOG |
| 12 | High | Autorização / IDOR / R | app/routes/users.py:91 | GET /api/v1/users sem auth expõe id, role, email e domínios de todos os usuários (habilita a fo | MITIGADO #464 (gate) |
| 13 | High | Autorização / IDOR / R | app/routes/skills.py:178 | Router de skills sem auth — criar/editar/deletar SKILL.md (código de comportamento dos agentes) | MITIGADO #464 (gate) |
| 14 | High | Autorização / IDOR / R | app/routes/dashboard.py:2118 | POST /api/v1/tools/execute e CRUD de tools/knowledge-sources sem auth no router dashboard | MITIGADO #464 (gate) |
| 15 | High | Consumo e Disponibilid | app/routes/agents.py:484 | Endpoint LLM /api/v1/agents/{id}/invoke sem autenticação e fora do bucket de rate-limit caro | MITIGADO #464 (gate) |
| 16 | High | Consumo e Disponibilid | app/routes/workspace.py:434 | POST /api/v1/workspace/upload sem autenticação e sem limite de tamanho — grava disco + RAM ilim | MITIGADO #464 (gate) |
| 17 | High | Injeção (SQL / comando | app/routes/workspace.py:439 | Path traversal na escrita de upload: POST /api/v1/workspace/upload usa file.filename sem saniti | MITIGADO #464 (gate) |
| 18 | High | Logging, Auditoria e P | app/routes/dashboard.py:1704 | GET /api/v1/history sem autenticação vaza audit_log, interactions, turns e envelopes de TODOS o | MITIGADO #464 (gate) |
| 19 | High | Riscos de LLM (OWASP L | app/routes/agents.py:483 | LLM10 — POST /api/v1/agents/{agent_id}/invoke executa o LLM SEM autenticação (consumo/custo ili | MITIGADO #464 (gate) |
| 20 | High | Riscos de LLM (OWASP L | app/core/auth.py:139 | LLM06 — Cookie de sessão user_id sem assinatura/MAC permite forjar identidade | RESOLVIDO #463 |
| 21 | High | Saída e XSS | app/templates/pages/workspace.html:1582 | XSS via javascript: URL em link markdown da resposta do agente (workspace / chat principal) | BACKLOG |
| 22 | High | Saída e XSS | app/templates/pages/tools.html:794 | XSS via javascript: URL em link markdown do resultado de execução de tool/MCP (página /mcp) | BACKLOG |
| 23 | High | Segredos e Configuraçã | app/routes/dashboard.py:2278 | PUT /api/v1/settings permite sobrescrever credenciais/URLs de LLM sem autenticação (credential  | MITIGADO #464 (gate) |
| 24 | Medium | Autenticação e Sessão | app/core/config.py:21 | Sem fail-fast de secret_key fraco/padrão no boot em produção (default "change-me") | AÇÃO USUÁRIO |
| 25 | Medium | Autenticação e Sessão | app/core/ratelimit.py:137 | Rate-limit de brute-force no login é chaveado por identidade forjável e furável | BACKLOG |
| 26 | Medium | Autorização / IDOR / R | app/routes/wizard.py:362 | Router wizard sem auth — geração LLM anônima (abuso de custo) a partir de descrição controlada  | MITIGADO #464 (gate) |
| 27 | Medium | Consumo e Disponibilid | app/core/ratelimit.py:160 | Precedência de operador em _bucket_for_path deixa endpoints caros de LLM (invoke) no bucket gen | BACKLOG |
| 28 | Medium | Consumo e Disponibilid | app/core/ratelimit.py:142 | X-Forwarded-For confiado sem allowlist de proxy: bypass total de rate-limit e crescimento ilimi | BACKLOG |
| 29 | Medium | Container / Deploy | Dockerfile:85 (CMD) + app/core/ratelimit.p | uvicorn com --forwarded-allow-ips=* confia em X-Forwarded-For de qualquer origem → bypass de ra | BACKLOG |
| 30 | Medium | Dependências e Supply  | requirements.txt:7 | requirements.txt sem teto de versão e sem lockfile: builds não reproduzíveis, expostos a depend | BACKLOG |
| 31 | Medium | Dependências e Supply  | .github/workflows/test.yml:50 | CI não roda auditoria de vulnerabilidades de dependências (sem pip-audit / safety / Dependabot  | BACKLOG |
| 32 | Medium | Injeção (SQL / comando | app/mcp/runtime.py:212 | Command injection via shell=True no Windows (create_subprocess_shell com comando controlado pel | RESOLVIDO #465 |
| 33 | Medium | Riscos de LLM (OWASP L | app/routes/agents.py:159 | LLM07 — Vazamento de system prompt / SKILL.md via GET /api/v1/agents e /api/v1/skills SEM auten | MITIGADO #464 (gate) |
| 34 | Medium | Riscos de LLM (OWASP L | app/core/prompt_guard.py:33 | LLM01 — Guardrail de prompt injection só cobre inglês; payloads pt-BR passam | BACKLOG |
| 35 | Medium | Segredos e Configuraçã | app/core/crypto.py:43 | Fallback de master key INSEGURO e determinístico falha ABERTO em produção (chave pública embuti | AÇÃO USUÁRIO |
| 36 | Medium | Segredos e Configuraçã | app/core/config.py:21 | secret_key default 'change-me' deriva chave Fernet previsível para tokens em repouso, sem guard | AÇÃO USUÁRIO |
| 37 | Medium | Transporte, CORS e Hea | infra/caddy/Caddyfile:55 | Sem X-Frame-Options nem CSP frame-ancestors em rotas da app (clickjacking) — comentário do Cadd | RESOLVIDO #466 |
| 38 | Medium | Transporte, CORS e Hea | app/core/config.py:139 | cookie_secure=False por padrão e o template de produção (.env.example com APP_ENV=production) n | BACKLOG |
| 39 | Low | Autenticação e Sessão | app/core/config.py:138 | CSRF completamente inativo: verify_csrf nunca é chamado, csrf_required=False, sem middleware CS | BACKLOG |
| 40 | Low | Autenticação e Sessão | app/routes/users.py:61 | Logout não invalida a sessão do lado servidor (cookie de id permanece válido) | BACKLOG |
| 41 | Low | Autenticação e Sessão | app/models/schemas.py:338 | Senha sem política mínima (comprimento/complexidade) no cadastro | BACKLOG |
| 42 | Low | Consumo e Disponibilid | app/core/ratelimit.py:85 | _MemoryLimiter (fallback sem Redis) nunca remove buckets vazios: vazamento de memória ilimitado | BACKLOG |
| 43 | Low | Consumo e Disponibilid | app/routes/users.py:28 | Login carrega até 1000 usuários e faz scan linear em Python (teto silencioso + custo por reques | BACKLOG |
| 44 | Low | Container / Deploy | .dockerignore (inexistente) + Dockerfile:6 | Ausência de .dockerignore: build context envia .env (segredos reais) e .git ao daemon; imagem f | BACKLOG |
| 45 | Low | Container / Deploy | docker-compose.yml:346-354:346 | Grafana com admin/admin e acesso anônimo habilitados por default no compose (env de compose) | BACKLOG |
| 46 | Low | Dependências e Supply  | .github/workflows/test.yml:31 | CI sem SAST (Semgrep/CodeQL/Bandit): código com padrões inseguros mergeia sem análise estática  | BACKLOG |
| 47 | Low | Logging, Auditoria e P | app/core/database.py:312 | audit_log não registra IP nem, em vários writes, o actor — e eventos de login/logout não são au | BACKLOG |
| 48 | Low | Logging, Auditoria e P | app/routes/logs_admin.py:470 | Handler /explain envia linhas de log CRUAS ao LLM externo e submit_entry devolve tipo+mensagem  | BACKLOG |
| 49 | Low | Riscos de LLM (OWASP L | app/core/llm_providers.py:44 | LLM10 — Nenhum limite de max_tokens por chamada LLM; cap de tokens é apenas aviso pós-fato | BACKLOG |
| 50 | Low | Riscos de LLM (OWASP L | app/agents/engine.py:1092 | LLM06/LLM05 — Tool calls MCP executadas com nome+args do LLM sem allowlist de operação nem huma | BACKLOG |
| 51 | Low | Saída e XSS | infra/caddy/Caddyfile:53 | Ausência total de Content-Security-Policy (sem defesa em profundidade contra XSS) | PARCIAL #466 |
| 52 | Low | Saída e XSS | app/templates/pages/workspace.html:1582 | Esquema data: também não é bloqueado nos renderers de link markdown | BACKLOG |
| 53 | Low | Transporte, CORS e Hea | app/core/auth.py:75 | Proteção CSRF implementada mas NUNCA aplicada (verify_csrf sem call site; csrf_required nunca l | BACKLOG |
| 54 | Info | Container / Deploy | Dockerfile:9,31 + docker-compose.yml:89,12 | Imagens base fixadas só por tag mutável (sem digest): supply-chain / reprodutibilidade | BACKLOG |
| 55 | Info | Dependências e Supply  | .pre-commit-config.yaml:13 | pre-commit sem hook de segurança de dependências/SAST (só higiene de arquivo) | BACKLOG |

