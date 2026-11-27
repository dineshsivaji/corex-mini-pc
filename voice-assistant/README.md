# Voice assistant — ask ChatGPT, answer on the Nest Mini

```
Phone (/voice page) / HA Assist / ESP32
   │ audio
   ▼
faster-whisper (wyoming-whisper :10300) ── text ──► chatgpt_api (:8090)
                                                     │  Chromium on Xvfb → chatgpt.com
                                                     ▼  answer
Home Assistant tts.speak → Piper (wyoming-piper :10200) → MP3 → Nest Mini (Google Cast)
```

| Path | What |
|---|---|
| `chatgpt_api.py` | FastAPI service: keeps one chatgpt.com session in Chromium, voice page, STT + speak |
| `Dockerfile`, `docker-entrypoint.sh`, `docker-compose.yml`, `requirements.txt` | Container for `chatgpt_api` (auto-starts on boot) |
| `.env.example` | Settings template — copy to `.env` (git-ignored) |
| `voice-stack/docker-compose.yml` | Whisper (STT) + Piper (TTS) containers for HA's Wyoming integration |
| `run_chatgpt_api.sh` | Run without Docker (venv + host Xvfb on `:99`) — for development |
| `chatgpt_browser.py` | Standalone CLI version (one question at a time), for experimenting |

HTTPS for the voice page comes from `../caddy` (`https://voice.home.dineshsivaji.dev`).
HA script/automation/config snippets are in `../homeassitant` (`voice_ask_chatgpt*.yaml`).

## Run

```bash
# Speech services (once)
cd voice-stack && docker compose up -d && cd ..

# chatgpt_api
cp .env.example .env && chmod 600 .env     # set HA_TOKEN
docker compose up -d --build
docker logs -f chatgpt-api
```

`restart: unless-stopped` + Docker enabled at boot ⇒ it comes back after a reboot.
Runtime state lives in `./data/` (git-ignored):
- `data/chatgpt-browser-profile/` — Chromium profile/cookies (keeps the bot check passed)
- `data/debug/` — screenshot + HTML saved when the chat page fails to load

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /` | Text question page |
| `GET /voice` | Phone voice page: tap to talk (https) or phone recorder upload (http), replay buttons |
| `GET/POST /ask` | `?q=…` or `{"question": …}` → `{"answer", "seconds"}` |
| `POST /voice/ask` | multipart `audio` (+ `speak`) → `{"heard", "answer", "repeated", "spoken", "seconds"}` |
| `POST /voice/speak` | `{"text": …}` speaks it; `{}` repeats the last answer |
| `GET /last` | Last question/answer (from any client) |
| `GET /health` | `browser_open`, `ready`, `busy`, `idle_seconds`, `closes_in_seconds` |
| `GET /screenshot` | What the hidden browser shows (when open) |
| `POST /reload` | Fresh chat now (opens the browser if closed) |

## Behaviour

- **Browser on demand:** Chromium launches on the first request, closes after `IDLE_CLOSE_MIN`
  (default 10) idle minutes, and relaunches on the next request (~2 s extra).
- **Headed, not headless:** chatgpt.com bot-checks headless Chromium, so it runs on a virtual Xvfb
  display (`:100` in the container).
- **Login modal:** closed automatically; if it sticks, the page is reloaded and the question retried.
- **Repeat:** saying "say that again" / "repeat" / "what did you say" replays the last answer without
  asking ChatGPT.
- **Persona "Ollie the Owl":** every question is prefixed with a kid-friendly character prompt
  (ages 5-7: excited, simple words, comparisons to everyday things, one fun fact, a small question
  back, under 50 words). Override with `PREFIX=` in `.env` (empty = send questions as-is).
- **Speech clean-up:** answers are passed through `clean_for_speech()` before they are returned or
  spoken - emojis/symbols and markdown are stripped, `°C`, `&`, `~`, `%` are spelled out.
  The original text is kept as `answer_raw` in `/ask` responses.
- One question at a time (queued); one retry with a fresh browser on failure.

## Troubleshooting

```bash
curl -s http://127.0.0.1:8090/health
curl -s http://127.0.0.1:8090/screenshot -o shot.png      # when the browser is open
ls data/debug/                                            # saved failures
```
Selectors for chatgpt.com are in `SELECTORS` at the top of `chatgpt_api.py` — update them when the
site changes.
