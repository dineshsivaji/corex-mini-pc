#!/usr/bin/env python3
"""
HTTP API that keeps one chatgpt.com browser session open in the background
and answers questions through it.

Setup:
    pip install fastapi uvicorn playwright
    playwright install chromium

Run:
    python chatgpt_api.py                    # desktop (opens a browser window)
    xvfb-run -a python chatgpt_api.py        # headless Ubuntu server (virtual display)

Use:
    open http://localhost:8090/              # simple web page: type a question, see the answer
    curl "http://localhost:8090/ask?q=Why+do+cats+purr"
    curl -X POST http://localhost:8090/ask -H "Content-Type: application/json" \
         -d '{"question": "Why do cats purr?"}'
    curl http://localhost:8090/health

Voice page (phone):
    open http://<server>:8090/voice          # record → Whisper → ChatGPT → spoken on the Nest Mini
    needs: pip install python-multipart ; ffmpeg ; wyoming-whisper container ; HA_TOKEN for speaking

Env vars (optional):
    PORT=8090  HOST=0.0.0.0  IDLE_CLOSE_MIN=10 (close Chromium after N idle minutes)  HEADLESS=0
    PREFIX="..."  (instruction added before each question; set PREFIX="" to disable)
    WHISPER_HOST=127.0.0.1  WHISPER_PORT=10300  STT_LANG=en
    HA_URL=http://127.0.0.1:8123  HA_TOKEN=<long-lived token>
    HA_SPEAKER=media_player.master_bedroom_speaker  HA_TTS=tts.piper  TTS_LANG=en_US  TTS_VOICE=
"""

import asyncio
import contextlib
import json
import os
import re
import tempfile
import time
import unicodedata
import urllib.request

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from pydantic import BaseModel

URL = "https://chatgpt.com/"
PROFILE_DIR = os.environ.get("PROFILE_DIR", "./.chatgpt-browser-profile")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8090"))
IDLE_CLOSE_SECONDS = float(os.environ.get("IDLE_CLOSE_MIN", os.environ.get("IDLE_RELOAD_MIN", "10"))) * 60
HEADLESS = os.environ.get("HEADLESS", "0") == "1"  # headless is often blocked; prefer xvfb-run
REPLY_TIMEOUT_S = 90
DEBUG_DIR = os.environ.get("DEBUG_DIR", "./debug")

# Voice: speech-to-text via the wyoming-whisper container, speech via Home Assistant
WHISPER_HOST = os.environ.get("WHISPER_HOST", "127.0.0.1")
WHISPER_PORT = int(os.environ.get("WHISPER_PORT", "10300"))
STT_LANG = os.environ.get("STT_LANG", "en")
HA_URL = os.environ.get("HA_URL", "http://127.0.0.1:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_SPEAKER = os.environ.get("HA_SPEAKER", "media_player.master_bedroom_speaker")
HA_TTS = os.environ.get("HA_TTS", "tts.piper")
TTS_LANG = os.environ.get("TTS_LANG", "en_US")
TTS_VOICE = os.environ.get("TTS_VOICE", "")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Persona added before every question (the chat resets after idle, so it's re-sent each time).
PREFIX = os.environ.get(
    "PREFIX",
    "You are Ollie, a cheerful owl who explains things to kids aged 5 to 7. "
    "Be excited, warm and a little silly, and vary how you start. "
    "Use simple words, compare to things kids know, share one amazing fact, "
    "and end with a tiny question back. Under 50 words, 2-3 short sentences, "
    "no emojis, lists or markdown. For scary or grown-up topics, suggest asking a grown-up. "
    "Question: ",
)

# ---------- make answers safe for text-to-speech ----------
_SPEECH_REPLACEMENTS = [
    (re.compile(r"\s*°\s*C\b"), " degrees Celsius"),
    (re.compile(r"\s*°\s*F\b"), " degrees Fahrenheit"),
    (re.compile(r"\s*°"), " degrees"),
    (re.compile(r"\s*&\s*"), " and "),
    (re.compile(r"~\s*(?=\d)"), "about "),
    (re.compile(r"(?<=\d)\s*%"), " percent"),
    (re.compile(r"\be\.g\.", re.IGNORECASE), "for example"),
]
_MARKDOWN = [
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),           # [text](url) → text
    (re.compile(r"(\*\*|__|\*|`)"), ""),                      # bold / italics / code marks
    (re.compile(r"(?m)^\s*(#{1,6}\s+|[-•*]\s+|\d+[.)]\s+)"), ""),  # headings, bullets, numbered lists
]


def clean_for_speech(text: str) -> str:
    """Strip emojis, symbols and markdown so Piper reads the answer naturally."""
    for pattern, repl in _SPEECH_REPLACEMENTS + _MARKDOWN:
        text = pattern.sub(repl, text)
    kept = []
    for ch in text:
        cp = ord(ch)
        if ch in "‍︎️" or 0x1F3FB <= cp <= 0x1F3FF:  # ZWJ, variation selectors, skin tones
            continue
        if unicodedata.category(ch) in ("So", "Sk", "Cs", "Co"):    # emojis & other symbols
            continue
        kept.append(ch)
    text = "".join(kept)
    # Former list items / lines: end each with punctuation so they don't run together when spoken.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    text = " ".join(ln if ln[-1] in ".!?,;:" else ln + "." for ln in lines)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)                 # no space before punctuation
    text = re.sub(r"([!?.]){3,}", r"\1", text)                   # "!!!" → "!"
    return text.strip()

SELECTORS = {
    "input": 'textarea[name="prompt"]:visible, textarea[aria-label="Chat with ChatGPT"]:visible, '
             "#prompt-textarea:visible",
    "send": 'button[data-testid="send-button"], button[aria-label="Send prompt"]',
    "assistant": 'li[data-message-role="assistant"], [data-message-author-role="assistant"]',
    "complete_attr": "data-message-complete",
    "reply_text": "[data-assistant-markdown-root]",
    # "Log in / sign up" modal (bottom sheet) close button.
    "login_close": 'button[data-bottom-sheet-close]:visible, '
                   'button:has(use[href="#lightweight-auth-close"]):visible',
}


class LoginModalStuck(Exception):
    """The login modal would not close; caller should reload the page."""


class ChatGPTBrowser:
    """Owns the browser. All page access goes through self.lock (one query at a time)."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.last_activity = time.time()
        self.last_reload = time.time()
        self.queries = 0
        self.ready = False
        self.last: dict | None = None  # {"question", "answer", "at"} - for "repeat that"
        self._pw = self._ctx = self.page = None

    # ---------- lifecycle ----------
    # The browser is opened on demand (first request) and closed after IDLE_CLOSE_SECONDS
    # without requests, so Chromium isn't using RAM/CPU while nobody is asking anything.
    async def start(self) -> None:
        self._pw = await async_playwright().start()
        print(f"[browser] ready to launch on first request (closes after "
              f"{IDLE_CLOSE_SECONDS / 60:g} min idle)")

    @property
    def is_open(self) -> bool:
        return self._ctx is not None

    async def _open(self) -> None:
        """Launch Chromium and load chatgpt.com. Caller must hold self.lock."""
        t0 = time.time()
        self._ctx = await self._pw.chromium.launch_persistent_context(PROFILE_DIR, headless=HEADLESS)
        self._ctx.on("close", lambda _: self._mark_closed())  # e.g. Chromium crashed
        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        await self._fresh_chat()
        print(f"[browser] opened in {time.time() - t0:.1f}s")

    def _mark_closed(self) -> None:
        self._ctx = self.page = None
        self.ready = False

    async def _close(self) -> None:
        """Close Chromium. Caller must hold self.lock."""
        ctx = self._ctx
        self._mark_closed()
        if ctx:
            with contextlib.suppress(Exception):
                await ctx.close()

    async def _ensure_open(self) -> None:
        if not self.is_open:
            await self._open()
        elif not self.ready:
            await self._fresh_chat()  # previous load failed: try again

    async def _save_debug(self, label: str) -> str:
        """Save a screenshot + HTML of the current page; return a short description."""
        os.makedirs(DEBUG_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = os.path.join(DEBUG_DIR, f"{stamp}-{label}")
        with contextlib.suppress(Exception):
            await self.page.screenshot(path=base + ".png", full_page=True)
        with contextlib.suppress(Exception):
            with open(base + ".html", "w") as f:
                f.write(await self.page.content())
        title = ""
        with contextlib.suppress(Exception):
            title = await self.page.title()
        return f"title={title!r} url={self.page.url} saved={base}.png/.html"

    async def stop(self) -> None:
        await self._close()
        with contextlib.suppress(Exception):
            await self._pw.stop()

    async def _fresh_chat(self, attempts: int = 2) -> None:
        """Go to the home page: a clean, new conversation (reloads again if the login modal sticks)."""
        for attempt in range(1, attempts + 1):
            await self.page.goto(URL, wait_until="domcontentloaded")
            await asyncio.sleep(1)  # let any modal render
            try:
                await self._dismiss_popups()
            except LoginModalStuck:
                print(f"[modal] still visible after load (attempt {attempt}), reloading")
                continue
            try:
                await self._input().wait_for(state="visible", timeout=30_000)
            except PWTimeout:
                info = await self._save_debug("no-input")
                print(f"[load] chat input not found (attempt {attempt}): {info}")
                continue
            self.ready = True
            self.last_reload = time.time()
            return
        self.ready = False
        raise RuntimeError("Chat page did not load (bot check, login wall, or selectors changed) "
                           f"- check {DEBUG_DIR}/")

    async def close_if_idle(self) -> bool:
        """Called periodically: close Chromium if nobody has asked anything for a while."""
        if not self.is_open or self.lock.locked():
            return False
        if time.time() - self.last_activity < IDLE_CLOSE_SECONDS:
            return False
        async with self.lock:
            await self._close()
        return True

    # ---------- page helpers ----------
    def _input(self):
        return self.page.locator(SELECTORS["input"]).first

    async def _close_login_modal(self) -> None:
        """If the login modal is showing, click its X. Raise LoginModalStuck if it stays open."""
        close = self.page.locator(SELECTORS["login_close"])
        if not await close.count():
            return
        print("[modal] login modal detected, closing")
        with contextlib.suppress(Exception):
            await close.first.click(timeout=3_000)
        for _ in range(10):  # up to ~2 s for it to disappear
            await asyncio.sleep(0.2)
            if not await close.count():
                return
        raise LoginModalStuck()

    async def _dismiss_popups(self) -> None:
        await self._close_login_modal()
        for text in ("Stay logged out", "Maybe later", "Dismiss"):
            btn = self.page.get_by_text(text, exact=True)
            with contextlib.suppress(Exception):
                if await btn.count() and await btn.first.is_visible():
                    await btn.first.click()
                    await asyncio.sleep(0.5)

    async def _wait_for_reply(self, before: int) -> str:
        msgs = self.page.locator(SELECTORS["assistant"])
        deadline = time.time() + REPLY_TIMEOUT_S

        while await msgs.count() <= before:
            if time.time() > deadline:
                raise TimeoutError("No reply appeared")
            await self._dismiss_popups()
            await asyncio.sleep(0.3)

        reply = msgs.last
        last_text, stable_since = "", time.time()
        while time.time() < deadline:
            await self._close_login_modal()  # modal can pop up mid-answer
            body = reply.locator(SELECTORS["reply_text"])
            text = (await (body.first if await body.count() else reply).inner_text()).strip()
            done = await reply.get_attribute(SELECTORS["complete_attr"]) is not None
            if text != last_text:
                last_text, stable_since = text, time.time()
            quiet = time.time() - stable_since
            if text and ((done and quiet > 1) or quiet > 3):
                return " ".join(text.split())
            await asyncio.sleep(0.3)
        if last_text:
            return " ".join(last_text.split())
        raise TimeoutError("Reply did not finish")

    async def _ask_once(self, question: str) -> str:
        await self._dismiss_popups()
        box = self._input()
        await box.wait_for(state="visible", timeout=30_000)
        before = await self.page.locator(SELECTORS["assistant"]).count()

        await box.click()
        await box.fill(PREFIX + question)
        send = self.page.locator(SELECTORS["send"]).first
        try:
            await send.wait_for(state="visible", timeout=3_000)
            await send.click()
        except PWTimeout:
            await box.press("Enter")

        return await self._wait_for_reply(before)

    # ---------- public ----------
    async def ask(self, question: str) -> str:
        async with self.lock:
            self.last_activity = time.time()
            try:
                await self._ensure_open()  # opens Chromium + chatgpt.com if it was closed
                answer = await self._ask_once(question)
            except Exception as e:
                # Modal stuck / page changed / browser crashed: start over and retry once.
                print(f"[ask] {type(e).__name__}: {e} -> restarting browser and retrying")
                await self._close()
                await self._open()
                answer = await self._ask_once(question)
            self.queries += 1
            self.last_activity = time.time()
            spoken = clean_for_speech(answer) or answer
            self.last = {"question": question, "answer": spoken, "answer_raw": answer, "at": time.time()}
            return spoken


browser = ChatGPTBrowser()


async def idle_closer() -> None:
    while True:
        await asyncio.sleep(30)
        with contextlib.suppress(Exception):
            if await browser.close_if_idle():
                print(f"[idle] no requests for {IDLE_CLOSE_SECONDS / 60:g} min - browser closed")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    await browser.start()
    task = asyncio.create_task(idle_closer())
    yield
    task.cancel()
    await browser.stop()


app = FastAPI(title="ChatGPT browser API", lifespan=lifespan)


class AskRequest(BaseModel):
    question: str


async def _answer(question: str) -> dict:
    question = question.strip()
    if not question:
        raise HTTPException(400, "question is empty")
    t0 = time.time()
    try:
        answer = await browser.ask(question)
    except Exception as e:
        raise HTTPException(502, f"browser error: {e}") from e
    raw = (browser.last or {}).get("answer_raw", answer)
    return {"question": question, "answer": answer, "answer_raw": raw, "seconds": round(time.time() - t0, 1)}


@app.post("/ask")
async def ask_post(req: AskRequest) -> dict:
    return await _answer(req.question)


@app.get("/ask")
async def ask_get(q: str) -> dict:
    return await _answer(q)


@app.get("/health")
async def health() -> dict:
    now = time.time()
    idle = now - browser.last_activity
    return {
        "browser_open": browser.is_open,
        "ready": browser.ready,
        "busy": browser.lock.locked(),
        "queries": browser.queries,
        "idle_seconds": round(idle),
        "closes_in_seconds": max(0, round(IDLE_CLOSE_SECONDS - idle)) if browser.is_open else None,
        "url": browser.page.url if browser.page else None,
        "last_question": browser.last["question"] if browser.last else None,
    }


@app.get("/screenshot")
async def screenshot() -> Response:
    """What the hidden browser is showing right now."""
    if not browser.page:
        raise HTTPException(409, "browser is closed (idle) - it opens on the next question")
    png = await browser.page.screenshot(full_page=True)
    return Response(png, media_type="image/png")


@app.post("/reload")
async def reload_now() -> dict:
    """Start a fresh chat now (opens the browser if it was closed)."""
    async with browser.lock:
        browser.last_activity = time.time()
        if browser.is_open:
            await browser._fresh_chat()
        else:
            await browser._open()
    return {"reloaded": True}


@app.get("/last")
async def last_answer() -> dict:
    """The most recent question/answer (from any client: /ask, /voice, Home Assistant)."""
    if not browser.last:
        raise HTTPException(404, "nothing asked yet")
    return {**browser.last, "age_seconds": round(time.time() - browser.last["at"])}


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Ask</title>
<style>
 body{font:17px/1.5 system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px}
 form{display:flex;gap:8px} input{flex:1;padding:10px;font:inherit}
 button{padding:10px 16px;font:inherit} .qa{margin-top:20px;padding:12px;border-radius:8px;background:#8881}
 .q{font-weight:600} small{opacity:.6}
</style></head><body>
<h2>Ask</h2>
<form id="f"><input id="q" placeholder="Type a question…" autofocus><button>Ask</button></form>
<div id="log"></div>
<script>
const f=document.getElementById('f'), q=document.getElementById('q'), log=document.getElementById('log');
f.onsubmit=async e=>{e.preventDefault(); const text=q.value.trim(); if(!text) return;
 const box=document.createElement('div'); box.className='qa';
 box.innerHTML='<div class="q"></div><div class="a">Thinking…</div>';
 box.querySelector('.q').textContent=text; log.prepend(box); q.value='';
 try{ const r=await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:text})});
  const d=await r.json(); const a=box.querySelector('.a');
  if(r.ok){a.textContent=d.answer; a.insertAdjacentHTML('beforeend',' <small>('+d.seconds+'s)</small>');}
  else a.textContent='Error: '+(d.detail||r.status);
 }catch(err){box.querySelector('.a').textContent='Error: '+err;}
};
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PAGE


# ======================= Voice: record → text → ChatGPT → Nest Mini =======================

async def to_pcm16k(audio: bytes) -> bytes:
    """Any phone recording (m4a/3gp/webm/ogg/wav/mp3) → raw 16 kHz mono 16-bit PCM via ffmpeg."""
    with tempfile.NamedTemporaryFile(suffix=".audio") as f:  # file, not pipe: m4a needs seeking
        f.write(audio)
        f.flush()
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", f.name,
            "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        pcm, err = await proc.communicate()
    if proc.returncode != 0 or not pcm:
        raise RuntimeError(f"could not decode audio: {err.decode(errors='ignore')[:200]}")
    return pcm


async def whisper_transcribe(pcm: bytes) -> str:
    """Send PCM to the wyoming-whisper container (Wyoming protocol over TCP) and return the text."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(WHISPER_HOST, WHISPER_PORT), 5)
    fmt = {"rate": 16000, "width": 2, "channels": 1}

    def send(event_type: str, data: dict | None = None, payload: bytes = b"") -> None:
        header: dict = {"type": event_type}
        if data is not None:
            header["data"] = data
        if payload:
            header["payload_length"] = len(payload)
        writer.write((json.dumps(header) + "\n").encode())
        if payload:
            writer.write(payload)

    try:
        send("transcribe", {"language": STT_LANG})
        send("audio-start", fmt)
        for i in range(0, len(pcm), 32_000):  # 1 s chunks
            send("audio-chunk", fmt, pcm[i:i + 32_000])
        send("audio-stop", {})
        await writer.drain()

        while True:
            line = await asyncio.wait_for(reader.readline(), 60)
            if not line:
                raise RuntimeError("whisper closed the connection")
            header = json.loads(line)
            data = header.get("data") or {}
            if header.get("data_length"):
                data.update(json.loads(await reader.readexactly(header["data_length"])))
            if header.get("payload_length"):
                await reader.readexactly(header["payload_length"])
            if header.get("type") == "transcript":
                return (data.get("text") or "").strip()
    finally:
        writer.close()


def _ha_speak_blocking(text: str) -> None:
    body = {
        "entity_id": HA_TTS,
        "media_player_entity_id": HA_SPEAKER,
        "message": text,
        "language": TTS_LANG,
        "cache": False,
    }
    if TTS_VOICE:
        body["options"] = {"voice": TTS_VOICE}
    req = urllib.request.Request(
        f"{HA_URL}/api/services/tts/speak",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


async def ha_speak(text: str) -> None:
    if not HA_TOKEN:
        raise RuntimeError("HA_TOKEN not set")
    await asyncio.to_thread(_ha_speak_blocking, text)


# Short phrases that mean "say the last answer again" (checked before asking ChatGPT).
REPEAT_RE = re.compile(
    r"^(please |can you |could you )?"
    r"(repeat( that| it| please| the answer)?|say (that|it) again|again|one more time|"
    r"what did you say|pardon( me)?|sorry what|come again|tell me again)"
    r"( please)?[.!?]*$",
    re.IGNORECASE,
)


def is_repeat_request(text: str) -> bool:
    return bool(REPEAT_RE.match(text.strip().strip(".,!? ")))


class SpeakRequest(BaseModel):
    text: str | None = None  # omit → repeat the last answer


@app.post("/voice/speak")
async def voice_speak(req: SpeakRequest | None = None) -> dict:
    """Speak text on the Nest Mini. With no text: repeat the last answer."""
    text = (req.text if req and req.text else None) or (browser.last or {}).get("answer")
    if not text:
        raise HTTPException(404, "nothing to repeat yet")
    text = clean_for_speech(text) or text
    try:
        await ha_speak(text)
    except Exception as e:
        raise HTTPException(502, f"speaker failed: {e}") from e
    return {"spoken": True, "text": text}


@app.post("/voice/ask")
async def voice_ask(audio: UploadFile = File(...), speak: bool = Form(True)) -> dict:
    t0 = time.time()
    raw = await audio.read()
    if not raw:
        raise HTTPException(400, "empty recording")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "recording too large")

    try:
        text = await whisper_transcribe(await to_pcm16k(raw))
    except Exception as e:
        raise HTTPException(502, f"speech-to-text failed: {e}") from e
    t_stt = time.time()
    if not text:
        raise HTTPException(422, "I couldn't hear any words - try again closer to the mic")

    repeated = False
    if is_repeat_request(text) and browser.last:
        answer, repeated = browser.last["answer"], True  # "say that again" → no ChatGPT call
    else:
        try:
            was_open = browser.is_open
            answer = await browser.ask(text)
            if not was_open:
                print("[voice] browser was closed (idle) - opened for this question")
        except Exception as e:
            raise HTTPException(502, f"ChatGPT failed: {e}") from e
    t_ai = time.time()

    spoken, speak_error = False, None
    if speak:
        try:
            await ha_speak(answer)
            spoken = True
        except Exception as e:
            speak_error = str(e)
    print(f"[voice] heard={text!r} answer={answer[:60]!r}... spoken={spoken} {speak_error or ''}")

    return {
        "heard": text,
        "answer": answer,
        "repeated": repeated,
        "spoken": spoken,
        "speak_error": speak_error,
        "seconds": {
            "stt": round(t_stt - t0, 1),
            "chatgpt": round(t_ai - t_stt, 1),
            "speak": round(time.time() - t_ai, 1),
            "total": round(time.time() - t0, 1),
        },
    }


VOICE_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Curious Buddy</title>
<style>
 body{font:18px/1.5 system-ui,sans-serif;max-width:560px;margin:24px auto;padding:0 16px;text-align:center}
 .mic{display:block;width:100%;padding:28px 0;font-size:26px;border:0;border-radius:18px;
      background:#2b7de9;color:#fff;cursor:pointer;user-select:none;-webkit-user-select:none}
 .mic.rec{background:#e5484d} .mic:disabled{opacity:.5}
 label.opt{display:block;margin:14px 0;font-size:16px}
 #status{min-height:1.5em;margin:12px 0;opacity:.8}
 .qa{text-align:left;margin-top:14px;padding:12px 14px;border-radius:12px;background:#8881}
 .q{font-weight:600} small{opacity:.6;display:block;margin-top:4px}
 input[type=file]{display:none}
 .again{margin-top:8px;padding:8px 14px;font:inherit;font-size:15px;border:1px solid #8886;
        border-radius:10px;background:transparent;color:inherit;cursor:pointer}
 #repeat{display:block;width:100%;margin-top:10px;padding:14px 0;font-size:18px}
</style></head><body>
<h2>🦉 Curious Buddy</h2>
<button id="live" class="mic" hidden>🎤 Tap to talk</button>
<label id="uploadBtn" class="mic" for="file" hidden>🎤 Tap to record a question</label>
<input id="file" type="file" accept="audio/*" capture>
<button id="repeat" class="again" disabled>🔁 Say the last answer again</button>
<label class="opt"><input id="speak" type="checkbox" checked> Play answer on the Nest Mini</label>
<div id="status"></div>
<div id="log"></div>
<script>
const $ = id => document.getElementById(id);
const status = t => $('status').textContent = t;

// Replay on the Nest Mini: no text = last answer (server side); with text = that specific answer.
async function play(text) {
  status('Playing on the Nest Mini…');
  try {
    const r = await fetch('/voice/speak', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                           body: JSON.stringify(text ? {text} : {})});
    const d = await r.json();
    status(r.ok ? '🔊 Played again' : 'Speaker: ' + (d.detail || r.status));
  } catch (e) { status('Error: ' + e); }
}
$('repeat').addEventListener('click', () => play());
fetch('/last').then(r => { if (r.ok) $('repeat').disabled = false; });

async function send(blob, name) {
  status('Listening to your recording…');
  const fd = new FormData();
  fd.append('audio', blob, name);
  fd.append('speak', $('speak').checked);
  const box = document.createElement('div'); box.className = 'qa';
  box.innerHTML = '<div class="q">…</div><div class="a">Thinking…</div><small></small>';
  $('log').prepend(box);
  try {
    const r = await fetch('/voice/ask', {method: 'POST', body: fd});
    const d = await r.json();
    if (!r.ok) { box.querySelector('.q').textContent = '⚠️'; box.querySelector('.a').textContent = d.detail || r.status; status(''); return; }
    box.querySelector('.q').textContent = '“' + d.heard + '”' + (d.repeated ? '  (repeating)' : '');
    box.querySelector('.a').textContent = d.answer;
    const s = d.seconds;
    box.querySelector('small').textContent =
      `heard ${s.stt}s · answer ${s.chatgpt}s · speak ${s.speak}s · total ${s.total}s` +
      (d.spoken ? ' · 🔊 played on Nest Mini' : (d.speak_error ? ' · speaker: ' + d.speak_error : ''));
    const again = document.createElement('button');
    again.className = 'again'; again.textContent = '🔁 Play this again';
    again.onclick = () => play(d.answer);
    box.appendChild(again);
    $('repeat').disabled = false;
    status('');
  } catch (e) { box.querySelector('.a').textContent = 'Error: ' + e; status(''); }
}

// Fallback: the phone's own recorder app (works over plain http).
function useUpload(reason) {
  $('live').hidden = true;
  $('uploadBtn').hidden = false;
  if (reason) status(reason);
}
$('file').addEventListener('change', e => {
  const f = e.target.files[0]; if (f) send(f, f.name || 'question.m4a'); e.target.value = '';
});

// Live mic only works on https (or with Chrome's insecure-origin flag). Tap to start, tap to stop.
if (window.isSecureContext && navigator.mediaDevices && window.MediaRecorder) {
  const btn = $('live'); btn.hidden = false;
  const idle = () => { btn.classList.remove('rec'); btn.textContent = '🎤 Tap to talk'; btn.disabled = false; };
  idle();
  let rec = null, chunks = [], stream = null, timer = null;

  btn.addEventListener('click', async () => {
    if (rec && rec.state === 'recording') { rec.stop(); return; }   // second tap = send
    btn.disabled = true; status('Allow the microphone if asked…');
    try {
      stream = await navigator.mediaDevices.getUserMedia({audio: true});
    } catch (err) {
      idle(); useUpload('Microphone blocked (' + err.name + ') - using the recorder app instead.');
      return;
    }
    chunks = [];
    rec = new MediaRecorder(stream);
    rec.ondataavailable = ev => { if (ev.data.size) chunks.push(ev.data); };
    rec.onstop = () => {
      clearTimeout(timer); stream.getTracks().forEach(t => t.stop()); idle();
      const type = rec.mimeType || 'audio/webm';
      const blob = new Blob(chunks, {type});
      if (blob.size < 1000) { status('That was too short - tap, speak, then tap again.'); return; }
      send(blob, type.includes('mp4') ? 'question.m4a' : 'question.webm');
    };
    rec.start();
    btn.disabled = false; btn.classList.add('rec'); btn.textContent = '⏹ Recording… tap to send';
    status('Speak now');
    timer = setTimeout(() => rec.state === 'recording' && rec.stop(), 20000);  // safety stop at 20 s
  });
} else {
  useUpload();
}
</script></body></html>"""


@app.get("/voice", response_class=HTMLResponse)
async def voice_page() -> str:
    return VOICE_PAGE


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
