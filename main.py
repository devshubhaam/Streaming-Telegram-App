import os, re, base64, hmac, hashlib
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
    url = read_token(token)
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
