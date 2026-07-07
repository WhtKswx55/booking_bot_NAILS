"""
database.py — слой работы с SQLite.

Хранит:
 - slots: доступные временные слоты (дата+время), статус (free/booked)
 - bookings: записи клиенток на слот, статус (active/cancelled)
 - clients: кэш клиенток (для истории/админки)
"""
import aiosqlite
from datetime import datetime, date, time
from contextlib import asynccontextmanager

DB_PATH = "booking.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_date TEXT NOT NULL,        -- 'YYYY-MM-DD'
    slot_time TEXT NOT NULL,        -- 'HH:MM'
    is_booked INTEGER NOT NULL DEFAULT 0,
    UNIQUE(slot_date, slot_time)
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER NOT NULL,
    client_tg_id INTEGER NOT NULL,
    client_name TEXT NOT NULL,
    client_username TEXT,
    service TEXT,
    phone TEXT,
    status TEXT NOT NULL DEFAULT 'active',   -- active / cancelled
    created_at TEXT NOT NULL,
    reminded INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(slot_id) REFERENCES slots(id)
);

CREATE TABLE IF NOT EXISTS clients (
    tg_id INTEGER PRIMARY KEY,
    name TEXT,
    username TEXT,
    phone TEXT,
    first_seen TEXT
);
"""


@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


async def init_db():
    async with get_db() as db:
        await db.executescript(SCHEMA)
        await db.commit()


# ---------- SLOTS ----------

async def add_slot(slot_date: str, slot_time: str):
    async with get_db() as db:
        try:
            await db.execute(
                "INSERT INTO slots (slot_date, slot_time, is_booked) VALUES (?, ?, 0)",
                (slot_date, slot_time),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False  # уже существует


async def add_slots_bulk(slots: list[tuple[str, str]]):
    """slots: список (date, time)"""
    added = 0
    async with get_db() as db:
        for d, t in slots:
            try:
                await db.execute(
                    "INSERT INTO slots (slot_date, slot_time, is_booked) VALUES (?, ?, 0)",
                    (d, t),
                )
                added += 1
            except aiosqlite.IntegrityError:
                continue
        await db.commit()
    return added


async def get_free_slots(from_date: str | None = None) -> list[dict]:
    q = "SELECT * FROM slots WHERE is_booked = 0"
    params = []
    if from_date:
        q += " AND slot_date >= ?"
        params.append(from_date)
    q += " ORDER BY slot_date, slot_time"
    async with get_db() as db:
        cur = await db.execute(q, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_slot(slot_id: int) -> dict | None:
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM slots WHERE id = ?", (slot_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_slot_booked(slot_id: int, booked: bool):
    async with get_db() as db:
        await db.execute(
            "UPDATE slots SET is_booked = ? WHERE id = ?", (1 if booked else 0, slot_id)
        )
        await db.commit()


async def delete_slot(slot_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
        await db.commit()


# ---------- CLIENTS ----------

async def upsert_client(tg_id: int, name: str, username: str | None, phone: str | None = None):
    async with get_db() as db:
        cur = await db.execute("SELECT tg_id FROM clients WHERE tg_id = ?", (tg_id,))
        exists = await cur.fetchone()
        if exists:
            if phone:
                await db.execute(
                    "UPDATE clients SET name=?, username=?, phone=? WHERE tg_id=?",
                    (name, username, phone, tg_id),
                )
            else:
                await db.execute(
                    "UPDATE clients SET name=?, username=? WHERE tg_id=?",
                    (name, username, tg_id),
                )
        else:
            await db.execute(
                "INSERT INTO clients (tg_id, name, username, phone, first_seen) VALUES (?, ?, ?, ?, ?)",
                (tg_id, name, username, phone, datetime.now().isoformat()),
            )
        await db.commit()


# ---------- BOOKINGS ----------

async def create_booking(
    slot_id: int, client_tg_id: int, client_name: str, client_username: str | None,
    service: str | None = None, phone: str | None = None
) -> int | None:
    """Возвращает booking_id, либо None если слот уже занят (гонка)."""
    async with get_db() as db:
        cur = await db.execute("SELECT is_booked FROM slots WHERE id = ?", (slot_id,))
        row = await cur.fetchone()
        if not row or row["is_booked"]:
            return None
        await db.execute("UPDATE slots SET is_booked = 1 WHERE id = ?", (slot_id,))
        cur = await db.execute(
            """INSERT INTO bookings
               (slot_id, client_tg_id, client_name, client_username, service, phone, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?)""",
            (slot_id, client_tg_id, client_name, client_username, service, phone, datetime.now().isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def get_booking(booking_id: int) -> dict | None:
    async with get_db() as db:
        cur = await db.execute(
            """SELECT b.*, s.slot_date, s.slot_time
               FROM bookings b JOIN slots s ON b.slot_id = s.id
               WHERE b.id = ?""",
            (booking_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_client_bookings(tg_id: int, active_only=True) -> list[dict]:
    q = """SELECT b.*, s.slot_date, s.slot_time
           FROM bookings b JOIN slots s ON b.slot_id = s.id
           WHERE b.client_tg_id = ?"""
    params = [tg_id]
    if active_only:
        q += " AND b.status = 'active'"
    q += " ORDER BY s.slot_date, s.slot_time"
    async with get_db() as db:
        cur = await db.execute(q, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def cancel_booking(booking_id: int):
    booking = await get_booking(booking_id)
    if not booking:
        return False
    async with get_db() as db:
        await db.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
        await db.execute("UPDATE slots SET is_booked = 0 WHERE id = ?", (booking["slot_id"],))
        await db.commit()
    return True


async def reschedule_booking(booking_id: int, new_slot_id: int) -> bool:
    """Переносит запись на новый слот. Возвращает False если новый слот занят."""
    booking = await get_booking(booking_id)
    if not booking:
        return False
    async with get_db() as db:
        cur = await db.execute("SELECT is_booked FROM slots WHERE id = ?", (new_slot_id,))
        row = await cur.fetchone()
        if not row or row["is_booked"]:
            return False
        # освобождаем старый слот, занимаем новый
        await db.execute("UPDATE slots SET is_booked = 0 WHERE id = ?", (booking["slot_id"],))
        await db.execute("UPDATE slots SET is_booked = 1 WHERE id = ?", (new_slot_id,))
        await db.execute(
            "UPDATE bookings SET slot_id = ?, reminded = 0 WHERE id = ?", (new_slot_id, booking_id)
        )
        await db.commit()
    return True


async def get_all_active_bookings(from_date: str | None = None) -> list[dict]:
    q = """SELECT b.*, s.slot_date, s.slot_time
           FROM bookings b JOIN slots s ON b.slot_id = s.id
           WHERE b.status = 'active'"""
    params = []
    if from_date:
        q += " AND s.slot_date >= ?"
        params.append(from_date)
    q += " ORDER BY s.slot_date, s.slot_time"
    async with get_db() as db:
        cur = await db.execute(q, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_bookings_needing_reminder(target_date: str) -> list[dict]:
    """Активные записи на target_date, по которым ещё не отправлено напоминание."""
    async with get_db() as db:
        cur = await db.execute(
            """SELECT b.*, s.slot_date, s.slot_time
               FROM bookings b JOIN slots s ON b.slot_id = s.id
               WHERE b.status = 'active' AND b.reminded = 0 AND s.slot_date = ?""",
            (target_date,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def mark_reminded(booking_id: int):
    async with get_db() as db:
        await db.execute("UPDATE bookings SET reminded = 1 WHERE id = ?", (booking_id,))
        await db.commit()
