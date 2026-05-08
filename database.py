import sqlite3
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import config

DB_PATH = "hockey_bot.db"
TZ = ZoneInfo(config.TIMEZONE)


# ════════════════════════════════════════════════════════════
#  ИНИЦИАЛИЗАЦИЯ
# ════════════════════════════════════════════════════════════

def init_db():
    with sqlite3.connect(DB_PATH) as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                username      TEXT,
                child_name    TEXT    NOT NULL,
                booking_date  TEXT    NOT NULL,
                booking_time  TEXT    NOT NULL,
                created_at    TEXT    NOT NULL,
                reminder_sent        INTEGER DEFAULT 0,
                confirmation_sent    INTEGER DEFAULT 0,
                status        TEXT    DEFAULT 'active'
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                chat_id   INTEGER PRIMARY KEY,
                username  TEXT,
                added_at  TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocked_slots (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_date TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                UNIQUE(slot_date, slot_time)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS extra_slots (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_date TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                UNIQUE(slot_date, slot_time)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS prices (
                venue TEXT NOT NULL,
                name  TEXT NOT NULL,
                price TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                PRIMARY KEY (venue, name)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS entry_fees (
                venue TEXT PRIMARY KEY,
                fee   TEXT NOT NULL
            )
        """)

        conn.commit()

    _ensure_trainer_admin()
    _ensure_prices()


def _ensure_trainer_admin():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO admins (chat_id, username, added_at) VALUES (?, ?, ?)",
            (config.TRAINER_CHAT_ID, "trainer", datetime.now(TZ).isoformat())
        )
        conn.commit()


def _ensure_prices():
    with sqlite3.connect(DB_PATH) as conn:
        for venue, items in config.PRICING.items():
            for i, (name, price) in enumerate(items):
                conn.execute(
                    "INSERT OR IGNORE INTO prices (venue, name, price, sort_order) VALUES (?, ?, ?, ?)",
                    (venue, name, price, i)
                )
        for venue, fee in config.ICE_ENTRY_FEE.items():
            conn.execute(
                "INSERT OR IGNORE INTO entry_fees (venue, fee) VALUES (?, ?)",
                (venue, fee)
            )
        conn.commit()


# ════════════════════════════════════════════════════════════
#  ЗАПИСИ
# ════════════════════════════════════════════════════════════

def add_booking(user_id, username, child_name, booking_date, booking_time):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """INSERT INTO bookings
               (user_id, username, child_name, booking_date, booking_time, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, username, child_name, booking_date, booking_time,
             datetime.now(TZ).isoformat())
        )
        return cur.lastrowid


def get_unavailable_times(booking_date: str) -> set:
    """Занятые + заблокированные слоты на дату."""
    with sqlite3.connect(DB_PATH) as conn:
        booked = {r[0] for r in conn.execute(
            "SELECT booking_time FROM bookings WHERE booking_date=? AND status='active'",
            (booking_date,)
        ).fetchall()}
        blocked = {r[0] for r in conn.execute(
            "SELECT slot_time FROM blocked_slots WHERE slot_date=?",
            (booking_date,)
        ).fetchall()}
    return booked | blocked


def get_user_bookings(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            """SELECT id, child_name, booking_date, booking_time, status
               FROM bookings WHERE user_id=? AND booking_date >= ?
               ORDER BY booking_date, booking_time""",
            (user_id, date.today().isoformat())
        ).fetchall()


def cancel_booking(booking_id: int, user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "UPDATE bookings SET status='cancelled' WHERE id=? AND user_id=?",
            (booking_id, user_id)
        )
        return cur.rowcount > 0


def admin_cancel_booking(booking_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "UPDATE bookings SET status='cancelled' WHERE id=?",
            (booking_id,)
        )
        return cur.rowcount > 0


def get_booking_by_id(booking_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT id, user_id, username, child_name, booking_date, booking_time FROM bookings WHERE id=?",
            (booking_id,)
        ).fetchone()


def get_all_upcoming_bookings():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            """SELECT id, username, child_name, booking_date, booking_time
               FROM bookings WHERE booking_date >= ? AND status='active'
               ORDER BY booking_date, booking_time""",
            (date.today().isoformat(),)
        ).fetchall()


def get_bookings_for_reminder():
    now = datetime.now(TZ)
    remind_until = now + timedelta(hours=config.REMINDER_HOURS_BEFORE)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT id, user_id, child_name, booking_date, booking_time
               FROM bookings WHERE status='active' AND reminder_sent=0 AND booking_date >= ?""",
            (date.today().isoformat(),)
        ).fetchall()
    result = []
    for row in rows:
        _, _, _, bdate, btime = row
        dt = datetime.strptime(f"{bdate} {btime}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        if now < dt <= remind_until:
            result.append(row)
    return result


def mark_reminder_sent(booking_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE bookings SET reminder_sent=1 WHERE id=?", (booking_id,))


# ════════════════════════════════════════════════════════════
#  АДМИНЫ
# ════════════════════════════════════════════════════════════

def is_admin(chat_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT 1 FROM admins WHERE chat_id=?", (chat_id,)
        ).fetchone() is not None


def add_admin(chat_id: int, username: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admins (chat_id, username, added_at) VALUES (?, ?, ?)",
            (chat_id, username, datetime.now(TZ).isoformat())
        )
        conn.commit()


def remove_admin(chat_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM admins WHERE chat_id=? AND chat_id != ?",
                     (chat_id, config.TRAINER_CHAT_ID))
        conn.commit()


def get_admins():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT chat_id, username FROM admins"
        ).fetchall()


# ════════════════════════════════════════════════════════════
#  БЛОКИРОВКА / ДОППУСК СЛОТОВ
# ════════════════════════════════════════════════════════════

def block_slot(slot_date: str, slot_time: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO blocked_slots (slot_date, slot_time) VALUES (?, ?)",
            (slot_date, slot_time)
        )
        conn.commit()


def get_blocked_slots_on_date(slot_date: str):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT id, slot_time FROM blocked_slots WHERE slot_date=?",
            (slot_date,)
        ).fetchall()


def delete_blocked_slot(slot_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM blocked_slots WHERE id=?", (slot_id,))
        conn.commit()


def add_extra_slot(slot_date: str, slot_time: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO extra_slots (slot_date, slot_time) VALUES (?, ?)",
            (slot_date, slot_time)
        )
        conn.commit()


def get_extra_slots(slot_date: str) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT slot_time FROM extra_slots WHERE slot_date=?", (slot_date,)
        ).fetchall()
    return [r[0] for r in rows]


def get_extra_slot_dates() -> list:
    """Даты с доп. слотами в ближайшие 14 дней."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT DISTINCT slot_date FROM extra_slots WHERE slot_date >= ?",
            (date.today().isoformat(),)
        ).fetchall()
    return [r[0] for r in rows]


# ════════════════════════════════════════════════════════════
#  ЦЕНЫ
# ════════════════════════════════════════════════════════════

def get_prices():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT venue, name, price FROM prices ORDER BY venue, sort_order"
        ).fetchall()
        fees = conn.execute("SELECT venue, fee FROM entry_fees").fetchall()

    pricing = {}
    for venue, name, price in rows:
        pricing.setdefault(venue, []).append((name, price))

    entry_fees = {v: f for v, f in fees}
    return pricing, entry_fees


def update_price(venue: str, name: str, new_price: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE prices SET price=? WHERE venue=? AND name=?",
            (new_price, venue, name)
        )
        conn.commit()


def update_entry_fee(venue: str, new_fee: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE entry_fees SET fee=? WHERE venue=?", (new_fee, venue)
        )
        conn.commit()


def get_bookings_for_confirmation():
    """Записи, которым нужно подтверждение за CONFIRMATION_HOURS_BEFORE часов."""
    now = datetime.now(TZ)
    confirm_until = now + timedelta(hours=config.CONFIRMATION_HOURS_BEFORE)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT id, user_id, child_name, booking_date, booking_time
               FROM bookings WHERE status='active' AND confirmation_sent=0 AND booking_date >= ?""",
            (date.today().isoformat(),)
        ).fetchall()
    result = []
    for row in rows:
        _, _, _, bdate, btime = row
        dt = datetime.strptime(f"{bdate} {btime}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        if now < dt <= confirm_until:
            result.append(row)
    return result


def mark_confirmation_sent(booking_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE bookings SET confirmation_sent=1 WHERE id=?", (booking_id,))


def cancel_booking_by_id(booking_id: int):
    """Отмена записи по id без проверки user_id (для кнопки в подтверждении)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
        conn.commit()
