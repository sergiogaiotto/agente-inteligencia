# Caddy reverse proxy com Caddyfile embutido na imagem.
# Eliminamos o bind mount externo pra deploys (Hostinger etc.) não dependerem
# de copiar arquivos avulsos do repo — basta `docker compose up --build`.
FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
