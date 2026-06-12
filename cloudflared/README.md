
1) cloudflared tunnel login
2) cloudflared tunnel create immich-tunnel
3) cloudflared tunnel route dns immich-tunnel immich.dineshsivaji.dev

4) Updated the config as follows

➜  ~ cat  ~/.cloudflared/config.yml
tunnel: immich-tunnel
credentials-file: /home/dinesh/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: immich.dineshsivaji.dev
    service: http://localhost:2283
  - service: http_status:404

4) cloudflared tunnel run immich-tunnel # to run one time 
5) sudo cloudflared --config /home/dinesh/.cloudflared/config.yml service install
2026-06-12T11:52:05Z INF Using Systemd
2026-06-12T11:52:08Z INF Linux service for cloudflared installed successfully

6) sudo systemctl start cloudflared
7) sudo systemctl enable cloudflared
8) systemctl status cloudflared
