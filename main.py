# main.py (RAILWAY BACKEND - TAM SÜRÜM V3.0)
import json
import sqlite3
import random
import string
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from typing import Dict, List
import os
import shutil
import uuid
import asyncio
import re
import time

# Gizli ayarlar (ADMIN_KEY, CF_TURN_KEY_ID, CF_TURN_API_TOKEN...) main.py'nin yanındaki .env dosyasından okunur.
# Satır formatı: ANAHTAR=deger   (# ile başlayan satırlar yorum). Railway gibi ortam değişkeni verilirse o öncelikli.
# .env dosyasını ASLA GitHub'a yükleme.
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
# Yüklenen dosyaları barındıracak klasörü oluştur ve dışa aç
os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
# --- VERİTABANI KURULUMU ---
def init_db():
    conn = sqlite3.connect("karargah.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, avatar_url TEXT, is_prime INTEGER DEFAULT 0, friend_code TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS servers (name TEXT PRIMARY KEY, owner TEXT, icon_url TEXT, invite_code TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY AUTOINCREMENT, server_name TEXT, name TEXT, type TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, server_name TEXT, channel_name TEXT, sender TEXT, text TEXT, time_str TEXT, msg_type TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS server_members (server_name TEXT, username TEXT, role TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS friends (user1 TEXT, user2 TEXT, status TEXT)''')
    # Kod adı <-> Supabase hesabı eşlemesi: bir kod adını sadece onu ilk alan hesap kullanabilir
    cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if "auth_id" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN auth_id TEXT")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_auth_id ON users(auth_id)")
    conn.commit()
    conn.close()

init_db()

# --- KİMLİK DOĞRULAMA (SUPABASE) ---
# İstemci her istekte Supabase oturum anahtarını (access token) gönderir:
#   REST:      Authorization: Bearer <token>
#   WebSocket: ?token=<token>   (tarayıcı WebSocket'e başlık ekleyemediği için)
# Backend token'ı Supabase'e sorarak doğrular ve kullanıcı adını KENDİ kaydından alır;
# istemcinin gönderdiği "sender/username" alanlarına artık güvenilmez.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://zhvlfezqhzfjflwjzira.supabase.co").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpodmxmZXpxaHpmamZsd2p6aXJhIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODk0NzY3NDAsImV4cCI6MjEwNTA1Mjc0MH0.JWT2S7FfWygc68G3Cmff9KPYM6lf60SxjruY9GQIwk0")
TOKEN_CACHE_SECONDS = 60
_token_cache: Dict[str, tuple] = {}

# Kod adında "_" yasak (DM oda adı DM_<a>_<b> şeklinde), URL'yi bozan karakterler de yasak
USERNAME_RE = re.compile(r"^[^\s/_?#%\\<>\"']{2,24}$")

class AuthError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail

def _fetch_supabase_user(token: str):
    import urllib.request, urllib.error
    req = urllib.request.Request(f"{SUPABASE_URL}/auth/v1/user", headers={
        "apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}", "User-Agent": "karargah-backend/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None
        raise

async def verify_token(token: str) -> dict:
    if not token:
        raise AuthError(401, "Oturum bulunamadı. Lütfen tekrar giriş yapın.")
    now = time.time()
    hit = _token_cache.get(token)
    if hit and hit[1] > now:
        return hit[0]
    try:
        user = await asyncio.to_thread(_fetch_supabase_user, token)
    except Exception as e:
        print("Supabase doğrulama hatası:", e)
        raise AuthError(503, "Kimlik doğrulama servisine ulaşılamadı.")
    if not user or not user.get("id"):
        raise AuthError(401, "Oturum süresi doldu. Lütfen tekrar giriş yapın.")
    if len(_token_cache) > 5000:
        for k in [k for k, v in _token_cache.items() if v[1] <= now]:
            _token_cache.pop(k, None)
    _token_cache[token] = (user, now + TOKEN_CACHE_SECONDS)
    return user

def _resolve_username(auth_id: str, wanted: str) -> str:
    db = get_db()
    try:
        row = db.execute("SELECT username FROM users WHERE auth_id = ?", (auth_id,)).fetchone()
        if row:
            return row["username"]
        wanted = (wanted or "").strip().upper()
        if not USERNAME_RE.match(wanted):
            raise AuthError(400, "Geçersiz kod adı (2-24 karakter; boşluk, _ / ? # % kullanılamaz).")
        row = db.execute("SELECT auth_id FROM users WHERE username = ?", (wanted,)).fetchone()
        if row and row["auth_id"] and row["auth_id"] != auth_id:
            raise AuthError(409, "Bu kod adı başka bir operatöre ait.")
        if row:
            db.execute("UPDATE users SET auth_id = ? WHERE username = ?", (auth_id, wanted))
        else:
            db.execute("INSERT INTO users (username, avatar_url, is_prime, friend_code, auth_id) VALUES (?, '', 0, ?, ?)",
                       (wanted, ''.join(random.choices(string.digits, k=8)), auth_id))
        db.commit()
        return wanted
    except sqlite3.IntegrityError:
        raise AuthError(409, "Bu kod adı başka bir operatöre ait.")
    finally:
        db.close()

async def auth_username(token: str, wanted: str = "") -> str:
    user = await verify_token(token)
    meta_name = (user.get("user_metadata") or {}).get("username") or wanted
    return _resolve_username(user["id"], meta_name)

async def current_user(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    try:
        return await auth_username(token)
    except AuthError as e:
        raise HTTPException(e.status, e.detail)

def dm_participants(room: str):
    if not room.startswith("DM_"):
        return None
    parts = room[3:].split("_")
    return parts if len(parts) == 2 else []

def member_role(db, server_name: str, username: str):
    row = db.execute("SELECT role FROM server_members WHERE server_name = ? AND username = ?", (server_name, username)).fetchone()
    return row["role"] if row else None

def can_access_room(db, room: str, username: str) -> bool:
    parts = dm_participants(room)
    if parts is not None:
        return username in parts
    return member_role(db, room, username) is not None

def require_room_access(db, room: str, username: str):
    if not can_access_room(db, room, username):
        db.close()
        raise HTTPException(403, "Bu karargaha erişim yetkiniz yok.")

def require_manager(db, server_name: str, username: str):
    if member_role(db, server_name, username) not in ("KURUCU", "MODERATÖR"):
        db.close()
        raise HTTPException(403, "Bu işlem için KURUCU veya MODERATÖR olmalısınız.")

def base_url_from(headers, scheme: str) -> str:
    env = os.environ.get("PUBLIC_URL")
    if env:
        return env.rstrip("/")
    proto = headers.get("x-forwarded-proto", scheme)
    host = headers.get("x-forwarded-host") or headers.get("host", "")
    return f"{proto}://{host}"

def is_own_upload(url: str, base: str) -> bool:
    # Avatar/logo/resim linki sadece kendi sunucumuzdaki dosya olabilir
    # (yoksa biri avatarına dış bir link koyup herkesin IP'sini toplayabilir)
    return url == "" or (url.startswith(base + "/uploads/") and "/.." not in url)

# --- WEBSOCKET MİMARİSİ VE HAYALET MODU HAFIZASI ---
operator_statuses = {}

# İstemciden gelip odaya aktarılan (relay) mesaj tipleri.
# NOT: Eskiden voice_ack / voice_leave / role_update / avatar_update / server_update
# burada yoktu ve sunucu bunları sessizce yutuyordu -> ses bağlantısı bazen hiç kurulmuyordu.
RELAY_TYPES = {"offer", "answer", "ice_candidate", "voice_join", "voice_ack", "voice_leave",
               "mute_status", "role_update", "avatar_update", "server_update"}

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Dict[str, WebSocket]] = {}
        self.user_connections: Dict[str, WebSocket] = {}
        # Oda başına: operatör -> bulunduğu ses kanalı (sunucu tarafı takip)
        self.voice_members: Dict[str, Dict[str, str]] = {}

    async def connect(self, websocket: WebSocket, room_id: str, operator_name: str):
        await websocket.accept()
        room = self.active_connections.setdefault(room_id, {})
        old = room.get(operator_name)
        room[operator_name] = websocket
        # Aynı isimle yeni bağlantı geldiyse (yeniden bağlanma / ikinci sekme) eskisini kapat
        if old is not None and old is not websocket:
            try: await old.close(code=4000)
            except Exception: pass

    def disconnect(self, websocket: WebSocket, room_id: str, operator_name: str) -> bool:
        # Sadece bu socket hâlâ kayıtlıysa sil. Eski socket geç kapanırsa yeni bağlantıyı
        # silmesin (önceki kodda kullanıcı "bağlı görünüp" hiçbir sinyal alamıyordu).
        room = self.active_connections.get(room_id)
        if not room or room.get(operator_name) is not websocket:
            return False
        del room[operator_name]
        if not room:
            del self.active_connections[room_id]
        return True

    async def safe_send(self, ws: WebSocket, message: str):
        try: await ws.send_text(message)
        except Exception: pass

    async def broadcast(self, message: str, room_id: str, exclude: WebSocket = None):
        if room_id in self.active_connections:
            for connection in list(self.active_connections[room_id].values()):
                if connection is not exclude:
                    await self.safe_send(connection, message)

    async def send_to(self, message: str, room_id: str, target: str) -> bool:
        ws = self.active_connections.get(room_id, {}).get(target)
        if ws is None: return False
        await self.safe_send(ws, message)
        return True

    async def broadcast_online_users(self, room_id: str):
        if room_id in self.active_connections:
            for op_name, connection in list(self.active_connections[room_id].items()):
                visible_users = []
                for other_op in self.active_connections[room_id].keys():
                    # Kural: Herkes kendini görür, ama başkası GİZLİ HAREKAT'taysa onu göremez
                    if other_op == op_name or operator_statuses.get(other_op, "ÇEVRİMİÇİ") != "GİZLİ HAREKAT":
                        visible_users.append(other_op)
                
                try: await connection.send_text(json.dumps({"type": "online_users", "users": visible_users}))
                except: pass

manager = ConnectionManager()

# --- WEBSOCKET UÇ NOKTALARI ---
async def ws_authenticate(websocket: WebSocket, operator_name: str):
    """Token'ı doğrular; hata olursa bağlantıyı açıklayıcı bir kodla kapatır ve None döner.
    4401 = oturum geçersiz, 4403 = yetki yok (istemci bu kodlarda yeniden bağlanmayı dener DEĞİL)."""
    try:
        username = await auth_username(websocket.query_params.get("token", ""))
    except AuthError as e:
        await websocket.accept()
        await websocket.close(code=4401 if e.status in (401, 409, 400) else 1013, reason=e.detail[:100])
        return None
    if username != operator_name:
        await websocket.accept()
        await websocket.close(code=4403, reason="Kimlik uyuşmazlığı")
        return None
    return username

@app.websocket("/ws/{server_name}/{operator_name}")
async def websocket_endpoint(websocket: WebSocket, server_name: str, operator_name: str):
    if await ws_authenticate(websocket, operator_name) is None:
        return
    db = get_db()
    allowed = can_access_room(db, server_name, operator_name)
    db.close()
    if not allowed:
        await websocket.accept()
        await websocket.close(code=4403, reason="Bu karargaha erişim yetkiniz yok")
        return
    upload_base = base_url_from(websocket.headers, "https" if websocket.url.scheme == "wss" else "http")
    await manager.connect(websocket, server_name, operator_name)
    if operator_name not in operator_statuses:
        operator_statuses[operator_name] = "ÇEVRİMİÇİ"
    
    time_now = datetime.now().strftime("%H:%M")
    await manager.broadcast(json.dumps({"type": "system", "text": f"{operator_name.upper()} AĞA BAĞLANDI.", "time": time_now}), server_name)
    await manager.broadcast_online_users(server_name)
    # Yeni gelen, odada kimin hangi ses kanalında olduğunu hemen görsün
    for op, ch in list(manager.voice_members.get(server_name, {}).items()):
        if op != operator_name:
            await manager.safe_send(websocket, json.dumps({"type": "voice_join", "sender": op, "voice_channel": ch, "announce": True}))

    try:
        while True:
            raw_data = await websocket.receive_text()
            try:
                data = json.loads(raw_data)
            except Exception:
                continue  # bozuk mesaj bağlantıyı düşürmesin
            if not isinstance(data, dict):
                continue
            msg_type = data.get("type", "chat")

            if msg_type == "ping":
                await manager.safe_send(websocket, json.dumps({"type": "pong"}))
                continue

            if msg_type in RELAY_TYPES:
                # Kimlik sahteciliğini engelle: gönderen her zaman bu socket'in sahibi
                data["sender"] = operator_name
                if msg_type == "voice_join":
                    manager.voice_members.setdefault(server_name, {})[operator_name] = data.get("voice_channel", "")
                elif msg_type == "voice_leave":
                    manager.voice_members.get(server_name, {}).pop(operator_name, None)
                out = json.dumps(data)
                target = data.get("target")
                if target:
                    # offer/answer/ice/voice_ack sadece hedefe gitsin (herkese yayma)
                    await manager.send_to(out, server_name, target)
                else:
                    await manager.broadcast(out, server_name, exclude=websocket)

            elif msg_type == "status_update":
                operator_statuses[operator_name] = data.get("status", "ÇEVRİMİÇİ")
                await manager.broadcast_online_users(server_name)

            elif msg_type in ["chat", "image"]:
                sender = operator_name
                text = str(data.get("text", ""))[:4000]
                channel_name = str(data.get("channel_name", "operasyon-merkezi"))[:100]
                if not text.strip() or (msg_type == "image" and not is_own_upload(text, upload_base)):
                    continue
                time_now = datetime.now().strftime("%H:%M")
                
                conn = sqlite3.connect("karargah.db")
                conn.execute("INSERT INTO messages (server_name, channel_name, sender, text, time_str, msg_type) VALUES (?, ?, ?, ?, ?, ?)", (server_name, channel_name, sender, text, time_now, msg_type))
                conn.commit()
                conn.close()
                await manager.broadcast(json.dumps({"type": msg_type, "name": sender, "text": text, "time": time_now, "channel_name": channel_name}), server_name)
                
    except WebSocketDisconnect:
        pass
    except Exception:
        # Önceden sadece WebSocketDisconnect yakalanıyordu; başka bir hata olursa
        # kullanıcı listede "hayalet" olarak kalıyordu.
        pass
    finally:
        if manager.disconnect(websocket, server_name, operator_name):
            # Ses kanalındaysa diğerleri eş bağlantısını hemen temizlesin
            if manager.voice_members.get(server_name, {}).pop(operator_name, None) is not None:
                await manager.broadcast(json.dumps({"type": "voice_leave", "sender": operator_name}), server_name)
            if not manager.voice_members.get(server_name):
                manager.voice_members.pop(server_name, None)
            still_here = any(operator_name in room for room in manager.active_connections.values())
            if not still_here and operator_name not in manager.user_connections:
                operator_statuses.pop(operator_name, None)
            time_now = datetime.now().strftime("%H:%M")
            await manager.broadcast(json.dumps({"type": "system", "text": f"{operator_name.upper()} BAĞLANTIYI KESTİ.", "time": time_now}), server_name)
            await manager.broadcast_online_users(server_name)

# --- TURN SUNUCUSU (NAT/CGNAT arkasındaki kullanıcılar için ses rölesi) ---
# Railway ortam değişkenleriyle ayarlanır:
#   Cloudflare Realtime TURN:  CF_TURN_KEY_ID, CF_TURN_API_TOKEN
#   veya herhangi bir TURN:     TURN_URLS (virgülle ayrılmış), TURN_USERNAME, TURN_CREDENTIAL
DEFAULT_ICE = [{"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.l.google.com:19302"]}]

def _fetch_cloudflare_ice():
    import urllib.request
    key_id = os.environ["CF_TURN_KEY_ID"]
    token = os.environ["CF_TURN_API_TOKEN"]
    req = urllib.request.Request(
        f"https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers",
        data=json.dumps({"ttl": 86400}).encode(), method="POST",
        # User-Agent şart: Cloudflare varsayılan "Python-urllib" kimliğini bot sayıp 403 (1010) veriyor
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "karargah-backend/1.0"})
    with urllib.request.urlopen(req, timeout=5) as r:
        body = json.loads(r.read())
    servers = body.get("iceServers", [])
    return servers if isinstance(servers, list) else [servers]

@app.get("/api/ice-servers")
async def ice_servers(me: str = Depends(current_user)):
    # Oturum şart: TURN kotası (ve faturası) bize ait, dışarıdan kullanılmasın
    try:
        if os.environ.get("CF_TURN_KEY_ID") and os.environ.get("CF_TURN_API_TOKEN"):
            return {"iceServers": await asyncio.to_thread(_fetch_cloudflare_ice)}
        if os.environ.get("TURN_URLS"):
            turn = {"urls": [u.strip() for u in os.environ["TURN_URLS"].split(",") if u.strip()],
                    "username": os.environ.get("TURN_USERNAME", ""),
                    "credential": os.environ.get("TURN_CREDENTIAL", "")}
            return {"iceServers": DEFAULT_ICE + [turn]}
    except Exception as e:
        print("TURN kimlik bilgisi alınamadı:", e)
    return {"iceServers": DEFAULT_ICE}

@app.websocket("/ws/user/{operator_name}")
async def websocket_user_endpoint(websocket: WebSocket, operator_name: str):
    if await ws_authenticate(websocket, operator_name) is None:
        return
    await websocket.accept()
    old = manager.user_connections.get(operator_name)
    manager.user_connections[operator_name] = websocket
    if old is not None and old is not websocket:
        try: await old.close(code=4000)
        except Exception: pass
    
    # Yeni bağlanan operatörün durumunu bildir
    my_status = operator_statuses.get(operator_name, "ÇEVRİMİÇİ")
    for op, conn in list(manager.user_connections.items()):
        if op != operator_name:
            try: await conn.send_text(json.dumps({"type": "friend_status", "operator": operator_name, "is_online": my_status != "GİZLİ HAREKAT", "status": my_status}))
            except: pass
            
    try:
        while True:
            raw_data = await websocket.receive_text()
            try:
                data = json.loads(raw_data)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            msg_type = data.get("type")
            if msg_type == "ping":
                await manager.safe_send(websocket, json.dumps({"type": "pong"}))
            elif msg_type == "call_user":
                target = data.get("target")
                if target in manager.user_connections:
                    await manager.user_connections[target].send_text(json.dumps({"type": "incoming_call", "caller": operator_name, "dm_room": data.get("dm_room")}))
            elif msg_type == "call_response":
                target = data.get("target")
                if target in manager.user_connections:
                    await manager.user_connections[target].send_text(json.dumps({"type": "call_response", "responder": operator_name, "action": data.get("action")}))
            elif msg_type == "status_update":
                st = data.get("status", "ÇEVRİMİÇİ")
                operator_statuses[operator_name] = st
                for op, conn in list(manager.user_connections.items()):
                    if op != operator_name:
                        try: await conn.send_text(json.dumps({"type": "friend_status", "operator": operator_name, "is_online": st != "GİZLİ HAREKAT", "status": st}))
                        except: pass
    except Exception:
        pass
    finally:
        # Sadece hâlâ kayıtlı socket bizsek sil (yeni sekme eskisinin yerini aldıysa dokunma)
        if manager.user_connections.get(operator_name) is websocket:
            del manager.user_connections[operator_name]
            for op, conn in list(manager.user_connections.items()):
                await manager.safe_send(conn, json.dumps({"type": "friend_status", "operator": operator_name, "is_online": False, "status": "ÇEVRİMDIŞI"}))


# --- REST API UÇ NOKTALARI ---
def get_db():
    conn = sqlite3.connect("karargah.db")
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/api/username-available/{username}")
async def username_available(username: str):
    # Kayıt ekranı, Supabase hesabı açmadan önce kod adının boş olup olmadığını sorar
    name = username.strip().upper()
    if not USERNAME_RE.match(name):
        return {"available": False, "reason": "Geçersiz kod adı (2-24 karakter; boşluk, _ / ? # % kullanılamaz)."}
    db = get_db()
    row = db.execute("SELECT auth_id FROM users WHERE username = ?", (name,)).fetchone()
    db.close()
    taken = bool(row and row["auth_id"])
    return {"available": not taken, "reason": "Bu kod adı alınmış." if taken else ""}

@app.post("/api/register")
async def register(request: Request, data: dict = None):
    # Giriş sonrası çağrılır: kod adını bu Supabase hesabına kilitler ve kesin kod adını döner
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    try:
        username = await auth_username(token, (data or {}).get("username", ""))
    except AuthError as e:
        raise HTTPException(e.status, e.detail)
    return {"status": "success", "username": username}

@app.get("/api/get-profile/{username}")
async def get_profile(username: str, me: str = Depends(current_user)):
    db = get_db()
    user = db.execute("SELECT avatar_url FROM users WHERE username = ?", (username,)).fetchone()
    db.close()
    return {"status": "success", "avatar_url": (user["avatar_url"] or "") if user else ""}

@app.post("/api/update-profile")
async def update_profile(request: Request, data: dict, me: str = Depends(current_user)):
    avatar = str(data.get("avatar_url", ""))
    if not is_own_upload(avatar, public_base_url(request)):
        raise HTTPException(400, "Geçersiz görsel adresi.")
    db = get_db()
    db.execute("UPDATE users SET avatar_url = ? WHERE username = ?", (avatar, me))
    db.commit()
    db.close()
    return {"status": "success"}

@app.get("/api/check-prime/{username}")
async def check_prime(username: str, me: str = Depends(current_user)):
    # Yol parametresi geriye uyumluluk için duruyor; her zaman oturum sahibinin durumu döner
    db = get_db()
    user = db.execute("SELECT is_prime FROM users WHERE username = ?", (me,)).fetchone()
    db.close()
    return {"is_prime": user["is_prime"] if user else 0}

@app.get("/api/servers/{username}")
async def get_servers(username: str, me: str = Depends(current_user)):
    db = get_db()
    servers = db.execute("SELECT s.name, s.owner, s.icon_url FROM servers s JOIN server_members sm ON s.name = sm.server_name WHERE sm.username = ?", (me,)).fetchall()
    db.close()
    return {"servers": [dict(s) for s in servers]}

@app.post("/api/servers")
async def create_server(data: dict, me: str = Depends(current_user)):
    name = str(data.get("name", "")).strip()
    if not (2 <= len(name) <= 40) or name.startswith("DM_") or name == "@HOME" or "/" in name:
        raise HTTPException(400, "Geçersiz karargah adı.")
    db = get_db()
    try:
        db.execute("INSERT INTO servers (name, owner, icon_url, invite_code) VALUES (?, ?, '', ?)", (name, me, ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))))
        db.execute("INSERT INTO server_members (server_name, username, role) VALUES (?, ?, 'KURUCU')", (name, me))
        db.execute("INSERT INTO channels (server_name, name, type) VALUES (?, 'operasyon-merkezi', 'text')", (name,))
        db.execute("INSERT INTO channels (server_name, name, type) VALUES (?, 'GENEL FREKANS', 'voice')", (name,))
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        raise HTTPException(400, "Bu isimde bir karargah zaten var.")
    db.close()
    return {"status": "success"}

@app.get("/api/channels/{server_name}")
async def get_channels(server_name: str, me: str = Depends(current_user)):
    db = get_db()
    require_room_access(db, server_name, me)
    channels = db.execute("SELECT name, type FROM channels WHERE server_name = ?", (server_name,)).fetchall()
    db.close()
    return {"channels": [dict(c) for c in channels]}

@app.post("/api/channels")
async def create_channel(data: dict, me: str = Depends(current_user)):
    server_name = str(data.get("server_name", ""))
    channel_name = str(data.get("channel_name", "")).strip()
    channel_type = data.get("channel_type")
    if channel_type not in ("text", "voice") or not (1 <= len(channel_name) <= 40):
        raise HTTPException(400, "Geçersiz kanal.")
    db = get_db()
    require_manager(db, server_name, me)
    if db.execute("SELECT 1 FROM channels WHERE server_name = ? AND name = ? AND type = ?", (server_name, channel_name, channel_type)).fetchone():
        db.close()
        raise HTTPException(400, "Bu isimde bir kanal zaten var.")
    db.execute("INSERT INTO channels (server_name, name, type) VALUES (?, ?, ?)", (server_name, channel_name, channel_type))
    db.commit()
    db.close()
    return {"status": "success"}

@app.delete("/api/channels")
async def delete_channel(data: dict, me: str = Depends(current_user)):
    server_name = str(data.get("server_name", ""))
    db = get_db()
    require_manager(db, server_name, me)
    db.execute("DELETE FROM channels WHERE server_name = ? AND name = ? AND type = ?", (server_name, data.get("channel_name"), data.get("channel_type")))
    db.commit()
    db.close()
    return {"status": "success"}

@app.get("/api/messages/{server_name}/{channel_name}")
async def get_messages(server_name: str, channel_name: str, me: str = Depends(current_user)):
    db = get_db()
    require_room_access(db, server_name, me)
    # Son 200 mesaj (eskiden kanalın TÜM geçmişi tek seferde gönderiliyordu)
    msgs = db.execute("SELECT sender as name, text, time_str as time, msg_type as type FROM messages WHERE server_name = ? AND channel_name = ? ORDER BY id DESC LIMIT 200", (server_name, channel_name)).fetchall()
    db.close()
    return {"messages": [dict(m) for m in reversed(msgs)]}

@app.get("/api/server/roles/{server_name}")
async def get_roles(server_name: str, me: str = Depends(current_user)):
    db = get_db()
    require_room_access(db, server_name, me)
    roles = db.execute("SELECT username, role FROM server_members WHERE server_name = ?", (server_name,)).fetchall()
    db.close()
    return {"roles": {r["username"]: r["role"] for r in roles}}

@app.post("/api/server/role")
async def assign_role(data: dict, me: str = Depends(current_user)):
    server_name = str(data.get("server_name", ""))
    role = data.get("role")
    target = str(data.get("username", ""))
    if role not in ("MODERATÖR", "OPERATÖR"):
        raise HTTPException(400, "Geçersiz rol.")
    db = get_db()
    # Rolleri sadece kurucu dağıtır; kurucunun kendi rolü değiştirilemez
    if member_role(db, server_name, me) != "KURUCU" or target == me:
        db.close()
        raise HTTPException(403, "Rolleri sadece KURUCU değiştirebilir.")
    db.execute("UPDATE server_members SET role = ? WHERE server_name = ? AND username = ?", (role, server_name, target))
    db.commit()
    db.close()
    return {"status": "success"}

@app.post("/api/update-server-icon")
async def update_server_icon(request: Request, data: dict, me: str = Depends(current_user)):
    server_name = str(data.get("server_name", ""))
    icon = str(data.get("icon_url", ""))
    if not is_own_upload(icon, public_base_url(request)):
        raise HTTPException(400, "Geçersiz görsel adresi.")
    db = get_db()
    require_manager(db, server_name, me)
    db.execute("UPDATE servers SET icon_url = ? WHERE name = ?", (icon, server_name))
    db.commit()
    db.close()
    return {"status": "success"}

@app.post("/api/join-server")
async def join_server(data: dict, me: str = Depends(current_user)):
    db = get_db()
    server = db.execute("SELECT name FROM servers WHERE invite_code = ?", (str(data.get("invite_code", "")).strip().upper(),)).fetchone()
    if not server:
        db.close()
        raise HTTPException(400, "Geçersiz davet kodu.")
    if member_role(db, server["name"], me) is None:
        db.execute("INSERT INTO server_members (server_name, username, role) VALUES (?, ?, 'OPERATÖR')", (server["name"], me))
        db.commit()
    db.close()
    return {"server_name": server["name"]}

@app.get("/api/servers/{server_name}/generate-invite")
async def gen_invite(server_name: str, me: str = Depends(current_user)):
    db = get_db()
    require_manager(db, server_name, me)
    invite = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    db.execute("UPDATE servers SET invite_code = ? WHERE name = ?", (invite, server_name))
    db.commit()
    db.close()
    return {"invite_code": invite}

def public_base_url(request: Request) -> str:
    # PUBLIC_URL ortam değişkeni varsa onu kullan; yoksa isteğin geldiği adresten türet
    # (Cloudflare Tunnel / Railway gibi proxy'ler X-Forwarded-Proto ve Host başlıklarını geçirir)
    return base_url_from(request.headers, request.url.scheme)

ALLOWED_UPLOAD_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

@app.post("/api/upload")
async def upload_file(request: Request, file: UploadFile = File(...), me: str = Depends(current_user)):
    # Sadece resim uzantıları: .html/.svg gibi dosyalar domain üzerinden sahte sayfa (phishing/XSS) barındırmaya yarar
    ext = (file.filename or "").rsplit('.', 1)[-1].lower()
    if ext not in ALLOWED_UPLOAD_EXT:
        raise HTTPException(400, "Sadece jpg, png, webp veya gif yüklenebilir.")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "Dosya çok büyük (en fazla 8 MB).")
    try:
        # Resmi benzersiz bir isimle kaydet (çakışma olmasın diye)
        new_filename = f"{uuid.uuid4().hex}.{ext}"
        file_location = f"uploads/{new_filename}"

        # Dosyayı sunucuya fiziksel olarak yaz
        with open(file_location, "wb") as file_object:
            file_object.write(data)

        # Flutter'ın okuyabileceği gerçek canlı linki oluştur
        file_url = f"{public_base_url(request)}/uploads/{new_filename}"
        return {"url": file_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Görsel yüklenemedi!")

@app.get("/api/friends/{username}")
async def get_friends(username: str, me: str = Depends(current_user)):
    db = get_db()
    user = db.execute("SELECT friend_code FROM users WHERE username = ?", (me,)).fetchone()
    my_code = user["friend_code"] if user and user["friend_code"] else ''.join(random.choices(string.digits, k=8))
    if not (user and user["friend_code"]):
        db.execute("UPDATE users SET friend_code = ? WHERE username = ?", (my_code, me))
        db.commit()

    # Arkadaşlık isteklerini ve listesini çek
    reqs = db.execute("SELECT user1 FROM friends WHERE user2 = ? AND status = 'pending'", (me,)).fetchall()
    friends = db.execute("SELECT u.username, u.avatar_url FROM friends f JOIN users u ON (f.user1 = u.username OR f.user2 = u.username) WHERE (f.user1 = ? OR f.user2 = ?) AND f.status = 'accepted' AND u.username != ?", (me, me, me)).fetchall()
    db.close()

    friends_data = []
    for f in friends:
        item = dict(f)
        u_name = item["username"]
        is_conn = u_name in manager.user_connections
        st = operator_statuses.get(u_name, "ÇEVRİMİÇİ") if is_conn else "ÇEVRİMDIŞI"
        if st == "GİZLİ HAREKAT":
            is_conn = False
            st = "ÇEVRİMDIŞI"
        item["is_online"] = is_conn
        item["status"] = st
        friends_data.append(item)

    return {"my_friend_code": my_code, "incoming_requests": [r["user1"] for r in reqs], "friends": friends_data}

@app.post("/api/friends/request")
async def add_friend(data: dict, me: str = Depends(current_user)):
    db = get_db()
    q = str(data.get("target", "")).strip()
    target = db.execute("SELECT username FROM users WHERE username = ? OR friend_code = ?", (q.upper(), q)).fetchone()
    if not target:
        db.close()
        raise HTTPException(400, "Operatör bulunamadı!")
    t = target["username"]
    if t == me:
        db.close()
        raise HTTPException(400, "Kendinizi ekleyemezsiniz.")
    if db.execute("SELECT 1 FROM friends WHERE (user1 = ? AND user2 = ?) OR (user1 = ? AND user2 = ?)", (me, t, t, me)).fetchone():
        db.close()
        raise HTTPException(400, "Zaten bağlantılısınız ya da bekleyen bir istek var.")
    db.execute("INSERT INTO friends (user1, user2, status) VALUES (?, ?, 'pending')", (me, t))
    db.commit()
    db.close()
    return {"message": "İstek gönderildi."}

@app.post("/api/friends/respond")
async def res_friend(data: dict, me: str = Depends(current_user)):
    sender = str(data.get("sender", ""))
    db = get_db()
    # Sadece KENDİNE gelen isteği kabul/red edebilirsin
    if data.get("action") == "accept": db.execute("UPDATE friends SET status = 'accepted' WHERE user1 = ? AND user2 = ? AND status = 'pending'", (sender, me))
    else: db.execute("DELETE FROM friends WHERE user1 = ? AND user2 = ? AND status = 'pending'", (sender, me))
    db.commit()
    db.close()
    return {"status": "success"}

@app.post("/api/friends/remove")
async def remove_friend(data: dict, me: str = Depends(current_user)):
    other = str(data.get("user2", ""))
    db = get_db()
    db.execute("DELETE FROM friends WHERE (user1 = ? AND user2 = ?) OR (user1 = ? AND user2 = ?)", (me, other, other, me))
    db.commit()
    db.close()
    return {"status": "success", "message": "Operatör bağlantısı kesildi."}

# --- GİZLİ KOMUT: MANUEL PRIME AKTİVASYONU (ZEKİ SÜRÜM) ---
def _require_admin(key: str):
    # Eskiden bu uçlar herkese açıktı: linki bilen herkes kendine Prime verebiliyor,
    # tüm kullanıcıları ve mesajları okuyabiliyordu. Railway'de ADMIN_KEY tanımla,
    # sonra ?key=... ile çağır. ADMIN_KEY yoksa uç tamamen kapalı.
    admin_key = os.environ.get("ADMIN_KEY")
    if not admin_key or key != admin_key:
        raise HTTPException(404, "Not Found")

@app.get("/api/secret-prime/{username}")
async def secret_give_prime(username: str, key: str = ""):
    _require_admin(key)
    try:
        conn = sqlite3.connect('karargah.db')
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [t[0] for t in cursor.fetchall()]
        table_name = next((t for t in ['users', 'user', 'operators'] if t in tables), None)
        if not table_name: return {"error": f"Tablo bulunamadı! Tablolar: {tables}"}
        
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = [c[1] for c in cursor.fetchall()]
        if "is_prime" not in columns: cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN is_prime INTEGER DEFAULT 0")
        
        cursor.execute(f"UPDATE {table_name} SET is_prime = 1 WHERE username = ?", (username,))
        conn.commit()
        conn.close()
        return {"status": "success", "message": f"Tebrikler! {username} artık PRIME statüsünde."}
    except Exception as e:
        return {"error": str(e)}

# --- İSTİHBARAT PANELİ (KULLANICI VE LOG İZLEME) ---
@app.get("/api/radar/istihbarat")
async def radar_istihbarat(key: str = ""):
    _require_admin(key)
    try:
        conn = sqlite3.connect('karargah.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        data = {"kullanicilar": [], "son_mesajlar": []}
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [t[0] for t in cursor.fetchall()]
        
        if 'users' in tables:
            data["kullanicilar"] = [dict(row) for row in cursor.execute("SELECT * FROM users LIMIT 50").fetchall()]
        if 'messages' in tables:
            data["son_mesajlar"] = [dict(row) for row in cursor.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 50").fetchall()]
            
        conn.close()
        return data
    except Exception as e:
        return {"error": str(e)}