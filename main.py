import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

BASE = Path(__file__).resolve().parent
# On Render, set SFX_DATA_DIR=/var/data and attach a persistent disk there.
DATA_DIR = Path(os.environ.get("SFX_DATA_DIR", str(BASE))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "sfx_network.db"
VAULT = DATA_DIR / "private_files"  # Private legacy-file storage.
MAX_FILE_SIZE = 100 * 1024 * 1024
SESSION_HOURS = 12
PASSWORD_ITERATIONS = 310_000
VAULT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="SFX Networks")
online_sockets = {}  # username -> set of WebSocket connections


def now():
    return datetime.now(timezone.utc)


def timestamp():
    return now().isoformat()


@contextmanager
def database():
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 20000")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    result = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return salt.hex() + ":" + result.hex()


def check_password(password, stored):
    try:
        salt_hex, expected = stored.split(":", 1)
        actual = hash_password(password, bytes.fromhex(salt_hex))
        return hmac.compare_digest(actual.split(":", 1)[1], expected)
    except (ValueError, TypeError):
        return False


def audit(username, action, detail=""):
    with database() as db:
        db.execute(
            "INSERT INTO audit (username, action, detail, created_at) VALUES (?, ?, ?, ?)",
            (username, action, str(detail)[:300], timestamp())
        )



def file_category(extension):
    extension = (extension or "").lower()
    groups = {
        "Images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".heic"},
        "Documents": {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".md"},
        "Videos": {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"},
        "Audio": {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"},
        "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
        "Code": {".py", ".js", ".html", ".css", ".java", ".json", ".xml", ".sql", ".ps1", ".bat"},
    }
    for label, extensions in groups.items():
        if extension in extensions:
            return label
    return "Other"


def initialize():
    with database() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'employee')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                uploader TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS call_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caller TEXT NOT NULL,
                recipient TEXT NOT NULL,
                call_type TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT
            );
        """)
        # Store newly uploaded file bytes inside SQLite. Keep legacy file rows usable.
        file_columns = {row["name"] for row in db.execute("PRAGMA table_info(files)").fetchall()}
        if "content" not in file_columns:
            db.execute("ALTER TABLE files ADD COLUMN content BLOB")
        if "category" not in file_columns:
            db.execute("ALTER TABLE files ADD COLUMN category TEXT NOT NULL DEFAULT 'Other'")
        # Categorise older files automatically, without changing their stored bytes.
        old_files = db.execute("SELECT id, name FROM files WHERE category = 'Other'").fetchall()
        for old in old_files:
            ext = Path(old["name"]).suffix.lower()
            category = file_category(ext)
            if category != "Other":
                db.execute("UPDATE files SET category = ? WHERE id = ?", (category, old["id"]))
        # Import legacy vault files into the database when possible.
        legacy = db.execute("SELECT id, stored_name, content FROM files WHERE content IS NULL").fetchall()
        for row in legacy:
            old_path = VAULT / row["stored_name"]
            if old_path.is_file():
                try:
                    db.execute("UPDATE files SET content = ? WHERE id = ?", (old_path.read_bytes(), row["id"]))
                except OSError:
                    pass
        if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            initial_password = secrets.token_urlsafe(24)
            db.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
                ("admin", hash_password(initial_password), timestamp())
            )
            print("\n===== SFX NETWORKS ADMIN SETUP =====")
            print("Username: admin")
            print("Initial password:", initial_password)
            print("Save this password now.")
            print("====================================\n")


initialize()


def get_user(request):
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    if not token or len(token) > 200:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with database() as db:
        return db.execute(
            """SELECT users.username, users.role
               FROM sessions JOIN users ON users.username = sessions.username
               WHERE sessions.token_hash = ? AND sessions.expires_at > ?""",
            (token_hash, timestamp())
        ).fetchone()


def user_from_token(token):
    if not token or len(token) > 200:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with database() as db:
        return db.execute(
            """SELECT users.username, users.role
               FROM sessions JOIN users ON users.username = sessions.username
               WHERE sessions.token_hash = ? AND sessions.expires_at > ?""",
            (token_hash, timestamp())
        ).fetchone()


def unauthorized():
    return JSONResponse({"error": "Please sign in."}, status_code=401)


def forbidden():
    return JSONResponse({"error": "Administrator permission required."}, status_code=403)


def create_session(username):
    token = secrets.token_urlsafe(32)
    with database() as db:
        db.execute(
            "INSERT INTO sessions (token_hash, username, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (hashlib.sha256(token.encode()).hexdigest(),
             username, (now() + timedelta(hours=SESSION_HOURS)).isoformat(), timestamp())
        )
    return token


def is_admin(user):
    return user and user["role"] == "admin"


async def send_to_user(username, payload):
    sockets = list(online_sockets.get(username, set()))
    for ws in sockets:
        try:
            await ws.send_json(payload)
        except Exception:
            online_sockets.get(username, set()).discard(ws)


async def broadcast_presence():
    online = sorted(name for name, sockets in online_sockets.items() if sockets)
    for name, sockets in list(online_sockets.items()):
        for ws in list(sockets):
            try:
                await ws.send_json({"type": "presence", "online": online})
            except Exception:
                sockets.discard(ws)


@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse(BASE / "index.html", media_type="text/html")


@app.get("/app.js")
def javascript():
    return FileResponse(BASE / "app.js", media_type="text/javascript")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/health")
def health():
    return {"service": "SFX Networks", "status": "running", "database": DB_PATH.name}


@app.post("/api/login")
async def login(request: Request):
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON."}, status_code=400)
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if len(username) > 32 or len(password) > 200:
        return JSONResponse({"error": "Invalid credentials."}, status_code=401)
    with database() as db:
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user or not check_password(password, user["password_hash"]):
        audit(username or "unknown", "login_failed")
        return JSONResponse({"error": "Invalid username or password."}, status_code=401)
    token = create_session(username)
    audit(username, "login_success")
    return {"token": token, "username": username, "role": user["role"],
            "expires_in_seconds": SESSION_HOURS * 3600}


@app.get("/api/me")
def me(request: Request):
    user = get_user(request)
    if not user:
        return unauthorized()
    return {"username": user["username"], "role": user["role"]}


@app.post("/api/logout")
def logout(request: Request):
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return {"message": "Signed out."}
    token_hash = hashlib.sha256(header[7:].strip().encode()).hexdigest()
    with database() as db:
        row = db.execute("SELECT username FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
    if row:
        audit(row["username"], "logout")
    return {"message": "Signed out."}


@app.get("/api/contacts")
def contacts(request: Request):
    user = get_user(request)
    if not user:
        return unauthorized()
    with database() as db:
        rows = db.execute("SELECT username, role FROM users ORDER BY username COLLATE NOCASE").fetchall()
    return {"contacts": [
        {"username": r["username"], "role": r["role"],
         "online": bool(online_sockets.get(r["username"]))}
        for r in rows if r["username"] != user["username"]
    ]}


@app.get("/api/messages")
def get_messages(request: Request, with_user: str = ""):
    user = get_user(request)
    if not user:
        return unauthorized()
    other = with_user.strip()
    if not other or len(other) > 32:
        return JSONResponse({"error": "Choose an employee to message."}, status_code=400)
    with database() as db:
        exists = db.execute("SELECT 1 FROM users WHERE username = ?", (other,)).fetchone()
        if not exists:
            return JSONResponse({"error": "Employee not found."}, status_code=404)
        rows = db.execute(
            """SELECT id, sender, recipient, body, created_at FROM messages
               WHERE (sender = ? AND recipient = ?) OR (sender = ? AND recipient = ?)
               ORDER BY id DESC LIMIT 300""",
            (user["username"], other, other, user["username"])
        ).fetchall()
    return {"messages": [dict(r) for r in reversed(rows)]}


@app.post("/api/messages")
async def send_message(request: Request):
    user = get_user(request)
    if not user:
        return unauthorized()
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON."}, status_code=400)
    recipient = str(data.get("recipient", "")).strip()
    body = str(data.get("body", "")).strip()
    if not recipient or len(recipient) > 32 or len(body) > 5000 or not body:
        return JSONResponse({"error": "Enter a recipient and a message (max 5,000 characters)."}, status_code=400)
    with database() as db:
        if not db.execute("SELECT 1 FROM users WHERE username = ?", (recipient,)).fetchone():
            return JSONResponse({"error": "Employee not found."}, status_code=404)
        cur = db.execute(
            "INSERT INTO messages (sender, recipient, body, created_at) VALUES (?, ?, ?, ?)",
            (user["username"], recipient, body, timestamp())
        )
        msg_id = cur.lastrowid
        row = db.execute("SELECT id, sender, recipient, body, created_at FROM messages WHERE id = ?", (msg_id,)).fetchone()
    message = dict(row)
    audit(user["username"], "message_sent", recipient)
    await send_to_user(recipient, {"type": "message", "message": message})
    return {"message": message}


@app.get("/api/files")
def list_files(request: Request):
    user = get_user(request)
    if not user:
        return unauthorized()
    with database() as db:
        rows = db.execute(
            "SELECT id, name, size, uploader, uploaded_at, category FROM files ORDER BY category COLLATE NOCASE, name COLLATE NOCASE LIMIT 500"
        ).fetchall()
    return {"files": [dict(r) for r in rows]}


@app.post("/api/files")
async def upload_file(request: Request, file: UploadFile = File(...), recipient: str = ""):
    user = get_user(request)
    if not user:
        await file.close()
        return unauthorized()
    original_name = Path(file.filename or "upload.bin").name.replace("\x00", "")[:200]
    if not original_name or original_name in (".", ".."):
        original_name = "upload.bin"
    content = bytearray()
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > MAX_FILE_SIZE:
                return JSONResponse({"error": "Maximum file size is 100 MB."}, status_code=413)
        file_id = uuid.uuid4().hex
        # File bytes are stored as a BLOB inside sfx_network.db, not in a public folder.
        with database() as db:
            db.execute(
                """INSERT INTO files (id, name, stored_name, size, uploader, uploaded_at, content, category)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (file_id, original_name, file_id + ".bin", len(content), user["username"], timestamp(),
                 sqlite3.Binary(content), file_category(Path(original_name).suffix))
            )
        audit(user["username"], "file_uploaded", original_name)
        return {"message": "Upload complete.", "id": file_id}
    finally:
        await file.close()


@app.get("/api/files/{file_id}")
def download_file(file_id: str, request: Request):
    user = get_user(request)
    if not user:
        return unauthorized()
    with database() as db:
        record = db.execute("SELECT name, stored_name, content FROM files WHERE id = ?", (file_id,)).fetchone()
    if not record:
        return JSONResponse({"error": "File not found."}, status_code=404)
    if record["content"] is not None:
        # Return the SQLite BLOB directly; no temporary download copy is created.
        from urllib.parse import quote
        audit(user["username"], "file_downloaded", record["name"])
        safe_name = record["name"].replace("\\r", "").replace("\\n", "")
        disposition = "attachment; filename*=UTF-8''" + quote(safe_name)
        return Response(
            content=record["content"],
            media_type="application/octet-stream",
            headers={"Content-Disposition": disposition}
        )
    legacy_path = VAULT / record["stored_name"]
    if not legacy_path.is_file():
        return JSONResponse({"error": "Stored file is missing."}, status_code=404)
    audit(user["username"], "file_downloaded", record["name"])
    return FileResponse(legacy_path, filename=record["name"])


@app.delete("/api/files/{file_id}")
def delete_file(file_id: str, request: Request):
    user = get_user(request)
    if not user:
        return unauthorized()
    with database() as db:
        row = db.execute("SELECT name, uploader FROM files WHERE id = ?", (file_id,)).fetchone()
        if not row:
            return JSONResponse({"error": "File not found."}, status_code=404)
        if user["role"] != "admin" and row["uploader"] != user["username"]:
            return forbidden()
        db.execute("DELETE FROM files WHERE id = ?", (file_id,))
    audit(user["username"], "file_deleted", row["name"])
    return {"message": "File deleted."}


@app.post("/api/admin/users")
async def create_employee(request: Request):
    admin = get_user(request)
    if not admin:
        return unauthorized()
    if admin["role"] != "admin":
        return forbidden()
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON."}, status_code=400)
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        return JSONResponse({"error": "Username must be 3–32 letters, digits, dots, dashes or underscores."}, status_code=400)
    if len(password) < 12 or len(password) > 200:
        return JSONResponse({"error": "Password must be at least 12 characters and at most 200."}, status_code=400)
    try:
        with database() as db:
            db.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'employee', ?)",
                (username, hash_password(password), timestamp())
            )
    except sqlite3.IntegrityError:
        return JSONResponse({"error": "That username already exists."}, status_code=409)
    audit(admin["username"], "employee_created", username)
    return {"message": "Employee account created.", "username": username}


@app.get("/api/admin/users")
def list_users(request: Request):
    admin = get_user(request)
    if not admin:
        return unauthorized()
    if admin["role"] != "admin":
        return forbidden()
    with database() as db:
        rows = db.execute("SELECT username, role, created_at FROM users ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, username COLLATE NOCASE").fetchall()
        employee_count = db.execute("SELECT COUNT(*) FROM users WHERE role='employee'").fetchone()[0]
        account_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    return {"employee_count": employee_count, "account_count": account_count, "users": [dict(r) for r in rows]}


@app.delete("/api/admin/users/{username}")
def delete_employee(username: str, request: Request):
    admin = get_user(request)
    if not admin:
        return unauthorized()
    if admin["role"] != "admin":
        return forbidden()
    username = username.strip()
    with database() as db:
        target = db.execute("SELECT username, role FROM users WHERE username = ?", (username,)).fetchone()
        if not target:
            return JSONResponse({"error": "Account not found."}, status_code=404)
        if target["role"] == "admin":
            return JSONResponse({"error": "Administrator accounts cannot be deleted here."}, status_code=400)
        db.execute("DELETE FROM sessions WHERE username = ?", (username,))
        db.execute("DELETE FROM users WHERE username = ?", (username,))
    audit(admin["username"], "employee_deleted", username)
    return {"message": f"Employee {username} deleted."}


@app.get("/api/admin/stats")
def admin_stats(request: Request):
    admin = get_user(request)
    if not admin:
        return unauthorized()
    if admin["role"] != "admin":
        return forbidden()
    with database() as db:
        employee_count = db.execute("SELECT COUNT(*) FROM users WHERE role='employee'").fetchone()[0]
        account_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active_sessions = db.execute("SELECT COUNT(*) FROM sessions WHERE expires_at > ?", (timestamp(),)).fetchone()[0]
        message_count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        file_count = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    return {"employee_count": employee_count, "account_count": account_count, "active_sessions": active_sessions,
            "message_count": message_count, "file_count": file_count}


@app.get("/api/admin/audit")
def audit_log(request: Request):
    admin = get_user(request)
    if not admin:
        return unauthorized()
    if admin["role"] != "admin":
        return forbidden()
    with database() as db:
        rows = db.execute("SELECT username, action, detail, created_at FROM audit ORDER BY id DESC LIMIT 200").fetchall()
    return {"events": [dict(r) for r in rows]}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    username = None
    try:
        first = await websocket.receive_json()
        if first.get("type") != "auth":
            await websocket.close(code=4401)
            return
        user = user_from_token(str(first.get("token", "")))
        if not user:
            await websocket.close(code=4401)
            return
        username = user["username"]
        online_sockets.setdefault(username, set()).add(websocket)
        await websocket.send_json({"type": "ready", "username": username})
        await broadcast_presence()
        while True:
            payload = await websocket.receive_json()
            kind = payload.get("type")
            if kind == "ping":
                await websocket.send_json({"type": "pong"})
            elif kind in ("call-offer", "call-answer", "ice-candidate", "call-end", "call-reject"):
                recipient = str(payload.get("to", "")).strip()
                if not recipient or len(recipient) > 32:
                    continue
                if not online_sockets.get(recipient):
                    await websocket.send_json({"type": "peer-unavailable", "to": recipient})
                    continue
                clean = {k: payload.get(k) for k in ("type", "to", "callType", "sdp", "candidate", "reason")}
                clean["from"] = username
                if kind == "call-offer":
                    with database() as db:
                        db.execute("INSERT INTO call_logs (caller, recipient, call_type, started_at) VALUES (?, ?, ?, ?)",
                                   (username, recipient, str(payload.get("callType", "audio"))[:10], timestamp()))
                await send_to_user(recipient, clean)
            else:
                await websocket.send_json({"type": "error", "message": "Unsupported event."})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if username:
            sockets = online_sockets.get(username)
            if sockets:
                sockets.discard(websocket)
                if not sockets:
                    online_sockets.pop(username, None)
            await broadcast_presence()
