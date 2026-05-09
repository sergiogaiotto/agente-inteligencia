# BACKLOG STUB — Onda 4c.3: Helm chart

> **Status**: aguardando ativação. Gate: time precisa estar usando Kubernetes.
> **Capturado em**: 2026-05-09 (após Onda 4 — pre-flight check em agent CRUD)
> **Como ativar**: criar `.planning/phases/04c3-helm-chart/00-PHASE.md` partindo deste stub e expandir os "Open questions" em decisões.

## Goal

Empacotar o `agente-inteligencia` como Helm chart deployável em qualquer cluster Kubernetes, com configuração externalizada via `values.yaml`, dependências declaradas (subcharts), e operador podendo subir `helm install agente-ia ./charts/agente-ia` num namespace limpo.

## Why

Hoje o app roda local (Docker) e o time não está em k8s. Quando estiver, os blocos de infra (Postgres, Redis, Qdrant, OPA, Tempo, LiteLLM, MCP servers) precisam de uma forma reproduzível de deploy. Um Helm chart resolve onboarding de novos clusters/ambientes (staging, prod, multi-tenant) sem manualismo de kubectl.

## Pré-requisitos (verificar antes de ativar)

| Item | Status atual (2026-05-09) | Notas |
|------|---------------------------|-------|
| Dockerfile multi-stage | ✓ existe ([Dockerfile](Dockerfile)) | non-root user (uid 1000), port 7000, healthcheck contra `/api/health` |
| Endpoint `/api/health` | ✓ existe ([main.py:67](app/main.py:67)) | exempt do rate-limit ([ratelimit.py:184-185](app/core/ratelimit.py:184)) |
| Lifespan graceful shutdown | ✓ existe ([main.py:18-26](app/main.py:18)) | drena tasks async do verifier antes de fechar pool |
| Config externalizada por env | ✓ ([config.py](app/core/config.py) — pydantic Settings com `.env`) | secrets ainda precisam de tratamento adequado em k8s |
| Multi-worker validado | ⚠ não validado | dispatcher async tem stats in-process (Onda 3 §00-PHASE.md anota isso como limitação) |
| Imagem publicada em registry | ✗ ainda só local | precisa de CI build + push antes do chart ser útil |
| `/healthz` + `/readyz` separados | ✗ usa `/api/health` único | em k8s, comum separar liveness (sempre 200 se processo vivo) e readiness (200 só quando DB conectado) — verificar se isso vira gargalo |

## Subcharts dependentes (avaliar fontes oficiais quando ativar)

Stack atual roda fora do app:

| Componente | Hoje | Opções em k8s |
|------------|------|---------------|
| PostgreSQL | container Docker | Bitnami chart, CrunchyDB operator, ou managed (RDS/CloudSQL/Aiven) |
| Redis | container Docker | Bitnami chart ou managed |
| Qdrant | container Docker | Qdrant tem k8s operator próprio (cloud-native-qdrant) |
| OPA | container Docker | OPA tem helm chart oficial; alternativa: sidecar por pod |
| Tempo (OTel) | endpoint configurável | Grafana Tempo chart |
| LiteLLM gateway | opcional, via `LLM_GATEWAY_ENABLED` | tem chart próprio ou deploy custom |
| MCP servers | processos auxiliares | cada server vira Deployment próprio com Service interno |

**Decisão a tomar quando ativar**: subcharts dependentes ou stack assumida externa? Híbrido (default in-cluster com flag `external: true` por componente) costuma ser pragmático.

## Scope sugerido (3 plans, 2 waves — ajustar quando ativar)

### Wave 1 — Chart base do app

- `Chart.yaml` (apiVersion v2, dependências em modo opt-in)
- `values.yaml` com defaults sensatos + comentários por bloco
- `templates/deployment.yaml` (replicas, image, env, probes, resources, securityContext non-root, podAntiAffinity)
- `templates/service.yaml` (ClusterIP)
- `templates/configmap.yaml` (config não-secreta)
- `templates/secret.yaml` (placeholder; produção via External Secrets Operator)
- `templates/serviceaccount.yaml`
- `templates/_helpers.tpl` (full name, labels)
- `values.schema.json` (validação de tipos)

### Wave 2 — Acoplamentos e operacional

- `templates/ingress.yaml` (opt-in via `ingress.enabled`)
- `templates/hpa.yaml` (CPU + memória; custom metrics ficam para depois)
- `templates/pvc.yaml` (data/uploads)
- `templates/networkpolicy.yaml` (opt-in)
- Subcharts em `charts/` (Postgres, Redis, Qdrant, OPA — cada um opt-in via `*.enabled`)
- Hooks `pre-install` para `init_db()` (se não confiar no lifespan)
- ServiceMonitor para Prometheus (quando OTel/Prometheus estiver no roadmap cross-worker)

### Wave 3 (opcional) — Cron jobs e extras

- `CronJob` para harness de regressão noturno
- `Job` template para migrations standalone
- Job de cleanup de `data/uploads` antigos

## Open questions (decisões necessárias na ativação)

1. **Postgres in-cluster ou managed?** In-cluster simplifica dev mas opera mal em produção sem PVC sólido. Managed reduz operação mas custa $$.
2. **Secrets**: External Secrets Operator (Vault/AWS Secrets Manager) ou Sealed Secrets? Ou continuar com k8s Secret cru (menor segurança)?
3. **Multi-tenancy**: namespace por tenant ou single namespace + RBAC?
4. **Ingress**: nginx-ingress, Traefik, ALB, ou service mesh (Istio/Linkerd)?
5. **HPA metrics**: CPU/mem padrão ou custom (request rate, judge backlog)? Onda 3 já anotou que stats do dispatcher são in-process — pra HPA por backlog precisa de Prometheus cross-worker antes.
6. **MCP servers**: Deployment próprio cada ou sidecars do app pod?
7. **`/healthz` vs `/readyz` separados**: vale o refator pra ter readiness real (testa DB conectado antes de aceitar tráfego)?
8. **Backup/restore**: scripts pra Postgres e Qdrant (opcional, se in-cluster)?
9. **Versionamento do chart**: SemVer + appVersion alinhado com tag git?
10. **Distribution**: chart museum, OCI registry, ou repo separado no GitHub?

## Riscos

- **Dispatcher async cross-worker** ([app/verifier/async_dispatcher.py](app/verifier/async_dispatcher.py)): contadores in-process. Em deploy multi-pod cada um tem os seus. UI `/quality` mostra apenas o pod que respondeu o GET. Antes de ativar HPA com replicas > 1, mover stats pra Redis/Prometheus.
- **Cap de concorrência por pod**: `verifier_max_concurrent_jobs=20` é por processo. Em N pods, capacidade total = N × 20. Se subir replicas, revisitar o cap.
- **Lifespan drain de 5s**: pode ser curto pra workloads com judge lento. Verificar `terminationGracePeriodSeconds` no Deployment ≥ 10s.
- **Settings `verifier_production_sample_rate`**: se diferente entre pods (configmap inconsistente), amostragem fica enviesada. Garantir um único ConfigMap e rolling update atômico.
- **`data/uploads` PVC**: stateful, complica HPA. Considerar S3/GCS antes do chart se uploads são parte importante do fluxo.

## Files to inspect quando ativar (já no repo hoje)

- [Dockerfile](Dockerfile) — base da imagem; expõe 7000, healthcheck em /api/health
- [app/main.py](app/main.py) — lifespan, OTel init, middlewares
- [app/core/config.py](app/core/config.py) — todos os env vars que viram values.yaml
- [app/core/database.py](app/core/database.py) — `init_db`, `close_db`, asyncpg pool
- [app/core/ratelimit.py](app/core/ratelimit.py) — middleware Redis com fallback memory
- [app/core/otel.py](app/core/otel.py) — OTLP exporter config
- [app/core/opa_client.py](app/core/opa_client.py) — endpoint OPA
- [app/evidence/qdrant_store.py](app/evidence/qdrant_store.py) — endpoint Qdrant
- [app/verifier/async_dispatcher.py](app/verifier/async_dispatcher.py) — anotação sobre cross-worker
- [requirements.txt](requirements.txt) — para Chart.yaml `appVersion`/dependências indiretas

## Pull-from-the-future checklist (quando virar phase real)

- [ ] CI publicando imagem em registry (GHCR ou ECR)
- [ ] Healthz/readyz separados implementados
- [ ] Stats cross-worker (Redis ou Prometheus) — fora deste backlog item, pré-requisito
- [ ] Decisões 1-10 acima documentadas em CONTEXT.md da phase
- [ ] Chart inicial testado com `helm install --dry-run` em cluster local (kind/k3d)
- [ ] Smoke deploy em staging cluster antes de produção
