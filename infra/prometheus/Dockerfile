# Prometheus com o scrape config baked (Onda 2). Espelha o padrão dos demais
# serviços de observabilidade (tempo/loki/grafana): imagem própria agente-*:local.
FROM prom/prometheus:v2.55.1
COPY prometheus.yml /etc/prometheus/prometheus.yml
