import json
import sqlite3
import os
import shutil
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Dict
import random
import string

app = FastAPI(title="Karargah Backend v1.4 - Dinamik Kanallar ve İzolasyon")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# 1. VERİTABANI YENİDEN İNŞASI VE GÜNCELLEMELER
def init_db():
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    
    cursor.execute("""CREATE TABLE IF NOT EXISTS operators (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, is_prime INTEGER DEFAULT 0, avatar_url TEXT DEFAULT '')""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS servers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, owner TEXT NOT NULL, icon_url TEXT DEFAULT '', invite_code TEXT DEFAULT '')""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, server_name TEXT NOT NULL, channel_name TEXT NOT NULL, sender TEXT NOT NULL, text TEXT NOT NULL, time_str TEXT NOT NULL, msg_type TEXT DEFAULT 'chat')""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS server_roles (id INTEGER PRIMARY KEY AUTOINCREMENT, server_name TEXT NOT NULL, username TEXT NOT NULL, role TEXT NOT NULL, UNIQUE(server_name, username))""")
    
    # YENİ TABLO: DİNAMİK KANALLAR
    cursor.execute("""CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY AUTOINCREMENT, server_name TEXT NOT NULL, channel_name TEXT NOT NULL, channel_type TEXT NOT NULL, UNIQUE(server_name, channel_name, channel_type))""")

    # Eski DB varsa yama yapsın
    try: cursor.execute("ALTER TABLE operators ADD COLUMN avatar_url TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE servers ADD COLUMN icon_url TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE servers ADD COLUMN invite_code TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE messages ADD COLUMN msg_type TEXT DEFAULT 'chat'")
    except: pass

    # GÜNCELLEME 1: KUZEY KARTALLARI İZOLASYONU (TAPU 'AKIN' KULLANICISINA VERİLİYOR)
    cursor.execute("SELECT COUNT(*) FROM servers")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO servers (name, owner) VALUES ('KUZEY KARTALLARI', 'AKIN')")
        
    try:
        # Mevcut veritabanındaki global KUZEY KARTALLARI'nı AKIN'a devret
        cursor.execute("UPDATE servers SET owner = 'AKIN' WHERE name = 'KUZEY KARTALLARI' AND owner = 'SİSTEM'")
    except: pass

    # GÜNCELLEME 2: MEVCUT SUNUCULARA VARSAYILAN KANALLARI EKLEME
    cursor.execute("SELECT name FROM servers")
    existing_servers = cursor.fetchall()
    for srv in existing_servers:
        s_name = srv[0]
        try:
            cursor.execute("INSERT INTO channels (server_name, channel_name, channel_type) VALUES (?, ?, ?)", (s_name, "operasyon-merkezi", "text"))
            cursor.execute("INSERT INTO channels (server_name, channel_name, channel_type) VALUES (?, ?, ?)", (s_name, "istihbarat-raporu", "text"))
            cursor.execute("INSERT INTO channels (server_name, channel_name, channel_type) VALUES (?, ?, ?)", (s_name, "GİZLİ HAREKAT", "voice"))
        except:
            pass # Zaten ekliyse hata vermez, geçer

    conn.commit()
    conn.close()

init_db()

# --- MODELLER ---
class PrimeUpdate(BaseModel): username: str
class ServerIconUpdate(BaseModel): server_name: str; icon_url: str
class OperatorAuth(BaseModel): username: str; password: str
class ServerCreate(BaseModel): name: str; owner: str
class RoleUpdate(BaseModel): server_name: str; username: str; role: str
class ProfileUpdate(BaseModel): username: str; avatar_url: str
class JoinServerData(BaseModel): username: str; invite_code: str
class ChannelCreate(BaseModel): server_name: str; channel_name: str; channel_type: str; operator_name: str
class ChannelDelete(BaseModel): server_name: str; channel_name: str; channel_type: str; operator_name: str

# --- REST API UÇ NOKTALARI ---

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
    return {"status": "success", "is_prime": result[0] if result else 0}

@app.post("/api/upload")
async def upload_image(request: Request, file: UploadFile = File(...)):
    safe_filename = file.filename.replace(" ", "_")
    file_location = f"uploads/{safe_filename}"
    with open(file_location, "wb") as buffer: shutil.copyfileobj(file.file, buffer)
    base_url = str(request.base_url).rstrip("/")
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
    cursor.execute("SELECT avatar_url, is_prime FROM operators WHERE username = ?", (username,))
    result = cursor.fetchone()
    conn.close()
    if result: return {"status": "success", "avatar_url": result[0], "is_prime": result[1]}
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

@app.post("/api/register")
def register_operator(data: OperatorAuth):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO operators (username, password) VALUES (?, ?)", (data.username, data.password))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Bu operatör kodu zaten kullanımda!")
    finally: conn.close()
    return {"status": "success"}

@app.post("/api/login")
def login_operator(data: OperatorAuth):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM operators WHERE username = ? AND password = ?", (data.username, data.password))
    user = cursor.fetchone()
    conn.close()
    if not user: raise HTTPException(status_code=401, detail="Hatalı operatör kodu veya şifre!")
    return {"status": "success"}

@app.post("/api/servers")
def create_server(data: ServerCreate):
    conn = sqlite3.connect("karargah.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM servers WHERE owner = ?", (data.owner,))
    server_count = cursor.fetchone()[0]
    if server_count >= 3:
        conn.close()
        raise HTTPException(status_code=403, detail="Maksimum karargah sınırına (3) ulaştınız!")
    try:
        cursor.execute("INSERT INTO servers (name, owner) VALUES (?, ?)", (data.name, data.owner))
        
        # YENİ SUNUCUYA VARSAYILAN KANALLARI EKLE
        cursor.execute("INSERT INTO channels (server_name, channel_name, channel_type) VALUES (?, ?, ?)", (data.name, "operasyon-merkezi", "text"))
        cursor.execute("INSERT INTO channels (server_name, channel_name, channel_type) VALUES (?, ?, ?)", (data.name, "istihbarat-raporu", "text"))
        cursor.execute("INSERT INTO channels (server_name, channel_name, channel_type) VALUES (?, ?, ?)", (data.name, "GİZLİ HAREKAT", "voice"))
        
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Bu isimde bir karargah zaten var!")
    finally: conn.close()
    return {"status": "success"}

# GÜNCELLEME 3: ARTIK HERKES SADECE KENDİ ODASINI VEYA DAVETLİ OLDUĞUNU GÖRÜR
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

# YENİ KANAL YÖNETİM API'LERİ
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
    # Şimdilik sadece kurucu açabilir
    if not owner or owner[0] != data.operator_name:
        conn.close()
        raise HTTPException(status_code=403, detail="Sadece Karargah Komutanı yeni kanal açabilir!")
        
    try:
        # İsimleri formata uygun hale getirme (Boşlukları '-' yap, BÜYÜK harf yap)
        safe_name = data.channel_name.replace(" ", "-").upper()
        cursor.execute("INSERT INTO channels (server_name, channel_name, channel_type) VALUES (?, ?, ?)", (data.server_name, safe_name, data.channel_type))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Bu isimde bir kanal zaten var!")
    finally:
        conn.close()
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

# --- WEBSOCKET & DAVET KODU ---

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
        if room_id in self.rooms:
            active_users = list(self.rooms[room_id].keys())
            msg = json.dumps({"type": "online_users", "users": active_users})
            for connection in self.rooms[room_id].values(): await connection.send_text(msg)

manager = ConnectionManager()

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
        except sqlite3.IntegrityError:
            pass
        conn.close()
        return {"status": "success", "server_name": server_name}
        
    conn.close()
    return {"status": "error", "detail": "Geçersiz veya süresi dolmuş davet kodu!"}

@app.websocket("/ws/{server_name}/{operator_name}")
async def websocket_endpoint(websocket: WebSocket, server_name: str, operator_name: str):
    room_id = server_name
    await manager.connect(websocket, room_id, operator_name)
    time_now = datetime.now().strftime("%H:%M")
    await manager.broadcast(json.dumps({"type": "system", "text": f"{operator_name.upper()} AĞA BAĞLANDI.", "time": time_now}), room_id)
    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            msg_type = data.get("type", "chat") 
            if msg_type in ["offer", "answer", "ice_candidate", "voice_join", "mute_status"]:
                await manager.broadcast(raw_data, room_id, exclude=websocket)
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
        time_now = datetime.now().strftime("%H:%M")
        await manager.broadcast(json.dumps({"type": "system", "text": f"{operator_name.upper()} BAĞLANTIYI KESTİ.", "time": time_now}), room_id)
        await manager.broadcast_online_users(room_id)