# Testes E2E de interface (Playwright)

A camada que faltava na pirâmide de testes. Unit + integração (Postgres real) já
cobrem o backend; os testes em `tests/e2e/` dirigem o **app real num browser
headless** (Chromium via Playwright) e validam que as **telas** funcionam — HTML,
JavaScript (Alpine.js), navegação, formulários e as jornadas do usuário.

## O que cobre

| Arquivo | O que valida |
|---|---|
| `test_smoke_pages.py` | Todas as ~24 telas (rotas de `frontend.PAGES` sem parâmetro) carregam autenticadas, HTTP < 400, `<title>` correto e **sem erro de JS não-tratado**. Mais o **auth gate** (tela protegida sem sessão → `/login`). |
| `test_journey_login.py` | Login pelo formulário real → cai no Dashboard. |
| `test_journey_criar_agente.py` | Wizard de criar agente → agente aparece na lista. |
| `test_journey_publicar_catalogo.py` | Wizard de 4 passos → entry publicada (redirect `/catalog/{id}`). |
| `test_journey_invocar_pipeline.py` | Invocar pipeline pelo Fluxograma → UI mostra o desfecho. Depende de LLM/ambiente → marcado `slow`, **pula** se não houver pipeline executável. |

Seletores são ancorados em `data-testid` (anti-flaky) — nunca em classe CSS ou
texto que muda. Ao mexer nesses elementos, mantenha o `data-testid`.

## Rodar localmente

Pré-requisitos (1x):

```bash
docker compose up -d app                       # app na porta 7000
python -m playwright install chromium          # browser headless
```

Se o banco já tem usuários, semeie o usuário de teste (só na 1ª vez):

```bash
docker exec agente_app python scripts/seed_e2e_user.py
```

> Em banco **vazio**, o suite cria o root sozinho pelo fluxo real de setup —
> o seed é dispensável.

Rodar:

```bash
pytest tests/e2e -m e2e                 # tudo
pytest tests/e2e -m "e2e and not slow"  # sem a jornada de pipeline (LLM)
pytest tests/e2e -m e2e --headed        # ver o browser (debug)
```

Excluir E2E da suíte normal: `pytest tests/ -m "not e2e and not integration"`.

## Config (env)

| Var | Default | |
|---|---|---|
| `E2E_BASE_URL` | `http://localhost:7000` | URL do app |
| `E2E_USERNAME` | `e2e_admin` | usuário de teste |
| `E2E_PASSWORD` | `e2e-pass-1234` | senha de teste |
| `E2E_DISPLAY_NAME` | `E2E Admin` | nome de exibição |

## Comportamento de skip (não falha à toa)

- App fora do ar (`/api/health` não responde) → **todos** os E2E pulam.
- Sem credenciais E2E válidas (banco com usuários e seed não rodado) → jornadas
  que exigem login pulam com instrução de rodar o seed.
- Sem pipeline executável → a jornada de pipeline pula.

## CI

Job `test-e2e` em `.github/workflows/test.yml` — **manual** (`workflow_dispatch`)
e **não-bloqueante** (`continue-on-error`). Sobe Postgres+Redis, inicia o app via
uvicorn e roda o subconjunto determinístico (`e2e and not slow`). O caminho
primário de E2E é local, contra `docker compose up`.

## Por que `data-testid` e não classe/texto

Seletor frágil é a causa nº 1 de teste E2E flaky. Os `data-testid` desacoplam o
teste do visual: trocar Tailwind, copy ou layout não quebra o teste; só mexer no
elemento de propósito quebra — que é exatamente quando o teste deve avisar.
