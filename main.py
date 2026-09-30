# main.py (RAILWAY BACKEND - TAM SÜRÜM V3.0)
import json
import sqlite3
import random
import string
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from typing import Dict, List
import os
import shutil
import uuid
import asyncio

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
    conn.commit()
    conn.close()

init_db()

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
@app.websocket("/ws/{server_name}/{operator_name}")
async def websocket_endpoint(websocket: WebSocket, server_name: str, operator_name: str):
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
                channel_name = data.get("channel_name", "operasyon-merkezi")
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
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        body = json.loads(r.read())
    servers = body.get("iceServers", [])
    return servers if isinstance(servers, list) else [servers]

@app.get("/api/ice-servers")
async def ice_servers():
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
    await websocket.accept()
    manager.user_connections[operator_name] = websocket
    
    # Yeni bağlanan operatörün durumunu bildir
    my_status = operator_statuses.get(operator_name, "ÇEVRİMİÇİ")
    for op, conn in list(manager.user_connections.items()):
        if op != operator_name:
            try: await conn.send_text(json.dumps({"type": "friend_status", "operator": operator_name, "is_online": my_status != "GİZLİ HAREKAT", "status": my_status}))
            except: pass
            
    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            msg_type = data.get("type")
            if msg_type == "call_user":
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
    except WebSocketDisconnect:
        if operator_name in manager.user_connections: del manager.user_connections[operator_name]
        for op, conn in list(manager.user_connections.items()):
            try: await conn.send_text(json.dumps({"type": "friend_status", "operator": operator_name, "is_online": False, "status": "ÇEVRİMDIŞI"}))
            except: pass


# --- REST API UÇ NOKTALARI ---
def get_db():
    conn = sqlite3.connect("karargah.db")
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/api/get-profile/{username}")
async def get_profile(username: str):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    db.close()
    if user: return {"status": "success", "avatar_url": user["avatar_url"]}
    return {"status": "success", "avatar_url": ""}

@app.post("/api/update-profile")
async def update_profile(data: dict):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO users (username, avatar_url, is_prime, friend_code) VALUES (?, ?, COALESCE((SELECT is_prime FROM users WHERE username=?), 0), COALESCE((SELECT friend_code FROM users WHERE username=?), ?))", 
               (data["username"], data.get("avatar_url", ""), data["username"], data["username"], ''.join(random.choices(string.digits, k=8))))
    db.commit()
    db.close()
    return {"status": "success"}

@app.get("/api/check-prime/{username}")
async def check_prime(username: str):
    db = get_db()
    user = db.execute("SELECT is_prime FROM users WHERE username = ?", (username,)).fetchone()
    db.close()
    return {"is_prime": user["is_prime"] if user else 0}

@app.get("/api/servers/{username}")
async def get_servers(username: str):
    db = get_db()
    servers = db.execute("SELECT s.name, s.owner, s.icon_url FROM servers s JOIN server_members sm ON s.name = sm.server_name WHERE sm.username = ?", (username,)).fetchall()
    db.close()
    return {"servers": [dict(s) for s in servers]}

@app.post("/api/servers")
async def create_server(data: dict):
    db = get_db()
    try:
        db.execute("INSERT INTO servers (name, owner, icon_url, invite_code) VALUES (?, ?, '', ?)", (data["name"], data["owner"], ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))))
        db.execute("INSERT INTO server_members (server_name, username, role) VALUES (?, ?, 'KURUCU')", (data["name"], data["owner"]))
        db.execute("INSERT INTO channels (server_name, name, type) VALUES (?, 'operasyon-merkezi', 'text')", (data["name"],))
        db.execute("INSERT INTO channels (server_name, name, type) VALUES (?, 'GENEL FREKANS', 'voice')", (data["name"],))
        db.commit()
    except:
        db.close()
        raise HTTPException(400, "Sunucu zaten var.")
    db.close()
    return {"status": "success"}

@app.get("/api/channels/{server_name}")
async def get_channels(server_name: str):
    db = get_db()
    channels = db.execute("SELECT name, type FROM channels WHERE server_name = ?", (server_name,)).fetchall()
    db.close()
    return {"channels": [dict(c) for c in channels]}

@app.post("/api/channels")
async def create_channel(data: dict):
    db = get_db()
    db.execute("INSERT INTO channels (server_name, name, type) VALUES (?, ?, ?)", (data["server_name"], data["channel_name"], data["channel_type"]))
    db.commit()
    db.close()
    return {"status": "success"}

@app.delete("/api/channels")
async def delete_channel(data: dict):
    db = get_db()
    db.execute("DELETE FROM channels WHERE server_name = ? AND name = ? AND type = ?", (data["server_name"], data["channel_name"], data["channel_type"]))
    db.commit()
    db.close()
    return {"status": "success"}

@app.get("/api/messages/{server_name}/{channel_name}")
async def get_messages(server_name: str, channel_name: str):
    db = get_db()
    msgs = db.execute("SELECT sender as name, text, time_str as time, msg_type as type FROM messages WHERE server_name = ? AND channel_name = ? ORDER BY id ASC", (server_name, channel_name)).fetchall()
    db.close()
    return {"messages": [dict(m) for m in msgs]}

@app.get("/api/server/roles/{server_name}")
async def get_roles(server_name: str):
    db = get_db()
    roles = db.execute("SELECT username, role FROM server_members WHERE server_name = ?", (server_name,)).fetchall()
    db.close()
    return {"roles": {r["username"]: r["role"] for r in roles}}

@app.post("/api/server/role")
async def assign_role(data: dict):
    db = get_db()
    db.execute("UPDATE server_members SET role = ? WHERE server_name = ? AND username = ?", (data["role"], data["server_name"], data["username"]))
    db.commit()
    db.close()
    return {"status": "success"}

@app.post("/api/update-server-icon")
async def update_server_icon(data: dict):
    # İstemci bu ucu çağırıyordu ama backend'de yoktu (logo güncelleme hiç çalışmıyordu)
    db = get_db()
    db.execute("UPDATE servers SET icon_url = ? WHERE name = ?", (data.get("icon_url", ""), data.get("server_name", "")))
    db.commit()
    db.close()
    return {"status": "success"}

@app.post("/api/join-server")
async def join_server(data: dict):
    db = get_db()
    server = db.execute("SELECT name FROM servers WHERE invite_code = ?", (data["invite_code"],)).fetchone()
    if not server:
        db.close()
        raise HTTPException(400, "Geçersiz davet kodu.")
    db.execute("INSERT OR IGNORE INTO server_members (server_name, username, role) VALUES (?, ?, 'OPERATÖR')", (server["name"], data["username"]))
    db.commit()
    db.close()
    return {"server_name": server["name"]}

@app.get("/api/servers/{server_name}/generate-invite")
async def gen_invite(server_name: str):
    db = get_db()
    invite = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    db.execute("UPDATE servers SET invite_code = ? WHERE name = ?", (invite, server_name))
    db.commit()
    db.close()
    return {"invite_code": invite}

def public_base_url(request: Request) -> str:
    # PUBLIC_URL ortam değişkeni varsa onu kullan; yoksa isteğin geldiği adresten türet
    # (Cloudflare Tunnel / Railway gibi proxy'ler X-Forwarded-Proto ve Host başlıklarını geçirir)
    env = os.environ.get("PUBLIC_URL")
    if env:
        return env.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}"

@app.post("/api/register")
async def register(data: dict):
    # Giriş ekranı kayıt sonrası bu ucu çağırıyordu ama backend'de yoktu (404 sessizce yutuluyordu)
    username = (data.get("username") or "").strip()
    if not username:
        raise HTTPException(400, "Kullanıcı adı gerekli.")
    db = get_db()
    db.execute("INSERT OR IGNORE INTO users (username, avatar_url, is_prime, friend_code) VALUES (?, '', 0, ?)",
               (username, ''.join(random.choices(string.digits, k=8))))
    db.commit()
    db.close()
    return {"status": "success"}

@app.post("/api/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    try:
        # Resmi benzersiz bir isimle kaydet (çakışma olmasın diye)
        ext = file.filename.split('.')[-1]
        new_filename = f"{uuid.uuid4().hex}.{ext}"
        file_location = f"uploads/{new_filename}"
        
        # Dosyayı sunucuya fiziksel olarak yaz
        with open(file_location, "wb+") as file_object:
            shutil.copyfileobj(file.file, file_object)
            
        # Flutter'ın okuyabileceği gerçek canlı linki oluştur
        file_url = f"{public_base_url(request)}/uploads/{new_filename}"
        return {"url": file_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Görsel yüklenemedi!")

@app.get("/api/friends/{username}")
async def get_friends(username: str):
    import random, string
    db = get_db()
    
    # 1. Veritabanında kullanıcıyı kontrol et
    user = db.execute("SELECT friend_code FROM users WHERE username = ?", (username,)).fetchone()
    
    # 2. Eğer kullanıcının Taktiksel ID'si zaten varsa onu kullan
    if user and user["friend_code"]:
        my_code = user["friend_code"]
    else:
        # YOKSA: Rastgele 8 haneli yepyeni bir Taktiksel ID üret
        my_code = ''.join(random.choices(string.digits, k=8))
        
        # Kullanıcı hiç kayıtlı değilse sisteme kaydet
        db.execute("INSERT OR IGNORE INTO users (username, avatar_url, is_prime, friend_code) VALUES (?, '', 0, ?)", (username, my_code))
        
        # Kullanıcı kayıtlı ama ID'si boş kalmışsa, ID'sini güncelle
        db.execute("UPDATE users SET friend_code = ? WHERE username = ?", (my_code, username))
        db.commit()
        
    # Arkadaşlık isteklerini ve listesini çek
    reqs = db.execute("SELECT user1 FROM friends WHERE user2 = ? AND status = 'pending'", (username,)).fetchall()
    friends = db.execute("SELECT u.username, u.avatar_url FROM friends f JOIN users u ON (f.user1 = u.username OR f.user2 = u.username) WHERE (f.user1 = ? OR f.user2 = ?) AND f.status = 'accepted' AND u.username != ?", (username, username, username)).fetchall()
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
async def add_friend(data: dict):
    db = get_db()
    target = db.execute("SELECT username FROM users WHERE username = ? OR friend_code = ?", (data["target"], data["target"])).fetchone()
    if not target: raise HTTPException(400, "Operatör bulunamadı!")
    db.execute("INSERT INTO friends (user1, user2, status) VALUES (?, ?, 'pending')", (data["sender"], target["username"]))
    db.commit()
    db.close()
    return {"message": "İstek gönderildi."}

@app.post("/api/friends/respond")
async def res_friend(data: dict):
    db = get_db()
    if data["action"] == "accept": db.execute("UPDATE friends SET status = 'accepted' WHERE user1 = ? AND user2 = ?", (data["sender"], data["receiver"]))
    else: db.execute("DELETE FROM friends WHERE user1 = ? AND user2 = ?", (data["sender"], data["receiver"]))
    db.commit()
    db.close()
    return {"status": "success"}

@app.post("/api/friends/remove")
async def remove_friend(data: dict):
    db = get_db()
    u1 = data.get("user1")
    u2 = data.get("user2")
    if u1 and u2:
        db.execute("DELETE FROM friends WHERE (user1 = ? AND user2 = ?) OR (user1 = ? AND user2 = ?)", (u1, u2, u2, u1))
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