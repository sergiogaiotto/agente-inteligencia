# Promtail (tail dos logs do Docker → Loki) com config embutida.
# Mantém os bind mounts de SISTEMA (/var/lib/docker/containers, /var/run/docker.sock)
# no compose — esses são paths do host que sempre existem em qualquer VPS.
FROM grafana/promtail:3.2.0
COPY promtail-config.yaml /etc/promtail/config.yaml
