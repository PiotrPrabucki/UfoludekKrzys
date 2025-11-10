from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from datetime import datetime
import json
import sqlite3
from pathlib import Path
import threading
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI(title="Krzys Comm")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],                
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).parent / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

@app.get("/", include_in_schema=False)
def root_index():
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    raise HTTPException(status_code=404, detail="Frontend index not found")

DB_FILE = Path(__file__).parent / "krzys.db"
LOCK = threading.Lock()


class StateUpdate(BaseModel):
    energy: Optional[int] = Field(None, ge=0, le=100)
    heart_rate: Optional[int] = Field(None, ge=0)
    temperature: Optional[float] = None
    mood: Optional[str] = None

class StateResponse(BaseModel):
    energy: int
    heart_rate: int
    temperature: float
    mood: str
    updated_at: str

class CommRequest(BaseModel):
    message: str

def _conn():
    conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with LOCK:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                energy INTEGER NOT NULL,
                heart_rate INTEGER NOT NULL,
                temperature REAL NOT NULL,
                mood TEXT NOT NULL,
                last_updated TEXT NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,   -- 'state' | 'comm'
                who TEXT,             -- 'krzys' | 'user' | NULL
                content TEXT NOT NULL, -- JSON
                ts TEXT NOT NULL
            );
        """)
        cur.execute("SELECT COUNT(*) as c FROM state;")
        if cur.fetchone()["c"] == 0:
            now = datetime.utcnow().isoformat() + "Z"
            cur.execute("""
                INSERT INTO state(id, energy, heart_rate, temperature, mood, last_updated)
                VALUES (1, ?, ?, ?, ?, ?);
            """, (100, 2, -5.5, "neutral", now))
            conn.commit()
        conn.commit()
        conn.close()

def get_state_from_db():
    with LOCK:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT energy, heart_rate, temperature, mood, last_updated FROM state WHERE id = 1;")
        row = cur.fetchone()
        conn.close()
        if not row:
            raise RuntimeError("State missing")
        return {
            "energy": row["energy"],
            "heart_rate": row["heart_rate"],
            "temperature": row["temperature"],
            "mood": row["mood"],
            "updated_at": row["last_updated"]
        }

def update_state_in_db(changed: dict):
    with LOCK:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT energy, heart_rate, temperature, mood FROM state WHERE id = 1;")
        row = cur.fetchone()
        if not row:
            conn.close()
            raise RuntimeError("State missing")
        state = {
            "energy": row["energy"],
            "heart_rate": row["heart_rate"],
            "temperature": row["temperature"],
            "mood": row["mood"]
        }
        state.update(changed)
        ts = datetime.utcnow().isoformat() + "Z"
        cur.execute("""
            UPDATE state
            SET energy = ?, heart_rate = ?, temperature = ?, mood = ?, last_updated = ?
            WHERE id = 1;
        """, (state["energy"], state["heart_rate"], state["temperature"], state["mood"], ts))
        rec_content = {"changed": changed, "state": state}
        cur.execute("""
            INSERT INTO history (type, who, content, ts) VALUES (?, ?, ?, ?);
        """, ("state", None, json.dumps(rec_content, ensure_ascii=False), ts))
        conn.commit()
        conn.close()
        return {**state, "updated_at": ts}

def append_history_record(rec_type: str, who: Optional[str], content: dict):
    ts = datetime.utcnow().isoformat() + "Z"
    with LOCK:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO history (type, who, content, ts) VALUES (?, ?, ?, ?);
        """, (rec_type, who, json.dumps(content, ensure_ascii=False), ts))
        conn.commit()
        conn.close()
    return ts

def read_history(limit: Optional[int] = None, types: Optional[List[str]] = None):
    with LOCK:
        conn = _conn()
        cur = conn.cursor()
        q = "SELECT id, type, who, content, ts FROM history"
        params = []
        if types:
            placeholders = ",".join("?" for _ in types)
            q += f" WHERE type IN ({placeholders})"
            params.extend(types)
        q += " ORDER BY id DESC"
        if limit:
            q += " LIMIT ?"
            params.append(limit)
        cur.execute(q, params)
        rows = cur.fetchall()
        conn.close()
    out = []
    for r in rows:
        try:
            content = json.loads(r["content"])
        except Exception:
            content = {"raw": r["content"]}
        out.append({
            "id": r["id"],
            "type": r["type"],
            "who": r["who"],
            "content": content,
            "ts": r["ts"]
        })
    return out


init_db()

@app.get("/state", response_model=StateResponse)
def get_state():
    return get_state_from_db()

@app.post("/state", response_model=StateResponse)
def update_state(payload: StateUpdate):
    changed = payload.dict(exclude_none=True)
    if not changed:
        raise HTTPException(status_code=400, detail="Brak pól do zaktualizowania")
    return update_state_in_db(changed)

@app.post("/comm")
def comm(req: CommRequest, who: int = 1):
    if who not in (0, 1):
        raise HTTPException(status_code=400, detail="Parametr who musi byc 0 (krzys) lub 1 (user)")
    who_str = "krzys" if who == 0 else "user"
    ts = append_history_record("comm", who_str, {"message": req.message})
    return {"status": "saved", "who": who_str, "ts": ts}

@app.get("/history")
def history(limit: Optional[int] = None, kind: Optional[str] = None):
    types = None
    if kind:
        if kind not in ("state", "comm"):
            raise HTTPException(status_code=400, detail="kind musi być 'state' lub 'comm'")
        types = [kind]
    items = read_history(limit=limit, types=types)
    return {"count": len(items), "items": items}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000)