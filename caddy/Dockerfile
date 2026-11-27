# Caddy with the Cloudflare DNS plugin (needed for DNS-01 certificates without opening any ports)
FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
