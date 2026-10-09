"""
SARN (Solo + Learn) - Gamifikatsiyalangan Ta'lim Telegram Boti va Mini App Serveri.

Xususiyatlar va Render Deploy Integratsiyasi:
- Telegram Bot: @sarnuzbot
- Baza guruhi (Telegram Storage Group): Admin yuklagan barcha fon rasmlari va
  har 1 soatda avtomatik ravishda butun SQLite bazasi (sarn.db) ushbu guruhga
  nusxalanadi va PIN qilib qo'yiladi.
- Render Ephemeral Diskdan Himoya: Render qayta deploy bo'lganda yoki server
  restart bo'lganda, bot avtomatik Baza guruhidagi PIN qilingan fayldan
  bazani (sarn.db) tiklaydi.
- Soatlik Auto-Backup: Har 1 soatda avtomatik zaxira guruhga yuklanadi va pin qilinadi,
  eski zaxira xabari esa guruh to'lib ketmasligi uchun o'chiriladi.
- /chatid buyrug'i: Guruh ichida yozilsa, bot o'sha guruhni Baza Guruhi deb belgilaydi.
- Render Health Check: Render taqdim etgan PORT bo'yicha veb-server ishlaydi.
"""

import asyncio
import base64
import json
import logging
import os
import sqlite3
import threading
import urllib.request
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.error import ChatMigrated
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)

# ==========================================
# 1. SOZLAMALAR VA KONFIGURATSIYA
# ==========================================
# SARN bot tokeni: @sarnuzbot
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7545071745:AAGexwd5i3TTorY7yPr3KQGIWDomzeLmvTY")
ADMIN_IDS = [5393636771]
DB_PATH = os.environ.get("DB_PATH", "sarn.db")
WEB_PORT = int(os.environ.get("PORT", "8080"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
IMAGES_DIR = os.path.join(WEB_DIR, "images")

# Standart Baza guruhi ID (muhit o'zgaruvchisidan ham o'qilishi mumkin)
_storage_chat_id_raw = os.environ.get("STORAGE_CHAT_ID", "").strip()
DEFAULT_STORAGE_CHAT_ID = int(_storage_chat_id_raw) if _storage_chat_id_raw.lstrip("-").isdigit() else None

# Rasmlar papkasini yaratish
os.makedirs(IMAGES_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("SARN_BOT")

# Conversation holatlari
WAITING_VIP_USER, WAITING_VIP_DAYS = range(2)
WAITING_BONUS_USER, WAITING_BONUS_TYPE, WAITING_BONUS_AMOUNT = range(2, 5)
WAITING_BROADCAST_MSG = 5
WAITING_WEBAPP_URL = 6
WAITING_BG_PHOTO, WAITING_BG_CODE, WAITING_BG_NAME, WAITING_BG_PRICE = range(7, 11)
WAITING_STORAGE_GROUP = 11
WAITING_DB_UPLOAD, WAITING_DB_UPLOAD_CONFIRM = range(12, 14)
WAITING_TEST_Q, WAITING_TEST_OPTA, WAITING_TEST_OPTB, WAITING_TEST_OPTC, WAITING_TEST_OPTD, WAITING_TEST_CORRECT, WAITING_TEST_SUBJECT = range(14, 21)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ==========================================
# 2. MA'LUMOTLAR BAZASI
# ==========================================

RANKS_SCALE = [
    ("F",   0,      500,       "Boshlang'ich"),
    ("E",   500,    1000,      "Boshlang'ich+"),
    ("D",   1000,   5000,      "O'rta"),
    ("C",   5000,   10000,     "Ilg'or"),
    ("B",   10000,  50000,     "Bilimdon"),
    ("A",   50000,  100000,    "Mutaxassis"),
    ("S",   100000, 200000,    "Usta"),
    ("SS",  200000, 500000,    "Afsonaviy"),
    ("SSS", 500000, 999999999, "O'qituvchi darajasi"),
]


def calculate_rank(exp: int):
    for i, (rank, min_exp, max_exp, title) in enumerate(RANKS_SCALE):
        if min_exp <= exp < max_exp:
            next_rank = RANKS_SCALE[i + 1][0] if i + 1 < len(RANKS_SCALE) else "MAX"
            next_exp = max_exp
            range_span = max_exp - min_exp
            current_in_level = exp - min_exp
            progress_pct = min(100, max(0, int((current_in_level / range_span) * 100)))
            return {
                "rank": rank, "title": title,
                "min_exp": min_exp, "max_exp": max_exp,
                "next_rank": next_rank, "next_rank_exp": next_exp,
                "needed_exp": max_exp - exp, "progress_pct": progress_pct,
            }
    return {
        "rank": "SSS", "title": "O'qituvchi darajasi",
        "min_exp": 500000, "max_exp": 999999999,
        "next_rank": "MAX", "next_rank_exp": 500000,
        "needed_exp": 0, "progress_pct": 100,
    }


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Foydalanuvchilar
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            exp INTEGER DEFAULT 0,
            coins INTEGER DEFAULT 0,
            rank TEXT DEFAULT 'F',
            vip_until TIMESTAMP,
            streak INTEGER DEFAULT 1,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Sozlamalar (webapp_url, storage_chat_id, last_backup_message_id, va h.k.)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Faollik
    cur.execute("""
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Fonlar (Telegram guruhi storage parametrlari va unikal kod bilan birga)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS backgrounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            filename TEXT NOT NULL,
            price INTEGER NOT NULL DEFAULT 0,
            code TEXT UNIQUE,
            file_id TEXT,
            storage_chat_id INTEGER,
            storage_message_id INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migratsiya
    try:
        cur.execute("ALTER TABLE backgrounds ADD COLUMN code TEXT")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE backgrounds ADD COLUMN file_id TEXT")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE backgrounds ADD COLUMN storage_chat_id INTEGER")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE backgrounds ADD COLUMN storage_message_id INTEGER")
    except Exception:
        pass
    try:
        cur.execute("UPDATE backgrounds SET code = id || 'bg' WHERE code IS NULL OR code = ''")
    except Exception:
        pass

    # Foydalanuvchi xarid qilgan fonlar
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_backgrounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            bg_id INTEGER NOT NULL,
            bought_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, bg_id),
            FOREIGN KEY (bg_id) REFERENCES backgrounds(id)
        )
    """)

    # Fanlar jadvali
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            slug TEXT NOT NULL UNIQUE,
            icon TEXT NOT NULL
        )
    """)

    # Test savollari jadvali
    cur.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER NOT NULL,
            question TEXT NOT NULL,
            option_a TEXT NOT NULL,
            option_b TEXT NOT NULL,
            option_c TEXT NOT NULL,
            option_d TEXT NOT NULL,
            correct_option TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (subject_id) REFERENCES subjects(id)
        )
    """)
    try:
        cur.execute("ALTER TABLE questions ADD COLUMN difficulty_level TEXT NOT NULL DEFAULT 'F'")
    except Exception:
        pass

    # Standart 10 ta fanni bazaga kiritish (agar bo'lmasa)
    default_subjects = [
        ("Informatika", "informatika", "fa-laptop-code"),
        ("Tarix", "tarix", "fa-landmark"),
        ("Matematika", "matematika", "fa-calculator"),
        ("Adabiyot", "adabiyot", "fa-feather-pointed"),
        ("Ona tili", "ona-tili", "fa-language"),
        ("Fizika", "fizika", "fa-atom"),
        ("Kimyo", "kimyo", "fa-flask-vial"),
        ("Ingliz tili", "ingliz-tili", "fa-earth-americas"),
        ("Rus tili", "rus-tili", "fa-book-atlas"),
        ("Geografiya", "geografiya", "fa-globe"),
    ]
    for name, slug, icon in default_subjects:
        cur.execute(
            "INSERT OR IGNORE INTO subjects (name, slug, icon) VALUES (?, ?, ?)",
            (name, slug, icon),
        )

    # Kunlik missiyalar jadvali
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_missions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            reward_exp INTEGER NOT NULL DEFAULT 0,
            mission_type TEXT NOT NULL DEFAULT 'questions',
            target INTEGER NOT NULL DEFAULT 5,
            date TEXT NOT NULL DEFAULT (date('now'))
        )
    """)

    # Har bir foydalanuvchi missiya holati
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_daily_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            mission_id INTEGER NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            session_start TEXT,
            UNIQUE(chat_id, mission_id),
            FOREIGN KEY (mission_id) REFERENCES daily_missions(id)
        )
    """)

    conn.commit()
    conn.close()


def get_setting(key: str, default=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_storage_chat_id():
    """Baza guruhi (storage chat) ID sini aniqlaydi."""
    val = get_setting("storage_chat_id", None)
    if val and val.lstrip("-").isdigit():
        return int(val)
    return DEFAULT_STORAGE_CHAT_ID


def _count_users_in_db() -> int:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        count = cur.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def get_or_create_user(chat_id: int, username: str = None, full_name: str = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO users (chat_id, username, full_name, exp, coins, rank) "
            "VALUES (?, ?, ?, 0, 0, 'F')",
            (chat_id, username, full_name),
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
    else:
        cur.execute(
            "UPDATE users SET username=?, full_name=?, last_seen=CURRENT_TIMESTAMP WHERE chat_id=?",
            (username, full_name, chat_id),
        )
        conn.commit()
    cur.execute("INSERT INTO activity (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()
    return row


def get_user_data(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT chat_id, username, full_name, exp, coins, rank, vip_until, streak "
        "FROM users WHERE chat_id = ?",
        (chat_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    exp = row[3]
    rank_info = calculate_rank(exp)
    return {
        "chat_id": row[0], "username": row[1] or "",
        "full_name": row[2] or "", "exp": exp,
        "coins": row[4], "rank": rank_info["rank"],
        "rank_title": rank_info["title"],
        "next_rank": rank_info["next_rank"],
        "next_rank_exp": rank_info["next_rank_exp"],
        "needed_exp": rank_info["needed_exp"],
        "progress_pct": rank_info["progress_pct"],
        "is_vip": is_vip(chat_id),
        "vip_until": row[6], "streak": row[7],
    }


def is_vip(chat_id: int) -> bool:
    if is_admin(chat_id):
        return True
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT vip_until IS NOT NULL AND vip_until > datetime('now') FROM users WHERE chat_id = ?",
        (chat_id,),
    )
    row = cur.fetchone()
    conn.close()
    return bool(row[0]) if row else False


def set_vip(chat_id: int, days: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET vip_until = datetime('now', ?) WHERE chat_id = ?",
        (f"+{int(days)} days", chat_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def revoke_vip(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET vip_until = NULL WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def add_exp(chat_id: int, amount: int):
    multiplier = 2 if is_vip(chat_id) else 1
    final_amount = amount * multiplier
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET exp = exp + ? WHERE chat_id = ?", (final_amount, chat_id))
    cur.execute("SELECT exp FROM users WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    if row:
        new_rank = calculate_rank(row[0])["rank"]
        cur.execute("UPDATE users SET rank = ? WHERE chat_id = ?", (new_rank, chat_id))
    conn.commit()
    conn.close()
    return final_amount


def add_coins(chat_id: int, amount: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET coins = coins + ? WHERE chat_id = ?", (amount, chat_id))
    conn.commit()
    conn.close()


def find_user_by_query(query: str):
    q = query.strip().lstrip("@")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if q.isdigit():
        cur.execute("SELECT chat_id FROM users WHERE chat_id = ?", (int(q),))
    else:
        cur.execute("SELECT chat_id FROM users WHERE username = ? COLLATE NOCASE", (q,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_stats():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT chat_id) FROM activity WHERE created_at >= date('now')")
    active_today = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE vip_until > datetime('now')")
    vip_count = cur.fetchone()[0]
    cur.execute("SELECT SUM(coins), SUM(exp) FROM users")
    sums = cur.fetchone()
    total_coins = sums[0] or 0
    total_exp = sums[1] or 0
    cur.execute("SELECT COUNT(*) FROM backgrounds")
    total_bgs = cur.fetchone()[0]
    conn.close()
    return {
        "total_users": total_users,
        "active_today": active_today,
        "vip_count": vip_count,
        "total_coins": total_coins,
        "total_exp": total_exp,
        "total_backgrounds": total_bgs,
    }


# ===== FON AMALIYOTLARI =====

def add_background(name: str, filename: str, price: int, code: str = None, file_id: str = None, storage_chat_id: int = None, storage_message_id: int = None) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO backgrounds (name, filename, price, code, file_id, storage_chat_id, storage_message_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (name, filename, price, code, file_id, storage_chat_id, storage_message_id),
    )
    bg_id = cur.lastrowid
    conn.commit()
    conn.close()
    return bg_id


def get_all_backgrounds():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, name, filename, price, code, file_id, storage_chat_id, storage_message_id FROM backgrounds ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r[0], "name": r[1],
            "filename": r[2], "price": r[3],
            "code": r[4] or f"{r[0]}bg",
            "file_id": r[5],
            "storage_chat_id": r[6],
            "storage_message_id": r[7],
            "url": f"/images/{r[2]}",
        }
        for r in rows
    ]


def get_background_by_id(bg_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, name, filename, price, code, file_id, storage_chat_id, storage_message_id FROM backgrounds WHERE id = ?", (bg_id,))
    r = cur.fetchone()
    conn.close()
    if not r:
        return None
    return {
        "id": r[0], "name": r[1], "filename": r[2], "price": r[3],
        "code": r[4] or f"{r[0]}bg", "file_id": r[5], "storage_chat_id": r[6], "storage_message_id": r[7],
        "url": f"/images/{r[2]}"
    }


def get_background_by_code(code: str):
    if not code:
        return None
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, filename, price, code, file_id, storage_chat_id, storage_message_id FROM backgrounds WHERE LOWER(code) = LOWER(?)",
        (code.strip(),),
    )
    r = cur.fetchone()
    conn.close()
    if not r:
        return None
    return {
        "id": r[0], "name": r[1], "filename": r[2], "price": r[3],
        "code": r[4] or f"{r[0]}bg", "file_id": r[5], "storage_chat_id": r[6], "storage_message_id": r[7],
        "url": f"/images/{r[2]}"
    }


def is_bg_code_taken(code: str) -> bool:
    if not code:
        return False
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM backgrounds WHERE LOWER(code) = LOWER(?)", (code.strip(),))
    row = cur.fetchone()
    conn.close()
    return bool(row)


def delete_background(bg_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT filename FROM backgrounds WHERE id = ?", (bg_id,))
    row = cur.fetchone()
    if row:
        fpath = os.path.join(IMAGES_DIR, row[0])
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
            except Exception:
                pass
    cur.execute("DELETE FROM user_backgrounds WHERE bg_id = ?", (bg_id,))
    cur.execute("DELETE FROM backgrounds WHERE id = ?", (bg_id,))
    conn.commit()
    conn.close()


def get_user_owned_backgrounds(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT b.id, b.name, b.filename, b.price, b.code
        FROM user_backgrounds ub
        JOIN backgrounds b ON ub.bg_id = b.id
        WHERE ub.chat_id = ?
        ORDER BY ub.bought_at DESC
    """, (chat_id,))
    rows = cur.fetchall()
    conn.close()
    return [
        {"id": r[0], "name": r[1], "filename": r[2],
         "price": r[3], "code": r[4] or f"{r[0]}bg", "url": f"/images/{r[2]}"}
        for r in rows
    ]


def buy_background(chat_id: int, bg_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        "SELECT id FROM user_backgrounds WHERE chat_id = ? AND bg_id = ?",
        (chat_id, bg_id),
    )
    if cur.fetchone():
        conn.close()
        return {"ok": False, "error": "already_owned"}

    cur.execute("SELECT price FROM backgrounds WHERE id = ?", (bg_id,))
    bg_row = cur.fetchone()
    if not bg_row:
        conn.close()
        return {"ok": False, "error": "not_found"}

    price = bg_row[0]

    cur.execute("SELECT coins FROM users WHERE chat_id = ?", (chat_id,))
    user_row = cur.fetchone()
    if not user_row or user_row[0] < price:
        conn.close()
        return {"ok": False, "error": "not_enough_coins"}

    new_coins = user_row[0] - price
    cur.execute("UPDATE users SET coins = ? WHERE chat_id = ?", (new_coins, chat_id))
    cur.execute(
        "INSERT OR IGNORE INTO user_backgrounds (chat_id, bg_id) VALUES (?, ?)",
        (chat_id, bg_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "error": None, "new_coins": new_coins}


def restore_image_from_telegram(filename: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT file_id FROM backgrounds WHERE filename = ?", (filename,))
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return False
    file_id = row[0]
    try:
        req_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
        with urllib.request.urlopen(req_url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data.get("ok"):
                fpath = data["result"]["file_path"]
                dl_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fpath}"
                save_path = os.path.join(IMAGES_DIR, filename)
                urllib.request.urlretrieve(dl_url, save_path)
                logger.info(f"Rasm Telegram serveridan qayta tiklandi: {filename}")
                return True
    except Exception as e:
        logger.warning(f"Rasm qayta tiklanmadi ({filename}): {e}")
    return False


# ===== FANLAR VA TEST SAVOLLARI AMALIYOTLARI =====

def get_all_subjects():
    """Barcha fanlar va ulardagi savollar sonini qaytaradi."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT s.id, s.name, s.slug, s.icon, COUNT(q.id) as q_count
        FROM subjects s
        LEFT JOIN questions q ON s.id = q.subject_id
        GROUP BY s.id
        ORDER BY s.id ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "name": r[1],
            "slug": r[2],
            "icon": r[3],
            "question_count": r[4],
        }
        for r in rows
    ]


def get_subject_by_id(subj_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, name, slug, icon FROM subjects WHERE id = ?", (subj_id,))
    r = cur.fetchone()
    conn.close()
    if not r:
        return None
    return {"id": r[0], "name": r[1], "slug": r[2], "icon": r[3]}


def add_question(subject_id: int, question: str, opt_a: str, opt_b: str, opt_c: str, opt_d: str, correct: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO questions (subject_id, question, option_a, option_b, option_c, option_d, correct_option)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (subject_id, question, opt_a, opt_b, opt_c, opt_d, correct.upper()))
    qid = cur.lastrowid
    conn.commit()
    conn.close()
    return qid


def get_subject_question_count(subject_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM questions WHERE subject_id = ?", (subject_id,))
    cnt = cur.fetchone()[0]
    conn.close()
    return cnt


def get_random_question(subject_id: int, difficulty_level: str = None):
    """Fan va qiyinlik darajasiga mos random savol qaytaradi."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    r = None
    if difficulty_level:
        cur.execute("""
            SELECT id, subject_id, question, option_a, option_b, option_c, option_d, correct_option, difficulty_level
            FROM questions WHERE subject_id = ? AND difficulty_level = ?
            ORDER BY RANDOM() LIMIT 1
        """, (subject_id, difficulty_level))
        r = cur.fetchone()

    # Agar ayni shu qiyinlikda topilmasa, fanning istalgan savolini olish
    if not r:
        cur.execute("""
            SELECT id, subject_id, question, option_a, option_b, option_c, option_d, correct_option, difficulty_level
            FROM questions WHERE subject_id = ?
            ORDER BY RANDOM() LIMIT 1
        """, (subject_id,))
        r = cur.fetchone()

    conn.close()
    if not r:
        return None
    return {
        "id": r[0], "subject_id": r[1], "question": r[2],
        "option_a": r[3], "option_b": r[4], "option_c": r[5], "option_d": r[6],
        "correct_option": r[7], "difficulty_level": r[8] or "F",
    }


def update_question_difficulty(question_id: int, new_level: str):
    """Savol qiyinlik darajasini yangilaydi."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE questions SET difficulty_level = ? WHERE id = ?", (new_level, question_id))
    conn.commit()
    conn.close()


RANK_ORDER = ['F', 'E', 'D', 'C', 'B', 'A', 'S', 'SS', 'SSS']

def shift_difficulty(current_level: str, correct: bool) -> str:
    """To'g'ri javob: 1 daraja pastga (osonroq). Noto'g'ri: 1 daraja tepaga (qiyinroq)."""
    idx = RANK_ORDER.index(current_level) if current_level in RANK_ORDER else 0
    if correct:
        idx = max(0, idx - 1)
    else:
        idx = min(len(RANK_ORDER) - 1, idx + 1)
    return RANK_ORDER[idx]


def get_rank_index(rank: str) -> int:
    """Rank harfini raqamli indeksga aylantiradi."""
    return RANK_ORDER.index(rank) if rank in RANK_ORDER else 0


# Xona uchun EXP va Coin sovg'alari (daraja bo'yicha)
ROOM_REWARDS = {
    'F':   {'exp_correct': 10,  'coins_correct': 2,   'exp_wrong': -5},
    'E':   {'exp_correct': 20,  'coins_correct': 4,   'exp_wrong': -10},
    'D':   {'exp_correct': 40,  'coins_correct': 8,   'exp_wrong': -20},
    'C':   {'exp_correct': 80,  'coins_correct': 15,  'exp_wrong': -40},
    'B':   {'exp_correct': 150, 'coins_correct': 30,  'exp_wrong': -75},
    'A':   {'exp_correct': 300, 'coins_correct': 60,  'exp_wrong': -150},
    'S':   {'exp_correct': 500, 'coins_correct': 100, 'exp_wrong': -250},
    'SS':  {'exp_correct': 800, 'coins_correct': 160, 'exp_wrong': -400},
    'SSS': {'exp_correct': 1500,'coins_correct': 300, 'exp_wrong': -750},
}


def get_today_gmt5() -> str:
    """GMT+5 vaqti bo'yicha bugungi sanani YYYY-MM-DD ko'rinishida qaytaradi."""
    now_gmt5 = datetime.utcnow() + timedelta(hours=5)
    return now_gmt5.strftime("%Y-%m-%d")


def generate_daily_missions():
    """Har kuni GMT+5 bo'yicha yangi missiyalar yaratadi."""
    today = get_today_gmt5()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM daily_missions WHERE date = ?", (today,))
    if cur.fetchone()[0] >= 2:
        conn.close()
        return
    cur.execute("DELETE FROM daily_missions WHERE date < date('now', '-5 days')")
    # Missiya 1: 5 ta savol yechish
    cur.execute(
        "INSERT INTO daily_missions (name, description, reward_exp, mission_type, target, date) VALUES (?,?,?,?,?,?)",
        ("5 ta savol yeching", "Tasodifiy yoki oddiy xonada 5 ta savolga to'g'ri javob bering", 100, "questions", 5, today)
    )
    # Missiya 2: 15 daqiqa vaqt sarflash
    cur.execute(
        "INSERT INTO daily_missions (name, description, reward_exp, mission_type, target, date) VALUES (?,?,?,?,?,?)",
        ("Tizimda 15 daqiqa vaqt sarflang", "Ilovada 15 daqiqa davomida faol bo'ling", 80, "time", 15, today)
    )
    conn.commit()
    conn.close()
    logger.info(f"Kunlik missiyalar yaratildi: {today}")


def process_room_answer(chat_id: int, question_id: int, selected_option: str, room_level: str) -> dict:
    """Xonada javob berilganda: to'g'ri/noto'g'ri tekshirib, EXP/Coin beradi va qiyinlik yangilaydi."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT correct_option, difficulty_level, question FROM questions WHERE id = ?", (question_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "question_not_found"}

    correct_opt = row[0].upper()
    diff_level = row[1] or 'F'
    is_correct = (selected_option.upper() == correct_opt)

    rewards = ROOM_REWARDS.get(room_level, ROOM_REWARDS['F'])

    exp_change = 0
    coin_change = 0

    if is_correct:
        exp_change = rewards['exp_correct']
        coin_change = rewards['coins_correct']
        # Tasodifiy xonada EXP 2x, hech qanday tanga berilmaydi
        if room_level == 'Random':
            exp_change = exp_change * 2
        add_exp(chat_id, exp_change)
        # Xonalardan tanga berilmaydi
    else:
        exp_change = rewards['exp_wrong']
        # EXP ni kamaytirish (0 dan pastga tushmasligi kerak)
        conn2 = sqlite3.connect(DB_PATH)
        cur2 = conn2.cursor()
        cur2.execute("UPDATE users SET exp = MAX(0, exp + ?) WHERE chat_id = ?", (exp_change, chat_id))
        cur2.execute("SELECT exp FROM users WHERE chat_id = ?", (chat_id,))
        r2 = cur2.fetchone()
        if r2:
            new_rank = calculate_rank(r2[0])["rank"]
            cur2.execute("UPDATE users SET rank = ? WHERE chat_id = ?", (new_rank, chat_id))
        conn2.commit()
        conn2.close()

    # Savol qiyinligini yangilash
    new_difficulty = shift_difficulty(diff_level, is_correct)
    update_question_difficulty(question_id, new_difficulty)

    # Yangilangan profil ma'lumotlari
    user_data = get_user_data(chat_id)

    return {
        "ok": True,
        "is_correct": is_correct,
        "correct_option": correct_opt,
        "exp_change": exp_change,
        "coin_change": coin_change,
        "new_difficulty": new_difficulty,
        "user": user_data,
    }


# ========================================================
# 3. ZAXIRALASH VA TIKLASH (RENDER EPHEMERAL DISK UCHUN)
# ========================================================

async def backup_database(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """
    sarn.db faylini (SQLite bazani) Baza guruhiga yuklaydi va PIN qiladi.
    Render bepul tarifida doimiy xotira bo'lmagani sababli, qayta deploy qilinganda
    baza shu PIN qilingan nusxadan tiklanadi.
    """
    backup_chat_id = get_storage_chat_id() or (ADMIN_IDS[0] if ADMIN_IDS else None)
    if not backup_chat_id:
        logger.warning("Bazani zaxiralash uchun chat topilmadi (Baza guruhi sozlanmagan).")
        return {"ok": False, "reason": "no_chat", "pinned": False, "users_count": 0, "chat_id": None, "error": None}

    if not os.path.exists(DB_PATH):
        return {"ok": False, "reason": "no_file", "pinned": False, "users_count": 0, "chat_id": backup_chat_id, "error": None}

    previous_backup_message_id = get_setting("last_backup_message_id")
    users_count = _count_users_in_db()

    # Xavfsizlik: agar baza bo'sh bo'lsa va oldingi zaxira bo'lsa, o'tkazib yuborish
    if users_count == 0 and previous_backup_message_id:
        logger.warning("Mahalliy baza bo'sh, yaxshi zaxirani o'chirmaslik uchun zaxiralash o'tkazib yuborildi.")
        return {"ok": False, "reason": "empty_skip", "pinned": False, "users_count": 0, "chat_id": backup_chat_id, "error": None}

    sent = None
    last_error = None
    attempts_left = 3
    attempt = 0
    while attempt < attempts_left:
        try:
            with open(DB_PATH, "rb") as f:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                sent = await context.bot.send_document(
                    chat_id=backup_chat_id,
                    document=f,
                    filename="sarn_backup.db",
                    caption=f"🗄 <b>SARN Avtomatik Zaxira (Backup)</b>\n"
                            f"📅 Vaqt: <code>{now_str}</code>\n"
                            f"👥 Foydalanuvchilar: <b>{users_count} ta</b>\n\n"
                            f"⚠️ <i>Ushbu xabarni O'CHIRMANG! Render qayta deploy bo'lganda baza shu yerdan tiklanadi.</i>",
                    parse_mode="HTML",
                )
            break
        except ChatMigrated as e:
            backup_chat_id = e.new_chat_id
            set_setting("storage_chat_id", str(backup_chat_id))
            last_error = e
            if attempts_left < 5:
                attempts_left += 1
        except Exception as e:
            last_error = e
            await asyncio.sleep(2)
        attempt += 1

    if sent is None:
        logger.error("Bazani zaxiralashda xatolik: %s", last_error)
        return {
            "ok": False, "reason": "upload_failed", "pinned": False,
            "users_count": users_count, "chat_id": backup_chat_id,
            "error": str(last_error) if last_error else None,
        }

    pinned_ok = False
    try:
        await context.bot.pin_chat_message(
            chat_id=backup_chat_id, message_id=sent.message_id, disable_notification=True
        )
        pinned_ok = True
    except Exception as e:
        logger.warning("Zaxira xabarini PIN qilib bo'lmadi (bot admin huquqida Pin messages yo'q bo'lishi mumkin): %s", e)

    set_setting("last_backup_message_id", str(sent.message_id))
    set_setting("last_backup_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # Eski zaxirani o'chirish (guruh to'lib ketmasligi uchun doim faqat 1 ta eng so'nggi zaxira qoladi)
    if previous_backup_message_id:
        try:
            await context.bot.delete_message(
                chat_id=backup_chat_id, message_id=int(previous_backup_message_id)
            )
        except Exception:
            pass

    return {
        "ok": True, "reason": "success", "pinned": pinned_ok,
        "users_count": users_count, "chat_id": backup_chat_id, "error": None,
    }


async def backup_database_job(context: ContextTypes.DEFAULT_TYPE):
    """Har 1 soatda avtomatik ishga tushadigan fon vazifasi."""
    logger.info("Avtomatik soatlik zaxiralash boshlandi...")
    res = await backup_database(context)
    if res.get("ok"):
        logger.info("✅ Soatlik zaxiralash muvaffaqiyatli yakunlandi.")
    else:
        logger.warning("⚠️ Soatlik zaxiralash bajarilmadi: %s", res.get("reason"))


async def _restore_from_pinned_backup(bot) -> dict:
    """Baza guruhida PIN qilingan zaxira faylni yuklab olib, o'rniga qo'yadi."""
    backup_chat_id = get_storage_chat_id() or (ADMIN_IDS[0] if ADMIN_IDS else None)
    if not backup_chat_id:
        return {"ok": False, "reason": "no_chat", "users_count": 0}

    try:
        chat = await bot.get_chat(backup_chat_id)
        pinned = chat.pinned_message
        if pinned is None or pinned.document is None:
            return {"ok": False, "reason": "no_pinned", "users_count": 0}
        file = await bot.get_file(pinned.document.file_id)
        await file.download_to_drive(DB_PATH)
    except Exception as e:
        logger.exception("Bazani zaxiradan tiklashda xatolik: %s", e)
        return {"ok": False, "reason": "download_failed", "users_count": 0, "error": str(e)}

    init_db()
    users_count = _count_users_in_db()
    logger.info("✅ Baza muvaffaqiyatli zaxiradan tiklandi (%s), foydalanuvchilar: %s.", DB_PATH, users_count)
    return {"ok": True, "reason": "success", "users_count": users_count}


async def restore_database_if_needed(bot):
    """Bot ishga tushganda chaqiriladi. Mahalliy baza bo'sh bo'lsa zaxiradan tiklaydi."""
    if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0:
        if _count_users_in_db() > 0:
            logger.info("Mahalliy baza mavjud (%s ta user), tiklash shart emas.", _count_users_in_db())
            return
    logger.info("Mahalliy baza topilmadi yoki bo'sh. Telegram zaxirasidan tiklashga urinilmoqda...")
    await _restore_from_pinned_backup(bot)


async def handle_pinned_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram'ning '[Bot] xabarni pin qildi' degan keraksiz bildirishnomasini avtomatik tozalaydi."""
    message = update.message
    if message is None or message.pinned_message is None:
        return
    try:
        await context.bot.delete_message(chat_id=message.chat_id, message_id=message.message_id)
    except Exception:
        pass


class _BotContextWrapper:
    def __init__(self, bot):
        self.bot = bot


async def hourly_backup_loop(bot):
    """JobQueue mavjud bo'lmaganda ham 1 soatlik zaxiralashni kafolatlaydigan asyncio sikli."""
    await asyncio.sleep(60)  # Ishga tushgandan 1 daqiqa o'tgach birinchi zaxira
    while True:
        try:
            await backup_database_job(_BotContextWrapper(bot))
        except Exception as e:
            logger.exception("Soatlik zaxiralash siklida xatolik: %s", e)
        await asyncio.sleep(3600)  # Har 1 soatda takrorlanadi


# ==========================================
# 4. O'RNATILGAN MINI APP VEB SERVERI
# ==========================================

class MiniAppHandler(SimpleHTTPRequestHandler):
    """Frontend fayllari, API va Render Health Check xizmati."""

    def translate_path(self, path):
        clean_path = urlparse(path).path.lstrip("/")
        if not clean_path or clean_path == "index.html":
            return os.path.join(WEB_DIR, "index.html")
        if clean_path.startswith("images/"):
            full_path = os.path.join(WEB_DIR, clean_path)
            if not os.path.exists(full_path):
                filename = os.path.basename(clean_path)
                restore_image_from_telegram(filename)
            return full_path
        return os.path.join(WEB_DIR, clean_path)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        # Health-check (Render va monitoring uchun)
        if parsed.path in ("/health", "/ping"):
            self._json_response(200, {"status": "ok", "app": "SARN", "time": datetime.now().isoformat()})
            return

        # === API: Foydalanuvchi profili ===
        if parsed.path == "/api/profile":
            chat_id = params.get("chat_id", [None])[0]
            if chat_id and chat_id.lstrip("-").isdigit():
                data = get_user_data(int(chat_id))
                if data:
                    self._json_response(200, data)
                    return
            self._json_response(404, {"error": "not_found"})
            return

        # === API: Barcha fonlar ===
        if parsed.path == "/api/backgrounds":
            bgs = get_all_backgrounds()
            self._json_response(200, bgs)
            return

        # === API: Mening xarid qilgan fonlarim ===
        if parsed.path == "/api/my_backgrounds":
            chat_id = params.get("chat_id", [None])[0]
            if chat_id and chat_id.lstrip("-").isdigit():
                owned = get_user_owned_backgrounds(int(chat_id))
                self._json_response(200, owned)
                return
            self._json_response(400, {"error": "chat_id required"})
            return

        # === API: Statistika ===
        if parsed.path == "/api/stats":
            self._json_response(200, get_stats())
            return

        # === API: Fanlar va savollar soni ===
        if parsed.path == "/api/subjects":
            subjs = get_all_subjects()
            self._json_response(200, subjs)
            return

        # === API: Xona uchun random savol ===
        if parsed.path == "/api/room_question":
            subject_id = params.get("subject_id", [None])[0]
            room_level = params.get("room_level", [None])[0]
            # Tasodifiy xona: agar subject_id 'random' bo'lsa yoki ko'rsatilmagan bo'lsa, istalgan fandan savol
            if room_level == 'Random' or subject_id in ('random', None, ''):
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute("""
                    SELECT q.id, q.subject_id, q.question, q.option_a, q.option_b, q.option_c, q.option_d, q.correct_option, q.difficulty_level, s.name
                    FROM questions q
                    LEFT JOIN subjects s ON q.subject_id = s.id
                    ORDER BY RANDOM() LIMIT 1
                """)
                row = cur.fetchone()
                conn.close()
                if row:
                    self._json_response(200, {
                        "id": row[0], "subject_id": row[1], "question": row[2],
                        "option_a": row[3], "option_b": row[4], "option_c": row[5], "option_d": row[6],
                        "correct_option": row[7], "difficulty_level": row[8] or "F",
                        "subject_name": row[9] or "Umumiy"
                    })
                else:
                    self._json_response(404, {"error": "no_questions", "message": "Bazada hali savollar yo'q"})
                return

            if subject_id and subject_id.isdigit():
                q = get_random_question(int(subject_id), room_level)
                if q:
                    self._json_response(200, q)
                else:
                    self._json_response(404, {"error": "no_questions", "message": "Bu fanda hali savollar yo'q"})
                return
            self._json_response(400, {"error": "subject_id required"})
            return

        # === API: Kunlik missiyalar ro'yxati ===
        if parsed.path == "/api/daily_missions":
            chat_id = params.get("chat_id", [None])[0]
            if not chat_id or not chat_id.lstrip("-").isdigit():
                self._json_response(400, {"error": "chat_id required"})
                return
            generate_daily_missions()
            cid = int(chat_id)
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            today = get_today_gmt5()
            cur.execute("SELECT id, name, description, reward_exp, mission_type, target FROM daily_missions WHERE date = ?", (today,))
            rows = cur.fetchall()
            missions = []
            for r in rows:
                mid = r[0]
                cur.execute("SELECT progress, completed, session_start FROM user_daily_progress WHERE chat_id=? AND mission_id=?", (cid, mid))
                prog = cur.fetchone()
                progress = prog[0] if prog else 0
                completed = prog[1] if prog else 0
                session_start = prog[2] if prog else None
                missions.append({
                    "id": mid, "name": r[1], "description": r[2],
                    "reward_exp": r[3], "type": r[4], "target": r[5],
                    "progress": progress, "completed": completed, "session_start": session_start
                })
            conn.close()
            now_gmt5 = datetime.datetime.utcnow() + datetime.timedelta(hours=5)
            tomorrow = (now_gmt5 + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            diff = tomorrow - now_gmt5
            hours_left, rem = divmod(int(diff.total_seconds()), 3600)
            mins_left = rem // 60
            next_reset_str = f"{hours_left}s {mins_left}d"
            self._json_response(200, {"missions": missions, "next_reset": next_reset_str})
            return

        # === API: Missiya (vaqt) boshlanishi ===
        if parsed.path == "/api/mission_start":
            chat_id = params.get("chat_id", [None])[0]
            mission_id = params.get("mission_id", [None])[0]
            if not chat_id or not mission_id:
                self._json_response(400, {"error": "missing"})
                return
            now_iso = (datetime.datetime.utcnow() + datetime.timedelta(hours=5)).isoformat()
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "INSERT OR IGNORE INTO user_daily_progress (chat_id, mission_id, progress, completed, session_start) VALUES (?,?,0,0,?)",
                (int(chat_id), int(mission_id), now_iso)
            )
            cur.execute(
                "UPDATE user_daily_progress SET session_start=? WHERE chat_id=? AND mission_id=? AND session_start IS NULL",
                (now_iso, int(chat_id), int(mission_id))
            )
            conn.commit()
            cur.execute("SELECT session_start FROM user_daily_progress WHERE chat_id=? AND mission_id=?", (int(chat_id), int(mission_id)))
            row = cur.fetchone()
            conn.close()
            self._json_response(200, {"ok": True, "session_start": row[0] if row else now_iso})
            return

        # === API: Vaqt missiyasi holatini tekshirish ===
        if parsed.path == "/api/mission_check_time":
            chat_id = params.get("chat_id", [None])[0]
            mission_id = params.get("mission_id", [None])[0]
            if not chat_id or not mission_id:
                self._json_response(400, {"error": "missing"})
                return
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT progress, completed, session_start FROM user_daily_progress WHERE chat_id=? AND mission_id=?", (int(chat_id), int(mission_id)))
            row = cur.fetchone()
            if not row or not row[2]:
                conn.close()
                self._json_response(200, {"elapsed": 0, "completed": False})
                return
            session_start = datetime.datetime.fromisoformat(row[2])
            now_gmt5 = datetime.datetime.utcnow() + datetime.timedelta(hours=5)
            elapsed_mins = int((now_gmt5 - session_start).total_seconds() / 60)
            completed = row[1]
            if elapsed_mins >= 15 and not completed:
                cur.execute("SELECT reward_exp FROM daily_missions WHERE id=?", (int(mission_id),))
                mrow = cur.fetchone()
                if mrow:
                    add_exp(int(chat_id), mrow[0] * 2)
                    add_coins(int(chat_id), 10)
                cur.execute("UPDATE user_daily_progress SET completed=1, progress=15 WHERE chat_id=? AND mission_id=?", (int(chat_id), int(mission_id)))
                conn.commit()
                completed = 1
            conn.close()
            self._json_response(200, {"elapsed": min(elapsed_mins, 15), "completed": bool(completed)})
            return

        # Statik fayllar (index.html, /images/...)
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)

        # === API: Fon xaridi ===
        if parsed.path == "/api/buy_background":
            content_len = int(self.headers.get("Content-Length", 0))
            body = {}
            if content_len:
                try:
                    body = json.loads(self.rfile.read(content_len).decode("utf-8"))
                except Exception:
                    pass

            chat_id = body.get("chat_id")
            bg_id = body.get("bg_id")

            if not chat_id or not bg_id:
                self._json_response(400, {"ok": False, "error": "bad_request"})
                return

            result = buy_background(int(chat_id), int(bg_id))
            self._json_response(200, result)
            return

        # === API: Xonada javob berish ===
        if parsed.path == "/api/room_answer":
            content_len = int(self.headers.get("Content-Length", 0))
            body = {}
            if content_len:
                try:
                    body = json.loads(self.rfile.read(content_len).decode("utf-8"))
                except Exception:
                    pass
            chat_id = body.get("chat_id")
            question_id = body.get("question_id")
            selected_option = body.get("selected_option")
            room_level = body.get("room_level", "F")
            if not all([chat_id, question_id, selected_option]):
                self._json_response(400, {"ok": False, "error": "missing_fields"})
                return
            result = process_room_answer(int(chat_id), int(question_id), selected_option, room_level)
            self._json_response(200, result)
            return

        # === API: Savol missiyasi progressini yangilash ===
        if parsed.path == "/api/mission_answer_progress":
            content_len = int(self.headers.get("Content-Length", 0))
            body = {}
            if content_len:
                try:
                    body = json.loads(self.rfile.read(content_len).decode("utf-8"))
                except Exception:
                    pass
            chat_id = body.get("chat_id")
            mission_id = body.get("mission_id")
            if not chat_id or not mission_id:
                self._json_response(400, {"ok": False, "error": "missing"})
                return
            today = get_today_gmt5()
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT target, reward_exp FROM daily_missions WHERE id=? AND date=?", (int(mission_id), today))
            mrow = cur.fetchone()
            if not mrow:
                conn.close()
                self._json_response(400, {"ok": False, "error": "mission_not_found"})
                return
            target, reward_exp = mrow
            cur.execute(
                "INSERT OR IGNORE INTO user_daily_progress (chat_id, mission_id, progress, completed) VALUES (?,?,0,0)",
                (int(chat_id), int(mission_id))
            )
            cur.execute("SELECT progress, completed FROM user_daily_progress WHERE chat_id=? AND mission_id=?", (int(chat_id), int(mission_id)))
            prow = cur.fetchone()
            progress, completed = prow
            if completed:
                conn.close()
                self._json_response(200, {"ok": True, "progress": progress, "target": target, "completed": True, "already": True})
                return
            progress += 1
            cur.execute("UPDATE user_daily_progress SET progress=? WHERE chat_id=? AND mission_id=?", (progress, int(chat_id), int(mission_id)))
            newly_completed = False
            if progress >= target:
                cur.execute("UPDATE user_daily_progress SET completed=1 WHERE chat_id=? AND mission_id=?", (int(chat_id), int(mission_id)))
                add_exp(int(chat_id), reward_exp * 2)
                add_coins(int(chat_id), 10)
                newly_completed = True
            conn.commit()
            conn.close()
            self._json_response(200, {"ok": True, "progress": progress, "target": target, "completed": newly_completed, "already": False})
            return

        self._json_response(404, {"error": "not_found"})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json_response(self, code: int, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass


def start_web_server(port: int):
    server_address = ("0.0.0.0", port)
    httpd = HTTPServer(server_address, MiniAppHandler)
    logger.info(f"Mini App Veb-serveri (Health Check) http://0.0.0.0:{port} manzilida ishga tushdi.")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# ==========================================
# 5. BOT KLAVIATURALARI
# ==========================================

def get_webapp_url() -> str:
    url = get_setting("webapp_url", "")
    if url:
        return url
    return os.environ.get("WEBAPP_URL", f"http://localhost:{WEB_PORT}")


def build_user_main_keyboard() -> InlineKeyboardMarkup:
    webapp_url = get_webapp_url()
    is_https = webapp_url.startswith("https://")
    buttons = [
        [
            InlineKeyboardButton(
                "🚀 SARN Mini App'ni Ochish",
                web_app=WebAppInfo(url=webapp_url) if is_https else None,
                callback_data=None if is_https else "open_app_alert",
            )
        ],
        [
            InlineKeyboardButton("👤 Mening Profilim", callback_data="u_profile"),
            InlineKeyboardButton("⭐ VIP Obuna", callback_data="u_vip"),
        ],
        [
            InlineKeyboardButton("📚 Loyiha Haqida", callback_data="u_about"),
            InlineKeyboardButton("🏆 Darajalar", callback_data="u_ranks"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def build_admin_main_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("📊 Statistika", callback_data="adm_stats"),
            InlineKeyboardButton("⭐ VIP Berish", callback_data="adm_vip"),
        ],
        [
            InlineKeyboardButton("🪙 Coin/EXP Berish", callback_data="adm_bonus"),
            InlineKeyboardButton("📢 Xabar Tarqatish", callback_data="adm_broadcast"),
        ],
        [
            InlineKeyboardButton("📝 Test Qo'shish", callback_data="adm_add_test"),
            InlineKeyboardButton("📚 Fanlar & Testlar", callback_data="adm_list_subjects"),
        ],
        [
            InlineKeyboardButton("🖼 Fon Qo'shish", callback_data="adm_add_bg"),
            InlineKeyboardButton("🗑 Fonlarni Ko'rish", callback_data="adm_list_bg"),
        ],
        [
            InlineKeyboardButton("📁 Baza Guruhi", callback_data="adm_storage_group"),
            InlineKeyboardButton("💾 Zaxira (Backup)", callback_data="adm_backup_menu"),
        ],
        [
            InlineKeyboardButton("🌐 WebApp URL", callback_data="adm_set_url"),
            InlineKeyboardButton("🔄 Yangilash", callback_data="adm_refresh"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


# ==========================================
# 6. BOT BUYRUQLARI VA ADMIN PANEL
# ==========================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    get_or_create_user(chat_id, user.username, user.full_name)
    caption = (
        f"👋 Assalomu alaykum, <b>{user.first_name}</b>!\n\n"
        f"📖 <b>SARN (Solo + Learn)</b> ta'lim platformasiga xush kelibsiz!\n"
        f"<i>«Yolg'iz istagan fanni o'rganing»</i>\n\n"
        f"🎮 Darajangizni <b>F</b> dan <b>SSS</b> gacha ko'taring, "
        f"tangalar yig'ing va profilingizni bezating.\n\n"
        f"👇 Quyidagi tugma orqali o'yin maydoniga kiring:"
    )
    await update.message.reply_html(caption, reply_markup=build_user_main_keyboard())


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    data = get_user_data(chat_id)
    if not data:
        get_or_create_user(chat_id, update.effective_user.username, update.effective_user.full_name)
        data = get_user_data(chat_id)
    vip_str = "⭐ Faol VIP" if data["is_vip"] else "Standart"
    text = (
        f"👤 <b>Mening Profilim:</b>\n\n"
        f"🔥 Daraja: <b>{data['rank']}</b> ({data['rank_title']})\n"
        f"⚡ EXP: <b>{data['exp']} / {data['next_rank_exp']}</b>\n"
        f"🪙 Tangalar: <b>{data['coins']} Coin</b>\n"
        f"👑 Status: <b>{vip_str}</b>\n\n"
        f"<i>Keyingi {data['next_rank']} darajaga {data['needed_exp']} EXP qoldi!</i>"
    )
    await update.message.reply_html(text, reply_markup=build_user_main_keyboard())


async def chatid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat = update.effective_chat
    if not is_admin(user_id):
        return

    text = f"🆔 Bu chatning ID raqami: <code>{chat.id}</code>\nTuri: <b>{chat.type}</b>\n\n"
    if chat.type in ("group", "supergroup", "channel"):
        set_setting("storage_chat_id", str(chat.id))
        text += (
            f"✅ <b>Baza Guruhi Sozlandi!</b>\n"
            f"Guruh: <b>{chat.title or chat.id}</b> (ID: <code>{chat.id}</code>)\n\n"
            f"Endi bot yuklanadigan barcha fon rasmlarini hamda **har 1 soatda butun bazani (sarn.db)** "
            f"avtomatik ushbu guruhga yuklab, PIN qilib qo'yadi.\n\n"
            f"⚠️ <i>Muhim: Botga ushbu guruh sozlamalarida 'Pin Messages' huquqini berishni unutmang.</i>"
        )
    else:
        current = get_storage_chat_id()
        text += (
            f"Hozirgi Baza Guruhi: <code>{current or 'Sozlanmagan'}</code>\n\n"
            f"Guruhni Baza Guruhi qilish uchun: botni o'sha guruhga admin qilib qo'shing "
            f"va guruh ichida <code>/chatid</code> yozing."
        )
    await update.message.reply_html(text)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Siz admin emassiz.")
        return
    stats = get_stats()
    webapp_url = get_webapp_url()
    storage_id = get_storage_chat_id()
    storage_text = f"<code>{storage_id}</code>" if storage_id else "<i>Sozlanmagan</i>"
    last_bk = get_setting("last_backup_time", "Hali qilinmagan")

    text = (
        f"👑 <b>SARN Admin Paneli</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{stats['total_users']}</b> ta\n"
        f"⚡ Bugungi faol: <b>{stats['active_today']}</b> ta\n"
        f"⭐ VIP a'zolar: <b>{stats['vip_count']}</b> ta\n"
        f"🪙 Umumiy Coinlar: <b>{stats['total_coins']:,}</b>\n"
        f"🖼 Do'kondagi fonlar: <b>{stats['total_backgrounds']}</b> ta\n"
        f"📁 Baza Guruhi: {storage_text}\n"
        f"💾 Oxirgi zaxira: <code>{last_bk}</code>\n"
        f"🌐 WebApp URL: <code>{webapp_url}</code>\n\n"
        f"Kerakli bo'limni tanlang 👇"
    )
    await update.message.reply_html(text, reply_markup=build_admin_main_keyboard())


async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if not is_admin(user_id):
        await query.answer("Ruxsat yo'q!", show_alert=True)
        return

    if data == "adm_stats":
        stats = get_stats()
        text = (
            f"📊 <b>To'liq Statistika:</b>\n\n"
            f"• Jami foydalanuvchilar: <b>{stats['total_users']}</b> ta\n"
            f"• Bugun dars qilganlar: <b>{stats['active_today']}</b> ta\n"
            f"• VIP obunachilar: <b>{stats['vip_count']}</b> ta\n"
            f"• Umumiy Coinlar: <b>{stats['total_coins']:,}</b> 🪙\n"
            f"• Umumiy EXP: <b>{stats['total_exp']:,}</b> ⚡\n"
            f"• Do'kondagi fonlar: <b>{stats['total_backgrounds']}</b> ta\n"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Orqaga", callback_data="adm_back")]])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

    elif data in ("adm_refresh", "adm_back"):
        stats = get_stats()
        webapp_url = get_webapp_url()
        storage_id = get_storage_chat_id()
        storage_text = f"<code>{storage_id}</code>" if storage_id else "<i>Sozlanmagan</i>"
        last_bk = get_setting("last_backup_time", "Hali qilinmagan")
        text = (
            f"👑 <b>SARN Admin Paneli</b>\n\n"
            f"👥 Foydalanuvchilar: <b>{stats['total_users']}</b> ta\n"
            f"⚡ Bugungi faol: <b>{stats['active_today']}</b> ta\n"
            f"⭐ VIP a'zolar: <b>{stats['vip_count']}</b> ta\n"
            f"🪙 Umumiy Coinlar: <b>{stats['total_coins']:,}</b>\n"
            f"🖼 Do'kondagi fonlar: <b>{stats['total_backgrounds']}</b> ta\n"
            f"📁 Baza Guruhi: {storage_text}\n"
            f"💾 Oxirgi zaxira: <code>{last_bk}</code>\n"
            f"🌐 WebApp URL: <code>{webapp_url}</code>\n\n"
            f"Kerakli bo'limni tanlang 👇"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_admin_main_keyboard())

    elif data == "adm_storage_group":
        cur_id = get_storage_chat_id()
        cur_txt = f"<code>{cur_id}</code>" if cur_id else "<i>Hali sozlanmagan</i>"
        text = (
            f"📁 <b>Media va Baza Guruhi Sozlamalari</b>\n\n"
            f"Hozirgi Baza Guruhi ID: {cur_txt}\n\n"
            f"<b>Bu nima uchun kerak?</b>\n"
            f"1) Barcha fon rasmlari guruhda xavfsiz saqlanadi.\n"
            f"2) <b>Har 1 soatda</b> bot avtomatik butun bazani (<code>sarn.db</code>) shu guruhga tashlab, PIN qiladi.\n"
            f"3) Render qayta deploy bo'lganda, bot shu guruhdagi PIN xabardan bazani avtomatik qayta tiklaydi!\n\n"
            f"<b>Qanday oson sozlanadi?</b>\n"
            f"• Maxfiy guruh yarating, botni admin qilib qo'shing va u yerda <code>/chatid</code> yozing!\n"
            f"Yoki ID raqamini pastdagi tugma orqali qo'lda kiriting:"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Guruh ID ni qo'lda kiritish", callback_data="adm_set_storage_group_start")],
            [InlineKeyboardButton("◀️ Orqaga", callback_data="adm_back")]
        ])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

    elif data == "adm_backup_menu":
        last_bk = get_setting("last_backup_time", "Mavjud emas")
        storage_id = get_storage_chat_id()
        st_text = f"<code>{storage_id}</code>" if storage_id else "❌ Sozlanmagan"
        text = (
            f"💾 <b>Ma'lumotlar Bazasi Zaxirasi (Backup)</b>\n\n"
            f"📁 Saqlash guruhi: {st_text}\n"
            f"👥 Jami foydalanuvchilar: <b>{_count_users_in_db()} ta</b>\n"
            f"⏰ So'nggi zaxira vaqti: <code>{last_bk}</code>\n"
            f"🔄 Avtomatik zaxiralash: <b>Har 1 soatda faol</b>\n\n"
            f"Render'da bot qayta deploy qilinganda ma'lumotlar yo'qolmasligi uchun "
            f"guruhdagi PIN qilingan zaxira nusxadan avtomatik tiklanadi."
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Hozir Zaxiralash", callback_data="adm_backup_now"),
                InlineKeyboardButton("♻️ Zaxiradan Tiklash", callback_data="adm_restore_now"),
            ],
            [
                InlineKeyboardButton("📥 DB Faylni Qo'lda Yuklash", callback_data="adm_db_upload_start"),
            ],
            [InlineKeyboardButton("◀️ Orqaga", callback_data="adm_back")]
        ])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

    elif data == "adm_backup_now":
        await query.edit_message_text("🔄 Baza zaxiralanmoqda, biroz kuting...")
        result = await backup_database(context)
        if result["ok"]:
            pin_msg = "✅ Ha (muvaffaqiyatli)" if result["pinned"] else "⚠️ Yo'q (botda Pin huquqi yo'q)"
            text = (
                f"✅ <b>Baza muvaffaqiyatli zaxiralandi!</b>\n\n"
                f"📁 Guruh ID: <code>{result['chat_id']}</code>\n"
                f"👥 Foydalanuvchilar: <b>{result['users_count']} ta</b>\n"
                f"📌 Xabar PIN qilindi: {pin_msg}\n"
            )
        else:
            text = (
                f"❌ <b>Zaxiralashda xatolik yuz berdi!</b>\n\n"
                f"Sabab: <code>{result['reason']}</code>\n"
                f"Xato: <code>{result.get('error') or 'Aniqlanmadi'}</code>\n\n"
                f"Baza guruhi to'g'ri sozlanganini va bot admin ekanini tekshiring."
            )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Zaxira menyusiga", callback_data="adm_backup_menu")]])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

    elif data == "adm_restore_now":
        await query.edit_message_text("♻️ Guruhdagi PIN xabardan baza tiklanmoqda...")
        result = await _restore_from_pinned_backup(context.bot)
        if result["ok"]:
            text = (
                f"✅ <b>Baza muvaffaqiyatli tiklandi!</b>\n\n"
                f"👥 Tiklangan foydalanuvchilar: <b>{result['users_count']} ta</b>\n"
                f"Baza holati to'liq yangilandi."
            )
        else:
            reason_map = {
                "no_chat": "Baza guruhi sozlanmagan",
                "no_pinned": "Guruhda PIN qilingan zaxira xabari topilmadi",
                "download_failed": "Faylni yuklab olishda xatolik yuz berdi"
            }
            text = (
                f"❌ <b>Bazani tiklab bo'lmadi!</b>\n\n"
                f"Sabab: <b>{reason_map.get(result['reason'], result['reason'])}</b>\n"
                f"{result.get('error', '')}"
            )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Zaxira menyusiga", callback_data="adm_backup_menu")]])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

    elif data == "adm_list_bg":
        bgs = get_all_backgrounds()
        if not bgs:
            await query.edit_message_text(
                "🖼 Hozircha do'konda hech qanday fon yo'q.\n«🖼 Fon Qo'shish» tugmasi orqali fon qo'shing.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Orqaga", callback_data="adm_back")]]),
            )
            return
        lines = ["🖼 <b>Do'kondagi Fonlar (Ombor):</b>\n"]
        buttons = []
        for bg in bgs:
            code_str = bg.get('code') or f"{bg['id']}bg"
            lines.append(f"• <b>[{code_str}] {bg['name']}</b> — {bg['price']} Coin (ID: {bg['id']})")
            buttons.append([
                InlineKeyboardButton(f"👁 [{code_str}] Ko'rish", callback_data=f"adm_view_bg_{bg['id']}"),
                InlineKeyboardButton(f"🗑 O'chirish", callback_data=f"adm_del_bg_{bg['id']}"),
            ])
        buttons.append([InlineKeyboardButton("◀️ Orqaga", callback_data="adm_back")])
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("adm_view_bg_"):
        bg_id = int(data.split("_")[-1])
        bg = get_background_by_id(bg_id)
        if not bg:
            await query.answer("Fon topilmadi!", show_alert=True)
            return
        code_str = bg.get('code') or f"{bg['id']}bg"
        caption = (
            f"🖼 <b>{bg['name']}</b>\n"
            f"🔑 <b>Kod:</b> <code>{code_str}</code>\n"
            f"🪙 <b>Narx:</b> <b>{bg['price']} Coin</b>\n"
            f"🆔 <b>ID:</b> <code>{bg['id']}</code>"
        )
        sent = False
        if bg.get("storage_chat_id") and bg.get("storage_message_id"):
            try:
                await context.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=bg["storage_chat_id"],
                    message_id=bg["storage_message_id"],
                    caption=caption,
                    parse_mode="HTML",
                )
                sent = True
            except Exception:
                pass
        if not sent and bg.get("file_id"):
            try:
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=bg["file_id"],
                    caption=caption,
                    parse_mode="HTML",
                )
                sent = True
            except Exception:
                pass
        if not sent:
            await query.answer("Rasmni yuklab bo'lmadi.", show_alert=True)

    elif data.startswith("adm_del_bg_"):
        bg_id = int(data.split("_")[-1])
        delete_background(bg_id)
        await query.answer("✅ Fon o'chirildi!", show_alert=True)
        bgs = get_all_backgrounds()
        if not bgs:
            await query.edit_message_text(
                "🖼 Do'kon bo'sh.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Orqaga", callback_data="adm_back")]]),
            )
            return
        lines = ["🖼 <b>Do'kondagi Fonlar:</b>\n"]
        buttons = []
        for bg in bgs:
            lines.append(f"• <b>{bg['name']}</b> — {bg['price']} Coin (ID: {bg['id']})")
            buttons.append([
                InlineKeyboardButton(f"👁 {bg['name']}", callback_data=f"adm_view_bg_{bg['id']}"),
                InlineKeyboardButton(f"🗑 O'chirish", callback_data=f"adm_del_bg_{bg['id']}"),
            ])
        buttons.append([InlineKeyboardButton("◀️ Orqaga", callback_data="adm_back")])
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm_list_subjects":
        subjects = get_all_subjects()
        total_q = sum(s["question_count"] for s in subjects)
        lines = [
            "📚 <b>SARN Fanlar va Test Savollari:</b>\n",
            f"Jami test savollari: <b>{total_q} ta</b>\n",
        ]
        for s in subjects:
            lines.append(f"• <b>{s['name']}</b>: <code>{s['question_count']} ta savol</code>")
        lines.append("\n<i>Yangi test qo'shish uchun pastdagi tugmani bosing:</i>")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Yangi Test Qo'shish", callback_data="adm_add_test")],
            [InlineKeyboardButton("◀️ Orqaga", callback_data="adm_back")]
        ])
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=kb,
        )


# ========== BAZA GURUHINI SOZLASH CONVERSATION ==========

async def adm_set_storage_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_html(
        "📁 <b>Baza Guruhi ID raqamini kiriting:</b>\n\n"
        "Guruhning manfiy ID raqamini yuboring (masalan: <code>-1002345678901</code>) "
        "yoki o'sha guruhdan biror xabarni shu yerga forward qiling.\n\n"
        "<i>Bekor qilish uchun /cancel yozing</i>"
    )
    return WAITING_STORAGE_GROUP


async def adm_set_storage_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    target_id = None

    if msg.forward_from_chat:
        target_id = msg.forward_from_chat.id
    elif msg.text and msg.text.strip().lstrip("-").isdigit():
        target_id = int(msg.text.strip())

    if not target_id:
        await msg.reply_text("❌ Tushunarsiz format. Guruh ID raqamini kiriting (masalan -100...) yoki /cancel:")
        return WAITING_STORAGE_GROUP

    try:
        member = await context.bot.get_chat_member(target_id, context.bot.id)
        if member.status not in ("administrator", "creator"):
            await msg.reply_text("⚠️ Bot bu chatda ADMIN emas. Avval botni admin qilib qo'shing, so'ng ID ni yuboring:")
            return WAITING_STORAGE_GROUP
    except Exception as e:
        await msg.reply_text(f"❌ Guruh tekshirilmadi: {e}. Bot u yerda bormi?")
        return WAITING_STORAGE_GROUP

    set_setting("storage_chat_id", str(target_id))
    await msg.reply_html(
        f"✅ <b>Baza guruhi muvaffaqiyatli saqlandi!</b>\nID: <code>{target_id}</code>\n\n"
        f"Endi fon rasmlari va soatlik baza zaxirasi ushbu guruhga avtomatik nusxalanadi.",
        reply_markup=build_admin_main_keyboard(),
    )
    return ConversationHandler.END


# ========== DB FAYLNI QO'LDA YUKLASH OQIMI ==========

async def adm_db_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_html(
        "📥 <b>Baza Faylini Qo'lda Yuklash (.db)</b>\n\n"
        "Iltimos, avval saqlangan <code>sarn.db</code> yoki <code>sarn_backup.db</code> faylini "
        "shu yerga <b>Hujjat (Document)</b> sifatida yuboring.\n\n"
        "⚠️ <i>DIQQAT: Bu joriy ma'lumotlar bazasini yangi yuklangan fayl bilan to'liq almashtiradi!</i>\n"
        "<i>Bekor qilish uchun /cancel</i>"
    )
    return WAITING_DB_UPLOAD


async def adm_db_upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg.document:
        await msg.reply_text("❌ Bu hujjat emas. Iltimos, .db faylni Document sifatida yuboring yoki /cancel:")
        return WAITING_DB_UPLOAD

    tmp_path = DB_PATH + ".import_tmp"
    try:
        file = await context.bot.get_file(msg.document.file_id)
        await file.download_to_drive(tmp_path)
    except Exception as e:
        await msg.reply_text(f"❌ Faylni yuklab olishda xatolik: {e}")
        return WAITING_DB_UPLOAD

    # SQLite validatsiya
    users_count = 0
    try:
        test_conn = sqlite3.connect(tmp_path)
        test_cur = test_conn.cursor()
        test_cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in test_cur.fetchall()}
        if "users" in tables:
            test_cur.execute("SELECT COUNT(*) FROM users")
            users_count = test_cur.fetchone()[0]
        test_conn.close()
    except Exception:
        tables = set()

    if "users" not in tables:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        await msg.reply_text("❌ Bu fayl yaroqsiz yoki SARN bazasi emas (users jadvali topilmadi).")
        return WAITING_DB_UPLOAD

    context.user_data["db_tmp_path"] = tmp_path
    context.user_data["db_tmp_users"] = users_count

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Ha, almashtirilsin", callback_data="adm_db_confirm")],
        [InlineKeyboardButton("❌ Bekor qilish", callback_data="adm_db_cancel")],
    ])
    await msg.reply_html(
        f"🔍 <b>Fayl tekshirildi:</b>\n"
        f"Topilgan foydalanuvchilar: <b>{users_count} ta</b>\n\n"
        f"Haqiqatan ham joriy bazani shu fayl bilan almashtirmoqchimisiz?",
        reply_markup=kb,
    )
    return WAITING_DB_UPLOAD_CONFIRM


async def adm_db_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tmp_path = context.user_data.pop("db_tmp_path", None)
    users_count = context.user_data.pop("db_tmp_users", 0)

    if not tmp_path or not os.path.exists(tmp_path):
        await query.edit_message_text("⚠️ Fayl topilmadi. Qaytadan urinib ko'ring: /admin")
        return ConversationHandler.END

    try:
        os.replace(tmp_path, DB_PATH)
    except Exception as e:
        logger.exception("Faylni almashtirishda xatolik: %s", e)
        await query.edit_message_text(f"❌ Faylni almashtirishda xatolik: {e}")
        return ConversationHandler.END

    init_db()
    # Yangi bazani darhol zaxiralab qo'yish
    await backup_database(context)

    await query.edit_message_text(
        f"🎉 <b>Baza muvaffaqiyatli almashtirildi!</b>\n"
        f"Jami foydalanuvchilar: <b>{users_count} ta</b>\n\n"
        f"Yangi baza Baza guruhiga ham darhol zaxiralandi.",
        parse_mode="HTML",
        reply_markup=build_admin_main_keyboard(),
    )
    return ConversationHandler.END


async def adm_db_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tmp_path = context.user_data.pop("db_tmp_path", None)
    if tmp_path and os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except Exception:
            pass
    await query.edit_message_text("Amal bekor qilindi.")
    return ConversationHandler.END


# ========== FON QO'SHISH OQIMI (Admin) ==========

async def adm_add_bg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_html(
        "🖼 <b>Yangi Fon Qo'shish</b>\n\n"
        "1-qadam: Fon rasmini yuboring (jpg yoki png):\n"
        "<i>(Bekor qilish uchun /cancel)</i>"
    )
    return WAITING_BG_PHOTO


async def adm_add_bg_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo and not update.message.document:
        await update.message.reply_text("⚠️ Iltimos, rasm yuboring (jpg yoki png fayl):")
        return WAITING_BG_PHOTO

    # Rasmni yuklab olish
    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
        file_id = update.message.photo[-1].file_id
    else:
        photo_file = await update.message.document.get_file()
        file_id = update.message.document.file_id

    ext = "jpg"
    orig_name = getattr(photo_file, "file_path", "") or ""
    if orig_name.lower().endswith(".png"):
        ext = "png"

    import re
    uid = photo_file.file_unique_id or file_id[:12]
    clean_uid = re.sub(r'[^a-zA-Z0-9_-]', '', uid)
    temp_filename = f"temp_bg_{clean_uid}.{ext}"
    temp_path = os.path.join(IMAGES_DIR, temp_filename)
    await photo_file.download_to_drive(temp_path)

    context.user_data["bg_pending_temp_path"] = temp_path
    context.user_data["bg_pending_ext"] = ext
    context.user_data["bg_pending_file_id"] = file_id
    context.user_data["bg_pending_msg_id"] = update.message.message_id
    context.user_data["bg_pending_chat_id"] = update.effective_chat.id

    await update.message.reply_html(
        f"✅ Rasm qabul qilindi!\n\n"
        f"2-qadam: Ushbu fon uchun <b>unikal KOD</b> kiriting (masalan: <code>1bg</code>, <code>2bg</code>, <code>sakura_bg</code>):\n"
        f"<i>(Har bir fon uchun alohida kod beriladi, bu orqali bot va guruhda rasmlar hech qachon chalkashmaydi)</i>"
    )
    return WAITING_BG_CODE


async def adm_add_bg_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import re
    code = update.message.text.strip().lower()
    if not re.match(r'^[a-zA-Z0-9_-]+$', code):
        await update.message.reply_html(
            "⚠️ Kod faqat lotin harflari, raqamlar va _ belgilaridan iborat bo'lishi kerak.\n"
            "Masalan: <code>1bg</code> yoki <code>2bg</code>. Qaytadan kiriting:"
        )
        return WAITING_BG_CODE

    if is_bg_code_taken(code):
        await update.message.reply_html(
            f"⚠️ <code>{code}</code> kodi allaqachon boshqa fonga berilgan!\n"
            f"Iltimos, boshqa kod kiriting (masalan: <code>2bg</code>):"
        )
        return WAITING_BG_CODE

    context.user_data["bg_pending_code"] = code
    await update.message.reply_html(
        f"✅ Kod: <code>{code}</code> biriktirildi!\n\n"
        f"3-qadam: Ushbu fon uchun <b>nom</b> kiriting (masalan: «Tungi Tog'»):"
    )
    return WAITING_BG_NAME


async def adm_add_bg_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Nom bo'sh bo'lmasligi kerak. Qaytadan kiriting:")
        return WAITING_BG_NAME
    context.user_data["bg_pending_name"] = name
    await update.message.reply_html(
        f"✅ Nom: <b>{name}</b>\n\n"
        f"4-qadam: Ushbu fon uchun <b>narx (Coin)</b> ni kiriting (masalan: <b>20</b>):"
    )
    return WAITING_BG_PRICE


async def adm_add_bg_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price_str = update.message.text.strip()
    if not price_str.isdigit():
        await update.message.reply_text("Iltimos, faqat musbat son kiriting (masalan: 20):")
        return WAITING_BG_PRICE

    price = int(price_str)
    code = context.user_data.get("bg_pending_code", "bg")
    name = context.user_data.get("bg_pending_name", "Nomsiz fon")
    ext = context.user_data.get("bg_pending_ext", "jpg")
    temp_path = context.user_data.get("bg_pending_temp_path", "")
    file_id = context.user_data.get("bg_pending_file_id", "")
    from_msg_id = context.user_data.get("bg_pending_msg_id")
    from_chat_id = context.user_data.get("bg_pending_chat_id")

    filename = f"bg_{code}.{ext}"
    final_path = os.path.join(IMAGES_DIR, filename)
    if temp_path and os.path.exists(temp_path):
        try:
            if os.path.exists(final_path):
                os.remove(final_path)
            os.replace(temp_path, final_path)
        except Exception as e:
            logger.warning("Faylni ko'chirishda xatolik: %s", e)

    storage_chat_id = get_storage_chat_id()
    storage_message_id = None
    storage_notice = ""

    # === BAZA GURUHIGA KOD BILAN NUSXALAB TASHLASH ===
    if storage_chat_id and from_msg_id:
        try:
            stored_msg = await context.bot.copy_message(
                chat_id=storage_chat_id,
                from_chat_id=from_chat_id,
                message_id=from_msg_id,
                caption=f"🖼 <b>SARN Fon Ombori</b>\n"
                        f"🔑 <b>Kod:</b> <code>{code}</code>\n"
                        f"🏷 <b>Nomi:</b> <b>{name}</b>\n"
                        f"🪙 <b>Narxi:</b> {price} Coin\n"
                        f"📅 <b>Sana:</b> {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                parse_mode="HTML",
            )
            storage_message_id = stored_msg.message_id
            storage_notice = f"\n📁 <i>Rasm Baza Guruhiga ({storage_chat_id}) <code>{code}</code> kodi bilan nusxalandi!</i>"
        except Exception as e:
            logger.warning(f"Baza guruhiga nusxalab bo'lmadi: {e}")
            storage_notice = "\n⚠️ <i>Baza guruhiga nusxalab bo'lmadi (guruh sozlamalarini tekshiring).</i>"

    bg_id = add_background(
        name=name,
        filename=filename,
        price=price,
        code=code,
        file_id=file_id,
        storage_chat_id=storage_chat_id,
        storage_message_id=storage_message_id,
    )

    await update.message.reply_html(
        f"🎉 <b>Yangi fon muvaffaqiyatli qo'shildi!</b>\n\n"
        f"🔑 <b>Kodi:</b> <code>{code}</code>\n"
        f"🖼 <b>Nomi:</b> <b>{name}</b>\n"
        f"🪙 <b>Narxi:</b> <b>{price} Coin</b>\n"
        f"🆔 <b>ID:</b> <code>{bg_id}</code>"
        f"{storage_notice}\n\n"
        f"Fon endi do'konda ko'rinadi va Baza guruhidan xohlagan paytda olinadi!",
        reply_markup=build_admin_main_keyboard(),
    )
    return ConversationHandler.END


# ========== VIP BERISH ==========

async def adm_vip_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_html(
        "⭐ <b>VIP berish:</b>\n\n"
        "Foydalanuvchining <b>Telegram ID</b> yoki <b>@username</b>ini yuboring:\n"
        "<i>(/cancel — bekor qilish)</i>"
    )
    return WAITING_VIP_USER


async def adm_vip_receive_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.message.text.strip()
    chat_id = find_user_by_query(target)
    if not chat_id:
        await update.message.reply_html(
            f"❌ Foydalanuvchi topilmadi: <code>{target}</code>.\n"
            f"Foydalanuvchi avval /start bosgan bo'lishi kerak. Qayta kiriting:"
        )
        return WAITING_VIP_USER
    context.user_data["vip_target_chat_id"] = chat_id
    await update.message.reply_html(
        f"✅ Foydalanuvchi topildi (ID: <code>{chat_id}</code>).\n\n"
        f"Necha kunga VIP? (Masalan: <b>30</b>, bekor qilish uchun <b>0</b>):"
    )
    return WAITING_VIP_DAYS


async def adm_vip_receive_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days_str = update.message.text.strip()
    if not days_str.isdigit():
        await update.message.reply_text("Faqat raqam kiriting (masalan: 30):")
        return WAITING_VIP_DAYS
    days = int(days_str)
    chat_id = context.user_data.get("vip_target_chat_id")
    if days == 0:
        revoke_vip(chat_id)
        await update.message.reply_html(f"🚫 (ID: <code>{chat_id}</code>) VIP bekor qilindi.")
    else:
        set_vip(chat_id, days)
        await update.message.reply_html(
            f"🎉 (ID: <code>{chat_id}</code>)ga <b>{days} kunlik</b> VIP berildi!"
        )
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⭐ <b>Tabriklaymiz!</b> Sizga {days} kunlik Premium VIP berildi!\n"
                     f"Endi har bir topshiriqdan 2 barobar ko'proq EXP olasiz!",
                parse_mode="HTML",
            )
        except Exception:
            pass
    return ConversationHandler.END


# ========== COIN / EXP BONUS ==========

async def adm_bonus_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_html(
        "🪙 <b>Coin yoki EXP qo'shish:</b>\n\n"
        "Foydalanuvchining <b>Telegram ID</b> yoki <b>@username</b>ini yuboring:\n"
        "<i>(/cancel — bekor qilish)</i>"
    )
    return WAITING_BONUS_USER


async def adm_bonus_receive_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.message.text.strip()
    chat_id = find_user_by_query(target)
    if not chat_id:
        await update.message.reply_html(f"❌ Topilmadi: <code>{target}</code>. Qayta kiriting:")
        return WAITING_BONUS_USER
    context.user_data["bonus_target_chat_id"] = chat_id
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🪙 Coin", callback_data="add_coin"),
        InlineKeyboardButton("⚡ EXP", callback_data="add_exp"),
    ]])
    await update.message.reply_html(
        f"Foydalanuvchi: <code>{chat_id}</code>. Nimani qo'shmoqchisiz?",
        reply_markup=kb,
    )
    return WAITING_BONUS_TYPE


async def adm_bonus_receive_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    btype = "coin" if query.data == "add_coin" else "exp"
    context.user_data["bonus_type"] = btype
    await query.message.reply_html(
        f"Qancha <b>{btype.upper()}</b> qo'shmoqchisiz? Raqam yuboring (masalan: 500):"
    )
    return WAITING_BONUS_AMOUNT


async def adm_bonus_receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amt_str = update.message.text.strip()
    if not amt_str.isdigit():
        await update.message.reply_text("Musbat butun raqam kiriting:")
        return WAITING_BONUS_AMOUNT
    amt = int(amt_str)
    chat_id = context.user_data.get("bonus_target_chat_id")
    btype = context.user_data.get("bonus_type", "coin")
    if btype == "coin":
        add_coins(chat_id, amt)
        await update.message.reply_html(f"✅ <b>+{amt} Coin</b> qo'shildi!")
    else:
        given = add_exp(chat_id, amt)
        await update.message.reply_html(f"✅ <b>+{given} EXP</b> qo'shildi!")
    return ConversationHandler.END


# ========== WEBAPP URL SOZLASH ==========

async def adm_seturl_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    current_url = get_webapp_url()
    await query.message.reply_html(
        f"🌐 <b>WebApp manzilini sozlash</b>\n\n"
        f"Hozirgi havola: <code>{current_url}</code>\n\n"
        f"Yangi HTTPS havolani yuboring:\n<i>(/cancel — bekor qilish)</i>"
    )
    return WAITING_WEBAPP_URL


async def adm_seturl_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_url = update.message.text.strip()
    if not (new_url.startswith("https://") or new_url.startswith("http://")):
        await update.message.reply_text("⚠️ Havola http:// yoki https:// bilan boshlanishi kerak:")
        return WAITING_WEBAPP_URL
    set_setting("webapp_url", new_url)
    await update.message.reply_html(
        f"✅ <b>WebApp yangilandi!</b>\n<code>{new_url}</code>",
        reply_markup=build_admin_main_keyboard(),
    )
    return ConversationHandler.END


# ========== BROADCAST ==========

async def adm_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_html(
        "📢 <b>Barcha foydalanuvchilarga xabar tarqatish</b>\n\n"
        "Xabarni yuboring (matn, rasm yoki video):\n"
        "<i>(/cancel — bekor qilish)</i>"
    )
    return WAITING_BROADCAST_MSG


async def adm_broadcast_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_chat_id = update.effective_chat.id
    message_id = update.message.message_id
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM users")
    users = [r[0] for r in cur.fetchall()]
    conn.close()
    sent = 0
    await update.message.reply_text(f"⏳ {len(users)} ta foydalanuvchiga yuborilmoqda...")
    for uid in users:
        try:
            await context.bot.copy_message(chat_id=uid, from_chat_id=admin_chat_id, message_id=message_id)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass
    await update.message.reply_html(
        f"✅ <b>Tarqatildi!</b> Yetkazildi: <b>{sent}/{len(users)}</b>",
        reply_markup=build_admin_main_keyboard(),
    )
    return ConversationHandler.END


# ========== TEST QO'SHISH OQIMI (Admin) ==========

def track_test_msg(context: ContextTypes.DEFAULT_TYPE, msg_id: int):
    """Test kiritish oqimidagi xabarlar ID sini saqlaydi."""
    msgs = context.user_data.setdefault("test_flow_msgs", [])
    if msg_id and msg_id not in msgs:
        msgs.append(msg_id)


async def cleanup_test_flow_messages(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Test kiritish oqimidagi barcha oraliq xabarlarni o'chiradi."""
    msgs = context.user_data.pop("test_flow_msgs", [])
    for mid in msgs:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass


async def adm_add_test_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END

    context.user_data["test_flow_msgs"] = []

    text = (
        "📝 <b>Yangi Test Savoli Qo'shish (1/6)</b>\n\n"
        "<b>Savol matnini yuboring:</b>\n"
        "<i>(Bekor qilish uchun /cancel yozing)</i>"
    )

    if update.callback_query:
        await update.callback_query.answer()
        msg = await update.callback_query.message.reply_html(text)
    else:
        track_test_msg(context, update.message.message_id)
        msg = await update.message.reply_html(text)

    track_test_msg(context, msg.message_id)
    return WAITING_TEST_Q


async def adm_add_test_q(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_test_msg(context, update.message.message_id)
    text = (update.message.text or "").strip()
    if not text:
        msg = await update.message.reply_text("⚠️ Savol matni bo'sh bo'lmasligi kerak. Qaytadan yozing:")
        track_test_msg(context, msg.message_id)
        return WAITING_TEST_Q

    context.user_data["test_q"] = text
    msg = await update.message.reply_html(
        "📝 <b>2/6: A variantni yuboring:</b>"
    )
    track_test_msg(context, msg.message_id)
    return WAITING_TEST_OPTA


async def adm_add_test_opta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_test_msg(context, update.message.message_id)
    text = (update.message.text or "").strip()
    if not text:
        msg = await update.message.reply_text("⚠️ A varianti bo'sh bo'lmasligi kerak. Qaytadan yozing:")
        track_test_msg(context, msg.message_id)
        return WAITING_TEST_OPTA

    context.user_data["test_opta"] = text
    msg = await update.message.reply_html(
        "📝 <b>3/6: B variantni yuboring:</b>"
    )
    track_test_msg(context, msg.message_id)
    return WAITING_TEST_OPTB


async def adm_add_test_optb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_test_msg(context, update.message.message_id)
    text = (update.message.text or "").strip()
    if not text:
        msg = await update.message.reply_text("⚠️ B varianti bo'sh bo'lmasligi kerak. Qaytadan yozing:")
        track_test_msg(context, msg.message_id)
        return WAITING_TEST_OPTB

    context.user_data["test_optb"] = text
    msg = await update.message.reply_html(
        "📝 <b>4/6: C variantni yuboring:</b>"
    )
    track_test_msg(context, msg.message_id)
    return WAITING_TEST_OPTC


async def adm_add_test_optc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_test_msg(context, update.message.message_id)
    text = (update.message.text or "").strip()
    if not text:
        msg = await update.message.reply_text("⚠️ C varianti bo'sh bo'lmasligi kerak. Qaytadan yozing:")
        track_test_msg(context, msg.message_id)
        return WAITING_TEST_OPTC

    context.user_data["test_optc"] = text
    msg = await update.message.reply_html(
        "📝 <b>5/6: D variantni yuboring:</b>"
    )
    track_test_msg(context, msg.message_id)
    return WAITING_TEST_OPTD


async def adm_add_test_optd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_test_msg(context, update.message.message_id)
    text = (update.message.text or "").strip()
    if not text:
        msg = await update.message.reply_text("⚠️ D varianti bo'sh bo'lmasligi kerak. Qaytadan yozing:")
        track_test_msg(context, msg.message_id)
        return WAITING_TEST_OPTD

    context.user_data["test_optd"] = text

    q = context.user_data["test_q"]
    opta = context.user_data["test_opta"]
    optb = context.user_data["test_optb"]
    optc = context.user_data["test_optc"]
    optd = context.user_data["test_optd"]

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("A", callback_data="test_cor_A"),
            InlineKeyboardButton("B", callback_data="test_cor_B"),
            InlineKeyboardButton("C", callback_data="test_cor_C"),
            InlineKeyboardButton("D", callback_data="test_cor_D"),
        ]
    ])

    msg = await update.message.reply_html(
        f"🎯 <b>6/6: To'g'ri javob variantini tanlang:</b>\n\n"
        f"❓ <b>Savol:</b> {q}\n"
        f"<b>A)</b> {opta}\n"
        f"<b>B)</b> {optb}\n"
        f"<b>C)</b> {optc}\n"
        f"<b>D)</b> {optd}\n\n"
        f"<i>Quyidagi tugmalardan to'g'ri javobni bosing (yoki A, B, C, D deb yozing):</i>",
        reply_markup=kb,
    )
    track_test_msg(context, msg.message_id)
    return WAITING_TEST_CORRECT


async def adm_add_test_correct_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    correct = query.data.replace("test_cor_", "").upper()
    context.user_data["test_correct"] = correct
    return await _show_test_subject_selection(update, context)


async def adm_add_test_correct_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_test_msg(context, update.message.message_id)
    text = (update.message.text or "").strip().upper()
    if text not in ("A", "B", "C", "D"):
        msg = await update.message.reply_text("⚠️ Iltimos, to'g'ri javob harfini tanlang (A, B, C yoki D):")
        track_test_msg(context, msg.message_id)
        return WAITING_TEST_CORRECT
    context.user_data["test_correct"] = text
    return await _show_test_subject_selection(update, context)


async def _show_test_subject_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subjects = get_all_subjects()
    buttons = []
    row = []
    for s in subjects:
        row.append(InlineKeyboardButton(f"{s['name']}", callback_data=f"test_sub_{s['id']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    text = (
        "📚 <b>Ushbu savol qaysi fanga tegishli?</b>\n\n"
        "Quyidagi fanlardan birini tanlang 👇"
    )
    if update.callback_query:
        msg = await update.callback_query.message.reply_html(text, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        msg = await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(buttons))
    track_test_msg(context, msg.message_id)
    return WAITING_TEST_SUBJECT


async def adm_add_test_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    subj_id = int(query.data.replace("test_sub_", ""))
    sub = get_subject_by_id(subj_id)
    if not sub:
        await query.answer("Fan topilmadi!", show_alert=True)
        return WAITING_TEST_SUBJECT

    q_text = context.user_data.get("test_q")
    opta = context.user_data.get("test_opta")
    optb = context.user_data.get("test_optb")
    optc = context.user_data.get("test_optc")
    optd = context.user_data.get("test_optd")
    correct = context.user_data.get("test_correct")

    # DB ga qo'shish
    add_question(subj_id, q_text, opta, optb, optc, optd, correct)
    new_count = get_subject_question_count(subj_id)

    # Oraliq yozishmalarni auto tozalash
    await cleanup_test_flow_messages(context, chat_id)

    # Yakuniy natija
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ <b>Test savoli muvaffaqiyatli saqlandi!</b>\n\n"
            f"📚 Fan: <b>{sub['name']}</b>\n"
            f"❓ Savol: <i>{q_text}</i>\n"
            f"🎯 To'g'ri javob: <b>{correct}</b>\n"
            f"📊 Fandagi jami savollar: <b>{new_count} ta</b> (+1)\n\n"
            f"<i>🧹 Chat tozaligi uchun test kiritish jarayonidagi barcha xabarlar o'chirildi.</i>"
        ),
        parse_mode="HTML",
        reply_markup=build_admin_main_keyboard(),
    )
    return ConversationHandler.END


async def cancel_test_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if update.message:
        track_test_msg(context, update.message.message_id)
    await cleanup_test_flow_messages(context, chat_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text="Amal bekor qilindi.",
        reply_markup=build_admin_main_keyboard(),
    )
    return ConversationHandler.END


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Amal bekor qilindi.")
    return ConversationHandler.END


# ========== FOYDALANUVCHI CALLBACK ==========

async def user_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.from_user.id

    if data == "open_app_alert":
        url = get_webapp_url()
        await query.answer(
            f"Mini App havolasi:\n{url}\n\nTelegram ichida to'liq ochish uchun HTTPS talab qilinadi.",
            show_alert=True,
        )
    elif data == "u_profile":
        u = get_user_data(chat_id)
        if not u:
            get_or_create_user(chat_id, query.from_user.username, query.from_user.full_name)
            u = get_user_data(chat_id)
        vip_str = "⭐ Faol VIP" if u["is_vip"] else "Standart"
        text = (
            f"👤 <b>Profilingiz:</b>\n\n"
            f"🔥 Daraja: <b>{u['rank']}</b> ({u['rank_title']})\n"
            f"⚡ EXP: <b>{u['exp']} / {u['next_rank_exp']}</b>\n"
            f"🪙 Tangalar: <b>{u['coins']} Coin</b>\n"
            f"👑 Status: <b>{vip_str}</b>\n\n"
            f"<i>Batafsil uchun Mini App'ni oching!</i>"
        )
        await query.message.reply_html(text, reply_markup=build_user_main_keyboard())
    elif data == "u_vip":
        text = (
            "⭐ <b>SARN Premium Obunasi</b>\n\n"
            "Obuna bo'lish majburiy emas. Agar Premium olsangiz:\n"
            "• ⚡ Barcha topshiriqlardan <b>x2 EXP</b>\n"
            "• 🪙 Do'kondagi fonlarga <b>maxsus chegirmalar</b>\n"
            "• 👑 Profilda tilla toj nishoni\n\n"
            "Obuna uchun admin bilan bog'laning: @ws12x"
        )
        await query.message.reply_html(text, reply_markup=build_user_main_keyboard())
    elif data == "u_about":
        text = (
            "📖 <b>SARN (Solo + Learn) Haqida:</b>\n\n"
            "🎯 <b>Muammo:</b> Yoshlarning darslarga befarqligi.\n"
            "💡 <b>Yechim:</b> Ta'limni RPG o'yiniga aylantirish!\n\n"
            "📚 10 ta fan, testlar, jangovar xonalar, ballar va reytinglar "
            "orqali bilimingizni sinang!"
        )
        await query.message.reply_html(text, reply_markup=build_user_main_keyboard())
    elif data == "u_ranks":
        text = (
            "🏆 <b>SARN Darajalar Shkalasi:</b>\n\n"
            "• <b>F</b>: 0 – 500 EXP\n"
            "• <b>E</b>: 500 – 1 000 EXP\n"
            "• <b>D</b>: 1 000 – 5 000 EXP\n"
            "• <b>C</b>: 5 000 – 10 000 EXP\n"
            "• <b>B</b>: 10 000 – 50 000 EXP\n"
            "• <b>A</b>: 50 000 – 100 000 EXP\n"
            "• <b>S</b>: 100 000 – 200 000 EXP\n"
            "• <b>SS</b>: 200 000 – 500 000 EXP\n"
            "• <b>SSS</b>: 500 000+ EXP"
        )
        await query.message.reply_html(text, reply_markup=build_user_main_keyboard())


# ==========================================
# 7. ASOSIY ISHGA TUSHIRISH (MAIN)
# ==========================================

async def _post_init(app):
    """Bot polling boshlashdan oldin ishga tushadigan bosqich: bazani tekshiradi/tiklaydi."""
    await restore_database_if_needed(app.bot)
    init_db()
    # JobQueue yo'q bo'lsa, asyncio orqa fondagi soatlik siklni ishga tushiramiz
    if app.job_queue is None:
        asyncio.create_task(hourly_backup_loop(app.bot))
        logger.info("JobQueue topilmadi - Soatlik zaxiralash asyncio orqa fonda ishga tushirildi.")


async def handle_bg_code_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foydalanuvchi yoki admin fon kodini yozganda (masalan '1bg'), guruhdan rasmni nusxalab yuboradi."""
    text = (update.message.text or "").strip().lower()
    if text.startswith("/bg"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            text = parts[1].strip()
        else:
            await update.message.reply_html(
                "ℹ️ Fonni ko'rish uchun uning kodini yozing (masalan: <code>1bg</code> yoki <code>/bg 1bg</code>)."
            )
            return True

    bg = get_background_by_code(text)
    if not bg:
        return False

    code_str = bg.get('code') or f"{bg['id']}bg"
    caption = (
        f"🖼 <b>{bg['name']}</b>\n"
        f"🔑 <b>Kod:</b> <code>{code_str}</code>\n"
        f"🪙 <b>Narxi:</b> {bg['price']} Coin\n"
        f"🆔 <b>ID:</b> <code>{bg['id']}</code>\n\n"
        f"<i>Ushbu fonni Mini App do'koni orqali xarid qilishingiz mumkin!</i>"
    )

    sent = False
    # Baza guruhidan nusxalab olish
    if bg.get("storage_chat_id") and bg.get("storage_message_id"):
        try:
            await context.bot.copy_message(
                chat_id=update.effective_chat.id,
                from_chat_id=bg["storage_chat_id"],
                message_id=bg["storage_message_id"],
                caption=caption,
                parse_mode="HTML",
            )
            sent = True
        except Exception as e:
            logger.warning("Baza guruhidan rasm nusxalanmadi: %s", e)

    # Agar guruhdan nusxalanmasa, file_id orqali yuborish
    if not sent and bg.get("file_id"):
        try:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=bg["file_id"],
                caption=caption,
                parse_mode="HTML",
            )
            sent = True
        except Exception:
            pass

    return sent


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    handled = await handle_bg_code_lookup(update, context)
    if handled:
        return


def main():
    init_db()
    start_web_server(WEB_PORT)

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(_post_init).build()

    # Baza guruhini sozlash conversation
    storage_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_set_storage_start, pattern=r"^adm_set_storage_group_start$")],
        states={
            WAITING_STORAGE_GROUP: [
                MessageHandler(filters.TEXT | filters.FORWARDED, adm_set_storage_receive)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_flow)],
    )

    # DB faylni qo'lda yuklash conversation
    db_upload_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_db_upload_start, pattern=r"^adm_db_upload_start$")],
        states={
            WAITING_DB_UPLOAD: [
                MessageHandler(filters.Document.ALL & ~filters.COMMAND, adm_db_upload_receive)
            ],
            WAITING_DB_UPLOAD_CONFIRM: [
                CallbackQueryHandler(adm_db_confirm, pattern=r"^adm_db_confirm$"),
                CallbackQueryHandler(adm_db_cancel, pattern=r"^adm_db_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_flow)],
    )

    # Fon qo'shish conversation (unikal kod bosqichi bilan)
    bg_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_add_bg_start, pattern=r"^adm_add_bg$")],
        states={
            WAITING_BG_PHOTO: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, adm_add_bg_photo),
            ],
            WAITING_BG_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_bg_code),
            ],
            WAITING_BG_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_bg_name),
            ],
            WAITING_BG_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_bg_price),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_flow)],
    )

    vip_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_vip_start, pattern=r"^adm_vip$")],
        states={
            WAITING_VIP_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_vip_receive_user)],
            WAITING_VIP_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_vip_receive_days)],
        },
        fallbacks=[CommandHandler("cancel", cancel_flow)],
    )

    bonus_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_bonus_start, pattern=r"^adm_bonus$")],
        states={
            WAITING_BONUS_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_bonus_receive_user)],
            WAITING_BONUS_TYPE: [CallbackQueryHandler(adm_bonus_receive_type, pattern=r"^add_(coin|exp)$")],
            WAITING_BONUS_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_bonus_receive_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel_flow)],
    )

    seturl_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_seturl_start, pattern=r"^adm_set_url$")],
        states={
            WAITING_WEBAPP_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_seturl_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel_flow)],
    )

    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_broadcast_start, pattern=r"^adm_broadcast$")],
        states={
            WAITING_BROADCAST_MSG: [MessageHandler(filters.ALL & ~filters.COMMAND, adm_broadcast_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel_flow)],
    )

    test_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(adm_add_test_start, pattern=r"^adm_add_test$"),
            CommandHandler("addtest", adm_add_test_start),
        ],
        states={
            WAITING_TEST_Q: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_test_q)
            ],
            WAITING_TEST_OPTA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_test_opta)
            ],
            WAITING_TEST_OPTB: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_test_optb)
            ],
            WAITING_TEST_OPTC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_test_optc)
            ],
            WAITING_TEST_OPTD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_test_optd)
            ],
            WAITING_TEST_CORRECT: [
                CallbackQueryHandler(adm_add_test_correct_cb, pattern=r"^test_cor_[ABCD]$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_test_correct_text),
            ],
            WAITING_TEST_SUBJECT: [
                CallbackQueryHandler(adm_add_test_subject, pattern=r"^test_sub_\d+$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_test_flow)],
    )

    # Handlerlarni ro'yxatga olish
    app.add_handler(storage_conv)
    app.add_handler(db_upload_conv)
    app.add_handler(bg_conv)
    app.add_handler(test_conv)
    app.add_handler(vip_conv)
    app.add_handler(bonus_conv)
    app.add_handler(seturl_conv)
    app.add_handler(broadcast_conv)

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("chatid", chatid_command))
    app.add_handler(CommandHandler("bg", handle_bg_code_lookup))

    app.add_handler(CallbackQueryHandler(admin_callbacks, pattern=r"^adm_"))
    app.add_handler(CallbackQueryHandler(user_callbacks, pattern=r"^(u_|open_app_alert)"))

    # Fon kodlarini (masalan '1bg', '2bg') matn orqali tezkor qidirish
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Xabar pin qilingandagi Telegram tizim xabarlarini tozalash
    app.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, handle_pinned_service_message))

    # PTB JobQueue orqali har 1 soatda zaxiralash
    if app.job_queue is not None:
        app.job_queue.run_repeating(backup_database_job, interval=3600, first=60)
        logger.info("PTB JobQueue orqali har 1 soatlik avtomatik zaxiralash rejalashtirildi.")

    # Kunlik missiyalarni yaratish (bot ishga tushganda va har kuni GMT+5 00:00 da)
    generate_daily_missions()
    def _daily_mission_reset_loop():
        import time
        while True:
            now_gmt5 = datetime.datetime.utcnow() + datetime.timedelta(hours=5)
            tomorrow = (now_gmt5 + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            wait_secs = (tomorrow - now_gmt5).total_seconds()
            time.sleep(max(wait_secs, 60))
            generate_daily_missions()
            logger.info("Kunlik missiyalar GMT+5 00:00 da yangilandi.")
    import threading as _thr
    _thr.Thread(target=_daily_mission_reset_loop, daemon=True, name="DailyMissionReset").start()
    logger.info("Kunlik missiya reset sikli ishga tushdi (har kuni GMT+5 00:00).")

    logger.info("SARN Telegram Boti (@sarnuzbot) ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
