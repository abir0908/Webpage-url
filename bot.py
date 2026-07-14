"""
NR Number Bot - Professional Edition
====================================
Complete rewrite with all requested features:
- 100% English, Bold text
- Web App for colored buttons (matches screenshots)
- 6 Main Menu buttons: GET NUMBER, BALANCE, REFER & EARN, SUPPORT, LEADERBOARD, METHOD
- Admin Panel with Manage Method Channel
- Force Join with colored styling
- GET NUMBER flow: Service -> Country -> Number display
- BALANCE: Integrated withdraw (Bkash, Nagad, Rocket, Binance)
- LEADERBOARD: Top 10 by OTP count
- METHOD: Optional channel join
- All original features preserved
"""

import os
import re
import time
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

import requests
from telebot import TeleBot, types

# =====================================================================
# 1. CONFIGURATION (environment variables)
# =====================================================================
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val


BOT_TOKEN = _require_env("BOT_TOKEN")
OWNER_ID = int(_require_env("OWNER_ID"))
ADMIN_GROUP_ID = int(_require_env("ADMIN_GROUP_ID"))
WITHDRAW_GROUP_ID = int(_require_env("WITHDRAW_GROUP_ID"))

DB_NAME = os.environ.get("DB_PATH", "bot_master_database.db")
WEB_APP_URL = os.environ.get("WEB_APP_URL", "")

_seed_channels_raw = os.environ.get("SEED_CHANNELS", "")
CHANNELS_TO_JOIN = []
if _seed_channels_raw:
    for pair in _seed_channels_raw.split(","):
        if "|" in pair:
            uname, link = pair.split("|", 1)
            CHANNELS_TO_JOIN.append({"username": uname.strip(), "link": link.strip()})

# =====================================================================
# 2. LOGGING
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("nr_bot")

# =====================================================================
# 3. THREAD-SAFE DATABASE LAYER
# =====================================================================
DB_LOCK = threading.RLock()


@contextmanager
def db_cursor(commit: bool = False):
    with DB_LOCK:
        conn = sqlite3.connect(DB_NAME, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        cur = conn.cursor()
        try:
            yield cur
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db():
    with db_cursor(commit=True) as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                name TEXT,
                balance REAL DEFAULT 0.0,
                referred_by INTEGER DEFAULT 0,
                otp_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'Active',
                is_admin INTEGER DEFAULT 0,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS active_numbers (
                number TEXT PRIMARY KEY,
                user_id INTEGER,
                range_code TEXT,
                service TEXT,
                country TEXT,
                allocated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS withdraws (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                method TEXT,
                wallet_number TEXT,
                status TEXT DEFAULT 'Pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                icon TEXT DEFAULT '📱',
                status TEXT DEFAULT 'ON'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS countries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                flag TEXT DEFAULT '🌐',
                range_code TEXT NOT NULL,
                status TEXT DEFAULT 'ON',
                UNIQUE(service_id, name)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                link TEXT,
                status TEXT DEFAULT 'ON'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS method_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT,
                status TEXT DEFAULT 'ON'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fsm_state (
                user_id INTEGER,
                chat_id INTEGER,
                state TEXT,
                data TEXT,
                PRIMARY KEY (user_id, chat_id)
            )
        """)

        default_settings = [
            ("api_url", "https://api.zenexnetwork.com"),
            ("api_key", ""),
            ("maintenance", "OFF"),
            ("bonus_amount", "30.0"),
            ("required_otp", "50"),
            ("referral_status", "ON"),
            ("min_withdraw", "0.5"),
            ("max_withdraw", "5000.0"),
            ("withdraw_status", "ON"),
            ("bkash_status", "ON"),
            ("nagad_status", "ON"),
            ("rocket_status", "ON"),
            ("binance_status", "ON"),
            ("admin_1_user", ""),
            ("admin_2_user", ""),
            ("otp_group_link", ""),
            ("method_channel_link", ""),
            ("welcome_msg", "✨✨ *WELCOME TO NR NUMBER BOT* ✨✨\n━━━━━━━━━━━━━━━━━━\n🚀 *START INSTANT OTP RECEPTION NOW!* 🚀\n🔹 *PLEASE USE THE BUTTONS BELOW :*"),
        ]
        for key, val in default_settings:
            cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))

        cur.execute("SELECT COUNT(*) FROM services")
        if cur.fetchone()[0] == 0:
            cur.executemany("INSERT INTO services (name) VALUES (?)",
                             [("Facebook",), ("Instagram",), ("Telegram",), ("WhatsApp",), ("Google",)])

        for ch in CHANNELS_TO_JOIN:
            cur.execute("INSERT OR IGNORE INTO channels (username, link) VALUES (?, ?)",
                        (ch["username"], ch["link"]))


def get_setting(key: str, default=None):
    with db_cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
    return row[0] if row else default


def update_setting(key: str, value):
    with db_cursor(commit=True) as cur:
        cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))


# =====================================================================
# 4. FSM (Finite State Machine)
# =====================================================================
def fsm_set_state(user_id: int, chat_id: int, state):
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT data FROM fsm_state WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
        row = cur.fetchone()
        data = row[0] if row else "{}"
        cur.execute(
            "INSERT INTO fsm_state (user_id, chat_id, state, data) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, chat_id) DO UPDATE SET state = excluded.state",
            (user_id, chat_id, state, data),
        )


def fsm_add_data(user_id: int, chat_id: int, **kwargs):
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT state, data FROM fsm_state WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
        row = cur.fetchone()
        state = row[0] if row else None
        data = json.loads(row[1]) if row and row[1] else {}
        data.update(kwargs)
        cur.execute(
            "INSERT INTO fsm_state (user_id, chat_id, state, data) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, chat_id) DO UPDATE SET data = excluded.data",
            (user_id, chat_id, state, json.dumps(data)),
        )


def fsm_get_data(user_id: int, chat_id: int) -> dict:
    with db_cursor() as cur:
        cur.execute("SELECT data FROM fsm_state WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
        row = cur.fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def fsm_get_state(user_id: int, chat_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT state FROM fsm_state WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
        row = cur.fetchone()
    return row[0] if row else None


def fsm_clear(user_id: int, chat_id: int):
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM fsm_state WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))


# =====================================================================
# 5. TEXT HELPERS
# =====================================================================
_MD_ESCAPE_RE = re.compile(r"([_*`\[\]])")


def esc_md(text) -> str:
    if text is None:
        return ""
    return _MD_ESCAPE_RE.sub(r"\\\1", str(text))


_OTP_CODE_RE = re.compile(r"(?<!\d)\d{4,8}(?!\d)")


def extract_otp_code(raw_message: str) -> str:
    if not raw_message:
        return ""
    match = _OTP_CODE_RE.search(raw_message)
    return match.group(0) if match else raw_message.strip()


def safe_send(chat_id, text, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logger.warning(f"send_message failed for chat_id={chat_id}: {e}")
        return None


# =====================================================================
# 6. BOT + FSM STATES
# =====================================================================
bot = TeleBot(BOT_TOKEN, parse_mode="Markdown")


class BotStates:
    VERIFY_PENDING = "VERIFY_PENDING"
    MAIN_MENU = "MAIN_MENU"
    WAITING_WITHDRAW_NUMBER = "WAITING_WITHDRAW_NUMBER"
    WAITING_WITHDRAW_AMOUNT = "WAITING_WITHDRAW_AMOUNT"
    SETTING_API_URL = "SETTING_API_URL"
    SETTING_API_KEY = "SETTING_API_KEY"
    ADDING_SERVICE = "ADDING_SERVICE"
    ADDING_COUNTRY = "ADDING_COUNTRY"
    ADDING_COUNTRY_RANGE = "ADDING_COUNTRY_RANGE"
    ADDING_COUNTRY_FLAG = "ADDING_COUNTRY_FLAG"
    ADDING_CHANNEL = "ADDING_CHANNEL"
    UP_ADMIN1 = "UP_ADMIN1"
    UP_ADMIN2 = "UP_ADMIN2"
    SET_REF_BONUS = "SET_REF_BONUS"
    SET_REF_REQ_OTP = "SET_REF_REQ_OTP"
    SET_MIN_WD = "SET_MIN_WD"
    SET_MAX_WD = "SET_MAX_WD"
    BROADCASTING_MSG = "BROADCASTING_MSG"
    USER_MANAGE_SEARCH = "USER_MANAGE_SEARCH"
    USER_MANAGE_BAL_ADD = "USER_MANAGE_BAL_ADD"
    USER_MANAGE_BAL_REM = "USER_MANAGE_BAL_REM"
    ADDING_BOT_ADMIN = "ADDING_BOT_ADMIN"
    SETTING_OTP_GROUP = "SETTING_OTP_GROUP"
    SETTING_METHOD_CHANNEL = "SETTING_METHOD_CHANNEL"


def is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    with db_cursor() as cur:
        cur.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
    return bool(row and row[0] == 1)


def reset_to_main_menu(user_id, chat_id):
    fsm_clear(user_id, chat_id)
    fsm_set_state(user_id, chat_id, BotStates.MAIN_MENU)


# =====================================================================
# 7. FORCE-JOIN / CHANNEL VERIFICATION
# =====================================================================
def get_active_channels():
    with db_cursor() as cur:
        cur.execute("SELECT username, link FROM channels WHERE status = 'ON'")
        return cur.fetchall()


def check_user_joined(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    ch_list = get_active_channels()
    if not ch_list:
        return True
    for ch_user, _ in ch_list:
        try:
            member = bot.get_chat_member(ch_user, user_id)
            if member.status in ("left", "kicked"):
                return False
        except Exception as e:
            logger.warning(f"get_chat_member failed for {ch_user}: {e}")
            return False
    return True


def get_force_join_keyboard():
    ch_list = get_active_channels()
    markup = types.InlineKeyboardMarkup(row_width=1)
    for i, (_, link) in enumerate(ch_list, start=1):
        markup.add(types.InlineKeyboardButton(text=f"🔗 Join Channel {i}", url=link))
    markup.add(types.InlineKeyboardButton(text="✅ Check Joined", callback_data="verify"))
    return markup


# =====================================================================
# 8. WEB APP HTML FOR COLORED BUTTONS
# =====================================================================
WEB_APP_HTML = r"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NR Number Bot</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            min-height: 100vh;
            padding: 20px;
            color: white;
        }
        .container { max-width: 400px; margin: 0 auto; }
        .header {
            text-align: center;
            padding: 20px 0;
            border-bottom: 2px solid #e94560;
            margin-bottom: 20px;
        }
        .header h1 { font-size: 24px; margin-bottom: 5px; }
        .header p { color: #a0a0a0; font-size: 14px; }
        .menu-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            margin-bottom: 20px;
        }
        .btn {
            border: none;
            padding: 18px 15px;
            border-radius: 15px;
            font-size: 15px;
            font-weight: bold;
            cursor: pointer;
            transition: all 0.3s ease;
            text-align: center;
            color: white;
            text-shadow: 0 1px 2px rgba(0,0,0,0.3);
            box-shadow: 0 4px 15px rgba(0,0,0,0.2);
        }
        .btn:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(0,0,0,0.3); }
        .btn:active { transform: translateY(0); }
        .btn-green {
            background: linear-gradient(135deg, #2ecc71 0%, #27ae60 100%);
            box-shadow: 0 4px 15px rgba(46, 204, 113, 0.4);
        }
        .btn-blue {
            background: linear-gradient(135deg, #3498db 0%, #2980b9 100%);
            box-shadow: 0 4px 15px rgba(52, 152, 219, 0.4);
        }
        .btn-purple {
            background: linear-gradient(135deg, #9b59b6 0%, #8e44ad 100%);
            box-shadow: 0 4px 15px rgba(155, 89, 182, 0.4);
        }
        .btn-full { grid-column: 1 / -1; }
        .emoji { font-size: 20px; margin-right: 5px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🚀 NR NUMBER BOT</h1>
            <p>Instant OTP Reception</p>
        </div>
        <div class="menu-grid">
            <button class="btn btn-green" onclick="sendData('get_number')">
                <span class="emoji">📞</span>GET NUMBER
            </button>
            <button class="btn btn-blue" onclick="sendData('balance')">
                <span class="emoji">💰</span>BALANCE
            </button>
            <button class="btn btn-green" onclick="sendData('refer')">
                <span class="emoji">👥</span>REFER & EARN
            </button>
            <button class="btn btn-blue" onclick="sendData('support')">
                <span class="emoji">💬</span>SUPPORT
            </button>
            <button class="btn btn-blue" onclick="sendData('leaderboard')">
                <span class="emoji">🏆</span>LEADERBOARD
            </button>
            <button class="btn btn-green" onclick="sendData('method')">
                <span class="emoji">📊</span>METHOD
            </button>
        </div>
        <button class="btn btn-purple btn-full" onclick="sendData('admin')">
            <span class="emoji">👑</span>ADMIN PANEL
        </button>
    </div>
    <script>
        function sendData(action) {
            if (window.Telegram && window.Telegram.WebApp) {
                window.Telegram.WebApp.sendData(action);
                window.Telegram.WebApp.close();
            } else {
                alert('Please open this in Telegram WebApp');
            }
        }
        if (window.Telegram && window.Telegram.WebApp) {
            window.Telegram.WebApp.ready();
            window.Telegram.WebApp.expand();
        }
    </script>
</body>
</html>"""


# =====================================================================
# 9. MAIN MENU
# =====================================================================
def show_main_menu(chat_id, user_id):
    web_app_url = WEB_APP_URL

    if web_app_url:
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton(
            text="🚀 OPEN COLORED MENU",
            web_app=types.WebAppInfo(url=web_app_url)
        ))

        reply_markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        reply_markup.add(
            types.KeyboardButton("📞 GET NUMBER"),
            types.KeyboardButton("💰 BALANCE")
        )
        reply_markup.add(
            types.KeyboardButton("👥 REFER & EARN"),
            types.KeyboardButton("💬 SUPPORT")
        )
        reply_markup.add(
            types.KeyboardButton("🏆 LEADERBOARD"),
            types.KeyboardButton("📊 METHOD")
        )
        if is_admin(user_id):
            reply_markup.add(types.KeyboardButton("👑 ADMIN PANEL"))

        welcome_text = get_setting("welcome_msg")
        safe_send(chat_id, welcome_text, reply_markup=reply_markup)
        safe_send(chat_id, "*🎨 FOR COLORED BUTTONS, TAP BELOW:*", reply_markup=markup)
    else:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        markup.add(
            types.KeyboardButton("📞 GET NUMBER"),
            types.KeyboardButton("💰 BALANCE")
        )
        markup.add(
            types.KeyboardButton("👥 REFER & EARN"),
            types.KeyboardButton("💬 SUPPORT")
        )
        markup.add(
            types.KeyboardButton("🏆 LEADERBOARD"),
            types.KeyboardButton("📊 METHOD")
        )
        if is_admin(user_id):
            markup.add(types.KeyboardButton("👑 ADMIN PANEL"))

        welcome_text = get_setting("welcome_msg")
        safe_send(chat_id, welcome_text, reply_markup=markup)


# =====================================================================
# 10. NUMBER PANEL API LAYER
# =====================================================================
class XeronError(Exception):
    pass


def _xeron_headers():
    return {"mapikey": get_setting("api_key", ""), "Content-Type": "application/json"}


def fetch_numbers_from_xeron(range_prefix: str, user_id: int, service: str, country: str, batch_size: int = 2):
    url = f"{get_setting('api_url')}/v1/getnum"
    numbers = []
    error_message = None

    for _ in range(batch_size):
        try:
            res = requests.post(
                url,
                headers=_xeron_headers(),
                json={"range": f"{range_prefix}XXX", "is_national": False, "remove_plus": False},
                timeout=10,
            )
        except requests.RequestException as e:
            logger.error(f"Number panel network error: {e}")
            error_message = "NETWORK ERROR - CANNOT REACH PANEL."
            break

        if res.status_code != 200:
            logger.error(f"Number panel HTTP {res.status_code}: {res.text[:300]}")
            error_message = f"PANEL ERROR (HTTP {res.status_code})."
            break

        try:
            res_data = res.json()
        except ValueError:
            logger.error("Number panel returned non-JSON response")
            error_message = "UNEXPECTED RESPONSE FROM PANEL."
            break

        meta_code = res_data.get("meta", {}).get("code")
        if meta_code == 200 and res_data.get("data"):
            full_num = res_data["data"].get("full_number")
            if full_num:
                with db_cursor(commit=True) as cur:
                    cur.execute(
                        "INSERT OR REPLACE INTO active_numbers (number, user_id, range_code, service, country) VALUES (?, ?, ?, ?, ?)",
                        (full_num, user_id, range_prefix, service, country),
                    )
                numbers.append(full_num)
            else:
                error_message = "NO NUMBER FOUND IN PANEL RESPONSE."
                break
        else:
            error_message = res_data.get("message") or "PANEL FAILED TO PROVIDE NUMBER."
            break

    return numbers, error_message


# =====================================================================
# 11. OTP BACKGROUND POLLING ENGINE
# =====================================================================
def process_referral_bonus(cur, uid):
    cur.execute("SELECT referred_by, otp_count FROM users WHERE user_id = ?", (uid,))
    row = cur.fetchone()
    if not row or not row[0]:
        return
    ref_by, otp_count = row
    req_otp = int(get_setting("required_otp", "50"))
    bonus = float(get_setting("bonus_amount", "30.0"))
    if otp_count == req_otp:
        cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (bonus, ref_by))
        safe_send(
            ref_by,
            f"🎁 *REFERRAL BONUS RECEIVED!*\n\n"
            f"YOUR REFERRED USER COMPLETED {req_otp} OTPS.\n"
            f"*+{bonus} BDT* ADDED TO YOUR BALANCE.",
        )


def otp_polling_loop():
    while True:
        try:
            with db_cursor() as cur:
                cur.execute("SELECT number, user_id, service, country FROM active_numbers")
                tracked = cur.fetchall()

            if tracked:
                url = f"{get_setting('api_url')}/v1/numsuccess/info"
                res = requests.get(url, headers=_xeron_headers(), timeout=10)
                if res.status_code == 200:
                    otps = res.json().get("data", {}).get("otps", [])
                    tracked_map = {n: (uid, srv, cnt) for n, uid, srv, cnt in tracked}

                    for otp in otps:
                        api_num = str(otp.get("number", "")).lstrip("+")
                        raw_message = otp.get("otp", "")
                        if api_num not in tracked_map:
                            continue
                        uid, srv, cnt = tracked_map[api_num]
                        code = extract_otp_code(raw_message)

                        text = (
                            f"🔐 *OTP RECEIVED!*\n\n"
                            f"📱 *NUMBER:* `+{api_num}`\n"
                            f"🔑 *CODE:* `{esc_md(code)}`"
                        )
                        safe_send(uid, text)

                        with db_cursor(commit=True) as cur:
                            cur.execute("UPDATE users SET otp_count = otp_count + 1 WHERE user_id = ?", (uid,))
                            cur.execute("DELETE FROM active_numbers WHERE number = ?", (api_num,))
                            process_referral_bonus(cur, uid)
        except Exception as e:
            logger.error(f"OTP polling loop exception: {e}")
        time.sleep(5)


# =====================================================================
# 12. COMMANDS
# =====================================================================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    username = message.from_user.username or "NoUsername"
    name = message.from_user.first_name or "NoName"

    ref_by = 0
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        potential_ref = int(args[1])
        if potential_ref != user_id:
            ref_by = potential_ref

    with db_cursor(commit=True) as cur:
        cur.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
        existing = cur.fetchone()
        if existing:
            if existing[0] == "Banned":
                safe_send(chat_id, "❌ *YOU ARE BANNED FROM THIS BOT.*")
                return
        else:
            admin_flag = 1 if user_id == OWNER_ID else 0
            cur.execute(
                "INSERT INTO users (user_id, username, name, referred_by, is_admin) VALUES (?, ?, ?, ?, ?)",
                (user_id, username, name, ref_by, admin_flag),
            )

    if get_setting("maintenance") == "ON" and user_id != OWNER_ID:
        safe_send(chat_id, "⚙️ *SORRY, BOT IS CURRENTLY UNDER MAINTENANCE.*")
        return

    if check_user_joined(user_id):
        fsm_set_state(user_id, chat_id, BotStates.MAIN_MENU)
        show_main_menu(chat_id, user_id)
    else:
        fsm_set_state(user_id, chat_id, BotStates.VERIFY_PENDING)
        safe_send(
            chat_id,
            "🔴 *PLEASE JOIN OUR CHANNELS TO USE THE BOT!*",
            reply_markup=get_force_join_keyboard(),
        )


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    reset_to_main_menu(message.from_user.id, message.chat.id)
    bot.reply_to(message, "✅ *CANCELLED. RETURNED TO MAIN MENU.*")
    show_main_menu(message.chat.id, message.from_user.id)


# =====================================================================
# 13. INLINE CALLBACK ROUTER
# =====================================================================
@bot.callback_query_handler(func=lambda call: True)
def global_callback_handler(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = call.data

    try:
        if data == "verify":
            if check_user_joined(user_id):
                bot.answer_callback_query(call.id, text="✅ VERIFICATION SUCCESSFUL!")
                try:
                    bot.delete_message(chat_id, msg_id)
                except Exception:
                    pass
                fsm_set_state(user_id, chat_id, BotStates.MAIN_MENU)
                show_main_menu(chat_id, user_id)
            else:
                bot.answer_callback_query(call.id, text="❌ YOU MUST JOIN ALL CHANNELS FIRST.", show_alert=True)

        elif data.startswith("svc:"):
            srv_id = int(data.split(":")[1])
            with db_cursor() as cur:
                cur.execute("SELECT name FROM services WHERE id = ?", (srv_id,))
                row = cur.fetchone()
            if not row:
                bot.answer_callback_query(call.id, text="❌ SERVICE NOT FOUND.", show_alert=True)
                return
            srv_name = row[0]
            fsm_add_data(user_id, chat_id, srv=srv_name)

            with db_cursor() as cur:
                cur.execute(
                    "SELECT id, name, flag FROM countries WHERE service_id = ? AND status = 'ON' ORDER BY name",
                    (srv_id,),
                )
                cnts = cur.fetchall()

            if not cnts:
                bot.answer_callback_query(call.id, text="❌ NO COUNTRIES CONFIGURED FOR THIS SERVICE.", show_alert=True)
                return

            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(*[types.InlineKeyboardButton(text=f"{flag} {name}", callback_data=f"cty:{cid}")
                         for cid, name, flag in cnts])
            markup.add(types.InlineKeyboardButton(text="« Back", callback_data="back_to_services"))
            bot.edit_message_text(
                f"🌍 *SELECT COUNTRY FOR {esc_md(srv_name)}:*",
                chat_id, msg_id, reply_markup=markup
            )
            bot.answer_callback_query(call.id)

        elif data == "back_to_services":
            with db_cursor() as cur:
                cur.execute("SELECT id, name, icon FROM services WHERE status = 'ON'")
                srvs = cur.fetchall()
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(*[types.InlineKeyboardButton(text=f"{icon} {name}", callback_data=f"svc:{sid}")
                         for sid, name, icon in srvs])
            bot.edit_message_text(
                "📞 *SELECT APP TO GET NUMBER:*\n━━━━━━━━━━━━━━━━━━",
                chat_id, msg_id, reply_markup=markup
            )
            bot.answer_callback_query(call.id)

        elif data.startswith("cty:") or data == "rng_retry":
            if data.startswith("cty:"):
                cty_id = int(data.split(":")[1])
                with db_cursor() as cur:
                    cur.execute("SELECT name, flag, range_code FROM countries WHERE id = ?", (cty_id,))
                    row = cur.fetchone()
                if not row:
                    bot.answer_callback_query(call.id, text="❌ COUNTRY NOT FOUND.", show_alert=True)
                    return
                cnt_name, cnt_flag, range_code = row
                fsm_add_data(user_id, chat_id, cnt=cnt_name, flag=cnt_flag, rng=range_code)
            else:
                u_data = fsm_get_data(user_id, chat_id)
                cnt_name = u_data.get("cnt", "Unknown")
                cnt_flag = u_data.get("flag", "🌐")
                range_code = u_data.get("rng")

            u_data = fsm_get_data(user_id, chat_id)
            srv = u_data.get("srv", "Unknown")

            bot.answer_callback_query(call.id)
            try:
                bot.edit_message_text(
                    "⚡ *GENERATING NUMBERS FROM PANEL, PLEASE WAIT...*", chat_id, msg_id
                )
            except Exception:
                pass

            numbers, error_message = fetch_numbers_from_xeron(range_code, user_id, srv, cnt_name)

            if not numbers:
                reason = error_message or "TEMPORARY PANEL ERROR! PLEASE TRY AGAIN."
                retry_markup = types.InlineKeyboardMarkup(row_width=1)
                retry_markup.add(types.InlineKeyboardButton(text="🔄 TRY AGAIN", callback_data="rng_retry"))
                retry_markup.add(types.InlineKeyboardButton(text="« Back", callback_data="back_to_services"))
                bot.edit_message_text(f"❌ *{esc_md(reason)}*", chat_id, msg_id, reply_markup=retry_markup)
                return

            action_buttons = [
                types.InlineKeyboardButton(text="🔄 Change Number", callback_data="rng_retry"),
            ]
            otp_group_link = get_setting("otp_group_link")
            if otp_group_link:
                action_buttons.append(types.InlineKeyboardButton(text="📋 OTP Group ↗️", url=otp_group_link))

            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(*action_buttons)
            markup.add(types.InlineKeyboardButton(text="« Back", callback_data="back_to_services"))

            num_lines = "\n".join(f"📞 Number {i+1}: `+{n}`" for i, n in enumerate(numbers))
            note = "\n\n⚠️ *LOW STOCK - FEWER NUMBERS PROVIDED.*" if len(numbers) < 2 else ""

            bot.edit_message_text(
                f"✅ *YOUR NUMBER DETAILS* ✅\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"━━━━━━\n\n"
                f"📱 *APP:* {esc_md(srv)} \"\n"
                f"🌍 *COUNTRY:* {cnt_flag} {esc_md(cnt_name)} \"\n"
                f"📦 *NUMBERS RECEIVED:*\n"
                f"{num_lines}\n"
                f"\"\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"━━━━━━\n"
                f"💌 *SMS STATUS:* 20 MINUTES REMAINING ⏳ ...",
                chat_id, msg_id, reply_markup=markup,
            )

        elif data.startswith("wd:"):
            method = data.split(":", 1)[1]
            fsm_add_data(user_id, chat_id, wd_method=method)
            fsm_set_state(user_id, chat_id, BotStates.WAITING_WITHDRAW_NUMBER)
            safe_send(chat_id, f"📱 *ENTER YOUR {esc_md(method)} NUMBER:*")
            bot.answer_callback_query(call.id)

        elif data.startswith("awd:"):
            handle_admin_withdraw_action(call, data)

        elif data.startswith("anav:"):
            handle_admin_navigation(call, data.split(":", 1)[1])

        elif data.startswith("cadd_svc:"):
            svc_id = int(data.split(":")[1])
            with db_cursor() as cur:
                cur.execute("SELECT name FROM services WHERE id = ?", (svc_id,))
                row = cur.fetchone()
            if not row:
                bot.answer_callback_query(call.id, text="❌ SERVICE NOT FOUND.", show_alert=True)
                return
            fsm_add_data(user_id, chat_id, new_country_service_id=svc_id, new_country_service_name=row[0])
            fsm_set_state(user_id, chat_id, BotStates.ADDING_COUNTRY)
            bot.answer_callback_query(call.id)
            safe_send(chat_id, f"➕ *ENTER NEW COUNTRY NAME FOR {esc_md(row[0])}:*")

        elif data.startswith("atoggle:") or data.startswith("adel:"):
            handle_admin_item_action(call, data)

        elif data.startswith("auser:"):
            handle_admin_user_action(call, data)

        elif data.startswith("arm_admin:"):
            handle_remove_admin(call, data)

        else:
            bot.answer_callback_query(call.id)

    except Exception as e:
        logger.exception(f"Callback handler error for data={data}: {e}")
        try:
            bot.answer_callback_query(call.id, text="⚠️ AN ERROR OCCURRED. PLEASE TRY AGAIN.", show_alert=True)
        except Exception:
            pass


def handle_admin_withdraw_action(call, data):
    _, action, wd_id_str = data.split(":")
    wd_id = int(wd_id_str)
    chat_id, msg_id = call.message.chat.id, call.message.message_id

    with db_cursor(commit=True) as cur:
        cur.execute("SELECT user_id, amount, method, wallet_number, status FROM withdraws WHERE id = ?", (wd_id,))
        wd = cur.fetchone()
        if not wd or wd[4] != "Pending":
            bot.answer_callback_query(call.id, text="⚠️ THIS REQUEST HAS ALREADY BEEN HANDLED.")
            return

        u_id, amt, meth, w_num = wd[0], wd[1], wd[2], wd[3]

        if action == "approve":
            cur.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                (amt, u_id, amt),
            )
            if cur.rowcount == 0:
                cur.execute("UPDATE withdraws SET status = 'Rejected' WHERE id = ?", (wd_id,))
                bot.edit_message_text(
                    f"💳 *WITHDRAW AUTO-REJECTED (INSUFFICIENT BALANCE)*\n\n"
                    f"👤 USER ID: {u_id}\n💵 AMOUNT: {amt} BDT",
                    chat_id, msg_id,
                )
                safe_send(u_id, "❌ *YOUR WITHDRAW REQUEST WAS CANCELLED DUE TO INSUFFICIENT BALANCE.*")
                return

            cur.execute("UPDATE withdraws SET status = 'Approved' WHERE id = ?", (wd_id,))
            bot.edit_message_text(
                f"💳 *WITHDRAW REQUEST APPROVED*\n\n"
                f"👤 USER ID: {u_id}\n💵 AMOUNT: {amt} BDT\n"
                f"📱 NUMBER: {esc_md(w_num)}\n💳 METHOD: {meth}\n\n"
                f"STATUS: ✅ APPROVED",
                chat_id, msg_id,
            )
            safe_send(u_id, "🎉 *YOUR WITHDRAW REQUEST IS APPROVED. THANK YOU!*")

        elif action == "reject":
            cur.execute("UPDATE withdraws SET status = 'Rejected' WHERE id = ?", (wd_id,))
            cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amt, u_id))
            bot.edit_message_text(
                f"💳 *WITHDRAW REQUEST REJECTED*\n\n"
                f"👤 USER ID: {u_id}\n💵 AMOUNT: {amt} BDT\n"
                f"📱 NUMBER: {esc_md(w_num)}\n💳 METHOD: {meth}\n\n"
                f"STATUS: ❌ REJECTED\n"
                f"💰 BALANCE REFUNDED",
                chat_id, msg_id,
            )
            safe_send(u_id, "❌ *YOUR WITHDRAW REQUEST IS REJECTED. BALANCE REFUNDED.*")

    bot.answer_callback_query(call.id)


# =====================================================================
# 14. WITHDRAW INPUT FLOW
# =====================================================================
def withdraw_number_input(message):
    user_id, chat_id = message.from_user.id, message.chat.id
    fsm_add_data(user_id, chat_id, wd_num=message.text.strip())
    fsm_set_state(user_id, chat_id, BotStates.WAITING_WITHDRAW_AMOUNT)
    bot.reply_to(message, "💵 *ENTER WITHDRAWAL AMOUNT:*")


def withdraw_amount_input(message):
    user_id, chat_id = message.from_user.id, message.chat.id
    amount_text = message.text.strip()

    try:
        amt = float(amount_text)
        if amt <= 0:
            raise ValueError
    except ValueError:
        bot.reply_to(message, "❌ *PLEASE ENTER A VALID POSITIVE NUMBER.*")
        return

    min_wd = float(get_setting("min_withdraw", "0.5"))
    max_wd = float(get_setting("max_withdraw", "5000"))
    if amt < min_wd or amt > max_wd:
        bot.reply_to(message, f"❌ *WITHDRAW MUST BE BETWEEN {min_wd}$ AND {max_wd}$.*")
        return

    with db_cursor() as cur:
        cur.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        bal = cur.fetchone()[0]
    if bal < amt:
        bot.reply_to(message, "❌ *INSUFFICIENT BALANCE.*")
        return

    u_data = fsm_get_data(user_id, chat_id)
    meth = u_data.get("wd_method")
    w_num = u_data.get("wd_num")

    with db_cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO withdraws (user_id, amount, method, wallet_number) VALUES (?, ?, ?, ?)",
            (user_id, amt, meth, w_num),
        )
        wd_id = cur.lastrowid
        cur.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amt, user_id))

    reset_to_main_menu(user_id, chat_id)
    bot.reply_to(message, "✅ *WITHDRAW REQUEST SUBMITTED. PENDING ADMIN APPROVAL.*")

    adm_markup = types.InlineKeyboardMarkup()
    adm_markup.add(
        types.InlineKeyboardButton(text="✅ APPROVE", callback_data=f"awd:approve:{wd_id}"),
        types.InlineKeyboardButton(text="❌ REJECT", callback_data=f"awd:reject:{wd_id}"),
    )
    adm_text = (
        f"📥 *NEW WITHDRAW REQUEST*\n\n"
        f"👤 *USER:* `{user_id}`\n💵 *AMOUNT:* {amt}$\n"
        f"📱 *NUMBER:* `{esc_md(w_num)}`\n💳 *METHOD:* {meth}\n"
        f"STATUS: 🟡 PENDING"
    )
    try:
        bot.send_message(WITHDRAW_GROUP_ID, adm_text, reply_markup=adm_markup)
    except Exception as e:
        logger.error(f"Failed to send alert to Withdraw Group: {e}")


# =====================================================================
# 15. ADMIN DASHBOARD
# =====================================================================
def send_admin_panel_dashboard(chat_id, user_id=None):
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM withdraws WHERE status = 'Pending'")
        pending_wd = cur.fetchone()[0]
        cur.execute("SELECT SUM(otp_count) FROM users")
        total_otp = cur.fetchone()[0] or 0

    dash_text = (
        f"👑 *ADMIN CONTROL PANEL*\n\n"
        f"📊 *DASHBOARD STATISTICS:*\n"
        f"🔹 TOTAL USERS: {total_users}\n"
        f"🔹 TOTAL PROCESSED OTP: {total_otp}\n"
        f"🔹 PENDING WITHDRAWS: {pending_wd}\n"
        f"⚙️ MAINTENANCE MODE: {get_setting('maintenance')}\n\n"
        f"*SELECT CATEGORY BELOW:*"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(text="👥 USER MANAGE", callback_data="anav:user"),
        types.InlineKeyboardButton(text="📡 API SETTINGS", callback_data="anav:api"),
    )
    markup.add(
        types.InlineKeyboardButton(text="📱 SERVICES", callback_data="anav:services"),
        types.InlineKeyboardButton(text="🌐 COUNTRIES", callback_data="anav:countries"),
    )
    markup.add(
        types.InlineKeyboardButton(text="📢 FORCE JOIN", callback_data="anav:fjoin"),
        types.InlineKeyboardButton(text="📣 OTP GROUP", callback_data="anav:otpgroup"),
    )
    markup.add(
        types.InlineKeyboardButton(text="💬 SUPPORT SET", callback_data="anav:supp"),
        types.InlineKeyboardButton(text="🎁 REFERRAL SET", callback_data="anav:ref"),
    )
    markup.add(
        types.InlineKeyboardButton(text="💳 WITHDRAW SET", callback_data="anav:wdset"),
        types.InlineKeyboardButton(text="📨 BROADCAST", callback_data="anav:bcast"),
    )
    markup.add(
        types.InlineKeyboardButton(text="📊 METHOD CHANNEL", callback_data="anav:methodch"),
        types.InlineKeyboardButton(text="⚙️ TOGGLE MAINTENANCE", callback_data="anav:togg_maint"),
    )
    if user_id == OWNER_ID:
        markup.add(types.InlineKeyboardButton(text="🛡️ MANAGE ADMINS", callback_data="anav:admins"))
    safe_send(chat_id, dash_text, reply_markup=markup)


def _list_items_markup(table, id_col, label_col, name_prefix=""):
    with db_cursor() as cur:
        cur.execute(f"SELECT {id_col}, {label_col}, status FROM {table} ORDER BY {id_col}")
        rows = cur.fetchall()
    markup = types.InlineKeyboardMarkup(row_width=1)
    for rid, label, status in rows:
        icon = "🟢" if status == "ON" else "🔴"
        markup.add(
            types.InlineKeyboardButton(text=f"{icon} {name_prefix}{label}", callback_data=f"atoggle:{table}:{rid}"),
        )
        markup.add(types.InlineKeyboardButton(text=f"🗑 DELETE {label}", callback_data=f"adel:{table}:{rid}"))
    return markup, rows


def handle_admin_navigation(call, target, answer_callback=True):
    chat_id, msg_id, user_id = call.message.chat.id, call.message.message_id, call.from_user.id

    if target == "togg_maint":
        new_st = "ON" if get_setting("maintenance") == "OFF" else "OFF"
        update_setting("maintenance", new_st)
        bot.answer_callback_query(call.id, text=f"MAINTENANCE MODE: {new_st}")
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        send_admin_panel_dashboard(chat_id, user_id)

    elif target == "api":
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(text="🔗 CHANGE API URL", callback_data="anav:seturl"))
        markup.add(types.InlineKeyboardButton(text="🔑 CHANGE API KEY", callback_data="anav:setkey"))
        api_key = get_setting("api_key") or "(NOT SET)"
        bot.edit_message_text(
            f"📡 *API SETTINGS PANEL*\n\nURL: `{get_setting('api_url')}`\nKEY: `{esc_md(api_key)}`",
            chat_id, msg_id, reply_markup=markup,
        )

    elif target == "seturl":
        fsm_set_state(user_id, chat_id, BotStates.SETTING_API_URL)
        safe_send(chat_id, "🔗 *ENTER NEW API BASE URL:*")

    elif target == "setkey":
        fsm_set_state(user_id, chat_id, BotStates.SETTING_API_KEY)
        safe_send(chat_id, "🔑 *ENTER NEW API KEY:*")

    elif target == "services":
        markup, rows = _list_items_markup("services", "id", "name")
        markup.add(types.InlineKeyboardButton(text="➕ ADD NEW SERVICE", callback_data="anav:add_service"))
        bot.edit_message_text("📱 *SERVICES* (🟢=ON / 🔴=OFF):", chat_id, msg_id, reply_markup=markup)

    elif target == "add_service":
        fsm_set_state(user_id, chat_id, BotStates.ADDING_SERVICE)
        safe_send(chat_id, "➕ *ENTER NEW SERVICE NAME:*")

    elif target == "countries":
        with db_cursor() as cur:
            cur.execute("""
                SELECT c.id, c.name, c.flag, c.range_code, c.status, s.name
                FROM countries c JOIN services s ON s.id = c.service_id
                ORDER BY s.name, c.name
            """)
            rows = cur.fetchall()
        markup = types.InlineKeyboardMarkup(row_width=1)
        for cid, name, flag, range_code, status, svc_name in rows:
            icon = "🟢" if status == "ON" else "🔴"
            markup.add(types.InlineKeyboardButton(
                text=f"{icon} [{svc_name}] {flag} {name} ({range_code}XXX)",
                callback_data=f"atoggle:countries:{cid}",
            ))
            markup.add(types.InlineKeyboardButton(text=f"🗑 DELETE {name}", callback_data=f"adel:countries:{cid}"))
        markup.add(types.InlineKeyboardButton(text="➕ ADD NEW COUNTRY", callback_data="anav:add_country"))
        bot.edit_message_text("🌐 *COUNTRIES:*", chat_id, msg_id, reply_markup=markup)

    elif target == "add_country":
        with db_cursor() as cur:
            cur.execute("SELECT id, name, icon FROM services WHERE status = 'ON' ORDER BY name")
            services = cur.fetchall()
        if not services:
            safe_send(chat_id, "❌ *ADD A SERVICE FIRST.*")
        else:
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(*[types.InlineKeyboardButton(text=f"{icon} {name}", callback_data=f"cadd_svc:{sid}")
                         for sid, name, icon in services])
            safe_send(chat_id, "➕ *SELECT SERVICE FOR THIS COUNTRY:*", reply_markup=markup)

    elif target == "fjoin":
        markup, rows = _list_items_markup("channels", "id", "username")
        markup.add(types.InlineKeyboardButton(text="➕ ADD NEW CHANNEL", callback_data="anav:add_channel"))
        bot.edit_message_text("📢 *FORCE JOIN CHANNELS:*", chat_id, msg_id, reply_markup=markup)

    elif target == "add_channel":
        fsm_set_state(user_id, chat_id, BotStates.ADDING_CHANNEL)
        safe_send(chat_id, "➕ *ENTER CHANNEL USERNAME (WITH @):*")

    elif target == "otpgroup":
        current = get_setting("otp_group_link")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            text="✏️ SET/CHANGE OTP GROUP LINK" if current else "➕ SET OTP GROUP LINK",
            callback_data="anav:set_otpgroup",
        ))
        if current:
            markup.add(types.InlineKeyboardButton(text="🗑 REMOVE OTP GROUP LINK", callback_data="anav:clear_otpgroup"))
        status_line = f"CURRENT LINK: {current}" if current else "NO OTP GROUP SET."
        bot.edit_message_text(f"📣 *MANAGE OTP GROUP*\n\n{esc_md(status_line)}", chat_id, msg_id, reply_markup=markup)

    elif target == "set_otpgroup":
        fsm_set_state(user_id, chat_id, BotStates.SETTING_OTP_GROUP)
        safe_send(chat_id, "📣 *ENTER OTP GROUP INVITE LINK OR @USERNAME:*")

    elif target == "clear_otpgroup":
        update_setting("otp_group_link", "")
        bot.answer_callback_query(call.id, text="🗑 OTP GROUP LINK REMOVED.")
        handle_admin_navigation(call, "otpgroup", answer_callback=False)

    elif target == "methodch":
        current = get_setting("method_channel_link")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            text="✏️ SET/CHANGE METHOD CHANNEL" if current else "➕ SET METHOD CHANNEL",
            callback_data="anav:set_methodch",
        ))
        if current:
            markup.add(types.InlineKeyboardButton(text="🗑 REMOVE METHOD CHANNEL", callback_data="anav:clear_methodch"))
        status_line = f"CURRENT LINK: {current}" if current else "NO METHOD CHANNEL SET."
        bot.edit_message_text(f"📊 *MANAGE METHOD CHANNEL*\n\n{esc_md(status_line)}", chat_id, msg_id, reply_markup=markup)

    elif target == "set_methodch":
        fsm_set_state(user_id, chat_id, BotStates.SETTING_METHOD_CHANNEL)
        safe_send(chat_id, "📊 *ENTER METHOD CHANNEL INVITE LINK OR @USERNAME:*")

    elif target == "clear_methodch":
        update_setting("method_channel_link", "")
        bot.answer_callback_query(call.id, text="🗑 METHOD CHANNEL REMOVED.")
        handle_admin_navigation(call, "methodch", answer_callback=False)

    elif target == "supp":
        fsm_set_state(user_id, chat_id, BotStates.UP_ADMIN1)
        safe_send(chat_id, "👤 *ENTER SUPPORT ADMIN 1 USERNAME (WITHOUT @):*")

    elif target == "ref":
        fsm_set_state(user_id, chat_id, BotStates.SET_REF_BONUS)
        safe_send(chat_id, "🎁 *ENTER REFERRAL BONUS AMOUNT:*")

    elif target == "wdset":
        fsm_set_state(user_id, chat_id, BotStates.SET_MIN_WD)
        safe_send(chat_id, "💳 *ENTER MINIMUM WITHDRAW LIMIT:*")

    elif target == "bcast":
        fsm_set_state(user_id, chat_id, BotStates.BROADCASTING_MSG)
        safe_send(chat_id, "📨 *ENTER BROADCAST MESSAGE:*")

    elif target == "user":
        fsm_set_state(user_id, chat_id, BotStates.USER_MANAGE_SEARCH)
        safe_send(chat_id, "🔍 *ENTER USER TELEGRAM ID:*")

    elif target == "admins":
        if user_id != OWNER_ID:
            bot.answer_callback_query(call.id, text="❌ ONLY OWNER CAN MANAGE ADMINS.", show_alert=True)
            return
        with db_cursor() as cur:
            cur.execute("SELECT user_id, username FROM users WHERE is_admin = 1 ORDER BY user_id")
            admins = cur.fetchall()
        markup = types.InlineKeyboardMarkup(row_width=1)
        for a_uid, a_uname in admins:
            label = f"@{a_uname}" if a_uname and a_uname != "NoUsername" else str(a_uid)
            markup.add(types.InlineKeyboardButton(
                text=f"🗑 REMOVE {label} ({a_uid})", callback_data=f"arm_admin:{a_uid}"
            ))
        markup.add(types.InlineKeyboardButton(text="➕ ADD NEW ADMIN", callback_data="anav:add_admin"))
        list_text = "🛡️ *BOT ADMINS*\n\n" + (
            "\n".join(f"• `{a_uid}` — @{a_uname}" for a_uid, a_uname in admins)
            if admins else "_NO EXTRA ADMINS YET._"
        )
        bot.edit_message_text(list_text, chat_id, msg_id, reply_markup=markup)

    elif target == "add_admin":
        if user_id != OWNER_ID:
            bot.answer_callback_query(call.id, text="❌ ONLY OWNER CAN ADD ADMINS.", show_alert=True)
            return
        fsm_set_state(user_id, chat_id, BotStates.ADDING_BOT_ADMIN)
        safe_send(chat_id, "➕ *ENTER USER ID TO PROMOTE AS ADMIN:*")

    if answer_callback:
        bot.answer_callback_query(call.id)


def handle_admin_item_action(call, data):
    action, table, id_str = data.split(":")
    allowed_tables = {"services", "countries", "channels"}
    if table not in allowed_tables:
        bot.answer_callback_query(call.id, text="❌ INVALID TARGET.", show_alert=True)
        return
    item_id = int(id_str)

    if action == "atoggle":
        with db_cursor(commit=True) as cur:
            cur.execute(f"SELECT status FROM {table} WHERE id = ?", (item_id,))
            row = cur.fetchone()
            if not row:
                bot.answer_callback_query(call.id, text="❌ NOT FOUND.", show_alert=True)
                return
            new_status = "OFF" if row[0] == "ON" else "ON"
            cur.execute(f"UPDATE {table} SET status = ? WHERE id = ?", (new_status, item_id))
        bot.answer_callback_query(call.id, text=f"STATUS CHANGED TO {new_status}")
    else:
        with db_cursor(commit=True) as cur:
            cur.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
        bot.answer_callback_query(call.id, text="🗑 DELETED")

    target_map = {"services": "services", "countries": "countries", "channels": "fjoin"}
    handle_admin_navigation(call, target_map[table], answer_callback=False)


def handle_admin_user_action(call, data):
    _, action, uid_str = data.split(":")
    target_uid = int(uid_str)
    with db_cursor(commit=True) as cur:
        if action == "ban":
            cur.execute("UPDATE users SET status = 'Banned' WHERE user_id = ?", (target_uid,))
            bot.answer_callback_query(call.id, text="🚫 USER BANNED.")
        elif action == "unban":
            cur.execute("UPDATE users SET status = 'Active' WHERE user_id = ?", (target_uid,))
            bot.answer_callback_query(call.id, text="✅ USER UNBANNED.")


def handle_remove_admin(call, data):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, text="❌ ONLY OWNER CAN REMOVE ADMINS.", show_alert=True)
        return
    target_uid = int(data.split(":")[1])
    if target_uid == OWNER_ID:
        bot.answer_callback_query(call.id, text="❌ CANNOT REMOVE OWNER.", show_alert=True)
        return
    with db_cursor(commit=True) as cur:
        cur.execute("UPDATE users SET is_admin = 0 WHERE user_id = ?", (target_uid,))
    bot.answer_callback_query(call.id, text="✅ ADMIN REMOVED.")
    safe_send(target_uid, "ℹ️ *YOU HAVE BEEN REMOVED FROM ADMIN PANEL.*")
    show_main_menu(target_uid, target_uid)
    handle_admin_navigation(call, "admins", answer_callback=False)


# =====================================================================
# 16. ADMIN TEXT-INPUT HANDLERS
# =====================================================================
def adm_save_api_url(message):
    update_setting("api_url", message.text.strip().rstrip("/"))
    reset_to_main_menu(message.from_user.id, message.chat.id)
    bot.reply_to(message, "✅ *API URL UPDATED SUCCESSFULLY.*")


def adm_save_api_key(message):
    update_setting("api_key", message.text.strip())
    reset_to_main_menu(message.from_user.id, message.chat.id)
    bot.reply_to(message, "✅ *API KEY UPDATED SUCCESSFULLY.*")


def adm_save_srv(message):
    try:
        with db_cursor(commit=True) as cur:
            cur.execute("INSERT INTO services (name) VALUES (?)", (message.text.strip(),))
        bot.reply_to(message, f"✅ *SERVICE '{esc_md(message.text.strip())}' ADDED SUCCESSFULLY.*")
    except sqlite3.IntegrityError:
        bot.reply_to(message, "❌ *THIS SERVICE ALREADY EXISTS.*")
    reset_to_main_menu(message.from_user.id, message.chat.id)


def adm_save_cnt(message):
    admin_user_id, chat_id = message.from_user.id, message.chat.id
    name = message.text.strip()
    if not name:
        bot.reply_to(message, "❌ *NAME CANNOT BE EMPTY. PLEASE TRY AGAIN:*")
        return
    fsm_add_data(admin_user_id, chat_id, new_country_name=name)
    fsm_set_state(admin_user_id, chat_id, BotStates.ADDING_COUNTRY_RANGE)
    bot.reply_to(message, f"✅ *COUNTRY NAME: {esc_md(name)}*\n\n🔢 *ENTER RANGE CODE:*")


def adm_save_country_range(message):
    admin_user_id, chat_id = message.from_user.id, message.chat.id
    range_code = message.text.strip()
    if not range_code or " " in range_code:
        bot.reply_to(message, "❌ *ENTER VALID RANGE CODE (NO SPACES):*")
        return
    fsm_add_data(admin_user_id, chat_id, new_country_range=range_code)
    fsm_set_state(admin_user_id, chat_id, BotStates.ADDING_COUNTRY_FLAG)
    bot.reply_to(
        message,
        f"✅ *RANGE: {esc_md(range_code)}XXX*\n\n"
        f"🏳️ *ENTER FLAG EMOJI (OR TYPE 'SKIP'):*",
    )


def adm_save_country_flag(message):
    admin_user_id, chat_id = message.from_user.id, message.chat.id
    text = message.text.strip()
    flag = "🌐" if text.lower() == "skip" else text

    u_data = fsm_get_data(admin_user_id, chat_id)
    service_id = u_data.get("new_country_service_id")
    service_name = u_data.get("new_country_service_name", "")
    name = u_data.get("new_country_name")
    range_code = u_data.get("new_country_range")

    if not (service_id and name and range_code):
        bot.reply_to(message, "⚠️ *SOME DATA WAS LOST. PLEASE START AGAIN.*")
        reset_to_main_menu(admin_user_id, chat_id)
        return

    try:
        with db_cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO countries (service_id, name, flag, range_code) VALUES (?, ?, ?, ?)",
                (service_id, name, flag, range_code),
            )
        bot.reply_to(
            message,
            f"✅ *ADDED SUCCESSFULLY:*\n\n"
            f"🔹 *SERVICE:* {esc_md(service_name)}\n"
            f"{flag} *COUNTRY:* {esc_md(name)}\n"
            f"🔢 *RANGE:* `{esc_md(range_code)}XXX`",
        )
    except sqlite3.IntegrityError:
        bot.reply_to(message, f"❌ *COUNTRY '{esc_md(name)}' ALREADY EXISTS UNDER '{esc_md(service_name)}'.*")

    reset_to_main_menu(admin_user_id, chat_id)


def adm_save_ch(message):
    ch_user = message.text.strip()
    if not ch_user.startswith("@"):
        bot.reply_to(message, "❌ *USERNAME MUST START WITH @. PLEASE TRY AGAIN:*")
        return
    ch_link = f"https://t.me/{ch_user.lstrip('@')}"
    try:
        with db_cursor(commit=True) as cur:
            cur.execute("INSERT INTO channels (username, link) VALUES (?, ?)", (ch_user, ch_link))
        bot.reply_to(message, f"✅ *FORCE JOIN CHANNEL {esc_md(ch_user)} CONNECTED SUCCESSFULLY.*")
    except sqlite3.IntegrityError:
        bot.reply_to(message, "❌ *THIS CHANNEL ALREADY EXISTS.*")
    reset_to_main_menu(message.from_user.id, message.chat.id)


def adm_up1(message):
    update_setting("admin_1_user", message.text.strip().lstrip("@"))
    fsm_set_state(message.from_user.id, message.chat.id, BotStates.UP_ADMIN2)
    bot.reply_to(message, "✅ *ADMIN 1 SAVED.*\n\n👤 *ENTER SUPPORT ADMIN 2 USERNAME (WITHOUT @), OR TYPE 'SKIP':*")


def adm_up2(message):
    txt = message.text.strip()
    if txt.lower() != "skip":
        update_setting("admin_2_user", txt.lstrip("@"))
        bot.reply_to(message, "✅ *ADMIN 2 SAVED.*")
    else:
        bot.reply_to(message, "⏭ *ADMIN 2 SETUP SKIPPED.*")
    reset_to_main_menu(message.from_user.id, message.chat.id)


def adm_save_otp_group(message):
    text = message.text.strip()
    if text.startswith("@"):
        link = f"https://t.me/{text.lstrip('@')}"
    elif text.startswith("https://t.me/") or text.startswith("http://t.me/"):
        link = text
    else:
        bot.reply_to(
            message,
            "❌ *INVALID FORMAT. PLEASE USE `https://t.me/+...` OR `@GroupUsername`, OR /CANCEL:*",
        )
        return
    update_setting("otp_group_link", link)
    bot.reply_to(message, f"✅ *OTP GROUP LINK SET:*\n{link}")
    reset_to_main_menu(message.from_user.id, message.chat.id)


def adm_save_method_channel(message):
    text = message.text.strip()
    if text.startswith("@"):
        link = f"https://t.me/{text.lstrip('@')}"
    elif text.startswith("https://t.me/") or text.startswith("http://t.me/"):
        link = text
    else:
        bot.reply_to(
            message,
            "❌ *INVALID FORMAT. PLEASE USE `https://t.me/+...` OR `@GroupUsername`, OR /CANCEL:*",
        )
        return
    update_setting("method_channel_link", link)
    bot.reply_to(message, f"✅ *METHOD CHANNEL LINK SET:*\n{link}")
    reset_to_main_menu(message.from_user.id, message.chat.id)


def adm_save_refb(message):
    try:
        val = float(message.text.strip())
    except ValueError:
        bot.reply_to(message, "❌ *PLEASE ENTER A NUMBER. TRY AGAIN:*")
        return
    update_setting("bonus_amount", val)
    fsm_set_state(message.from_user.id, message.chat.id, BotStates.SET_REF_REQ_OTP)
    bot.reply_to(message, "✅ *REFERRAL BONUS SAVED.*\n\n🔢 *ENTER REQUIRED OTP COUNT FOR BONUS (e.g., 50):*")


def adm_save_req_otp(message):
    if not message.text.strip().isdigit():
        bot.reply_to(message, "❌ *PLEASE ENTER A NUMBER. TRY AGAIN:*")
        return
    update_setting("required_otp", int(message.text.strip()))
    bot.reply_to(message, "✅ *REQUIRED OTP COUNT SAVED.*")
    reset_to_main_menu(message.from_user.id, message.chat.id)


def adm_save_minwd(message):
    try:
        val = float(message.text.strip())
    except ValueError:
        bot.reply_to(message, "❌ *PLEASE ENTER A NUMBER. TRY AGAIN:*")
        return
    update_setting("min_withdraw", val)
    fsm_set_state(message.from_user.id, message.chat.id, BotStates.SET_MAX_WD)
    bot.reply_to(message, "✅ *MINIMUM WITHDRAW LIMIT SAVED.*\n\n💳 *ENTER MAXIMUM WITHDRAW LIMIT (e.g., 5000):*")


def adm_save_maxwd(message):
    try:
        val = float(message.text.strip())
    except ValueError:
        bot.reply_to(message, "❌ *PLEASE ENTER A NUMBER. TRY AGAIN:*")
        return
    min_wd = float(get_setting("min_withdraw", "0"))
    if val < min_wd:
        bot.reply_to(message, f"❌ *MAXIMUM MUST BE GREATER THAN MINIMUM ({min_wd}). TRY AGAIN:*")
        return
    update_setting("max_withdraw", val)
    bot.reply_to(message, "✅ *MAXIMUM WITHDRAW LIMIT SAVED.*")
    reset_to_main_menu(message.from_user.id, message.chat.id)


def adm_add_bot_admin(message):
    admin_user_id, chat_id = message.from_user.id, message.chat.id
    if admin_user_id != OWNER_ID:
        bot.reply_to(message, "❌ *ONLY OWNER CAN ADD ADMINS.*")
        reset_to_main_menu(admin_user_id, chat_id)
        return
    if not message.text.strip().isdigit():
        bot.reply_to(message, "❌ *ID MUST BE NUMERIC. TRY AGAIN, OR /CANCEL:*")
        return
    target_uid = int(message.text.strip())
    with db_cursor() as cur:
        cur.execute("SELECT username, is_admin FROM users WHERE user_id = ?", (target_uid,))
        row = cur.fetchone()
    if not row:
        bot.reply_to(message, "❌ *USER NOT FOUND. ASK THEM TO /START THE BOT FIRST.*")
        return
    username, already_admin = row
    if already_admin == 1:
        bot.reply_to(message, f"ℹ️ *{target_uid} (@{esc_md(username)}) IS ALREADY AN ADMIN.*")
    else:
        with db_cursor(commit=True) as cur:
            cur.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (target_uid,))
        bot.reply_to(message, f"✅ *{target_uid} (@{esc_md(username)}) PROMOTED TO ADMIN.*")
        safe_send(target_uid, "🎉 *YOU HAVE BEEN GRANTED ADMIN PANEL ACCESS!*")
        show_main_menu(target_uid, target_uid)
    reset_to_main_menu(admin_user_id, chat_id)


def adm_user_search(message):
    if not message.text.strip().isdigit():
        bot.reply_to(message, "❌ *ID MUST BE NUMERIC.*")
        return
    u_id = int(message.text.strip())
    with db_cursor() as cur:
        cur.execute("SELECT user_id, username, balance, otp_count, status FROM users WHERE user_id = ?", (u_id,))
        row = cur.fetchone()
    if not row:
        bot.reply_to(message, "❌ *USER NOT FOUND IN DATABASE.*")
        reset_to_main_menu(message.from_user.id, message.chat.id)
        return
    fsm_add_data(message.from_user.id, message.chat.id, target_uid=u_id)
    fsm_set_state(message.from_user.id, message.chat.id, BotStates.USER_MANAGE_BAL_ADD)
    ban_markup = types.InlineKeyboardMarkup()
    if row[4] == "Banned":
        ban_markup.add(types.InlineKeyboardButton(text="✅ UNBAN USER", callback_data=f"auser:unban:{u_id}"))
    else:
        ban_markup.add(types.InlineKeyboardButton(text="🚫 BAN USER", callback_data=f"auser:ban:{u_id}"))
    bot.send_message(
        message.chat.id,
        f"👤 *USER FOUND:*\n\nID: `{row[0]}`\nUsername: @{esc_md(row[1])}\n"
        f"Balance: {row[2]} BDT\nOTP Received: {row[3]}\nStatus: {row[4]}",
        reply_markup=ban_markup,
    )
    bot.send_message(
        message.chat.id,
        "✨ *ENTER AMOUNT TO ADD TO THIS USER'S BALANCE (TYPE 0 TO SKIP):*",
    )


def adm_user_bal_add_exec(message):
    try:
        amount = float(message.text.strip())
    except ValueError:
        bot.reply_to(message, "❌ *INVALID FORMAT. PLEASE ENTER A NUMBER:*")
        return
    u_data = fsm_get_data(message.from_user.id, message.chat.id)
    t_uid = u_data.get("target_uid")
    if amount > 0:
        with db_cursor(commit=True) as cur:
            cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, t_uid))
        bot.reply_to(message, f"✅ *{amount} BDT ADDED TO USER {t_uid} BALANCE.*")
        safe_send(t_uid, f"💰 *ADMIN ADDED {amount} BDT TO YOUR BALANCE.*")
    else:
        bot.reply_to(message, "⏭ *ADD SKIPPED.*")
    fsm_set_state(message.from_user.id, message.chat.id, BotStates.USER_MANAGE_BAL_REM)
    bot.send_message(
        message.chat.id,
        "✨ *ENTER AMOUNT TO REMOVE FROM THIS USER'S BALANCE (TYPE 0 TO SKIP):*",
    )


def adm_user_bal_rem_exec(message):
    try:
        amount = float(message.text.strip())
    except ValueError:
        bot.reply_to(message, "❌ *INVALID FORMAT. PLEASE ENTER A NUMBER:*")
        return
    u_data = fsm_get_data(message.from_user.id, message.chat.id)
    t_uid = u_data.get("target_uid")
    if amount > 0:
        with db_cursor(commit=True) as cur:
            cur.execute(
                "UPDATE users SET balance = MAX(balance - ?, 0) WHERE user_id = ?",
                (amount, t_uid),
            )
        bot.reply_to(message, f"✅ *{amount} BDT REMOVED FROM USER {t_uid} BALANCE.*")
        safe_send(t_uid, f"⚠️ *ADMIN REMOVED {amount} BDT FROM YOUR BALANCE.*")
    else:
        bot.reply_to(message, "⏭ *REMOVE SKIPPED.*")
    reset_to_main_menu(message.from_user.id, message.chat.id)


def adm_broadcast_exec(message):
    with db_cursor() as cur:
        cur.execute("SELECT user_id FROM users")
        users = cur.fetchall()
    bot.reply_to(message, f"📢 *BROADCASTING TO {len(users)} USERS...*")
    success, fail = 0, 0
    for (uid,) in users:
        try:
            bot.send_message(uid, message.text)
            success += 1
        except Exception:
            fail += 1
        time.sleep(0.05)
    bot.send_message(
        message.chat.id,
        f"✅ *BROADCAST COMPLETE!*\n\n🔹 DELIVERED: {success}\n❌ FAILED: {fail}",
    )
    reset_to_main_menu(message.from_user.id, message.chat.id)


# =====================================================================
# 17. MAIN MENU BUTTON ROUTER
# =====================================================================
def main_reply_router(message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    with db_cursor() as cur:
        cur.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
    if row and row[0] == "Banned":
        safe_send(chat_id, "❌ *YOU ARE BANNED.*")
        return

    if not check_user_joined(user_id):
        fsm_set_state(user_id, chat_id, BotStates.VERIFY_PENDING)
        safe_send(
            chat_id,
            "🔴 *PLEASE JOIN OUR CHANNELS TO USE THE BOT!*",
            reply_markup=get_force_join_keyboard(),
        )
        return

    text = message.text or ""

    if text == "📞 GET NUMBER":
        with db_cursor() as cur:
            cur.execute("SELECT id, name, icon FROM services WHERE status = 'ON'")
            srvs = cur.fetchall()
        if not srvs:
            safe_send(chat_id, "❌ *NO SERVICES AVAILABLE.*")
            return
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(*[types.InlineKeyboardButton(text=f"{icon} {name}", callback_data=f"svc:{sid}")
                     for sid, name, icon in srvs])
        safe_send(chat_id, "📞 *SELECT APP TO GET NUMBER:*\n━━━━━━━━━━━━━━━━━━", reply_markup=markup)

    elif text == "💰 BALANCE":
        if get_setting("withdraw_status") == "OFF":
            safe_send(chat_id, "❌ *WITHDRAW SYSTEM IS TEMPORARILY DISABLED.*")
            return
        with db_cursor() as cur:
            cur.execute("SELECT balance, otp_count FROM users WHERE user_id = ?", (user_id,))
            bal, otp_cnt = cur.fetchone()

        min_wd = float(get_setting("min_withdraw", "0.5"))

        markup = types.InlineKeyboardMarkup(row_width=2)
        if get_setting("bkash_status") == "ON":
            markup.add(types.InlineKeyboardButton(text="Bkash", callback_data="wd:Bkash"))
        if get_setting("nagad_status") == "ON":
            markup.add(types.InlineKeyboardButton(text="Nagad", callback_data="wd:Nagad"))
        if get_setting("rocket_status") == "ON":
            markup.add(types.InlineKeyboardButton(text="Rocket", callback_data="wd:Rocket"))
        if get_setting("binance_status") == "ON":
            markup.add(types.InlineKeyboardButton(text="Binance", callback_data="wd:Binance"))
        markup.add(types.InlineKeyboardButton(text="Back to Menu", callback_data="back_to_menu"))

        safe_send(
            chat_id,
            f"💰 *BALANCE*\n"
            f"💎 *CURRENT BALANCE:* {bal:.4f}$\n"
            f"📈 *MINIMUM WITHDRAW:* {min_wd}$\n\n"
            f"*CHOOSE A WITHDRAWAL METHOD BELOW:*",
            reply_markup=markup,
        )

    elif text == "👥 REFER & EARN":
        bot_info = bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
        with db_cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,))
            total_ref = cur.fetchone()[0]
        ref_text = (
            f"👥 *REFER & EARN SYSTEM*\n\n"
            f"WHEN YOUR REFERRED USER COMPLETES "
            f"`{get_setting('required_otp')}` OTPS, "
            f"YOU EARN `{get_setting('bonus_amount')} BDT` BONUS.\n\n"
            f"📊 *YOUR REFERRAL STATUS:*\n"
            f"🔹 TOTAL REFERRALS: {total_ref}\n\n"
            f"🔗 [CLICK YOUR REFERRAL LINK]({ref_link})"
        )
        safe_send(chat_id, ref_text)

    elif text == "💬 SUPPORT":
        a1 = get_setting("admin_1_user")
        a2 = get_setting("admin_2_user")
        markup = types.InlineKeyboardMarkup(row_width=2)
        buttons = []
        if a1:
            buttons.append(types.InlineKeyboardButton(text="👤 Admin 1", url=f"https://t.me/{a1}"))
        if a2:
            buttons.append(types.InlineKeyboardButton(text="👤 Admin 2", url=f"https://t.me/{a2}"))
        if buttons:
            markup.add(*buttons)
            safe_send(chat_id, "💬 *NEED HELP?*\n\nCONTACT OUR ADMINS BELOW:", reply_markup=markup)
        else:
            safe_send(chat_id, "💬 *SUPPORT NOT CONFIGURED YET. PLEASE TRY AGAIN LATER.*")

    elif text == "🏆 LEADERBOARD":
        with db_cursor() as cur:
            cur.execute(
                "SELECT name, username, otp_count FROM users WHERE status = 'Active' ORDER BY otp_count DESC LIMIT 10"
            )
            top_users = cur.fetchall()

        if not top_users:
            safe_send(chat_id, "🏆 *NO DATA AVAILABLE YET.*")
            return

        lines = []
        for i, (name, username, otp_count) in enumerate(top_users, 1):
            display_name = username or name or f"User_{i}"
            lines.append(f"{i}. {esc_md(display_name)} ➔ {otp_count} OTPs")

        text = (
            f"🏆 *OTP LEADERBOARD (TOP USERS)*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"━━━━━━\n\n"
            + "\n".join(lines)
        )
        safe_send(chat_id, text)

    elif text == "📊 METHOD":
        method_link = get_setting("method_channel_link")
        markup = types.InlineKeyboardMarkup(row_width=1)
        if method_link:
            markup.add(types.InlineKeyboardButton(text="📢 JOIN METHOD CHANNEL", url=method_link))
        markup.add(types.InlineKeyboardButton(text="Back to Menu", callback_data="back_to_menu"))

        safe_send(
            chat_id,
            f"📊 *METHOD CHANNEL*\n\n"
            f"JOIN OUR METHOD CHANNEL TO LEARN "
            f"DIFFERENT PAYMENT METHODS AND "
            f"WITHDRAWAL TECHNIQUES!",
            reply_markup=markup,
        )

    elif text == "👑 ADMIN PANEL":
        if is_admin(user_id):
            send_admin_panel_dashboard(chat_id, user_id)
        else:
            safe_send(chat_id, "❌ *YOU ARE NOT AUTHORIZED FOR THIS SECTION.*")


# =====================================================================
# 18. FSM DISPATCH TABLE + UNIVERSAL TEXT HANDLER
# =====================================================================
STATE_HANDLERS = {
    BotStates.WAITING_WITHDRAW_NUMBER: withdraw_number_input,
    BotStates.WAITING_WITHDRAW_AMOUNT: withdraw_amount_input,
    BotStates.SETTING_API_URL: adm_save_api_url,
    BotStates.SETTING_API_KEY: adm_save_api_key,
    BotStates.ADDING_SERVICE: adm_save_srv,
    BotStates.ADDING_COUNTRY: adm_save_cnt,
    BotStates.ADDING_COUNTRY_RANGE: adm_save_country_range,
    BotStates.ADDING_COUNTRY_FLAG: adm_save_country_flag,
    BotStates.ADDING_CHANNEL: adm_save_ch,
    BotStates.UP_ADMIN1: adm_up1,
    BotStates.UP_ADMIN2: adm_up2,
    BotStates.SET_REF_BONUS: adm_save_refb,
    BotStates.SET_REF_REQ_OTP: adm_save_req_otp,
    BotStates.SET_MIN_WD: adm_save_minwd,
    BotStates.SET_MAX_WD: adm_save_maxwd,
    BotStates.USER_MANAGE_SEARCH: adm_user_search,
    BotStates.USER_MANAGE_BAL_ADD: adm_user_bal_add_exec,
    BotStates.USER_MANAGE_BAL_REM: adm_user_bal_rem_exec,
    BotStates.ADDING_BOT_ADMIN: adm_add_bot_admin,
    BotStates.SETTING_OTP_GROUP: adm_save_otp_group,
    BotStates.SETTING_METHOD_CHANNEL: adm_save_method_channel,
    BotStates.BROADCASTING_MSG: adm_broadcast_exec,
}


@bot.message_handler(func=lambda m: True)
def universal_text_router(message):
    state = fsm_get_state(message.from_user.id, message.chat.id)
    handler = STATE_HANDLERS.get(state)
    if handler:
        try:
            handler(message)
        except Exception as e:
            logger.exception(f"State handler error (state={state}): {e}")
            safe_send(message.chat.id, "⚠️ *AN ERROR OCCURRED. PLEASE TRY /CANCEL.*")
        return
    main_reply_router(message)


# =====================================================================
# 19. ENTRYPOINT
# =====================================================================
if __name__ == "__main__":
    init_db()
    threading.Thread(target=otp_polling_loop, daemon=True).start()

    print("=" * 57)
    print("🤖 NR NUMBER BOT IS ONLINE!")
    print("⚙️ OTP Polling Engine Initialized.")
    print("=" * 57)

    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception as e:
            logger.error(f"Polling crashed, restarting in 5s: {e}")
            time.sleep(5)
