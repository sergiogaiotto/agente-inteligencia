# Grafana com provisioning + dashboards embutidos na imagem.
# Volume nomeado `grafana_data` continua persistindo /var/lib/grafana
# (banco interno do Grafana com users, prefs, etc.) — mas dashboards JSONs
# vivem dentro da imagem e podem ser versionados via git.
FROM grafana/grafana:11.3.0
COPY provisioning /etc/grafana/provisioning
COPY dashboards /var/lib/grafana/dashboards
