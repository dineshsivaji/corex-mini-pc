# Caddy — LAN-only HTTPS

Real Let's Encrypt certificates for services on the mini PC, reachable **only inside the home
network**. Nothing is exposed to the internet: no port forwarding, no Cloudflare Tunnel, no proxy.

| URL | Goes to |
|---|---|
| `https://voice.home.dineshsivaji.dev` | `127.0.0.1:8090` — chatgpt_api (voice page at `/voice`) |
| `https://ha.home.dineshsivaji.dev` | `127.0.0.1:8123` — Home Assistant |

## How it works

```
Phone ──DNS lookup──► Cloudflare DNS: *.home.dineshsivaji.dev = 192.168.1.50 (DNS only, grey cloud)
  │
  └──HTTPS──► 192.168.1.50:443 (ufw: LAN only) ──► Caddy ──► service by hostname
```

- **Certificate:** wildcard `*.home.dineshsivaji.dev` via the ACME **DNS-01** challenge. Caddy uses a
  Cloudflare API token to create a temporary TXT record; renewal (~every 60 days) is automatic.
  Only DNS lookups and the TXT record touch Cloudflare — traffic stays on the LAN.
- **LAN-only:** the DNS record points to a private IP, `ufw` allows 80/443 only from
  `192.168.1.0/24`, and the Caddyfile aborts any request not from `private_ranges`.
- **Image:** official `caddy:2` rebuilt with the `caddy-dns/cloudflare` plugin (see `Dockerfile`).
- **Network:** `network_mode: host`, so upstreams are plain `127.0.0.1:<port>`.

## Setup

1. **Cloudflare API token** — My Profile → API Tokens → Create Token → *Edit zone DNS* template →
   Zone Resources: Include → Specific zone → `dineshsivaji.dev`.
2. **DNS record** — in the `dineshsivaji.dev` zone:
   `A  *.home  192.168.1.50`  **Proxy: DNS only (grey cloud)**.
3. **Firewall** (LAN only):
   ```bash
   sudo ufw allow from 192.168.1.0/24 to any port 443 proto tcp comment 'Caddy HTTPS (LAN only)'
   sudo ufw allow from 192.168.1.0/24 to any port 80 proto tcp comment 'Caddy HTTP->HTTPS (LAN only)'
   ```
4. **Secrets** — `.env` is git-ignored; never commit it:
   ```bash
   cp .env.example .env && chmod 600 .env   # set LOCAL_DOMAIN and CF_API_TOKEN
   ```
5. **Run:**
   ```bash
   docker compose up -d --build
   docker logs -f caddy        # expect: certificate obtained successfully
   ```
6. **Home Assistant** must trust the proxy, or it answers `400 Bad Request`
   (in `configuration.yaml`):
   ```yaml
   http:
     use_x_forwarded_for: true
     trusted_proxies:
       - 127.0.0.1
       - ::1
   ```
   Keep HA's *internal URL* as `http://192.168.1.50:8123` — the Nest Mini (Google Cast) fetches TTS
   audio from it.

## Adding another LAN-only service

Add a block inside the `*.{$LOCAL_DOMAIN}` site in `Caddyfile`, then `docker compose restart caddy`:

```
@photos host photos.{$LOCAL_DOMAIN}
handle @photos {
    reverse_proxy 127.0.0.1:2283
}
```
The wildcard certificate already covers the new name.

## Checks

```bash
dig +short voice.home.dineshsivaji.dev        # → 192.168.1.50 (if empty: router DNS-rebind protection)
curl -s https://voice.home.dineshsivaji.dev/health
```

## Not in git

`.env` (Cloudflare token), `data/` (certificates + ACME account key), `config/` — see the repo `.gitignore`.
