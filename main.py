# main.py (RAILWAY BACKEND - TAM SÜRÜM V3.0)
import json
import sqlite3
import random
import string
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, List

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

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

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Dict[str, WebSocket]] = {}
        self.user_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, room_id: str, operator_name: str):
        await websocket.accept()
        if room_id not in self.active_connections:
            self.active_connections[room_id] = {}
        self.active_connections[room_id][operator_name] = websocket

    def disconnect(self, websocket: WebSocket, room_id: str, operator_name: str):
        if room_id in self.active_connections and operator_name in self.active_connections[room_id]:
            del self.active_connections[room_id][operator_name]
            if not self.active_connections[room_id]:
                del self.active_connections[room_id]

    async def broadcast(self, message: str, room_id: str, exclude: WebSocket = None):
        if room_id in self.active_connections:
            for connection in list(self.active_connections[room_id].values()):
                if connection != exclude:
                    try: await connection.send_text(message)
                    except: pass

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
    
    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            msg_type = data.get("type", "chat") 
            
            if msg_type in ["offer", "answer", "ice_candidate", "voice_join", "mute_status"]:
                await manager.broadcast(raw_data, server_name, exclude=websocket)
            
            elif msg_type == "status_update":
                operator_statuses[operator_name] = data.get("status", "ÇEVRİMİÇİ")
                await manager.broadcast_online_users(server_name)
                
            elif msg_type in ["chat", "image"]:
                sender = data.get("sender", "BİLİNMEYEN")
                text = data.get("text", "") 
                channel_name = data.get("channel_name", "operasyon-merkezi")
                time_now = datetime.now().strftime("%H:%M")
                
                conn = sqlite3.connect("karargah.db")
                conn.execute("INSERT INTO messages (server_name, channel_name, sender, text, time_str, msg_type) VALUES (?, ?, ?, ?, ?, ?)", (server_name, channel_name, sender, text, time_now, msg_type))
                conn.commit()
                conn.close()
                await manager.broadcast(json.dumps({"type": msg_type, "name": sender, "text": text, "time": time_now, "channel_name": channel_name}), server_name)
                
    except WebSocketDisconnect:
        manager.disconnect(websocket, server_name, operator_name)
        if operator_name in operator_statuses: del operator_statuses[operator_name]
        time_now = datetime.now().strftime("%H:%M")
        await manager.broadcast(json.dumps({"type": "system", "text": f"{operator_name.upper()} BAĞLANTIYI KESTİ.", "time": time_now}), server_name)
        await manager.broadcast_online_users(server_name)

@app.websocket("/ws/user/{operator_name}")
async def websocket_user_endpoint(websocket: WebSocket, operator_name: str):
    await websocket.accept()
    manager.user_connections[operator_name] = websocket
    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            if data.get("type") == "call_user":
                target = data.get("target")
                if target in manager.user_connections:
                    await manager.user_connections[target].send_text(json.dumps({"type": "incoming_call", "caller": operator_name, "dm_room": data.get("dm_room")}))
            elif data.get("type") == "call_response":
                target = data.get("target")
                if target in manager.user_connections:
                    await manager.user_connections[target].send_text(json.dumps({"type": "call_response", "responder": operator_name, "action": data.get("action")}))
    except WebSocketDisconnect:
        if operator_name in manager.user_connections: del manager.user_connections[operator_name]


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

@app.post("/api/join-server")
async def join_server(data: dict):
    db = get_db()
    server = db.execute("SELECT name FROM servers WHERE invite_code = ?", (data["invite_code"],)).fetchone()
    if not server: raise HTTPException(400, "Geçersiz davet kodu.")
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

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    return {"url": "https://karargah.stratr.com.tr/assets/default_image.jpg"} # Gerçek sunucuda burası bulut depolamaya gidecek

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
    
    return {"my_friend_code": my_code, "incoming_requests": [r["user1"] for r in reqs], "friends": [dict(f) for f in friends]}
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

# --- GİZLİ KOMUT: MANUEL PRIME AKTİVASYONU (ZEKİ SÜRÜM) ---
@app.get("/api/secret-prime/{username}")
async def secret_give_prime(username: str):
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
async def radar_istihbarat():
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