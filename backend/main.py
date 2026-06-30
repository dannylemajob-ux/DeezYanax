"""
DeezYanax Backend — FastAPI + Telethon
Actúa como puente entre la web y @deezload2bot en Telegram
"""
import asyncio
import base64
import hmac
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import aiofiles
import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest
from telethon.tl.types import KeyboardButtonCallback

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "frontend"

load_dotenv(BASE_DIR / ".env")

def parse_int_env(name: str, default: int = 0) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        return int(value)
    except ValueError:
        return default

# ── Config ────────────────────────────────────────────────────────────────────
API_ID        = parse_int_env("TELEGRAM_API_ID")
API_HASH      = os.getenv("TELEGRAM_API_HASH", "")
PHONE         = os.getenv("TELEGRAM_PHONE", "")
SESSION_NAME  = os.getenv("TELEGRAM_SESSION", "deezyanax_session")
SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING", "").strip()
BOT_USERNAME  = "deezload2bot"
DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)
LOCAL_SAVE_DIR = Path(os.getenv("DEEZYANAX_SAVE_DIR", str(Path.home() / "Downloads"))).expanduser()
FOLDERS_DIR = LOCAL_SAVE_DIR
FOLDERS_DIR.mkdir(parents=True, exist_ok=True)
MAX_CONCURRENT_DOWNLOADS = 5
AUTH_USER = os.getenv("DEEZYANAX_AUTH_USER", "yanax")
AUTH_PASSWORD = os.getenv("DEEZYANAX_AUTH_PASSWORD", "")
AUTH_SECRET = os.getenv("DEEZYANAX_AUTH_SECRET", API_HASH or SESSION_STRING or "deezyanax-local-secret")
AUTH_COOKIE = "deezyanax_session"
AUTH_TTL_SECONDS = 60 * 60 * 24 * 7

# ── Estado global ─────────────────────────────────────────────────────────────
client: TelegramClient = None
pending: dict[str, asyncio.Future] = {}   # request_id → Future
playlist_jobs: dict[str, dict] = {}       # job_id → Estado temporal de playlist/link
active_link_job_id: Optional[str] = None
last_message_ids: list[int] = []          # Para rastrear mensajes del bot
current_quality = "MP3_320"               # Calidad por defecto
download_tasks: set[asyncio.Task] = set()
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

def telegram_configured() -> bool:
    return bool(API_ID and API_HASH and PHONE)

def require_telegram_client() -> TelegramClient:
    if not telegram_configured():
        raise HTTPException(
            status_code=503,
            detail="Faltan credenciales de Telegram en backend/.env. Completa TELEGRAM_API_ID, TELEGRAM_API_HASH y TELEGRAM_PHONE.",
        )
    if not client or not client.is_connected():
        raise HTTPException(
            status_code=503,
            detail="Telegram no está conectado. Ejecuta setup_session.py y reinicia el backend.",
        )
    return client

# ── Modelos ───────────────────────────────────────────────────────────────────
class SearchRequest(BaseModel):
    query: str
    search_type: str = "track"  # track | album | artist | playlist | global

class LinkRequest(BaseModel):
    url: str

class SelectRequest(BaseModel):
    message_id: int
    button_index: int
    row_index: int = 0

class QualityRequest(BaseModel):
    quality: str  # FLAC | MP3_320 | MP3_128

class LoginRequest(BaseModel):
    username: str
    password: str

class InlineButton(BaseModel):
    text: str
    data: Optional[str] = None
    row: int
    col: int

class SearchResult(BaseModel):
    request_id: str
    message_id: int
    text: str
    buttons: list[InlineButton]

class AudioResult(BaseModel):
    request_id: str
    file_id: str
    title: str
    performer: str
    duration: int
    file_name: str
    download_url: str
    thumb_url: Optional[str] = None

class LinkJobResult(BaseModel):
    job_id: str
    status: str
    message: str

class JobButtonRequest(BaseModel):
    message_id: int
    button_index: int
    row_index: int = 0

# ── Ciclo de vida ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    if not telegram_configured():
        print("⚠️  Telegram no configurado. Backend iniciado en modo diagnóstico.")
        print("   Completa backend/.env y ejecuta: python setup_session.py")
        yield
        return

    print("🚀 Iniciando cliente Telegram...")
    session = StringSession(SESSION_STRING) if SESSION_STRING else SESSION_NAME
    client = TelegramClient(session, API_ID, API_HASH)
    await client.start(phone=PHONE)
    print(f"✅ Conectado como: {(await client.get_me()).username}")

    # Escuchar mensajes del bot
    @client.on(events.NewMessage(from_users=BOT_USERNAME))
    async def on_bot_message(event):
        await handle_bot_message(event)

    @client.on(events.MessageEdited(from_users=BOT_USERNAME))
    async def on_bot_message_edited(event):
        await handle_bot_message(event)

    yield

    print("🛑 Desconectando...")
    for task in list(download_tasks):
        task.cancel()
    if download_tasks:
        await asyncio.gather(*download_tasks, return_exceptions=True)
    await client.disconnect()

app = FastAPI(title="DeezYanax API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?|https://deezyanax\.onrender\.com",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def make_auth_token(username: str) -> str:
    expires = str(int(time.time()) + AUTH_TTL_SECONDS)
    nonce = secrets.token_urlsafe(12)
    payload = f"{username}:{expires}:{nonce}"
    sig = hmac.new(AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}:{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode()

def verify_auth_token(token: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        username, expires, nonce, sig = raw.split(":", 3)
        if username != AUTH_USER or int(expires) < int(time.time()):
            return False
    except Exception:
        return False
    payload = f"{username}:{expires}:{nonce}"
    expected = hmac.new(AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)

def is_public_path(path: str) -> bool:
    return (
        path == "/health"
        or path == "/favicon.ico"
        or path.startswith("/api/auth/")
    )

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    protected = path.startswith("/api/") or path.startswith("/downloads/")
    if protected and not is_public_path(path):
        token = request.cookies.get(AUTH_COOKIE, "")
        if not verify_auth_token(token):
            return JSONResponse({"detail": "No autenticado"}, status_code=401)
    return await call_next(request)

# ── Handler de mensajes del bot ───────────────────────────────────────────────
async def handle_bot_message(event):
    msg = event.message

    if await handle_active_link_job(event):
        return
    
    # Resolver el future más antiguo pendiente
    if not pending:
        return
    
    request_id = next(iter(pending))
    future = pending[request_id]
    
    if future.done():
        return

    # ¿Es un audio?
    if msg.audio or msg.document:
        data = await download_audio_message(event)
        data["request_id"] = request_id
        future.set_result({"type": "audio", "data": data})
        del pending[request_id]
        return

    # ¿Es texto con botones inline?
    if msg.text and msg.buttons:
        buttons = []
        for r_idx, row in enumerate(msg.buttons):
            for c_idx, btn in enumerate(row):
                buttons.append(InlineButton(
                    text=btn.text,
                    data=btn.data.decode() if btn.data else None,
                    row=r_idx,
                    col=c_idx,
                ))
        
        result = SearchResult(
            request_id=request_id,
            message_id=msg.id,
            text=msg.text,
            buttons=buttons,
        )
        future.set_result({"type": "results", "data": result.dict()})
        del pending[request_id]
        return

    # Mensaje de texto simple (confirmación, error, etc.)
    if msg.text:
        future.set_result({"type": "text", "data": {"text": msg.text, "message_id": msg.id}})
        del pending[request_id]

# ── Handler de links/playlists ────────────────────────────────────────────────
def serialize_buttons(msg) -> list[dict]:
    buttons = []
    if not msg.buttons:
        return buttons

    for r_idx, row in enumerate(msg.buttons):
        for c_idx, btn in enumerate(row):
            buttons.append({
                "text": btn.text,
                "row": r_idx,
                "col": c_idx,
                "has_data": bool(getattr(btn, "data", None)),
            })
    return buttons

def upsert_bot_card(job: dict, msg) -> None:
    text = msg.text or ""
    preview_title = extract_preview_title(msg)
    if preview_title:
        job["playlist_title"] = preview_title

    card = {
        "message_id": msg.id,
        "text": text,
        "buttons": serialize_buttons(msg),
        "date": msg.date.isoformat() if msg.date else "",
    }

    total_match = re.search(r"total\s+tracks:\s*(\d+)", text, flags=re.IGNORECASE)
    if total_match:
        job["expected_total"] = int(total_match.group(1))

    cards = job.setdefault("bot_messages", [])
    for idx, existing in enumerate(cards):
        if existing["message_id"] == msg.id:
            cards[idx] = card
            return
    cards.append(card)

def extract_preview_title(msg) -> str:
    candidates = []
    preview = getattr(msg, "web_preview", None)
    if preview:
        candidates.extend([
            getattr(preview, "title", ""),
            getattr(preview, "site_name", ""),
        ])

    media = getattr(msg, "media", None)
    webpage = getattr(media, "webpage", None)
    if webpage:
        candidates.extend([
            getattr(webpage, "title", ""),
            getattr(webpage, "site_name", ""),
        ])

    for candidate in candidates:
        value = str(candidate or "").strip()
        if value and not value.lower().startswith("spotify"):
            return value
    return ""

def is_processing_text(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ["wait", "processing", "processed", "procesando"])

def is_finished_text(text: str) -> bool:
    lowered = text.lower().strip()
    return "finished" in lowered or lowered in {"done", "completed"}

def get_original_file_name(msg) -> str:
    if msg.file and getattr(msg.file, "name", None):
        return msg.file.name

    media = msg.audio or msg.document
    for attr in getattr(media, "attributes", []) or []:
        if hasattr(attr, "file_name") and attr.file_name:
            return attr.file_name

    return ""

def is_zip_message(msg) -> bool:
    file_name = get_original_file_name(msg).lower()
    mime_type = str(getattr(getattr(msg, "file", None), "mime_type", "") or "").lower()
    return file_name.endswith(".zip") or "zip" in mime_type

def split_track_name(name: str) -> tuple[str, str]:
    clean = Path(name).stem.strip()
    clean = re.sub(r"[_]+", " ", clean)
    clean = re.sub(r"\s+", " ", clean)

    for sep in [" - ", " – ", " — "]:
        if sep in clean:
            left, right = clean.split(sep, 1)
            if left.strip() and right.strip():
                return right.strip(), left.strip()

    return clean, "Desconocido"

def safe_download_name(value: str, fallback: str) -> str:
    clean = re.sub(r"[\\/:*?\"<>|]+", " ", value or fallback)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:140] or fallback

def friendly_track_filename(track: dict, index: Optional[int] = None) -> str:
    ext = Path(str(track.get("file_name") or track.get("original_name") or "track.mp3")).suffix or ".mp3"
    prefix = f"{index:02d} - " if index else ""
    performer = str(track.get("performer") or "").strip()
    title = str(track.get("title") or track.get("original_name") or "track").strip()
    if performer and performer.lower() != "desconocido":
        base = f"{prefix}{performer} - {title}"
    else:
        base = f"{prefix}{title}"
    return f"{safe_download_name(base, 'track')}{ext}"

def extract_audio_meta(msg, fallback_name: str, dest: Path) -> tuple[str, str, int]:
    title = ""
    performer = ""
    duration = 0

    if msg.audio and msg.file and msg.file.media:
        for attr in msg.file.media.attributes:
            if hasattr(attr, "title") and attr.title:
                title = attr.title
            if hasattr(attr, "performer") and attr.performer:
                performer = attr.performer
            if hasattr(attr, "duration") and attr.duration:
                duration = attr.duration

    if not title:
        source_name = fallback_name or (msg.text or "").strip() or dest.stem
        parsed_title, parsed_performer = split_track_name(source_name)
        title = parsed_title
        performer = performer or parsed_performer

    return title or fallback_name or dest.stem, performer or "Desconocido", duration

def job_counts(job: dict) -> dict:
    tracks = job.get("tracks", [])
    ready = sum(1 for t in tracks if t.get("status") == "ready")
    error = sum(1 for t in tracks if t.get("status") == "error")
    downloading = sum(1 for t in tracks if t.get("status") == "downloading")
    skipped = len(job.get("skipped", []))
    return {
        "received": len(tracks),
        "ready": ready,
        "error": error,
        "downloading": downloading,
        "skipped": skipped,
        "expected": job.get("expected_total") or len(tracks),
    }

def refresh_job_status(job: dict) -> None:
    counts = job_counts(job)
    job["counts"] = counts

    if job.get("bot_finished"):
        job["status"] = "finished"
        job["message"] = "Finished."
        return

    if counts["received"] > 0:
        job["status"] = "processing"
        job["message"] = "Procesando canciones desde Telegram..."

async def download_audio_message(event, job: Optional[dict] = None) -> dict:
    msg = event.message
    file_id = str(uuid.uuid4())
    file_name = get_original_file_name(msg)
    mime_type = str(getattr(msg.file, "mime_type", "")).lower()
    if file_name and "." in file_name:
        ext = Path(file_name).suffix
    elif "zip" in mime_type:
        ext = ".zip"
    elif "flac" in mime_type:
        ext = ".flac"
    else:
        ext = ".mp3"
    dest = DOWNLOADS_DIR / f"{file_id}{ext}"

    track = None
    if job is not None:
        track = {
            "id": file_id,
            "status": "downloading",
            "progress": 0,
            "title": "Descargando...",
            "performer": "",
            "duration": 0,
            "file_name": dest.name,
            "original_name": file_name,
            "download_url": None,
            "thumb_url": None,
            "kind": "file",
        }
        job["tracks"].append(track)
        job["status"] = "downloading"
        job["message"] = "Descargando canciones..."

    def on_progress(current: int, total: int):
        if track and total:
            track["progress"] = min(99, int((current / total) * 100))

    await msg.download_media(file=str(dest), progress_callback=on_progress)

    title, performer, duration = extract_audio_meta(msg, file_name, dest)
    thumb_url = None
    if msg.photo or (hasattr(msg, "file") and getattr(msg.file, "thumb", None)):
        thumb_dest = DOWNLOADS_DIR / f"{file_id}_thumb.jpg"
        try:
            await client.download_media(msg, file=str(thumb_dest), thumb=-1)
            thumb_url = f"/downloads/{file_id}_thumb.jpg"
        except Exception:
            pass

    data = {
        "request_id": job["id"] if job else "",
        "file_id": file_id,
        "title": title,
        "performer": performer,
        "duration": duration,
        "file_name": dest.name,
        "original_name": file_name,
        "friendly_name": friendly_track_filename({"title": title, "performer": performer, "file_name": dest.name}),
        "download_url": f"/downloads/{dest.name}",
        "thumb_url": thumb_url,
        "status": "ready",
        "progress": 100,
        "kind": "zip" if ext.lower() == ".zip" else "audio",
    }

    if track:
        track.update(data)

    return data

def create_track_from_message(msg, job: dict) -> tuple[dict, Path, str]:
    file_id = str(uuid.uuid4())
    file_name = get_original_file_name(msg)
    mime_type = str(getattr(msg.file, "mime_type", "")).lower()
    if file_name and "." in file_name:
        ext = Path(file_name).suffix
    elif "zip" in mime_type:
        ext = ".zip"
    elif "flac" in mime_type:
        ext = ".flac"
    else:
        ext = ".mp3"
    dest = DOWNLOADS_DIR / f"{file_id}{ext}"
    title, performer, duration = extract_audio_meta(msg, file_name, dest)

    track = {
        "id": file_id,
        "message_id": msg.id,
        "status": "received",
        "progress": 100,
        "title": title,
        "performer": performer,
        "duration": duration,
        "file_name": dest.name,
        "original_name": file_name,
        "friendly_name": friendly_track_filename({
            "title": title,
            "performer": performer,
            "file_name": dest.name,
            "original_name": file_name,
        }, len(job.get("tracks", [])) + 1),
        "download_url": None,
        "thumb_url": None,
        "kind": "zip" if ext.lower() == ".zip" else "audio",
    }
    job["tracks"].append(track)
    refresh_job_status(job)
    print(f"🎧 Procesada desde Telegram: #{len(job['tracks'])} {title} - {performer}")
    return track, dest, ext

def create_zip_from_message(msg, job: dict) -> dict:
    zip_info = job.get("zip_file") or {}
    file_id = zip_info.get("id") or str(uuid.uuid4())
    file_name = get_original_file_name(msg)
    playlist_title = safe_download_name(job.get("playlist_title") or "playlist", "playlist")
    dest = DOWNLOADS_DIR / f"{file_id}.zip"
    zip_info.update({
        "id": file_id,
        "message_id": msg.id,
        "status": "received",
        "progress": 100,
        "file_name": dest.name,
        "original_name": file_name or f"{playlist_title}.zip",
        "friendly_name": f"{playlist_title}.zip",
        "download_url": None,
    })
    job["zip_file"] = zip_info
    job["message"] = "ZIP recibido desde Telegram. Listo para descargar."
    print(f"🗃️ ZIP recibido desde Telegram: {zip_info['friendly_name']}")
    return zip_info

async def download_track_file(event, job: dict, track: dict, dest: Path, ext: str) -> None:
    msg = event.message
    try:
        async with download_semaphore:
            track["status"] = "downloading"
            refresh_job_status(job)

            def on_progress(current: int, total: int):
                if total:
                    track["progress"] = min(99, max(1, int((current / total) * 100)))

            await msg.download_media(file=str(dest), progress_callback=on_progress)
            thumb_url = None
            if msg.photo or (hasattr(msg, "file") and getattr(msg.file, "thumb", None)):
                thumb_dest = DOWNLOADS_DIR / f"{track['id']}_thumb.jpg"
                try:
                    await client.download_media(msg, file=str(thumb_dest), thumb=-1)
                    thumb_url = f"/downloads/{track['id']}_thumb.jpg"
                except Exception:
                    pass

            track.update({
                "download_url": f"/downloads/{dest.name}",
                "thumb_url": thumb_url,
                "status": "ready",
                "progress": 100,
                "kind": "zip" if ext.lower() == ".zip" else "audio",
            })
            print(f"✅ Guardado local: {track['title']}")
    except Exception as exc:
        track["status"] = "error"
        track["error"] = str(exc)
        job["message"] = f"Error guardando {track.get('title')}: {exc}"
        print(f"❌ Error guardando {track.get('title')}: {exc}")
    finally:
        refresh_job_status(job)

def record_job_track(event, job: dict) -> None:
    msg_id = event.message.id
    if is_zip_message(event.message):
        existing = job.get("zip_file") or {}
        if existing.get("message_id") != msg_id:
            create_zip_from_message(event.message, job)
        return

    if any(track.get("message_id") == msg_id for track in job.get("tracks", [])):
        return

    create_track_from_message(event.message, job)

def archive_state_key(mode: str = "flat") -> str:
    return "folder_archive" if mode == "folder" else "archive"

def get_archive_state(job: dict, mode: str = "flat") -> dict:
    key = archive_state_key(mode)
    archive = job.get(key)
    if not archive:
        archive = {
            "status": "idle",
            "mode": "folder" if mode == "folder" else "flat",
            "progress": 0,
            "completed": 0,
            "total": 0,
            "download_url": None,
            "file_name": None,
            "error": None,
        }
        job[key] = archive
    return archive

async def build_local_archive(job: dict, mode: str = "flat") -> None:
    mode = "folder" if mode == "folder" else "flat"
    archive = get_archive_state(job, mode)
    tracks = [t for t in job.get("tracks", []) if t.get("status") != "error"]
    archive.update({
        "status": "downloading",
        "mode": mode,
        "progress": 1,
        "completed": 0,
        "total": len(tracks),
        "download_url": None,
        "error": None,
    })
    job["message"] = "Descargando canciones para crear carpeta..." if mode == "folder" else "Descargando canciones para comprimir..."
    refresh_job_status(job)

    if not tracks:
        archive.update({"status": "error", "error": "No hay canciones para comprimir"})
        job["message"] = archive["error"]
        return

    completed = 0

    async def ensure_one(track: dict) -> None:
        nonlocal completed
        await ensure_track_local(job, track)
        completed += 1
        archive["completed"] = completed
        archive["progress"] = min(90, max(1, int((completed / len(tracks)) * 90)))
        refresh_job_status(job)

    try:
        await asyncio.gather(*(ensure_one(track) for track in tracks))
        ready_tracks = [t for t in tracks if t.get("status") == "ready" and t.get("download_url")]
        if not ready_tracks:
            raise RuntimeError("No se pudo guardar ninguna cancion para comprimir")

        archive["status"] = "zipping"
        archive["progress"] = 95
        job["message"] = "Empaquetando carpeta..." if mode == "folder" else "Creando ZIP local..."

        playlist_title = safe_download_name(job.get("playlist_title") or "playlist", "playlist")
        zip_path = DOWNLOADS_DIR / f"{mode}_{job['id']}.zip"
        used_names = set()
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive_file:
            for idx, track in enumerate(ready_tracks, start=1):
                source = resolve_track_source(track)
                if not source or not source.exists():
                    continue

                arcname = friendly_track_filename(track, idx)
                if mode == "folder":
                    arcname = f"{playlist_title}/{arcname}"
                dedupe = 2
                while arcname in used_names:
                    stem = Path(arcname).stem
                    ext = Path(arcname).suffix
                    parent = str(Path(arcname).parent)
                    file_name = f"{stem} ({dedupe}){ext}"
                    arcname = f"{parent}/{file_name}" if parent != "." else file_name
                    dedupe += 1
                used_names.add(arcname)
                archive_file.write(source, arcname)

        archive.update({
            "status": "ready",
            "progress": 100,
            "completed": len(ready_tracks),
            "total": len(tracks),
            "file_name": zip_path.name,
            "download_url": f"/api/job/{job['id']}/archive/download?mode={mode}",
            "friendly_name": f"{playlist_title}.zip",
        })
        job["message"] = "Carpeta lista para descargar." if mode == "folder" else "ZIP local listo."
        print(f"✅ Paquete {mode} listo: {archive['friendly_name']}")
    except Exception as exc:
        archive.update({
            "status": "error",
            "error": str(exc),
        })
        job["message"] = f"Error creando ZIP local: {exc}"
        print(f"❌ Error creando ZIP local: {exc}")
    finally:
        refresh_job_status(job)

def get_folder_state(job: dict) -> dict:
    folder = job.get("folder")
    if not folder:
        folder = {
            "status": "idle",
            "progress": 0,
            "completed": 0,
            "total": 0,
            "path": None,
            "base_path": str(FOLDERS_DIR.resolve()),
            "folder_name": None,
            "error": None,
        }
        job["folder"] = folder
    return folder

def resolve_track_source(track: dict) -> Optional[Path]:
    file_name = track.get("file_name")
    if file_name:
        source = DOWNLOADS_DIR / str(file_name)
        if source.exists():
            return source

    folder_file = track.get("folder_file")
    if folder_file:
        source = Path(str(folder_file))
        if source.exists():
            return source

    return None

async def link_or_copy_file(source: Path, destination: Path) -> None:
    if destination.exists():
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)

async def build_local_folder(job: dict) -> None:
    folder = get_folder_state(job)
    tracks = [t for t in job.get("tracks", []) if t.get("status") != "error"]
    playlist_title = safe_download_name(job.get("playlist_title") or "Playlist", "Playlist")
    target_dir = FOLDERS_DIR / playlist_title

    folder.update({
        "status": "downloading",
        "progress": 1,
        "completed": 0,
        "total": len(tracks),
        "path": str(target_dir.resolve()),
        "base_path": str(FOLDERS_DIR.resolve()),
        "folder_name": playlist_title,
        "error": None,
    })
    job["message"] = f"Guardando en Descargas/{playlist_title}..."
    refresh_job_status(job)

    if not tracks:
        folder.update({"status": "error", "error": "No hay canciones para guardar"})
        job["message"] = folder["error"]
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    completed = 0
    used_names = set()
    progress_by_track = {str(track["id"]): 0 for track in tracks}
    name_lock = asyncio.Lock()
    state_lock = asyncio.Lock()

    def update_folder_progress() -> None:
        if not tracks:
            folder["progress"] = 0
            return
        progress = int(sum(progress_by_track.values()) / len(tracks))
        folder["progress"] = min(99, max(1, progress))

    async def mark_saved(track: dict, destination: Path, file_name: str) -> None:
        nonlocal completed
        track.update({
            "folder_file": str(destination),
            "folder_name": file_name,
            "download_url": f"/api/job/{job['id']}/track/{track['id']}",
            "status": "ready",
            "progress": 100,
        })
        async with state_lock:
            progress_by_track[str(track["id"])] = 100
            completed += 1
            folder["completed"] = completed
            update_folder_progress()
        refresh_job_status(job)

    async def save_one(index: int, track: dict) -> None:
        async with name_lock:
            file_name = friendly_track_filename(track, index)
            dedupe = 2
            while file_name in used_names:
                stem = Path(file_name).stem
                ext = Path(file_name).suffix
                file_name = f"{stem} ({dedupe}){ext}"
                dedupe += 1
            used_names.add(file_name)
            destination = target_dir / file_name

        if destination.exists() and destination.stat().st_size > 0:
            await mark_saved(track, destination, file_name)
            return

        source = resolve_track_source(track)
        if source:
            await link_or_copy_file(source, destination)
            await mark_saved(track, destination, file_name)
            return

        tg = require_telegram_client()
        entity = await tg.get_entity(BOT_USERNAME)
        msg = await tg.get_messages(entity, ids=track["message_id"])
        if not msg or not (msg.audio or msg.document):
            track["status"] = "error"
            track["error"] = "Mensaje de audio no encontrado en Telegram"
            refresh_job_status(job)
            return

        async with download_semaphore:
            track["status"] = "downloading"
            track["progress"] = 1
            refresh_job_status(job)

            def on_progress(current: int, total: int):
                if not total:
                    return
                progress = min(99, max(1, int((current / total) * 100)))
                track["progress"] = progress
                progress_by_track[str(track["id"])] = progress
                update_folder_progress()

            await msg.download_media(file=str(destination), progress_callback=on_progress)
            await mark_saved(track, destination, file_name)

    try:
        await asyncio.gather(*(save_one(idx, track) for idx, track in enumerate(tracks, start=1)))
        folder.update({
            "status": "ready",
            "progress": 100,
            "completed": completed,
            "total": len(tracks),
            "path": str(target_dir.resolve()),
            "base_path": str(FOLDERS_DIR.resolve()),
            "folder_name": playlist_title,
        })
        job["message"] = f"Carpeta lista en Descargas/{playlist_title}"
        print(f"✅ Carpeta local lista: {target_dir.resolve()}")
    except Exception as exc:
        folder.update({"status": "error", "error": str(exc)})
        job["message"] = f"Error guardando carpeta local: {exc}"
        print(f"❌ Error guardando carpeta local: {exc}")
    finally:
        refresh_job_status(job)

async def ensure_track_local(job: dict, track: dict) -> dict:
    if track.get("status") == "ready" and track.get("download_url") and resolve_track_source(track):
        return track
    if track.get("folder_file") and resolve_track_source(track):
        track.update({
            "status": "ready",
            "progress": 100,
            "download_url": f"/api/job/{job['id']}/track/{track['id']}",
        })
        return track
    if track.get("status") == "downloading":
        while track.get("status") == "downloading":
            await asyncio.sleep(0.5)
        return track

    tg = require_telegram_client()
    entity = await tg.get_entity(BOT_USERNAME)
    msg = await tg.get_messages(entity, ids=track["message_id"])
    if not msg or not (msg.audio or msg.document):
        track["status"] = "error"
        track["error"] = "Mensaje de audio no encontrado en Telegram"
        refresh_job_status(job)
        return track

    ext = Path(str(track.get("file_name") or track.get("original_name") or "track.mp3")).suffix or ".mp3"
    dest = DOWNLOADS_DIR / str(track["file_name"])
    await download_track_file(type("Event", (), {"message": msg})(), job, track, dest, ext)
    return track

async def handle_active_link_job(event) -> bool:
    global active_link_job_id
    if not active_link_job_id:
        return False

    job = playlist_jobs.get(active_link_job_id)
    if not job or job["status"] == "error":
        return False

    msg = event.message

    if msg.text or msg.buttons:
        upsert_bot_card(job, msg)
        if msg.buttons:
            job["status"] = "waiting_action"
            job["message"] = "El bot espera que elijas una opción."

    if msg.audio or msg.document:
        record_job_track(event, job)
        return True

    if msg.text:
        job["last_text"] = msg.text
        lowered = msg.text.lower()
        if "cannot upload" in lowered or "prohibited" in lowered or "no alternative" in lowered:
            skipped = job.setdefault("skipped", [])
            if not any(item.get("message_id") == msg.id for item in skipped):
                skipped.append({
                    "message_id": msg.id,
                    "text": msg.text,
                    "date": msg.date.isoformat() if msg.date else "",
                })
            refresh_job_status(job)
            return True
        if is_finished_text(msg.text):
            job["bot_finished"] = True
            refresh_job_status(job)
            return True
        if is_processing_text(msg.text):
            job["status"] = "processing"
            job["message"] = msg.text
            return True

    return False

# ── Helper: esperar respuesta del bot ─────────────────────────────────────────
async def wait_for_response(timeout: int = 30) -> dict:
    request_id = str(uuid.uuid4())
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    pending[request_id] = future
    
    try:
        result = await asyncio.wait_for(future, timeout=timeout)
        return result
    except asyncio.TimeoutError:
        pending.pop(request_id, None)
        raise HTTPException(status_code=504, detail="El bot no respondió a tiempo. Intenta de nuevo.")

# ── Helper: limpiar archivos viejos ──────────────────────────────────────────
async def cleanup_old_files():
    """Elimina archivos y jobs temporales de más de 1 hora"""
    now = time.time()
    for f in DOWNLOADS_DIR.iterdir():
        if f.resolve() == FOLDERS_DIR.resolve():
            continue
        if now - f.stat().st_mtime > 3600:
            if f.is_dir():
                shutil.rmtree(f, ignore_errors=True)
            else:
                f.unlink(missing_ok=True)
    for job_id, job in list(playlist_jobs.items()):
        if now - job.get("created_at", now) > 3600:
            playlist_jobs.pop(job_id, None)

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    connected = bool(client and client.is_connected())
    return {
        "status": "ok",
        "telegram_configured": telegram_configured(),
        "telegram_connected": connected,
        "quality": current_quality,
    }


@app.get("/api/auth/session")
async def auth_session(request: Request):
    token = request.cookies.get(AUTH_COOKIE, "")
    return {"authenticated": verify_auth_token(token), "username": AUTH_USER}


@app.post("/api/auth/login")
async def auth_login(req: LoginRequest, request: Request):
    if not AUTH_PASSWORD:
        raise HTTPException(503, "Login no configurado en el servidor")
    valid_user = hmac.compare_digest(req.username, AUTH_USER)
    valid_password = hmac.compare_digest(req.password, AUTH_PASSWORD)
    if not (valid_user and valid_password):
        raise HTTPException(401, "Usuario o password incorrecto")

    response = JSONResponse({"authenticated": True, "username": AUTH_USER})
    response.set_cookie(
        key=AUTH_COOKIE,
        value=make_auth_token(AUTH_USER),
        max_age=AUTH_TTL_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout():
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(AUTH_COOKIE, path="/")
    return response


@app.post("/api/quality")
async def set_quality(req: QualityRequest):
    """Cambiar calidad de audio"""
    global current_quality
    tg = require_telegram_client()
    valid = ["FLAC", "MP3_320", "MP3_128"]
    if req.quality not in valid:
        raise HTTPException(400, f"Calidad inválida. Opciones: {valid}")
    
    # Enviar /settings al bot
    await tg.send_message(BOT_USERNAME, "/settings")
    response = await wait_for_response(15)
    
    if response["type"] != "results":
        raise HTTPException(502, "No se pudo abrir el menú de settings")
    
    # Buscar el botón de la calidad deseada
    buttons_data = response["data"]["buttons"]
    target_btn = None
    for btn in buttons_data:
        if req.quality.lower() in btn["text"].lower() or req.quality.replace("_", " ").lower() in btn["text"].lower():
            target_btn = btn
            break
    
    if not target_btn:
        # Intentar hacer match parcial
        for btn in buttons_data:
            if "320" in btn["text"] and "320" in req.quality:
                target_btn = btn
                break
            if "128" in btn["text"] and "128" in req.quality:
                target_btn = btn
                break
            if "flac" in btn["text"].lower() and req.quality == "FLAC":
                target_btn = btn
                break
    
    if not target_btn or not target_btn["data"]:
        raise HTTPException(502, "Botón de calidad no encontrado")
    
    # Hacer click en el botón
    msg_id = response["data"]["message_id"]
    entity = await tg.get_entity(BOT_USERNAME)
    await tg(GetBotCallbackAnswerRequest(
        peer=entity,
        msg_id=msg_id,
        data=target_btn["data"].encode(),
    ))
    
    current_quality = req.quality
    return {"success": True, "quality": current_quality}


@app.post("/api/search")
async def search(req: SearchRequest):
    """Buscar música por texto"""
    tg = require_telegram_client()
    
    # Enviar query al bot
    await tg.send_message(BOT_USERNAME, req.query)
    response = await wait_for_response(20)
    
    if response["type"] == "audio":
        # El bot reconoció directamente y devolvió audio
        return {"type": "audio", "data": response["data"]}
    
    if response["type"] != "results":
        raise HTTPException(502, f"Respuesta inesperada: {response}")
    
    # Si hay botones de tipo de búsqueda, seleccionar el correcto
    buttons = response["data"]["buttons"]
    search_type_map = {
        "track": ["track", "canción", "song", "🔍"],
        "album": ["album", "álbum", "💿"],
        "artist": ["artist", "artista", "🎤"],
        "playlist": ["playlist", "🎵"],
        "global": ["global", "🌐", "all"],
        "label": ["label", "🏷"],
    }
    
    target_keywords = search_type_map.get(req.search_type, ["track"])
    target_btn = None
    
    for btn in buttons:
        btn_text_lower = btn["text"].lower()
        if any(kw.lower() in btn_text_lower for kw in target_keywords):
            target_btn = btn
            break
    
    # Si no encontramos el tipo exacto, usar el primero
    if not target_btn and buttons:
        target_btn = buttons[0]
    
    if target_btn and target_btn["data"]:
        msg_id = response["data"]["message_id"]
        entity = await tg.get_entity(BOT_USERNAME)
        await tg(GetBotCallbackAnswerRequest(
            peer=entity,
            msg_id=msg_id,
            data=target_btn["data"].encode(),
        ))
        
        # Esperar resultados de búsqueda
        response2 = await wait_for_response(25)
        return response2
    
    return response


@app.post("/api/select")
async def select_result(req: SelectRequest):
    """Seleccionar un resultado de la lista haciendo click en su botón"""
    tg = require_telegram_client()
    
    # Obtener el mensaje con los botones
    entity = await tg.get_entity(BOT_USERNAME)
    messages = await tg.get_messages(entity, ids=req.message_id)
    
    if not messages or not messages.buttons:
        raise HTTPException(404, "Mensaje no encontrado o sin botones")
    
    msg = messages
    try:
        row = msg.buttons[req.row_index]
        btn = row[req.button_index]
    except (IndexError, TypeError):
        raise HTTPException(400, "Índice de botón inválido")
    
    if not isinstance(btn, KeyboardButtonCallback) and not hasattr(btn, 'data'):
        raise HTTPException(400, "El botón no tiene callback data")
    
    await tg(GetBotCallbackAnswerRequest(
        peer=entity,
        msg_id=req.message_id,
        data=btn.data,
    ))
    
    # Esperar el audio
    response = await wait_for_response(45)
    return response


@app.post("/api/link")
async def process_link(req: LinkRequest, background_tasks: BackgroundTasks):
    """Procesar link directo de Deezer o Spotify"""
    global active_link_job_id
    tg = require_telegram_client()
    
    url = req.url.strip()
    is_valid = bool(re.match(r'https?://(www\.)?(deezer\.com|open\.spotify\.com)/', url))
    
    if not is_valid:
        raise HTTPException(400, "URL inválida. Solo se aceptan links de Deezer o Spotify.")
    
    job_id = str(uuid.uuid4())
    playlist_jobs[job_id] = {
        "id": job_id,
        "url": url,
        "status": "queued",
        "message": "Enviando link al bot...",
        "playlist_title": "Playlist",
        "tracks": [],
        "bot_messages": [],
        "skipped": [],
        "zip_file": None,
        "archive": None,
        "folder": None,
        "expected_total": None,
        "bot_finished": False,
        "counts": {"received": 0, "ready": 0, "error": 0, "downloading": 0, "skipped": 0, "expected": 0},
        "created_at": time.time(),
        "updated_at": time.time(),
        "last_text": "",
    }
    active_link_job_id = job_id

    await tg.send_message(BOT_USERNAME, url)
    
    background_tasks.add_task(cleanup_old_files)
    return {"type": "job", "data": LinkJobResult(job_id=job_id, status="queued", message="Link enviado al bot.").dict()}


@app.get("/api/job/{job_id}")
async def get_job(job_id: str):
    """Estado temporal de una descarga por link/playlist"""
    job = playlist_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado o expirado")

    refresh_job_status(job)
    job["updated_at"] = time.time()
    return {"type": "job", "data": job}


@app.get("/api/job/{job_id}/zip")
async def download_job_zip(job_id: str, mode: str = "flat"):
    """Descarga el ZIP local cuando ya esta listo."""
    job = playlist_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado o expirado")

    refresh_job_status(job)
    playlist_title = safe_download_name(job.get("playlist_title") or "playlist", "playlist")
    mode = "folder" if mode == "folder" else "flat"
    archive = get_archive_state(job, mode)
    if archive.get("status") != "ready" or not archive.get("file_name"):
        raise HTTPException(409, "La descarga aun no esta lista")

    zip_path = DOWNLOADS_DIR / str(archive["file_name"])
    if not zip_path.exists():
        archive.update({"status": "error", "error": "Archivo ZIP no encontrado"})
        raise HTTPException(404, "Archivo ZIP no encontrado")

    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=archive.get("friendly_name") or f"{playlist_title}.zip",
    )


@app.post("/api/job/{job_id}/archive/start")
async def start_job_archive(job_id: str, mode: str = "flat"):
    """Inicia la descarga paralela y compresion local de la playlist."""
    job = playlist_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado o expirado")

    mode = "folder" if mode == "folder" else "flat"
    refresh_job_status(job)
    if not job.get("bot_finished"):
        raise HTTPException(409, "Espera a que el bot termine con Finished.")

    tracks = [t for t in job.get("tracks", []) if t.get("status") != "error"]
    if not tracks:
        raise HTTPException(404, "No hay canciones para comprimir")

    archive = get_archive_state(job, mode)
    if archive.get("status") in {"downloading", "zipping"}:
        return {"type": "archive", "data": archive}
    if archive.get("status") == "ready" and archive.get("file_name"):
        return {"type": "archive", "data": archive}

    task = asyncio.create_task(build_local_archive(job, mode))
    download_tasks.add(task)
    task.add_done_callback(download_tasks.discard)
    return {"type": "archive", "data": archive}


@app.get("/api/job/{job_id}/archive/status")
async def get_job_archive_status(job_id: str, mode: str = "flat"):
    job = playlist_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado o expirado")
    refresh_job_status(job)
    mode = "folder" if mode == "folder" else "flat"
    return {"type": "archive", "data": get_archive_state(job, mode)}


@app.get("/api/job/{job_id}/archive/download")
async def download_job_archive(job_id: str, mode: str = "flat"):
    return await download_job_zip(job_id, mode)


@app.post("/api/job/{job_id}/folder/start")
async def start_job_folder(job_id: str):
    """Guarda la playlist como una carpeta local real en el backend."""
    job = playlist_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado o expirado")

    refresh_job_status(job)
    if not job.get("bot_finished"):
        raise HTTPException(409, "Espera a que el bot termine con Finished.")

    tracks = [t for t in job.get("tracks", []) if t.get("status") != "error"]
    if not tracks:
        raise HTTPException(404, "No hay canciones para guardar")

    folder = get_folder_state(job)
    if folder.get("status") == "downloading":
        return {"type": "folder", "data": folder}
    if folder.get("status") == "ready" and folder.get("path"):
        return {"type": "folder", "data": folder}

    task = asyncio.create_task(build_local_folder(job))
    download_tasks.add(task)
    task.add_done_callback(download_tasks.discard)
    return {"type": "folder", "data": folder}


@app.get("/api/job/{job_id}/folder/status")
async def get_job_folder_status(job_id: str):
    job = playlist_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado o expirado")
    refresh_job_status(job)
    return {"type": "folder", "data": get_folder_state(job)}


@app.post("/api/job/{job_id}/folder/open")
async def open_job_folder(job_id: str):
    """Abre la carpeta local generada en el explorador de archivos."""
    job = playlist_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado o expirado")

    folder = get_folder_state(job)
    folder_path = folder.get("path")
    if folder.get("status") != "ready" or not folder_path:
        raise HTTPException(409, "La carpeta aun no esta lista")

    path = Path(str(folder_path))
    if not path.exists() or not path.is_dir():
        raise HTTPException(404, "Carpeta no encontrada")

    try:
        subprocess.Popen(
            ["xdg-open", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        raise HTTPException(500, f"No se pudo abrir la carpeta: {exc}") from exc

    return {"ok": True, "path": str(path)}


@app.get("/api/job/{job_id}/track/{track_id}")
async def download_job_track(job_id: str, track_id: str):
    """Descarga una cancion lista con nombre humano"""
    job = playlist_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado o expirado")

    track = next((t for t in job.get("tracks", []) if t.get("id") == track_id), None)
    if not track:
        raise HTTPException(404, "Cancion no encontrada")

    await ensure_track_local(job, track)
    if track.get("status") != "ready":
        raise HTTPException(409, track.get("error") or "La cancion aun no esta lista")

    source = resolve_track_source(track)
    if not source or not source.exists():
        raise HTTPException(404, "Archivo no encontrado")

    filename = track.get("friendly_name") or friendly_track_filename(track)
    media_type = "audio/flac" if source.suffix.lower() == ".flac" else "audio/mpeg"
    return FileResponse(
        path=str(source),
        media_type=media_type,
        filename=filename,
    )


@app.get("/api/jobs")
async def list_jobs():
    """Debug: lista jobs temporales activos"""
    data = []
    for job in playlist_jobs.values():
        refresh_job_status(job)
        data.append({
            "id": job["id"],
            "playlist_title": job.get("playlist_title"),
            "status": job["status"],
            "message": job["message"],
            "counts": job.get("counts", {}),
            "bot_finished": job.get("bot_finished", False),
            "expected_total": job.get("expected_total"),
            "zip_file": job.get("zip_file"),
            "archive": job.get("archive"),
            "folder_archive": job.get("folder_archive"),
            "folder": job.get("folder"),
            "skipped": job.get("skipped", []),
            "tracks_preview": [
                {
                    "title": t.get("title"),
                    "performer": t.get("performer"),
                    "status": t.get("status"),
                    "progress": t.get("progress"),
                    "original_name": t.get("original_name"),
                    "friendly_name": t.get("friendly_name"),
                    "download_url": t.get("download_url"),
                }
                for t in job.get("tracks", [])[:10]
            ],
        })
    return {"jobs": data}


@app.post("/api/job/{job_id}/button")
async def click_job_button(job_id: str, req: JobButtonRequest):
    """Hace click en cualquier botón inline del bot dentro de un job temporal"""
    global active_link_job_id
    tg = require_telegram_client()
    job = playlist_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado o expirado")

    entity = await tg.get_entity(BOT_USERNAME)
    msg = await tg.get_messages(entity, ids=req.message_id)
    if not msg or not msg.buttons:
        raise HTTPException(404, "Mensaje del bot no encontrado o sin botones")

    try:
        row = msg.buttons[req.row_index]
        btn = row[req.button_index]
    except (IndexError, TypeError):
        raise HTTPException(400, "Índice de botón inválido")

    if not hasattr(btn, "data") or not btn.data:
        raise HTTPException(400, "El botón no tiene callback data")

    active_link_job_id = job_id
    job["status"] = "processing"
    job["message"] = f"Ejecutando: {btn.text}"
    await tg(GetBotCallbackAnswerRequest(
        peer=entity,
        msg_id=req.message_id,
        data=btn.data,
    ))
    return {"type": "job", "data": job}


@app.post("/api/callback")
async def handle_callback(req: SelectRequest):
    """Handler genérico para cualquier callback de botón inline por message_id + data"""
    pass


@app.get("/downloads/{file_name}")
async def serve_download(file_name: str):
    """Servir archivos de audio descargados"""
    file_path = DOWNLOADS_DIR / file_name
    
    # Seguridad: no permitir path traversal
    if ".." in file_name or "/" in file_name:
        raise HTTPException(400, "Nombre de archivo inválido")
    
    if not file_path.exists():
        raise HTTPException(404, "Archivo no encontrado")
    
    media_type = "audio/flac" if file_name.endswith(".flac") else "audio/mpeg"
    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=file_name,
    )


@app.get("/api/history")
async def get_history():
    """Obtener historial de mensajes recientes con el bot"""
    tg = require_telegram_client()
    entity = await tg.get_entity(BOT_USERNAME)
    messages = []
    async for msg in tg.iter_messages(entity, limit=20):
        messages.append({
            "id": msg.id,
            "text": msg.text or "",
            "date": msg.date.isoformat(),
            "from_bot": msg.from_id is not None,
            "has_audio": bool(msg.audio),
            "has_buttons": bool(msg.buttons),
        })
    return {"messages": messages}


@app.get("/favicon.ico")
async def favicon():
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
        "<rect width='64' height='64' rx='14' fill='#7c5cfc'/>"
        "<path d='M23 18v24.5a7 7 0 1 1-4-6.3V15h27v20.5a7 7 0 1 1-4-6.3V18H23z' fill='white'/>"
        "</svg>"
    )
    return Response(content=svg, media_type="image/svg+xml")


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
