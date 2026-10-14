from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI, File, Form, Header, HTTPException, Query, Request,
    UploadFile, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ============================================================
# SFX NETWORKS — PERFORMANCE UPDATE 3.0
# FastAPI + SQLite WAL + WebSockets + Quick Transfer
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
FILES_DIR = BASE_DIR / "shared_files"
DB_PATH = BASE_DIR / "sfx_networks.db"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
FILES_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_MESSAGE_LENGTH = 2000
MAX_DEVICE_NAME_LENGTH = 48
MAX_ACTIVE_DEVICES = 100
MAX_CHAT_HISTORY = 200
MAX_FILE_LIST = 500
MAX_UPLOADS_PER_MINUTE = 10
MAX_MESSAGES_PER_MINUTE = 30
MAX_JOIN_PER_MINUTE = 20
WS_SEND_TIMEOUT = 3.0

NETWORK_KEY = os.environ.get("IC_NETWORK_KEY", "").strip()
if not NETWORK_KEY:
    raise RuntimeError(
        "IC_NETWORK_KEY is not configured. In PowerShell run:\n"
        '$env:IC_NETWORK_KEY = "use-a-long-private-random-key"\n'
        "Then restart the server."
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    """Open a short-lived SQLite connection with low-latency settings."""
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 10000")
    db.execute("PRAGMA synchronous = NORMAL")
    db.execute("PRAGMA temp_store = MEMORY")
    return db


def init_db() -> None:
    with get_db() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                device_name TEXT NOT NULL,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                stored_name TEXT NOT NULL,
                filename TEXT NOT NULL,
                size INTEGER NOT NULL,
                device_id TEXT NOT NULL,
                device_name TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                media_type TEXT NOT NULL DEFAULT 'other',
                caption TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_devices_seen ON devices(last_seen DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_device ON messages(device_id);
            CREATE INDEX IF NOT EXISTS idx_files_uploaded ON files(uploaded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_files_device ON files(device_id);
            """
        )
        # Safely migrate databases created by version 2.0.
        columns = {row[1] for row in db.execute("PRAGMA table_info(files)").fetchall()}
        if "media_type" not in columns:
            db.execute("ALTER TABLE files ADD COLUMN media_type TEXT NOT NULL DEFAULT 'other'")
        if "caption" not in columns:
            db.execute("ALTER TABLE files ADD COLUMN caption TEXT NOT NULL DEFAULT ''")


active_sockets: dict[str, WebSocket] = {}
rate_limits: dict[str, list[float]] = {}
rate_limit_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(init_db)
    yield
    sockets = list(active_sockets.values())
    active_sockets.clear()
    if sockets:
        await asyncio.gather(
            *(safe_close(ws, 1001, "Server shutting down") for ws in sockets),
            return_exceptions=True,
        )


app = FastAPI(
    title="SFX Networks",
    description="Private IC Productions messaging and Quick Transfer network.",
    version="3.0.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


async def safe_close(ws: WebSocket, code: int, reason: str) -> None:
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        pass


def check_key(key: str | None) -> None:
    if not key or not secrets.compare_digest(key, NETWORK_KEY):
        raise HTTPException(status_code=401, detail="Invalid network key")


def get_request_key(x_network_key: str | None, key: str | None) -> str | None:
    return x_network_key or key


async def enforce_rate_limit(identity: str, limit: int, period: int = 60) -> None:
    """Bounded, concurrency-safe in-memory rate limiter."""
    now = time.monotonic()
    async with rate_limit_lock:
        entries = rate_limits.setdefault(identity, [])
        # Prune expired entries and avoid unbounded stale identities.
        entries[:] = [stamp for stamp in entries if now - stamp < period]
        if len(entries) >= limit:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please wait before trying again.",
            )
        entries.append(now)
        if len(rate_limits) > 5000:
            for key in list(rate_limits)[:1000]:
                if not rate_limits[key] or now - rate_limits[key][-1] >= period:
                    rate_limits.pop(key, None)


def clean_filename(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n\x00")
    name = name.strip().strip(".")
    return name[:180] or "shared-file"


def infer_media_type(filename: str, requested: str | None = None) -> str:
    if requested in {"image", "video", "short", "document", "other"}:
        return requested
    mime, _ = mimetypes.guess_type(filename)
    suffix = Path(filename).suffix.lower()
    if mime and mime.startswith("image/"):
        return "image"
    if mime and mime.startswith("video/"):
        return "video"
    if suffix in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv", ".rtf", ".odt"}:
        return "document"
    return "other"


def get_device(device_id: str) -> sqlite3.Row | None:
    with get_db() as db:
        return db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()


def require_device(device_id: str | None) -> sqlite3.Row:
    if not device_id:
        raise HTTPException(status_code=401, detail="Connect to SFX Networks first.")
    device = get_device(device_id)
    if not device:
        raise HTTPException(status_code=401, detail="Unknown device. Reconnect.")
    return device


def touch_device(device_id: str) -> None:
    with get_db() as db:
        db.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (utc_now(), device_id))


def public_device(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "device_id": row["id"],
        "name": row["name"],
        "device_name": row["name"],
        "created_at": row["created_at"],
        "last_seen": row["last_seen"],
        "online": row["id"] in active_sockets,
    }


def public_file(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "file_id": row["id"],
        "filename": row["filename"],
        "name": row["filename"],
        "size": row["size"],
        "device_id": row["device_id"],
        "device_name": row["device_name"],
        "uploaded_at": row["uploaded_at"],
        "media_type": row["media_type"],
        "type": row["media_type"],
        "is_short": row["media_type"] == "short",
        "short": row["media_type"] == "short",
        "caption": row["caption"],
    }


async def broadcast(payload: dict[str, Any]) -> None:
    """Send concurrently so one slow client doesn't stall every other client."""
    if not active_sockets:
        return
    message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    snapshot = list(active_sockets.items())

    async def send_one(device_id: str, ws: WebSocket) -> str | None:
        try:
            await asyncio.wait_for(ws.send_text(message), timeout=WS_SEND_TIMEOUT)
            return None
        except Exception:
            return device_id

    results = await asyncio.gather(
        *(send_one(device_id, ws) for device_id, ws in snapshot),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, str) and active_sockets.get(result) is not None:
            active_sockets.pop(result, None)


async def get_public_files() -> list[dict[str, Any]]:
    def query() -> list[dict[str, Any]]:
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM files ORDER BY uploaded_at DESC LIMIT ?", (MAX_FILE_LIST,)
            ).fetchall()
        return [public_file(row) for row in rows]
    return await asyncio.to_thread(query)


async def get_public_devices() -> list[dict[str, Any]]:
    def query() -> list[dict[str, Any]]:
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM devices ORDER BY last_seen DESC LIMIT ?", (MAX_ACTIVE_DEVICES,)
            ).fetchall()
        return [public_device(row) for row in rows]
    return await asyncio.to_thread(query)


async def load_message_history() -> list[dict[str, Any]]:
    def query() -> list[dict[str, Any]]:
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM messages ORDER BY timestamp DESC LIMIT ?", (MAX_CHAT_HISTORY,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "device_id": row["device_id"],
                "name": row["device_name"],
                "device_name": row["device_name"],
                "text": row["text"],
                "timestamp": row["timestamp"],
                "created_at": row["timestamp"],
            }
            for row in reversed(rows)
        ]
    return await asyncio.to_thread(query)


@app.get("/")
async def home():
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        return JSONResponse({"name": "SFX Networks", "version": app.version, "status": "running", "message": "Place your frontend at static/index.html"})
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health():
    return {"status": "ok", "name": "SFX Networks", "version": app.version, "time": utc_now()}


@app.get("/api")
async def api_info():
    return {
        "name": "SFX Networks API",
        "version": app.version,
        "endpoints": ["/health", "/api/join", "/api/devices", "/api/messages", "/api/files", "/api/files/{file_id}/download", "/ws"],
    }


@app.post("/api/join")
async def join_network(
    request: Request,
    x_network_key: str | None = Header(default=None, alias="X-Network-Key"),
    key: str | None = Query(default=None),
):
    check_key(get_request_key(x_network_key, key))
    client = request.client.host if request.client else "unknown"
    await enforce_rate_limit("join:" + client, MAX_JOIN_PER_MINUTE)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON request body.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")

    name = "".join(ch for ch in str(body.get("device_name") or body.get("name") or "").strip() if ch.isprintable())[:MAX_DEVICE_NAME_LENGTH]
    if not name:
        raise HTTPException(status_code=400, detail="A valid device name is required.")
    requested_id = str(body.get("device_id") or "").strip()
    now = utc_now()

    def write() -> dict[str, Any]:
        with get_db() as db:
            existing = db.execute("SELECT * FROM devices WHERE id = ?", (requested_id,)).fetchone() if requested_id else None
            if existing:
                device_id = existing["id"]
                db.execute("UPDATE devices SET name = ?, last_seen = ? WHERE id = ?", (name, now, device_id))
            else:
                device_id = str(uuid.uuid4())
                db.execute("INSERT INTO devices (id, name, created_at, last_seen) VALUES (?, ?, ?, ?)", (device_id, name, now, now))
            row = db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
            return {"ok": True, "device_id": device_id, "id": device_id, "device": public_device(row), "message": "Connected to SFX Networks."}

    result = await asyncio.to_thread(write)
    return result


@app.get("/api/devices")
async def list_devices(
    x_network_key: str | None = Header(default=None, alias="X-Network-Key"),
    key: str | None = Query(default=None),
):
    check_key(get_request_key(x_network_key, key))
    devices = await get_public_devices()
    return {"devices": devices, "count": len(devices)}


@app.get("/api/messages")
async def list_messages(
    x_network_key: str | None = Header(default=None, alias="X-Network-Key"),
    key: str | None = Query(default=None),
):
    check_key(get_request_key(x_network_key, key))
    messages = await load_message_history()
    return {"messages": messages, "count": len(messages)}


@app.delete("/api/messages/{message_id}")
async def delete_message(
    message_id: str,
    x_network_key: str | None = Header(default=None, alias="X-Network-Key"),
    x_device_id: str | None = Header(default=None, alias="X-Device-ID"),
    key: str | None = Query(default=None),
    device_id: str | None = Query(default=None),
):
    """Delete only a message belonging to the requesting device."""
    check_key(get_request_key(x_network_key, key))
    owner_id = x_device_id or device_id
    if not owner_id:
        raise HTTPException(status_code=400, detail="Device ID is required to delete your own message.")

    def delete() -> bool:
        with get_db() as db:
            cursor = db.execute("DELETE FROM messages WHERE id = ? AND device_id = ?", (message_id, owner_id))
            return cursor.rowcount > 0

    deleted = await asyncio.to_thread(delete)
    if not deleted:
        raise HTTPException(status_code=404, detail="Message not found or it does not belong to your device.")
    await broadcast({"type": "message_deleted", "id": message_id})
    return {"ok": True, "deleted_id": message_id}


@app.get("/api/files")
async def list_files(
    x_network_key: str | None = Header(default=None, alias="X-Network-Key"),
    key: str | None = Query(default=None),
):
    check_key(get_request_key(x_network_key, key))
    files = await get_public_files()
    return {"files": files, "count": len(files)}


@app.post("/api/files")
async def upload_file(
    file: UploadFile = File(...),
    x_network_key: str | None = Header(default=None, alias="X-Network-Key"),
    key: str | None = Form(default=None),
    device_id: str | None = Form(default=None),
    device_name: str | None = Form(default=None),
    transfer_type: str | None = Form(default=None),
    caption: str | None = Form(default=None),
):
    check_key(get_request_key(x_network_key, key))
    device = await asyncio.to_thread(require_device, device_id)
    await enforce_rate_limit("upload:" + device["id"], MAX_UPLOADS_PER_MINUTE)

    filename = clean_filename(file.filename or "shared-file")
    media_type = infer_media_type(filename, transfer_type)
    caption = "".join(ch for ch in (caption or "") if ch.isprintable())[:180]
    file_id = str(uuid.uuid4())
    stored_name = file_id + ".blob"
    destination = FILES_DIR / stored_name
    size = 0

    try:
        # Read asynchronously and move disk writes off the event loop.
        handle = await asyncio.to_thread(destination.open, "wb")
        try:
            while True:
                chunk = await file.read(256 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    raise HTTPException(status_code=413, detail="File exceeds the 25 MB limit.")
                await asyncio.to_thread(handle.write, chunk)
        finally:
            await asyncio.to_thread(handle.close)
    except Exception:
        await asyncio.to_thread(destination.unlink, missing_ok=True)
        raise
    finally:
        await file.close()

    now = utc_now()
    uploader = device["name"]

    def save_metadata() -> None:
        with get_db() as db:
            db.execute(
                """INSERT INTO files
                   (id, stored_name, filename, size, device_id, device_name, uploaded_at, media_type, caption)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (file_id, stored_name, filename, size, device["id"], uploader, now, media_type, caption),
            )

    try:
        await asyncio.to_thread(save_metadata)
    except Exception as exc:
        await asyncio.to_thread(destination.unlink, missing_ok=True)
        raise HTTPException(status_code=500, detail="Could not save file metadata.") from exc

    await broadcast({"type": "files", "files": await get_public_files()})
    return {"ok": True, "message": "File uploaded successfully.", "file": {
        "id": file_id, "file_id": file_id, "filename": filename, "size": size,
        "device_id": device["id"], "device_name": uploader, "media_type": media_type,
        "type": media_type, "caption": caption, "uploaded_at": now,
    }}


@app.get("/api/files/{file_id}/download")
async def download_file(
    file_id: str,
    x_network_key: str | None = Header(default=None, alias="X-Network-Key"),
    key: str | None = Query(default=None),
):
    check_key(get_request_key(x_network_key, key))

    def query():
        with get_db() as db:
            return db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    row = await asyncio.to_thread(query)
    if not row:
        raise HTTPException(status_code=404, detail="File not found.")
    path = FILES_DIR / row["stored_name"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Stored file is missing.")
    return FileResponse(
        path,
        filename=row["filename"],
        media_type=mimetypes.guess_type(row["filename"])[0] or "application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    key: str | None = Query(default=None),
    device_id: str | None = Query(default=None),
):
    if not key or not secrets.compare_digest(key, NETWORK_KEY):
        await websocket.close(code=1008, reason="Invalid network key")
        return

    device = await asyncio.to_thread(get_device, device_id or "")
    if not device:
        await websocket.close(code=1008, reason="Join the network first")
        return

    await websocket.accept()
    device_id = device["id"]
    old = active_sockets.get(device_id)
    if old and old is not websocket:
        await safe_close(old, 1000, "Device reconnected")
    active_sockets[device_id] = websocket
    await asyncio.to_thread(touch_device, device_id)

    try:
        await websocket.send_json({"type": "history", "messages": await load_message_history()})
        await broadcast({"type": "devices", "devices": await get_public_devices()})
        while True:
            raw = await websocket.receive_text()
            if len(raw) > 10000:
                await websocket.send_json({"type": "error", "message": "Message payload too large."})
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON message."})
                continue
            if not isinstance(payload, dict):
                continue

            msg_type = payload.get("type")
            if msg_type == "heartbeat":
                await asyncio.to_thread(touch_device, device_id)
                await websocket.send_json({"type": "heartbeat", "ok": True})
                continue
            if msg_type != "message":
                continue

            try:
                await enforce_rate_limit("chat:" + device_id, MAX_MESSAGES_PER_MINUTE)
            except HTTPException as exc:
                await websocket.send_json({"type": "error", "message": exc.detail})
                continue

            text = str(payload.get("text") or payload.get("message") or "").strip()
            if not text:
                continue
            if len(text) > MAX_MESSAGE_LENGTH:
                await websocket.send_json({"type": "error", "message": f"Message exceeds {MAX_MESSAGE_LENGTH} characters."})
                continue

            message = {
                "id": str(uuid.uuid4()),
                "device_id": device_id,
                "name": device["name"],
                "device_name": device["name"],
                "text": text,
                "timestamp": utc_now(),
                "created_at": utc_now(),
            }

            def save_message() -> None:
                with get_db() as db:
                    db.execute(
                        "INSERT INTO messages (id, device_id, device_name, text, timestamp) VALUES (?, ?, ?, ?, ?)",
                        (message["id"], device_id, device["name"], text, message["timestamp"]),
                    )
                    db.execute(
                        "DELETE FROM messages WHERE id IN (SELECT id FROM messages ORDER BY timestamp DESC LIMIT -1 OFFSET ?)",
                        (MAX_CHAT_HISTORY,),
                    )
            await asyncio.to_thread(save_message)
            await broadcast({"type": "message", "message": message})
            await asyncio.to_thread(touch_device, device_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        await safe_close(websocket, 1011, "Internal server error")
    finally:
        if active_sockets.get(device_id) is websocket:
            active_sockets.pop(device_id, None)
            await broadcast({"type": "devices", "devices": await get_public_devices()})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response
