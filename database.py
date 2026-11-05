import aiosqlite
import json
from datetime import datetime
from config import DB_PATH

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            telegram_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            data TEXT DEFAULT '{}',
            main_message_id INTEGER,
            institution_request_id TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            event TEXT,
            details TEXT,
            created_at TEXT
        );
        """)
        await db.commit()

async def get_or_create_user(tg_id, first_name, username):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, first_name, username, created_at) VALUES (?,?,?,?)",
            (tg_id, first_name or "", username or "", datetime.utcnow().isoformat())
        )
        await db.commit()

async def get_session(tg_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT state, data, main_message_id, institution_request_id FROM sessions WHERE telegram_id=?",
            (tg_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "state": row[0],
                "data": json.loads(row[1] or "{}"),
                "main_message_id": row[2],
                "institution_request_id": row[3],
            }

async def save_session(tg_id, state=None, data=None, main_message_id=None, institution_request_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        existing = await get_session(tg_id)
        now = datetime.utcnow().isoformat()
        if existing is None:
            await db.execute(
                "INSERT INTO sessions (telegram_id, state, data, main_message_id, institution_request_id, updated_at) VALUES (?,?,?,?,?,?)",
                (tg_id, state or "inicio", json.dumps(data or {}), main_message_id, institution_request_id, now)
            )
        else:
            updates, params = [], []
            if state is not None:
                updates.append("state=?"); params.append(state)
            if data is not None:
                updates.append("data=?"); params.append(json.dumps(data))
            if main_message_id is not None:
                updates.append("main_message_id=?"); params.append(main_message_id)
            if institution_request_id is not None:
                updates.append("institution_request_id=?"); params.append(institution_request_id)
            updates.append("updated_at=?"); params.append(now)
            params.append(tg_id)
            await db.execute(f"UPDATE sessions SET {', '.join(updates)} WHERE telegram_id=?", params)
        await db.commit()

async def reset_session(tg_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE telegram_id=?", (tg_id,))
        await db.commit()

async def log_event(tg_id, event, details=""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO events (telegram_id, event, details, created_at) VALUES (?,?,?,?)",
            (tg_id, event, details, datetime.utcnow().isoformat())
        )
        await db.commit()

async def get_admin_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total_users = (await c.fetchone())[0]
        async with db.execute("SELECT state, COUNT(*) FROM sessions GROUP BY state") as c:
            rows = await c.fetchall()
        async with db.execute("SELECT COUNT(*) FROM events") as c:
            total_events = (await c.fetchone())[0]
    lines = [f"📊 *Painel Admin*\n\n👥 Usuários: {total_users}\n📝 Eventos: {total_events}\n\n*Sessões por estado:*"]
    for state, count in rows:
        lines.append(f"• `{state}` — {count}")
    return "\n".join(lines)
