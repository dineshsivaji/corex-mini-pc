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
    PERSONA_FILE=persona.txt  (sent once at the start of every new chat)
    PREFIX="..."  (optional text added before each question; default none)
    WHISPER_HOST=127.0.0.1  WHISPER_PORT=10300  STT_LANG=en
    HA_URL=http://127.0.0.1:8123  HA_TOKEN=<long-lived token>
    HA_SPEAKER=media_player.master_bedroom_speaker  HA_TTS=tts.piper  TTS_LANG=en_US  TTS_VOICE=
"""

import asyncio
import contextlib
import json
import os
import random
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

# Persona: sent ONCE as the first message of every new chat (browser start, idle reopen, retry).
# Edit the file and the next new chat uses it - no rebuild needed (it's mounted in Docker).
PERSONA_FILE = os.environ.get(
    "PERSONA_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona.txt"))
PRIME_TIMEOUT_S = 60

# Optional text added before every question (default: none - questions are sent as-is).
PREFIX = os.environ.get("PREFIX", "")

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
        self.primed = False            # persona sent in the current chat
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
        self.primed = False

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
            try:
                await self._prime()  # new chat → give it the persona once
            except Exception as e:
                info = await self._save_debug("prime-failed")
                print(f"[persona] priming failed (attempt {attempt}): {e} - {info}")
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

    async def _send(self, text: str) -> str:
        """Type a message into the chat, send it, and return the reply text."""
        await self._dismiss_popups()
        box = self._input()
        await box.wait_for(state="visible", timeout=30_000)
        before = await self.page.locator(SELECTORS["assistant"]).count()

        await box.click()
        await box.fill(text)
        send = self.page.locator(SELECTORS["send"]).first
        try:
            await send.wait_for(state="visible", timeout=3_000)
            await send.click()
        except PWTimeout:
            await box.press("Enter")

        return await self._wait_for_reply(before)

    async def _ask_once(self, question: str) -> str:
        return await self._send(PREFIX + question)

    async def _prime(self) -> None:
        """Send the persona as the first message of a new chat (once per chat)."""
        self.primed = False
        try:
            with open(PERSONA_FILE) as f:
                persona = f.read().strip()
        except FileNotFoundError:
            print(f"[persona] {PERSONA_FILE} not found - questions go without a persona")
            return
        if not persona:
            return
        t0 = time.time()
        reply = await asyncio.wait_for(self._send(persona), PRIME_TIMEOUT_S)
        self.primed = True
        print(f"[persona] sent ({len(persona.split())} words) in {time.time() - t0:.1f}s, "
              f"reply: {reply[:40]!r}")

    async def warm_up(self) -> None:
        """At startup: open the browser and prime the chat so the first question is fast."""
        async with self.lock:
            self.last_activity = time.time()  # idle timer starts now
            try:
                await self._ensure_open()
            except Exception as e:
                print(f"[startup] warm-up failed, will retry on first question: {e}")

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
    warm = asyncio.create_task(browser.warm_up())  # open + persona in the background
    task = asyncio.create_task(idle_closer())
    prewarm = asyncio.create_task(_prewarm_phrases())  # Kokoro fixed phrases for /voice-kokoro
    yield
    task.cancel()
    warm.cancel()
    prewarm.cancel()
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
        "persona_sent": browser.primed,
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
    return await _voice_speak(req, ha_speak)


async def _voice_speak(req: SpeakRequest | None, speak_fn) -> dict:
    text = (req.text if req and req.text else None) or (browser.last or {}).get("answer")
    if not text:
        raise HTTPException(404, "nothing to repeat yet")
    text = clean_for_speech(text) or text
    try:
        await speak_fn(text)
    except Exception as e:
        raise HTTPException(502, f"speaker failed: {e}") from e
    return {"spoken": True, "text": text}


@app.post("/voice/ask")
async def voice_ask(audio: UploadFile = File(...), speak: bool = Form(True)) -> dict:
    return await _voice_ask(audio, speak, ha_speak)


async def _voice_ask(audio: UploadFile, speak: bool, speak_fn) -> dict:
    """Recording → Whisper → ChatGPT → speak_fn(answer) on the Nest Mini."""
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
            await speak_fn(answer)
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
 /* Big, high-contrast, black-and-white layout so it reads well on an e-ink screen */
 body{font:28px/1.5 system-ui,sans-serif;max-width:900px;margin:20px auto;padding:0 16px;
      text-align:center;background:#fff;color:#000}
 h2{font-size:44px;margin:10px 0 20px}
 .mic{display:block;width:100%;box-sizing:border-box;padding:40px 0;font-size:42px;font-weight:700;
      border:4px solid #000;border-radius:20px;background:#000;color:#fff;cursor:pointer;
      user-select:none;-webkit-user-select:none}
 .mic.rec{background:#fff;color:#000;border:6px dashed #000}
 .mic:disabled{background:#fff;color:#000;border-style:dotted}
 label.opt{display:block;margin:20px 0;font-size:26px}
 label.opt input{width:32px;height:32px;vertical-align:middle;margin-right:10px}
 #status{min-height:1.5em;margin:16px 0;font-size:30px;font-weight:700}
 .qa{text-align:left;margin-top:20px;padding:18px 20px;border:3px solid #000;border-radius:14px}
 .q{font-weight:700;font-size:30px;margin-bottom:10px}
 .a{font-size:34px;line-height:1.55}
 small{display:block;margin-top:10px;font-size:20px}
 input[type=file]{display:none}
 .again{margin-top:14px;padding:16px 22px;font:inherit;font-size:26px;font-weight:700;
        border:3px solid #000;border-radius:14px;background:#fff;color:#000;cursor:pointer}
 #repeat{display:block;width:100%;margin-top:16px;padding:24px 0;font-size:32px}
 #repeat:disabled{border-style:dotted}
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


# ---------------------------------------------------------------------------
# Trial page /voice-kokoro: kid mode + answers spoken with Kokoro TTS instead of HA's Piper.
# Kokoro makes an MP3 here; we keep it in memory and ask HA to play its URL on the Nest Mini
# (media_player.play_media), so no HA configuration is needed.
#
# One question at a time: a small state machine  idle → thinking → speaking → idle.
# While not idle, new questions/replays get 409. "speaking" ends when HA reports the Nest Mini
# stopped playing our clip (with a time cap in case HA never reports it).
KOKORO_URL = os.environ.get("KOKORO_URL", "http://127.0.0.1:8880").rstrip("/")
KOKORO_VOICE = os.environ.get("KOKORO_VOICE", "af_heart")
KOKORO_SPEED = float(os.environ.get("KOKORO_SPEED", "1.0"))
KOKORO_SLOW_SPEED = float(os.environ.get("KOKORO_SLOW_SPEED", "0.85"))  # the "Slower" button
KOKORO_KEEP = 60  # cached clips (answers + fixed phrases); same text → no re-generation

# Said right after the question was understood, while ChatGPT works (one at random, no repeats).
HEARD_PHRASES = [
    "Ooh, good one!", "Hmm, let me think!", "Let me find out!", "Okay, thinking cap on!",
    "Ooh, let me look in my brain!", "One moment!", "Hmm, let me see!",
]
SAY = {
    "no_words": "Oops, I didn't hear you. Tap and try again!",
    "ai_failed": "Oh no, my brain got tangled! Can you ask me again?",
}
# Quick-reply buttons on the page → what is sent to ChatGPT (same chat, so "that" has context).
QUICK_REPLIES = {
    "more": "Tell me more about that!",
    "why": "Why is that?",
    "easier": "I didn't understand. Can you say it in a simpler way?",
    "quiz": "Ask me one fun quiz question about this, then wait for my answer.",
    "yes": "Yes!",
    "tellme": "I don't know, tell me!",
}


def _lan_base_url() -> str:
    """URL the Nest Mini can fetch audio from: http://<this PC's LAN IP>:PORT (override MEDIA_BASE_URL)."""
    if os.environ.get("MEDIA_BASE_URL"):
        return os.environ["MEDIA_BASE_URL"].rstrip("/")
    import socket
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the LAN interface
        return f"http://{s.getsockname()[0]}:{PORT}"


# ---------- clips (Kokoro audio, cached by text + voice + speed) ----------
_clips: dict[str, dict] = {}  # id -> {"mp3": bytes, "seconds": float}; insertion order = age


def _kokoro_tts_blocking(text: str, speed: float) -> bytes:
    body = {"model": "kokoro", "input": text, "voice": KOKORO_VOICE, "speed": speed,
            "response_format": "mp3", "stream": False}
    req = urllib.request.Request(f"{KOKORO_URL}/v1/audio/speech", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


async def _mp3_seconds(mp3: bytes, text: str, speed: float) -> float:
    with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
        f.write(mp3)
        f.flush()
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", f.name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
    try:
        return float(out.strip())
    except ValueError:
        return len(text.split()) / (2.7 * speed)  # rough words-per-second estimate


async def get_clip(text: str, speed: float | None = None) -> str:
    """Return the id of an MP3 for this text, making it with Kokoro only if not cached."""
    import hashlib
    speed = speed or KOKORO_SPEED
    clip_id = hashlib.sha1(f"{KOKORO_VOICE}|{speed}|{text}".encode()).hexdigest()[:16]
    if clip_id in _clips:
        _clips[clip_id] = _clips.pop(clip_id)  # mark as recently used
        return clip_id
    t0 = time.time()
    try:
        mp3 = await asyncio.to_thread(_kokoro_tts_blocking, text, speed)
    except Exception as e:
        raise RuntimeError(f"Kokoro failed ({KOKORO_URL}): {e}") from e
    _clips[clip_id] = {"mp3": mp3, "seconds": await _mp3_seconds(mp3, text, speed)}
    while len(_clips) > KOKORO_KEEP:
        _clips.pop(next(iter(_clips)))
    print(f"[kokoro] {len(text.split())} words → {_clips[clip_id]['seconds']:.1f}s of speech "
          f"in {time.time() - t0:.1f}s")
    return clip_id


@app.get("/kokoro-audio/{clip_id}.mp3")
async def kokoro_audio(clip_id: str) -> Response:
    clip = _clips.get(clip_id)
    if clip is None:
        raise HTTPException(404, "clip expired")
    return Response(clip["mp3"], media_type="audio/mpeg")


# ---------- Home Assistant: play / stop / watch the Nest Mini ----------
def _ha_call_blocking(path: str, body: dict | None = None):
    req = urllib.request.Request(
        f"{HA_URL}{path}", data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"null")


async def ha(path: str, body: dict | None = None):
    if not HA_TOKEN:
        raise RuntimeError("HA_TOKEN not set")
    return await asyncio.to_thread(_ha_call_blocking, path, body)


async def play_clip(clip_id: str) -> str:
    url = f"{_lan_base_url()}/kokoro-audio/{clip_id}.mp3"
    await ha("/api/services/media_player/play_media",
             {"entity_id": HA_SPEAKER, "media_content_id": url, "media_content_type": "audio/mpeg"})
    return url


async def wait_until_played(url: str, seconds: float) -> None:
    """Return when the Nest Mini has finished playing `url` (or something else took over).
    Never waits longer than the clip length + 5 s; if HA never shows it playing, waits the clip length."""
    t0 = time.time()
    cap = min(seconds + 5, 180)
    started = False
    while time.time() - t0 < cap:
        try:
            st = await ha(f"/api/states/{HA_SPEAKER}")
            ours = st["state"] == "playing" and st["attributes"].get("media_content_id") == url
        except Exception:
            ours = None  # HA unreachable: fall back to the clip length
        if ours:
            started = True
        elif started and ours is False:
            return  # finished, stopped, or replaced by other audio
        elif not started and time.time() - t0 > 8:
            await asyncio.sleep(max(0.0, seconds + 1 - (time.time() - t0)))  # never seen playing
            return
        await asyncio.sleep(0.5)


# ---------- the one-at-a-time state ----------
class KidVoice:
    def __init__(self) -> None:
        self.state = "idle"     # idle | thinking | speaking
        self.since = time.time()
        self.token = 0          # bumps on every new action, so stale watchers don't reset a newer one
        self.question = ""
        self.answer = ""
        self._last_heard = ""

    def begin(self, question: str = "") -> int:
        """Claim the speaker for a new action, or 409 if busy. No await → atomic in asyncio."""
        if self.state != "idle":
            raise HTTPException(409, f"busy: {self.state}")
        self.token += 1
        self._set("thinking")
        self.question = question
        return self.token

    def _set(self, state: str) -> None:
        self.state, self.since = state, time.time()

    def finish(self, token: int) -> None:
        if token == self.token:
            self._set("idle")

    async def speak(self, token: int, text: str, speed: float | None = None) -> None:
        """Make (or reuse) the clip, play it, and go back to idle once the Nest Mini finishes."""
        try:
            clip_id = await get_clip(text, speed)
            if token != self.token:
                return  # grown-up pressed Stop meanwhile
            url = await play_clip(clip_id)
        except Exception:
            self.finish(token)
            raise
        self._set("speaking")
        asyncio.create_task(self._watch(token, url, _clips[clip_id]["seconds"]))

    async def _watch(self, token: int, url: str, seconds: float) -> None:
        with contextlib.suppress(Exception):
            await wait_until_played(url, seconds)
        self.finish(token)

    def heard_phrase(self) -> str:
        options = [p for p in HEARD_PHRASES if p != self._last_heard] or HEARD_PHRASES
        self._last_heard = random.choice(options)
        return self._last_heard

    def snapshot(self) -> dict:
        return {"state": self.state, "seconds": round(time.time() - self.since, 1),
                "question": self.question, "answer": self.answer,
                "asks_back": self.answer.rstrip().endswith("?")}


kid = KidVoice()


async def _prewarm_phrases() -> None:
    """Make the fixed phrases once at startup so they play instantly (Kokoro may still be starting)."""
    for _ in range(20):
        try:
            for text in [*HEARD_PHRASES, *SAY.values()]:
                await get_clip(text)
            print(f"[kokoro] {len(HEARD_PHRASES) + len(SAY)} fixed phrases ready")
            return
        except Exception:
            await asyncio.sleep(15)


async def _say_and_wait(text: str) -> None:
    """Play a short fixed phrase and wait for it to finish (used for spoken errors)."""
    with contextlib.suppress(Exception):
        clip_id = await get_clip(text)
        await wait_until_played(await play_clip(clip_id), _clips[clip_id]["seconds"])


async def _kid_answer(token: int, question: str, t0: float, t_stt: float, filler: bool) -> dict:
    """Ask ChatGPT (with an instant 'let me think' phrase meanwhile) and speak the answer."""
    filler_task = None
    if filler:
        async def _filler():
            clip_id = await get_clip(kid.heard_phrase())
            await wait_until_played(await play_clip(clip_id), _clips[clip_id]["seconds"])
        filler_task = asyncio.create_task(_filler())
    try:
        answer = await browser.ask(question)
    except Exception as e:
        if filler_task:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(filler_task, 8)
        await _say_and_wait(SAY["ai_failed"])
        kid.finish(token)
        raise HTTPException(502, f"ChatGPT failed: {e}") from e
    t_ai = time.time()
    kid.answer = answer
    if filler_task:  # let "Hmm, let me think!" finish before the answer starts
        with contextlib.suppress(Exception):
            await asyncio.wait_for(filler_task, 8)
    speak_error = None
    try:
        await kid.speak(token, answer)
    except Exception as e:
        speak_error = str(e)
    print(f"[kid] q={question!r} a={answer[:60]!r}... {speak_error or ''}")
    return {
        "heard": question, "answer": answer, "asks_back": answer.rstrip().endswith("?"),
        "spoken": speak_error is None, "speak_error": speak_error,
        "seconds": {"stt": round(t_stt - t0, 1), "chatgpt": round(t_ai - t_stt, 1),
                    "speak": round(time.time() - t_ai, 1), "total": round(time.time() - t0, 1)},
    }


@app.get("/voice-kokoro/state")
async def kid_state() -> dict:
    return kid.snapshot()


@app.post("/voice-kokoro/warm")
async def kid_warm() -> dict:
    """Called when the child taps the mic: open ChatGPT now if it was closed for being idle."""
    if not browser.is_open and not browser.lock.locked():
        asyncio.create_task(browser.warm_up())
        return {"warming": True}
    return {"warming": False}


@app.post("/voice-kokoro/ask")
async def kid_ask(audio: UploadFile = File(...)) -> dict:
    token = kid.begin()
    t0 = time.time()
    try:
        raw = await audio.read()
        if not raw or len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(400, "empty or too large recording")
        text = await whisper_transcribe(await to_pcm16k(raw))
    except HTTPException:
        kid.finish(token)
        raise
    except Exception as e:
        kid.finish(token)
        raise HTTPException(502, f"speech-to-text failed: {e}") from e
    t_stt = time.time()
    if not text:
        await _say_and_wait(SAY["no_words"])
        kid.finish(token)
        raise HTTPException(422, "I couldn't hear any words - try again closer to the mic")
    kid.question = text
    if is_repeat_request(text) and browser.last:  # "say that again" → cached clip, no ChatGPT
        kid.answer = browser.last["answer"]
        try:
            await kid.speak(token, kid.answer)
        except Exception as e:
            raise HTTPException(502, f"speaker failed: {e}") from e
        return {"heard": text, "answer": kid.answer, "repeated": True, "spoken": True,
                "asks_back": kid.answer.rstrip().endswith("?"),
                "seconds": {"stt": round(t_stt - t0, 1), "total": round(time.time() - t0, 1)}}
    return await _kid_answer(token, text, t0, t_stt, filler=True)


class QuickReply(BaseModel):
    kind: str  # a key of QUICK_REPLIES


@app.post("/voice-kokoro/quick")
async def kid_quick(req: QuickReply) -> dict:
    question = QUICK_REPLIES.get(req.kind)
    if not question:
        raise HTTPException(400, f"unknown quick reply {req.kind!r}")
    token = kid.begin(question)
    t0 = time.time()
    return await _kid_answer(token, question, t0, t0, filler=False)


class KidSpeak(BaseModel):
    text: str | None = None  # omit → the last answer
    slow: bool = False


@app.post("/voice-kokoro/speak")
async def kid_speak(req: KidSpeak | None = None) -> dict:
    """Replay (cached clip → starts in ~1-2 s). slow=True uses KOKORO_SLOW_SPEED."""
    req = req or KidSpeak()
    text = req.text or kid.answer or (browser.last or {}).get("answer")
    if not text:
        raise HTTPException(404, "nothing to repeat yet")
    text = clean_for_speech(text) or text
    token = kid.begin(kid.question)
    try:
        await kid.speak(token, text, KOKORO_SLOW_SPEED if req.slow else None)
    except Exception as e:
        raise HTTPException(502, f"speaker failed: {e}") from e
    return {"spoken": True, "text": text}


@app.post("/voice-kokoro/stop")
async def kid_stop() -> dict:
    """Grown-up Stop: silence the Nest Mini and unlock the page."""
    kid.token += 1
    kid._set("idle")
    with contextlib.suppress(Exception):
        await ha("/api/services/media_player/media_stop", {"entity_id": HA_SPEAKER})
    return kid.snapshot()


@app.get("/voice-kokoro", response_class=HTMLResponse)
async def voice_kokoro_page() -> str:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_kokoro.html")) as f:
        return f.read()


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
