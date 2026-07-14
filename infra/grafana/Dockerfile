# Grafana com provisioning + dashboards embutidos na imagem.
# Volume nomeado `grafana_data` persiste /var/lib/grafana (banco interno com
# users/prefs) — e é por ISSO que os dashboards NÃO podem morar lá dentro:
# Docker só popula volume nomeado a partir da imagem no PRIMEIRO uso; com o
# volume já existente, um JSON novo baked em /var/lib/grafana/dashboards
# NUNCA aparecia em rebuild (footgun pego no mapa do dashboard RED, 35.13.0).
# Os dashboards agora vivem em /etc/grafana/dashboards (não-sombreado —
# mudanças propagam em todo rebuild) e o provider aponta pra lá.
FROM grafana/grafana:11.3.0
COPY provisioning /etc/grafana/provisioning
COPY dashboards /etc/grafana/dashboards
