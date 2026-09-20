import json
import sqlite3
import os
import shutil
import time
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Dict, Optional
import random
import string

app = FastAPI(title="Karargah Backend v1.9 - Direct Call Ringing Engine")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

def generate_unique_friend_code(cursor):
    while True:
        code = str(random.randint(10000000, 99999999))
        cursor.execute("SELECT id FROM operators WHERE friend_code = ?", (code,))
        if not cursor.fetchone():
            return code

def init_db():
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("""CREATE TABLE IF NOT EXISTS operators (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, is_prime INTEGER DEFAULT 0, avatar_url TEXT DEFAULT '', friend_code TEXT DEFAULT '')""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS servers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, owner TEXT NOT NULL, icon_url TEXT DEFAULT '', invite_code TEXT DEFAULT '')""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, server_name TEXT NOT NULL, channel_name TEXT NOT NULL, sender TEXT NOT NULL, text TEXT NOT NULL, time_str TEXT NOT NULL, msg_type TEXT DEFAULT 'chat')""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS server_roles (id INTEGER PRIMARY KEY AUTOINCREMENT, server_name TEXT NOT NULL, username TEXT NOT NULL, role TEXT NOT NULL, UNIQUE(server_name, username))""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY AUTOINCREMENT, server_name TEXT NOT NULL, channel_name TEXT NOT NULL, channel_type TEXT NOT NULL, UNIQUE(server_name, channel_name, channel_type))""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS friends (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, receiver TEXT NOT NULL, status TEXT DEFAULT 'pending', UNIQUE(sender, receiver))""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS direct_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, receiver TEXT NOT NULL, text TEXT NOT NULL, time_str TEXT NOT NULL, msg_type TEXT DEFAULT 'chat')""")

    try: cursor.execute("ALTER TABLE operators ADD COLUMN avatar_url TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE operators ADD COLUMN friend_code TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE servers ADD COLUMN icon_url TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE servers ADD COLUMN invite_code TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE messages ADD COLUMN msg_type TEXT DEFAULT 'chat'")
    except: pass

    cursor.execute("SELECT id, username, friend_code FROM operators")
    users = cursor.fetchall()
    for u in users:
        if not u[2] or len(str(u[2])) < 5:
            new_code = generate_unique_friend_code(cursor)
            cursor.execute("UPDATE operators SET friend_code = ? WHERE id = ?", (new_code, u[0]))

    cursor.execute("SELECT COUNT(*) FROM servers")
    if cursor.fetchone()[0] == 0: cursor.execute("INSERT INTO servers (name, owner) VALUES ('KUZEY KARTALLARI', 'AKIN')")
    try: cursor.execute("UPDATE servers SET owner = 'AKIN' WHERE name = 'KUZEY KARTALLARI' AND owner = 'SİSTEM'")
    except: pass

    cursor.execute("SELECT name FROM servers")
    for srv in cursor.fetchall():
        try:
            cursor.execute("INSERT INTO channels (server_name, channel_name, channel_type) VALUES (?, ?, ?)", (srv[0], "operasyon-merkezi", "text"))
            cursor.execute("INSERT INTO channels (server_name, channel_name, channel_type) VALUES (?, ?, ?)", (srv[0], "istihbarat-raporu", "text"))
            cursor.execute("INSERT INTO channels (server_name, channel_name, channel_type) VALUES (?, ?, ?)", (srv[0], "GİZLİ HAREKAT", "voice"))
        except: pass

    conn.commit()
    conn.close()

init_db()

class PrimeUpdate(BaseModel): username: str
class ServerIconUpdate(BaseModel): server_name: str; icon_url: str
class OperatorAuth(BaseModel): username: str; password: str
class ServerCreate(BaseModel): name: str; owner: str
class RoleUpdate(BaseModel): server_name: str; username: str; role: str
class ProfileUpdate(BaseModel): username: str; avatar_url: str
class JoinServerData(BaseModel): username: str; invite_code: str
class ChannelCreate(BaseModel): server_name: str; channel_name: str; channel_type: str; operator_name: str
class ChannelDelete(BaseModel): server_name: str; channel_name: str; channel_type: str; operator_name: str
class FriendRequestData(BaseModel): sender: str; target: str
class FriendRespondData(BaseModel): sender: str; receiver: str; action: str

# --- ARKADAŞLIK VE STEAM KODU API'LERİ ---

@app.post("/api/friends/request")
def send_friend_request(data: FriendRequestData):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    target_clean = data.target.strip().replace("-", "").replace(" ", "")

    cursor.execute("SELECT username FROM operators WHERE username = ? OR friend_code = ?", (target_clean.upper(), target_clean))
    found_user = cursor.fetchone()
    if not found_user:
        cursor.execute("SELECT username FROM operators WHERE username = ?", (target_clean,))
        found_user = cursor.fetchone()

    if not found_user:
        conn.close()
        raise HTTPException(status_code=404, detail="Operatör veya Taktiksel ID bulunamadı!")
    
    receiver_name = found_user[0]
    if data.sender.upper() == receiver_name.upper():
        conn.close()
        raise HTTPException(status_code=400, detail="Kendinize bağlantı isteği gönderemezsiniz!")

    try:
        cursor.execute("SELECT status FROM friends WHERE sender = ? AND receiver = ?", (receiver_name, data.sender))
        rev = cursor.fetchone()
        if rev:
            cursor.execute("UPDATE friends SET status = 'accepted' WHERE sender = ? AND receiver = ?", (receiver_name, data.sender))
            conn.commit()
            conn.close()
            return {"status": "success", "message": f"{receiver_name} ile bağlantı kuruldu!"}

        cursor.execute("INSERT INTO friends (sender, receiver, status) VALUES (?, ?, 'pending')", (data.sender, receiver_name))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Bu operatörle zaten bağlantınız var veya bekleyen bir istek mevcut!")
    finally: conn.close()
    return {"status": "success", "message": f"{receiver_name} adlı operatöre istek iletildi."}

@app.post("/api/friends/respond")
def respond_friend_request(data: FriendRespondData):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    if data.action == "accept":
        cursor.execute("UPDATE friends SET status = 'accepted' WHERE sender = ? AND receiver = ?", (data.sender, data.receiver))
    elif data.action == "reject":
        cursor.execute("DELETE FROM friends WHERE sender = ? AND receiver = ?", (data.sender, data.receiver))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.get("/api/friends/{username}")
def get_friends(username: str):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("SELECT friend_code FROM operators WHERE username = ?", (username,))
    my_code_row = cursor.fetchone()
    
    if not my_code_row:
        my_code = str(random.randint(10000000, 99999999))
        cursor.execute("INSERT INTO operators (username, password, friend_code) VALUES (?, ?, ?)", (username, "supabase_secured", my_code))
        conn.commit()
    elif not my_code_row[0] or len(str(my_code_row[0])) < 5:
        my_code = str(random.randint(10000000, 99999999))
        cursor.execute("UPDATE operators SET friend_code = ? WHERE username = ?", (my_code, username))
        conn.commit()
    else:
        my_code = my_code_row[0]

    cursor.execute("SELECT sender FROM friends WHERE receiver = ? AND status = 'pending'", (username,))
    incoming_requests = [row[0] for row in cursor.fetchall()]
    
    cursor.execute("""
        SELECT receiver FROM friends WHERE sender = ? AND status = 'accepted'
        UNION
        SELECT sender FROM friends WHERE receiver = ? AND status = 'accepted'
    """, (username, username))
    accepted_friends = [row[0] for row in cursor.fetchall()]
    
    friends_data = []
    for f_name in accepted_friends:
        cursor.execute("SELECT avatar_url, is_prime, friend_code FROM operators WHERE username = ?", (f_name,))
        res = cursor.fetchone()
        is_prime = 1 if f_name == "AKIN" else (res[1] if res else 0)
        f_code = res[2] if res and res[2] else ""
        friends_data.append({"username": f_name, "avatar_url": res[0] if res else "", "is_prime": is_prime, "friend_code": f_code})
        
    conn.close()
    return {"status": "success", "my_friend_code": my_code, "incoming_requests": incoming_requests, "friends": friends_data}

@app.post("/api/register")
def register_operator(data: OperatorAuth):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    try:
        new_code = generate_unique_friend_code(cursor)
        cursor.execute("INSERT INTO operators (username, password, friend_code) VALUES (?, ?, ?)", (data.username, data.password, new_code))
        conn.commit()
    except sqlite3.IntegrityError:
        cursor.execute("SELECT friend_code FROM operators WHERE username = ?", (data.username,))
        row = cursor.fetchone()
        if row and not row[0]:
            new_code = generate_unique_friend_code(cursor)
            cursor.execute("UPDATE operators SET friend_code = ? WHERE username = ?", (new_code, data.username))
            conn.commit()
    finally: conn.close()
    return {"status": "success"}

# --- STANDART API'LER ---

@app.post("/api/shopier-webhook")
async def shopier_webhook(request: Request):
    try:
        form_data = await request.form()
        status = form_data.get("status")
        operator_name = form_data.get("custom_payload") or form_data.get("order_note") 
        if status == "success" and operator_name:
            safe_op_name = operator_name.strip().upper()
            conn = sqlite3.connect("karargah.db")
            cursor = conn.cursor()
            cursor.execute("UPDATE operators SET is_prime = 1 WHERE username = ?", (safe_op_name,))
            conn.commit()
            conn.close()
            return {"status": "success", "message": f"{safe_op_name} Prime yapildi."}
        return {"status": "ignored"}
    except Exception as e: return {"status": "error", "detail": str(e)}

@app.post("/api/upgrade-prime")
def upgrade_to_prime(data: PrimeUpdate):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE operators SET is_prime = 1 WHERE username = ?", (data.username,))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.post("/api/update-server-icon")
def update_server_icon(data: ServerIconUpdate):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE servers SET icon_url = ? WHERE name = ?", (data.icon_url, data.server_name))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.get("/api/check-prime/{username}")
def check_prime(username: str):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_prime FROM operators WHERE username = ?", (username,))
    result = cursor.fetchone()
    conn.close()
    is_prime = 1 if username == "AKIN" else (result[0] if result else 0)
    return {"status": "success", "is_prime": is_prime}

@app.post("/api/upload")
async def upload_image(request: Request, file: UploadFile = File(...)):
    timestamp = int(time.time())
    safe_filename = f"{timestamp}_{file.filename.replace(' ', '_')}"
    file_location = f"uploads/{safe_filename}"
    with open(file_location, "wb") as buffer: shutil.copyfileobj(file.file, buffer)
    base_url = "https://karargah-backend-production.up.railway.app"
    return {"status": "success", "url": f"{base_url}/uploads/{safe_filename}"}

@app.post("/api/update-profile")
def update_profile(data: ProfileUpdate):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE operators SET avatar_url = ? WHERE username = ?", (data.avatar_url, data.username))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.get("/api/get-profile/{username}")
def get_profile(username: str):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("SELECT avatar_url, is_prime, friend_code FROM operators WHERE username = ?", (username,))
    result = cursor.fetchone()
    conn.close()
    if result: 
        is_prime = 1 if username == "AKIN" else result[1]
        return {"status": "success", "avatar_url": result[0], "is_prime": is_prime, "friend_code": result[2]}
    return {"status": "error"}

@app.post("/api/server/role")
def update_member_role(data: RoleUpdate):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("""INSERT INTO server_roles (server_name, username, role) VALUES (?, ?, ?)
                      ON CONFLICT(server_name, username) DO UPDATE SET role = ?""", 
                   (data.server_name, data.username, data.role, data.role))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.get("/api/server/roles/{server_name}")
def get_server_roles(server_name: str):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("SELECT username, role FROM server_roles WHERE server_name = ?", (server_name,))
    roles = cursor.fetchall()
    conn.close()
    return {"status": "success", "roles": {r[0]: r[1] for r in roles}}

@app.post("/api/servers")
def create_server(data: ServerCreate):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_prime FROM operators WHERE username = ?", (data.owner,))
    user_data = cursor.fetchone()
    is_prime = 1 if data.owner == "AKIN" else (user_data[0] if user_data else 0)
    cursor.execute("SELECT COUNT(*) FROM servers WHERE owner = ?", (data.owner,))
    server_count = cursor.fetchone()[0]
    max_servers = 5 if is_prime else 1
    
    if server_count >= max_servers:
        conn.close()
        raise HTTPException(status_code=403, detail=f"Maksimum karargah sınırına ({max_servers}) ulaştınız! Prime'a geçerek sınırı artırabilirsiniz.")
        
    try:
        cursor.execute("INSERT INTO servers (name, owner) VALUES (?, ?)", (data.name, data.owner))
        cursor.execute("INSERT INTO channels (server_name, channel_name, channel_type) VALUES (?, ?, ?)", (data.name, "operasyon-merkezi", "text"))
        cursor.execute("INSERT INTO channels (server_name, channel_name, channel_type) VALUES (?, ?, ?)", (data.name, "istihbarat-raporu", "text"))
        cursor.execute("INSERT INTO channels (server_name, channel_name, channel_type) VALUES (?, ?, ?)", (data.name, "GİZLİ HAREKAT", "voice"))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Bu isimde bir karargah zaten var!")
    finally: conn.close()
    return {"status": "success"}

@app.get("/api/servers/{username}")
def get_servers(username: str):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT name, owner, icon_url 
        FROM servers 
        WHERE owner = ? OR name IN (SELECT server_name FROM server_roles WHERE username = ?)
    """, (username, username))
    servers = cursor.fetchall()
    conn.close()
    return {"status": "success", "servers": [{"name": s[0], "owner": s[1], "icon_url": s[2] if len(s)>2 and s[2] else ""} for s in servers]}

@app.get("/api/channels/{server_name}")
def get_channels(server_name: str):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("SELECT channel_name, channel_type FROM channels WHERE server_name = ?", (server_name,))
    channels = cursor.fetchall()
    conn.close()
    return {"status": "success", "channels": [{"name": c[0], "type": c[1]} for c in channels]}

@app.post("/api/channels")
def create_channel(data: ChannelCreate):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("SELECT owner FROM servers WHERE name = ?", (data.server_name,))
    owner = cursor.fetchone()
    if not owner or owner[0] != data.operator_name:
        conn.close()
        raise HTTPException(status_code=403, detail="Sadece Karargah Komutanı yeni kanal açabilir!")
    try:
        safe_name = data.channel_name.replace(" ", "-").upper()
        cursor.execute("INSERT INTO channels (server_name, channel_name, channel_type) VALUES (?, ?, ?)", (data.server_name, safe_name, data.channel_type))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Bu isimde bir kanal zaten var!")
    finally: conn.close()
    return {"status": "success"}

@app.delete("/api/channels")
def delete_channel(data: ChannelDelete):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("SELECT owner FROM servers WHERE name = ?", (data.server_name,))
    owner = cursor.fetchone()
    if not owner or owner[0] != data.operator_name:
        conn.close()
        raise HTTPException(status_code=403, detail="Sadece Karargah Komutanı kanal silebilir!")
    cursor.execute("DELETE FROM channels WHERE server_name = ? AND channel_name = ? AND channel_type = ?", (data.server_name, data.channel_name, data.channel_type))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.get("/api/messages/{server_name}/{channel_name}")
def get_messages(server_name: str, channel_name: str):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("SELECT sender, text, time_str, msg_type FROM messages WHERE server_name = ? AND channel_name = ? ORDER BY id ASC LIMIT 50", (server_name, channel_name))
    msgs = cursor.fetchall()
    conn.close()
    return {"status": "success", "messages": [{"name": m[0], "text": m[1], "time": m[2], "type": m[3] if len(m)>3 and m[3] else "chat"} for m in msgs]}

# --- ODA WEBSOCKET YÖNETİCİSİ ---
class ConnectionManager:
    def __init__(self): self.rooms: Dict[str, Dict[str, WebSocket]] = {}
    async def connect(self, websocket: WebSocket, room_id: str, operator_name: str):
        await websocket.accept()
        if room_id not in self.rooms: self.rooms[room_id] = {}
        self.rooms[room_id][operator_name] = websocket
        await self.broadcast_online_users(room_id)
    def disconnect(self, websocket: WebSocket, room_id: str, operator_name: str):
        if room_id in self.rooms and operator_name in self.rooms[room_id]:
            del self.rooms[room_id][operator_name]
            if not self.rooms[room_id]: del self.rooms[room_id]
    async def broadcast(self, message: str, room_id: str, exclude: WebSocket = None):
        if room_id in self.rooms:
            for connection in self.rooms[room_id].values():
                if connection != exclude: await connection.send_text(message)
async def broadcast_online_users(self, room_id: str):
        if room_id in self.active_connections:
            # GİZLİ HAREKAT modunda olanları listeden çıkarıyoruz (Filtre)
            visible_users = []
            for op_name in self.active_connections[room_id].keys():
                if operator_statuses.get(op_name, "ÇEVRİMİÇİ") != "GİZLİ HAREKAT":
                    visible_users.append(op_name)
                    
            message = json.dumps({"type": "online_users", "users": visible_users})
            for connection in self.active_connections[room_id].values():
                await connection.send_text(message)
manager = ConnectionManager()
operator_statuses = {}

# --- GİZLİ KOMUT: MANUEL PRIME AKTİVASYONU (ZEKİ SÜRÜM) ---
@app.get("/api/secret-prime/{username}")
async def secret_give_prime(username: str):
    import sqlite3
    try:
        conn = sqlite3.connect('karargah.db')
        cursor = conn.cursor()
        
        # 1. Hangi tablo kullanılıyor otomatik bulalım
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [t[0] for t in cursor.fetchall()]
        
        table_name = None
        for t in ['users', 'user', 'operators', 'operator', 'accounts']:
            if t in tables:
                table_name = t
                break
                
        if not table_name:
            return {"error": f"Tablo bulunamadı! Mevcut tablolar: {tables}"}
            
        # 2. is_prime sütunu var mı kontrol et, yoksa ekle (Otomatik Göç)
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = [c[1] for c in cursor.fetchall()]
        
        if "is_prime" not in columns:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN is_prime INTEGER DEFAULT 0")
            
        # Kullanıcı adı sütununu bul
        user_col = "username"
        if "username" not in columns:
            if "operator_name" in columns: user_col = "operator_name"
            elif "name" in columns: user_col = "name"
            
        # 3. Prime yetkisini bas!
        cursor.execute(f"UPDATE {table_name} SET is_prime = 1 WHERE {user_col} = ?", (username,))
        conn.commit()
        conn.close()
        
        return {"status": "success", "message": f"Tebrikler! {username} artık PRIME statüsünde. (Hedef Tablo: {table_name})"}
    except Exception as e:
        return {"error": str(e)}

# --- İSTİHBARAT PANELİ (KULLANICI VE LOG İZLEME) ---
@app.get("/api/radar/istihbarat")
async def radar_istihbarat():
    import sqlite3
    try:
        conn = sqlite3.connect('karargah.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Hangi tablolar var bakalım
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [t[0] for t in cursor.fetchall()]
        
        data = {"tablolar": tables, "kullanicilar": [], "son_mesajlar": []}
        
        # Kullanıcılar tablosunu bul ve son 50 kaydı getir
        user_table = next((t for t in ['users', 'user', 'operators', 'accounts'] if t in tables), None)
        if user_table:
            cursor.execute(f"SELECT * FROM {user_table} LIMIT 50")
            data["kullanicilar"] = [dict(row) for row in cursor.fetchall()]
            
        # Logları (Mesajları) bul ve son 50 kaydı getir
        if 'messages' in tables:
            cursor.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 50")
            data["son_mesajlar"] = [dict(row) for row in cursor.fetchall()]
            
        conn.close()
        return data
    except Exception as e:
        return {"error": str(e)}
# --- YENİ: KİŞİSEL ÇAĞRI VE BİLDİRİM SANTRALİ (GLOBAL USER WEBSOCKET) ---
class UserConnectionManager:
    def __init__(self):
        self.user_sockets: Dict[str, WebSocket] = {}
    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        self.user_sockets[username] = websocket
    def disconnect(self, username: str):
        if username in self.user_sockets:
            del self.user_sockets[username]
    async def send_to_user(self, username: str, message: dict):
        if username in self.user_sockets:
            try:
                await self.user_sockets[username].send_text(json.dumps(message))
            except Exception:
                pass

user_manager = UserConnectionManager()

@app.websocket("/ws/user/{username}")
async def user_ws_endpoint(websocket: WebSocket, username: str):
    await user_manager.connect(websocket, username)
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            msg_type = msg.get("type")
            if msg_type == "call_user":
                target = msg.get("target")
                if target in user_manager.user_sockets:
                    await user_manager.send_to_user(target, {
                        "type": "incoming_call",
                        "caller": username,
                        "dm_room": msg.get("dm_room")
                    })
                else:
                    await user_manager.send_to_user(username, {
                        "type": "call_response",
                        "responder": target,
                        "action": "offline"
                    })
            elif msg_type == "call_response":
                target = msg.get("target")
                await user_manager.send_to_user(target, {
                    "type": "call_response",
                    "responder": username,
                    "action": msg.get("action")
                })
    except WebSocketDisconnect:
        user_manager.disconnect(username)

# -------------------------------------------------------------

@app.get("/api/servers/{server_name}/generate-invite")
def generate_invite(server_name: str):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    cursor.execute("UPDATE servers SET invite_code = ? WHERE name = ?", (code, server_name))
    conn.commit()
    conn.close()
    return {"status": "success", "invite_code": code}

@app.post("/api/join-server")
def join_server(data: JoinServerData):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM servers WHERE invite_code = ?", (data.invite_code,))
    result = cursor.fetchone()
    if result:
        server_name = result[0]
        try:
            cursor.execute("INSERT INTO server_roles (server_name, username, role) VALUES (?, ?, ?)", (server_name, data.username, "OPERATÖR"))
            conn.commit()
        except sqlite3.IntegrityError: pass
        conn.close()
        return {"status": "success", "server_name": server_name}
    conn.close()
    return {"status": "error", "detail": "Geçersiz veya süresi dolmuş davet kodu!"}

@app.websocket("/ws/{server_name}/{operator_name}")
async def websocket_endpoint(websocket: WebSocket, server_name: str, operator_name: str):
    room_id = server_name
    await manager.connect(websocket, room_id, operator_name)
    
    # Sisteme ilk giren standart ÇEVRİMİÇİ olur
    operator_statuses[operator_name] = "ÇEVRİMİÇİ"
    
    time_now = datetime.now().strftime("%H:%M")
    await manager.broadcast(json.dumps({"type": "system", "text": f"{operator_name.upper()} AĞA BAĞLANDI.", "time": time_now}), room_id)
    
    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            msg_type = data.get("type", "chat") 
            
            if msg_type in ["offer", "answer", "ice_candidate", "voice_join", "mute_status"]:
                await manager.broadcast(raw_data, room_id, exclude=websocket)
                
            elif msg_type == "status_update":
                # KULLANICI STATÜSÜNÜ DEĞİŞTİRDİ! (Çevrimiçi, DND, Gizli Harekat)
                status = data.get("status", "ÇEVRİMİÇİ")
                operator_statuses[operator_name] = status
                # Statü değiştiği için sağ paneli (online listesini) herkese baştan çizdir
                await manager.broadcast_online_users(room_id)
                
            elif msg_type in ["chat", "image"]:
                sender = data.get("sender", "BİLİNMEYEN")
                text = data.get("text", "") 
                channel_name = data.get("channel_name", "operasyon-merkezi")
                time_now = datetime.now().strftime("%H:%M")
                
                conn = sqlite3.connect("karargah.db")
                cursor = conn.cursor()
                cursor.execute("""INSERT INTO messages (server_name, channel_name, sender, text, time_str, msg_type) VALUES (?, ?, ?, ?, ?, ?)""", (server_name, channel_name, sender, text, time_now, msg_type))
                conn.commit()
                conn.close()
                
                chat_msg = json.dumps({"type": msg_type, "name": sender, "text": text, "time": time_now, "channel_name": channel_name})
                await manager.broadcast(chat_msg, room_id)
                
    except WebSocketDisconnect:
        manager.disconnect(websocket, room_id, operator_name)
        # Adam sistemden çıkınca statü hafızasını temizle
        if operator_name in operator_statuses:
            del operator_statuses[operator_name]
            
        time_now = datetime.now().strftime("%H:%M")
        await manager.broadcast(json.dumps({"type": "system", "text": f"{operator_name.upper()} BAĞLANTIYI KESTİ.", "time": time_now}), room_id)
        await manager.broadcast_online_users(room_id)