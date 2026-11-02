import os, re, time, base64, hmac, hashlib
from urllib.parse import urlparse, unquote
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")  # e.g. https://yourapp.onrender.com
SECRET = os.environ.get("SECRET", BOT_TOKEN).encode()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

URL_RE = re.compile(r"https?://\S+", re.I)


# ---------- signed token (source URL hidden + tamper-proof) ----------
def _sig(raw: str) -> str:
    return hmac.new(SECRET, raw.encode(), hashlib.sha256).hexdigest()[:16]


def make_token(url: str) -> str:
    raw = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"{raw}.{_sig(raw)}"


def read_token(token: str) -> str:
    try:
        raw, sig = token.rsplit(".", 1)
    except ValueError:
        raise HTTPException(400, "bad token")
    if not hmac.compare_digest(sig, _sig(raw)):
        raise HTTPException(403, "invalid token")
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()


# ---------- Telegram bot ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Video ka direct link (.mp4) bhejo, main play button bana dunga.")


async def on_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = URL_RE.search(update.message.text or "")
    if not m:
        return
    url = m.group(0)
    if sf_code(url):
        try:
            await resolve(url)
        except ResolveError as e:
            await update.message.reply_text(f"Savefiles link resolve nahi hua: {e}")
            return
    player = f"{BASE_URL}/player?t={make_token(url)}"
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("👉 Play Online Free", web_app=WebAppInfo(url=player))]]
    )
    await update.message.reply_text(
        "🎬 Watch this video for FREE!\n\n▶️ Tap the button below to play instantly",
        reply_markup=kb,
    )


tg_app = Application.builder().token(BOT_TOKEN).build()
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))

http = httpx.AsyncClient(
    follow_redirects=True,
    timeout=httpx.Timeout(30.0, read=None),
    limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"},
)


# ---------- Savefiles.com resolver (apni account ki API se direct link) ----------
SAVEFILES_KEY = os.environ.get("SAVEFILES_KEY", "")
SAVEFILES_HOST = os.environ.get("SAVEFILES_HOST", "https://savefiles.com").rstrip("/")
_cache = {}  # file_code -> (direct_url, expiry)


class ResolveError(Exception):
    pass


def sf_code(url: str):
    p = urlparse(url)
    if not (p.hostname or "").lower().endswith("savefiles.com"):
        return None
    parts = [x for x in p.path.split("/") if x]
    if not parts:
        return None
    if parts[0] in ("d", "e", "v", "embed", "file") and len(parts) > 1:
        return parts[1]
    return parts[0]


def _find_url(o):
    if isinstance(o, str):
        return o if o.startswith("http") else None
    if isinstance(o, dict):
        for k in ("url", "direct_link", "download_url", "link"):
            if k in o:
                r = _find_url(o[k])
                if r:
                    return r
        for val in o.values():
            r = _find_url(val)
            if r:
                return r
    if isinstance(o, list):
        for val in o:
            r = _find_url(val)
            if r:
                return r
    return None


async def resolve(url: str) -> str:
    code = sf_code(url)
    if not code:
        return url  # normal direct link
    if not SAVEFILES_KEY:
        raise ResolveError("SAVEFILES_KEY set nahi hai")
    hit = _cache.get(code)
    if hit and hit[1] > time.time():
        return hit[0]
    last = "link nahi mila"
    for ep in ("file/direct_link", "file/direct"):
        try:
            r = await http.get(
                f"{SAVEFILES_HOST}/api/{ep}",
                params={"key": SAVEFILES_KEY, "file_code": code},
                timeout=20,
            )
            data = r.json()
        except Exception as e:
            last = type(e).__name__
            continue
        if data.get("status") == 200 and data.get("result") is not None:
            direct = _find_url(data["result"])
            if direct:
                if ".m3u8" in direct.split("?")[0]:
                    raise ResolveError("Ye HLS (.m3u8) link hai, abhi support nahi")
                _cache[code] = (direct, time.time() + 1800)
                return direct
        last = str(data.get("msg") or data.get("status") or "link nahi mila")
    raise ResolveError(last)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    yield
    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()
    await http.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return {"status": "ok"}


@app.get("/player")
async def player():
    return FileResponse(os.path.join(BASE_DIR, "player.html"), headers={"Cache-Control": "no-cache"})


# ---------- Range-aware proxy: seeking + fast start ----------
@app.get("/stream/{token}")
async def stream(token: str, request: Request):
    try:
        url = await resolve(read_token(token))
    except ResolveError:
        raise HTTPException(502, "source resolve failed")
    headers = {}
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]

    upstream = await http.send(http.build_request("GET", url, headers=headers), stream=True)
    if upstream.status_code >= 400:
        await upstream.aclose()
        raise HTTPException(upstream.status_code, "source error")

    out = {"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=86400"}
    for h in ("content-length", "content-range"):
        if h in upstream.headers:
            out[h] = upstream.headers[h]
    out["Content-Type"] = upstream.headers.get("content-type", "video/mp4")
    if request.query_params.get("dl"):
        name = os.path.basename(unquote(urlparse(url).path)) or "video.mp4"
        name = name.replace('"', "")
        out["Content-Disposition"] = f'attachment; filename="{name}"'

    async def body():
        try:
            async for chunk in upstream.aiter_raw(256 * 1024):
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=out)
