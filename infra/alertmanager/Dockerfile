# Alertmanager com a config de roteamento baked (Onda 6). Espelha o padrão dos
# demais serviços de observabilidade (prometheus/tempo/loki): imagem agente-*:local.
FROM prom/alertmanager:v0.27.0
COPY alertmanager.yml /etc/alertmanager/alertmanager.yml
