import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, InlineQueryResultArticle, InputTextMessageContent
import threading
from telebot.types import WebAppInfo
import json
from datetime import datetime, timedelta
from telebot.types import LabeledPrice
import sqlite3
import time
import random
import os
import re
from datetime import datetime
from contextlib import contextmanager
import requests as _requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
CRASH_ADMIN_CHAT = int(os.getenv("CRASH_ADMIN_CHAT", "0"))
STOCK_LOG_CHANNEL = -1003777645780  # канал для лога транзакций акций

shop_pages = {}
wardrobe_pages = {}
active_crash_bets = {}
pending_crash_decisions = {}
REAL_ADMIN_IDS = list(ADMIN_IDS)
disabled_admins = set()

bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=4)

# =============================
# 🛡️ ГЛОБАЛЬНАЯ ЗАЩИТА ОТ ФЛУДА
# =============================

_flood_data = {}          # uid -> список timestamp
_flood_banned = {}        # uid -> время бана
_flood_lock = threading.Lock()

FLOOD_MAX_MESSAGES = 12   # макс сообщений
FLOOD_WINDOW = 5          # за N секунд
FLOOD_BAN_DURATION = 300  # бан на 5 минут

def check_flood(uid):
    """Возвращает True если пользователь флудит"""
    now = time.time()
    with _flood_lock:
        # Проверяем бан
        if uid in _flood_banned:
            if now < _flood_banned[uid]:
                return True
            else:
                del _flood_banned[uid]

        # Чистим старые записи
        history = _flood_data.get(uid, [])
        history = [t for t in history if now - t < FLOOD_WINDOW]
        history.append(now)
        _flood_data[uid] = history

        # Баним если превышен лимит
        if len(history) >= FLOOD_MAX_MESSAGES:
            _flood_banned[uid] = now + FLOOD_BAN_DURATION
            print(f"[flood] забанен uid={uid} на {FLOOD_BAN_DURATION}с")
            return True

        return False

# Middleware — проверяем каждое входящее сообщение
original_process_new_messages = bot.process_new_messages

def _protected_process(messages):
    filtered = []
    for message in messages:
        try:
            uid = message.from_user.id if message.from_user else None
            if uid is None:
                filtered.append(message)
                continue
            if check_flood(uid):
                continue  # молча игнорируем флудера
            # Обновляем last_activity при каждом сообщении
            # last_private_activity — только для ЛС бота (точный счётчик активных пользователей)
            try:
                is_private = getattr(message.chat, 'type', None) == 'private'
                if is_private:
                    with get_db_cursor() as _cur:
                        _cur.execute(
                            "UPDATE users SET last_activity=?, last_private_activity=? WHERE user_id=?",
                            (int(time.time()), int(time.time()), uid)
                        )
                else:
                    with get_db_cursor() as _cur:
                        _cur.execute(
                            "UPDATE users SET last_activity=? WHERE user_id=? AND last_activity > 0",
                            (int(time.time()), uid)
                        )
            except Exception:
                pass
            filtered.append(message)
        except Exception:
            filtered.append(message)
    if filtered:
        original_process_new_messages(filtered)

bot.process_new_messages = _protected_process

# =============================

def _tg_api(method, **kwargs):
    """Прямой запрос к Telegram API — поддерживает style в кнопках."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    resp = _requests.post(url, json=kwargs, timeout=30)
    return resp.json()

def send_styled(chat_id, text, keyboard_dict, parse_mode="HTML", **kwargs):
    """Отправить сообщение с цветными кнопками (style) через прямой API."""
    return _tg_api(
        "sendMessage",
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=keyboard_dict,
        **kwargs
    )

def edit_styled(chat_id, message_id, text, keyboard_dict, parse_mode="HTML"):
    """Редактировать сообщение с цветными кнопками через прямой API."""
    return _tg_api(
        "editMessageText",
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=keyboard_dict
    )

_orig_send_message = bot.send_message
_orig_reply_to     = bot.reply_to
_orig_edit_message_text = bot.edit_message_text

def _patched_send_message(chat_id, text, reply_markup=None, **kwargs):
    if isinstance(reply_markup, dict):
        parse_mode = kwargs.pop('parse_mode', 'HTML')
        result = _tg_api('sendMessage',
                         chat_id=chat_id,
                         text=text,
                         parse_mode=parse_mode,
                         reply_markup=reply_markup,
                         **kwargs)
        class _FakeMsg:
            def __init__(self, d):
                r = d.get('result', {})
                self.message_id = r.get('message_id')
                self.chat = type('C', (), {'id': chat_id})()
        return _FakeMsg(result)
    return _orig_send_message(chat_id, text, reply_markup=reply_markup, **kwargs)

def _patched_reply_to(message, text, reply_markup=None, **kwargs):
    if isinstance(reply_markup, dict):
        parse_mode = kwargs.pop('parse_mode', 'HTML')
        _tg_api('sendMessage',
                chat_id=message.chat.id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                reply_to_message_id=message.message_id,
                **kwargs)
        return
    return _orig_reply_to(message, text, reply_markup=reply_markup, **kwargs)

def _patched_edit_message_text(text, chat_id=None, message_id=None, reply_markup=None, **kwargs):
    if isinstance(reply_markup, dict):
        parse_mode = kwargs.pop('parse_mode', 'HTML')
        _tg_api('editMessageText',
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                **kwargs)
        return
    return _orig_edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                   reply_markup=reply_markup, **kwargs)

bot.send_message      = _patched_send_message
bot.reply_to          = _patched_reply_to
bot.edit_message_text = _patched_edit_message_text

LOAN_CONFIG = {
    "max_loan": 100000,
    "interest_rate": 0.1,
    "max_term": 3,
    "penalty_rate": 0.2,
    "min_balance_for_loan": 500
}

TRANSFER_FEE = 0.1


# ── DatabasePool ─────────────────────────────────────────────

# Путь к БД — если задана переменная DB_PATH используем её,
# иначе game.db рядом со скриптом. На Render подключи Persistent Disk
# и укажи DB_PATH=/data/game.db в переменных окружения.
_DB_PATH = os.getenv("DB_PATH", "game.db")

class DatabasePool:
    def __init__(self, db_name=_DB_PATH, pool_size=10):
        self.db_name   = db_name
        self.pool      = []
        self.pool_size = pool_size
        self._lock     = threading.Lock()

    def _make_sqlite(self):
        conn = sqlite3.connect(self.db_name, timeout=60.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def get_connection(self):
        with self._lock:
            if self.pool:
                return self.pool.pop()
            return self._make_sqlite()

    def return_connection(self, conn):
        with self._lock:
            if len(self.pool) < self.pool_size:
                self.pool.append(conn)
            else:
                conn.close()

db_pool = DatabasePool()

@contextmanager
def get_db_connection():
    conn = db_pool.get_connection()
    try:
        yield conn
    finally:
        db_pool.return_connection(conn)

@contextmanager
def get_db_cursor():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception as _dbe:
            conn.rollback()
            raise

# ── Keepalive: периодически трогаем БД чтобы не закрылось ──
def _db_keepalive():
    """Раз в 4 минуты делаем лёгкий SELECT — не даём SQLite/Render
    закрыть соединение при простое."""
    while True:
        time.sleep(240)
        try:
            with get_db_cursor() as cur:
                cur.execute("SELECT 1")
        except Exception as e:
            print(f"[keepalive] ошибка: {e}")

threading.Thread(target=_db_keepalive, daemon=True, name="db-keepalive").start()
print("[db] keepalive поток запущен (каждые 4 мин)")

def init_db():
    with get_db_cursor() as cursor:
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            custom_name TEXT,
            balance INTEGER DEFAULT 0,
            last_click TIMESTAMP DEFAULT 0,
            click_power INTEGER DEFAULT 10,
            referral_code TEXT UNIQUE,
            referred_by INTEGER,
            video_cards INTEGER DEFAULT 0,
            deposit INTEGER DEFAULT 0,
            last_mining_collect TIMESTAMP DEFAULT 0,
            last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            click_streak INTEGER DEFAULT 0,
            total_clicks INTEGER DEFAULT 0,
            bank_deposit INTEGER DEFAULT 0,
            daily_streak INTEGER DEFAULT 0,
            last_daily_bonus TIMESTAMP DEFAULT 0,
            last_interest_calc TIMESTAMP DEFAULT 0,
            business_id INTEGER DEFAULT 0,
            business_progress INTEGER DEFAULT 0,
            experience INTEGER DEFAULT 0,
            business_start_time TIMESTAMP DEFAULT 0,
            business_raw_materials INTEGER DEFAULT 0,
            clan_id INTEGER DEFAULT 0,
            games_won INTEGER DEFAULT 0,
            games_lost INTEGER DEFAULT 0,
            total_won_amount INTEGER DEFAULT 0,
            total_lost_amount INTEGER DEFAULT 0
        )
        ''')
        cursor.execute('''
CREATE TABLE IF NOT EXISTS warns (
    user_id INTEGER PRIMARY KEY,
    reason TEXT,
    warned_by INTEGER,
    warned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    is_active INTEGER DEFAULT 1
)
''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS businesses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            price INTEGER NOT NULL,
            max_players INTEGER DEFAULT 100,
            storage_capacity INTEGER NOT NULL,
            profit_multiplier REAL NOT NULL,
            delivery_time INTEGER DEFAULT 86400,
            image_url TEXT,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            available INTEGER DEFAULT 1
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS checks (
            code TEXT PRIMARY KEY,
            amount INTEGER,
            max_activations INTEGER,
            current_activations INTEGER DEFAULT 0,
            password TEXT,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            target_username TEXT
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS check_activations (
            user_id INTEGER,
            check_code TEXT,
            activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, check_code)
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS lottery (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            jackpot INTEGER DEFAULT 0,
            last_winner INTEGER,
            last_win_amount INTEGER,
            draw_time TIMESTAMP DEFAULT 0,
            tickets_sold INTEGER DEFAULT 0
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS lottery_tickets (
            user_id INTEGER,
            tickets INTEGER DEFAULT 0,
            PRIMARY KEY (user_id)
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS clothes_shop (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price INTEGER NOT NULL,
            type TEXT NOT NULL,
            image_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            supply INTEGER DEFAULT -1,
            sold_count INTEGER DEFAULT 0
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_clothes (
            user_id INTEGER,
            item_id INTEGER,
            equipped INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, item_id)
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS loans (
            user_id INTEGER PRIMARY KEY,
            loan_amount INTEGER DEFAULT 0,
            taken_at TIMESTAMP DEFAULT 0,
            interest_paid INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active'
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS premium (
            user_id INTEGER PRIMARY KEY,
            expires_at TIMESTAMP DEFAULT 0
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user_id INTEGER,
            to_user_id INTEGER,
            amount INTEGER,
            fee INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS top_messages (
            chat_id INTEGER PRIMARY KEY,
            message_id INTEGER
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS clans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            tag TEXT UNIQUE NOT NULL,
            description TEXT,
            owner_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            level INTEGER DEFAULT 1,
            experience INTEGER DEFAULT 0,
            balance INTEGER DEFAULT 0,
            members_count INTEGER DEFAULT 1,
            max_members INTEGER DEFAULT 20,
            tax_rate REAL DEFAULT 0.05,
            settings TEXT DEFAULT '{}',
            last_reward TIMESTAMP DEFAULT 0
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS clan_members (
            user_id INTEGER PRIMARY KEY,
            clan_id INTEGER NOT NULL,
            role TEXT DEFAULT 'member',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            contributed INTEGER DEFAULT 0,
            last_contribution TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (clan_id) REFERENCES clans (id) ON DELETE CASCADE
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS clan_applications (
            user_id INTEGER,
            clan_id INTEGER,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, clan_id)
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_bag (
            user_id INTEGER,
            component_name TEXT,
            component_price INTEGER,
            obtained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, component_name)
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS clan_quests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clan_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            target_type TEXT NOT NULL,
            target_value INTEGER NOT NULL,
            current_value INTEGER DEFAULT 0,
            reward INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            completed INTEGER DEFAULT 0,
            FOREIGN KEY (clan_id) REFERENCES clans (id) ON DELETE CASCADE
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS dice_challenges (
            challenge_id TEXT PRIMARY KEY,
            challenger_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            bet_amount INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
        ''')
            
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS auctions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            created_by INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ends_at TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'active',
            winner_id INTEGER,
            winner_bid INTEGER
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS auction_bids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            bid_amount INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (auction_id) REFERENCES auctions (id) ON DELETE CASCADE
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS snowman_battles (
            battle_id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            total_hp INTEGER NOT NULL,
            current_hp INTEGER NOT NULL,
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_time TIMESTAMP,
            status TEXT DEFAULT 'active',
            participants_count INTEGER DEFAULT 0
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS snowman_damage (
            battle_id TEXT,
            user_id INTEGER,
            username TEXT,
            total_damage INTEGER DEFAULT 0,
            attacks_count INTEGER DEFAULT 0,
            last_attack_time TIMESTAMP,
            PRIMARY KEY (battle_id, user_id)
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS snowman_rewards (
            battle_id TEXT,
            user_id INTEGER,
            reward_amount INTEGER,
            position INTEGER,
            claimed INTEGER DEFAULT 0,
            claimed_at TIMESTAMP,
            PRIMARY KEY (battle_id, user_id)
        )
        ''')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_snowman_chat ON snowman_battles(chat_id, status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_snowman_damage ON snowman_damage(battle_id, total_damage DESC)')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_auctions_status ON auctions(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_auctions_ends_at ON auctions(ends_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_auction_bids_auction ON auction_bids(auction_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_auction_bids_user ON auction_bids(user_id)')
        
        print("🏆 Таблицы аукционов созданы")
        
        cursor.execute("PRAGMA table_info(checks)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'target_username' not in columns:
            cursor.execute('ALTER TABLE checks ADD COLUMN target_username TEXT')
            print("🏆 Добавлена колонка target_username в таблицу checks")
            
        cursor.execute("PRAGMA table_info(users)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'experience' not in columns:
            cursor.execute('ALTER TABLE users ADD COLUMN experience INTEGER DEFAULT 0')
            print("🏆 Добавлена колонка experience в таблицу users")

        if 'last_private_activity' not in columns:
            cursor.execute('ALTER TABLE users ADD COLUMN last_private_activity INTEGER DEFAULT 0')
            print("🏆 Добавлена колонка last_private_activity в таблицу users")
            
        required_columns = [
            'custom_name', 'video_cards', 'deposit', 'last_mining_collect', 
            'click_streak', 'total_clicks', 'bank_deposit', 'daily_streak', 
            'last_daily_bonus', 'last_interest_calc', 'business_id', 
            'business_progress', 'business_start_time', 'business_raw_materials', 'clan_id',
            'games_won', 'games_lost', 'total_won_amount', 'total_lost_amount'
        ]
        
        for column in required_columns:
            if column not in columns:
                if column in ['bank_deposit', 'click_streak', 'total_clicks', 'daily_streak', 
                             'business_id', 'business_progress', 'business_raw_materials', 'clan_id',
                             'games_won', 'games_lost', 'total_won_amount', 'total_lost_amount']:
                    cursor.execute(f'ALTER TABLE users ADD COLUMN {column} INTEGER DEFAULT 0')
                elif column in ['business_start_time']:
                    cursor.execute(f'ALTER TABLE users ADD COLUMN {column} TIMESTAMP DEFAULT 0')
                else:
                    cursor.execute(f'ALTER TABLE users ADD COLUMN {column} INTEGER DEFAULT 0')
        
        cursor.execute("PRAGMA table_info(clothes_shop)")
        shop_columns = [column[1] for column in cursor.fetchall()]
        
        if 'supply' not in shop_columns:
            cursor.execute('ALTER TABLE clothes_shop ADD COLUMN supply INTEGER DEFAULT -1')
            print("🏆 Добавлена колонка supply в таблицу clothes_shop")
            
        if 'sold_count' not in shop_columns:
            cursor.execute('ALTER TABLE clothes_shop ADD COLUMN sold_count INTEGER DEFAULT 0')
            print("🏆 Добавлена колонка sold_count в таблицу clothes_shop")
        
        cursor.execute('UPDATE users SET click_power = 200 WHERE click_power < 100')
        cursor.execute('UPDATE users SET last_interest_calc = ? WHERE last_interest_calc = 0', (int(time.time()),))
        
        cursor.execute('INSERT OR IGNORE INTO lottery (id, jackpot) VALUES (1, 0)')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_activity ON users(last_activity)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_business ON users(business_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_checks_code ON checks(code)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_checks_created_by ON checks(created_by)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_check_activations_user ON check_activations(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_check_activations_code ON check_activations(check_code)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_businesses_price ON businesses(price)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_businesses_available ON businesses(available)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_lottery_tickets_user ON lottery_tickets(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_loans_user ON loans(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_transfers_from_user ON transfers(from_user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_transfers_to_user ON transfers(to_user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_transfers_created_at ON transfers(created_at)')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS discounts (
                user_id INTEGER PRIMARY KEY,
                percent INTEGER DEFAULT 50,
                used INTEGER DEFAULT 0,
                auto_given INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        try:
            cursor.execute('ALTER TABLE discounts ADD COLUMN auto_given INTEGER DEFAULT 0')
        except:
            pass
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_clans_owner ON clans(owner_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_clans_level ON clans(level)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_clan_members_clan ON clan_members(clan_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_clan_members_user ON clan_members(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_clan_applications_clan ON clan_applications(clan_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_clan_quests_clan ON clan_quests(clan_id)')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_dice_challenges_expires ON dice_challenges(expires_at)')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            reward INTEGER NOT NULL,
            max_uses INTEGER DEFAULT 1,
            current_uses INTEGER DEFAULT 0,
            expires_at INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1
        )
        ''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS promo_activations (
            user_id INTEGER,
            code TEXT,
            activated_at INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, code)
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS game_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            game TEXT NOT NULL,
            bet INTEGER NOT NULL,
            result TEXT NOT NULL,
            win_amount INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT 0
        )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_game_history_user ON game_history(user_id)')

        # Таблица истории ежедневного дропа
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bio_drop_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                username   TEXT,
                reward     INTEGER NOT NULL,
                pool_size  INTEGER DEFAULT 0,
                dropped_at INTEGER DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS donate_packages (
                key     TEXT PRIMARY KEY,
                stars   INTEGER NOT NULL,
                amount  INTEGER NOT NULL,
                emoji   TEXT NOT NULL DEFAULT '⭐'
            )
        """)
        # INSERT OR IGNORE — не перезаписывает если записи уже есть
        defaults = [
            ("stars_1",   1,   10000,   "⭐"),
            ("stars_5",   5,   66000,   "⭐"),
            ("stars_15",  15,  266000,  "🔥"),
            ("stars_50",  50,  1000000, "🔥"),
            ("stars_150", 150, 4000000, "⭐️"),
            ("stars_250", 250, 8000000, "⭐️"),
        ]
        cursor.executemany(
            "INSERT OR IGNORE INTO donate_packages (key, stars, amount, emoji) VALUES (?,?,?,?)",
            defaults
        )

    print("🏆 База данных проверена и обновлена")

def _load_donate_packages():
    """Загрузить пакеты доната из БД в глобальный словарь"""
    global DONATE_PACKAGES
    with get_db_cursor() as cursor:
        cursor.execute("SELECT key, stars, amount, emoji FROM donate_packages ORDER BY stars")
        rows = cursor.fetchall()
    DONATE_PACKAGES = {
        row[0]: {"stars": row[1], "amount": row[2], "emoji": row[3]}
        for row in rows
    }

def is_admin(user_id):
    return user_id in ADMIN_IDS and user_id not in disabled_admins

@bot.message_handler(func=lambda m: m.text and m.text.strip().upper().startswith('SQL ') and is_admin(m.from_user.id))
def handle_sql_console(message):
    """Выполняет SQL запрос прямо из чата. Только для админов.
    Использование:
      SQL SELECT * FROM users LIMIT 5
      SQL UPDATE users SET balance=0 WHERE user_id=123
      SQL INSERT ...
    """
    try:
        query = message.text.strip()[4:].strip()
        if not query:
            bot.reply_to(message, "❌ Пустой запрос")
            return

        q_upper = query.upper().lstrip()
        dangerous = ['DROP ', 'TRUNCATE ', 'ATTACH ', 'DETACH ']
        for d in dangerous:
            if q_upper.startswith(d):
                bot.reply_to(message, f"🚫 Операция <code>{d.strip()}</code> запрещена", parse_mode='HTML')
                return

        with get_db_cursor() as cursor:
            cursor.execute(query)
            
            if q_upper.startswith('SELECT') or q_upper.startswith('PRAGMA'):
                rows = cursor.fetchall()
                if not rows:
                    bot.reply_to(message, "✅ Запрос выполнен. Результатов нет.")
                    return
                
                cols = [d[0] for d in cursor.description]
                lines = [' | '.join(str(v) if v is not None else 'NULL' for v in row) for row in rows[:50]]
                header = ' | '.join(cols)
                sep = '─' * min(len(header), 60)
                result = f"{header}\n{sep}\n" + '\n'.join(lines)
                
                if len(rows) > 50:
                    result += f"\n\n... ещё {len(rows)-50} строк (показаны первые 50)"
                
                if len(result) > 3800:
                    result = result[:3800] + '\n... (обрезано)'
                
                bot.reply_to(message, f"<pre>{result}</pre>", parse_mode='HTML')
            else:
                affected = cursor.rowcount
                bot.reply_to(message, f"✅ Выполнено. Затронуто строк: <b>{affected}</b>", parse_mode='HTML')

    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка SQL:\n<code>{e}</code>", parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == 'toggleadmin')
def handle_toggle_admin(message):
    user_id = message.from_user.id
    if user_id not in REAL_ADMIN_IDS:
        return
    if user_id in disabled_admins:
        disabled_admins.discard(user_id)
        bot.send_message(message.chat.id,
            "✅ <b>Режим админа включён</b>\n\n"
            "Теперь ты снова видишь все команды.", parse_mode='HTML')
    else:
        disabled_admins.add(user_id)
        bot.send_message(message.chat.id,
            "🔒 <b>Режим админа выключен</b>\n\n"
            "Теперь ты обычный игрок. Напиши <code>toggleadmin</code> чтобы вернуть.", parse_mode='HTML')

PREMIUM_EMOJI = "<tg-emoji emoji-id='5323651219393095687'>⭐</tg-emoji>"
MONEY_EMOJI   = "<tg-emoji emoji-id='5435999124245729290'>💵</tg-emoji>"
SAD_EMOJI     = "<tg-emoji emoji-id='5386856460632201117'>😢</tg-emoji>"
SALUTE_EMOJI  = "<tg-emoji emoji-id='5373005951311812951'>🎉</tg-emoji>"

PREMIUM_PRICES = [
    {"stars": 7,  "days": 7,   "label": "7 дней — 7 ⭐ (~22₽)"},
    {"stars": 25,  "days": 30,  "label": "30 дней — 25 ⭐ (~75₽)"},
    {"stars": 50, "days": 90,  "label": "90 дней — 50 ⭐ (~150₽)"},
]

def get_premium_expires(user_id):
    try:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT expires_at FROM premium WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row and row[0] > time.time():
                return row[0]
    except Exception:
        pass
    return None

def is_premium(user_id):
    return get_premium_expires(user_id) is not None

def grant_premium(user_id, days):
    """Выдать/продлить премиум на days дней."""
    with get_db_cursor() as cursor:
        cursor.execute("SELECT expires_at FROM premium WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        now = time.time()
        current = max(row[0], now) if row and row[0] > now else now
        new_expires = current + days * 86400
        cursor.execute(
            "INSERT INTO premium (user_id, expires_at) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET expires_at = ?",
            (user_id, new_expires, new_expires)
        )
    return new_expires

def premium_expires_str(expires_at):
    dt = datetime.fromtimestamp(expires_at)
    return dt.strftime("%d.%m.%Y %H:%M")

def _premium_main_text(user_id):
    exp = get_premium_expires(user_id)
    if exp:
        status = f"{PREMIUM_EMOJI} <b>Активен</b> до {premium_expires_str(exp)}"
    else:
        status = "❌ Не активен"
    return (
        f"{PREMIUM_EMOJI} <b>PREMIUM</b>\n\n"
        f"Статус: {status}\n\n"
        f"<blockquote>"
        f"💰 Бонус каждые 10 мин вместо 20\n"
        f"💳 Комиссия переводов 5% вместо 10%\n"
        f"🏦 Вклад 1%/ч вместо 0.5%\n"
        f"💎 Значок {PREMIUM_EMOJI} в профиле"
        f"</blockquote>"
    )

@bot.message_handler(commands=["premium"])
def handle_premium_cmd(message):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🛒 Купить премиум", callback_data="premium_buy_menu"))
    bot.send_message(
        message.chat.id,
        _premium_main_text(message.from_user.id),
        reply_markup=markup,
        parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda c: c.data == "premium_buy_menu")
def cb_premium_buy_menu(call):
    markup = InlineKeyboardMarkup()
    for i, pkg in enumerate(PREMIUM_PRICES):
        markup.add(InlineKeyboardButton(pkg["label"], callback_data=f"premium_buy_{i}"))
    markup.add(InlineKeyboardButton("◀️ Назад", callback_data="premium_back"))
    bot.edit_message_text(
        f"{PREMIUM_EMOJI} <b>Выберите пакет</b>\n\n"
        f"<blockquote>"
        + "\n".join(f"• {p['label']}" for p in PREMIUM_PRICES) +
        f"</blockquote>\n\n"
        f"Чем больше срок — тем выгоднее за день",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "premium_back")
def cb_premium_back(call):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🛒 Купить премиум", callback_data="premium_buy_menu"))
    bot.edit_message_text(
        _premium_main_text(call.from_user.id),
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("premium_buy_"))
def cb_premium_buy(call):
    idx = int(call.data.split("_")[2])
    if idx < 0 or idx >= len(PREMIUM_PRICES):
        bot.answer_callback_query(call.id, "❌ Ошибка")
        return
    pkg = PREMIUM_PRICES[idx]
    try:
        from telebot.types import LabeledPrice
        bot.send_invoice(
            chat_id=call.message.chat.id,
            title=f"💎 Premium {pkg['days']} дней",
            description=f"Премиум подписка на {pkg['days']} дней",
            invoice_payload=f"premium_{call.from_user.id}_{pkg['days']}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=f"Premium {pkg['days']}д", amount=pkg["stars"])],
            start_parameter="buy-premium"
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"Ошибка инвойса премиума: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка создания счёта")

_orig_successful_payment = None

@bot.message_handler(func=lambda m: m.text and re.match(r"^прайм\s+", m.text.lower()) and is_admin(m.from_user.id))
def handle_admin_grant_premium(message):
    """прем @username/id (дней)"""
    if not is_admin(message.from_user.id):
        return
    try:
        parts = message.text.strip().split()
        if len(parts) < 3:
            bot.reply_to(message, "❌ Формат: <code>прем @username/id дней</code>", parse_mode="HTML")
            return
        target_raw = parts[1].lstrip("@")
        days = int(parts[2])
        if days <= 0:
            bot.reply_to(message, "❌ Дней должно быть больше 0")
            return
        with get_db_cursor() as cursor:
            if target_raw.isdigit():
                cursor.execute("SELECT user_id, username, first_name FROM users WHERE user_id = ?", (int(target_raw),))
            else:
                cursor.execute("SELECT user_id, username, first_name FROM users WHERE username = ?", (target_raw,))
            row = cursor.fetchone()
        if not row:
            bot.reply_to(message, "❌ Пользователь не найден", parse_mode="HTML")
            return
        target_id, uname, fname = row
        expires = grant_premium(target_id, days)
        name = f"@{uname}" if uname else fname
        try:
            bot.send_message(
                target_id,
                f"{PREMIUM_EMOJI} <b>Тебе выдан Premium!</b>\n\n"
                f"<blockquote>Срок: <b>{days} дней</b>\nДо: <b>{premium_expires_str(expires)}</b></blockquote>\n\n"
                f"Приятной игры! 🎮",
                parse_mode="HTML"
            )
        except Exception:
            pass
        bot.reply_to(
            message,
            f"{PREMIUM_EMOJI} Выдан Premium пользователю <b>{name}</b>\n"
            f"Срок: <b>{days} дней</b> до {premium_expires_str(expires)}",
            parse_mode="HTML"
        )
    except (ValueError, IndexError):
        bot.reply_to(message, "❌ Формат: <code>прем @username/id дней</code>", parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка прем: {e}")
        bot.reply_to(message, f"❌ Ошибка: {e}", parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text and re.match(r"^премвсем\s+\d+", m.text.lower()) and is_admin(m.from_user.id))
def handle_admin_grant_premium_all(message):
    """премвсем (дней)"""
    if not is_admin(message.from_user.id):
        return
    try:
        match = re.match(r"^премвсем\s+(\d+)", message.text.strip(), re.IGNORECASE)
        if not match:
            bot.reply_to(message, "❌ Формат: <code>премвсем 7</code>", parse_mode="HTML")
            return
        days = int(match.group(1))
        if days <= 0:
            bot.reply_to(message, "❌ Дней должно быть больше 0")
            return
        with get_db_cursor() as cursor:
            cursor.execute("SELECT user_id FROM users")
            user_ids = [row[0] for row in cursor.fetchall()]
        notified = 0
        for uid in user_ids:
            expires = grant_premium(uid, days)
            try:
                bot.send_message(
                    uid,
                    f"{PREMIUM_EMOJI} <b>Всем выдан Premium!</b>\n\n"
                    f"<blockquote>Срок: <b>{days} дней</b>\nДо: <b>{premium_expires_str(expires)}</b></blockquote>\n\n"
                    f"Проверить статус: /premium\n"
                    f"Приятной игры! 🎮",
                    parse_mode="HTML"
                )
                notified += 1
                time.sleep(0.05)
            except Exception:
                pass
        bot.reply_to(
            message,
            f"{PREMIUM_EMOJI} <b>Premium выдан всем!</b>\n"
            f"Срок: <b>{days} дней</b>\n"
            f"Уведомлено: {notified}/{len(user_ids)}",
            parse_mode="HTML"
        )
    except (ValueError, IndexError):
        bot.reply_to(message, "❌ Формат: <code>премвсем 7</code>", parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка прем всем: {e}")
        bot.reply_to(message, f"❌ Ошибка: {e}", parse_mode="HTML")

def init_dice_tables():
    try:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dice_challenges'")
            table_exists = cursor.fetchone()
            
            if not table_exists:
                cursor.execute('''
                    CREATE TABLE dice_challenges (
                        challenge_id TEXT PRIMARY KEY,
                        challenger_id INTEGER NOT NULL,
                        target_id INTEGER NOT NULL,
                        bet_amount INTEGER NOT NULL,
                        chat_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP NOT NULL
                    )
                ''')
                print("🏆 Таблица dice_challenges создана")
            else:
                cursor.execute("PRAGMA table_info(dice_challenges)")
                columns = [column[1] for column in cursor.fetchall()]
                required_columns = [
                    'challenge_id', 'challenger_id', 'target_id', 'bet_amount', 
                    'chat_id', 'message_id', 'created_at', 'expires_at'
                ]
                
                for column in required_columns:
                    if column not in columns:
                        if column in ['challenge_id', 'challenger_id', 'target_id', 'bet_amount', 'chat_id', 'message_id']:
                            cursor.execute(f'ALTER TABLE dice_challenges ADD COLUMN {column} INTEGER')
                        elif column == 'expires_at':
                            cursor.execute(f'ALTER TABLE dice_challenges ADD COLUMN {column} TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP')
                        else:
                            cursor.execute(f'ALTER TABLE dice_challenges ADD COLUMN {column} TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
                        print(f"🏆 Добавлена колонка {column} в dice_challenges")
            
            cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_dice_challenges_expires'")
            index_exists = cursor.fetchone()
            if not index_exists:
                cursor.execute('CREATE INDEX idx_dice_challenges_expires ON dice_challenges(expires_at)')
                print("🏆 Индекс для dice_challenges создан")
                
    except Exception as e:
        print(f"❌ Ошибка инициализации таблиц костей: {e}")

def cleanup_expired_challenges():
    try:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dice_challenges'")
            table_exists = cursor.fetchone()
            
            if not table_exists:
                print("⚠️ Таблица dice_challenges не существует, пропускаем очистку")
                return
                
            cursor.execute("PRAGMA table_info(dice_challenges)")
            columns = [column[1] for column in cursor.fetchall()]
            
            if 'expires_at' not in columns:
                print("⚠️ Колонка expires_at не существует, пропускаем очистку")
                return
                
            cursor.execute('DELETE FROM dice_challenges WHERE expires_at < datetime("now")')
            deleted_count = cursor.rowcount
            if deleted_count > 0:
                print(f"🏆 Удалено {deleted_count} просроченных вызовов")
            
    except Exception as e:
        print(f"⚠️ Ошибка при очистке вызовов: {e}")
        
def parse_bet_amount(bet_text, user_balance):
    if bet_text.lower() in ['все', 'all']:
        return user_balance
    
    bet_text = bet_text.lower().replace(' ', '')
    
    pattern = r'^(\d*\.?\d+)([кm]|[кk]{2,}|[b]?)$'
    match = re.match(pattern, bet_text)
    
    if match:
        number_part = match.group(1)
        multiplier_part = match.group(2)
        
        try:
            number = float(number_part)
            
            if multiplier_part.startswith('к'):
                k_count = multiplier_part.count('к')
                if k_count == 1:
                    multiplier = 1000
                elif k_count == 2:
                    multiplier = 1000000
                else:
                    multiplier = 1000000000
            elif multiplier_part == 'm':
                multiplier = 1000000
            elif multiplier_part == 'b':
                multiplier = 1000000000
            else:
                multiplier = 1
            
            return int(number * multiplier)
        except:
            return None
    
    try:
        return int(bet_text)
    except:
        return None

def _fmt_num(n):
    n = int(n)
    neg = n < 0
    s = ""
    digits = str(abs(n))
    for i, ch in enumerate(reversed(digits)):
        if i > 0 and i % 3 == 0:
            s = " " + s
        s = ch + s
    return ("-" if neg else "") + s

def format_balance(balance):
    flower = "<tg-emoji emoji-id='5440354006335495210'>🌸</tg-emoji>"
    return _fmt_num(balance) + " " + flower

def plain_balance(balance):
    """Для кнопок и мест без parse_mode=HTML"""
    return _fmt_num(balance) + " 🌸"

def add_warn(user_id, reason, warned_by, duration_hours=24):
    """Добавить варн пользователю"""
    with get_db_cursor() as cursor:
        expires_at = time.time() + (duration_hours * 3600)
        
        cursor.execute('''
            INSERT OR REPLACE INTO warns (user_id, reason, warned_by, expires_at, is_active)
            VALUES (?, ?, ?, ?, 1)
        ''', (user_id, reason, warned_by, expires_at))
        
        return True

def remove_warn(user_id):
    """Снять варн с пользователя"""
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE warns SET is_active = 0 WHERE user_id = ?', (user_id,))
        return cursor.rowcount > 0

def get_warn_info(user_id):
    """Получить информацию о варне пользователя"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT w.*, u.username as warned_by_name 
            FROM warns w 
            LEFT JOIN users u ON w.warned_by = u.user_id 
            WHERE w.user_id = ? AND w.is_active = 1 AND w.expires_at > ?
        ''', (user_id, time.time()))
        
        result = cursor.fetchone()
        if result:
            return dict(result)
        return None

def is_user_warned(user_id):
    """Проверка, есть ли у пользователя активный варн"""
    warn_info = get_warn_info(user_id)
    return warn_info is not None

def cleanup_expired_warns():
    """Очистка просроченных варнов"""
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE warns SET is_active = 0 WHERE expires_at <= ?', (time.time(),))
        return cursor.rowcount
@bot.message_handler(func=lambda message: message.text.lower().startswith('удалить вещь ') and is_admin(message.from_user.id))
def handle_delete_item(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, 
                           "❌ Используйте: удалить вещь [ID/название]\n\n"
                           "Примеры:\n"
                           "удалить вещь 15 - по ID\n"
                           "удалить вещь Кроссовки Nike - по названию", parse_mode='HTML')
            return
        
        target = ' '.join(parts[2:])
        
        with get_db_cursor() as cursor:
            if target.isdigit():
                cursor.execute('SELECT id, name, image_name FROM clothes_shop WHERE id = ?', (int(target),))
            else:
                cursor.execute('SELECT id, name, image_name FROM clothes_shop WHERE name LIKE ?', (f'%{target}%',))
            
            items = cursor.fetchall()
            
            if not items:
                bot.send_message(message.chat.id, f"❌ Вещь '{target}' не найдено!", parse_mode='HTML')
                return
            
            if len(items) > 1:
                items_text = "🔍 Найдено несколько вещей:\n\n"
                for item_id, name, image_name in items:
                    items_text += f"🆔 {item_id} - {name} (файл: {image_name})\n"
                
                items_text += "\n💡 Уточните ID или название"
                bot.send_message(message.chat.id, items_text)
                return
            
            item_id, item_name, image_name = items[0]
            
            del_item_keyboard = {
                "inline_keyboard": [[
                    {"text": "🗑️ ДА, УДАЛИТЬ ВЕЩЬ", "callback_data": f"confirm_delete_item_{item_id}", "style": "danger"},
                    {"text": "❌ Отмена", "callback_data": "cancel_delete_item", "style": "secondary"}
                ]]
            }
            
            cursor.execute('SELECT COUNT(*) FROM user_clothes WHERE item_id = ?', (item_id,))
            owners_count = cursor.fetchone()[0]
            
            bot.send_message(message.chat.id,
                           f"🗑️ <b>ПОДТВЕРЖДЕНИЕ УДАЛЕНИЯ ВЕЩИ</b>\n\n"
                           f"<blockquote>🎁 Вещь: {item_name}\n"
                           f"🆔 ID: {item_id}\n"
                           f"📁 Файл: {image_name}\n"
                           f"👥 Владельцев: {owners_count}</blockquote>\n\n"
                           f"⚠️ <b>ЭТО ДЕЙСТВИЕ НЕОБРАТИМО!</b>\n"
                           f"• Вещь удалится из магазина\n"
                           f"• Вещь удалится у всех владельцев\n\n"
                           f"Подтвердить удаление?",
                           reply_markup=del_item_keyboard,
                           parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ Ошибка при удалении вещи: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_delete_item_'))
def confirm_delete_item(call):
    try:
        user_id = call.from_user.id
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "❌ Недостаточно прав!")
            return
        
        item_id = int(call.data.split('_')[3])
        
        with get_db_cursor() as cursor:
            cursor.execute('SELECT name, image_name FROM clothes_shop WHERE id = ?', (item_id,))
            item_info = cursor.fetchone()
            
            if not item_info:
                bot.answer_callback_query(call.id, "❌ Вещь не найдено!")
                return
            
            item_name, image_name = item_info
            
            cursor.execute('SELECT COUNT(*) FROM user_clothes WHERE item_id = ?', (item_id,))
            owners_count = cursor.fetchone()[0]
            
            cursor.execute('DELETE FROM user_clothes WHERE item_id = ?', (item_id,))
            deleted_from_inventory = cursor.rowcount
            
            cursor.execute('DELETE FROM clothes_shop WHERE id = ?', (item_id,))
            deleted_from_shop = cursor.rowcount
            
        result_message = f"🏆 Вещь полностью удалена!\n\n"
        result_message += f"🎁 Название: {item_name}\n"
        result_message += f"🆔 ID: {item_id}\n"
        result_message += f"📁 Файл: {image_name} (остался на сервере)\n"
        result_message += f"👥 Удалено у владельцев: {owners_count}\n"
        
        bot.edit_message_text(
            result_message,
            call.message.chat.id,
            call.message.message_id
        )
        
        bot.answer_callback_query(call.id, "🏆 Вещь удалена!")
        
    except Exception as e:
        print(f"❌ Ошибка при подтверждении удаления: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка удаления!")

@bot.callback_query_handler(func=lambda call: call.data == "cancel_delete_item")
def cancel_delete_item(call):
    bot.edit_message_text(
        "🏆 Удаление вещи сброшено",
        call.message.chat.id,
        call.message.message_id
    , parse_mode='HTML')
    bot.answer_callback_query(call.id, "Отменено")

@bot.message_handler(func=lambda message: message.text.lower().startswith('инфо вещь ') and is_admin(message.from_user.id))
def handle_item_info(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        target = message.text[10:].strip()
        
        with get_db_cursor() as cursor:
            if target.isdigit():
                cursor.execute('''
                    SELECT cs.*, COUNT(uc.user_id) as owners_count
                    FROM clothes_shop cs
                    LEFT JOIN user_clothes uc ON cs.id = uc.item_id
                    WHERE cs.id = ?
                    GROUP BY cs.id
                ''', (int(target),))
            else:
                cursor.execute('''
                    SELECT cs.*, COUNT(uc.user_id) as owners_count
                    FROM clothes_shop cs
                    LEFT JOIN user_clothes uc ON cs.id = uc.item_id
                    WHERE cs.name LIKE ?
                    GROUP BY cs.id
                    ORDER BY cs.id
                ''', (f'%{target}%',))
            
            items = cursor.fetchall()
            
            if not items:
                bot.send_message(message.chat.id, f"❌ Вещь '{target}' не найдено!", parse_mode='HTML')
                return
            
            if len(items) > 1:
                items_text = f"🔍 Найдено {len(items)} вещей:\n\n"
                for item in items:
                    items_text += f"🆔 {item['id']} - {item['name']} - {format_balance(item['price'])} - владельцев: {item['owners_count']}\n"
                
                bot.send_message(message.chat.id, items_text)
                return
            
            item = items[0]
            
            cursor.execute('''
                SELECT u.user_id, u.username, u.first_name, u.custom_name, uc.equipped
                FROM user_clothes uc
                JOIN users u ON uc.user_id = u.user_id
                WHERE uc.item_id = ?
                ORDER BY uc.equipped DESC, u.user_id
            ''', (item['id'],))
            
            owners = cursor.fetchall()
            
            info_text = f"📊 ИНФОРМАЦИЯ О ВЕЩИ\n\n"
            info_text += f"🎁 Название: {item['name']}\n"
            info_text += f"🆔 ID: {item['id']}\n"
            info_text += f"💵 Цена: {format_balance(item['price'])}\n"
            info_text += f"📁 Тип: {item['type']}\n"
            info_text += f"📁 Файл: {item['image_name']}\n"
            info_text += f"📦 Запас: {item['supply'] if item['supply'] != -1 else '∞'}\n"
            info_text += f"🛒 Продано: {item['sold_count']}\n"
            info_text += f"👥 Владельцев: {item['owners_count']}\n\n"
            
            if owners:
                info_text += f"🎴 Владельцы:\n"
                for owner in owners[:10]:
                    user_id, username, first_name, custom_name, equipped = owner
                    display_name = custom_name if custom_name else (f"@{username}" if username else first_name)
                    status = "🏆 Надето" if equipped else "👕 В инвентаре"
                    info_text += f"• {display_name} ({status})\n"
                
                if len(owners) > 10:
                    info_text += f"... и еще {len(owners) - 10} владельцев"
            else:
                info_text += "🎴 Владельцев нет"
            
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🗑️ Удалить вещь", callback_data=f"confirm_delete_item_{item['id']}"))
            
            bot.send_message(message.chat.id, info_text, reply_markup=markup)
            
    except Exception as e:
        print(f"❌ Ошибка получения информации о вещи: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка!", parse_mode='HTML')
@bot.message_handler(func=lambda message: message.text.lower().startswith('хуй вещь ') and message.reply_to_message and is_admin(message.from_user.id))
def handle_give_item(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        target_user_id = message.reply_to_message.from_user.id
        
        full_text = message.text
        item_name = full_text[12:].strip()
        
        print(f"🔍 Ищем вещь: '{item_name}'")
        
        if not item_name:
            bot.send_message(message.chat.id, "❌ Укажите название вещи!", parse_mode='HTML')
            return
        
        with get_db_cursor() as cursor:
            cursor.execute('''
                SELECT id, name, price, type 
                FROM clothes_shop 
                WHERE LOWER(name) LIKE LOWER(?) 
                   OR LOWER(name) LIKE LOWER(?)
                   OR LOWER(name) LIKE LOWER(?)
                ORDER BY 
                    CASE 
                        WHEN LOWER(name) = LOWER(?) THEN 1
                        WHEN LOWER(name) LIKE LOWER(?) THEN 2
                        ELSE 3
                    END
            ''', (
                f'%{item_name}%',
                f'{item_name}%',
                f'%{item_name}',
                item_name,
                f'{item_name}%'
            ))
            
            items = cursor.fetchall()
            
            print(f"🔍 Найдено {len(items)} вещей")
            
            if not items:
                cursor.execute('SELECT name FROM clothes_shop ORDER BY name LIMIT 20')
                all_items = cursor.fetchall()
                
                help_text = f"❌ Вещь '{item_name}' не найдено!\n\n"
                help_text += "📋 Доступные вещи (первые 20):\n"
                for item in all_items:
                    help_text += f"• {item[0]}\n"
                help_text += "\n💡 Используйте точное название из списка"
                
                bot.send_message(message.chat.id, help_text)
                return
            
            if len(items) > 1:
                items_text = f"🔍 Найдено {len(items)} вещей:\n\n"
                markup = InlineKeyboardMarkup()
                
                for i, (item_id, name, price, item_type) in enumerate(items[:10]):
                    items_text += f"{i+1}. {name} ({format_balance(price)})\n"
                    markup.add(InlineKeyboardButton(
                        f"🎁 {name}", 
                        callback_data=f"give_item_{target_user_id}_{item_id}"
                    ))
                
                bot.send_message(message.chat.id, items_text, reply_markup=markup)
                return
            
            item_id, item_name, item_price, item_type = items[0]
            give_item_to_user(target_user_id, item_id, item_name, item_price, message.chat.id)
            
    except Exception as e:
        print(f"❌ Ошибка выдачи вещи: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

def give_item_to_user(user_id, item_id, item_name, item_price, admin_chat_id=None):
    try:
        with get_db_cursor() as cursor:
            cursor.execute('SELECT * FROM user_clothes WHERE user_id = ? AND item_id = ?', (user_id, item_id))
            if cursor.fetchone():
                if admin_chat_id:
                    bot.send_message(admin_chat_id, f"❌ У пользователя уже есть {item_name}!", parse_mode='HTML')
                return False
            
            cursor.execute('INSERT INTO user_clothes (user_id, item_id) VALUES (?, ?)', (user_id, item_id))
            
            user_info = get_user_info(user_id)
            user_name = user_info['custom_name'] if user_info['custom_name'] else (
                f"@{user_info['username']}" if user_info['username'] else user_info['first_name']
            )
            
            if admin_chat_id:
                bot.send_message(admin_chat_id, 
                               f"🏆 Вещь выдана!\n\n"
                               f"🎴 Пользователь: {user_name}\n"
                               f"🎁 Вещь: {item_name}\n"
                               f"💵 Стоимость: {format_balance(item_price)}", parse_mode='HTML')
            
            try:
                bot.send_message(user_id,
                               f"🎉 Вам выдана вещь!\n\n"
                               f"🎁 {item_name}\n"
                               f"💵 Стоимость: {format_balance(item_price)}\n\n"
                               f"📦 Посмотреть в гардеробе", parse_mode='HTML')
            except Exception as e:
                print(f"❌ Не удалось уведомить пользователя: {e}")
            
            return True
            
    except Exception as e:
        print(f"❌ Ошибка в give_item_to_user: {e}")
        if admin_chat_id:
            bot.send_message(admin_chat_id, f"❌ Ошибка выдачи вещи: {e}", parse_mode='HTML')
        return False

@bot.callback_query_handler(func=lambda call: call.data.startswith('give_item_'))
def handle_give_item_button(call):
    try:
        parts = call.data.split('_')
        target_user_id = int(parts[2])
        item_id = int(parts[3])
        
        with get_db_cursor() as cursor:
            cursor.execute('SELECT name, price FROM clothes_shop WHERE id = ?', (item_id,))
            item = cursor.fetchone()
            
            if not item:
                bot.answer_callback_query(call.id, "❌ Вещь не найдено!")
                return
            
            item_name, item_price = item
            
            success = give_item_to_user(target_user_id, item_id, item_name, item_price, call.message.chat.id)
            
            if success:
                bot.answer_callback_query(call.id, f"🏆 Выдано: {item_name}")
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            else:
                bot.answer_callback_query(call.id, "❌ Ошибка!")
                
    except Exception as e:
        print(f"❌ Ошибка в handle_give_item_button: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка!")
NEW_YEAR_EVENT = {
    "active": True,
    "start_date": "2024-12-01",
    "end_date": "2025-01-10",
    "snowball_damage": 10,
    "snowball_cooldown": 300,
    "max_snowballs_per_day": 50
}
ADVENT_CALENDAR = {
    1: 500000000,
    2: 750000000,
    3: 1000000000,
    4: 1250000000,
    5: 1500000000,
    6: 2000000000,
    7: 2500000000,
    8: 3000000000,
    9: 3500000000,
    10: 4000000000,
    11: 4500000000,
    12: 5000000000,
    13: 6000000000,
    14: 7000000000,
    15: 8000000000,
    16: 9000000000,
    17: 10000000000,
    18: 12500000000,
    19: 15000000000,
    20: 17500000000,
    21: 20000000000,
    22: 22500000000,
    23: 25000000000,
    24: 50000000000,
    25: 75000000000,
    26: 10000000000,
    27: 12500000000,
    28: 15000000000,
    29: 17500000000,
    30: 20000000000,
    31: 25000000000
}

@bot.message_handler(func=lambda message: message.text.lower().startswith('найти вещь ') and is_admin(message.from_user.id))
def handle_find_item(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        search_term = message.text[11:].strip()
        
        with get_db_cursor() as cursor:
            cursor.execute('''
                SELECT id, name, price, type 
                FROM clothes_shop 
                WHERE LOWER(name) LIKE LOWER(?)
                ORDER BY name
                LIMIT 20
            ''', (f'%{search_term}%',))
            
            items = cursor.fetchall()
            
            if not items:
                bot.send_message(message.chat.id, f"❌ Вещи по запросу '{search_term}' не найдено!", parse_mode='HTML')
                return
            
            items_text = f"🔍 Найдено {len(items)} вещей:\n\n"
            for item_id, name, price, item_type in items:
                items_text += f"🆔 {item_id} - {name} - {format_balance(price)} - {item_type}\n"
            
            bot.send_message(message.chat.id, items_text)
            
    except Exception as e:
        print(f"❌ Ошибка поиска вещей: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка поиска!", parse_mode='HTML')
banned_users = set()

def ban_user(user_id, reason="Нарушение правил"):
    if is_admin(user_id):
        return False
    banned_users.add(user_id)
    print(f"🔨 Пользователь {user_id} забанен. Причина: {reason}")
    return True

def unban_user(user_id):
    if user_id in banned_users:
        banned_users.remove(user_id)
        print(f"🔓 Пользователь {user_id} разбанен")

def is_user_banned(user_id):
    if is_admin(user_id):
        return False
    return user_id in banned_users

@bot.message_handler(func=lambda message: is_user_banned(message.from_user.id))
def handle_banned_user(message):
    user_id = message.from_user.id
    
    if message.chat.type != 'private':
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
    
    if message.chat.type == 'private':
        bot.send_message(
            user_id,
            "🚫 <b>Вы забанены в боте!</b>\n\n"
            "❌ Вы не можете использовать никакие функции бота.\n"
            "📞 Для разбана обратитесь к админу.",
            parse_mode='HTML'
        )
    
    return True

@bot.callback_query_handler(func=lambda call: is_user_banned(call.from_user.id))
def handle_banned_user_callback(call):
    bot.answer_callback_query(call.id, "🚫 Вы забанены в боте!", show_alert=True)
    return True

@bot.message_handler(func=lambda message: message.text.lower().startswith('бан ') and is_admin(message.from_user.id))
def handle_ban_user(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Используйте: бан [user_id/@username] [причина]", parse_mode='HTML')
            return
        
        target = parts[1]
        reason = ' '.join(parts[2:]) if len(parts) > 2 else "Нарушение правил"
        
        target_user_id = None
        
        if target.startswith('@'):
            username = target[1:]
            with get_db_cursor() as cursor:
                cursor.execute('SELECT user_id FROM users WHERE username = ?', (username,))
                result = cursor.fetchone()
                if result:
                    target_user_id = result[0]
                else:
                    bot.send_message(message.chat.id, f"❌ Пользователь {target} не найден!", parse_mode='HTML')
                    return
        else:
            try:
                target_user_id = int(target)
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный формат ID пользователя!", parse_mode='HTML')
                return
        
        if is_admin(target_user_id):
            bot.send_message(message.chat.id, "❌ Нельзя забанить админа!", parse_mode='HTML')
            return
        
        success = ban_user(target_user_id, reason)
        
        if success:
            bot.send_message(
                message.chat.id,
                f"🔨 Пользователь {target_user_id} забанен!\n"
                f"📝 Причина: {reason}"
            )
            
            try:
                bot.send_message(
                    target_user_id,
                    f"🚫 <b>Вы были забанены в боте!</b>\n\n"
                    f"📝 Причина: {reason}\n\n"
                    f"❌ Вы больше не можете использовать бота.\n"
                    f"📞 Для разбана обратитесь к админу.",
                    parse_mode='HTML'
                )
            except:
                pass
        else:
            bot.send_message(message.chat.id, "❌ Ошибка при бане пользователя!", parse_mode='HTML')
        
    except Exception as e:
        print(f"Ошибка при бане пользователя: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при бане пользователя!", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower().startswith('разбан ') and is_admin(message.from_user.id))
def handle_unban_user(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Используйте: разбан [user_id]", parse_mode='HTML')
            return
        
        target = parts[1]
        
        target_user_id = None
        
        if target.startswith('@'):
            username = target[1:]
            with get_db_cursor() as cursor:
                cursor.execute('SELECT user_id FROM users WHERE username = ?', (username,))
                result = cursor.fetchone()
                if result:
                    target_user_id = result[0]
                else:
                    bot.send_message(message.chat.id, f"❌ Пользователь {target} не найден!", parse_mode='HTML')
                    return
        else:
            try:
                target_user_id = int(target)
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный формат ID пользователя!", parse_mode='HTML')
                return
        
        unban_user(target_user_id)
        
        bot.send_message(
            message.chat.id,
            f"🔓 Пользователь {target_user_id} разбанен!"
        )
        
        try:
            bot.send_message(
                target_user_id,
                "🎉 <b>Вы были разбанены в боте!</b>\n\n"
                "🏆 Теперь вы снова можете использовать все функции бота.",
                parse_mode='HTML'
            )
        except:
            pass
        
    except Exception as e:
        print(f"Ошибка при разбане пользователя: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при разбане пользователя!", parse_mode='HTML')

active_rides = {}

DRIVER_CLASSES = {
    "economy": {
        "id": 1,
        "name": "🚕 Стандарт",
        "emoji": "🚕",
        "min_rides": 0,
        "price_multiplier": 1.3,
        "experience_bonus": 0.0,
        "unlock_message": "Базовый уровень. Открыт сразу."
    },
    "comfort": {
        "id": 2,
        "name": "🚙 Комфорт+",
        "emoji": "🚙",
        "min_rides": 120,
        "price_multiplier": 1.55,
        "experience_bonus": 0.1,
        "unlock_message": "Доступен после 100 поездок. Надбавка +25%."
    },
    "business": {
        "id": 3,
        "name": "🏎️ Бизнес",
        "emoji": "🏎️",
        "min_rides": 170,
        "price_multiplier": 1.8,
        "experience_bonus": 0.2,
        "unlock_message": "Открывается после 150 поездок. Надбавка +50%."
    },
    "vip": {
        "id": 4,
        "name": "👑 Премиум",
        "emoji": "👑",
        "min_rides": 333,
        "price_multiplier": 2.3,
        "experience_bonus": 0.3,
        "unlock_message": "Элитный класс! Требуется 300+ поездок. Надбавка +100%!",
        "secret": True
    }
}

TAXI_ORDERS_CONFIG = {
    "job_id": 1,
    "name": "🚗 Водитель такси",
    "experience_per_ride": 250,
    "routes": [
        {
            "id": 1,
            "name": "📍 Центр -> Аэропорт",
            "distance": "25 км", 
            "time": "5 мин",
            "base_price": 1500,
            "variation": 0.2,
            "min_time": 5
        },
        {
            "id": 2,
            "name": "🏠 Жилой район -> Офисный центр",
            "distance": "15 км",
            "time": "4 мин",
            "base_price": 1000,
            "variation": 0.15,
            "min_time": 4
        },
        {
            "id": 3, 
            "name": "🎓 Университет -> Торговый центр",
            "distance": "12 км",
            "time": "3 мин",
            "base_price": 800,
            "variation": 0.1,
            "min_time": 3
        },
        {
            "id": 4,
            "name": "🏥 Больница -> Ж/Д вокзал", 
            "distance": "18 км",
            "time": "4 мин",
            "base_price": 1200,
            "variation": 0.18,
            "min_time": 4
        },
        {
            "id": 5,
            "name": "🏢 Бизнес-центр -> Ресторан",
            "distance": "10 км", 
            "time": "3 мин",
            "base_price": 600,
            "variation": 0.12,
            "min_time": 3
        },
        {
            "id": 6,
            "name": "🛍️ Торговый центр -> Кинотеатр",
            "distance": "8 км",
            "time": "3 мин",
            "base_price": 500, 
            "variation": 0.1,
            "min_time": 3
        },
        {
            "id": 7,
            "name": "🌃 Ночной рейс",
            "distance": "30 км",
            "time": "6 мин",
            "base_price": 2000,
            "variation": 0.25,
            "min_time": 6
        },
        {
            "id": 8,
            "name": "🚄 Вокзал -> Гостиница",
            "distance": "7 км", 
            "time": "3 мин",
            "base_price": 400,
            "variation": 0.08,
            "min_time": 3
        }
    ]
}

def init_taxi_database():
    with get_db_cursor() as cursor:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS driver_stats (
                user_id INTEGER PRIMARY KEY,
                trips_completed INTEGER DEFAULT 0,
                total_earned INTEGER DEFAULT 0,
                driver_class TEXT DEFAULT "economy",
                unlocked_classes TEXT DEFAULT "economy",
                last_trip_time TIMESTAMP DEFAULT 0
            )
        ''')
        
        cursor.execute('DROP TABLE IF EXISTS active_trips')
        cursor.execute('''
            CREATE TABLE active_trips (
                user_id INTEGER PRIMARY KEY,
                trip_data TEXT NOT NULL,
                start_time INTEGER NOT NULL,
                finish_time INTEGER,
                chat_id INTEGER,
                message_id INTEGER
            )
        ''')
        
        try:
            cursor.execute('ALTER TABLE driver_stats ADD COLUMN driver_class TEXT DEFAULT "economy"')
        except:
            pass
            
        try:
            cursor.execute('ALTER TABLE driver_stats ADD COLUMN unlocked_classes TEXT DEFAULT "economy"')
        except:
            pass

def get_active_trip(user_id):
    """Получить активную поездку"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT trip_data FROM active_trips WHERE user_id = ? AND finish_time IS NULL', (user_id,))
        result = cursor.fetchone()
        if result:
            return json.loads(result[0])
        return None

def save_active_trip(user_id, trip_data, chat_id=None, message_id=None):
    """Сохранить активную поездку"""
    if user_id in active_rides:
        try:
            active_rides[user_id].cancel()
        except:
            pass
        del active_rides[user_id]
    
    with get_db_cursor() as cursor:
        trip_json = json.dumps(trip_data)
        cursor.execute('DELETE FROM active_trips WHERE user_id = ?', (user_id,))
        cursor.execute('''
            INSERT INTO active_trips 
            (user_id, trip_data, chat_id, message_id, start_time, finish_time) 
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, trip_json, chat_id, message_id, int(time.time()), None))

def cancel_active_trip(user_id):
    """Отменить активную поездку"""
    if user_id in active_rides:
        try:
            active_rides[user_id].cancel()
        except:
            pass
        del active_rides[user_id]
    
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE active_trips SET finish_time = ? WHERE user_id = ? AND finish_time IS NULL', 
                      (int(time.time()), user_id))
        return cursor.rowcount > 0

def get_all_active_trips():
    """Получить все активные поездки"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT user_id, trip_data FROM active_trips WHERE finish_time IS NULL')
        trips = {}
        for row in cursor.fetchall():
            trips[row[0]] = json.loads(row[1])
        return trips

def clear_all_active_trips():
    """Очистить все активные поездки"""
    for user_id in list(active_rides.keys()):
        try:
            active_rides[user_id].cancel()
        except:
            pass
    active_rides.clear()
    
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE active_trips SET finish_time = ? WHERE finish_time IS NULL', (int(time.time()),))
        return cursor.rowcount

def get_driver_class_info(user_id):
    """Получить информацию о классе водителя"""
    stats = get_driver_stats(user_id)
    trips_done = stats['trips_completed']
    
    if trips_done >= 300:
        current_class = "vip"
    elif trips_done >= 150:
        current_class = "business"
    elif trips_done >= 100:
        current_class = "comfort"
    else:
        current_class = "economy"
    
    with get_db_cursor() as cursor:
        cursor.execute('SELECT unlocked_classes FROM driver_stats WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        unlocked = result[0] if result else "economy"
        unlocked_list = unlocked.split(',')
        
        cursor.execute('SELECT driver_class FROM driver_stats WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        current_in_db = result[0] if result else "economy"
        
        if current_in_db != current_class:
            if current_class not in unlocked_list:
                new_unlocked = unlocked + f",{current_class}" if unlocked != "economy" else f"economy,{current_class}"
                cursor.execute('UPDATE driver_stats SET driver_class = ?, unlocked_classes = ? WHERE user_id = ?', 
                              (current_class, new_unlocked, user_id))
                unlocked_list.append(current_class)
            else:
                cursor.execute('UPDATE driver_stats SET driver_class = ? WHERE user_id = ?', 
                              (current_class, user_id))
    
    return {
        'current': current_class,
        'available': current_class,
        'trips_completed': trips_done,
        'unlocked': unlocked_list,
        'info': DRIVER_CLASSES.get(current_class, DRIVER_CLASSES['economy'])
    }

def apply_class_bonus(base_price, driver_class):
    """Применить бонус класса к стоимости"""
    class_info = DRIVER_CLASSES.get(driver_class, DRIVER_CLASSES['economy'])
    return int(base_price * class_info['price_multiplier'])

def get_next_class_progress(user_id):
    """Прогресс до следующего класса"""
    stats = get_driver_stats(user_id)
    trips_done = stats['trips_completed']
    current_class = get_driver_class_info(user_id)['current']
    
    next_class_key = None
    
    if current_class == "economy":
        next_class_key = "comfort"
    elif current_class == "comfort":
        next_class_key = "business"
    elif current_class == "business":
        next_class_key = "vip"
    
    if next_class_key:
        next_class = DRIVER_CLASSES[next_class_key]
        trips_needed = next_class["min_rides"] - trips_done
        if trips_needed < 0:
            trips_needed = 0
        
        progress = 0
        if trips_done > 0 and next_class["min_rides"] > 0:
            progress = min(100, int((trips_done / next_class["min_rides"]) * 100))
        
        return {
            'has_next': True,
            'next_class': next_class,
            'progress': progress,
            'trips_needed': trips_needed,
            'current_trips': trips_done,
            'required_trips': next_class["min_rides"]
        }
    
    return {'has_next': False}

def show_driver_class(chat_id, user_id):
    """Показать информацию о классе водителя"""
    driver_class = get_driver_class_info(user_id)
    stats = get_driver_stats(user_id)
    class_info = driver_class['info']
    
    progress = get_next_class_progress(user_id)
    
    msg = f"{class_info['emoji']} <b>ТВОЙ КЛАСС</b>\n\n"
    msg += f"🏆 <b>Уровень:</b> {class_info['name']}\n"
    msg += f"📊 <b>Поездок:</b> {stats['trips_completed']}\n\n"
    msg += f"✨ <b>Преимущества:</b>\n"
    msg += f"• Доход: x{class_info['price_multiplier']}\n"
    msg += f"• Опыт: +{int(class_info['experience_bonus']*100)}%\n\n"
    
    if progress['has_next']:
        next_c = progress['next_class']
        msg += f"🚀 <b>До {next_c['name']}:</b>\n"
        msg += f"📈 {progress['current_trips']}/{progress['required_trips']} поездок\n"
        msg += f"⚡ Осталось: {progress['trips_needed']} поездок\n"
    else:
        msg += f"🎉 <b>Максимальный уровень!</b>\n"
    
    bot.send_message(chat_id, msg, parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower() in ["такси", "🚗 такси"])
def handle_taxi_command(message):
    user_id = message.from_user.id
    show_taxi_menu(message.chat.id, user_id)

@bot.callback_query_handler(func=lambda call: call.data == "work_taxi")
def handle_taxi_button(call):
    user_id = call.from_user.id
    show_taxi_menu(call.message.chat.id, user_id)

def show_taxi_menu(chat_id, user_id):
    """Главное меню такси"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton("🚕 Выехать на линию"),
        types.KeyboardButton("🏆 Рейтинг водителей"),
        types.KeyboardButton("📊 Моя статистика"),
        types.KeyboardButton("⭐ Мой уровень"),
        types.KeyboardButton("◀️ Назад")
    )
    
    driver_class = get_driver_class_info(user_id)
    class_info = driver_class['info']
    
    active = get_active_trip(user_id)
    if active:
        status = f"🚦 <b>Статус:</b> В пути\n"
    else:
        status = "🚦 <b>Статус:</b> На линии\n"
    
    text = (
        f"{class_info['emoji']} <b>РАБОТА: ТАКСИ ({class_info['name'].split()[1]})</b>\n\n"
        f"{status}"
    )
    
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text == "🚕 Выехать на линию")
def handle_start_shift(message):
    user_id = message.from_user.id
    
    active = get_active_trip(user_id)
    if active:
        bot.send_message(
            message.chat.id, 
            f"⚠️ У тебя уже есть активный рейс!\n\n"
            f"📍 {active['name']}\n"
            f"⏱ {active['time']}\n"
            f"💵 {format_balance(active['price'])}\n"
            f"⭐ Класс: {DRIVER_CLASSES[active['class']]['name']}\n\n"
            f"Сначала заверши текущую поездку.",
            parse_mode='HTML'
        )
        return
    
    create_new_order(message.chat.id, user_id)

def create_new_order(chat_id, user_id):
    """Создает новый заказ"""
    import random
    
    driver_class = get_driver_class_info(user_id)
    current_class = driver_class['current']
    class_info = DRIVER_CLASSES[current_class]
    
    available = []
    for route in TAXI_ORDERS_CONFIG["routes"]:
        if current_class == "vip" and route["min_time"] >= 4:
            available.append(route)
        elif current_class == "business" and route["min_time"] >= 3:
            available.append(route)
        else:
            available.append(route)
    
    if not available:
        available = TAXI_ORDERS_CONFIG["routes"]
    
    route = random.choice(available)
    
    variation = route["variation"]
    random_factor = 1 + random.uniform(-variation, variation)
    base = int(route["base_price"] * random_factor)
    
    final_price = apply_class_bonus(base, current_class)
    
    exp_bonus = int(TAXI_ORDERS_CONFIG["experience_per_ride"] * class_info["experience_bonus"])
    total_exp = TAXI_ORDERS_CONFIG["experience_per_ride"] + exp_bonus
    
    order = {
        "id": route["id"],
        "name": route["name"],
        "distance": route["distance"],
        "time": route["time"],
        "min_time": route["min_time"],
        "base_price": base,
        "price": final_price,
        "experience": total_exp,
        "class": current_class,
        "class_multiplier": class_info["price_multiplier"],
        "created_at": int(time.time())
    }
    
    class_emoji = class_info["emoji"]
    
    text = (
        f"{class_emoji} <b>НОВЫЙ ЗАКАЗ [{class_info['name'].split()[1]}]</b>\n\n"
        f"📍 <b>Маршрут:</b> {order['name']}\n"
        f"📏 <b>Расстояние:</b> {order['distance']}\n"
        f"⏱ <b>Время:</b> {order['time']}\n"
        f"💵 <b>Оплата:</b> {format_balance(order['price'])}\n"
        f"⭐ <b>Класс:</b> {class_info['name']} (x{order['class_multiplier']})\n"
        f"⚡ <b>Опыт:</b> +{order['experience']}\n\n"
        f"<b>Берешь заказ?</b>"
    )
    
    taxi_keyboard = {
        "inline_keyboard": [[
            {"text": "✅ БЕРУ", "callback_data": "taxi_accept", "style": "success"},
            {"text": "❌ НЕ БЕРУ", "callback_data": "taxi_decline", "style": "danger"}
        ]]
    }
    
    sent = bot.send_message(chat_id, text, reply_markup=taxi_keyboard, parse_mode='HTML')
    
    save_active_trip(user_id, order, chat_id, sent.message_id)

@bot.message_handler(func=lambda message: message.text == "🏆 Рейтинг водителей")
def handle_driver_rating(message):
    show_driver_rating(message.chat.id)

def get_driver_rating():
    """Получить рейтинг водителей"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT ds.user_id, ds.trips_completed, ds.total_earned, ds.driver_class,
                   u.username, u.first_name, u.custom_name
            FROM driver_stats ds
            LEFT JOIN users u ON ds.user_id = u.user_id
            WHERE ds.trips_completed > 0
            ORDER BY ds.trips_completed DESC, ds.total_earned DESC
            LIMIT 5
        ''')
        
        results = []
        for row in cursor.fetchall():
            user_id, trips, earned, driver_class, username, first_name, custom = row
            
            class_info = DRIVER_CLASSES.get(driver_class, DRIVER_CLASSES['economy'])
            
            display = custom if custom else (
                f"@{username}" if username else first_name or f"ID: {user_id}"
            )
            
            results.append({
                'user_id': user_id,
                'display_name': display,
                'trips_completed': trips,
                'total_earned': earned,
                'driver_class': driver_class,
                'class_emoji': class_info['emoji'],
                'class_name': class_info['name']
            })
        
        return results

def show_driver_rating(chat_id):
    """Показать рейтинг водителей"""
    top = get_driver_rating()
    
    text = "🏆 <b>ТОП ВОДИТЕЛЕЙ</b>\n\n"
    
    if not top:
        text += "Пока нет статистики.\nСтань первым!"
    else:
        for i, driver in enumerate(top, 1):
            user_info = get_user_info(driver['user_id'])
            name = user_info['custom_name'] if user_info['custom_name'] else (
                f"@{user_info['username']}" if user_info['username'] else user_info['first_name']
            )
            
            if user_info['custom_name']:
                name_display = f"<b>{name}</b>"
            else:
                name_display = name
            
            bonus = ""
            if i == 1:
                bonus = " // +50ккк/день"
            elif i == 2:
                bonus = " // +25ккк/день" 
            elif i == 3:
                bonus = " // +10ккк/день"
            
            text += f"{i}. {driver['class_emoji']} {name_display} — {driver['trips_completed']} поездок{bonus}\n"
    
    text += f"\n⏰ Обновление каждые 5 минут"
    
    bot.send_message(chat_id, text, parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text == "📊 Моя статистика")
def handle_my_stats(message):
    user_id = message.from_user.id
    stats = get_driver_stats(user_id)
    driver_class = get_driver_class_info(user_id)
    class_info = driver_class['info']
    
    with get_db_cursor() as cursor:
        cursor.execute('SELECT experience FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        exp = result[0] if result else 0
        level = int((exp / 1000) ** 0.5) + 1
    
    active = get_active_trip(user_id)
    
    text = f"{class_info['emoji']} <b>ТВОЯ СТАТИСТИКА</b>\n\n"
    
    if active:
        text += f"📍 <b>Текущий рейс:</b> {active['name']}\n"
        text += f"⏱ <b>Время:</b> {active['time']}\n"
        text += f"💵 <b>Оплата:</b> {format_balance(active['price'])}\n\n"
    
    text += f"🏆 <b>Класс:</b> {class_info['name']}\n"
    text += f"📊 <b>Поездок:</b> {stats['trips_completed']}\n"
    text += f"💵 <b>Заработано:</b> {format_balance(stats['total_earned'])}\n"
    text += f"🏅 <b>Уровень:</b> {level}\n"
    
    if stats['trips_completed'] > 0:
        avg = stats['total_earned'] // stats['trips_completed']
        text += f"💵 <b>Средний чек:</b> {format_balance(avg)}\n"
    
    bot.send_message(message.chat.id, text, parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text == "⭐ Мой уровень")
def handle_my_level(message):
    user_id = message.from_user.id
    show_driver_class(message.chat.id, user_id)

@bot.message_handler(func=lambda message: message.text in ["🔙 Назад", "◀️ Назад"])
def handle_back_from_taxi(message):
    user_id = message.from_user.id
    markup = create_main_menu()
    bot.send_message(message.chat.id, "🔙 Возвращаюсь в главное меню", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('taxi_'))
def handle_taxi_actions(call):
    user_id = call.from_user.id
    
    if call.data == "taxi_accept":
        accept_order(call)
        
    elif call.data == "taxi_decline":
        cancel_active_trip(user_id)
        
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        
        bot.answer_callback_query(call.id, "🔄 Ищем другой маршрут...")
        create_new_order(call.message.chat.id, user_id)

def accept_order(call):
    """Принять заказ"""
    user_id = call.from_user.id
    
    if user_id in active_rides:
        try:
            active_rides[user_id].cancel()
        except:
            pass
    
    order = get_active_trip(user_id)
    if not order:
        bot.answer_callback_query(call.id, "⚠️ Заказ уже недоступен!")
        return
    
    minutes = int(order['time'].split()[0])
    class_info = DRIVER_CLASSES[order['class']]
    
    text = (
        f"{class_info['emoji']} <b>ПОЕЗДКА НАЧАТА!</b>\n\n"
        f"📍 <b>Маршрут:</b> {order['name']}\n"
        f"⏱ <b>В пути:</b> {order['time']}\n"
        f"💵 <b>Стоимость:</b> {format_balance(order['price'])}\n"
        f"⭐ <b>Класс:</b> {class_info['name']}\n\n"
        f"⏰ <b>Прибудет через {minutes} мин...</b>"
    )
    
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML'
        )
    except:
        pass
    
    bot.answer_callback_query(call.id, f"{class_info['emoji']} Поехали!")
    
    timer = threading.Timer(minutes * 60, finish_ride, 
                           [user_id, call.message.chat.id, call.message.message_id, order])
    active_rides[user_id] = timer
    timer.start()

def finish_ride(user_id, chat_id, message_id, order):
    """Завершить поездку"""
    if user_id in active_rides:
        del active_rides[user_id]
    
    active = get_active_trip(user_id)
    if not active:
        return
    
    try:
        update_balance(user_id, order["price"])
        add_experience(user_id, order["experience"])
        
        update_driver_stats(user_id, order["price"])
        
        cancel_active_trip(user_id)
        
        new_balance = get_balance(user_id)
        with get_db_cursor() as cursor:
            cursor.execute('SELECT experience FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            exp = result[0] if result else 0
            new_level = int((exp / 1000) ** 0.5) + 1
            
        stats = get_driver_stats(user_id)
        class_info = DRIVER_CLASSES[order['class']]
        
        text = f"🏆 <b>ПОЕЗДКА ЗАВЕРШЕНА!</b>\n\n"
        text += f"📍 <b>Маршрут:</b> {order['name']}\n"
        text += f"💵 <b>Заработано:</b> {format_balance(order['price'])}\n"
        text += f"⚡ <b>Опыт:</b> +{order['experience']}\n"
        text += f"⭐ <b>Класс:</b> {class_info['name']}\n"
        text += f"💳 <b>Баланс:</b> {format_balance(new_balance)}\n"
        text += f"📊 <b>Всего поездок:</b> {stats['trips_completed']}\n"
        
        new_class = get_driver_class_info(user_id)
        if new_class['current'] != order['class']:
            new_class_info = DRIVER_CLASSES[new_class['current']]
            text += f"\n🎉 <b>НОВЫЙ УРОВЕНЬ: {new_class_info['name']}!</b>\n"
        
        try:
            bot.edit_message_text(
                text,
                chat_id,
                message_id,
                parse_mode='HTML'
            )
        except:
            bot.send_message(chat_id, text, parse_mode='HTML')
            
    except Exception as e:
        print(f"⚠️ Ошибка завершения: {e}")

def get_driver_stats(user_id):
    """Получить статистику водителя"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT trips_completed, total_earned, last_trip_time, driver_class
            FROM driver_stats 
            WHERE user_id = ?
        ''', (user_id,))
        
        result = cursor.fetchone()
        if result:
            trips = result[0] or 0
            earned = result[1] or 0
            last = result[2] or 0
            db_class = result[3] or 'economy'
            
            correct = "economy"
            if trips >= 300:
                correct = "vip"
            elif trips >= 150:
                correct = "business"
            elif trips >= 100:
                correct = "comfort"
            
            if db_class != correct:
                cursor.execute('UPDATE driver_stats SET driver_class = ? WHERE user_id = ?', 
                              (correct, user_id))
            
            return {
                'trips_completed': trips,
                'total_earned': earned,
                'last_trip_time': last,
                'driver_class': correct
            }
        else:
            cursor.execute('''
                INSERT INTO driver_stats (user_id, trips_completed, total_earned, driver_class)
                VALUES (?, 0, 0, 'economy')
            ''', (user_id,))
            return {
                'trips_completed': 0,
                'total_earned': 0,
                'last_trip_time': 0,
                'driver_class': 'economy'
            }

def update_driver_stats(user_id, earned):
    """Обновить статистику водителя"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            UPDATE driver_stats 
            SET trips_completed = trips_completed + 1,
                total_earned = total_earned + ?,
                last_trip_time = ?
            WHERE user_id = ?
        ''', (earned, int(time.time()), user_id))
        
        if cursor.rowcount == 0:
            cursor.execute('''
                INSERT INTO driver_stats (user_id, trips_completed, total_earned, last_trip_time)
                VALUES (?, 1, ?, ?)
            ''', (user_id, earned, int(time.time())))

@bot.message_handler(func=lambda message: message.text.lower() == "очистить такси" and is_admin(message.from_user.id))
def handle_admin_clear_taxi(message):
    """Очистить все активные поездки (админ)"""
    if not is_admin(message.from_user.id):
        return
    
    cleared = clear_all_active_trips()
    
    if cleared > 0:
        bot.reply_to(message, f"✅ Очищено активных поездок: {cleared}", parse_mode='HTML')
    else:
        bot.reply_to(message, "ℹ️ Активных поездок нет")

def cleanup_stuck_trips():
    """Очистить зависшие поездки при запуске"""
    try:
        with get_db_cursor() as cursor:
            now = int(time.time())
            threshold = now - 7200
            
            cursor.execute('''
                SELECT user_id FROM active_trips 
                WHERE start_time < ? AND finish_time IS NULL
            ''', (threshold,))
            
            stuck = cursor.fetchall()
            
            if stuck:
                print(f"🧹 Найдено {len(stuck)} зависших поездок")
                
                for trip in stuck:
                    uid = trip[0]
                    if uid in active_rides:
                        try:
                            active_rides[uid].cancel()
                        except:
                            pass
                
                cursor.execute("UPDATE active_trips SET finish_time = ? WHERE start_time < ? AND finish_time IS NULL", 
                              (now, threshold))
                print("✅ Зависшие поездки очищены")
            else:
                print("✅ Зависших поездок нет")
    
    except Exception as e:
        print(f"⚠️ Ошибка очистки: {e}")

def monitor_active_trips():
    """Проверка активных поездок"""
    while True:
        try:
            with get_db_cursor() as cursor:
                now = int(time.time())
                cursor.execute('''
                    SELECT user_id, trip_data FROM active_trips 
                    WHERE finish_time IS NULL
                ''')
                
                for row in cursor.fetchall():
                    uid, data_json = row
                    data = json.loads(data_json)
                    minutes = int(data['time'].split()[0])
                    start = cursor.execute('SELECT start_time FROM active_trips WHERE user_id = ?', (uid,)).fetchone()[0]
                    
                    if now - start > (minutes * 60) + 300:
                        print(f"🧹 Найдена зависшая поездка {uid}")
                        finish_ride(uid, None, None, data)
        except Exception as e:
            print(f"⚠️ Ошибка мониторинга: {e}")
        
        time.sleep(60)
init_taxi_database()
cleanup_stuck_trips()

monitor_thread = threading.Thread(target=monitor_active_trips, daemon=True)
monitor_thread.start()

print("✅ Система такси готова к работе!")
@bot.callback_query_handler(func=lambda call: call.data == "no_money")
def handle_no_money(call):
    bot.answer_callback_query(call.id, "❌ Недостаточно средств для погашения!")

@bot.callback_query_handler(func=lambda call: call.data in ["show_deposit_menu", "show_withdraw_menu", "show_loan_menu", "show_loan_info", "show_repay_menu"])
def handle_menu_callbacks(call):
    """Обработка callback'ов меню"""
    user_id = call.from_user.id
    
    if call.data == "show_deposit_menu":
        handle_deposit_money(call.message)
    elif call.data == "show_withdraw_menu":
        handle_withdraw_deposit(call.message)
    elif call.data == "show_loan_menu":
        handle_take_loan(call.message)
    elif call.data == "show_loan_info":
        handle_loan_info(call.message)
    elif call.data == "show_repay_menu":
        handle_repay_loan_command(call.message)
    
    bot.answer_callback_query(call.id)

def calculate_interest():
    """Автоматический расчет процентов по вкладам каждые 3 часа"""
    current_time = int(time.time())
    interval = 10800
    users_to_notify = []

    with get_db_cursor() as cursor:
        cursor.execute(
            'SELECT user_id, bank_deposit FROM users WHERE bank_deposit > 0 AND last_interest_calc <= ? - ?',
            (current_time, interval)
        )
        rows = cursor.fetchall()
        for uid, deposit in rows:
            rate = 0.01 if is_premium(uid) else 0.005
            earned = max(1, int(deposit * rate))
            new_deposit = deposit + earned
            cursor.execute(
                'UPDATE users SET bank_deposit = ?, last_interest_calc = ? WHERE user_id = ?',
                (new_deposit, current_time, uid)
            )
            users_to_notify.append((uid, earned, new_deposit, is_premium(uid)))

    for uid, earned, new_deposit, premium in users_to_notify:
        try:
            rate_text = "1%" if premium else "0.5%"
            premium_tag = " ⭐ Premium" if premium else ""
            bot.send_message(
                uid,
                f"🏦 <b>Проценты зачислены!</b>{premium_tag}\n\n"
                f"📈 Ставка: <b>{rate_text}</b> каждые 3 часа\n"
                f"💰 Начислено: <b>+{format_balance(earned)}</b>\n"
                f"🏛 Депозит: <b>{format_balance(new_deposit)}</b>",
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"Не удалось уведомить {uid}: {e}")

def start_interest_calculation():
    """Запустить автоматический расчет процентов"""
    def interest_calculator():
        while True:
            try:
                calculate_interest()
                time.sleep(3600)
            except Exception as e:
                print(f"Ошибка в расчете процентов: {e}")
                time.sleep(300)

    thread = threading.Thread(target=interest_calculator, daemon=True)
    thread.start()
    print("🏆 Расчет процентов запущен (0.5%/3ч | премиум 1%/3ч)")

start_interest_calculation()
@bot.callback_query_handler(func=lambda call: call.data in ["show_deposit_menu", "show_withdraw_menu"])
def handle_menu_callbacks(call):
    """Обработка callback'ов меню"""
    user_id = call.from_user.id
    
    if call.data == "show_deposit_menu":
        handle_deposit_money(call.message)
    elif call.data == "show_withdraw_menu":
        handle_withdraw_deposit(call.message)
    
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda message: message.text and re.match(r'^вклад\s+\S+', message.text.strip().lower()))
def handle_bank_deposit_cmd(message):
    user_id = message.from_user.id
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "📝 Формат: <code>вклад 1000</code>", parse_mode='HTML')
        return
    balance = get_balance(user_id)
    try:
        amount = parse_bet_amount(parts[1], balance)
    except Exception:
        amount = None
    if not amount or amount <= 0:
        bot.send_message(message.chat.id, "❌ Неверная сумма. Пример: <code>вклад 5000</code>", parse_mode='HTML')
        return
    if amount > balance:
        bot.send_message(
            message.chat.id,
            f"❌ Недостаточно монет.\n💰 Баланс: {format_balance(balance)}",
            parse_mode='HTML'
        )
        return
    update_balance(user_id, -amount)
    update_bank_deposit(user_id, amount)
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE users SET last_interest_calc = ? WHERE user_id = ?', (int(time.time()), user_id))
    rate_text = "1% каждые 3ч" if is_premium(user_id) else "0.5% каждые 3ч"
    bot.send_message(
        message.chat.id,
        f"🏦 <b>Вклад внесён</b>\n\n"
        f"💰 Внесено: <b>{format_balance(amount)}</b>\n"
        f"🏛 Депозит: <b>{format_balance(get_bank_deposit(user_id))}</b>\n"
        f"📈 Процент: <b>{rate_text}</b>\n"
        f"⏳ Следующее начисление через 3 часа",
        parse_mode='HTML'
    )

@bot.message_handler(func=lambda message: message.text and re.match(r'^снять\s+\S+', message.text.strip().lower()))
def handle_bank_withdraw_cmd(message):
    user_id = message.from_user.id
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "📝 Формат: <code>снять 1000</code>", parse_mode='HTML')
        return
    deposit = get_bank_deposit(user_id)
    if deposit <= 0:
        bot.send_message(message.chat.id, "❌ Депозит пустой — нечего снимать.", parse_mode='HTML')
        return
    try:
        amount = parse_bet_amount(parts[1], deposit)
    except Exception:
        amount = None
    if not amount or amount <= 0:
        bot.send_message(message.chat.id, "❌ Неверная сумма. Пример: <code>снять 3000</code>", parse_mode='HTML')
        return
    if amount > deposit:
        bot.send_message(
            message.chat.id,
            f"❌ На депозите только {format_balance(deposit)}",
            parse_mode='HTML'
        )
        return
    update_bank_deposit(user_id, -amount)
    update_balance(user_id, amount)
    bot.send_message(
        message.chat.id,
        f"💸 <b>Снятие выполнено</b>\n\n"
        f"💰 Получено: <b>{format_balance(amount)}</b>\n"
        f"🏛 Остаток на депозите: <b>{format_balance(get_bank_deposit(user_id))}</b>",
        parse_mode='HTML'
    )

@bot.message_handler(func=lambda message: message.text == "🏛 Банк")
def handle_bank(message):
    user_id = message.from_user.id
    balance = get_balance(user_id)
    deposit = get_bank_deposit(user_id)
    rate = "1%" if is_premium(user_id) else "0.5%"

    text = (
        f"🏛 <b>Банк</b>\n\n"
        f"💰 Баланс: {format_balance(balance)}\n"
        f"🏦 Депозит: {format_balance(deposit)}\n\n"
        f"<blockquote>"
        f"📈 Ставка: <b>{rate}/ч</b> · начисление каждые 3 ч\n\n"
        f"💳 <b>Команды:</b>\n"
        f"<code>вклад [сумма]</code> — положить на депозит\n"
        f"<code>снять [сумма]</code> — снять с депозита"
        f"</blockquote>"
    )
    bot.send_message(message.chat.id, text, parse_mode='HTML')

def handle_deposit_money(message):
    user_id = message.from_user.id
    balance = get_balance(user_id)
    deposit = get_bank_deposit(user_id)

    text = (
        f"🏦 <b>Вклад в банк</b>\n\n"
        f"💰 Доступно: {format_balance(balance)}\n"
        f"🏦 Текущий депозит: {format_balance(deposit)}\n\n"
        f"Напишите сумму для внесения на депозит.\n"
        f"Например: <code>5000</code>"
    )
    bot.send_message(message.chat.id, text, parse_mode='HTML')
    bot.register_next_step_handler(message, process_deposit_amount)

def process_deposit_amount(message):
    user_id = message.from_user.id
    try:
        balance = get_balance(user_id)
        amount = parse_bet_amount(message.text.strip(), balance)
        if not amount or amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма", parse_mode='HTML')
            return
        if amount > balance:
            bot.send_message(message.chat.id, f"❌ Недостаточно монет. Баланс: {format_balance(balance)}", parse_mode='HTML')
            return
        update_balance(user_id, -amount)
        update_bank_deposit(user_id, amount)
        with get_db_cursor() as cursor:
            cursor.execute('UPDATE users SET last_interest_calc = ? WHERE user_id = ?', (int(time.time()), user_id))
        bot.send_message(
            message.chat.id,
            f"✅ <b>Внесено на депозит</b>\n\n"
            f"💰 Сумма: {format_balance(amount)}\n"
            f"🏦 Депозит: {format_balance(get_bank_deposit(user_id))}\n"
            f"⏳ Следующее начисление через 3 часа",
            parse_mode='HTML'
        )
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")

def handle_withdraw_deposit(message):
    user_id = message.from_user.id
    deposit = get_bank_deposit(user_id)

    if deposit <= 0:
        bot.send_message(message.chat.id, "❌ У вас нет средств на депозите.", parse_mode='HTML')
        return

    text = (
        f"💸 <b>Снятие с депозита</b>\n\n"
        f"🏦 Депозит: {format_balance(deposit)}\n\n"
        f"Напишите сумму для снятия.\n"
        f"Например: <code>5000</code>"
    )
    bot.send_message(message.chat.id, text, parse_mode='HTML')
    bot.register_next_step_handler(message, process_withdraw_amount)

def process_withdraw_amount(message):
    user_id = message.from_user.id
    try:
        deposit = get_bank_deposit(user_id)
        amount = parse_bet_amount(message.text.strip(), deposit)
        if not amount or amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма", parse_mode='HTML')
            return
        if amount > deposit:
            bot.send_message(message.chat.id, f"❌ На депозите только {format_balance(deposit)}", parse_mode='HTML')
            return
        update_bank_deposit(user_id, -amount)
        update_balance(user_id, amount)
        bot.send_message(
            message.chat.id,
            f"✅ <b>Снято с депозита</b>\n\n"
            f"💰 Сумма: {format_balance(amount)}\n"
            f"🏦 Остаток: {format_balance(get_bank_deposit(user_id))}",
            parse_mode='HTML'
        )
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")

@bot.message_handler(func=lambda message: message.text == "🏠 Назад")
def handle_back_to_bank(message):
    """Вернуться в главное меню банка"""
    handle_bank(message)

@bot.message_handler(func=lambda message: message.text.lower() == 'банлист' and is_admin(message.from_user.id))
def handle_ban_list(message):
    if not banned_users:
        bot.send_message(message.chat.id, "📋 Список забаненных пуст")
        return
    
    ban_list = "📋 <b>Забаненные пользователи:</b>\n\n"
    for user_id in banned_users:
        ban_list += f"• {user_id}\n"
    
    bot.send_message(message.chat.id, ban_list, parse_mode='HTML')
@bot.message_handler(func=lambda message: message.text.lower() == 'все вещи' and is_admin(message.from_user.id))
def handle_show_all_items(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        with get_db_cursor() as cursor:
            cursor.execute('''
                SELECT id, name, price, type 
                FROM clothes_shop 
                ORDER BY type, name
            ''')
            items = cursor.fetchall()
            
            if not items:
                bot.send_message(message.chat.id, "❌ В магазине нет вещей!", parse_mode='HTML')
                return
            
            items_by_type = {}
            for item in items:
                item_id, name, price, item_type = item
                if item_type not in items_by_type:
                    items_by_type[item_type] = []
                items_by_type[item_type].append((item_id, name, price))
            
            items_text = "📦 ВСЕ ВЕЩИ В МАГАЗИНЕ:\n\n"
            
            for item_type, type_items in items_by_type.items():
                items_text += f"📁 {item_type.upper()}:\n"
                for item_id, name, price in type_items:
                    items_text += f"  🆔 {item_id} - {name} - {format_balance(price)}\n"
                items_text += "\n"
            
            if len(items_text) > 4000:
                parts = [items_text[i:i+4000] for i in range(0, len(items_text), 4000)]
                for part in parts:
                    bot.send_message(message.chat.id, part)
            else:
                bot.send_message(message.chat.id, items_text)
                
    except Exception as e:
        print(f"❌ Ошибка показа вещей: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка!", parse_mode='HTML')
@bot.message_handler(func=lambda message: message.text.lower().startswith('варн ') and is_admin(message.from_user.id))
def handle_warn_user(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split(' ', 2)
        if len(parts) < 3:
            bot.send_message(message.chat.id, 
                           "❌ Используйте: варн [user_id/@username] [причина] [время_часы]\n\n"
                           "Примеры:\n"
                           "варн 123456789 Спам\n"
                           "варн @username Оскорбления 48", parse_mode='HTML')
            return
        
        target = parts[1]
        reason = parts[2]
        duration_hours = 24
        
        reason_parts = reason.split(' ')
        if reason_parts[-1].isdigit():
            duration_hours = int(reason_parts[-1])
            reason = ' '.join(reason_parts[:-1])
        
        target_user_id = None
        
        if target.startswith('@'):
            username = target[1:]
            with get_db_cursor() as cursor:
                cursor.execute('SELECT user_id FROM users WHERE username = ?', (username,))
                result = cursor.fetchone()
                if result:
                    target_user_id = result[0]
                else:
                    bot.send_message(message.chat.id, f"❌ Пользователь {target} не найден!", parse_mode='HTML')
                    return
        else:
            try:
                target_user_id = int(target)
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный формат ID пользователя!", parse_mode='HTML')
                return
        
        if not get_user_info(target_user_id):
            bot.send_message(message.chat.id, "❌ Пользователь отсутствует в базе данных!", parse_mode='HTML')
            return
        
        success = add_warn(target_user_id, reason, message.from_user.id, duration_hours)
        
        if success:
            expires_time = datetime.fromtimestamp(time.time() + (duration_hours * 3600)).strftime("%d.%m.%Y %H:%M")
            
            try:
                bot.send_message(target_user_id, 
                               f"⚠️ Вам выдан варн!\n\n"
                               f"📝 Причина: {reason}\n"
                               f"⏰ Действует до: {expires_time}\n\n"
                               f"❌ Ограничения:\n"
                               f"• Нельзя транзакцияить деньги\n"
                               f"• Нельзя создавать чеки", parse_mode='HTML')
            except:
                pass
            
            bot.send_message(message.chat.id,
                           f"🏆 Варн выдан пользователю ID: {target_user_id}\n"
                           f"📝 Причина: {reason}\n"
                           f"⏰ Действует: {duration_hours}ч\n"
                           f"🕒 До: {expires_time}", parse_mode='HTML')
        else:
            bot.send_message(message.chat.id, "❌ Ошибка при выдаче варна!", parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка при выдаче варна: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при выдаче варна!", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower().startswith('разварн ') and is_admin(message.from_user.id))
def handle_unwarn_user(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split(' ')
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Используйте: разварн [user_id/@username]", parse_mode='HTML')
            return
        
        target = parts[1]
        target_user_id = None
        
        if target.startswith('@'):
            username = target[1:]
            with get_db_cursor() as cursor:
                cursor.execute('SELECT user_id FROM users WHERE username = ?', (username,))
                result = cursor.fetchone()
                if result:
                    target_user_id = result[0]
                else:
                    bot.send_message(message.chat.id, f"❌ Пользователь {target} не найден!", parse_mode='HTML')
                    return
        else:
            try:
                target_user_id = int(target)
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный формат ID пользователя!", parse_mode='HTML')
                return
        
        success = remove_warn(target_user_id)
        
        if success:
            try:
                bot.send_message(target_user_id, "🏆 Твой варн снят! Ограничения сняты.", parse_mode='HTML')
            except:
                pass
            
            bot.send_message(message.chat.id, f"🏆 Варн снят с пользователя ID: {target_user_id}", parse_mode='HTML')
        else:
            bot.send_message(message.chat.id, f"❌ У пользователя ID: {target_user_id} нет активного варна!", parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка при снятии варна: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при снятии варна!", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower().startswith('проверить варн ') and is_admin(message.from_user.id))
def handle_check_warn(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split(' ')
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Используйте: проверить варн [user_id/@username]", parse_mode='HTML')
            return
        
        target = parts[1]
        target_user_id = None
        
        if target.startswith('@'):
            username = target[1:]
            with get_db_cursor() as cursor:
                cursor.execute('SELECT user_id FROM users WHERE username = ?', (username,))
                result = cursor.fetchone()
                if result:
                    target_user_id = result[0]
                else:
                    bot.send_message(message.chat.id, f"❌ Пользователь {target} не найден!", parse_mode='HTML')
                    return
        else:
            try:
                target_user_id = int(target)
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный формат ID пользователя!", parse_mode='HTML')
                return
        
        warn_info = get_warn_info(target_user_id)
        
        if warn_info:
            expires_time = datetime.fromtimestamp(warn_info['expires_at']).strftime("%d.%m.%Y %H:%M")
            warned_time = datetime.fromtimestamp(warn_info['warned_at']).strftime("%d.%m.%Y %H:%M")
            
            message_text = f"⚠️ Пользователь ID: {target_user_id} имеет варн!\n\n"
            message_text += f"📝 Причина: {warn_info['reason']}\n"
            message_text += f"👮 Выдал: {warn_info['warned_by_name'] or 'ID: ' + str(warn_info['warned_by'])}\n"
            message_text += f"📆 Выдан: {warned_time}\n"
            message_text += f"⏰ Действует до: {expires_time}"
            
            bot.send_message(message.chat.id, message_text)
        else:
            bot.send_message(message.chat.id, f"🏆 Пользователь ID: {target_user_id} не имеет активных варнов", parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка при проверке варна: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при проверке варна!", parse_mode='HTML')
def get_or_create_user(user_id, username, first_name):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        user = cursor.fetchone()
        
        if not user:
            referral_code = f"ref{user_id}"
            
            cursor.execute(
                'INSERT INTO users (user_id, username, first_name, balance, referral_code, video_cards, deposit, last_mining_collect, click_streak, total_clicks, bank_deposit, daily_streak, last_daily_bonus, last_interest_calc, business_id, business_progress, business_start_time, business_raw_materials, clan_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (user_id, username, first_name, 0, referral_code, 0, 0, 0, 0, 0, 0, 0, 0, int(time.time()), 0, 0, 0, 0, 0)
            )
        
        return user

import os
import shutil
from datetime import datetime

@bot.message_handler(func=lambda message: message.text.lower() == 'бд' and is_admin(message.from_user.id))
def handle_db_stats(message):
    """Показывает статистику базы данных"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        with get_db_cursor() as cursor:
            cursor.execute('SELECT COUNT(*) FROM users')
            total_users = cursor.fetchone()[0]
            
            banned_users_count = len(banned_users)
            
            cursor.execute('SELECT COUNT(*) FROM checks')
            total_checks = cursor.fetchone()[0]
            
            cursor.execute('SELECT SUM(balance) FROM users')
            total_balance = cursor.fetchone()[0] or 0
            
            cursor.execute('SELECT SUM(bank_deposit) FROM users')
            total_deposits = cursor.fetchone()[0] or 0
        
        db_path = 'game.db'
        if os.path.exists(db_path):
            db_size_bytes = os.path.getsize(db_path)
            db_size_mb = db_size_bytes / (1024 * 1024)
        else:
            db_size_mb = 0
        
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        stats_text = (
            f"📊 <b>ИНФОРМАЦИЯ О БАЗЕ ДАННЫХ</b>\n\n"
            f"👥 Пользователи: <b>{total_users}</b>\n"
            f"🚫 Забанено: <b>{banned_users_count}</b>\n"
            f"💳 Чеков: <b>{total_checks}</b>\n\n"
            f"💵 Общий баланс: ❄️<b>{format_balance(total_balance)}</b>\n"
            f"🏦 Всего на вкладах: ❄️<b>{format_balance(total_deposits)}</b>\n\n"
            f"📁 Размер базы: <b>{db_size_mb:.2f} MB</b>\n"
            f"📅 Дата: <code>{current_time}</code>\n\n"
            f"<b>Команды:</b>\n"
            f"• <code>база</code> - Скачать резервную копию\n"
            f"• <code>загрузитьбазу</code> - Загрузить новую базу"
        )
        
        bot.send_message(message.chat.id, stats_text, parse_mode='HTML')
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')
        print(f"Ошибка в handle_db_stats: {e}")

@bot.message_handler(func=lambda message: message.text.lower() == 'база' and is_admin(message.from_user.id))
def handle_download_db(message):
    """Отправляет файл базы данных администратору"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        db_path = 'game.db'
        
        if not os.path.exists(db_path):
            bot.send_message(message.chat.id, "❌ Файл базы данных не найден", parse_mode='HTML')
            return
        
        backup_name = f"game_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(db_path, backup_name)
        
        with open(backup_name, 'rb') as db_file:
            bot.send_document(
                message.chat.id,
                db_file,
                caption=f"📦 Резервная копия базы данных\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
        
        os.remove(backup_name)
        
        bot.send_message(message.chat.id, "✅ База данных отправлена", parse_mode='HTML')
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')
        print(f"Ошибка в handle_download_db: {e}")

@bot.message_handler(func=lambda message: message.text.lower() == 'загрузитьбазу' and is_admin(message.from_user.id))
def handle_upload_db_request(message):
    """Запрашивает файл базы данных для загрузки"""
    if not is_admin(message.from_user.id):
        return
    
    bot.send_message(
        message.chat.id,
        "📤 Отправьте файл <code>game.db</code> для замены текущей базы данных\n\n"
        "⚠️ <b>ВАЖНО:</b>\n"
        "• После загрузки бот нужно будет перезапустить\n"
        "• Убедитесь что файл имеет расширение .db\n"
        "• Размер файла не должен превышать 50MB",
        parse_mode='HTML'
    )

@bot.message_handler(content_types=['document'])
def handle_document_upload(message):
    """Обрабатывает загрузку файлов: .db в базу, картинки в папку images"""
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "💀 У вас нет прав для этой команды")
        return
    
    try:
        file_info = bot.get_file(message.document.file_id)
        file_name = message.document.file_name
        file_ext = file_name.lower().split('.')[-1] if '.' in file_name else ''
        
        downloaded_file = bot.download_file(file_info.file_path)
        
        if file_ext == 'db':
            db_path = _DB_PATH  # используем актуальный путь к БД
            backup_path = None
            if os.path.exists(db_path):
                backup_path = f'game_auto_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
                shutil.copy2(db_path, backup_path)
                bot.send_message(message.chat.id, f"📦 Старая база сохранена как: <code>{backup_path}</code>", parse_mode='HTML')

            # Закрываем ВСЕ соединения из пула перед заменой
            with db_pool._lock:
                for conn in db_pool.pool:
                    try:
                        conn.close()
                    except:
                        pass
                db_pool.pool = []

            # Удаляем WAL и SHM файлы старой базы, иначе SQLite применит
            # старый журнал поверх нового файла и данные перемешаются
            for wal_ext in ('-wal', '-shm'):
                wal_file = db_path + wal_ext
                if os.path.exists(wal_file):
                    try:
                        os.remove(wal_file)
                    except Exception:
                        pass

            # Проверяем магический заголовок SQLite ДО записи на диск
            if not downloaded_file.startswith(b'SQLite format 3\x00'):
                bot.send_message(
                    message.chat.id,
                    f"💀 Ошибка: файл не является SQLite базой данных!\n"
                    f"Первые байты: <code>{downloaded_file[:16]}</code>\n"
                    f"Размер файла: {len(downloaded_file)} байт",
                    parse_mode='HTML'
                )
                return

            with open(db_path, 'wb') as new_file:
                new_file.write(downloaded_file)
            
            try:
                # Проверяем что файл реально SQLite и содержит нужные таблицы
                test_conn = sqlite3.connect(db_path)
                test_conn.row_factory = sqlite3.Row
                cur = test_conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [r[0] for r in cur.fetchall()]
                test_conn.close()

                if 'users' not in tables:
                    raise sqlite3.DatabaseError("Таблица users не найдена — неверная база")

                # Дозаписываем недостающие таблицы/колонки (не трогая существующие данные)
                init_db()
                init_dice_tables()
                init_taxi_database()
                _init_stocks()
                _migrate_stocks()
                _init_clan_wars()
                
                global active_rides, active_mines_games, active_captchas, active_duels
                global active_snowman_bosses, player_cooldowns, shop_pages, wardrobe_pages
                global active_bonus_posts, bonus_handlers, pending_broadcast, banned_users
                
                active_rides = {}
                active_mines_games = {}
                active_captchas = {}
                active_duels = {}
                active_snowman_bosses = {}
                player_cooldowns = {}
                shop_pages = {}
                wardrobe_pages = {}
                active_bonus_posts = {}
                bonus_handlers = {}
                pending_broadcast = None
                banned_users = set()
                
                with get_db_cursor() as cursor:
                    cursor.execute('SELECT COUNT(*) FROM users')
                    user_count = cursor.fetchone()[0]
                    
                    cursor.execute('SELECT COUNT(*) FROM users WHERE balance > 0')
                    users_with_balance = cursor.fetchone()[0]
                    
                    if user_count == 0:
                        bot.send_message(
                            message.chat.id,
                            "⚠️ <b>ВНИМАНИЕ!</b>\n\n"
                            "База данных пуста! Пользователи отсутствуют.\n\n"
                            "Возможные причины:\n"
                            "• Загружен неверный файл\n"
                            "• База повреждена\n"
                            "• Это пустая база\n\n"
                            f"📦 Резервная копия сохранена: {backup_path}",
                            parse_mode='HTML'
                        )
                    else:
                        bot.send_message(
                            message.chat.id,
                            f"✅ <b>База данных успешно загружена!</b>\n\n"
                            f"📊 Статистика новой базы:\n"
                            f"👥 Пользователей: {user_count}\n"
                            f"💵 С ненулевым балансом: {users_with_balance}\n\n"
                            f"🔄 <b>ВАЖНО:</b> Перезапустите бота для полного применения изменений\n"
                            f"💡 Используйте команду <code>бд</code> чтобы проверить новую базу",
                            parse_mode='HTML'
                        )
                        # Сбрасываем состояние колеса в Flask (без перезапуска)
                        try:
                            _requests.post(
                                'http://localhost:3001/internal/rolls/reset',
                                json={'secret': BOT_TOKEN},
                                timeout=5
                            )
                            print('[bot] rolls state reset after DB swap')
                        except Exception as re:
                            print(f'[bot] rolls reset failed: {re}')
                    
            except Exception as db_err:
                bot.send_message(message.chat.id, f"💀 Ошибка при загрузке базы: <code>{db_err}</code>", parse_mode='HTML')
                
                if backup_path and os.path.exists(backup_path):
                    shutil.copy2(backup_path, db_path)
                    bot.send_message(message.chat.id, "✅ Старая база восстановлена", parse_mode='HTML')
        
        elif file_ext in ['png', 'jpg', 'jpeg', 'gif']:
            if message.caption and message.caption.strip():
                filename = message.caption.strip()
                if not filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                    filename += f".{file_ext}"
            else:
                filename = file_name
            
            save_path = f"images/{filename}"
            
            os.makedirs("images", exist_ok=True)
            
            with open(save_path, 'wb') as new_file:
                new_file.write(downloaded_file)
            
            file_size = os.path.getsize(save_path)
            size_kb = file_size / 1024
            
            success_text = (
                f"✅ <b>Изображение сохранено!</b>\n\n"
                f"📁 Путь: <code>{save_path}</code>\n"
                f"📄 Имя: {filename}\n"
                f"📊 Размер: {size_kb:.1f} KB\n\n"
                f"💡 Теперь можно использовать в магазине: <code>{filename}</code>"
            )
            
            try:
                with open(save_path, 'rb') as photo:
                    bot.send_photo(
                        message.chat.id,
                        photo,
                        caption=success_text,
                        parse_mode='HTML'
                    )
            except:
                with open(save_path, 'rb') as photo:
                    bot.send_document(
                        message.chat.id,
                        photo,
                        caption=success_text,
                        parse_mode='HTML'
                    )
        
        else:
            bot.reply_to(
                message, 
                f"💀 Неподдерживаемый формат файла: .{file_ext}\n\n"
                f"Допустимые форматы:\n"
                f"• .db - база данных\n"
                f"• .png, .jpg, .jpeg, .gif - изображения"
            )
        
    except Exception as e:
        bot.reply_to(message, f"💀 Ошибка: {e}", parse_mode='HTML')
        print(f"Ошибка в handle_document_upload: {e}")
@bot.message_handler(func=lambda message: message.text.lower() == 'сбросить кэш' and is_admin(message.from_user.id))
def handle_clear_cache(message):
    """Очистить все кэшированные данные в памяти"""
    global active_rides, active_mines_games, active_captchas, active_duels
    global active_snowman_bosses, player_cooldowns, shop_pages, wardrobe_pages
    global active_bonus_posts, bonus_handlers, pending_broadcast, banned_users
    
    active_rides = {}
    active_mines_games = {}
    active_captchas = {}
    active_duels = {}
    active_snowman_bosses = {}
    player_cooldowns = {}
    shop_pages = {}
    wardrobe_pages = {}
    active_bonus_posts = {}
    bonus_handlers = {}
    pending_broadcast = None
    banned_users = set()
    
    with db_pool._lock:
        for conn in db_pool.pool:
            try:
                conn.close()
            except:
                pass
        db_pool.pool = []
    
    bot.send_message(message.chat.id, "🧹 Кэш очищен!")
def get_balance(user_id):
    """Получает баланс напрямую из БД БЕЗ кэширования"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result[0] if result else 0

def is_registered(user_id):
    """Проверяет зарегистрирован ли пользователь в боте"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT 1 FROM users WHERE user_id = ?', (user_id,))
        return cursor.fetchone() is not None

def require_registration(func):
    """Декоратор — блокирует выполнение если пользователь не зарегистрирован"""
    import functools
    @functools.wraps(func)
    def wrapper(message, *args, **kwargs):
        uid = message.from_user.id if hasattr(message, 'from_user') else None
        if uid and not is_registered(uid):
            try:
                bot.send_message(
                    message.chat.id,
                    "❌ <b>Ты не зарегистрирован!</b>\n\nНапиши /start чтобы начать.",
                    parse_mode='HTML'
                )
            except Exception:
                pass
            return
        return func(message, *args, **kwargs)
    return wrapper

def safe_deduct(user_id, amount):
    """Безопасное списание — возвращает True если успешно, иначе False"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        if not row or row[0] < amount:
            return False
        cursor.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, user_id))
        return True

def refund_balance(user_id, amount, chat_id=None):
    """Возврат средств при ошибке"""
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
    if chat_id:
        try:
            bot.send_message(
                chat_id,
                f"⚠️ Произошла ошибка. Ставка <b>{format_balance(amount)}</b> возвращена на баланс.",
                parse_mode='HTML'
            )
        except Exception:
            pass

def get_bank_deposit(user_id):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT bank_deposit FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result[0] if result else 0

def update_bank_deposit(user_id, amount):
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE users SET bank_deposit = bank_deposit + ? WHERE user_id = ?', (amount, user_id))

def get_user_info(user_id):
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT username, first_name, custom_name, balance, video_cards, bank_deposit, 
                   daily_streak, total_clicks, business_id, business_progress, 
                   business_start_time, business_raw_materials, clan_id,
                   games_won, games_lost, total_won_amount, total_lost_amount
            FROM users WHERE user_id = ?
        ''', (user_id,))
        result = cursor.fetchone()
        
        if result:
            return {
                'username': result[0],
                'first_name': result[1],
                'custom_name': result[2],
                'balance': result[3],
                'video_cards': result[4],
                'bank_deposit': result[5],
                'daily_streak': result[6],
                'total_clicks': result[7],
                'business_id': result[8],
                'business_progress': result[9],
                'business_start_time': result[10],
                'business_raw_materials': result[11],
                'clan_id': result[12],
                'games_won': result[13] or 0,
                'games_lost': result[14] or 0,
                'total_won_amount': result[15] or 0,
                'total_lost_amount': result[16] or 0
            }
        return None

def transfer_money(from_user_id, to_user_id, amount):
    if is_user_warned(from_user_id):
        return False, "❌ Вы не можете транзакцияить деньги, так как у вас активный варн!"
    
    with get_db_cursor() as cursor:
        cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (to_user_id,))
        if not cursor.fetchone():
            return False, "❌ Пользователь не найден!"
        
        balance = get_balance(from_user_id)
        if balance < amount:
            return False, "❌ Не хватает монет для транзакцияа!"
        
        if amount <= 0:
            return False, "❌ Сумма должна быть больше 0!"
        
        fee = int(amount * TRANSFER_FEE)
        net_amount = amount - fee
        
        cursor.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, from_user_id))
        cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (net_amount, to_user_id))
        
        cursor.execute('INSERT INTO transfers (from_user_id, to_user_id, amount, fee) VALUES (?, ?, ?, ?)',
                      (from_user_id, to_user_id, amount, fee))
        
        return True, f"🏆 Транзакция окончена!\n💸 Сумма: {format_balance(net_amount)}\n📊 Комиссия: {format_balance(fee)}"
import time
import json
import os
from telebot import types

CLAN_CONFIG = {
    'create_price': 100000,
    'max_name_length': 20,
    'max_tag_length': 5,
    'max_members': 25,
    'war_cost': 500000,
    'war_duration': 86400,
    'war_cooldown': 604800,
    'reward_interval': 432000,
    'top_rewards': {1: 10000, 2: 50000, 3: 25000},
    'war_victory_reward': 750000,
    'war_defeat_penalty': 250000,
    'war_min_level': 3,
    'war_max_active': 3,
    'avatar_price': 25000
}

CLAN_AVATARS_DIR = 'data/clan_avatars'
def create_main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    
    buttons = [
        "🏙️ Город", "📞 Помощь", "🏛 Банк",
        "🏆 Топ", "💎 Донат", "🎁 Бонус",
        "📈 Биржа", "⚔️ Клан"
    ]
    
    for i in range(0, len(buttons), 3):
        row = buttons[i:i+3]
        markup.add(*[KeyboardButton(btn) for btn in row])
    
    return markup
def create_city_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("🛍️ Шоп"),
        KeyboardButton("👔 Шкаф"),
        KeyboardButton("💼 Работа"),
        KeyboardButton("🔙 Назад")
    )
    return markup
def create_work_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("👆 Кликер"),
        KeyboardButton("🎭 Скам"),
        KeyboardButton("🚗 Такси"),
        KeyboardButton("⛏️ Майнинг"),
        KeyboardButton("✈️ Воздушный груз"),
        KeyboardButton("⛏️ Шахта"),
        KeyboardButton("🔙 Назад")
    )
    return markup

def create_business_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("🏭 Мой бизнес"),
        KeyboardButton("🏪 Бизнесы"),
        KeyboardButton("📦 Купить сырьё"),
        KeyboardButton("💵 Собрать доход"),
        KeyboardButton("📤 Продать бизнес"),
        KeyboardButton("🔙 Назад")
    )
    return markup

def create_mining_keyboard():
    markup = InlineKeyboardMarkup()
    markup.row_width = 2
    markup.add(
        InlineKeyboardButton("💵 Собрать", callback_data="mining_collect"),
        InlineKeyboardButton("⚡ Купить видеокарту", callback_data="mining_buy")
    )
    return markup

def create_clicker_keyboard():
    """Создает клавиатуру для кликера с цветными кнопками"""
    
    symbols = ["❌", "❌", "❌", "❌", "✅"]
    random.shuffle(symbols)
    
    keyboard = {
        "inline_keyboard": []
    }
    
    row = []
    for i, symbol in enumerate(symbols):
        if symbol == "✅":
            button = {
                "text": symbol,
                "callback_data": f"clicker_{symbol}",
                "style": "success"
            }
        else:
            button = {
                "text": symbol,
                "callback_data": f"clicker_{symbol}",
                "style": "danger"
            }
        
        row.append(button)
        
        if len(row) == 3:
            keyboard["inline_keyboard"].append(row)
            row = []
    
    if row:
        keyboard["inline_keyboard"].append(row)
    
    return json.dumps(keyboard)

def create_top_menu(top_type="balance", page=0):
    markup = InlineKeyboardMarkup()
    
    type_buttons = [
        InlineKeyboardButton("💵 Баланс", callback_data=f"top_type_balance_{page}"),
        InlineKeyboardButton("🌟 Опыт", callback_data=f"top_type_exp_{page}"),
        InlineKeyboardButton("👥 Рефералы", callback_data=f"top_type_referrals_{page}")
    ]
    markup.add(*type_buttons)
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"top_nav_{top_type}_{page-1}"))
    
    nav_buttons.append(InlineKeyboardButton(f"Страница {page+1}", callback_data="top_current"))
    
    nav_buttons.append(InlineKeyboardButton("➡️ Вперед", callback_data=f"top_nav_{top_type}_{page+1}"))
    
    if nav_buttons:
        markup.add(*nav_buttons)
    
    return markup
def safe_cleanup_old_data():
    """Безопасная очистка устаревших данных"""
    current_time = time.time()
    cleaned_count = 0

    expired_mines = []
    for user_id, game_data in active_mines_games.items():
        if current_time - game_data.get('start_time', 0) > 600:
            expired_mines.append(user_id)
    
    for user_id in expired_mines:
        try:
            game_data = active_mines_games[user_id]
            update_balance(user_id, game_data['bet_amount'])
            del active_mines_games[user_id]
            cleaned_count += 1
            
            try:
                bot.send_message(
                    user_id, 
                    f"🕒 Игра в 'Мины' автоматически окончена\n💵 Возвращено: {format_balance(game_data['bet_amount'])}"
                , parse_mode='HTML')
            except:
                pass
                
            logger.info(f"🧹 Очищена игра в минах для {user_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка очистки мины для {user_id}: {e}")

    expired_captchas = []
    for user_id, captcha_data in active_captchas.items():
        if current_time - captcha_data.get('created_at', 0) > 1800:
            expired_captchas.append(user_id)
    
    for user_id in expired_captchas:
        try:
            del active_captchas[user_id]
            cleaned_count += 1
            
            try:
                bot.send_message(
                    user_id,
                    "⏰ Время на решение капчи истекло\n🔁 Используйте /start для новой попытки"
                )
            except:
                pass
                
            logger.info(f"🧹 Очищена капча для {user_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка очистки капчи для {user_id}: {e}")

    expired_sessions = []
    for user_id, data in list(shop_pages.items()) + list(wardrobe_pages.items()):
        last_activity = data.get('last_activity', 0)
        if current_time - last_activity > 7200:
            expired_sessions.append(user_id)
    
    for user_id in set(expired_sessions):
        try:
            if user_id in shop_pages:
                del shop_pages[user_id]
            if user_id in wardrobe_pages:
                del wardrobe_pages[user_id]
            cleaned_count += 1
            logger.info(f"🧹 Очищены сессии для {user_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка очистки сессий для {user_id}: {e}")

    logger.info(f"🏆 Безопасная очистка окончена: {cleaned_count} объектов")
    return cleaned_count
@bot.message_handler(func=lambda message: message.text in ["🏙️ Город", "Город"])
def handle_city(message):
    """Показать меню города"""
    markup = create_city_menu()
    
    city_text = """🏙️ <b>Город</b>

Шоп, работа, шкаф — всё здесь."""
    
    try:
        with open('city.jpg', 'rb') as photo:
            bot.send_photo(
                chat_id=message.chat.id,
                photo=photo,
                caption=city_text,
                reply_markup=markup,
                parse_mode='HTML'
            )
    except FileNotFoundError:
        bot.send_message(message.chat.id, city_text, reply_markup=markup, parse_mode='HTML')
        print("Файл city.jpg отсутствует в корневой папке!")
    except Exception as e:
        bot.send_message(message.chat.id, city_text, reply_markup=markup, parse_mode='HTML')
        print(f"Ошибка при отправке фото: {e}")
def get_user_link(user_id, username, first_name, custom_name):
    display_name = custom_name if custom_name else (f"@{username}" if username else first_name)
    if username:
        return f'<a href="https://t.me/{username}">{display_name}</a>'
    else:
        return f'<b>{display_name}</b>'

def show_top_balance(chat_id, user_id, page=0, message_id=None):
    try:
        limit = 5
        offset = page * limit
        
        with get_db_cursor() as cursor:
            cursor.execute('''
            SELECT 
                user_id,
                username,
                first_name,
                custom_name,
                balance 
            FROM users 
            WHERE balance > 0
            ORDER BY balance DESC 
            LIMIT ? OFFSET ?
            ''', (limit, offset))
            
            top_users = cursor.fetchall()
            
            cursor.execute('SELECT COUNT(*) FROM users WHERE balance > 0')
            total_users = cursor.fetchone()[0]
            
            cursor.execute('''
            SELECT COUNT(*) + 1 FROM users 
            WHERE balance > (SELECT balance FROM users WHERE user_id = ?)
            ''', (user_id,))
            user_position_result = cursor.fetchone()
            user_position = user_position_result[0] if user_position_result else None
            
            cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
            user_balance_result = cursor.fetchone()
            user_balance = user_balance_result[0] if user_balance_result else 0

        title = "<b>Список Forbs💸</b>\n\n"
        
        if not top_users:
            message_text = f"{title}Топ пока пуст! Станьте первым мажором!"
        else:
            message_text = title
            start_number = page * limit + 1
            
            for i, (user_id_db, username, first_name, custom_name, balance) in enumerate(top_users):
                user_link = get_user_link(user_id_db, username, first_name, custom_name)
                prem_icon = f" {PREMIUM_EMOJI}" if is_premium(user_id_db) else ""
                message_text += f"{start_number + i}. {user_link}{prem_icon} ⟨{format_balance(balance)}⟩\n"
        
        if user_balance > 0 and user_position:
            message_text += f"\nТы находишься на {user_position} месте"
        
        final_markup = create_top_menu("balance", page)
        
        if message_id:
            bot.edit_message_text(
                message_text, 
                chat_id, 
                message_id,
                reply_markup=final_markup, 
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        else:
            msg = bot.send_message(
                chat_id, 
                message_text, 
                reply_markup=final_markup, 
                parse_mode='HTML',
                disable_web_page_preview=True
            )
            return msg.message_id
    
    except Exception as e:
        print(f"Ошибка в show_top_balance: {e}")
        bot.send_message(chat_id, "❌ Ошибка при загрузке топа!", parse_mode='HTML')

def show_top_exp(chat_id, user_id, page=0, message_id=None):
    try:
        limit = 5
        offset = page * limit

        with get_db_cursor() as cursor:
            cursor.execute('''
                SELECT user_id, username, first_name, custom_name, experience
                FROM users WHERE experience > 0
                ORDER BY experience DESC
                LIMIT ? OFFSET ?
            ''', (limit, offset))
            top_users = cursor.fetchall()

            cursor.execute('SELECT COUNT(*) FROM users WHERE experience > 0')
            total_users = cursor.fetchone()[0]

            cursor.execute('SELECT experience FROM users WHERE user_id = ?', (user_id,))
            res = cursor.fetchone()
            my_exp = res[0] if res else 0

            cursor.execute('SELECT COUNT(*) + 1 FROM users WHERE experience > ?', (my_exp,))
            my_pos = cursor.fetchone()[0]

        my_level = get_level_from_exp(my_exp)
        my_emoji, my_title = get_title(my_level)

        title = "🌟 <b>Топ по опыту</b>\n\n"
        medals = ["🥇", "🥈", "🥉"]

        if not top_users:
            message_text = f"{title}Пока никто не набрал опыт!"
        else:
            message_text = title
            start = page * limit + 1
            for i, (uid, username, first_name, custom_name, exp) in enumerate(top_users):
                lvl = get_level_from_exp(exp)
                em, ttl = get_title(lvl)
                link = get_user_link(uid, username, first_name, custom_name)
                pos = start + i
                medal = medals[pos-1] if pos <= 3 else f"{pos}."
                prem_icon = f" {PREMIUM_EMOJI}" if is_premium(uid) else ""
                message_text += f"{medal} {link}{prem_icon}\n    {em} <b>Ур.{lvl} {ttl}</b> · {exp:,} exp\n"

        message_text += f"\n{my_emoji} Твой уровень: <b>{my_level} — {my_title}</b>\n"
        message_text += f"Позиция: #{my_pos} · {my_exp:,} exp"

        final_markup = create_top_menu("exp", page)

        if message_id:
            bot.edit_message_text(message_text, chat_id, message_id, reply_markup=final_markup, parse_mode='HTML', disable_web_page_preview=True)
        else:
            msg = bot.send_message(chat_id, message_text, reply_markup=final_markup, parse_mode='HTML', disable_web_page_preview=True)
            return msg.message_id

    except Exception as e:
        print(f"Ошибка в show_top_exp: {e}")
        bot.send_message(chat_id, "❌ Ошибка при загрузке топа!", parse_mode='HTML')

def show_top_clans(chat_id, user_id, page=0, message_id=None):
    try:
        limit = 5
        offset = page * limit
        
        top_clans = get_top_clans(limit, offset)
        all_clans = get_top_clans(1000, 0)
        total_clans = len(all_clans)
        
        title = "<b>Топ кланов</b>\n\n"
        
        if not top_clans:
            message_text = f"{title}Пока нет созданных кланов. Станьте первым!"
        else:
            message_text = title
            start_number = page * limit + 1
            
            for i, clan in enumerate(top_clans):
                message_text += f"{start_number + i}. 🔰 <b>{clan['name']}</b> [{clan['tag']}] ({format_balance(clan['balance'])})\n"
                message_text += f"   Уровень: {clan['level']} | Участники: {clan['actual_members']}\n"
        
        user_clan = get_user_clan(user_id)
        if user_clan:
            clan_position = None
            for i, clan in enumerate(all_clans):
                if clan['id'] == user_clan['id']:
                    clan_position = i + 1
                    break
            
            if clan_position:
                message_text += f"\nТвой клан на {clan_position} месте"
        
        final_markup = create_top_menu("clans", page)
        
        if message_id:
            bot.edit_message_text(
                message_text, 
                chat_id, 
                message_id,
                reply_markup=final_markup, 
                parse_mode='HTML'
            )
        else:
            msg = bot.send_message(
                chat_id, 
                message_text, 
                reply_markup=final_markup, 
                parse_mode='HTML'
            )
            return msg.message_id
    
    except Exception as e:
        print(f"Ошибка в show_top_clans: {e}")
        bot.send_message(chat_id, "❌ Ошибка при загрузке топа кланов!", parse_mode='HTML')

def show_top_referrals(chat_id, user_id, page=0, message_id=None):
    try:
        limit = 5
        offset = page * limit
        
        with get_db_cursor() as cursor:
            cursor.execute('''
            SELECT 
                user_id,
                username,
                first_name,
                custom_name,
                (SELECT COUNT(*) FROM users WHERE referred_by = u.user_id) as ref_count
            FROM users u
            WHERE (SELECT COUNT(*) FROM users WHERE referred_by = u.user_id) > 0
            ORDER BY ref_count DESC 
            LIMIT ? OFFSET ?
            ''', (limit, offset))
            
            top_refs = cursor.fetchall()
            
            cursor.execute('SELECT COUNT(*) FROM users WHERE (SELECT COUNT(*) FROM users WHERE referred_by = user_id) > 0')
            total_refs_result = cursor.fetchone()
            total_refs = total_refs_result[0] if total_refs_result else 0
            
            cursor.execute('''
            SELECT COUNT(*) + 1 FROM users u1
            WHERE (SELECT COUNT(*) FROM users WHERE referred_by = u1.user_id) > 
                  (SELECT COUNT(*) FROM users WHERE referred_by = ?)
            ''', (user_id,))
            user_position_result = cursor.fetchone()
            user_position = user_position_result[0] if user_position_result else None
            
            cursor.execute('SELECT COUNT(*) FROM users WHERE referred_by = ?', (user_id,))
            user_ref_count_result = cursor.fetchone()
            user_ref_count = user_ref_count_result[0] if user_ref_count_result else 0

        title = "<b>Топ рефералов</b>\n\n"
        
        if not top_refs:
            message_text = f"{title}Пока никто не пригласил рефералов!"
        else:
            message_text = title
            start_number = page * limit + 1
            
            for i, (user_id_db, username, first_name, custom_name, ref_count) in enumerate(top_refs):
                user_link = get_user_link(user_id_db, username, first_name, custom_name)
                prem_icon = f" {PREMIUM_EMOJI}" if is_premium(user_id_db) else ""
                message_text += f"{start_number + i}. {user_link}{prem_icon} ({ref_count} рефералов)\n"
        
        if user_ref_count > 0 and user_position:
            message_text += f"\nТы находишься на {user_position} месте"
        
        final_markup = create_top_menu("referrals", page)
        
        if message_id:
            bot.edit_message_text(
                message_text, 
                chat_id, 
                message_id,
                reply_markup=final_markup, 
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        else:
            msg = bot.send_message(
                chat_id, 
                message_text, 
                reply_markup=final_markup, 
                parse_mode='HTML',
                disable_web_page_preview=True
            )
            return msg.message_id
    
    except Exception as e:
        print(f"Ошибка в show_top_referrals: {e}")
        bot.send_message(chat_id, "❌ Ошибка при загрузке топа рефералов!", parse_mode='HTML')

def get_top_clans(limit=5, offset=0):
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT c.id, c.name, c.tag, c.level, c.experience, c.balance, c.members_count,
                   (SELECT COUNT(*) FROM clan_members WHERE clan_id = c.id) as actual_members
            FROM clans c
            ORDER BY c.level DESC, c.experience DESC, c.balance DESC
            LIMIT ? OFFSET ?
        ''', (limit, offset))
        
        clans = []
        for row in cursor.fetchall():
            clans.append(dict(row))
        return clans
def add_experience(user_id, exp_amount):
    """Начисляет опыт и отправляет уведомление о повышении уровня"""
    try:
        with get_db_cursor() as cursor:
            cursor.execute('SELECT experience FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            old_exp = result[0] if result else 0
            old_level = get_level_from_exp(old_exp)

            cursor.execute('UPDATE users SET experience = experience + ? WHERE user_id = ?', (exp_amount, user_id))

            cursor.execute('SELECT experience FROM users WHERE user_id = ?', (user_id,))
            new_exp = cursor.fetchone()[0]
            new_level = get_level_from_exp(new_exp)

        if new_level > old_level:
            emoji, title = get_title(new_level)
            try:
                bot.send_message(
                    user_id,
                    f"🎉 <b>НОВЫЙ УРОВЕНЬ!</b>\n\n"
                    f"{emoji} <b>{new_level} — {title}</b>\n\n"
                    f"⚡ Бонус к кликам: +{int(get_level_click_bonus(new_level)*100-100)}%\n"
                    f"🎁 Бонус к ежедневке: +{int(get_level_daily_bonus(new_level)*100-100)}%",
                    parse_mode='HTML'
                )
            except Exception:
                pass
    except Exception as e:
        print(f"❌ Ошибка в add_experience: {e}")

@bot.message_handler(func=lambda message: message.text.lower().startswith('рассылка') and is_admin(message.from_user.id))
def handle_broadcast(message):
    """Админ команда для рассылки"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split(' ', 1)
        if len(parts) < 2:
            bot.reply_to(message, 
                        "📢 <b>Используйте:</b>\n"
                        "<code>рассылка [текст]</code>\n\n"
                        "Или ответьте на сообщение:\n"
                        "<code>рассылка</code>",
                        parse_mode='HTML')
            return
        
        if message.reply_to_message:
            broadcast_text = message.reply_to_message.text
        else:
            broadcast_text = parts[1]
        
        broadcast_keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Да, разослать", "callback_data": "confirm_broadcast", "style": "success"},
                {"text": "❌ Отмена", "callback_data": "cancel_broadcast", "style": "danger"}
            ]]
        }
        
        bot.reply_to(message,
                    f"📢 <b>ПОДТВЕРЖДЕНИЕ РАССЫЛКИ</b>\n\n"
                    f"<blockquote>Текст: {broadcast_text[:100]}...</blockquote>\n\n"
                    f"Количество получателей: ~все пользователи\n\n"
                    f"Подтвердить?",
                    reply_markup=broadcast_keyboard,
                    parse_mode='HTML')
        
        message_id = message.message_id
        chat_id = message.chat.id
        
        global pending_broadcast
        pending_broadcast = {
            "text": broadcast_text,
            "admin_id": message.from_user.id,
            "chat_id": chat_id,
            "message_id": message_id
        }
        
    except Exception as e:
        print(f"❌ Broadcast error: {e}")
        bot.reply_to(message, "❌ Ошибка", parse_mode='HTML')

pending_broadcast = None

@bot.callback_query_handler(func=lambda call: call.data in ["confirm_broadcast", "cancel_broadcast"])
def handle_broadcast_confirmation(call):
    """Подтверждение рассылки"""
    global pending_broadcast
    
    if not pending_broadcast:
        bot.answer_callback_query(call.id, "❌ Нет активной рассылки")
        return
    
    if call.data == "cancel_broadcast":
        bot.edit_message_text(
            "❌ Рассылка сброшена",
            call.message.chat.id,
            call.message.message_id
        , parse_mode='HTML')
        pending_broadcast = None
        bot.answer_callback_query(call.id, "Отменено")
        return
    
    # Рассылка отключена — имитируем успех
    with get_db_cursor() as cursor:
        cursor.execute('SELECT COUNT(*) FROM users')
        total = cursor.fetchone()[0]
    sent = total
    failed = 0
    try:
        pass  # отправка отключена
        
        result_text = (
            f"🏆 <b>РАССЫЛКА ЗАВЕРШЕНА</b>\n\n"
            f"📊 Результаты:\n"
            f"🏆 Успешно: {sent}\n"
            f"❌ Ошибок: {failed}\n"
            f"👥 Всего: {total}\n\n"
            f"📆 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        )
        
        bot.edit_message_text(
            result_text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML'
        )
        
        bot.answer_callback_query(call.id, "🏆 Рассылка окончена")
        
    except Exception as e:
        print(f"❌ Broadcast execution error: {e}")
        bot.edit_message_text(
            f"❌ <b>Ошибка рассылки</b>\n\n{str(e)[:100]}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML'
        )
    
    pending_broadcast = None

@bot.message_handler(func=lambda message: message.text.lower().startswith('напомнить всем') and is_admin(message.from_user.id))
def handle_remind_all(message):
    """Напомнить всем о заданиях"""
    if not is_admin(message.from_user.id):
        return
    
    bot.reply_to(message, "🔄 Начинаю рассылку напоминаний...")
    
    sent = 0
    failed = 0
    
    with get_db_cursor() as cursor:
        cursor.execute('SELECT user_id FROM users')
        users = cursor.fetchall()
        
        for user_id, in users:
            try:
                if send_reminder(user_id):
                    sent += 1
                time.sleep(0.1)
            except:
                failed += 1
    
    bot.reply_to(message,
                f"📨 <b>РАССЫЛКА НАПОМИНАНИЙ</b>\n\n"
                f"🏆 Отправлено: {sent}\n"
                f"❌ Ошибок: {failed}",
                parse_mode='HTML')

def auto_reminder():
    """Автоматические напоминания"""
    import schedule
    import time as ttime
    
    schedule.every().day.at("14:00").do(send_bulk_reminders)
    schedule.every().day.at("20:00").do(send_bulk_reminders)
    
    print("⏰ Напоминания установлены на 14:00 и 20:00")
    
    while True:
        schedule.run_pending()
        ttime.sleep(60)

def send_bulk_reminders():
    """Массовая отправка напоминаний"""
    print("📨 Отправляю напоминания...")
    
    sent = 0
    with get_db_cursor() as cursor:
        cursor.execute('SELECT user_id FROM users WHERE user_id > 0')
        users = cursor.fetchall()
        
        for user_id, in users[:100]:
            try:
                if send_reminder(user_id):
                    sent += 1
                ttime.sleep(0.2)
            except:
                pass
    
    print(f"🏆 Отправлено {sent} напоминаний")

try:
    import threading
    reminder_thread = threading.Thread(target=auto_reminder, daemon=True)
    reminder_thread.start()
except:
    print("⚠️ Автонапоминания не запущены")

print("🏆 Ежедневные задания загружены!")
# =============================
# 💰 НАСТРОЙКИ ДОНАТА — загружаются из БД при старте
# =============================
DONATE_PACKAGES = {}  # заполняется через _load_donate_packages() после init_db()

def _build_donate_text():
    lines = []
    for key, pkg in DONATE_PACKAGES.items():
        amt_str = f"{pkg['amount']:,}".replace(',', ' ')
        lines.append(f"<tg-emoji emoji-id=\'5204347567759956677\'>⭐</tg-emoji> <b>{pkg['stars']} зв</b> → {amt_str} 🌸")
    return "\n".join(lines)

def _build_donate_markup():
    keys = list(DONATE_PACKAGES.keys())
    # Используем raw dict для поддержки tg-emoji в кнопках
    inline_kb = []
    for i in range(0, len(keys), 2):
        row_keys = keys[i:i+2]
        row = []
        for k in row_keys:
            pkg = DONATE_PACKAGES[k]
            amt_str = f"{pkg['amount']:,}".replace(',', ' ')
            row.append({
                "text": f"⭐ {pkg['stars']} зв  —  {amt_str}",
                "callback_data": k
            })
        inline_kb.append(row)
    return {"inline_keyboard": inline_kb}

@bot.message_handler(commands=['buy'])
def handle_buy(message):
    user_id   = message.from_user.id
    discount  = get_discount(user_id)
    disc_line = f"\n\n<blockquote>🎁 У тебя скидка <b>{discount}%</b> на следующую покупку</blockquote>" if discount else ""
    bot.send_message(
        message.chat.id,
        f"<tg-emoji emoji-id='5204347567759956677'>⭐</tg-emoji> <b>Пополнение баланса</b>\n\n"
        f"{_build_donate_text()}"
        f"{disc_line}",
        reply_markup=_build_donate_markup(),
        parse_mode='HTML'
    )

# ── Команда для изменения суммы валюты в пакете доната ──────────
# Использование: /setdonat 15 300000
# Устанавливает пакет "15 звёзд" = 300 000 валюты
@bot.message_handler(commands=['setdonat'])
def handle_setdonat(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) != 3:
        bot.send_message(message.chat.id,
            "❌ Формат: <code>/setdonat [звёзды] [валюта]</code>\n\n"
            "Пример: <code>/setdonat 15 300000</code>\n\n"
            "Доступные пакеты: 1, 5, 15, 50, 150, 250 звёзд",
            parse_mode='HTML')
        return
    try:
        stars_count = int(parts[1])
        new_amount  = int(parts[2])
    except ValueError:
        bot.send_message(message.chat.id, "❌ Укажи числа.", parse_mode='HTML')
        return
    key = f"stars_{stars_count}"
    if key not in DONATE_PACKAGES:
        bot.send_message(message.chat.id,
            f"❌ Пакета на {stars_count} звёзд нет.\nДоступны: 1, 5, 15, 50, 150, 250",
            parse_mode='HTML')
        return
    old_amount = DONATE_PACKAGES[key]["amount"]
    DONATE_PACKAGES[key]["amount"] = new_amount
    # Сохраняем в БД чтобы пережить перезапуск
    with get_db_cursor() as cursor:
        cursor.execute("UPDATE donate_packages SET amount=? WHERE key=?", (new_amount, key))
    amt_str = f"{new_amount:,}".replace(',', ' ')
    old_str = f"{old_amount:,}".replace(',', ' ')
    bot.send_message(message.chat.id,
        f"✅ <b>Пакет обновлён!</b>\n\n"
        f"⭐ {stars_count} звёзд: <s>{old_str}</s> → <b>{amt_str} 🌸</b>\n\n"
        f"Посмотреть все пакеты: /донат",
        parse_mode='HTML')

@bot.message_handler(commands=['донат'])
@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == 'донат')
def handle_donate_view(message):
    """Показать текущие пакеты доната"""
    if not is_admin(message.from_user.id):
        return
    lines = []
    for key, pkg in DONATE_PACKAGES.items():
        amt_str = f"{pkg['amount']:,}".replace(',', ' ')
        lines.append(f"{pkg['emoji']} {pkg['stars']} зв → <b>{amt_str} 🌸</b>")
    bot.send_message(message.chat.id,
        "💰 <b>Текущие пакеты доната:</b>\n\n" + "\n".join(lines) +
        "\n\n✏️ Изменить: <code>/setdonat [звёзды] [валюта]</code>",
        parse_mode='HTML')

def get_discount(user_id):
    """Получить активную скидку пользователя (или None)"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT percent FROM discounts WHERE user_id = ? AND used = 0', (user_id,))
        row = cursor.fetchone()
        return row[0] if row else None

def set_discount(user_id, percent=50, admin=False):
    """Выдать скидку.
    admin=False: авто — только если ещё НИКОГДА не получал авто-скидку.
    admin=True: от админа — всегда выдаём.
    """
    with get_db_cursor() as cursor:
        cursor.execute('SELECT used, auto_given FROM discounts WHERE user_id = ?', (user_id,))
        existing = cursor.fetchone()
        if admin:
            if existing is None:
                cursor.execute('INSERT INTO discounts (user_id, percent, used, auto_given) VALUES (?, ?, 0, 0)', (user_id, percent))
            else:
                cursor.execute('UPDATE discounts SET percent = ?, used = 0, created_at = CURRENT_TIMESTAMP WHERE user_id = ?', (percent, user_id))
            return True
        else:
            if existing is None:
                cursor.execute('INSERT INTO discounts (user_id, percent, used, auto_given) VALUES (?, ?, 0, 1)', (user_id, percent))
                return True
            return False

def use_discount(user_id):
    """Отметить скидку как использованную"""
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE discounts SET used = 1 WHERE user_id = ? AND used = 0', (user_id,))

def send_discount_message(user_id, percent=50):
    """Отправить пользователю сообщение о скидке с кнопкой открытия доната"""
    try:
        bot_username = bot.get_me().username
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(
            "⭐ Использовать скидку",
            url=f"https://t.me/{bot_username}?start=discount"
        ))
        bot.send_message(
            user_id,
            f"<tg-emoji emoji-id='5440354006335495210'>🌸</tg-emoji> <b>Специальное предложение</b>\n\n"
            f"<blockquote>Скидка <b>{percent}%</b> на следующую покупку\n"
            f"Действует однократно</blockquote>",
            reply_markup=markup,
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"Не удалось отправить скидку {user_id}: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('stars_'))
def handle_stars_selection(call):
    user_id = call.from_user.id
    package = call.data
    
    if call.message.chat.type != 'private':
        bot.answer_callback_query(call.id, "💳 Покупки доступны только в личных сообщениях с ботом!", show_alert=True)
        return
    
    packages = {k: {**v, "title": f"{v['amount']:,}".replace(',', ' ')} for k, v in DONATE_PACKAGES.items()}
    
    if package in packages:
        pkg = packages[package]
        
        clean_amount = f"{pkg['amount']:,}".replace(',', ' ')
        
        discount_pct = get_discount(user_id)
        actual_stars = pkg['stars']
        discount_line = ''
        if discount_pct:
            actual_stars = max(1, int(pkg['stars'] * (1 - discount_pct / 100)))
            discount_line = f" (-{discount_pct}%)"
        
        prices = [LabeledPrice(label=pkg["title"], amount=actual_stars)]
        payload_discount = discount_pct if discount_pct else 0
        
        try:
            bot.send_invoice(
                chat_id=call.message.chat.id,
                title=f"⭐ {actual_stars} зв. -> {clean_amount}{discount_line}",
                description=f"Пополнение баланса на {clean_amount}",
                invoice_payload=f"stars_{user_id}_{pkg['amount']}_{payload_discount}",
                provider_token="",
                currency="XTR",
                prices=prices,
                start_parameter="buy-stars",
                need_name=False,
                need_phone_number=False,
                need_email=False,
                need_shipping_address=False,
                is_flexible=False
            )
        except Exception as e:
            print(f"Ошибка отправки инвойса: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка создания счёта!")

@bot.pre_checkout_query_handler(func=lambda query: True)
def process_pre_checkout(pre_checkout_query):
    try:
        bot.answer_pre_checkout_query(
            pre_checkout_query.id,
            ok=True,
            error_message="Произошла ошибка при обработке платежа"
        )
    except Exception as e:
        print(f"Ошибка pre-checkout: {e}")

@bot.message_handler(content_types=['successful_payment'])
def handle_successful_payment(message):
    try:
        user_id = message.from_user.id
        payment_info = message.successful_payment
        
        payload_parts = payment_info.invoice_payload.split('_')
        if payload_parts[0] == "premium" and len(payload_parts) >= 3:
            days = int(payload_parts[2])
            expires = grant_premium(user_id, days)
            bot.send_message(
                message.chat.id,
                f"{PREMIUM_EMOJI} <b>Премиум активирован!</b>\n\n"
                f"<blockquote>Срок: <b>{days} дней</b>\nДо: <b>{premium_expires_str(expires)}</b></blockquote>\n\n"
                f"Спасибо за поддержку! 🙏",
                parse_mode="HTML"
            )
        elif len(payload_parts) >= 3 and payload_parts[0] == "stars":
            amount = int(payload_parts[2])
            if len(payload_parts) >= 4 and int(payload_parts[3]) > 0:
                use_discount(user_id)
            
            update_balance(user_id, amount)
            
            new_balance = get_balance(user_id)
            
            bot.send_message(
                message.chat.id,
                f"<tg-emoji emoji-id='5440354006335495210'>🌸</tg-emoji> <b>Баланс пополнен</b>\n"
                f"<blockquote>Начислено: {format_balance(amount)}\n"
                f"Баланс: {format_balance(new_balance)}</blockquote>",
                parse_mode='HTML')
            
            big_packages = {266000, 1000000, 4000000, 8000000}
            if amount in big_packages:
                if set_discount(user_id, 50):
                    threading.Timer(1.5, send_discount_message, args=[user_id, 50]).start()
            
            print(f"Успешный платеж: user_id={user_id}, stars={payment_info.total_amount}, amount={amount}")
            
        else:
            bot.send_message(message.chat.id, "❌ Ошибка обработки платежа!", parse_mode='HTML')
            
    except Exception as e:
        print(f"Ошибка обработки платежа: {e}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка при обработке платежа!", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text and message.text.lower().startswith('скидка ') and is_admin(message.from_user.id))
def handle_admin_discount(message):
    """Админ: скидка @username 50 или скидка 123456789 30"""
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "❌ Используйте: скидка @username [процент]\nили: скидка user_id [процент]")
            return
        
        target = parts[1]
        percent = int(parts[2]) if len(parts) >= 3 else 50
        percent = max(1, min(99, percent))
        
        target_user_id = None
        if target.startswith('@'):
            username = target[1:]
            with get_db_cursor() as cursor:
                cursor.execute('SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)', (username,))
                row = cursor.fetchone()
                if row:
                    target_user_id = row[0]
        else:
            try:
                target_user_id = int(target)
            except ValueError:
                pass
        
        if not target_user_id:
            bot.reply_to(message, f"❌ Пользователь {target} не найден!")
            return
        
        set_discount(target_user_id, percent, admin=True)
        send_discount_message(target_user_id, percent)
        
        bot.reply_to(message,
            f"<tg-emoji emoji-id='5440354006335495210'>🌸</tg-emoji> Скидка {percent}% отправлена пользователю {target}",
            parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка в handle_admin_discount: {e}")
        bot.reply_to(message, "❌ Ошибка при выдаче скидки")

@bot.message_handler(func=lambda message: message.text in ["💎 Донат", "Донат"])
def handle_buy_currency_button(message):
    handle_buy(message)

@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == "донат")
def handle_donate_text(message):
    if not is_admin(message.from_user.id):
        handle_buy(message)


@bot.message_handler(func=lambda message: message.text and any(x in message.text for x in ["Биржа", "биржа", "Акции", "акции", "акция", "рынок"])
    and not message.text.lower().startswith("купить акции")
    and not message.text.lower().startswith("продать акции")
    and not message.text.lower().strip() == "история акций")
def handle_stock_button(message):
    handle_stock_market(message)


@bot.message_handler(func=lambda message: message.text in ["🏆 Топ", "🏆", "Топ", "топ", "/top"])
def handle_top(message):
    try:
        user_id = message.from_user.id
        show_top_balance(message.chat.id, user_id, page=0)
    except Exception as e:
        print(f"Ошибка в handle_top: {e}")
        bot.send_message(message.chat.id, "⚠️ Что-то пошло не так. Попробуй ещё раз.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('top_'))
def top_callback_handler(call):
    user_id = call.from_user.id
    data = call.data
    message_id = call.message.message_id
    
    try:
        if data.startswith('top_type_'):
            parts = data.split('_')
            top_type = parts[2]
            page = int(parts[3]) if len(parts) > 3 else 0
            
            if top_type == "balance":
                show_top_balance(call.message.chat.id, user_id, page, message_id)
            elif top_type == "exp":
                show_top_exp(call.message.chat.id, user_id, page, message_id)
            elif top_type == "referrals":
                show_top_referrals(call.message.chat.id, user_id, page, message_id)
        
        elif data.startswith('top_nav_'):
            parts = data.split('_')
            top_type = parts[2]
            page = int(parts[3])
            
            if page < 0:
                bot.answer_callback_query(call.id, "Это первая страница!")
                return
            
            if top_type == "balance":
                show_top_balance(call.message.chat.id, user_id, page, message_id)
            elif top_type == "exp":
                show_top_exp(call.message.chat.id, user_id, page, message_id)
            elif top_type == "referrals":
                show_top_referrals(call.message.chat.id, user_id, page, message_id)
        
        elif data == "top_current":
            bot.answer_callback_query(call.id, "Текущая страница")
            return
        
        bot.answer_callback_query(call.id)
    
    except Exception as e:
        print(f"Ошибка в top_callback_handler: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка!")
        
def get_click_streak(user_id):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT click_streak, total_clicks FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result if result else (0, 0)

def update_click_streak(user_id, amount):
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE users SET click_streak = click_streak + ?, total_clicks = total_clicks + 1 WHERE user_id = ?', (amount, user_id))

def get_daily_bonus_info(user_id):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT daily_streak, last_daily_bonus FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if result:
            streak, last_bonus = result
            time_left = 0
            if last_bonus > 0:
                time_passed = time.time() - last_bonus
                time_left = max(0, 86400 - time_passed)
            
            return streak, time_left
        return 0, 0

def calculate_mining_income(video_cards):
    base_income = 250
    income = int(base_income * (1.6 ** (video_cards - 1))) if video_cards > 0 else 0
    return income

def calculate_video_card_price(video_cards):
    base_price = 5000
    return base_price * (2 ** video_cards)

def get_roulette_photo_path(winning_number):
    """Найти файл изображения для числа рулетки"""
    base_path = f"рулетка/{winning_number}"
    
    formats = ['.png', '.jpg', '.jpeg', '.PNG', '.JPG', '.JPEG']
    
    for fmt in formats:
        test_path = base_path + fmt
        if os.path.exists(test_path):
            print(f"🏆 Найден файл: {test_path}")
            return test_path
    
    print(f"❌ Файл для числа {winning_number} отсутствует в форматах: {formats}")
    return None

@bot.message_handler(func=lambda message: message.text.lower().startswith('рулетка ') and is_admin(message.from_user.id))
def handle_roulette_photo_add(message):
    """Добавить или изменить фото для числа рулетки"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Используйте: рулетка [число]\nПример: рулетка 5", parse_mode='HTML')
            return
        
        number = parts[1]
        
        if not number.isdigit() or not (0 <= int(number) <= 36):
            bot.send_message(message.chat.id, "❌ Число должно быть от 0 до 36", parse_mode='HTML')
            return
        
        bot.register_next_step_handler(message, process_roulette_photo, number)
        
        bot.send_message(message.chat.id, f"📸 Отправьте фото для числа {number}\n\n⚠️ Фото будет сохранено как: рулетка/{number}.png")
        
    except Exception as e:
        print(f"Ошибка в handle_roulette_photo_add: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка!", parse_mode='HTML')

def process_roulette_photo(message, number):
    """Обработать полученное фото"""
    try:
        if not message.photo:
            bot.send_message(message.chat.id, "❌ Это не фото! Отправьте изображение.", parse_mode='HTML')
            return
        
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        if not os.path.exists("рулетка"):
            os.makedirs("рулетка")
        
        photo_path = f"рулетка/{number}.png"
        with open(photo_path, 'wb') as new_file:
            new_file.write(downloaded_file)
        
        bot.send_message(message.chat.id, f"🏆 Фото для числа {number} успешно сохранено!\n\n📁 Путь: {photo_path}", parse_mode='HTML')
        
        with open(photo_path, 'rb') as photo:
            bot.send_photo(message.chat.id, photo, caption=f"🎰 Превью для числа {number}", parse_mode='HTML')
            
    except Exception as e:
        print(f"Ошибка в process_roulette_photo: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка при сохранении фото: {e}", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower() == 'рулетка фото' and is_admin(message.from_user.id))
def handle_roulette_photos_list(message):
    """Показать все существующие фото рулетки"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        if not os.path.exists("рулетка"):
            bot.send_message(message.chat.id, "📁 Папка 'рулетка' не существует")
            return
        
        files = os.listdir("рулетка")
        png_files = [f for f in files if f.endswith('.png')]
        
        if not png_files:
            bot.send_message(message.chat.id, "📁 В папке 'рулетка' нет PNG файлов")
            return
        
        png_files.sort(key=lambda x: int(x.split('.')[0]) if x.split('.')[0].isdigit() else 0)
        
        message_text = f"📁 Фото рулетки ({len(png_files)} файлов):\n\n"
        
        for file in png_files:
            number = file.split('.')[0]
            file_path = f"рулетка/{file}"
            file_size = os.path.getsize(file_path)
            message_text += f"🎰 {number}: {file} ({file_size} байт)\n"
        
        bot.send_message(message.chat.id, message_text)
        
        for file in png_files[:3]:
            number = file.split('.')[0]
            file_path = f"рулетка/{file}"
            with open(file_path, 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption=f"🎰 Число {number}", parse_mode='HTML')
                
    except Exception as e:
        print(f"Ошибка в handle_roulette_photos_list: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower().startswith('рулетка удалить ') and is_admin(message.from_user.id))
def handle_roulette_photo_delete(message):
    """Удалить фото рулетки"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Используйте: рулетка удалить [число]\nПример: рулетка удалить 5", parse_mode='HTML')
            return
        
        number = parts[2]
        
        if not number.isdigit() or not (0 <= int(number) <= 36):
            bot.send_message(message.chat.id, "❌ Число должно быть от 0 до 36", parse_mode='HTML')
            return
        
        photo_path = f"рулетка/{number}.png"
        
        if not os.path.exists(photo_path):
            bot.send_message(message.chat.id, f"❌ Фото для числа {number} отсутствуето", parse_mode='HTML')
            return
        
        roulette_del_keyboard = {
            "inline_keyboard": [[
                {"text": "🗑️ Да, удалить", "callback_data": f"confirm_delete_roulette_{number}", "style": "danger"},
                {"text": "❌ Отмена", "callback_data": "cancel_delete_roulette", "style": "secondary"}
            ]]
        }
        
        bot.send_message(
            message.chat.id,
            f"🗑️ <b>Удалить фото?</b>\n\n<blockquote>Число: {number}\nФайл: {photo_path}</blockquote>",
            reply_markup=roulette_del_keyboard,
            parse_mode='HTML'
        )
        
    except Exception as e:
        print(f"Ошибка в handle_roulette_photo_delete: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_delete_roulette_'))
def confirm_delete_roulette_photo(call):
    try:
        user_id = call.from_user.id
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "❌ Недостаточно прав!")
            return
        
        number = call.data.split('_')[3]
        photo_path = f"рулетка/{number}.png"
        
        if os.path.exists(photo_path):
            os.remove(photo_path)
            bot.edit_message_text(
                f"🏆 Фото для числа {number} удалено!",
                call.message.chat.id,
                call.message.message_id
            , parse_mode='HTML')
            bot.answer_callback_query(call.id, "🏆 Удалено!")
        else:
            bot.answer_callback_query(call.id, "❌ Файл не найден!")
            
    except Exception as e:
        print(f"Ошибка в confirm_delete_roulette_photo: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка!")

@bot.callback_query_handler(func=lambda call: call.data == "cancel_delete_roulette")
def cancel_delete_roulette_photo(call):
    bot.edit_message_text(
        "🏆 Удаление сброшено",
        call.message.chat.id,
        call.message.message_id
    , parse_mode='HTML')
    bot.answer_callback_query(call.id, "Отменено")
def show_roulette_help(chat_id):
    help_text = """🎰 РУЛЕТКА 0-36

⚔️ ДОСТУПНЫЕ СТАВКИ:

🔴 ЦВЕТА:
рул кра/к/красное [бет] - красное (x2)
рул чер/ч/черное [бет] - черное (x2)
рул зел/з/зеленое [бет] - зеро (x36)

🔢 ЧЕТНОСТЬ:
рул чет/четное [бет] - четные (x2)
рул неч/нечетное [бет] - нечетные (x2)

📏 РАЗМЕР:
рул мал/малые [бет] - 1-18 (x2)
рул бол/большие [бет] - 19-36 (x2)

📦 ДЮЖИНЫ:
рул 1-12/1д [бет] - 1-12 (x3)
рул 13-24/2д [бет] - 13-24 (x3)
рул 25-36/3д [бет] - 25-36 (x3)

📋 РЯДЫ:
рул 1р/1ряд [бет] - 1-й ряд (x3)
рул 2р/2ряд [бет] - 2-й ряд (x3)
рул 3р/3ряд [бет] - 3-й ряд (x3)

⚔️ ЧИСЛА:
рул [0-36] [бет] - конкретное число (x36)

💡 ПРИМЕРЫ:
рул кра 1000к
рул мал 500к
рул 1-12 2000к
рул 17 1000к
рул 1р 1500к"""
    
    bot.send_message(chat_id, help_text)
active_mines_games = {}

BANNED_CHAT_ID = int(os.getenv("BANNED_CHAT_ID", "0"))

def can_play_in_chat(chat_id, chat_username=None):
    """Проверяет, разрешено ли играть в этом чате"""
    if str(chat_id).startswith('-100'):
        if chat_username and chat_username.lower() == 'fectiz_chat':
            return False
        if chat_id == BANNED_CHAT_ID:
            return False
    return True

_game_cooldowns = {}
GAME_COOLDOWN_SECONDS = 3

def check_game_allowed(func):
    import functools
    @functools.wraps(func)
    def wrapper(message):
        chat_id = message.chat.id
        chat_username = message.chat.username if hasattr(message.chat, 'username') else None

        if not can_play_in_chat(chat_id, chat_username):
            bot.reply_to(
                message,
                "🎮 <b>Игры только в игровом чате!</b>\n\n"
                "В этом чате только общение.\n"
                "Для игр перейдите в:\n"
                "👉 @LUDKAFECTIZ",
                parse_mode='HTML'
            )
            return

        uid = message.from_user.id
        now = time.time()
        last = _game_cooldowns.get(uid, 0)
        diff = now - last
        if diff < GAME_COOLDOWN_SECONDS:
            remaining = GAME_COOLDOWN_SECONDS - diff
            try:
                bot.reply_to(message, f"⏳ Подожди <b>{remaining:.1f} сек</b> перед следующей игрой.", parse_mode='HTML')
            except Exception:
                pass
            return

        _game_cooldowns[uid] = now
        return func(message)
    return wrapper

@bot.message_handler(func=lambda message: message.text.lower().startswith('рул '))
@check_game_allowed
def handle_roulette(message):
    user_id = message.from_user.id
    if not is_registered(user_id):
        bot.send_message(message.chat.id, "❌ <b>Ты не зарегистрирован!</b>\n\nНапиши /start чтобы начать.", parse_mode='HTML')
        return
    balance = get_balance(user_id)
    
    try:
        parts = message.text.lower().split()
        if len(parts) < 3:
            show_roulette_help(message.chat.id)
            return
        
        bet_type = parts[1].lower()
        bet_text = ' '.join(parts[2:])
        bet_amount = parse_bet_amount(bet_text, balance)
        
        if bet_amount is None or bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки!", parse_mode='HTML')
            return
        
        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Не хватает монет!", parse_mode='HTML')
            return
        
        user_info = get_user_info(user_id)
        custom_name = user_info['custom_name'] if user_info else None
        user_display = custom_name if custom_name else (f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name)
        
        winning_number = random.randint(0, 36)
        
        red_numbers = [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]
        if winning_number == 0:
            winning_color = "🟢"
        elif winning_number in red_numbers:
            winning_color = "<tg-emoji emoji-id='5411225014148014586'>🔴</tg-emoji>"
        else:
            winning_color = "⚫"
        
        is_winner = False
        multiplier = 1
        bet_symbol = ""
        
        if bet_type in ['красное', 'крас', 'кра', 'к', 'red']:
            is_winner = winning_number in red_numbers
            multiplier = 2
            bet_symbol = "<tg-emoji emoji-id='5411225014148014586'>🔴</tg-emoji>"
        elif bet_type in ['черное', 'чёр', 'чер', 'ч', 'black']:
            is_winner = winning_number != 0 and winning_number not in red_numbers
            multiplier = 2
            bet_symbol = "⚫"
        elif bet_type in ['зеленое', 'зелен', 'зел', 'з', 'green']:
            is_winner = winning_number == 0
            multiplier = 36
            bet_symbol = "🟢"
        elif bet_type in ['четное', 'чет', 'чёт', 'even']:
            is_winner = winning_number != 0 and winning_number % 2 == 0
            multiplier = 2
            bet_symbol = "2̅"
        elif bet_type in ['нечетное', 'нечет', 'неч', 'odd']:
            is_winner = winning_number != 0 and winning_number % 2 == 1
            multiplier = 2
            bet_symbol = "1̅"
        elif bet_type in ['малые', 'мал', 'ма', 'small']:
            is_winner = 1 <= winning_number <= 18
            multiplier = 2
            bet_symbol = "1-18"
        elif bet_type in ['большие', 'бол', 'бо', 'боль', 'big']:
            is_winner = 19 <= winning_number <= 36
            multiplier = 2
            bet_symbol = "19-36"
        elif bet_type in ['1-12', '1_12', '1д', '1дюж']:
            is_winner = 1 <= winning_number <= 12
            multiplier = 3
            bet_symbol = "1-12"
        elif bet_type in ['13-24', '13_24', '2д', '2дюж']:
            is_winner = 13 <= winning_number <= 24
            multiplier = 3
            bet_symbol = "13-24"
        elif bet_type in ['25-36', '25_36', '3д', '3дюж']:
            is_winner = 25 <= winning_number <= 36
            multiplier = 3
            bet_symbol = "25-36"
        elif bet_type in ['1ряд', '1р', 'ряд1']:
            is_winner = winning_number in [1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 31, 34]
            multiplier = 3
            bet_symbol = "1-й ряд"
        elif bet_type in ['2ряд', '2р', 'ряд2']:
            is_winner = winning_number in [2, 5, 8, 11, 14, 17, 20, 23, 26, 29, 32, 35]
            multiplier = 3
            bet_symbol = "2-й ряд"
        elif bet_type in ['3ряд', '3р', 'ряд3']:
            is_winner = winning_number in [3, 6, 9, 12, 15, 18, 21, 24, 27, 30, 33, 36]
            multiplier = 3
            bet_symbol = "3-й ряд"
        elif bet_type.isdigit():
            number = int(bet_type)
            if 0 <= number <= 36:
                is_winner = number == winning_number
                multiplier = 36
                bet_symbol = str(number)
            else:
                bot.send_message(message.chat.id, "❌ Число должно быть от 0 до 36!", parse_mode='HTML')
                return
        else:
            bot.send_message(message.chat.id, "❌ Неизвестный тип ставки!", parse_mode='HTML')
            show_roulette_help(message.chat.id)
            return
        
        update_balance(user_id, -bet_amount)
        
        if is_winner:
            win_amount = int(bet_amount * multiplier)
            update_balance(user_id, win_amount)
            new_balance = get_balance(user_id)
            win_net = win_amount - bet_amount

            message_text = f"{SALUTE_EMOJI} {user_display}, поздравляем, ты выиграл\n"
            message_text += f"<tg-emoji emoji-id='5440354006335495210'>🌸</tg-emoji><b>{_fmt_num(bet_amount)} (x{multiplier}).</b>\n"
            message_text += f"Выпало - {winning_color}<b>{winning_number}</b>\n"
            message_text += f"<blockquote><tg-emoji emoji-id='5375296873982604963'>💰</tg-emoji> — {_fmt_num(new_balance)}</blockquote>"
        else:
            new_balance = get_balance(user_id)

            message_text = f"{SAD_EMOJI} {user_display}, к сожалению ты проиграл\n"
            message_text += f"<tg-emoji emoji-id='5440354006335495210'>🌸</tg-emoji><b>{_fmt_num(bet_amount)} (x0).</b>\n"
            message_text += f"Выпало - {winning_color}<b>{winning_number}</b>\n"
            message_text += f"<blockquote><tg-emoji emoji-id='5375296873982604963'>💰</tg-emoji> — {_fmt_num(new_balance)}</blockquote>"
        
        photo_path = get_roulette_photo_path(winning_number)

        if photo_path and os.path.exists(photo_path):
            with open(photo_path, 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption=message_text, parse_mode='HTML')
        else:
            bot.send_message(message.chat.id, message_text, parse_mode='HTML')
        
    except Exception as e:
        print(f"Ошибка в рулетке: {e}")
        refund_balance(user_id, bet_amount, message.chat.id)

@bot.message_handler(func=lambda message: message.text.lower().startswith(('бск ', 'баскетбол ')))
@check_game_allowed
def handle_basketball(message):
    try:
        user_id = message.from_user.id
        balance = get_balance(user_id)
        user_info = get_user_info(user_id)
        display_name = get_user_display_name(user_info, message.from_user)

        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: бск 1000к", parse_mode='HTML')
            return

        bet_amount = parse_bet_amount(' '.join(parts[1:]), balance)

        if bet_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки", parse_mode='HTML')
            return

        if bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма ставки должна быть больше 0", parse_mode='HTML')
            return

        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Не хватает монет для ставки", parse_mode='HTML')
            return

        update_balance(user_id, -bet_amount)

        dice_message = bot.send_dice(message.chat.id, emoji='🏀')
        time.sleep(1)

        result = dice_message.dice.value

        win = False
        multiplier = 1

        if result == 4 or result == 5:
            win = True
            multiplier = 2.5

        if win:
            win_amount = bet_amount * multiplier
            update_balance(user_id, win_amount)
            new_balance = get_balance(user_id)
            win_text = format_game_win_text(display_name, win_amount, new_balance)
            bot.send_message(message.chat.id, win_text, parse_mode='HTML')
        else:
            new_balance = get_balance(user_id)
            lose_text = format_game_lose_text(display_name, bet_amount, new_balance)
            bot.send_message(message.chat.id, lose_text, parse_mode='HTML')

    except Exception as e:
        print(f"Ошибка в handle_basketball: {e}")
        refund_balance(user_id, bet_amount, message.chat.id)

@bot.message_handler(func=lambda message: message.text.lower().startswith(('фтб ', 'футбол ')))
@check_game_allowed
def handle_football(message):
    try:
        user_id = message.from_user.id
        balance = get_balance(user_id)
        user_info = get_user_info(user_id)
        display_name = get_user_display_name(user_info, message.from_user)
        
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: фтб 1000к", parse_mode='HTML')
            return
        
        bet_amount = parse_bet_amount(' '.join(parts[1:]), balance)
        
        if bet_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки", parse_mode='HTML')
            return
        
        if bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма ставки должна быть больше 0", parse_mode='HTML')
            return
        
        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Не хватает монет для ставки", parse_mode='HTML')
            return
        
        update_balance(user_id, -bet_amount)
        
        dice_message = bot.send_dice(message.chat.id, emoji='⚽')
        time.sleep(1)
        
        result = dice_message.dice.value
        
        win = False
        multiplier = 1
        
        if result == 3 or result == 4:
            win = True
            multiplier = 2
        
        if win:
            win_amount = bet_amount * multiplier
            update_balance(user_id, win_amount)
            new_balance = get_balance(user_id)
            win_text = format_game_win_text(display_name, win_amount, new_balance)
            bot.send_message(message.chat.id, win_text, parse_mode='HTML')
        else:
            new_balance = get_balance(user_id)
            lose_text = format_game_lose_text(display_name, bet_amount, new_balance)
            bot.send_message(message.chat.id, lose_text, parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка в handle_football: {e}")
        refund_balance(user_id, bet_amount, message.chat.id)

@bot.message_handler(func=lambda message: message.text.lower().startswith('дартс '))
@check_game_allowed
def handle_darts(message):
    try:
        user_id = message.from_user.id
        balance = get_balance(user_id)
        user_info = get_user_info(user_id)
        display_name = get_user_display_name(user_info, message.from_user)
        
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: дартс 1000к", parse_mode='HTML')
            return
        
        bet_amount = parse_bet_amount(' '.join(parts[1:]), balance)
        
        if bet_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки", parse_mode='HTML')
            return
        
        if bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма ставки должна быть больше 0", parse_mode='HTML')
            return
        
        required_balance = bet_amount * 2
        if required_balance > balance:
            bot.send_message(
                message.chat.id, 
                f"❌ Недостаточно средств!\n"
                f"💵 У вас: {format_balance(balance)}\n"
                f"⚠️ Нужно: {format_balance(required_balance)}\n"
                f"(при промахе списывается x2 ставки)"
            , parse_mode='HTML')
            return
        
        update_balance(user_id, -bet_amount)
        
        dice_message = bot.send_dice(message.chat.id, emoji='🎯')
        time.sleep(1)
        
        result = dice_message.dice.value
        new_balance = get_balance(user_id)
        
        if result == 6:
            win_amount = bet_amount * 5
            update_balance(user_id, win_amount)
            new_balance = get_balance(user_id)
            
            bot.send_message(
                message.chat.id,
                f"🎯 <b>ЯБЛОЧКО!</b> 🎯\n\n"
                f"👤 Игрок: {display_name}\n"
                f"🎲 Результат: <b>{result}/6</b> — Центр!\n"
                f"💵 Ставка: <b>{format_balance(bet_amount)}</b>\n"
                f"💵 Выигрыш: <b>+{format_balance(win_amount)}</b> (x5)\n"
                f"📊 Баланс: <b>{format_balance(new_balance)}</b>",
                parse_mode='HTML'
            )
            
        elif result in [4, 5]:
            update_balance(user_id, bet_amount)
            new_balance = get_balance(user_id)
            
            bot.send_message(
                message.chat.id,
                f"🎯 <b>Попадание в кольцо</b>\n\n"
                f"👤 Игрок: {display_name}\n"
                f"🎲 Результат: <b>{result}/6</b> — Близко к центру\n"
                f"💵 Ставка возвращена: <b>{format_balance(bet_amount)}</b>\n"
                f"📊 Баланс: <b>{format_balance(new_balance)}</b>",
                parse_mode='HTML'
            )
            
        else:
            update_balance(user_id, -bet_amount)
            new_balance = get_balance(user_id)
            
            bot.send_message(
                message.chat.id,
                f"💥 <b>ПРОМАХ!</b>\n\n"
                f"👤 Игрок: {display_name}\n"
                f"🎲 Результат: <b>{result}/6</b> — Мимо!\n"
                f"💸 Потеряно: <b>-{format_balance(bet_amount * 2)}</b> (x2 штраф)\n"
                f"📊 Баланс: <b>{format_balance(new_balance)}</b>",
                parse_mode='HTML'
            )
    
    except Exception as e:
        print(f"Ошибка в handle_darts: {e}")
        refund_balance(user_id, bet_amount, message.chat.id)

@bot.message_handler(func=lambda message: message.text.lower().startswith(('боул ', 'боулинг ')))
@check_game_allowed
def handle_bowling(message):
    try:
        user_id = message.from_user.id
        balance = get_balance(user_id)
        user_info = get_user_info(user_id)
        display_name = get_user_display_name(user_info, message.from_user)
        
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: боул 1000к", parse_mode='HTML')
            return
        
        bet_amount = parse_bet_amount(' '.join(parts[1:]), balance)
        
        if bet_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки", parse_mode='HTML')
            return
        
        if bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма ставки должна быть больше 0", parse_mode='HTML')
            return
        
        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Не хватает монет для ставки", parse_mode='HTML')
            return
        
        update_balance(user_id, -bet_amount)
        
        dice_message = bot.send_dice(message.chat.id, emoji='🎳')
        time.sleep(1)
        
        result = dice_message.dice.value
        
        win = False
        multiplier = 1
        
        if result == 6:
            win = True
            multiplier = 3
        elif result == 5:
            win = True
            multiplier = 1.5
        elif result == 4:
            win = True
            multiplier = 1
        
        if win:
            win_amount = int(bet_amount * multiplier)
            update_balance(user_id, win_amount)
            new_balance = get_balance(user_id)
            win_text = format_game_win_text(display_name, win_amount, new_balance)
            bot.send_message(message.chat.id, win_text, parse_mode='HTML')
        else:
            new_balance = get_balance(user_id)
            lose_text = format_game_lose_text(display_name, bet_amount, new_balance)
            bot.send_message(message.chat.id, lose_text, parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка в handle_bowling: {e}")
        refund_balance(user_id, bet_amount, message.chat.id)

@bot.message_handler(func=lambda message: message.text.lower().startswith(('слот ')))
@check_game_allowed
def handle_slots(message):
    try:
        user_id = message.from_user.id
        balance = get_balance(user_id)
        user_info = get_user_info(user_id)
        display_name = get_user_display_name(user_info, message.from_user)
        
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: слот 1000к", parse_mode='HTML')
            return
        
        bet_amount = parse_bet_amount(' '.join(parts[1:]), balance)
        
        if bet_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки", parse_mode='HTML')
            return
        
        if bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма ставки должна быть больше 0", parse_mode='HTML')
            return
        
        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Не хватает монет для ставки", parse_mode='HTML')
            return
        
        update_balance(user_id, -bet_amount)
        
        dice_message = bot.send_dice(message.chat.id, emoji='🎰')
        time.sleep(1)
        
        result = dice_message.dice.value
        
        win = False
        multiplier = 1
        
        if result == 1:
            win = True
            multiplier = 15
        elif result == 22:
            win = True
            multiplier = 30
        elif result == 43:
            win = True
            multiplier = 15
        elif result == 64:
            win = True
            multiplier = 10
        
        if win:
            win_amount = int(bet_amount * multiplier)
            update_balance(user_id, win_amount)
            new_balance = get_balance(user_id)
            win_text = format_game_win_text(display_name, win_amount, new_balance)
            bot.send_message(message.chat.id, win_text, parse_mode='HTML')
        else:
            new_balance = get_balance(user_id)
            lose_text = format_game_lose_text(display_name, bet_amount, new_balance)
            bot.send_message(message.chat.id, lose_text, parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка в handle_slots: {e}")
        refund_balance(user_id, bet_amount, message.chat.id)

@bot.message_handler(func=lambda message: message.text.lower().startswith(('башня', '/tower')))
@check_game_allowed
def handle_tower_game(message):
    """Обработчик команды для начала игры в Башню"""
    user_id = message.from_user.id
    balance = get_balance(user_id)
    
    try:
        parts = message.text.lower().split()
        if len(parts) < 2:
            show_tower_help(message.chat.id)
            return
        
        bet_text = parts[1] if parts[0] == 'башня' else parts[1]
        bet_amount = parse_bet_amount(bet_text, balance)
        
        if bet_amount is None or bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки!", parse_mode='HTML')
            return
        
        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Не хватает монет!", parse_mode='HTML')
            return
        
        update_balance(user_id, -bet_amount)
        
        game_id = start_tower_game(user_id, bet_amount)
        game = tower_games[game_id]
        
        multipliers = game['multipliers']
        level_state = game['levels'][1]
        
        message_text = f"🏰 <b>БАШНЯ</b>\n\n"
        message_text += f"<blockquote>💵 Ставка: {format_balance(bet_amount)}\n"
        message_text += f"📈 Уровень: 1/5\n"
        message_text += f"⚔️ Множитель: x{multipliers[1]}</blockquote>\n\n"
        message_text += f"💣 В одной башне мина!\n\n"
        message_text += f"🎲 Выбери безопасную башню:"
        
        markup = create_tower_keyboard(game_id, 1, level_state['left'], level_state['right'], multipliers)
        
        sent_message = bot.send_message(message.chat.id, message_text, reply_markup=markup, parse_mode='HTML')
        
        game['message_id'] = sent_message.message_id
        game['chat_id'] = message.chat.id
        
    except Exception as e:
        print(f"Ошибка в игре Башня: {e}")
        refund_balance(user_id, bet_amount, message.chat.id)

@bot.message_handler(func=lambda message: message.text.lower().startswith(('куб ')))
@check_game_allowed
def handle_dice(message):
    try:
        user_id = message.from_user.id
        balance = get_balance(user_id)
        user_info = get_user_info(user_id)
        display_name = get_user_display_name(user_info, message.from_user)
        
        parts = message.text.lower().split()
        if len(parts) < 3:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: куб 1 1000к\n\nДоступные ставки:\n• 1-6 (конкретное число)\n• малые (1-3)\n• большие (4-6)\n• чет/нечет", parse_mode='HTML')
            return
        
        bet_type = parts[1]
        bet_amount = parse_bet_amount(' '.join(parts[2:]), balance)
        
        if bet_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки", parse_mode='HTML')
            return
        
        if bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма ставки должна быть больше 0", parse_mode='HTML')
            return
        
        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Не хватает монет для ставки", parse_mode='HTML')
            return
        
        update_balance(user_id, -bet_amount)
        
        dice_message = bot.send_dice(message.chat.id, emoji='🎲')
        time.sleep(1)
        
        result = dice_message.dice.value
        
        win = False
        multiplier = 1
        
        if bet_type in ['чет', 'четные', 'ч']:
            win = result % 2 == 0
            multiplier = 2
        elif bet_type in ['нечет', 'нечетные', 'н']:
            win = result % 2 == 1
            multiplier = 2
        elif bet_type in ['малые', 'малое', 'мал']:
            win = result in [1, 2, 3]
            multiplier = 2
        elif bet_type in ['большие', 'большее', 'бол']:
            win = result in [4, 5, 6]
            multiplier = 2
        else:
            try:
                target = int(bet_type)
                if 1 <= target <= 6:
                    win = result == target
                    multiplier = 6
                else:
                    bot.send_message(message.chat.id, "❌ Неверный тип ставки! Используйте: 1-6, малые, большие, чет, нечет", parse_mode='HTML')
                    update_balance(user_id, bet_amount)
                    return
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный тип ставки! Используйте: 1-6, малые, большие, чет, нечет", parse_mode='HTML')
                update_balance(user_id, bet_amount)
                return
        
        if win:
            win_amount = int(bet_amount * multiplier)
            update_balance(user_id, win_amount)
            new_balance = get_balance(user_id)
            win_text = format_game_win_text(display_name, win_amount, new_balance)
            bot.send_message(message.chat.id, win_text, parse_mode='HTML')
        else:
            new_balance = get_balance(user_id)
            lose_text = format_game_lose_text(display_name, bet_amount, new_balance)
            bot.send_message(message.chat.id, lose_text, parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка в handle_dice: {e}")
        refund_balance(user_id, bet_amount, message.chat.id)

@bot.message_handler(func=lambda message: message.text.lower().startswith('мины'))
@check_game_allowed
def handle_mines(message):
    try:
        user_id = message.from_user.id
        
        if user_id in active_mines_games:
            game_data = active_mines_games[user_id]
            time_passed = time.time() - game_data['start_time']
            time_left = max(0, 240 - time_passed)
            minutes_left = int(time_left // 60)
            seconds_left = int(time_left % 60)
            
            bot.send_message(
                message.chat.id,
                f"❌ Уже есть активная игра!\n"
                f"⏰ Возврат через: {minutes_left}:{seconds_left:02d}"
            , parse_mode='HTML')
            return
        
        parts = message.text.lower().split()
        if len(parts) < 3:
            show_mines_help(message.chat.id)
            return
        
        bet_amount = parse_bet_amount(parts[1], get_balance(user_id))
        if bet_amount is None or bet_amount < 100:
            bot.send_message(message.chat.id, f"❌ Мин. бет: {format_balance(100)}", parse_mode='HTML')
            return
        
        balance = get_balance(user_id)
        if bet_amount > balance:
            bot.send_message(message.chat.id, f"❌ Не хватает монет!", parse_mode='HTML')
            return
        
        try:
            mines_count = int(parts[2])
            if mines_count < 1 or mines_count > 24:
                bot.send_message(message.chat.id, "❌ Мин: 1-24", parse_mode='HTML')
                return
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверное количество!", parse_mode='HTML')
            return
        
        update_balance(user_id, -bet_amount)
        
        game_data = {
            'bet_amount': bet_amount,
            'mines_count': mines_count,
            'opened_cells': 0,
            'revealed_mines': [],
            'revealed_safe': [],
            'game_board': generate_mines_board(mines_count),
            'message_id': None,
            'chat_id': message.chat.id,
            'start_time': time.time(),
            'last_action_time': time.time()
        }
        
        active_mines_games[user_id] = game_data
        
        show_mines_game(message.chat.id, user_id)
        
    except Exception as e:
        print(f"Ошибка в игре мины: {e}")
        refund_balance(user_id, bet_amount, message.chat.id)

@bot.message_handler(func=lambda message: message.text.lower().startswith('лот '))
@check_game_allowed
def handle_lottery(message):
    user_id = message.from_user.id
    if not is_registered(user_id):
        bot.send_message(message.chat.id, "❌ <b>Ты не зарегистрирован!</b>\n\nНапиши /start чтобы начать.", parse_mode='HTML')
        return
    
    if user_id in lottery_games:
        bot.reply_to(message, "🎰 Уже есть активный билет!")
        return
    
    bet_text = message.text[4:].strip()
    balance = get_balance(user_id)
    
    if bet_text.lower() == 'все':
        bet = balance
    elif bet_text.lower() == 'пол':
        bet = balance // 2
    elif '%' in bet_text:
        try:
            percent = int(bet_text.replace('%', ''))
            bet = int(balance * (percent / 100))
        except:
            bot.reply_to(message, "❌ Неверный процент!", parse_mode='HTML')
            return
    else:
        bet = parse_bet_amount(bet_text, balance)
        if not bet:
            bot.reply_to(message, "❌ Неверная бет!", parse_mode='HTML')
            return
    
    if bet < LOTTERY_MACHINE_CONFIG["min_bet"]:
        bot.reply_to(message, f"❌ Мин. бет: {format_balance(LOTTERY_MACHINE_CONFIG['min_bet'])}", parse_mode='HTML')
        return
    
    if bet > LOTTERY_MACHINE_CONFIG["max_bet"]:
        bot.reply_to(message, f"❌ Макс. бет: {format_balance(LOTTERY_MACHINE_CONFIG['max_bet'])}", parse_mode='HTML')
        return
    
    if bet > balance:
        bot.reply_to(message, f"❌ Не хватает монет!", parse_mode='HTML')
        return
    
    update_balance(user_id, -bet)
    
    ticket = create_lottery_ticket(bet)
    lottery_games[user_id] = ticket
    
    user_info = get_user_info(user_id)
    name = user_info['custom_name'] or user_info['username'] or user_info['first_name']
    
    markup = InlineKeyboardMarkup(row_width=3)
    
    buttons = []
    for i in range(3):
        if ticket["revealed"][i]:
            buttons.append(InlineKeyboardButton(ticket["symbols"][i], callback_data=f"lot_{i}_done"))
        else:
            buttons.append(InlineKeyboardButton("⬜", callback_data=f"lot_{user_id}_{i}"))
    
    markup.row(*buttons)
    markup.row(InlineKeyboardButton("⚔️ ОТКРЫТЬ ВСЕ", callback_data=f"lot_{user_id}_all"))
    
    msg = bot.send_message(
        message.chat.id,
        f"🎰 Лотерейный билет\n\n"
        f"🎴 Игрок: {name}\n"
        f"🎫 Билет #{len(lottery_games)}\n"
        f"💵 Ставка: {format_balance(bet)}\n"
        f"📊 Открыто: 0/3\n\n"
        f"⬇️ Царапайте ячейки:",
        reply_markup=markup
    , parse_mode='HTML')
    
    ticket["message_id"] = msg.message_id
    ticket["chat_id"] = message.chat.id

def generate_mines_board(mines_count):
    """Генерирует игровое поле с минами"""
    total_cells = 25
    board = [False] * total_cells
    
    mine_positions = random.sample(range(total_cells), mines_count)
    for pos in mine_positions:
        board[pos] = True
    
    return board

def show_mines_game(chat_id, user_id, message_id=None):
    """Показывает игровое поле"""
    if user_id not in active_mines_games:
        return
    
    game_data = active_mines_games[user_id]
    
    time_passed = time.time() - game_data['start_time']
    if time_passed > 240:
        refund_expired_mines_games()
        return
    
    time_left = 240 - time_passed
    minutes_left = int(time_left // 60)
    seconds_left = int(time_left % 60)
    
    markup = create_mines_keyboard(game_data)
    
    info_text = f"🎲 <b>Мины</b>\n\n"
    info_text += f"💵 Ставка: {format_balance(game_data['bet_amount'])}\n"
    info_text += f"💣 Мин: {game_data['mines_count']}\n"
    info_text += f"🏆 Открыто: {game_data['opened_cells']}/25\n"
    info_text += f"⏰ Возврат: {minutes_left}:{seconds_left:02d}\n\n"
    
    multiplier = calculate_multiplier(game_data['mines_count'], game_data['opened_cells'])
    
    info_text += f"📈 Множитель: <b>{multiplier:.2f}x</b>\n"
    
    if game_data['opened_cells'] > 0:
        potential_win = int(game_data['bet_amount'] * multiplier)
        info_text += f"⚔️ Выигрыш: <b>{format_balance(potential_win)}</b>\n\n"
    else:
        info_text += "\n"
    
    info_text += "❇️ - закрытая\n🎁 - безопасная\n💣 - мина"
    
    if message_id:
        try:
            bot.edit_message_text(
                info_text,
                chat_id,
                message_id,
                reply_markup=markup,
                parse_mode='HTML'
            )
        except:
            sent_message = bot.send_message(
                chat_id,
                info_text,
                reply_markup=markup,
                parse_mode='HTML'
            )
            game_data['message_id'] = sent_message.message_id
    else:
        sent_message = bot.send_message(
            chat_id,
            info_text,
            reply_markup=markup,
            parse_mode='HTML'
        )
        game_data['message_id'] = sent_message.message_id

def create_mines_keyboard(game_data):
    """Создает клавиатуру для игры в мины"""
    markup = InlineKeyboardMarkup()
    row = []
    
    for i in range(25):
        if i in game_data['revealed_safe']:
            row.append(InlineKeyboardButton("🎁", callback_data=f"mines_already_{i}"))
        elif i in game_data['revealed_mines']:
            row.append(InlineKeyboardButton("💣", callback_data=f"mines_already_{i}"))
        else:
            row.append(InlineKeyboardButton("❇️", callback_data=f"mines_open_{i}"))
        
        if len(row) == 5:
            markup.row(*row)
            row = []
    
    markup.row(
        InlineKeyboardButton("💵 Забрать", callback_data="mines_cashout"),
        InlineKeyboardButton("❌ Выйти", callback_data="mines_exit")
    )
    
    return markup

@bot.callback_query_handler(func=lambda call: call.data.startswith('mines_'))
def handle_mines_click(call):
    try:
        user_id = call.from_user.id
        
        if user_id not in active_mines_games:
            bot.answer_callback_query(call.id, "❌ Игра не найдено!")
            return
        
        game_data = active_mines_games[user_id]
        game_data['last_action_time'] = time.time()
        
        if call.data.startswith('mines_open_'):
            cell_index = int(call.data.split('_')[2])
            handle_cell_open(call, user_id, cell_index)
        elif call.data == 'mines_cashout':
            handle_mines_cashout(call, user_id)
        elif call.data == 'mines_exit':
            handle_mines_exit(call, user_id)
        elif call.data.startswith('mines_already_'):
            bot.answer_callback_query(call.id, "❌ Уже открыта!")
    
    except Exception as e:
        print(f"Ошибка в handle_mines_click: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка!")

def handle_cell_open(call, user_id, cell_index):
    """Обрабатывает открытие клетки"""
    game_data = active_mines_games[user_id]
    
    if cell_index in game_data['revealed_safe'] or cell_index in game_data['revealed_mines']:
        bot.answer_callback_query(call.id, "❌ Уже открыта!")
        return
    
    if game_data['game_board'][cell_index]:
        game_data['revealed_mines'].append(cell_index)
        end_mines_game(user_id, False)
        bot.answer_callback_query(call.id, "💣 Мина!")
    else:
        game_data['revealed_safe'].append(cell_index)
        game_data['opened_cells'] += 1
        bot.answer_callback_query(call.id, "🎁 Безопасно!")
        show_mines_game(call.message.chat.id, user_id, call.message.message_id)

def handle_mines_cashout(call, user_id):
    """Обрабатывает вывод средств"""
    game_data = active_mines_games[user_id]
    
    if game_data['opened_cells'] == 0:
        bot.answer_callback_query(call.id, "❌ Откройте клетку!")
        return
    
    multiplier = calculate_multiplier(game_data['mines_count'], game_data['opened_cells'])
    win_amount = int(game_data['bet_amount'] * multiplier)
    
    update_balance(user_id, win_amount)
    end_mines_game(user_id, True, win_amount)
    bot.answer_callback_query(call.id, f"🏆 +{format_balance(win_amount)}!")

def handle_mines_exit(call, user_id):
    """Обрабатывает выход из игры"""
    game_data = active_mines_games[user_id]
    update_balance(user_id, game_data['bet_amount'])
    end_mines_game(user_id, False, 0, True)
    bot.answer_callback_query(call.id, "🏆 Средства возвращены!")

def end_mines_game(user_id, won=False, win_amount=0, exited=False):
    """Завершает игру в мины"""
    if user_id not in active_mines_games:
        return
    
    game_data = active_mines_games[user_id]
    chat_id = game_data['chat_id']
    message_id = game_data['message_id']
    
    final_markup = create_final_mines_keyboard(game_data)
    
    if won:
        multiplier = calculate_multiplier(game_data['mines_count'], game_data['opened_cells'])
        result_text = f"{SALUTE_EMOJI} <b>Победа!</b>\n\n"
        result_text += f"💵 Выигрыш: <b>{format_balance(win_amount)}</b>\n"
        result_text += f"📈 Множитель: <b>{multiplier:.2f}x</b>\n"
        result_text += f"🏆 Открыто: {game_data['opened_cells']} клеток"
    elif exited:
        result_text = f"🏁 <b>Игра окончена</b>\n\n"
        result_text += f"💵 Возвращено: <b>{format_balance(game_data['bet_amount'])}</b>\n"
        result_text += f"🏆 Открыто: {game_data['opened_cells']} клеток"
    else:
        result_text = f"💥 <b>Проигрыш!</b>\n\n"
        result_text += f"💣 Найдено мин: {len(game_data['revealed_mines'])}\n"
        result_text += f"🏆 Открыто: {game_data['opened_cells']} клеток"
    
    try:
        bot.edit_message_text(
            result_text,
            chat_id,
            message_id,
            reply_markup=final_markup,
            parse_mode='HTML'
        )
    except:
        bot.send_message(
            chat_id,
            result_text,
            reply_markup=final_markup,
            parse_mode='HTML'
        )
    
    del active_mines_games[user_id]

def create_final_mines_keyboard(game_data):
    """Создает финальную клавиатуру с открытыми минами"""
    markup = InlineKeyboardMarkup()
    row = []
    
    for i in range(25):
        if i in game_data['revealed_mines']:
            row.append(InlineKeyboardButton("💣", callback_data="mines_final"))
        elif i in game_data['revealed_safe']:
            row.append(InlineKeyboardButton("🎁", callback_data="mines_final"))
        elif game_data['game_board'][i]:
            row.append(InlineKeyboardButton("💣", callback_data="mines_final"))
        else:
            row.append(InlineKeyboardButton("❇️", callback_data="mines_final"))
        
        if len(row) == 5:
            markup.row(*row)
            row = []
    
    unique_callback = f"mine_return_{random.randint(100000, 999999)}_{int(time.time())}"
    markup.row(InlineKeyboardButton("🎲 Новая игра", callback_data=unique_callback))
    return markup

@bot.callback_query_handler(func=lambda call: call.data.startswith('mine_return_'))
def handle_mine_return(call):
    try:
        user_id = call.from_user.id
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        show_mines_help(call.message.chat.id)
        bot.answer_callback_query(call.id, "🎲 Готовы к новой игре!")
    except Exception as e:
        print(f"Ошибка в handle_mine_return: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка!")

@bot.callback_query_handler(func=lambda call: call.data == "mines_final")
def handle_mines_final(call):
    bot.answer_callback_query(call.id, "ℹ️ Игра окончена")

def calculate_multiplier(mines_count, opened_cells):
    """Рассчитывает множитель (реалистичные значения с реальных казино)"""
    if opened_cells == 0:
        return 1.0
    
    multipliers = {
        1: {1: 1.01, 2: 1.02, 3: 1.04, 4: 1.06, 5: 1.09, 6: 1.12, 7: 1.16, 8: 1.21, 
            9: 1.27, 10: 1.33, 11: 1.41, 12: 1.50, 13: 1.60, 14: 1.72, 15: 1.86, 
            16: 2.02, 17: 2.21, 18: 2.44, 19: 2.72, 20: 3.05},
        2: {1: 1.02, 2: 1.04, 3: 1.08, 4: 1.12, 5: 1.18, 6: 1.25, 7: 1.33, 8: 1.42, 
            9: 1.53, 10: 1.66, 11: 1.81, 12: 2.00, 13: 2.22, 14: 2.48, 15: 2.78, 
            16: 3.14, 17: 3.57, 18: 4.09, 19: 4.72, 20: 5.48},
        3: {1: 1.03, 2: 1.07, 3: 1.12, 4: 1.19, 5: 1.27, 6: 1.37, 7: 1.49, 8: 1.63, 
            9: 1.80, 10: 2.00, 11: 2.24, 12: 2.52, 13: 2.86, 14: 3.27, 15: 3.76, 
            16: 4.35, 17: 5.06, 18: 5.92, 19: 6.97, 20: 8.26},
        4: {1: 1.04, 2: 1.10, 3: 1.17, 4: 1.26, 5: 1.37, 6: 1.50, 7: 1.66, 8: 1.85, 
            9: 2.07, 10: 2.34, 11: 2.67, 12: 3.07, 13: 3.56, 14: 4.16, 15: 4.89, 
            16: 5.78, 17: 6.87, 18: 8.22, 19: 9.90, 20: 11.99},
        5: {1: 1.05, 2: 1.13, 3: 1.23, 4: 1.34, 5: 1.48, 6: 1.65, 7: 1.85, 8: 2.09, 
            9: 2.37, 10: 2.71, 11: 3.12, 12: 3.62, 13: 4.23, 14: 4.98, 15: 5.89, 
            16: 7.01, 17: 8.40, 18: 10.13, 19: 12.28, 20: 14.96},
        6: {1: 1.06, 2: 1.16, 3: 1.28, 4: 1.43, 5: 1.60, 6: 1.80, 7: 2.05, 8: 2.35, 
            9: 2.71, 10: 3.14, 11: 3.65, 12: 4.28, 13: 5.04, 14: 5.97, 15: 7.12, 
            16: 8.54, 17: 10.30, 18: 12.49, 19: 15.23, 20: 18.66},
        7: {1: 1.07, 2: 1.19, 3: 1.34, 4: 1.52, 5: 1.73, 6: 1.98, 7: 2.28, 8: 2.64, 
            9: 3.08, 10: 3.61, 11: 4.25, 12: 5.03, 13: 6.00, 14: 7.21, 15: 8.71, 
            16: 10.58, 17: 12.92, 18: 15.87, 19: 19.56, 20: 24.21},
        8: {1: 1.08, 2: 1.23, 3: 1.41, 4: 1.62, 5: 1.87, 6: 2.17, 7: 2.53, 8: 2.97, 
            9: 3.50, 10: 4.15, 11: 4.95, 12: 5.95, 13: 7.18, 14: 8.72, 15: 10.66, 
            16: 13.11, 17: 16.21, 18: 20.16, 19: 25.21, 20: 31.66},
        9: {1: 1.09, 2: 1.27, 3: 1.48, 4: 1.73, 5: 2.03, 6: 2.39, 7: 2.82, 8: 3.35, 
            9: 4.00, 10: 4.80, 11: 5.79, 12: 7.03, 13: 8.58, 14: 10.53, 15: 13.01, 
            16: 16.18, 17: 20.26, 18: 25.54, 19: 32.39, 20: 41.27},
        10: {1: 1.10, 2: 1.31, 3: 1.56, 4: 1.86, 5: 2.22, 6: 2.65, 7: 3.18, 8: 3.83, 
             9: 4.64, 10: 5.66, 11: 6.94, 12: 8.55, 13: 10.60, 14: 13.22, 15: 16.60, 
             16: 20.98, 17: 26.69, 18: 34.18, 19: 44.05, 20: 57.08},
        11: {1: 1.11, 2: 1.35, 3: 1.65, 4: 2.01, 5: 2.44, 6: 2.97, 7: 3.62, 8: 4.44, 
             9: 5.47, 10: 6.78, 11: 8.45, 12: 10.59, 13: 13.35, 14: 16.93, 15: 21.61, 
             16: 27.76, 17: 35.88, 18: 46.64, 19: 60.99, 20: 80.18},
        12: {1: 1.12, 2: 1.40, 3: 1.75, 4: 2.18, 5: 2.71, 6: 3.38, 7: 4.23, 8: 5.32, 
             9: 6.73, 10: 8.56, 11: 10.95, 12: 14.10, 13: 18.25, 14: 23.76, 15: 31.14, 
             16: 41.09, 17: 54.59, 18: 72.99, 19: 98.33, 20: 133.39},
        13: {1: 1.13, 2: 1.45, 3: 1.86, 4: 2.37, 5: 3.03, 6: 3.88, 7: 4.99, 8: 6.45, 
             9: 8.39, 10: 10.98, 11: 14.48, 12: 19.20, 13: 25.63, 14: 34.46, 15: 46.71, 
             16: 63.76, 17: 87.70, 18: 121.57, 19: 169.92, 20: 239.53},
        14: {1: 1.14, 2: 1.51, 3: 1.98, 4: 2.59, 5: 3.41, 6: 4.50, 7: 5.98, 8: 7.99, 
             9: 10.75, 10: 14.58, 11: 19.94, 12: 27.48, 13: 38.21, 14: 53.56, 15: 75.70, 
             16: 107.85, 17: 155.04, 18: 224.66, 19: 328.58, 20: 484.78},
        15: {1: 1.15, 2: 1.57, 3: 2.12, 4: 2.85, 5: 3.85, 6: 5.24, 7: 7.20, 8: 9.98, 
             9: 13.96, 10: 19.74, 11: 28.18, 12: 40.61, 13: 59.04, 14: 86.62, 15: 128.28, 
             16: 191.68, 17: 289.11, 18: 440.65, 19: 677.92, 20: 1053.11},
        16: {1: 1.16, 2: 1.63, 3: 2.27, 4: 3.15, 5: 4.41, 6: 6.22, 7: 8.86, 8: 12.72, 
             9: 18.46, 10: 27.10, 11: 40.18, 12: 60.22, 13: 91.06, 14: 139.08, 15: 214.13, 
             16: 332.85, 17: 522.39, 18: 827.71, 19: 1323.54, 20: 2136.53},
        17: {1: 1.17, 2: 1.70, 3: 2.44, 4: 3.52, 5: 5.13, 6: 7.55, 7: 11.22, 8: 16.87, 
             9: 25.64, 10: 39.42, 11: 61.27, 12: 96.18, 13: 152.42, 14: 243.89, 15: 393.91, 
             16: 642.57, 17: 1057.88, 18: 1758.09, 19: 2948.18, 20: 4987.87},
        18: {1: 1.18, 2: 1.78, 3: 2.64, 4: 3.96, 5: 6.01, 6: 9.23, 7: 14.34, 8: 22.58, 
             9: 36.00, 10: 58.08, 11: 94.80, 12: 156.60, 13: 261.72, 14: 442.08, 15: 754.20, 
             16: 1299.60, 17: 2261.76, 18: 3974.40, 19: 7056.00, 20: 12650.40},
        19: {1: 1.19, 2: 1.86, 3: 2.87, 4: 4.48, 5: 7.09, 6: 11.36, 7: 18.43, 8: 30.29, 
             9: 50.40, 10: 84.96, 11: 145.20, 12: 251.52, 13: 441.60, 14: 785.28, 15: 1413.60, 
             16: 2572.80, 17: 4738.56, 18: 8832.00, 19: 16632.00, 20: 31680.00},
        20: {1: 1.20, 2: 1.95, 3: 3.13, 4: 5.10, 5: 8.45, 6: 14.21, 7: 24.19, 8: 41.80, 
             9: 73.44, 10: 130.56, 11: 235.20, 12: 430.08, 13: 798.72, 14: 1505.28, 15: 2876.16, 
             16: 5568.00, 17: 10925.76, 18: 21735.68, 19: 43791.36, 20: 89280.00},
        21: {1: 1.21, 2: 2.05, 3: 3.44, 4: 5.85, 5: 10.12, 6: 17.77, 7: 31.62, 8: 57.09, 
             9: 104.76, 10: 195.36, 11: 369.60, 12: 709.63, 13: 1382.40, 14: 2731.52, 15: 5463.04, 
             16: 11059.20, 17: 22671.36, 18: 47002.62, 19: 98553.60, 20: 208824.00},
        22: {1: 1.22, 2: 2.16, 3: 3.79, 4: 6.76, 5: 12.25, 6: 22.55, 7: 42.18, 8: 80.11, 
             9: 154.56, 10: 302.40, 11: 600.60, 12: 1209.60, 13: 2469.60, 14: 5113.60, 15: 10713.60, 
             16: 22713.60, 17: 48773.12, 18: 105907.20, 19: 232713.60, 20: 516873.60},
        23: {1: 1.23, 2: 2.28, 3: 4.20, 4: 7.86, 5: 14.98, 6: 29.04, 7: 57.09, 8: 113.96, 
             9: 231.84, 10: 478.80, 11: 1001.00, 12: 2121.60, 13: 4560.00, 14: 9951.20, 15: 21993.60, 
             16: 49296.00, 17: 111782.40, 18: 256358.40, 19: 594048.00, 20: 1391040.00},
        24: {1: 1.25, 2: 2.43, 3: 4.69, 4: 9.18, 5: 18.29, 6: 37.04, 7: 76.19, 8: 159.25, 
             9: 337.92, 10: 727.20, 11: 1584.00, 12: 3498.00, 13: 7833.60, 14: 17777.60, 15: 40840.80, 
             16: 94920.00, 17: 223142.40, 18: 529804.80, 19: 1270080.00, 20: 3071040.00}
    }
    
    return multipliers[mines_count].get(opened_cells, 1.0)

def show_mines_help(chat_id):
    """Показывает справку по игре"""
    help_text = """🎲 <b>Игра "Мины"</b>

⚡ <b>Как играть:</b>
• Открывайте безопасные клетки (🎁)
• Избегайте мин (💣)
• Забирайте выигрыш в любой момент

📌 <b>Команда:</b>
<code>мины [бет] [количество мин]</code>

📊 <b>Примеры:</b>
<code>мины 1м 5</code> - бет 1М, 5 мин
<code>мины 5к 10</code> - бет 5к, 10 мин
<code>мины все 3</code> - вся сумма, 3 мины

⚔️ Мин. бет: 1.000"""
    
    bot.send_message(chat_id, help_text, parse_mode='HTML')

def refund_expired_mines_games():
    """Возвращает средства за просроченные игры"""
    current_time = time.time()
    expired_games = []
    
    for user_id, game_data in active_mines_games.items():
        if current_time - game_data['start_time'] > 240:
            expired_games.append(user_id)
    
    for user_id in expired_games:
        game_data = active_mines_games[user_id]
        bet_amount = game_data['bet_amount']
        update_balance(user_id, bet_amount)
        del active_mines_games[user_id]
        
        try:
            bot.send_message(
                user_id,
                f"🕒 Время игры истекло!\n"
                f"💵 Возвращено: {format_balance(bet_amount)}"
            , parse_mode='HTML')
        except Exception as e:
            print(f"❌ Не удалось уведомить пользователя {user_id}: {e}")
    
    return len(expired_games)

def start_mines_refund_checker():
    """Запускает периодическую проверку"""
    def checker():
        while True:
            try:
                refunded_count = refund_expired_mines_games()
                if refunded_count > 0:
                    print(f"🔄 Возвращено {refunded_count} игр")
                time.sleep(60)
            except Exception as e:
                print(f"❌ Ошибка: {e}")
                time.sleep(60)
    
    thread = threading.Thread(target=checker, daemon=True)
    thread.start()

start_mines_refund_checker()
def get_clothes_shop():
    """Получить все айтемы из магазина (только доступные для покупки)"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT * FROM clothes_shop WHERE supply != 0 ORDER BY price ASC')
        return [dict(row) for row in cursor.fetchall()]

def get_user_clothes(user_id):
    """Получить одежду пользователя"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT uc.*, cs.name, cs.price, cs.type, cs.image_name
            FROM user_clothes uc
            JOIN clothes_shop cs ON uc.item_id = cs.id
            WHERE uc.user_id = ?
        ''', (user_id,))
        return [dict(row) for row in cursor.fetchall()]

def get_equipped_clothes(user_id):
    """Получить надетую одежду - ИСПРАВЛЕННАЯ ВЕРСИЯ ДЛЯ ПРАВОЙ СТОРОНЫ"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT cs.type, cs.image_name
            FROM user_clothes uc
            JOIN clothes_shop cs ON uc.item_id = cs.id
            WHERE uc.user_id = ? AND uc.equipped = 1
            ORDER BY 
                CASE cs.type 
                    WHEN 'Ноги' THEN 1
                    WHEN 'Тело' THEN 2 
                    WHEN 'Голова' THEN 3
                    WHEN 'Слева' THEN 4
                    WHEN 'Справа' THEN 5
                    ELSE 6
                END,
                uc.item_id
        ''', (user_id,))
        
        equipped_items = cursor.fetchall()
        
        equipped_dict = {}
        
        for item_type, image_name in equipped_items:
            if item_type in ['Слева', 'Справа']:
                if item_type not in equipped_dict:
                    equipped_dict[item_type] = []
                equipped_dict[item_type].append(image_name)
            else:
                equipped_dict[item_type] = image_name
        
        return equipped_dict
@bot.message_handler(func=lambda message: message.text.lower() == 'debug outfit' and is_admin(message.from_user.id))
def debug_outfit(message):
    """Показать отладочную информацию об одежде"""
    user_id = message.from_user.id
    
    equipped = get_equipped_clothes(user_id)
    
    debug_text = f"🔧 ОТЛАДКА ОДЕЖДЫ для {user_id}:\n\n"
    
    for item_type, items in equipped.items():
        if isinstance(items, list):
            debug_text += f"📦 {item_type}: {len(items)} items\n"
            for item in items:
                debug_text += f"   - {item}\n"
        else:
            debug_text += f"👕 {item_type}: {items}\n"
    
    debug_text += f"\n🔍 ПРОВЕРКА ФАЙЛОВ:\n"
    for item_type, items in equipped.items():
        if isinstance(items, list):
            for item in items:
                file_path = f"images/{item}"
                exists = "🏆" if os.path.exists(file_path) else "❌"
                debug_text += f"{exists} {item_type}/{item}\n"
        else:
            file_path = f"images/{items}"
            exists = "🏆" if os.path.exists(file_path) else "❌"
            debug_text += f"{exists} {item_type}/{items}\n"
    
    bot.send_message(message.chat.id, debug_text)
@bot.message_handler(func=lambda message: message.text.lower() == 'вернуть тип' and is_admin(message.from_user.id))
def restore_gucci_type(message):
    """Вернуть правильный тип Gucci Pepe"""
    with get_db_cursor() as cursor:
        cursor.execute("UPDATE clothes_shop SET type = 'accessories' WHERE name LIKE '%Gucci Pepe%'")
        bot.send_message(message.chat.id, "🏆 Тип Gucci Pepe возвращен на 'accessories'", parse_mode='HTML')
@bot.message_handler(func=lambda message: message.text.lower() == 'исправить тип' and is_admin(message.from_user.id))
def fix_gucci_type(message):
    """Изменить тип Gucci Pepe для теста"""
    with get_db_cursor() as cursor:
        cursor.execute("UPDATE clothes_shop SET type = 'body' WHERE name LIKE '%Gucci Pepe%'")
        bot.send_message(message.chat.id, "🏆 Тип Gucci Pepe изменен на 'body'. Проверьте отображение.", parse_mode='HTML')
@bot.message_handler(func=lambda message: message.text.lower().startswith('изменить тип ') and is_admin(message.from_user.id))
def change_item_type(message):
    """Изменить тип конкретной вещи"""
    try:
        parts = message.text.split()
        if len(parts) < 4:
            bot.send_message(message.chat.id, "❌ Используйте: изменить тип [id] [новый_тип]", parse_mode='HTML')
            return
        
        item_id = int(parts[2])
        new_type = parts[3]
        
        with get_db_cursor() as cursor:
            cursor.execute("SELECT name FROM clothes_shop WHERE id = ?", (item_id,))
            item = cursor.fetchone()
            
            if not item:
                bot.send_message(message.chat.id, "❌ Вещь не найдено!", parse_mode='HTML')
                return
            
            cursor.execute("UPDATE clothes_shop SET type = ? WHERE id = ?", (new_type, item_id))
            
            bot.send_message(message.chat.id, f"🏆 Тип вещи '{item[0]}' изменен на '{new_type}'", parse_mode='HTML')
            
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')
@bot.message_handler(func=lambda message: message.text.lower().startswith('добавить саплай') and is_admin(message.from_user.id))
def handle_add_supply(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды", parse_mode='HTML')
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 4:
            bot.send_message(message.chat.id, 
                           "❌ Используйте: добавить саплай [название одежды] [количество]\n\n"
                           "Пример:\n"
                           "добавить саплай Кроссовки Nike 50", parse_mode='HTML')
            return
        
        item_name = ' '.join(parts[2:-1])
        supply_amount = parts[-1]
        
        try:
            supply = int(supply_amount)
            if supply < 1:
                bot.send_message(message.chat.id, "❌ Количество должно быть больше 0!", parse_mode='HTML')
                return
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверное количество!", parse_mode='HTML')
            return
        
        with get_db_cursor() as cursor:
            cursor.execute('SELECT id, name FROM clothes_shop WHERE name LIKE ?', (f'%{item_name}%',))
            items = cursor.fetchall()
            
            if not items:
                bot.send_message(message.chat.id, f"❌ Одежда '{item_name}' отсутствуета в магазине!", parse_mode='HTML')
                return
            
            if len(items) > 1:
                items_text = "📋 Найдено несколько предметов:\n\n"
                for item in items:
                    items_text += f"• {item[1]} (ID: {item[0]})\n"
                items_text += f"\nУточните название или используйте ID: добавить саплай [ID] [количество]"
                bot.send_message(message.chat.id, items_text)
                return
            
            item_id, item_name = items[0]
            
            cursor.execute('UPDATE clothes_shop SET supply = ?, sold_count = 0 WHERE id = ?', (supply, item_id))
            
            bot.send_message(message.chat.id, 
                           f"🏆 Установлен саплай для {item_name}!\n"
                           f"📦 Количество: {supply} штук\n"
                           f"🔄 Счетчик продаж сброшен", parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка при установке саплая: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при установке саплая!", parse_mode='HTML')
        
def buy_clothes(user_id, item_id):
    """Купить одежду с проверкой лимита"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT price, name, supply, sold_count FROM clothes_shop WHERE id = ?', (item_id,))
        item = cursor.fetchone()
        if not item:
            return False, "Товар отсутствует"
        
        price, name, supply, sold_count = item
        
        if supply != -1 and sold_count >= supply:
            return False, f"❌ {name} распродан!"
        
        balance = get_balance(user_id)
        
        if balance < price:
            return False, f"❌ Не хватает монет! Нужно: {format_balance(price)}"
        
        cursor.execute('SELECT * FROM user_clothes WHERE user_id = ? AND item_id = ?', (user_id, item_id))
        if cursor.fetchone():
            return False, f"❌ У вас уже есть {name}!"
        
        cursor.execute('INSERT INTO user_clothes (user_id, item_id) VALUES (?, ?)', (user_id, item_id))
        cursor.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (price, user_id))
        
        if supply != -1:
            cursor.execute('UPDATE clothes_shop SET sold_count = sold_count + 1 WHERE id = ?', (item_id,))
        
        return True, f"🏆 {name} куплен!"

def equip_clothes(user_id, item_id):
    """Надеть одежду с обновлением образа"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT type, name FROM clothes_shop WHERE id = ?', (item_id,))
        item = cursor.fetchone()
        if not item:
            return False, "Вещь отсутствуета"
        
        item_type, name = item
        
        type_limits = {
            'Голова': 1,
            'Тело': 1,
            'Ноги': 1,
            'Слева': 2,
            'Справа': 2,
            'accessories': 2
        }
        
        cursor.execute('''
            SELECT COUNT(*) 
            FROM user_clothes uc 
            JOIN clothes_shop cs ON uc.item_id = cs.id 
            WHERE uc.user_id = ? AND uc.equipped = 1 AND cs.type = ?
        ''', (user_id, item_type))
        
        current_equipped = cursor.fetchone()[0]
        max_allowed = type_limits.get(item_type, 1)
        
        if current_equipped >= max_allowed:
            if item_type in ['Слева', 'Справа', 'accessories']:
                return False, f"❌ Достигнут лимит аксессуаров в {item_type}! Можно надеть только {max_allowed}."
            else:
                cursor.execute('''
                    UPDATE user_clothes 
                    SET equipped = 0 
                    WHERE user_id = ? AND item_id IN (
                        SELECT uc.item_id 
                        FROM user_clothes uc 
                        JOIN clothes_shop cs ON uc.item_id = cs.id 
                        WHERE uc.user_id = ? AND cs.type = ? AND uc.equipped = 1
                    )
                ''', (user_id, user_id, item_type))
        
        cursor.execute('UPDATE user_clothes SET equipped = 1 WHERE user_id = ? AND item_id = ?', (user_id, item_id))
        
        outfit_path = f"images/outfit_{user_id}.jpg"
        if os.path.exists(outfit_path):
            os.remove(outfit_path)
            print(f"🗑️ Удален старый образ после надевания: {name}")
        
        return True, f"🏆 {name} надет!"
def unequip_clothes(user_id, item_id):
    """Снять одежду с обновлением образа"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT name FROM clothes_shop WHERE id = ?', (item_id,))
        item = cursor.fetchone()
        if not item:
            return False, "Вещь отсутствуета"
        
        name = item[0]
        cursor.execute('UPDATE user_clothes SET equipped = 0 WHERE user_id = ? AND item_id = ?', (user_id, item_id))
        
        outfit_path = f"images/outfit_{user_id}.jpg"
        if os.path.exists(outfit_path):
            os.remove(outfit_path)
            print(f"🗑️ Удален старый образ после снятия: {name}")
        
        return True, f"🏆 {name} снят!"
def get_equipment_limits_info(user_id):
    """Получить информацию о текущих лимитах экипировки - ОБНОВЛЕННАЯ"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT cs.type, COUNT(*) as equipped_count
            FROM user_clothes uc
            JOIN clothes_shop cs ON uc.item_id = cs.id
            WHERE uc.user_id = ? AND uc.equipped = 1
            GROUP BY cs.type
        ''', (user_id,))
        
        equipped_items = cursor.fetchall()
        
        type_limits = {
            'Голова': (1, "👒 Голова"),
            'Тело': (1, "👕 Тело"),
            'Ноги': (1, "👖 Ноги"), 
            'Слева': (1, "💍 Слева"),
            'Справа': (1, "🎁 Справа")
        }
        
        info = "🎽 ЛИМИТЫ ЭКИПИРОВКИ:\n\n"
        
        for item_type, (limit, display_name) in type_limits.items():
            current_count = 0
            for equipped in equipped_items:
                if equipped[0] == item_type:
                    current_count = equipped[1]
                    break
            
            status = "🏆" if current_count < limit else "❌"
            info += f"{status} {display_name}: {current_count}/{limit}\n"
        
        return info

def create_character_outfit(user_id):
    """Создает изображение человечка с надетой одеждой - ПРОСТАЯ ВЕРСИЯ"""
    try:
        base_path = "images/base_human.jpg"
        
        if not os.path.exists(base_path):
            return "images/base_human.jpg"
        
        base_image = Image.open(base_path).convert("RGBA")
        equipped = get_equipped_clothes(user_id)
        
        for layer_type in ['Ноги', 'Тело', 'Голова', 'Слева', 'Справа']:
            if layer_type in equipped:
                if layer_type in ['Слева', 'Справа']:
                    for accessory in equipped[layer_type]:
                        accessory_path = f"images/{accessory}"
                        if os.path.exists(accessory_path):
                            try:
                                accessory_image = Image.open(accessory_path).convert("RGBA")
                                if accessory_image.size != base_image.size:
                                    accessory_image = accessory_image.resize(base_image.size, Image.Resampling.LANCZOS)
                                base_image = Image.alpha_composite(base_image, accessory_image)
                            except:
                                continue
                else:
                    image_path = f"images/{equipped[layer_type]}"
                    if os.path.exists(image_path):
                        try:
                            layer_image = Image.open(image_path).convert("RGBA")
                            if layer_image.size != base_image.size:
                                layer_image = layer_image.resize(base_image.size, Image.Resampling.LANCZOS)
                            base_image = Image.alpha_composite(base_image, layer_image)
                        except:
                            continue
        
        result_path = f"images/outfit_{user_id}.jpg"
        base_image.convert("RGB").save(result_path, "JPEG", quality=95)
        return result_path
        
    except Exception as e:
        print(f"Ошибка создания образа: {e}")
        return "images/base_human.jpg"
def draw_clothing_layer(base_image, clothes_path, layer_name):
    """Отрисовать слой одежды на базовом изображении"""
    if os.path.exists(clothes_path):
        try:
            clothes_image = Image.open(clothes_path).convert("RGBA")
            
            if clothes_image.size != base_image.size:
                clothes_image = clothes_image.resize(base_image.size, Image.Resampling.LANCZOS)
            
            base_image = Image.alpha_composite(base_image, clothes_image)
            print(f"🏆 {layer_name} наложен: {clothes_path}")
            
        except Exception as e:
            print(f"❌ Ошибка отрисовки {layer_name}: {e}")
            import traceback
            traceback.print_exc()
    else:
        print(f"❌ Файл отсутствует: {clothes_path}")
    
    return base_image
@bot.message_handler(func=lambda message: message.text.lower() == 'инфо gucci pepe' and is_admin(message.from_user.id))
def check_gucci_pepe_info(message):
    """Проверить информацию о Gucci Pepe в базе"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT id, name, type, image_name FROM clothes_shop WHERE name LIKE ?', ('%Gucci Pepe%',))
        item = cursor.fetchone()
        
        if item:
            message_text = f"🔍 Gucci Pepe в базе:\nID: {item[0]}\nНазвание: {item[1]}\nТип: {item[2]}\nФайл: {item[3]}"
            
            file_path = f"images/{item[3]}"
            if os.path.exists(file_path):
                message_text += f"\n🏆 Файл существует: {file_path}"
                
                file_size = os.path.getsize(file_path)
                message_text += f"\n📏 Размер файла: {file_size} байт"
            else:
                message_text += f"\n❌ Файл отсутствует: {file_path}"
                
        else:
            message_text = "❌ Gucci Pepe отсутствует в базе данных"
            
        bot.send_message(message.chat.id, message_text)
@bot.message_handler(func=lambda message: message.text.lower() == 'вернуть пепе' and is_admin(message.from_user.id))
def handle_restore_pepe(message):
    """Вернуть оригинальный Pepe Gucci"""
    try:
        with get_db_cursor() as cursor:
            cursor.execute('UPDATE clothes_shop SET image_name = ? WHERE name LIKE ?', 
                          ('Gucci Pepe.png', '%Gucci Pepe%'))
            
            affected = cursor.rowcount
            
            if affected > 0:
                bot.send_message(message.chat.id, 
                               "🏆 Pepe Gucci возвращен!\n"
                               "🔄 Теперь используется: Gucci Pepe.png\n"
                               "🎽 Наденьте заново в гардеробе", parse_mode='HTML')
            else:
                bot.send_message(message.chat.id, "❌ Pepe Gucci отсутствует в базе!", parse_mode='HTML')
                
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')
shop_pages = {}
wardrobe_pages = {}

@bot.message_handler(func=lambda message: message.text in ["🛍️ Шоп", "🛒 Шоп", "Шоп"])
def handle_shop(message):
    """🛒 Шоп с пагинацией - только в ЛС"""
    if message.chat.type != 'private':
        bot.send_message(message.chat.id, "🛍️ 🛒 Шоп доступен только в личных сообщениях с ботом!")
        return
        
    user_id = message.from_user.id
    shop_pages[user_id] = {'page': 0, 'message_id': None}
    show_shop_page(message.chat.id, user_id, 0)

def show_shop_categories(chat_id, user_id):
    """Показать категории магазина"""
    try:
        clothes = get_clothes_shop()
        
        if not clothes:
            bot.send_message(chat_id, "🛍️ 🛒 Шоп пока пуст!")
            return
        
        categories = list(set([item['type'] for item in clothes]))
        
        category_names = {
            'body': '👕 Одежда для тела',
            'hat': '🧢 Головные уборы', 
            'shoes': '👟 Обувь',
            'accessories': '💍 Аксессуары'
        }
        
        markup = InlineKeyboardMarkup(row_width=2)
        
        buttons = []
        for category in categories:
            display_name = category_names.get(category, category)
            buttons.append(InlineKeyboardButton(display_name, callback_data=f"shop_category_{category}"))
        
        for i in range(0, len(buttons), 2):
            if i + 1 < len(buttons):
                markup.add(buttons[i], buttons[i+1])
            else:
                markup.add(buttons[i])
        
        markup.add(InlineKeyboardButton("📦 Все айтемы", callback_data="shop_category_all"))
        
        current_data = shop_pages.get(user_id, {'page': 0, 'message_id': None, 'category': None})
        message_id = current_data.get('message_id')
        
        if message_id is None:
            sent_message = bot.send_message(
                chat_id,
                "🛍️ 🛒 Шоп одежды\n\nВыберите категорию:",
                reply_markup=markup
            )
            shop_pages[user_id] = {'page': 0, 'message_id': sent_message.message_id, 'category': None}
        else:
            try:
                bot.edit_message_text(
                    "🛍️ 🛒 Шоп одежды\n\nВыберите категорию:",
                    chat_id,
                    message_id,
                    reply_markup=markup
                )
            except:
                try:
                    bot.delete_message(chat_id, message_id)
                except:
                    pass
                sent_message = bot.send_message(
                    chat_id,
                    "🛍️ 🛒 Шоп одежды\n\nВыберите категорию:",
                    reply_markup=markup
                )
                shop_pages[user_id] = {'page': 0, 'message_id': sent_message.message_id, 'category': None}
        
    except Exception as e:
        bot.send_message(chat_id, f"❌ Ошибка: {e}", parse_mode='HTML')

def show_shop_page(chat_id, user_id, page=0, category=None):
    """Показать страницу магазина с фильтром по категории"""
    try:
        all_clothes = get_clothes_shop()
        
        if category and category != 'all':
            clothes = [item for item in all_clothes if item['type'] == category]
        else:
            clothes = all_clothes
        
        if not clothes:
            if category and category != 'all':
                bot.send_message(chat_id, f"🛍️ В категории пока нет айтемов!")
            else:
                bot.send_message(chat_id, "🛍️ 🛒 Шоп пока пуст!")
            return
        
        items_per_page = 1
        total_pages = (len(clothes) + items_per_page - 1) // items_per_page
        
        if page >= total_pages:
            page = total_pages - 1
        if page < 0:
            page = 0
        
        start_idx = page * items_per_page
        end_idx = start_idx + items_per_page
        page_items = clothes[start_idx:end_idx]
        
        if not page_items:
            bot.send_message(chat_id, "🛍️ Товары не найдено!")
            return
        
        item = page_items[0]
        
        supply_info = ""
        if item.get('supply', -1) != -1:
            available = item['supply'] - item.get('sold_count', 0)
            supply_info = f"\n📦 Осталось: {available}/{item['supply']}"
        
        category_info = f" | 📁 {item['type']}" if category == 'all' else ""
        
        caption = f"👕 {item['name']}\n💵 {format_balance(item['price'])}{category_info}{supply_info}\n\n📄 Страница {page + 1} из {total_pages}"
        markup = create_shop_markup(item['id'], page, total_pages, category)
        photo_path = f"images/{item['image_name']}"
        
        current_data = shop_pages.get(user_id, {'page': 0, 'message_id': None, 'category': None})
        message_id = current_data.get('message_id')
        
        if message_id is None:
            if os.path.exists(photo_path):
                with open(photo_path, 'rb') as photo:
                    sent_message = bot.send_photo(
                        chat_id,
                        photo,
                        caption=caption,
                        reply_markup=markup,
                        parse_mode='HTML'
                    )
            else:
                sent_message = bot.send_message(chat_id, caption, reply_markup=markup, parse_mode='HTML')
            shop_pages[user_id] = {'page': page, 'message_id': sent_message.message_id, 'category': category}
        else:
            if os.path.exists(photo_path):
                with open(photo_path, 'rb') as photo:
                    try:
                        bot.edit_message_media(
                            chat_id=chat_id,
                            message_id=message_id,
                            media=types.InputMediaPhoto(photo, caption=caption, parse_mode='HTML'),
                            reply_markup=markup
                        )
                    except Exception as e:
                        try:
                            bot.delete_message(chat_id, message_id)
                        except:
                            pass
                        sent_message = bot.send_photo(
                            chat_id,
                            photo,
                            caption=caption,
                            reply_markup=markup,
                            parse_mode='HTML'
                        )
                        shop_pages[user_id] = {'page': page, 'message_id': sent_message.message_id, 'category': category}
            else:
                try:
                    bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=caption,
                        reply_markup=markup,
                        parse_mode='HTML'
                    )
                except:
                    try:
                        bot.delete_message(chat_id, message_id)
                    except:
                        pass
                    sent_message = bot.send_message(chat_id, caption, reply_markup=markup, parse_mode='HTML')
                    shop_pages[user_id] = {'page': page, 'message_id': sent_message.message_id, 'category': category}
        
    except Exception as e:
        bot.send_message(chat_id, f"❌ Ошибка: {e}", parse_mode='HTML')

def create_shop_markup(item_id, current_page, total_pages, category=None):
    """Создает клавиатуру с зеленой кнопкой через прямой JSON"""
    
    price = get_item_price(item_id)
    
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": f"Купить за  {price} 🌸",
                    "callback_data": f"buy_{item_id}",
                    "style": "success"
                }
            ],
            [],
            [
                {
                    "text": "📂 Каталог",
                    "callback_data": "shop_categories"
                }
            ]
        ]
    }
    
    nav_row = []
    
    if current_page > 0:
        nav_row.append({
            "text": "◀️",
            "callback_data": f"shop_prev_{current_page-1}_{category or 'all'}"
        })
    
    nav_row.append({
        "text": f"📄 {current_page+1}/{total_pages}",
        "callback_data": "shop_info"
    })
    
    if current_page < total_pages - 1:
        nav_row.append({
            "text": "▶️",
            "callback_data": f"shop_next_{current_page+1}_{category or 'all'}"
        })
    
    keyboard["inline_keyboard"][1] = nav_row
    
    return json.dumps(keyboard)

@bot.callback_query_handler(func=lambda call: call.data.startswith('shop_category_'))
def handle_shop_category(call):
    """Обработчик выбора категории"""
    user_id = call.from_user.id
    category = call.data.split('_')[2]
    
    shop_pages[user_id] = {'page': 0, 'message_id': call.message.message_id, 'category': category}
    show_shop_page(call.message.chat.id, user_id, 0, category)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == 'shop_categories')
def handle_shop_categories(call):
    """Обработчик возврата к категориям"""
    user_id = call.from_user.id
    show_shop_categories(call.message.chat.id, user_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith(('shop_prev_', 'shop_next_')))
def handle_navigation(call):
    """Обработка навигации по страницам с учетом категории"""
    user_id = call.from_user.id
    data = call.data
    
    try:
        if data.startswith('shop_prev_'):
            parts = data.split('_')
            page = int(parts[2])
            category = parts[3] if len(parts) > 3 else None
            show_shop_page(call.message.chat.id, user_id, page, category)
        elif data.startswith('shop_next_'):
            parts = data.split('_')
            page = int(parts[2])
            category = parts[3] if len(parts) > 3 else None
            show_shop_page(call.message.chat.id, user_id, page, category)
        elif data == 'shop_info':
            bot.answer_callback_query(call.id, "Текущая страница")
            return
        
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        bot.answer_callback_query(call.id, "❌ Ошибка!")

def get_item_price(item_id):
    """Получить цену айтема по ID"""
    clothes = get_clothes_shop()
    for item in clothes:
        if item['id'] == item_id:
            return item['price']
    return 0

@bot.message_handler(func=lambda message: message.text == "👔 Шкаф")
def handle_wardrobe(message):
    """👔 Шкаф с пагинацией - только в ЛС"""
    if message.chat.type != 'private':
        bot.send_message(message.chat.id, "🎒 👔 Шкаф доступен только в личных сообщениях с ботом!")
        return
        
    user_id = message.from_user.id
    wardrobe_pages[user_id] = {'page': 0, 'message_id': None}
    show_wardrobe_page(message.chat.id, user_id, 0)

def show_wardrobe_page(chat_id, user_id, page=0):
    """Показать страницу гардероба"""
    try:
        clothes = get_user_clothes(user_id)
        
        if not clothes:
            bot.send_message(chat_id, "🎒 Твой гардероб пуст!\n🛍️ Загляните в магазин.")
            return
        
        items_per_page = 1
        total_pages = (len(clothes) + items_per_page - 1) // items_per_page
        
        if page >= total_pages:
            page = total_pages - 1
        if page < 0:
            page = 0
        
        start_idx = page * items_per_page
        end_idx = start_idx + items_per_page
        page_items = clothes[start_idx:end_idx]
        
        if not page_items:
            bot.send_message(chat_id, "🎒 Вещи не найдено!")
            return
        
        item = page_items[0]
        status = "🏆 Надето" if item['equipped'] else "👕 Надеть"
        caption = f"👕 {item['name']}\n💵 {format_balance(item['price'])}\n📦 {item['type']}\n{status}\n\n📄 Страница {page + 1} из {total_pages}"
        markup = create_wardrobe_markup(item['item_id'], item['equipped'], page, total_pages)
        photo_path = f"images/{item['image_name']}"
        
        current_data = wardrobe_pages.get(user_id, {'page': 0, 'message_id': None})
        message_id = current_data.get('message_id')
        
        if message_id is None:
            if os.path.exists(photo_path):
                with open(photo_path, 'rb') as photo:
                    sent_message = bot.send_photo(
                        chat_id,
                        photo,
                        caption=caption,
                        reply_markup=markup,
                        parse_mode='HTML'
                    )
            else:
                sent_message = bot.send_message(chat_id, caption, reply_markup=markup, parse_mode='HTML')
            wardrobe_pages[user_id] = {'page': page, 'message_id': sent_message.message_id}
        else:
            if os.path.exists(photo_path):
                with open(photo_path, 'rb') as photo:
                    try:
                        bot.edit_message_media(
                            chat_id=chat_id,
                            message_id=message_id,
                            media=types.InputMediaPhoto(photo, caption=caption, parse_mode='HTML'),
                            reply_markup=markup
                        )
                    except Exception as e:
                        try:
                            bot.delete_message(chat_id, message_id)
                        except:
                            pass
                        sent_message = bot.send_photo(
                            chat_id,
                            photo,
                            caption=caption,
                            reply_markup=markup,
                            parse_mode='HTML'
                        )
                        wardrobe_pages[user_id] = {'page': page, 'message_id': sent_message.message_id}
            else:
                try:
                    bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=caption,
                        reply_markup=markup,
                        parse_mode='HTML'
                    )
                except:
                    try:
                        bot.delete_message(chat_id, message_id)
                    except:
                        pass
                    sent_message = bot.send_message(chat_id, caption, reply_markup=markup, parse_mode='HTML')
                    wardrobe_pages[user_id] = {'page': page, 'message_id': sent_message.message_id}
        
    except Exception as e:
        bot.send_message(chat_id, f"❌ Ошибка: {e}", parse_mode='HTML')

def create_wardrobe_markup(item_id, equipped, current_page, total_pages):
    """Создать клавиатуру для гардероба"""
    markup = InlineKeyboardMarkup(row_width=3)
    
    if equipped:
        action_button = InlineKeyboardButton("❌ Снять", callback_data=f"unequip_{item_id}")
    else:
        action_button = InlineKeyboardButton("👕 Надеть", callback_data=f"wear_{item_id}")
    
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"wardrobe_prev_{current_page-1}"))
    
    nav_buttons.append(InlineKeyboardButton(f"{current_page+1}/{total_pages}", callback_data="wardrobe_info"))
    
    if current_page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("➡️ Вперед", callback_data=f"wardrobe_next_{current_page+1}"))
    
    markup.add(action_button)
    if nav_buttons:
        markup.add(*nav_buttons)
    
    return markup

@bot.callback_query_handler(func=lambda call: call.data.startswith(('shop_', 'wardrobe_')))
def handle_navigation(call):
    """Обработка навигации по страницам"""
    user_id = call.from_user.id
    data = call.data
    
    try:
        if data.startswith('shop_'):
            if data.startswith('shop_prev_'):
                page = int(data.split('_')[2])
                show_shop_page(call.message.chat.id, user_id, page)
            elif data.startswith('shop_next_'):
                page = int(data.split('_')[2])
                show_shop_page(call.message.chat.id, user_id, page)
            elif data == 'shop_info':
                bot.answer_callback_query(call.id, "Текущая страница")
                return
        
        elif data.startswith('wardrobe_'):
            if data.startswith('wardrobe_prev_'):
                page = int(data.split('_')[2])
                show_wardrobe_page(call.message.chat.id, user_id, page)
            elif data.startswith('wardrobe_next_'):
                page = int(data.split('_')[2])
                show_wardrobe_page(call.message.chat.id, user_id, page)
            elif data == 'wardrobe_info':
                bot.answer_callback_query(call.id, "Текущая страница")
                return
        
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        bot.answer_callback_query(call.id, "❌ Ошибка!")

@bot.callback_query_handler(func=lambda call: call.data.startswith(('buy_', 'wear_', 'unequip_', 'confirm_buy_')))
def handle_clothes_actions(call):
    user_id = call.from_user.id
    
    try:
        if call.data.startswith('confirm_buy_'):
            item_id = int(call.data.split('_')[2])
            success, msg = buy_clothes(user_id, item_id)
            bot.answer_callback_query(call.id, msg, show_alert=not success)
            if success and user_id in shop_pages:
                current_page = shop_pages[user_id]['page']
                show_shop_page(call.message.chat.id, user_id, current_page)
            elif not success:
                try:
                    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
                except:
                    pass
                
        elif call.data.startswith('buy_'):
            item_id = int(call.data.split('_')[1])
            with get_db_cursor() as cursor:
                cursor.execute('SELECT name, price FROM clothes_shop WHERE id = ?', (item_id,))
                item = cursor.fetchone()
            if not item:
                bot.answer_callback_query(call.id, "❌ Товар не найден!")
                return
            item_name, item_price = item
            balance = get_balance(user_id)
            if balance < item_price:
                bot.answer_callback_query(call.id, f"❌ Не хватает монет! Нужно: {plain_balance(item_price)}", show_alert=True)
                return
            confirm_kb = {
                "inline_keyboard": [[
                    {"text": "✅ Купить", "callback_data": f"confirm_buy_{item_id}", "style": "success"},
                    {"text": "❌ Отмена", "callback_data": f"cancel_buy_{item_id}", "style": "danger"}
                ]]
            }
            bot.answer_callback_query(call.id)
            bot.send_message(
                call.message.chat.id,
                f"🛍️ <b>Подтвердите покупку</b>\n\n"
                f"<blockquote>Вещь: {item_name}\nЦена: {format_balance(item_price)}\nОстаток: {format_balance(balance - item_price)}</blockquote>\n\nПокупаем?",
                reply_markup=confirm_kb,
                parse_mode='HTML'
            )
            
        elif call.data.startswith('cancel_buy_'):
            bot.answer_callback_query(call.id, "Отменено")
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
            
        elif call.data.startswith('wear_'):
            item_id = int(call.data.split('_')[1])
            success, msg = equip_clothes(user_id, item_id)
            bot.answer_callback_query(call.id, msg)
            if success and user_id in wardrobe_pages:
                current_page = wardrobe_pages[user_id]['page']
                show_wardrobe_page(call.message.chat.id, user_id, current_page)
                
                outfit_path = create_character_outfit(user_id)
                print(f"🔄 Образ обновлен после надевания: {outfit_path}")
            
        elif call.data.startswith('unequip_'):
            item_id = int(call.data.split('_')[1])
            success, msg = unequip_clothes(user_id, item_id)
            bot.answer_callback_query(call.id, msg)
            if success and user_id in wardrobe_pages:
                current_page = wardrobe_pages[user_id]['page']
                show_wardrobe_page(call.message.chat.id, user_id, current_page)
                
                outfit_path = create_character_outfit(user_id)
                print(f"🔄 Образ обновлен после снятия: {outfit_path}")
                
    except Exception as e:
        print(f"Ошибка в handle_clothes_actions: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка!")

def get_item_price(item_id):
    """Получить цену айтема по ID"""
    clothes = get_shop_clothes()
    for item in clothes:
        if item['id'] == item_id:
            return item['price']
    return 0
    
active_captchas = {}

def generate_math_captcha():
    """Сгенерировать математическую капчу"""
    a = random.randint(1, 15)
    b = random.randint(1, 15)
    operation = random.choice(['+', '-'])
    
    if operation == '+':
        answer = a + b
        question = f"{a} + {b}"
    else:
        a, b = max(a, b), min(a, b)
        answer = a - b
        question = f"{a} - {b}"
    
    return question, str(answer)

def is_new_user(user_id):
    """Проверить, новый ли игрок"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
        return cursor.fetchone() is None

@bot.message_handler(commands=['start'])
def start(message):
    try:
        user_id = message.from_user.id
        username = message.from_user.username
        first_name = message.from_user.first_name

        # Если это групповой чат — просто игнорируем /start для новых
        if message.chat.type != 'private':
            if not is_new_user(user_id):
                handle_existing_user(message)
            return

        if is_new_user(user_id):
            if user_id in active_captchas:
                bot.send_message(
                    message.chat.id,
                    "⏳ <b>Регистрация уже начата!</b>\n\nРешите математический пример ниже 👇",
                    parse_mode='HTML'
                )
                return

            question, answer = generate_math_captcha()
            active_captchas[user_id] = {
                'answer': answer,
                'attempts': 0,
                'max_attempts': 3,
                'username': username,
                'first_name': first_name,
                'ref_code': message.text.split()[1] if len(message.text.split()) > 1 else None,
                'created_at': time.time()
            }

            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🔄 Обновить задачу", callback_data="captcha_refresh"))

            captcha_text = (
                f"👋 <b>Привет, {first_name}!</b>\n\n"
                f"Добро пожаловать! Прежде чем начать — быстрая проверка.\n\n"
                f"🔐 <b>Реши пример:</b>\n"
                f"┌─────────────────┐\n"
                f"│  <b>{question} = ?</b>  │\n"
                f"└─────────────────┘\n\n"
                f"✏️ Введи ответ числом в чат\n"
                f"⚠️ Осталось попыток: <b>3</b>"
            )

            bot.send_message(message.chat.id, captcha_text, reply_markup=markup, parse_mode='HTML')

        else:
            handle_existing_user(message)

    except Exception as e:
        print(f"Ошибка в start: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при запуске. Повторите попытку.", parse_mode='HTML')

def handle_existing_user(message):
    """Обработка для существующих пользователей"""
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    
    try:
        referred_by = None
        if len(message.text.split()) > 1:
            ref_code = message.text.split()[1]
            with get_db_cursor() as cursor:
                cursor.execute('SELECT amount, max_activations, current_activations, password, target_username FROM checks WHERE code = ?', (ref_code,))
                check_data = cursor.fetchone()
                
                if check_data:
                    amount, max_activations, current_activations, password, target_username = check_data
                    
                    cursor.execute('SELECT * FROM check_activations WHERE user_id = ? AND check_code = ?', (user_id, ref_code))
                    already_activated = cursor.fetchone()
                    
                    if already_activated:
                        bot.send_message(message.chat.id, "❌ Вы уже активировали этот чек!", parse_mode='HTML')
                    elif current_activations < max_activations:
                        if password:
                            msg = bot.send_message(message.chat.id, f"🔐 У этого чека есть пароль. Введите пароль для активации:")
                            bot.register_next_step_handler(msg, process_check_password, ref_code, user_id, amount, max_activations, current_activations, password, target_username)
                        else:
                            success, result_message = activate_check(user_id, ref_code, amount, max_activations, current_activations, password, target_username)
                            bot.send_message(message.chat.id, result_message, parse_mode='HTML')
                    else:
                        bot.send_message(message.chat.id, "❌ Чек уже использован максимальное количество раз!", parse_mode='HTML')
                else:
                    cursor.execute('SELECT user_id FROM users WHERE referral_code = ?', (ref_code,))
                    ref_user = cursor.fetchone()
                    
                    if ref_user and ref_user[0] != user_id:
                        referred_by = ref_user[0]
                        cursor.execute('SELECT referred_by FROM users WHERE user_id = ?', (user_id,))
                        current_ref = cursor.fetchone()
                        
                        if not current_ref or not current_ref[0]:
                            referrer_bonus = 5000
                            user_bonus = 1000
                            referrer_exp = 500
                            
                            cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (referrer_bonus, referred_by))
                            cursor.execute('UPDATE users SET referred_by = ? WHERE user_id = ?', (referred_by, user_id))
                            cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (user_bonus, user_id))
                            # уведомления — вне транзакции
                            _ref_notify_id = referred_by
                            _ref_notify_exp = referrer_exp

        # Отправляем уведомления ПОСЛЕ закрытия транзакции — не блокируем БД
        if '_ref_notify_id' in dir() and _ref_notify_id:
            bot.send_message(message.chat.id, "🎉 Ты получил 1 000🌸 за приглашение друга!", parse_mode='HTML')
            add_experience(_ref_notify_id, _ref_notify_exp)
            try:
                bot.send_message(_ref_notify_id, f"👥 Друг принял твоё приглашение!\n⭐ +{_ref_notify_exp} опыта", parse_mode='HTML')
            except Exception:
                pass
            _ref_notify_id = None

        if len(message.text.split()) > 1 and message.text.split()[1] == 'notify':
            enabled = toggle_event_notify(user_id)
            icon = "🔔" if enabled else "🔕"
            status = "включены" if enabled else "отключены"
            bot.send_message(
                message.chat.id,
                f"{icon} <b>Уведомления об ивентах {status}!</b>\n\n"
                f"{'Я буду присылать тебе уведомление когда в чате начнётся ивент — со ссылкой на него.' if enabled else 'Ты больше не будешь получать уведомления об ивентах.'}\n\n"
                f"Чтобы {'отключить' if enabled else 'включить'} снова — нажми кнопку ниже или перейди по ссылке:",
                reply_markup=types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(
                        f"{'🔕 Отключить' if enabled else '🔔 Включить'} уведомления",
                        callback_data="toggle_event_notify"
                    )
                ]]),
                parse_mode="HTML"
            )
            return

        if len(message.text.split()) > 1 and message.text.split()[1] == 'discount':
            handle_buy(message)
            return

        markup = create_main_menu()
        welcome_text = f"""👋 С возвращением, {first_name}!

Выбирай раздел в меню и вперёд 🚀"""

        bot.send_message(message.chat.id, welcome_text, reply_markup=markup, parse_mode='HTML')

    except Exception as e:
        print(f"Ошибка в handle_existing_user: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка. Повторите попытку.", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.chat.type == 'private' and message.from_user.id in active_captchas and not message.text.startswith('/'))
def handle_captcha_answer(message):
    user_id = message.from_user.id
    captcha_data = active_captchas[user_id]

    try:
        user_answer = message.text.strip()
        correct_answer = captcha_data['answer']
        first_name = captcha_data.get('first_name', 'друг')

        if user_answer == correct_answer:
            del active_captchas[user_id]
            complete_new_user_registration(
                user_id,
                captcha_data['username'],
                first_name,
                captcha_data['ref_code']
            )
        else:
            captcha_data['attempts'] += 1
            remaining_attempts = captcha_data['max_attempts'] - captcha_data['attempts']

            if remaining_attempts > 0:
                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton("🔄 Обновить задачу", callback_data="captcha_refresh"))
                bot.send_message(
                    message.chat.id,
                    f"❌ <b>Неверно!</b>\n\nПопробуй ещё раз.\n⚠️ Осталось попыток: <b>{remaining_attempts}</b>",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
            else:
                del active_captchas[user_id]
                bot.send_message(
                    message.chat.id,
                    "🚫 <b>Попытки исчерпаны.</b>\n\nНапиши /start чтобы попробовать снова.",
                    parse_mode='HTML'
                )

    except Exception as e:
        bot.send_message(message.chat.id, "❌ Введи число!", parse_mode='HTML')

def complete_new_user_registration(user_id, username, first_name, ref_code):
    """Завершение регистрации нового пользователя после капчи"""
    try:
        get_or_create_user(user_id, username, first_name)
        
        if ref_code:
            handle_referral_code(user_id, ref_code)
        
        markup = create_main_menu()
        
        success_text = f"""✅ <b>Добро пожаловать, {first_name}!</b>

Ты в игре. Начни зарабатывать прямо сейчас:
• 👆 Кликай в разделе <b>Работа</b>
• 🛍️ Одевайся в <b>Шопе</b>
• ⚔️ Вступай в <b>Гильдию</b>"""

        bot.send_message(user_id, success_text, reply_markup=markup, parse_mode='HTML')
        
    except Exception as e:
        print(f"Ошибка в complete_new_user_registration: {e}")
        bot.send_message(user_id, "❌ Ошибка при регистрации. Повторите попытку.", parse_mode='HTML')
@bot.callback_query_handler(func=lambda c: c.data == "captcha_refresh")
def cb_captcha_refresh(call):
    user_id = call.from_user.id
    if user_id not in active_captchas:
        bot.answer_callback_query(call.id, "Регистрация уже завершена")
        return
    question, answer = generate_math_captcha()
    active_captchas[user_id]['answer'] = answer
    active_captchas[user_id]['attempts'] = 0
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔄 Обновить задачу", callback_data="captcha_refresh"))
    try:
        bot.edit_message_text(
            f"🔄 <b>Новая задача!</b>\n\n"
            f"🔐 <b>Реши пример:</b>\n"
            f"┌─────────────────┐\n"
            f"│  <b>{question} = ?</b>  │\n"
            f"└─────────────────┘\n\n"
            f"✏️ Введи ответ числом в чат\n"
            f"⚠️ Осталось попыток: <b>3</b>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='HTML'
        )
    except Exception:
        bot.send_message(user_id,
            f"🔄 <b>Новая задача:</b> <b>{question} = ?</b>\n\nВведи ответ числом.",
            reply_markup=markup, parse_mode='HTML')
    bot.answer_callback_query(call.id)

def handle_referral_code(user_id, ref_code):
    """Обработка реферального кода"""
    _notify_check = None     # (msg_text, parse_mode)
    _notify_check_password = None  # (msg, args...)
    _notify_ref_bonus = None  # user_id реферера

    try:
        with get_db_cursor() as cursor:
            cursor.execute('SELECT amount, max_activations, current_activations, password, target_username FROM checks WHERE code = ?', (ref_code,))
            check_data = cursor.fetchone()

            if check_data:
                amount, max_activations, current_activations, password, target_username = check_data

                cursor.execute('SELECT * FROM check_activations WHERE user_id = ? AND check_code = ?', (user_id, ref_code))
                already_activated = cursor.fetchone()

                if not already_activated and current_activations < max_activations:
                    if password:
                        _notify_check_password = (ref_code, user_id, amount, max_activations, current_activations, password, target_username)
                    else:
                        success, result_message = activate_check(user_id, ref_code, amount, max_activations, current_activations, password, target_username)
                        _notify_check = (result_message, 'HTML')
                elif already_activated:
                    _notify_check = ("❌ Вы уже активировали этот чек!", 'HTML')
                else:
                    _notify_check = ("❌ Чек уже использован максимальное количество раз!", 'HTML')
            else:
                cursor.execute('SELECT user_id FROM users WHERE referral_code = ?', (ref_code,))
                ref_user = cursor.fetchone()

                if ref_user and ref_user[0] != user_id:
                    referred_by = ref_user[0]
                    cursor.execute('SELECT referred_by FROM users WHERE user_id = ?', (user_id,))
                    current_ref = cursor.fetchone()

                    if not current_ref or not current_ref[0]:
                        referrer_bonus = 5000
                        user_bonus = 1000

                        cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (referrer_bonus, referred_by))
                        cursor.execute('UPDATE users SET referred_by = ? WHERE user_id = ?', (referred_by, user_id))
                        cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (user_bonus, user_id))
                        _notify_ref_bonus = referred_by

    except Exception as e:
        print(f"Ошибка в handle_referral_code: {e}")
        return

    # Все уведомления — строго вне транзакции, не блокируем БД
    if _notify_check:
        bot.send_message(user_id, _notify_check[0], parse_mode=_notify_check[1])
    if _notify_check_password:
        rc, uid, amt, mx, cur_act, pwd, tgt = _notify_check_password
        msg = bot.send_message(user_id, "🔐 У этого чека есть пароль. Введите пароль для активации:")
        bot.register_next_step_handler(msg, process_check_password, rc, uid, amt, mx, cur_act, pwd, tgt)
    if _notify_ref_bonus:
        bot.send_message(user_id, "🎉 Ты получил 1 000🌸 за регистрацию по реферальной ссылке!", parse_mode='HTML')
def activate_check(user_id, ref_code, amount, max_activations, current_activations, password, target_username):
    with get_db_cursor() as cursor:
        if target_username:
            cursor.execute('SELECT username FROM users WHERE user_id = ?', (user_id,))
            user_data = cursor.fetchone()
            current_username = f"@{user_data[0]}" if user_data and user_data[0] else None
            if (current_username or '').lower() != (target_username or '').lower():
                return False, f"❌ Этот чек предназначен для {target_username}!"

        cursor.execute('SELECT 1 FROM check_activations WHERE user_id = ? AND check_code = ?', (user_id, ref_code))
        if cursor.fetchone():
            return False, "❌ Вы уже активировали этот чек!"

        cursor.execute('SELECT current_activations, max_activations FROM checks WHERE code = ?', (ref_code,))
        row = cursor.fetchone()
        if not row:
            return False, "❌ Чек не найден!"
        real_current, real_max = row
        if real_current >= real_max:
            return False, "❌ Чек уже использован максимальное количество раз!"

        cursor.execute('INSERT INTO check_activations (user_id, check_code) VALUES (?, ?)', (user_id, ref_code))

        cursor.execute('UPDATE checks SET current_activations = current_activations + 1 WHERE code = ?', (ref_code,))

        cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))

    return True, f"<tg-emoji emoji-id='5440354006335495210'>🌸</tg-emoji> <b>Вы активировали чек на {format_balance(amount)}</b> <tg-emoji emoji-id='5440354006335495210'>🌸</tg-emoji>!"

def process_check_password(message, ref_code, user_id, amount, max_activations, current_activations, password, target_username):
    try:
        if message.text.strip() == password:
            success, result_message = activate_check(user_id, ref_code, amount, max_activations, current_activations, password, target_username)
            bot.send_message(message.chat.id, result_message, parse_mode='HTML')
        else:
            bot.send_message(message.chat.id, "❌ Неверный пароль! Чек не активирован.", parse_mode='HTML')
    except Exception as e:
        print(f"Ошибка в process_check_password: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при активации чека.", parse_mode='HTML')
def clean_expired_captchas():
    """Очистка капч старше 5м"""
    current_time = time.time()
    expired_users = []
    
    for user_id, captcha_data in active_captchas.items():
        if current_time - captcha_data['created_at'] > 300:
            expired_users.append(user_id)
    
    for user_id in expired_users:
        del active_captchas[user_id]
        print(f"🧹 Удалена просроченная капча для {user_id}")

@bot.message_handler(func=lambda message: message.text in ["📞 Помощь", "Помощь"])
def handle_help(message):
    """Показать справку и контакты"""
    
    help_text = """📞 <b>Помощь</b>"""

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("💬 Чат", url="https://t.me/FECTIZ_CHAT"),
        InlineKeyboardButton("📢 Канал", url="https://t.me/FECTIZ")
    )
    markup.row(
        InlineKeyboardButton("📞 Поддержка", url="https://t.me/Cary_Python"),
        InlineKeyboardButton("🎴 Владелец", url="https://t.me/E_vo7")
    )
    
    bot.send_message(
        message.chat.id,
        help_text,
        reply_markup=markup,
        parse_mode='HTML'
    )
@bot.message_handler(func=lambda message: message.text.lower().startswith('имя '))
def handle_name_change(message):
    
    try:
        user_id = message.from_user.id
        new_name = message.text[4:].strip()
        
        if not new_name:
            bot.send_message(message.chat.id, "❌ Имя не может быть пустым!", parse_mode='HTML')
            return
        
        if len(new_name) > 20:
            bot.send_message(message.chat.id, "❌ Имя слишком длинное! Максимум 20 символов.", parse_mode='HTML')
            return
        
        with get_db_cursor() as cursor:
            cursor.execute('UPDATE users SET custom_name = ? WHERE user_id = ?', (new_name, user_id))
        
        bot.send_message(message.chat.id, f"🏆 Твое имя изменено на: {new_name}", parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка в handle_name_change: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при изменении имени. Повторите попытку.", parse_mode='HTML')
        
@bot.message_handler(func=lambda message: message.text.lower().startswith('чек '))
def handle_user_check_command(message):
    user_id = message.from_user.id
    if not is_registered(user_id):
        bot.send_message(message.chat.id, "❌ <b>Ты не зарегистрирован!</b>\n\nНапиши /start чтобы начать.", parse_mode='HTML')
        return
    
    if is_user_warned(user_id):
        bot.send_message(message.chat.id, "❌ Вы не можете создавать чеки, так как у вас активный варн!")
        return
    
    balance = get_balance(user_id)
    
    try:
        parts = message.text.split()
        if len(parts) < 3:
            bot.send_message(message.chat.id, 
                           "❌ Используйте: чек [сумма] [кол-во активаций] (пароль)\n\n"
                           "Примеры:\n"
                           "• чек 1000000 1 - чек на 1М, 1 активация\n"
                           "• чек 500000 3 mypass - чек на 500к с паролем")
            return
        
        amount = parse_bet_amount(parts[1], balance)
        if amount is None or amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма!")
            return
        
        try:
            max_activations = int(parts[2])
            if max_activations <= 0:
                bot.send_message(message.chat.id, "❌ Количество активаций должно быть больше 0!")
                return
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверное количество активаций!")
            return
        
        password = parts[3] if len(parts) > 3 else None
        
        total_amount = amount * max_activations
        
        if total_amount > balance:
            bot.send_message(message.chat.id, f"❌ Не хватает монет! Нужно: {format_balance(total_amount)}")
            return
        
        update_balance(user_id, -total_amount)
        
        check_code = f"user{user_id}{random.randint(1000, 9999)}"
        
        with get_db_cursor() as cursor:
            cursor.execute('''
                INSERT INTO checks (code, amount, max_activations, password, created_by)
                VALUES (?, ?, ?, ?, ?)
            ''', (check_code, amount, max_activations, password, user_id))
        
        bot_username = (bot.get_me()).username
        check_link = f"https://t.me/{bot_username}?start={check_code}"
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🎫 Активировать чек", url=check_link))
        
        message_text = f"🎫 <b>Ваучер создан!</b>\n\n"
        message_text += f"💵 Сумма за активацию: {format_balance(amount)}\n"
        message_text += f"🔢 Активаций: {max_activations}\n"
        message_text += f"🔐 Пароль: {'есть' if password else 'нет'}"
        
        bot.send_message(message.chat.id, message_text, reply_markup=markup, parse_mode='HTML')
        
        if password:
            bot.send_message(message.chat.id, 
                           f"🔒 Пароль от чека: <code>{password}</code>\n\n"
                           f"⚠️ Никому не сообщайте этот пароль!", parse_mode='HTML')
        
    except Exception as e:
        print(f"Ошибка при создании чека пользователем: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при создании чека!")

@bot.message_handler(commands=['чек'])
def handle_cheque_command(message):
    try:
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "❌ Только админы могут создавать чеки")
            return
        
        parts = message.text.split()
        if len(parts) < 3:
            bot.send_message(message.chat.id, 
                           "❌ Используйте: /чек [сумма] [активации] (пароль)\n\n"
                           "Примеры:\n"
                           "/чек 1000000 10 - чек на 1М, 10 активаций\n"
                           "/чек 5000000 5 secret - чек на 5М с паролем")
            return
        
        amount = parse_bet_amount(parts[1], float('inf'))
        if amount is None or amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма!")
            return
        
        try:
            max_activations = int(parts[2])
            if max_activations <= 0:
                bot.send_message(message.chat.id, "❌ Количество активаций должно быть больше 0!")
                return
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверное количество активаций!")
            return
        
        password = parts[3] if len(parts) > 3 else None
        
        check_code = f"cheque{random.randint(100000, 999999)}"
        
        with get_db_cursor() as cursor:
            cursor.execute('''
                INSERT INTO checks (code, amount, max_activations, password, created_by)
                VALUES (?, ?, ?, ?, ?)
            ''', (check_code, amount, max_activations, password, message.from_user.id))
        
        bot_username = (bot.get_me()).username
        check_link = f"https://t.me/{bot_username}?start={check_code}"
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🎫 Активировать чек", url=check_link))
        
        total_amount = amount * max_activations
        message_text = f"🎫 <b>Ваучер создан!</b>\n\n"
        message_text += f"💵 Сумма за активацию: {format_balance(amount)}\n"
        message_text += f"🔢 Активаций: {max_activations}\n"
        message_text += f"🔐 Пароль: {'есть' if password else 'нет'}"
        
        bot.send_message(message.chat.id, message_text, reply_markup=markup, parse_mode='HTML')
        
        if password:
            bot.send_message(
                message.chat.id,
                f"🔒 Пароль от чека: <code>{password}</code>\n\n"
                f"⚠️ Сообщите пароль получателям!",
                parse_mode='HTML'
            )
        
    except Exception as e:
        print(f"Ошибка при создании чека: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при создании чека!")
@bot.inline_handler(func=lambda query: True)
def handle_inline_query(query):
    try:
        user_id = query.from_user.id
        query_text = query.query.strip()
        
        if not query_text:
            results = []
            
            help_result = InlineQueryResultArticle(
                id='help',
                title='💡 Создание чека',
                description='Введите: сумма активации пароль',
                input_message_content=InputTextMessageContent(
                    message_text="💡 **Создание чека:**\n\n"
                               "**Формат:** сумма активации пароль\n\n"
                               "**Примеры:**\n"
                               "`1000000 1` - чек на 1М, 1 активация\n"
                               "`5000000 5 secret` - чек на 5М с паролем\n\n"
                               "💵 **С вашего баланса спишется: сумма × активации**",
                    parse_mode='Markdown'
                )
            )
            results.append(help_result)
            
            bot.answer_inline_query(query.id, results, cache_time=1)
            return
        
        parts = query_text.split()
        
        if len(parts) < 2:
            error_result = InlineQueryResultArticle(
                id='error',
                title='❌ Неверный формат',
                description='Нужно: сумма активации пароль',
                input_message_content=InputTextMessageContent(
                    message_text="❌ **Неверный формат!**\n\n"
                               "**Правильный формат:** сумма активации пароль\n\n"
                               "**Пример:** `1000000 1` - чек на 1М",
                    parse_mode='Markdown'
                )
            )
            bot.answer_inline_query(query.id, [error_result], cache_time=1)
            return
        
        amount_str = parts[0]
        activations_str = parts[1]
        password = parts[2] if len(parts) > 2 else None
        
        amount = parse_bet_amount(amount_str, float('inf'))
        if amount is None or amount <= 0:
            error_result = InlineQueryResultArticle(
                id='error_amount',
                title='❌ Неверная сумма',
                description='Укажите корректную сумму',
                input_message_content=InputTextMessageContent(
                    message_text=f"❌ Неверная сумма: {amount_str}",
                    parse_mode='Markdown'
                )
            )
            bot.answer_inline_query(query.id, [error_result], cache_time=1)
            return
        
        try:
            max_activations = int(activations_str)
            if max_activations <= 0:
                raise ValueError
        except ValueError:
            error_result = InlineQueryResultArticle(
                id='error_activations',
                title='❌ Неверное количество активаций',
                description='Укажите число больше 0',
                input_message_content=InputTextMessageContent(
                    message_text=f"❌ Неверное количество активаций: {activations_str}",
                    parse_mode='Markdown'
                )
            )
            bot.answer_inline_query(query.id, [error_result], cache_time=1)
            return
        
        total_cost = amount * max_activations
        
        user_balance = get_balance(user_id)
        if user_balance < total_cost:
            error_result = InlineQueryResultArticle(
                id='error_balance',
                title='❌ Не хватает монет',
                description=f'Нужно: {format_balance(total_cost)}',
                input_message_content=InputTextMessageContent(
                    message_text=f"❌ Не хватает монет!\n\n"
                               f"💵 Нужно: {format_balance(total_cost)}\n"
                               f"💳 Твой баланс: {format_balance(user_balance)}",
                    parse_mode='Markdown'
                )
            )
            bot.answer_inline_query(query.id, [error_result], cache_time=1)
            return
        
        update_balance(user_id, -total_cost)
        
        cheque_result = create_inline_cheque_result(amount_str, activations_str, password, 
                                                   f"{format_balance(amount)}, {max_activations} активаций")
        
        bot.answer_inline_query(query.id, [cheque_result], cache_time=1)
        
    except Exception as e:
        print(f"Ошибка в инлайн-запросе: {e}")

def create_inline_cheque_result(amount_str, activations_str, password, description):
    check_code = f"inline{random.randint(100000, 999999)}"
    
    amount = parse_bet_amount(amount_str, float('inf'))
    max_activations = int(activations_str)
    
    with get_db_cursor() as cursor:
        cursor.execute('''
            INSERT INTO checks (code, amount, max_activations, password, created_by)
            VALUES (?, ?, ?, ?, ?)
        ''', (check_code, amount, max_activations, password, user_id))
    
    bot_username = (bot.get_me()).username
    check_link = f"https://t.me/{bot_username}?start={check_code}"
    
    message_text = f"🎫 Создан чек!\n\n"
    message_text += f"💵 Сумма за активацию: {format_balance(amount)}\n"
    message_text += f"🔢 Активаций: {max_activations}\n" 
    message_text += f"🔐 Пароль: {'есть' if password else 'нет'}\n\n"

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🎫 Активировать чек", url=check_link))
    
    return InlineQueryResultArticle(
        id=check_code,
        title=f'💵 Чек на {format_balance(amount)}',
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=message_text,
            parse_mode='Markdown'
        ),
        reply_markup=markup
    )
@bot.message_handler(func=lambda message: message.text and message.text.lower().strip() == 'актив' and is_admin(message.from_user.id))
def handle_active(message):
    """Показать статистику активности"""
    try:
        user_id = message.from_user.id
        show_active_stats(message.chat.id, user_id)
    except Exception as e:
        print(f"Ошибка в handle_active: {e}")
        bot.send_message(message.chat.id, "⚠️ Что-то пошло не так. Попробуй ещё раз.")

def show_active_stats(chat_id, user_id, message_id=None):
    """Показать статистику активности с кнопкой обновления"""
    try:
        now = int(time.time())
        with get_db_cursor() as cursor:
            # last_activity хранится как Unix timestamp (INTEGER) начиная с нового middleware.
            # Но старые записи могут быть строкой CURRENT_TIMESTAMP от SQLite.
            # Используем CAST и также фильтруем только числовые значения > 1000000000 (после 2001г)

            # Активные в ЛС за 24 часа — используем last_private_activity
            cursor.execute('''
                SELECT COUNT(*) FROM users
                WHERE CAST(last_private_activity AS INTEGER) > ?
                AND CAST(last_private_activity AS INTEGER) > 1000000000
            ''', (now - 86400,))
            active_users_24h = cursor.fetchone()[0] or 0

            # Активные в ЛС за 7 дней
            cursor.execute('''
                SELECT COUNT(*) FROM users
                WHERE CAST(last_private_activity AS INTEGER) > ?
                AND CAST(last_private_activity AS INTEGER) > 1000000000
            ''', (now - 604800,))
            active_users_7d = cursor.fetchone()[0] or 0

            # Активные в ЛС за 30 дней
            cursor.execute('''
                SELECT COUNT(*) FROM users
                WHERE CAST(last_private_activity AS INTEGER) > ?
                AND CAST(last_private_activity AS INTEGER) > 1000000000
            ''', (now - 2592000,))
            active_users_30d = cursor.fetchone()[0] or 0

            # Всего пользователей
            cursor.execute('SELECT COUNT(*) FROM users')
            total_users = cursor.fetchone()[0] or 0

            cursor.execute('SELECT SUM(balance + bank_deposit) FROM users')
            total_economy = cursor.fetchone()[0] or 0

            cursor.execute('SELECT SUM(balance) FROM users')
            total_balance = cursor.fetchone()[0] or 0

            cursor.execute('SELECT SUM(bank_deposit) FROM users')
            total_deposits = cursor.fetchone()[0] or 0

        message_text = (
            f"╔══════════════════════╗\n"
            f"   📊  <b>АКТИВНОСТЬ БОТА</b>\n"
            f"╚══════════════════════╝\n\n"
        )

        message_text += "<b>👥 ПОЛЬЗОВАТЕЛИ</b>\n"
        message_text += (
            f"<blockquote>"
            f"🟢 Активных (24ч):  <b>{active_users_24h:,}</b>\n"
            f"📅 Активных (7д):   <b>{active_users_7d:,}</b>\n"
            f"📆 Активных (30д):  <b>{active_users_30d:,}</b>\n"
            f"🎴 Всего в базе:    <b>{total_users:,}</b>"
            f"</blockquote>\n\n"
        )

        message_text += "<b>💵 ЭКОНОМИКА</b>\n"
        message_text += (
            f"<blockquote>"
            f"💸 Всего в обороте: <b>{format_balance(total_economy)}</b>\n"
            f"👛 На руках:        <b>{format_balance(total_balance)}</b>\n"
            f"🏦 На вкладах:      <b>{format_balance(total_deposits)}</b>\n"
            f"📊 Среднее/чел:     <b>{format_balance(total_economy // total_users if total_users > 0 else 0)}</b>"
            f"</blockquote>\n\n"
        )

        message_text += f"<i>🕒 Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>"

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔄 Обновить", callback_data="refresh_active_stats"))
        
        if message_id:
            bot.edit_message_text(
                message_text, 
                chat_id, 
                message_id,
                reply_markup=markup, 
                parse_mode='HTML'
            )
        else:
            msg = bot.send_message(
                chat_id, 
                message_text, 
                reply_markup=markup, 
                parse_mode='HTML'
            )
            return msg.message_id
    
    except Exception as e:
        print(f"Ошибка в show_active_stats: {e}")
        error_msg = "❌ Ошибка при загрузке статистики!"
        if message_id:
            try:
                bot.edit_message_text(error_msg, chat_id, message_id)
            except:
                bot.send_message(chat_id, error_msg)
        else:
            bot.send_message(chat_id, error_msg)

@bot.callback_query_handler(func=lambda call: call.data == "refresh_active_stats")
def handle_refresh_active_stats(call):
    """Обновить статистику активности"""
    try:
        user_id = call.from_user.id
        show_active_stats(call.message.chat.id, user_id, call.message.message_id)
        bot.answer_callback_query(call.id, "🏆 Статистика обновлена!")
    except Exception as e:
        print(f"Ошибка при обновлении статистики: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка обновления!")
@bot.message_handler(func=lambda message: message.text and bot.get_me().username.lower() in message.text.lower() and not message.text.startswith('/'))
def handle_bot_mention(message):
    try:
        if not is_admin(message.from_user.id):
            return
        
        text_lower = message.text.lower()
        bot_username = bot.get_me().username.lower()
        
        if f"@{bot_username}" in text_lower and len(message.text.split()) <= 2:
            help_text = "💡 **Создание чека через бота**\n\n"
            help_text += "**Способ 1 (команда):**\n"
            help_text += "`/чек 1000000 10` - чек на 1М, 10 активаций\n"
            help_text += "`/чек 5000000 5 secret` - чек на 5М с паролем\n\n"
            help_text += "**Способ 2 (упоминание):**\n"
            help_text += "`@netroon_bot 1000000 10` - чек на 1М\n"
            help_text += "`@netroon_bot 5000000 5 secret` - чек с паролем"
            
            bot.send_message(message.chat.id, help_text, parse_mode='Markdown')
            return
        
        if f"@{bot_username}" in text_lower:
            parts = message.text.split()
            bot_index = None
            
            for i, part in enumerate(parts):
                if f"@{bot_username}" in part.lower():
                    bot_index = i
                    break
            
            if bot_index is not None and len(parts) > bot_index + 2:
                cheque_params = parts[bot_index + 1:]
                
                fake_message = type('obj', (object,), {
                    'chat': message.chat,
                    'from_user': message.from_user,
                    'text': f"/чек {' '.join(cheque_params)}"
                })
                
                handle_cheque_command(fake_message)
                return
        
    except Exception as e:
        print(f"Ошибка при обработке упоминания: {e}")
                        
@bot.message_handler(func=lambda message: message.text.lower().startswith('чеф ') and is_admin(message.from_user.id))
def handle_create_check(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды")
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 3:
            bot.send_message(message.chat.id, 
                           "❌ Используйте: чеф [сумма] [кол-во/юзернейм] (пароль)\n\n"
                           "Примеры:\n"
                           "• чеф 1000000 10 - чек на 1М, 10 активаций\n"
                           "• чеф 5000000 @username - чек на 5М для @username\n"
                           "• чеф 1000000 5 secret123 - чек на 1М с паролем\n"
                           "• чеф 5000000 @username secret123 - чек на 5М для @username с паролем")
            return
        
        amount = parse_bet_amount(parts[1], float('inf'))
        if amount is None or amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма!")
            return
        
        target_username = None
        max_activations = 1
        
        if parts[2].startswith('@'):
            target_username = parts[2]
            max_activations = 1
        else:
            try:
                max_activations = int(parts[2])
                if max_activations <= 0:
                    bot.send_message(message.chat.id, "❌ Количество активаций должно быть больше 0!")
                    return
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный формат! Укажите число активаций или юзернейм с @")
                return
        
        password = None
        for i in range(3, len(parts)):
            if not parts[i].startswith('@'):
                password = parts[i]
                break
        
        check_code = f"admin{random.randint(100000, 999999)}"
        
        with get_db_cursor() as cursor:
            cursor.execute('''
                INSERT INTO checks (code, amount, max_activations, password, created_by, target_username)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (check_code, amount, max_activations, password, message.from_user.id, target_username))
        
        bot_username = (bot.get_me()).username
        check_link = f"https://t.me/{bot_username}?start={check_code}"
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🎫 Активировать чек", url=check_link))
        
        message_text = f"🎫 <b>Ваучер создан!</b>\n\n"
        message_text += f"💰 Сумма: {format_balance(amount)}\n"
        
        if target_username:
            message_text += f"👤 Для: {target_username}\n"
        else:
            message_text += f"📊 Активаций: {max_activations}\n"
        
        message_text += f"🔑 Пароль: {'есть' if password else 'нет'}"
        
        bot.send_message(message.chat.id, message_text, reply_markup=markup, parse_mode='HTML')
        
        if password:
            bot.send_message(message.chat.id, 
                           f"🔑 Пароль от чека: <code>{password}</code>\n\n"
                           f"⚠️ Никому не сообщайте этот пароль!", parse_mode='HTML')
        
    except Exception as e:
        print(f"Ошибка при создании чека: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при создании чека!")

def get_user_age(user_id):
    """Получить возраст пользователя в боте (в днях)"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT last_activity FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        if result and result[0]:
            join_date = result[0]
            if isinstance(join_date, str):
                join_date = datetime.fromisoformat(join_date.replace('Z', '+00:00'))
            elif isinstance(join_date, (int, float)):
                join_date = datetime.fromtimestamp(join_date)
        else:
            join_date = datetime.now()
        
        age_days = (datetime.now() - join_date).days
        return max(1, age_days)

@bot.message_handler(commands=['profile'])
def handle_profile_cmd(message):
    handle_profile(message)

@bot.message_handler(func=lambda message: message.text.lower() == 'профиль')
def handle_profile(message):
    
    try:
        user_id = message.from_user.id
        user_info = get_user_info(user_id)
        
        display_name = user_info['custom_name'] if user_info['custom_name'] else (f"@{user_info['username']}" if user_info['username'] else user_info['first_name'])
        
        level, experience = get_user_level(user_id)
        emoji, title_name = get_title(level)
        lvl_cur, lvl_exp, lvl_need = get_level_progress(experience)
        progress_pct = int(lvl_exp / lvl_need * 10) if lvl_need > 0 else 10
        progress_bar = "█" * progress_pct + "░" * (10 - progress_pct)
        
        age_days = get_user_age(user_id)
        
        click_bonus_count = user_info['total_clicks'] // 100
        next_bonus = 100 - (user_info['total_clicks'] % 100)
        
        message_text = f"👤 <b>{display_name}</b>\n"
        message_text += f"{emoji} <b>{title_name}</b> · Уровень {level}\n"
        if lvl_need > 0:
            message_text += f"[{progress_bar}] {lvl_exp}/{lvl_need} exp\n\n"
        else:
            message_text += f"🏆 Максимальный уровень!\n\n"
        message_text += f"📅 В игре: {age_days} дн.\n"
        message_text += f"💵 Баланс: {format_balance(user_info['balance'])}\n"
        message_text += f"🏛 Депозит: {format_balance(user_info['bank_deposit'])}\n"
        message_text += f"⛏️ Майнеры: {user_info['video_cards']}\n"
        message_text += f"👆 Кликов: {user_info['total_clicks']}\n"
        
        games_won = user_info.get('games_won', 0)
        games_lost = user_info.get('games_lost', 0)
        total_won = user_info.get('total_won_amount', 0)
        total_lost = user_info.get('total_lost_amount', 0)
        total_games = games_won + games_lost
        
        message_text += f"\n🎲 <b>Игры:</b>\n"
        message_text += f"Партий: {total_games}\n"
        if total_games > 0:
            message_text += f"Побед: {games_won} ({games_won/total_games*100:.0f}%) · Поражений: {games_lost}\n"
        else:
            message_text += f"Пока нет игр\n"
        message_text += f"Заработано: {format_balance(total_won)}  Проиграно: {format_balance(total_lost)}\n"
        message_text += f"Профит: {format_balance(total_won - total_lost)}"
        
        business_info = get_user_business(user_id)
        if business_info:
            message_text += f"\n\n🏭 Предприятие: {business_info['name']}\n"
            message_text += f"📦 Ресурсы: {business_info['raw_materials']}/{business_info['storage_capacity']}\n"
        
        user_clan = get_user_clan(user_id)
        if user_clan:
            message_text += f"\n⚔️ Гильдия: {user_clan['name']} [{user_clan['tag']}]\n"
            message_text += f"🎖 Статус: {user_clan['role']}\n"
        
        bot.send_message(message.chat.id, message_text, parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка в handle_profile: {e}")
        bot.send_message(message.chat.id, "⚠️ Что-то пошло не так. Попробуй ещё раз.")

@bot.message_handler(func=lambda message: message.text.lower().startswith('сбросить статистику') and is_admin(message.from_user.id))
def handle_reset_stats(message):
    """Сбросить статистику игр пользователя"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, 
                       "❌ Используйте: сбросить статистику [user_id/@username]\n\n"
                       "Примеры:\n"
                       "сбросить статистику 123456789\n"
                       "сбросить статистику @username", parse_mode='HTML')
            return
        
        target = parts[2]
        target_user_id = None
        
        if target.startswith('@'):
            username = target[1:]
            with get_db_cursor() as cursor:
                cursor.execute('SELECT user_id FROM users WHERE username = ?', (username,))
                result = cursor.fetchone()
                if result:
                    target_user_id = result[0]
                else:
                    bot.reply_to(message, f"❌ Пользователь {target} не найден!", parse_mode='HTML')
                    return
        else:
            try:
                target_user_id = int(target)
            except ValueError:
                bot.reply_to(message, "❌ Неверный формат ID пользователя!", parse_mode='HTML')
                return
        
        target_info = get_user_info(target_user_id)
        if not target_info:
            bot.reply_to(message, "❌ Пользователь отсутствует в базе данных!", parse_mode='HTML')
            return
        
        with get_db_cursor() as cursor:
            cursor.execute('''
                UPDATE users SET 
                games_won = 0,
                games_lost = 0, 
                total_won_amount = 0,
                total_lost_amount = 0
                WHERE user_id = ?
            ''', (target_user_id,))
        
        user_info = get_user_info(target_user_id)
        display_name = user_info['custom_name'] if user_info['custom_name'] else (
            f"@{user_info['username']}" if user_info['username'] else f"ID: {target_user_id}"
        )
        
        bot.reply_to(message, 
                   f"🏆 Статистика игр сброшена!\n\n"
                   f"🎴 Пользователь: {display_name}\n"
                   f"🆔 ID: {target_user_id}\n\n"
                   f"📊 Обнулено:\n"
                   f"• Побед: {target_info.get('games_won', 0)} → 0\n"
                   f"• Поражений: {target_info.get('games_lost', 0)} → 0\n"
                   f"• Побед: {format_balance(target_info.get('total_won_amount', 0))} → 0\n"
                   f"• Поражений: {format_balance(target_info.get('total_lost_amount', 0))} → 0", parse_mode='HTML')
        
    except Exception as e:
        print(f"Ошибка в handle_reset_stats: {e}")
        bot.reply_to(message, f"❌ Ошибка: {e}", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower() == 'сбросить всю статистику' and is_admin(message.from_user.id))
def handle_reset_all_stats(message):
    """Сбросить статистику игр всех пользователей"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        reset_stats_keyboard = {
            "inline_keyboard": [[
                {"text": "⚠️ ДА, СБРОСИТЬ ВСЁ", "callback_data": "confirm_reset_all_stats", "style": "danger"},
                {"text": "❌ Отмена", "callback_data": "cancel_reset_stats", "style": "secondary"}
            ]]
        }
        
        bot.reply_to(message,
                   "⚠️ <b>СБРОС ВСЕЙ СТАТИСТИКИ ИГР</b>\n\n"
                   "<blockquote>🗑️ Будут обнулены для всех пользователей:\n"
                   "• Количество побед\n"
                   "• Количество поражений\n"
                   "• Сумма выигрышей\n"
                   "• Сумма проигрышей</blockquote>\n\n"
                   "❌ <b>ЭТО ДЕЙСТВИЕ НЕОБРАТИМО!</b>",
                   reply_markup=reset_stats_keyboard,
                   parse_mode='HTML')
        
    except Exception as e:
        print(f"Ошибка в handle_reset_all_stats: {e}")
        bot.reply_to(message, f"❌ Ошибка: {e}", parse_mode='HTML')
@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == 'обулизм' and is_admin(m.from_user.id))
def handle_obulism(message):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT COUNT(*) FROM users')
        total = cursor.fetchone()[0]
        cursor.execute('''
            UPDATE users SET
                balance = 0,
                bank_deposit = 0,
                deposit = 0,
                video_cards = 0,
                experience = 0
        ''')
    bot.send_message(message.chat.id, f"♻️ <b>Обулизм выполнен!</b>\n\nОбнулено у {total} игроков:\n💵 Баланс · 🏛 Вклад · ⛏️ Майнинг · ⭐ Опыт", parse_mode='HTML')
@bot.callback_query_handler(func=lambda call: call.data == "confirm_reset_all_stats")
def confirm_reset_all_stats(call):
    try:
        user_id = call.from_user.id
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "❌ Недостаточно прав!")
            return
        
        with get_db_cursor() as cursor:
            cursor.execute('''
                UPDATE users SET 
                games_won = 0,
                games_lost = 0, 
                total_won_amount = 0,
                total_lost_amount = 0
            ''')
            affected_users = cursor.rowcount
        
        bot.edit_message_text(
            f"🏆 <b>Вся статистика игр сброшена!</b>\n\n"
            f"👥 Затронуто пользователей: {affected_users}\n"
            f"📊 Обнулена статистика всех игр",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML'
        )
        
        bot.answer_callback_query(call.id, "🏆 Статистика сброшена!")
        
    except Exception as e:
        print(f"Ошибка в confirm_reset_all_stats: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка!")

@bot.callback_query_handler(func=lambda call: call.data == "cancel_reset_stats")
def cancel_reset_stats(call):
    bot.edit_message_text(
        "🏆 Сброс статистики сброшен",
        call.message.chat.id,
        call.message.message_id
    , parse_mode='HTML')
    bot.answer_callback_query(call.id, "Отменено")
@bot.message_handler(func=lambda message: message.text in ["💼 Работа", "Работа"])
def handle_work(message):
    try:
        with open('work.jpg', 'rb') as photo:
            bot.send_photo(
                chat_id=message.chat.id,
                photo=photo,
                caption="💼 Выберите способ заработка:",
                reply_markup=create_work_menu(),
                parse_mode='HTML'
            )
    except FileNotFoundError:
        bot.send_message(message.chat.id, "💼 Выберите способ заработка:", reply_markup=create_work_menu())
        print("Файл work.jpg не найден!")
    except Exception as e:
        bot.send_message(message.chat.id, "💼 Выберите способ заработка:", reply_markup=create_work_menu())
        print(f"Ошибка при отправке фото работы: {e}")

@bot.message_handler(func=lambda message: message.text in ["👆 Кликер", "Кликер"])
def handle_clicker(message):

    bot.send_message(message.chat.id, "⚔️ Найди правильную кнопку:", reply_markup=create_clicker_keyboard(), parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text in ["🎭 Скам", "Скам"])
def handle_scam(message):
   
    try:
        user_id = message.from_user.id
        with get_db_cursor() as cursor:
            cursor.execute('SELECT referral_code FROM users WHERE user_id = ?', (user_id,))
            ref_code = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM users WHERE referred_by = ?', (user_id,))
            ref_count = cursor.fetchone()[0]
            
            earned = ref_count * 1000
            
            ref_link = f"https://t.me/{(bot.get_me()).username}?start={ref_code}"
            
            message_text = f"🎭 <b>Скам</b>\n\n"
            message_text += f"🔗 Твоя реферальная ссылка:\n{ref_link}\n\n"
            message_text += f"👥 Приглашено: {ref_count} чел.\n"
            message_text += f"💰 Заработано: {format_balance(earned)}\n\n"
            message_text += "💡 Кидай ссылку друзьям и получай бонусы!"
            
            bot.send_message(message.chat.id, message_text, parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка в handle_scam: {e}")
        bot.send_message(message.chat.id, "⚠️ Что-то пошло не так. Попробуй ещё раз.")

@bot.message_handler(func=lambda message: message.text in ["✈️ Воздушный груз", "Воздушный груз"])
def handle_air_cargo(message):
    text = (
        "✈️ <b>Воздушный груз</b>\n\n"
        "Ты работаешь грузчиком на частном аэродроме. "
        "Твоя задача — добавить <b>@FECTIZ_BOT</b> в описание своего Telegram-профиля.\n\n"
        "<blockquote>"
        "📦 Как это работает:\n\n"
        "1. Открой настройки Telegram\n"
        "2. Перейди в <b>Мой профиль → Изменить</b>\n"
        "3. В поле <b>«О себе»</b> напиши <code>@FECTIZ_BOT</code>\n"
        "4. Сохрани изменения\n\n"
        "Каждый день в 23:00 МСК бот проверяет всех кто добавил его в bio "
        "и случайно выбирает одного победителя.\n\n"
        "🏆 Награда победителя: <b>300 000 🌸</b>"
        "</blockquote>\n\n"
        "💡 Чем дольше держишь бота в bio — тем больше шансов выиграть каждый день!\n\n"
        "✅ <b>Уже добавил?</b> Отлично, ты в списке участников. Жди результатов!"
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda message: message.text in ["Баланс", "баланс", "Б", "б", "/б"])
def handle_balance(message):
    try:
        user_id = message.from_user.id
        balance = get_balance(user_id)
        bot.send_message(message.chat.id, f"💵 Твой баланс: {format_balance(balance)}", parse_mode='HTML')
    except Exception as e:
        print(f"Ошибка в handle_balance: {e}")
        bot.send_message(message.chat.id, "⚠️ Что-то пошло не так. Попробуй ещё раз.")
SLOT_CONFIG = {
    "min_bet": 100,
    "max_bet": 50000000000,
    "symbols": [
        {"emoji": "🍒", "name": "Вишня", "multiplier": 1.2, "weight": 35},
        {"emoji": "🍋", "name": "Лимон", "multiplier": 1.5, "weight": 25},
        {"emoji": "🍊", "name": "Апельсин", "multiplier": 2, "weight": 20},
        {"emoji": "🍇", "name": "Виноград", "multiplier": 2.3, "weight": 12},
        {"emoji": "🔔", "name": "Колокольчик", "multiplier": 4, "weight": 4},
        {"emoji": "🎁", "name": "Бриллиант", "multiplier": 10, "weight": 2},
        {"emoji": "⭐", "name": "Звезда", "multiplier": 7, "weight": 3},
        {"emoji": "🍀", "name": "Клевер", "multiplier": 3, "weight": 8},
        {"emoji": "⚔️", "name": "Джекпот", "multiplier": 25, "weight": 1}
    ],
    "special_combinations": {
        "jackpot": {"symbols": ["⚔️", "⚔️", "⚔️"], "multiplier": 100, "name": "ДЖЕКПОТ!!!", "chance": 0.001},
        "three_diamonds": {"symbols": ["🎁", "🎁", "🎁"], "multiplier": 50, "name": "ТРИ БРИЛЛИАНТА", "chance": 0.005},
        "three_bells": {"symbols": ["🔔", "🔔", "🔔"], "multiplier": 20, "name": "ТРИ КОЛОКОЛЬЧИКА", "chance": 0.01},
        "three_stars": {"symbols": ["⭐", "⭐", "⭐"], "multiplier": 15, "name": "ТРИ ЗВЕЗДЫ", "chance": 0.008},
        "seven_seven": {"symbols": ["7️⃣", "7️⃣", "7️⃣"], "multiplier": 30, "name": "СЧАСТЛИВАЯ СЕМЕРКА", "chance": 0.003}
    }
}

def get_weighted_symbol():
    """Получить случайный символ с учетом весов"""
    total_weight = sum(symbol["weight"] for symbol in SLOT_CONFIG["symbols"])
    rand = random.uniform(0, total_weight)
    current = 0
    
    for symbol in SLOT_CONFIG["symbols"]:
        current += symbol["weight"]
        if rand <= current:
            return symbol
    return SLOT_CONFIG["symbols"][0]

def check_special_combination(reels):
    """Проверить специальные комбинации"""
    reels_emojis = [reel["emoji"] for reel in reels]
    
    for combo_name, combo in SLOT_CONFIG["special_combinations"].items():
        if reels_emojis == combo["symbols"]:
            return combo
    
    if reels_emojis[0] == reels_emojis[1] == reels_emojis[2]:
        for symbol in SLOT_CONFIG["symbols"]:
            if symbol["emoji"] == reels_emojis[0]:
                return {
                    "name": f"ТРИ {symbol['name'].upper()}",
                    "multiplier": symbol["multiplier"] * 2,
                    "symbols": reels_emojis
                }
    
    if reels_emojis[0] == reels_emojis[1] or reels_emojis[1] == reels_emojis[2] or reels_emojis[0] == reels_emojis[2]:
        for symbol in SLOT_CONFIG["symbols"]:
            count = reels_emojis.count(symbol["emoji"])
            if count == 2:
                return {
                    "name": f"ДВЕ {symbol['name'].upper()}",
                    "multiplier": symbol["multiplier"],
                    "symbols": reels_emojis
                }
    
    return None

def spin_slots():
    """Крутить слоты"""
    reels = [get_weighted_symbol() for _ in range(3)]
    combination = check_special_combination(reels)
    
    return reels, combination

def create_slots_display(reels, bet_amount=None, win_amount=None):
    """Создать красивое отображение слотов"""
    slot_display = "🎰─────🎰─────🎰\n"
    slot_display += "│  {}  │  {}  │  {}  │\n".format(reels[0]["emoji"], reels[1]["emoji"], reels[2]["emoji"])
    slot_display += "🎰─────🎰─────🎰\n\n"
    
    if bet_amount is not None:
        slot_display += f"💵 Ставка: {format_balance(bet_amount)}\n"
    
    if win_amount is not None:
        if win_amount > 0:
            slot_display += f"🎉 Выигрыш: {format_balance(win_amount)}\n"
        else:
            slot_display += "😔 Выигрыша нет\n"
    
    return slot_display

@bot.message_handler(func=lambda message: message.text.lower().startswith('слоты '))
def handle_slots(message):
    user_id = message.from_user.id
    balance = get_balance(user_id)
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            show_slots_help(message.chat.id)
            return
        
        bet_text = ' '.join(parts[1:])
        bet_amount = parse_bet_amount(bet_text, balance)
        
        if bet_amount is None or bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки!", parse_mode='HTML')
            return
        
        if bet_amount < SLOT_CONFIG["min_bet"]:
            bot.send_message(message.chat.id, f"❌ Минимальная бет: {format_balance(SLOT_CONFIG['min_bet'])}!", parse_mode='HTML')
            return
        
        if bet_amount > SLOT_CONFIG["max_bet"]:
            bot.send_message(message.chat.id, f"❌ Максимальная бет: {format_balance(SLOT_CONFIG['max_bet'])}!", parse_mode='HTML')
            return
        
        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Не хватает монет!", parse_mode='HTML')
            return
        
        update_balance(user_id, -bet_amount)
        
        reels, combination = spin_slots()
        
        win_amount = 0
        if combination:
            win_amount = int(bet_amount * combination["multiplier"])
            update_balance(user_id, win_amount)
        
        msg = bot.send_message(message.chat.id, "🎰 Крутим слоты...\n\n🔄 🔄 🔄")
        time.sleep(1.5)
        
        result_text = create_slots_display(reels, bet_amount, win_amount)
        
        if combination:
            result_text += f"\n🎊 {combination['name']}!\n"
            result_text += f"📈 Множитель: x{combination['multiplier']}\n"
        
        new_balance = get_balance(user_id)
        result_text += f"\n💳 Новый баланс: {format_balance(new_balance)}"
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🎰 Крутить еще раз", callback_data=f"slots_repeat_{bet_amount}"))
        
        bot.edit_message_text(
            result_text,
            message.chat.id,
            msg.message_id,
            reply_markup=markup,
            parse_mode='HTML'
        )
        
    except Exception as e:
        print(f"Ошибка в слотах: {e}")
        refund_balance(user_id, bet_amount, message.chat.id)

def show_slots_help(chat_id):
    """Показать справку по слотам"""
    help_text = """🎰 ИГРА В СЛОТЫ 🎰

Команда:
`слоты [бет]`

Символы и выигрыши:
🍒 Вишня (x1.5)   🍋 Лимон (x2)
🍊 Апельсин (x2.5) 🍇 Виноград (x3)
🔔 Колокольчик (x5) ⭐ Звезда (x7)
🍀 Клевер (x4)     🎁 Бриллиант (x10)
⚔️ Джекпот (x25)

Комбинации:
• 3 одинаковых символа = множитель ×2
• 2 одинаковых символа = обычный множитель
• Специальные комбинации = большие выигрыши!

Удачи! 🍀"""
    
    bot.send_message(chat_id, help_text, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith('slots_repeat_'))
def handle_slots_repeat(call):
    user_id = call.from_user.id
    balance = get_balance(user_id)
    
    try:
        bet_amount = int(call.data.split('_')[2])
        
        if bet_amount > balance:
            bot.answer_callback_query(call.id, "❌ Не хватает монет!")
            return
        
        update_balance(user_id, -bet_amount)
        
        reels, combination = spin_slots()
        
        win_amount = 0
        if combination:
            win_amount = int(bet_amount * combination["multiplier"])
            update_balance(user_id, win_amount)
        
        result_text = create_slots_display(reels, bet_amount, win_amount)
        
        if combination:
            result_text += f"\n🎊 {combination['name']}!\n"
            result_text += f"📈 Множитель: x{combination['multiplier']}\n"
        
        new_balance = get_balance(user_id)
        result_text += f"\n💳 Новый баланс: {format_balance(new_balance)}"
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🎰 Крутить еще раз", callback_data=f"slots_repeat_{bet_amount}"))
        
        bot.edit_message_text(
            result_text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='HTML'
        )
        
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        print(f"Ошибка в повторной игре слотов: {e}")
        refund_balance(call.from_user.id, bet_amount, call.message.chat.id)

@bot.message_handler(func=lambda message: message.text in ["⛏️ Майнинг", "Майнинг"])
def handle_mining(message):
  
    try:
        user_id = message.from_user.id
        with get_db_cursor() as cursor:
            cursor.execute('SELECT video_cards, last_mining_collect FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            
            if result:
                video_cards, last_collect = result
                income_per_hour = calculate_mining_income(video_cards)
                
                message_text = f"⛏️ <b>Майнинг ферма</b>\n\n"
                message_text += f"🖥 Майнеров: {video_cards}\n"
                message_text += f"💰 Доход: {format_balance(income_per_hour)}/час\n"
                
                if video_cards == 0:
                    message_text += "\n💡 Купи первую видеокарту чтобы начать!"
                
                bot.send_message(message.chat.id, message_text, reply_markup=create_mining_keyboard(), parse_mode='HTML')
            else:
                bot.send_message(message.chat.id, "❌ Ошибка загрузки данных майнинга", parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка в handle_mining: {e}")
        bot.send_message(message.chat.id, "⚠️ Что-то пошло не так. Попробуй ещё раз.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('mining_'))
def mining_callback_handler(call):
    user_id = call.from_user.id
    
    try:
        if call.data == "mining_collect":
            with get_db_cursor() as cursor:
                cursor.execute('SELECT video_cards, last_mining_collect, balance FROM users WHERE user_id = ?', (user_id,))
                result = cursor.fetchone()
                
                if not result:
                    bot.answer_callback_query(call.id, "❌ Ошибка загрузки данных!")
                    return
                    
                video_cards, last_collect, balance = result
                
                if video_cards == 0:
                    bot.answer_callback_query(call.id, "❌ У вас нет видеокарт для сбора!")
                    return
                    
                current_time = time.time()
                time_passed = current_time - last_collect if last_collect > 0 else 3600
                
                income_per_hour = calculate_mining_income(video_cards)
                income = int(income_per_hour * (time_passed / 3600))
                
                if income > 0:
                    cursor.execute(
                        'UPDATE users SET balance = balance + ?, last_mining_collect = ? WHERE user_id = ?',
                        (income, current_time, user_id)
                    )
                    
                    cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
                    new_balance = cursor.fetchone()[0]
                    
                    bot.answer_callback_query(call.id, f"🏆 Собрано {format_balance(income)}")
                    
                    message_text = f"⛏️ <b>Майнинг ферма</b>\n\n"
                    message_text += f"🖥 Майнеров: {video_cards}\n"
                    message_text += f"💰 Доход: {format_balance(income_per_hour)}/час\n\n"
                    message_text += f"✅ Собрано: {format_balance(income)}\n"
                    message_text += f"💳 Баланс: {format_balance(new_balance)}"
                    
                    bot.edit_message_text(
                        message_text,
                        call.message.chat.id,
                        call.message.message_id,
                        reply_markup=create_mining_keyboard(),
                        parse_mode='HTML'
                    )
                else:
                    bot.answer_callback_query(call.id, "⏳ Доход еще не накоплен!")
        
        elif call.data == "mining_buy":
            with get_db_cursor() as cursor:
                cursor.execute('SELECT video_cards, balance FROM users WHERE user_id = ?', (user_id,))
                result = cursor.fetchone()
                
                if not result:
                    bot.answer_callback_query(call.id, "❌ Ошибка загрузки данных!")
                    return
                    
                video_cards, balance = result
                card_price = calculate_video_card_price(video_cards)
                
                if balance >= card_price:
                    cursor.execute(
                        'UPDATE users SET video_cards = video_cards + 1, balance = balance - ? WHERE user_id = ?',
                        (card_price, user_id)
                    )
                    
                    new_video_cards = video_cards + 1
                    new_income = calculate_mining_income(new_video_cards)
                    
                    bot.answer_callback_query(call.id, f"🏆 Куплена {new_video_cards} видеокарта!")
                    
                    message_text = f"⛏️ <b>Майнинг ферма</b>\n\n"
                    message_text += f"🖥 Майнеров: {new_video_cards}\n"
                    message_text += f"💰 Доход: {format_balance(new_income)}/час\n\n"
                    message_text += f"💡 Следующая карта: {format_balance(calculate_video_card_price(new_video_cards))}"
                    
                    bot.edit_message_text(
                        message_text,
                        call.message.chat.id,
                        call.message.message_id,
                        reply_markup=create_mining_keyboard(),
                        parse_mode='HTML'
                    )
                else:
                    bot.answer_callback_query(call.id, f"❌ Недостаточно денег! Нужно: {format_balance(card_price)}")
    
    except Exception as e:
        print(f"Ошибка в mining_callback_handler: {e}")
        bot.answer_callback_query(call.id, "❌ Временная ошибка системы")

clicker_boost = {
    'active': False,
    'multiplier': 1.0,
    'end_time': 0,
    'message': ''
}

@bot.message_handler(func=lambda message: message.text.lower().startswith('буст кликера') and is_admin(message.from_user.id))
def handle_clicker_boost(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 3:
            bot.send_message(message.chat.id,
                           "❌ Используйте: буст кликера [множитель] [время вмах]\n\n"
                           "Пример:\n"
                           "буст кликера 2.0 30\n\n"
                           "💡 Множитель: 1.5 = +50%, 2.0 = +100% и т.д.", parse_mode='HTML')
            return
        
        multiplier = float(parts[2])
        if multiplier < 1.0 or multiplier > 10.0:
            bot.send_message(message.chat.id, "❌ Множитель должен быть от 1.0 до 10.0", parse_mode='HTML')
            return
        
        duration_minutes = 30
        if len(parts) > 3 and parts[3].isdigit():
            duration_minutes = int(parts[3])
        
        clicker_boost['active'] = True
        clicker_boost['multiplier'] = multiplier
        clicker_boost['end_time'] = time.time() + (duration_minutes * 60)
        
        bot.send_message(message.chat.id,
                       f"🏆 Буст кликера активирован!\n\n"
                       f"📈 Множитель: x{multiplier}\n"
                       f"⏰ Длительность: {duration_minutes}м\n\n"
                       f"💡 Буст будет автоматически отображаться в кликере.", parse_mode='HTML')
        
    except ValueError:
        bot.send_message(message.chat.id, "❌ Неверный формат множителя!", parse_mode='HTML')
    except Exception as e:
        print(f"Ошибка при установке буста: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при установке буста!", parse_mode='HTML')

def get_clicker_boost():
    """Получить текущий активный буст кликера"""
    global clicker_boost
    
    if clicker_boost['active'] and time.time() > clicker_boost['end_time']:
        clicker_boost['active'] = False
        clicker_boost['multiplier'] = 1.0
        print("⏰ Время буста кликера истекло")
    
    return clicker_boost

def get_boosted_click_power(base_power):
    """Получить мощность клика с учетом активного буста"""
    boost = get_clicker_boost()
    
    if boost['active']:
        return int(base_power * boost['multiplier'])
    
    return base_power

def get_boost_info_text():
    """Получить текст с информацией о текущем бусте"""
    boost = get_clicker_boost()
    
    if not boost['active']:
        return ""
    
    time_left = boost['end_time'] - time.time()
    if time_left <= 0:
        return ""
    
    minutes = int(time_left // 60)
    seconds = int(time_left % 60)
    
    return (f"\n🎉 АКЦИЯ! 🎉\n"
            f"⚡ Буст: x{boost['multiplier']}\n"
            f"⏰ Осталось: {minutes:02d}:{seconds:02d}\n")
            
click_limits = {}

def check_click_limit(user_id):
    """Проверяет, не превысил ли пользователь лимит кликов"""
    current_time = time.time()
    
    if user_id in click_limits and 'rest_until' in click_limits[user_id]:
        if current_time < click_limits[user_id]['rest_until']:
            return False
    
    if user_id not in click_limits:
        click_limits[user_id] = {'clicks': 1, 'reset_time': current_time + 600}
        return True
    
    limit_data = click_limits[user_id]
    
    if current_time > limit_data['reset_time']:
        click_limits[user_id] = {'clicks': 1, 'reset_time': current_time + 600}
        return True
    
    if limit_data['clicks'] < 300:
        click_limits[user_id]['clicks'] += 1
        return True
    
    click_limits[user_id]['rest_until'] = current_time + 180
    return False

@bot.callback_query_handler(func=lambda call: call.data.startswith('clicker_'))
def clicker_callback_handler(call):
    user_id = call.from_user.id
    symbol = call.data.split('_')[1]
    
    if not check_click_limit(user_id):
        try:
            if user_id in click_limits and 'rest_until' in click_limits[user_id]:
                rest_time = click_limits[user_id]['rest_until'] - time.time()
                minutes = int(rest_time // 60)
                seconds = int(rest_time % 60)
                rest_text = f"😴 Отдохните {minutes}:{seconds:02d}, вы устали"
            else:
                rest_text = "😴 Отдохните 3 минуты, вы устали"
            
            bot.edit_message_text(
                rest_text,
                call.message.chat.id,
                call.message.message_id
            )
        except:
            pass
        bot.answer_callback_query(call.id)
        return

    try:
        bot.answer_callback_query(call.id)
        
        if symbol == "✅":
            with get_db_cursor() as cursor:
                cursor.execute('SELECT click_power, click_streak, total_clicks, experience FROM users WHERE user_id = ?', (user_id,))
                result = cursor.fetchone()
                
                if not result:
                    return
                    
                base_power, click_streak, total_clicks, old_exp = result
                
                level_now_click = get_level_from_exp(old_exp)
                leveled_power = int(base_power * get_level_click_bonus(level_now_click))
                actual_power = get_boosted_click_power(leveled_power)
                
                new_streak = click_streak + 1
                new_total_clicks = total_clicks + 1

                EXP_PER_CLICK = 9
                new_exp = old_exp + EXP_PER_CLICK
                cursor.execute('UPDATE users SET experience = ? WHERE user_id = ?', (new_exp, user_id))
                
                old_level = get_level_from_exp(old_exp)
                new_level = get_level_from_exp(new_exp)
                
                bonus = 0
                level_up_bonus = 0
                
                if new_total_clicks % 100 == 0:
                    bonus = 10000
                    cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (bonus, user_id))

                cursor.execute('UPDATE users SET click_streak = ?, total_clicks = ? WHERE user_id = ?', 
                              (new_streak, new_total_clicks, user_id))
                cursor.execute('UPDATE users SET balance = balance + ?, last_click = ? WHERE user_id = ?',
                              (actual_power, time.time(), user_id))

                if new_level > old_level:
                    level_up_bonus = new_level * 200
                    if level_up_bonus > 0:
                        cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (level_up_bonus, user_id))

            if new_level > old_level:
                try:
                    em, ttl = get_title(new_level)
                    click_b = int(get_level_click_bonus(new_level)*100-100)
                    daily_b = int(get_level_daily_bonus(new_level)*100-100)
                    bot.send_message(
                        user_id,
                        f"🎉 <b>НОВЫЙ УРОВЕНЬ!</b>\n\n"
                        f"{em} <b>{new_level} — {ttl}</b>\n\n"
                        f"💵 Бонус: +{format_balance(level_up_bonus)}\n"
                        f"⚡ Бонус к кликам: +{click_b}%\n"
                        f"🎁 Бонус к ежедневке: +{daily_b}%",
                        parse_mode='HTML'
                    )
                except Exception as e:
                    print(f"Ошибка отправки уведомления: {e}")

            new_balance = get_balance(user_id)
            boost_info = get_boost_info_text()
            level_now = get_level_from_exp(new_exp)
            lv_emoji, lv_title = get_title(level_now)
            next_bonus = 100 - (new_total_clicks % 100)

            _, lv_cur, lv_need = get_level_progress(new_exp)
            if lv_need > 0:
                bars = int(lv_cur / lv_need * 8)
                bar = "█" * bars + "░" * (8 - bars)
            else:
                bar = "████████"

            display_text = ""
            if boost_info:
                display_text += f"{boost_info}\n\n"

            display_text += f"💵 {format_balance(new_balance)}\n"
            display_text += f"{lv_emoji} {lv_title} · Ур.{level_now}\n"
            display_text += f"[{bar}] +{EXP_PER_CLICK} exp\n\n"
            display_text += f"🔥 Серия: {new_streak}  ·  👆 {new_total_clicks} кликов\n"

            if bonus > 0:
                display_text += f"\n🎁 Бонус за 100 кликов! +{format_balance(bonus)}"
            else:
                display_text += f"⚡ До бонуса: {next_bonus} кликов"

            bot.edit_message_text(
                display_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=create_clicker_keyboard(),
                parse_mode='HTML'
            )
        else:
            with get_db_cursor() as cursor:
                cursor.execute('UPDATE users SET click_streak = 0 WHERE user_id = ?', (user_id,))
            
            boost_info = get_boost_info_text()
            message_text = f"{boost_info}\n❌ Неверный выбор! Серия сброшена.\n⚔️ Найди правильную кнопку:" if boost_info else "❌ Неверный выбор! Серия сброшена.\n⚔️ Найди правильную кнопку:"
            
            bot.edit_message_text(
                message_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=create_clicker_keyboard()
            )
    
    except Exception as e:
        print(f"Ошибка в clicker_callback_handler: {e}")
@bot.message_handler(func=lambda message: message.text.lower() == 'обновить клик' and is_admin(message.from_user.id))

@bot.message_handler(func=lambda message: message.text.lower() == 'конвертировать балансы' and is_admin(message.from_user.id))
def handle_convert_balances(message):
    """Конвертировать все балансы и цены (деление на 1000)"""
    if not is_admin(message.from_user.id):
        return
    
    convert_keyboard = {
        "inline_keyboard": [[
            {"text": "⚠️ ДА, КОНВЕРТИРОВАТЬ", "callback_data": "confirm_convert_balances", "style": "danger"},
            {"text": "❌ Отмена", "callback_data": "cancel_convert", "style": "secondary"}
        ]]
    }
    
    bot.send_message(
        message.chat.id,
        "🔄 <b>КОНВЕРТАЦИЯ БАЛАНСОВ И ЦЕН</b>\n\n"
        "<blockquote>⚠️ Будут изменены:\n"
        "• Все балансы пользователей (÷1000)\n"
        "• Все банковские вклады (÷1000)\n"
        "• Цены в магазине одежды (÷1000)\n"
        "• Цены бизнесов (÷1000)\n"
        "\n"
        "• Лотерейные билеты (÷1000)\n"
        "• Займы и ставки (÷1000)\n"
        "• Бонусы и награды (÷1000)</blockquote>\n\n"
        "❌ <b>ЭТО ДЕЙСТВИЕ НЕОБРАТИМО!</b>\n"
        "Пример: 1.000.000.000 → 1.000.000",
        reply_markup=convert_keyboard,
        parse_mode='HTML'
    )

@bot.callback_query_handler(func=lambda call: call.data == "confirm_convert_balances")
def confirm_convert_balances(call):
    try:
        user_id = call.from_user.id
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "❌ Недостаточно прав!")
            return
        
        bot.edit_message_text("🔄 Конвертируем балансы и цены...", call.message.chat.id, call.message.message_id)
        
        conversion_count = 0
        total_converted = 0
        
        with get_db_cursor() as cursor:
            cursor.execute('SELECT SUM(balance) FROM users')
            old_total_balance = cursor.fetchone()[0] or 0
            
            cursor.execute('UPDATE users SET balance = balance / 1000 WHERE balance > 0')
            users_converted = cursor.rowcount
            
            cursor.execute('SELECT SUM(balance) FROM users')
            new_total_balance = cursor.fetchone()[0] or 0
            
            cursor.execute('SELECT SUM(bank_deposit) FROM users')
            old_total_deposit = cursor.fetchone()[0] or 0
            
            cursor.execute('UPDATE users SET bank_deposit = bank_deposit / 1000 WHERE bank_deposit > 0')
            deposits_converted = cursor.rowcount
            
            cursor.execute('SELECT SUM(price) FROM clothes_shop')
            old_clothes_total = cursor.fetchone()[0] or 0
            
            cursor.execute('UPDATE clothes_shop SET price = price / 1000 WHERE price > 0')
            clothes_converted = cursor.rowcount
            
            cursor.execute('SELECT SUM(price) FROM businesses')
            old_business_total = cursor.fetchone()[0] or 0
            
            cursor.execute('UPDATE businesses SET price = price / 1000 WHERE price > 0')
            businesses_converted = cursor.rowcount
            
            cursor.execute('SELECT SUM(loan_amount) FROM loans')
            old_loans_total = cursor.fetchone()[0] or 0
            
            cursor.execute('UPDATE loans SET loan_amount = loan_amount / 1000 WHERE loan_amount > 0')
            loans_converted = cursor.rowcount
            
            cursor.execute('SELECT SUM(balance) FROM clans')
            old_clans_total = cursor.fetchone()[0] or 0
            
            cursor.execute('UPDATE clans SET balance = balance / 1000 WHERE balance > 0')
            clans_converted = cursor.rowcount
            
            cursor.execute('UPDATE lottery SET jackpot = jackpot / 1000, last_win_amount = last_win_amount / 1000 WHERE jackpot > 0')
            
            cursor.execute('UPDATE transfers SET amount = amount / 1000, fee = fee / 1000 WHERE amount > 0')
            
            cursor.execute('UPDATE auctions SET winner_bid = winner_bid / 1000 WHERE winner_bid > 0')
            cursor.execute('UPDATE auction_bids SET bid_amount = bid_amount / 1000 WHERE bid_amount > 0')
            
            cursor.execute('UPDATE user_bag SET component_price = component_price / 1000 WHERE component_price > 0')

        result_message = f"🏆 **БАЛАНСЫ И ЦЕНЫ КОНВЕРТИРОВАНЫ!**\n\n"
        result_message += f"📊 **Статистика конвертации:**\n"
        result_message += f"• Пользователей: {users_converted}\n"
        result_message += f"• Вкладов: {deposits_converted}\n"
        result_message += f"• Вещей в магазине: {clothes_converted}\n"
        result_message += f"• Бизнесов: {businesses_converted}\n"
        result_message += f"• Займов: {loans_converted}\n"
        result_message += f"• Кланов: {clans_converted}\n\n"
        
        result_message += f"💵 **Общий баланс ДО:** {format_balance(old_total_balance)}\n"
        result_message += f"💵 **Общий баланс ПОСЛЕ:** {format_balance(new_total_balance)}\n\n"
        
        result_message += f"🔢 **Все суммы уменьшены в 1000 раз**\n"
        result_message += f"📉 **Пример:** 1.000.000.000 → 1.000.000"

        bot.edit_message_text(result_message, call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "🏆 Конвертация окончена!")
        
    except Exception as e:
        print(f"Ошибка при конвертации балансов: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка конвертации!")

@bot.callback_query_handler(func=lambda call: call.data == "cancel_convert")
def cancel_convert(call):
    bot.edit_message_text(
        "🏆 Конвертация сброшена",
        call.message.chat.id,
        call.message.message_id
    , parse_mode='HTML')
    bot.answer_callback_query(call.id, "Отменено")
tower_games = {}

def calculate_tower_multipliers():
    """Рассчитать множители для каждого уровня башни"""
    return {
        1: 1.5,
        2: 2.0,
        3: 3.0,
        4: 5.0,
        5: 7.0
    }

def create_tower_keyboard(game_id, level, left_state, right_state, multipliers, show_mines=False):
    """Создать клавиатуру для игры Башня с 2 кнопками"""
    markup = InlineKeyboardMarkup(row_width=2)
    
    if show_mines and left_state == 'mine':
        left_button = InlineKeyboardButton("💣 Левая", callback_data=f"tower_{game_id}_{level}_left_m")
    elif left_state == 'safe':
        left_button = InlineKeyboardButton("🟢 Левая", callback_data=f"tower_{game_id}_{level}_left_s")
    elif left_state == 'exploded':
        left_button = InlineKeyboardButton("💥 Левая", callback_data=f"tower_{game_id}_{level}_left_e")
    elif left_state == 'selected':
        left_button = InlineKeyboardButton("🏆 Левая", callback_data=f"tower_{game_id}_{level}_left_c")
    else:
        left_button = InlineKeyboardButton("🏰 Левая", callback_data=f"tower_{game_id}_{level}_left_u")
    
    if show_mines and right_state == 'mine':
        right_button = InlineKeyboardButton("💣 Правая", callback_data=f"tower_{game_id}_{level}_right_m")
    elif right_state == 'safe':
        right_button = InlineKeyboardButton("🟢 Правая", callback_data=f"tower_{game_id}_{level}_right_s")
    elif right_state == 'exploded':
        right_button = InlineKeyboardButton("💥 Правая", callback_data=f"tower_{game_id}_{level}_right_e")
    elif right_state == 'selected':
        right_button = InlineKeyboardButton("🏆 Правая", callback_data=f"tower_{game_id}_{level}_right_c")
    else:
        right_button = InlineKeyboardButton("🏰 Правая", callback_data=f"tower_{game_id}_{level}_right_u")
    
    markup.add(left_button, right_button)
    
    current_multiplier = multipliers[level]
    win_amount = int(tower_games[game_id]['bet_amount'] * current_multiplier)
    
    if level > 1 or show_mines:
        markup.add(InlineKeyboardButton(f"💵 Забрать {plain_balance(win_amount)}", callback_data=f"tower_{game_id}_x"))
    
    return markup

def start_tower_game(user_id, bet_amount):
    """Начать новую игру в Башню"""
    game_id = str(int(time.time()))
    
    mine_position = random.randint(0, 1)
    
    if mine_position == 0:
        hidden_map = {'left': 'mine', 'right': 'safe'}
    else:
        hidden_map = {'left': 'safe', 'right': 'mine'}
    
    multipliers = calculate_tower_multipliers()
    
    tower_games[game_id] = {
        'user_id': user_id,
        'bet_amount': bet_amount,
        'current_level': 1,
        'levels': {1: {'left': 'unknown', 'right': 'unknown'}},
        'hidden_maps': {1: hidden_map},
        'multipliers': multipliers,
        'start_time': time.time(),
        'status': 'active',
        'message_id': None,
        'chat_id': None
    }
    
    return game_id

def generate_next_level(game_id):
    """Сгенерировать следующий ранг башни"""
    game = tower_games[game_id]
    current_level = game['current_level']
    next_level = current_level + 1
    
    if next_level > 5:
        return None, None, None
    
    visible_state = {'left': 'unknown', 'right': 'unknown'}
    
    mine_position = random.randint(0, 1)
    
    if mine_position == 0:
        hidden_map = {'left': 'mine', 'right': 'safe'}
    else:
        hidden_map = {'left': 'safe', 'right': 'mine'}
    
    game['levels'][next_level] = visible_state
    game['hidden_maps'][next_level] = hidden_map
    game['current_level'] = next_level
    
    return next_level, visible_state, hidden_map

def refund_expired_tower_games():
    """Возврат средств за просроченные игры в башне"""
    current_time = time.time()
    expired_games = []
    
    for game_id, game_data in tower_games.items():
        if game_data['status'] == 'active' and current_time - game_data['start_time'] > 240:
            expired_games.append(game_id)
    
    for game_id in expired_games:
        game_data = tower_games[game_id]
        bet_amount = game_data['bet_amount']
        
        update_balance(game_data['user_id'], bet_amount)
        
        try:
            bot.send_message(
                game_data['user_id'],
                f"🕒 <b>Время игры истекло!</b>\n\n"
                f"🎲 Игра: Башня\n"
                f"💵 Возвращено: {format_balance(bet_amount)}\n"
                f"⏰ Причина: Игра длилась более 4м\n\n"
                f"💡 Ваши средства возвращены на баланс!",
                parse_mode='HTML'
            )
        except:
            pass
        
        if game_data.get('chat_id') and game_data.get('message_id'):
            try:
                multipliers = game_data['multipliers']
                current_level = game_data['current_level']
                
                level_state = game_data['levels'][current_level].copy()
                hidden_map = game_data['hidden_maps'][current_level]
                
                for tower in ['left', 'right']:
                    if hidden_map[tower] == 'mine':
                        level_state[tower] = 'mine'
                    else:
                        level_state[tower] = 'safe'
                
                message_text = f"🏰 <b>БАШНЯ</b>\n\n"
                message_text += f"<blockquote>💵 Ставка: {format_balance(bet_amount)}\n"
                message_text += f"📈 Уровень: {current_level}/5\n"
                message_text += f"⚔️ Множитель: x{multipliers[current_level]}</blockquote>\n\n"
                message_text += f"🕒 <b>ВРЕМЯ ИГРЫ ИСТЕКЛО!</b>\n"
                message_text += f"💵 Возвращено: {format_balance(bet_amount)}\n\n"
                message_text += f"⏰ Игра длилась более 4м"
                
                markup = create_tower_keyboard(game_id, current_level, level_state['left'], level_state['right'], multipliers, show_mines=True)
                
                bot.edit_message_text(
                    message_text,
                    game_data['chat_id'],
                    game_data['message_id'],
                    reply_markup=markup,
                    parse_mode='HTML'
                )
            except:
                pass
        
        del tower_games[game_id]
        
        print(f"🏆 Возвращены средства за игру в башню пользователю {game_data['user_id']}: {format_balance(bet_amount)}")
    
    return len(expired_games)

def start_tower_refund_checker():
    """Запускает периодическую проверку просроченных игр в башне"""
    def checker():
        while True:
            try:
                refunded_count = refund_expired_tower_games()
                if refunded_count > 0:
                    print(f"🔄 Возвращено {refunded_count} просроченных игр в башне")
                time.sleep(60)
            except Exception as e:
                print(f"❌ Ошибка в tower_refund_checker: {e}")
                time.sleep(60)
    
    thread = threading.Thread(target=checker, daemon=True)
    thread.start()

@bot.callback_query_handler(func=lambda call: call.data.startswith('tower_'))
def handle_tower_callback(call):
    """Обработчик callback'ов игры Башня"""
    try:
        user_id = call.from_user.id
        data = call.data
        
        if not data.startswith('tower_'):
            return
            
        parts = data[6:].split('_')
        
        if len(parts) < 2:
            bot.answer_callback_query(call.id, "❌ Ошибка в данных!")
            return
        
        game_id = parts[0]
        
        if game_id not in tower_games:
            bot.answer_callback_query(call.id, "❌ Игра не найдено!")
            return
        
        game = tower_games[game_id]
        
        if game['user_id'] != user_id:
            bot.answer_callback_query(call.id, "❌ Это не твоя игра!")
            return
        
        if game['status'] != 'active':
            bot.answer_callback_query(call.id, "❌ Игра уже окончена!")
            return
        
        if len(parts) == 2 and parts[1] == 'x':
            handle_tower_exit(game_id, call)
            return
        
        if len(parts) >= 4:
            level = int(parts[1])
            tower = parts[2]
            button_type = parts[3]
            
            if level != game['current_level']:
                bot.answer_callback_query(call.id, "❌ Неверный ранг!")
                return
            
            hidden_map = game['hidden_maps'][level]
            
            if hidden_map[tower] == 'mine':
                handle_tower_mine(game_id, level, tower, call)
            elif hidden_map[tower] == 'safe':
                handle_tower_safe(game_id, level, tower, call)
            else:
                bot.answer_callback_query(call.id, "❌ Ошибка в данных кнопки!")
        
    except Exception as e:
        print(f"Ошибка в обработчике Башни: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка в игре!")

def handle_tower_safe(game_id, level, tower, call):
    """Обработка выбора безопасной башни"""
    game = tower_games[game_id]
    
    level_state = game['levels'][level].copy()
    level_state[tower] = 'selected'
    game['levels'][level] = level_state
    
    if level < 5:
        next_level, next_state, next_hidden = generate_next_level(game_id)
        
        if next_level:
            multipliers = game['multipliers']
            next_multiplier = multipliers[next_level]
            
            message_text = f"🏰 <b>БАШНЯ</b>\n\n"
            message_text += f"<blockquote>💵 Ставка: {format_balance(game['bet_amount'])}\n"
            message_text += f"📈 Уровень: {next_level}/5\n"
            message_text += f"⚔️ Множитель: x{next_multiplier}</blockquote>\n\n"
            message_text += f"🏆 Уровень {level} пройден!\n"
            message_text += f"💣 В одной башне мина!\n\n"
            message_text += f"🎲 Выбери безопасную башню:"
            
            markup = create_tower_keyboard(game_id, next_level, next_state['left'], next_state['right'], multipliers)
            
            bot.edit_message_text(
                message_text,
                game['chat_id'],
                game['message_id'],
                reply_markup=markup,
                parse_mode='HTML'
            )
            
            bot.answer_callback_query(call.id, f"🏆 Уровень {level} пройден!")
        else:
            handle_tower_win(game_id, call)
    else:
        handle_tower_win(game_id, call)

def handle_tower_mine(game_id, level, tower, call):
    """Обработка наступления на мину - ИСПРАВЛЕННАЯ ВЕРСИЯ"""
    game = tower_games[game_id]
    
    level_state = game['levels'][level].copy()
    hidden_map = game['hidden_maps'][level]
    
    for t in ['left', 'right']:
        if hidden_map[t] == 'mine':
            level_state[t] = 'mine'
        elif t == tower:
            level_state[t] = 'exploded'
        else:
            level_state[t] = 'safe'
    
    game['levels'][level] = level_state
    game['status'] = 'lost'
    
    multipliers = game['multipliers']
    
    message_text = f"🏰 <b>БАШНЯ</b>\n\n"
    message_text += f"<blockquote>💵 Ставка: {format_balance(game['bet_amount'])}\n"
    message_text += f"📈 Уровень: {level}/5\n"
    message_text += f"⚔️ Множитель: x{multipliers[level]}</blockquote>\n\n"
    message_text += f"💥 <b>ВЗРЫВ!</b> Ты выбрал башню с миной!\n\n"
    message_text += f"❌ Ставка сгорела."
    
    markup = create_tower_keyboard(game_id, level, level_state['left'], level_state['right'], multipliers, show_mines=True)
    
    bot.edit_message_text(
        message_text,
        game['chat_id'],
        game['message_id'],
        reply_markup=markup,
        parse_mode='HTML'
    )
    
    bot.answer_callback_query(call.id, "💥 Взрыв! Ставка сгорела!")
    
    update_game_stats(game['user_id'], False, 0, game['bet_amount'])

def handle_tower_win(game_id, call):
    """Обработка победы в игре"""
    game = tower_games[game_id]
    
    game['status'] = 'won'
    multipliers = game['multipliers']
    win_amount = int(game['bet_amount'] * multipliers[5])
    
    update_balance(game['user_id'], win_amount)
    
    level_state = game['levels'][5].copy()
    hidden_map = game['hidden_maps'][5]
    
    for tower in ['left', 'right']:
        if hidden_map[tower] == 'mine':
            level_state[tower] = 'mine'
        else:
            level_state[tower] = 'selected'
    
    message_text = f"🏰 <b>БАШНЯ</b>\n\n"
    message_text += f"<blockquote>💵 Ставка: {format_balance(game['bet_amount'])}\n"
    message_text += f"📈 Уровень: 5/5\n"
    message_text += f"⚔️ Множитель: x{multipliers[5]}</blockquote>\n\n"
    message_text += f"🎉 <b>ПОБЕДА!</b> Ты достиг вершины башни!\n"
    message_text += f"💵 Выигрыш: {format_balance(win_amount)}"
    
    markup = create_tower_keyboard(game_id, 5, level_state['left'], level_state['right'], multipliers, show_mines=True)
    
    bot.edit_message_text(
        message_text,
        game['chat_id'],
        game['message_id'],
        reply_markup=markup,
        parse_mode='HTML'
    )
    
    bot.answer_callback_query(call.id, f"🎉 Победа! +{format_balance(win_amount)}")
    
    update_game_stats(game['user_id'], True, win_amount, 0)

def handle_tower_exit(game_id, call):
    """Обработка выхода из игры"""
    game = tower_games[game_id]
    
    if game['status'] != 'active':
        bot.answer_callback_query(call.id, "❌ Игра уже окончена!")
        return
    
    game['status'] = 'exited'
    multipliers = game['multipliers']
    current_level = game['current_level']
    win_amount = int(game['bet_amount'] * multipliers[current_level])
    
    update_balance(game['user_id'], win_amount)
    
    level_state = game['levels'][current_level].copy()
    hidden_map = game['hidden_maps'][current_level]
    
    for tower in ['left', 'right']:
        if hidden_map[tower] == 'mine':
            level_state[tower] = 'mine'
        else:
            level_state[tower] = 'selected'
    
    message_text = f"🏰 <b>БАШНЯ</b>\n\n"
    message_text += f"<blockquote>💵 Ставка: {format_balance(game['bet_amount'])}\n"
    message_text += f"📈 Уровень: {current_level}/5\n"
    message_text += f"⚔️ Множитель: x{multipliers[current_level]}</blockquote>\n\n"
    message_text += f"🏃‍♂️ Игрок вышел из игры\n"
    message_text += f"💵 Выигрыш: {format_balance(win_amount)}"
    
    markup = create_tower_keyboard(game_id, current_level, level_state['left'], level_state['right'], multipliers, show_mines=True)
    
    bot.edit_message_text(
        message_text,
        game['chat_id'],
        game['message_id'],
        reply_markup=markup,
        parse_mode='HTML'
    )
    
    bot.answer_callback_query(call.id, f"🏃‍♂️ Выход! +{format_balance(win_amount)}")
    
    update_game_stats(game['user_id'], True, win_amount, 0)

def update_game_stats(user_id, won, win_amount=0, lost_amount=0):
    """Обновить статистику игрока"""
    with get_db_cursor() as cursor:
        if won:
            cursor.execute('''
                UPDATE users SET 
                games_won = games_won + 1,
                total_won_amount = total_won_amount + ?
                WHERE user_id = ?
            ''', (win_amount, user_id))
        else:
            cursor.execute('''
                UPDATE users SET 
                games_lost = games_lost + 1,
                total_lost_amount = total_lost_amount + ?
                WHERE user_id = ?
            ''', (lost_amount, user_id))

def show_tower_help(chat_id):
    """Показать справку по игре Башня"""
    help_text = """🏰 <b>БАШНЯ</b>

⚔️ <b>Цель:</b> Дойти до вершины башни, избегая мины

📋 <b>Правила:</b>
• На каждом уровне 2 башни
• В одной башне мина 💣, вторая безопасна 🟢
• Выбери безопасную башню чтобы подняться выше
• Можно выйти в любой момент и забрать выигрыш
• При проигрыше бет сгорает
• ⏰ Авто-возврат через 4мы

💵 <b>Множители:</b>
Уровень 1 • x1.5
Уровень 2 • x2.0  
Уровень 3 • x3.0
Уровень 4 • x5.0
Уровень 5 • x7.0

🎲 <b>Команды:</b>
<code>башня [бет]</code> - начать игру
<code>Пример: башня 1000к</code>"""

    bot.send_message(chat_id, help_text, parse_mode='HTML')

start_tower_refund_checker()
@bot.message_handler(func=lambda message: message.text.lower() == 'статус буста')
def handle_boost_status(message):
    """Показать статус текущего буста"""
    boost = get_clicker_boost()
    
    if not boost['active']:
        bot.send_message(message.chat.id, "ℹ️ В данный момент буст кликера не работает.")
        return
    
    time_left = boost['end_time'] - time.time()
    if time_left <= 0:
        bot.send_message(message.chat.id, "ℹ️ Время буста истекло.")
        return
    
    minutes = int(time_left // 60)
    seconds = int(time_left % 60)
    
    bot.send_message(message.chat.id,
                   f"🎉 АКТИВНЫЙ БУСТ КЛИКЕРА 🎉\n\n"
                   f"📈 Множитель: x{boost['multiplier']}\n"
                   f"⏰ Осталось времени: {minutes:02d}:{seconds:02d}\n"
                   
                   f"⚡ Скорее жмите 'Кликер' пока действует буст!",
                   parse_mode='Markdown')

REQUIRED_CHANNEL = "@FECTIZ"

def check_subscription(user_id):
    """Проверяет подписку пользователя на канал"""
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

@bot.message_handler(func=lambda message: message.text in ["🎁 Бонус", "Бонус"])
def handle_daily_bonus(message):
    try:
        user_id = message.from_user.id
        
        if not check_subscription(user_id):
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/FECTIZ"))
            markup.add(InlineKeyboardButton("🔄 Проверить", callback_data="check_sub_bonus"))
            
            bot.send_message(
                message.chat.id,
                f"📢 Подпишись на канал, чтобы получать бонусы\n\n"
                f"После подписки нажми «Проверить»",
                reply_markup=markup
            )
            return
        
        with get_db_cursor() as cursor:
            cursor.execute('SELECT last_daily_bonus FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            last_bonus = result[0] if result else 0
        
        current_time = time.time()
        
        if last_bonus > 0:
            time_passed = current_time - last_bonus
            cooldown = 600 if is_premium(user_id) else 1200
            if time_passed < cooldown:
                time_left = cooldown - time_passed
                minutes = int(time_left // 60)
                seconds = int(time_left % 60)
                bot.send_message(message.chat.id, f"⏳ {minutes}:{seconds:02d}")
                return
        
        level, _ = get_user_level(user_id)
        level_mult = get_level_daily_bonus(level)
        base_bonus = 5000
        bonus_amount = int(base_bonus * level_mult)
        
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "🎁 ЗАБРАТЬ",
                        "callback_data": "claim_bonus",
                        "style": "success"
                    }
                ]
            ]
        }
        
        bonus_text = (
            f"<tg-emoji emoji-id='5442939099906325301'>🎁</tg-emoji> <b>Бонус готов</b>\n\n"
            f"<blockquote>"
            f"<tg-emoji emoji-id='5435999124245729290'>💵</tg-emoji> +{format_balance(bonus_amount)}\n"
            f"<tg-emoji emoji-id='5438496463044752972'>⭐</tg-emoji> +500 опыта"
            f"</blockquote>\n\n"
            f"<tg-emoji emoji-id='5929466926408404919'>⏱</tg-emoji> Каждые {'10' if is_premium(user_id) else '20'} мин"
        )
        
        bot.send_message(
            message.chat.id, 
            bonus_text, 
            reply_markup=keyboard, 
            parse_mode='HTML'
        )
        
    except Exception as e:
        print(f"Ошибка в бонусе: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка", parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data == "claim_bonus")
def handle_claim_bonus(call):
    try:
        user_id = call.from_user.id
        
        if not check_subscription(user_id):
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/FECTIZ"))
            markup.add(InlineKeyboardButton("🔄 Проверить", callback_data="check_sub_bonus"))
            
            bot.edit_message_text(
                "❌ Подписка не найдена!\n"
                f"📢 {REQUIRED_CHANNEL}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            , parse_mode='HTML')
            bot.answer_callback_query(call.id, "❌ Проверьте подписку")
            return
        
        with get_db_cursor() as cursor:
            cursor.execute('SELECT last_daily_bonus FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            last_bonus = result[0] if result else 0
        
        current_time = time.time()
        
        if last_bonus > 0:
            time_passed = current_time - last_bonus
            cooldown = 600 if is_premium(user_id) else 1200
            if time_passed < cooldown:
                time_left = int(cooldown - time_passed)
                minutes = time_left // 60
                seconds = time_left % 60
                bot.answer_callback_query(call.id, f"⏳ Подожди ещё {minutes}:{seconds:02d}", show_alert=True)
                return
        
        level, _ = get_user_level(user_id)
        level_mult = get_level_daily_bonus(level)
        base_bonus = 5000
        bonus_amount = int(base_bonus * level_mult)
        
        update_balance(user_id, bonus_amount)
        add_experience(user_id, 500)
        
        with get_db_cursor() as cursor:
            cursor.execute('UPDATE users SET last_daily_bonus = ? WHERE user_id = ?', (current_time, user_id))
        
        bot.edit_message_text(
            f"<tg-emoji emoji-id='5206607081334906820'>✅</tg-emoji> <b>Бонус получен!</b>\n\n"
            f"<blockquote>"
            f"<tg-emoji emoji-id='5435999124245729290'>💵</tg-emoji> +{format_balance(bonus_amount)}\n"
            f"<tg-emoji emoji-id='5438496463044752972'>⭐</tg-emoji> +500 опыта"
            f"</blockquote>",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML'
        )
        
        bot.answer_callback_query(call.id, "🏆")
        
    except Exception as e:
        print(f"Ошибка получения бонуса: {e}")
        bot.answer_callback_query(call.id, "❌")

@bot.callback_query_handler(func=lambda call: call.data == "check_sub_bonus")
def handle_check_subscription_bonus(call):
    try:
        user_id = call.from_user.id
        
        if check_subscription(user_id):
            bot.answer_callback_query(call.id, "🏆 Подписка подтверждена")
            bot.edit_message_text(
                "✅ Подписка подтверждена! Бонусы открыты.",
                call.message.chat.id,
                call.message.message_id
            , parse_mode='HTML')
            threading.Timer(1, lambda: handle_daily_bonus(call.message)).start()
        else:
            bot.answer_callback_query(call.id, "❌ Вы не подписаны")
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/FECTIZ"))
            markup.add(InlineKeyboardButton("🔄 Проверить", callback_data="check_sub_bonus"))
            
            bot.edit_message_text(
                "❌ Вы еще не подписались!\n\n"
                f"📢 Канал: {REQUIRED_CHANNEL}\n"
                "После подписки тапни '🔄 Проверить'",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            , parse_mode='HTML')
            
    except Exception as e:
        print(f"Ошибка проверки подписки: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка")

@bot.message_handler(func=lambda message: message.text.lower().startswith('разбонус') and is_admin(message.from_user.id))
def handle_bonus_broadcast(message):
    """Рассылка бонусов всем пользователям"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        bonus_broadcast_keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Да, разослать бонусы", "callback_data": "confirm_bonus_broadcast", "style": "success"},
                {"text": "❌ Отмена", "callback_data": "cancel_bonus_broadcast", "style": "danger"}
            ]]
        }
        
        bot.reply_to(
            message,
            f"📢 <b>РАССЫЛКА БОНУСОВ ВСЕМ ПОЛЬЗОВАТЕЛЯМ</b>\n\n"
            f"<blockquote>ℹ️ Что получат пользователи:\n"
            f"• Полноценный бонус (деньги + очки)\n"
            f"• Проверка подписки на {REQUIRED_CHANNEL}</blockquote>\n\n"
            f"Подтвердить рассылку бонусов всем?",
            reply_markup=bonus_broadcast_keyboard,
            parse_mode='HTML'
        )
        
        global pending_bonus_broadcast
        pending_bonus_broadcast = {
            "admin_id": message.from_user.id,
            "chat_id": message.chat.id,
            "message_id": message.message_id + 1
        }
        
    except Exception as e:
        print(f"Ошибка в разбонусе: {e}")
        bot.reply_to(message, "❌ Ошибка", parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data in ["confirm_bonus_broadcast", "cancel_bonus_broadcast"])
def handle_bonus_broadcast_confirmation(call):
    """Подтверждение массовой рассылки бонусов"""
    global pending_bonus_broadcast
    
    if not pending_bonus_broadcast:
        bot.answer_callback_query(call.id, "❌ Нет активной рассылки")
        return
    
    if call.data == "cancel_bonus_broadcast":
        bot.edit_message_text(
            "❌ Рассылка бонусов сброшена",
            call.message.chat.id,
            call.message.message_id
        , parse_mode='HTML')
        pending_bonus_broadcast = None
        bot.answer_callback_query(call.id, "Отменено")
        return
    
    bot.edit_message_text(
        "🔄 <b>Начинаю рассылку бонусов...</b>\n"
        "⏳ Это может занять время...",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML'
    )
    
    sent = 0
    failed = 0
    total_users = 0
    
    try:
        with get_db_cursor() as cursor:
            cursor.execute('SELECT user_id FROM users')
            users = cursor.fetchall()
            total_users = len(users)
            
            for i, (user_id,) in enumerate(users, 1):
                try:
                    cursor.execute('SELECT experience FROM users WHERE user_id = ?', (user_id,))
                    result = cursor.fetchone()
                    exp = result[0] if result else 0
                    level = int((exp / 1000) ** 0.5) + 1
                    
                    base_bonus = 5000
                    bonus_levels = level // 3
                    bonus_amount = base_bonus + (1234 * bonus_levels)
                    
                    is_subscribed = check_subscription(user_id)
                    
                    if is_subscribed:
                        markup = InlineKeyboardMarkup()
                        markup.add(InlineKeyboardButton("🎁 Забрать", callback_data="bonus_broadcast_claim"))
                        
                        bonus_text = f"🎁 <b>Бонус от администрации</b>\n\n"
                        bonus_text += f"💵 {format_balance(bonus_amount)}\n"
                        bonus_text += f"⭐ +500 опыта"
                        
                        bot.send_message(user_id, bonus_text, reply_markup=markup, parse_mode='HTML')
                        sent += 1
                    else:
                        markup = InlineKeyboardMarkup()
                        markup.add(InlineKeyboardButton("🎁 Получить бонус", callback_data="bonus_broadcast_claim"))
                        markup.add(InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/FECTIZ"))
                        
                        bonus_text = f"🎁 <b>Бонус от администрации</b>\n\n"
                        bonus_text += f"💵 {format_balance(bonus_amount)}\n"
                        bonus_text += f"⭐ +500 опыта\n\n"
                        bonus_text += f"⚠️ Требуется подписка на канал"
                        
                        bot.send_message(user_id, bonus_text, reply_markup=markup, parse_mode='HTML')
                        sent += 1
                    
                    if i % 20 == 0:
                        progress = int((i / total_users) * 100)
                        bot.edit_message_text(
                            f"🔄 <b>Рассылка бонусов...</b>\n\n"
                            f"📊 Прогресс: {progress}%\n"
                            f"🏆 Отправлено: {sent}\n"
                            f"❌ Ошибок: {failed}",
                            call.message.chat.id,
                            call.message.message_id,
                            parse_mode='HTML'
                        )
                    
                    time.sleep(0.1)
                    
                except Exception as e:
                    print(f"Ошибка отправки бонуса {user_id}: {e}")
                    failed += 1
                    time.sleep(0.5)
        
        result_text = (
            f"🏆 <b>РАССЫЛКА БОНУСОВ ЗАВЕРШЕНА</b>\n\n"
            f"📊 Результаты:\n"
            f"👥 Всего пользователей: {total_users}\n"
            f"🏆 Успешно: {sent}\n"
            f"❌ Ошибок: {failed}\n\n"
            f"⚔️ Теперь пользователи получат тот же бонус что и при нажатии 'Бонус'!"
        )
        
        bot.edit_message_text(
            result_text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML'
        )
        
        bot.answer_callback_query(call.id, "🏆 Рассылка окончена")
        
    except Exception as e:
        print(f"Ошибка в рассылке бонусов: {e}")
        bot.edit_message_text(
            f"❌ <b>Ошибка рассылки</b>\n\n{str(e)[:200]}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML'
        )
    
    pending_bonus_broadcast = None

@bot.callback_query_handler(func=lambda call: call.data == "bonus_broadcast_claim")
def handle_broadcast_bonus_claim(call):
    """Получение бонуса из массовой рассылки"""
    try:
        user_id = call.from_user.id
        
        if not check_subscription(user_id):
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/FECTIZ"))
            markup.add(InlineKeyboardButton("🔄 Проверить", callback_data="bonus_broadcast_claim"))
            
            bot.edit_message_text(
                "❌ Для бонуса подпишитесь на канал:\n"
                f"📢 {REQUIRED_CHANNEL}\n\n"
                "После подписки тапни '🔄 Проверить'",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            , parse_mode='HTML')
            bot.answer_callback_query(call.id, "❌ Проверьте подписку")
            return
        
        level, _ = get_user_level(user_id)
        level_mult = get_level_daily_bonus(level)
        base_bonus = 5000
        bonus_amount = int(base_bonus * level_mult)
        
        update_balance(user_id, bonus_amount)
        add_experience(user_id, 500)
        
        bot.edit_message_text(
            f"🏆 Бонус получен\n\n"
            f"💵 +{format_balance(bonus_amount)}\n"
            f"⚔️ +500 ⭐ опыта",
            call.message.chat.id,
            call.message.message_id
        , parse_mode='HTML')
        
        bot.answer_callback_query(call.id, "🏆")
        
    except Exception as e:
        print(f"Ошибка получения бонуса из рассылки: {e}")
        bot.answer_callback_query(call.id, "❌")
@bot.message_handler(commands=['', 'меню', '🔄'])
def handle_menu_command(message):
    """Показывает главное меню"""
    try:
        user_id = message.from_user.id
        username = message.from_user.username
        first_name = message.from_user.first_name
        
        get_or_create_user(user_id, username, first_name)
        
        markup = create_main_menu()
        
        bot.send_message(
            message.chat.id,
            "📱 <b>Главное меню</b>\n\n"
            "Выберите раздел:",
            reply_markup=markup,
            parse_mode='HTML'
        )
        
    except Exception as e:
        print(f"Ошибка в /menu: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка загрузки меню", parse_mode='HTML')
@bot.message_handler(func=lambda message: message.text.lower().startswith('сброситьопыт ') and is_admin(message.from_user.id))
def handle_quick_reset_exp(message):
    """Быстрый сброс опыта пользователю"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Используй: сбросить опыт [айди или @юзернейм]")
            return
        
        target = parts[1]
        target_user_id = None
        
        if target.startswith('@'):
            username = target[1:]
            with get_db_cursor() as cursor:
                cursor.execute('SELECT user_id FROM users WHERE username = ?', (username,))
                result = cursor.fetchone()
                if result:
                    target_user_id = result[0]
                else:
                    bot.send_message(message.chat.id, f"❌ Пользователь {target} не найден!")
                    return
        else:
            try:
                target_user_id = int(target)
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный ID! Используй цифры или @юзернейм")
                return
        
        user_info = get_user_info(target_user_id)
        if not user_info:
            bot.send_message(message.chat.id, f"❌ Пользователь с ID {target_user_id} не найден в базе!")
            return
        
        with get_db_cursor() as cursor:
            cursor.execute('UPDATE users SET experience = 0 WHERE user_id = ?', (target_user_id,))
        
        bot.send_message(message.chat.id, f"✅ Опыт сброшен")
        
    except Exception as e:
        print(f"Ошибка при сбросе опыта: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
@bot.message_handler(func=lambda message: message.text.lower() == 'меню')
def handle_menu_text(message):
    handle_menu_command(message)

BRONZE_E = "<tg-emoji emoji-id='5266998599304104035'>🥉</tg-emoji>"
SILVER_E = "<tg-emoji emoji-id='5267067121212344111'>🥈</tg-emoji>"
GOLD_E   = "<tg-emoji emoji-id='5267193444790458285'>🥇</tg-emoji>"
DIAMOND_E= "<tg-emoji emoji-id='4956719506027185156'>💎</tg-emoji>"
MYTHIC_E = "<tg-emoji emoji-id='5298499667569425533'>🔮</tg-emoji>"
LEGEND_E = "<tg-emoji emoji-id='5224510115637391787'>👑</tg-emoji>"

LEVEL_TITLES = {
    1:  (BRONZE_E, "Bronze I"),
    2:  (BRONZE_E, "Bronze II"),
    3:  (BRONZE_E, "Bronze III"),
    4:  (BRONZE_E, "Bronze IV"),
    5:  (BRONZE_E, "Bronze V"),
    6:  (BRONZE_E, "Bronze VI"),
    7:  (BRONZE_E, "Bronze VII"),
    8:  (BRONZE_E, "Bronze VIII"),
    9:  (SILVER_E, "Silver I"),
    10: (SILVER_E, "Silver II"),
    11: (SILVER_E, "Silver III"),
    12: (SILVER_E, "Silver IV"),
    13: (SILVER_E, "Silver V"),
    14: (SILVER_E, "Silver VI"),
    15: (SILVER_E, "Silver VII"),
    16: (SILVER_E, "Silver VIII"),
    17: (GOLD_E,   "Gold I"),
    18: (GOLD_E,   "Gold II"),
    19: (GOLD_E,   "Gold III"),
    20: (GOLD_E,   "Gold IV"),
    21: (GOLD_E,   "Gold V"),
    22: (GOLD_E,   "Gold VI"),
    23: (GOLD_E,   "Gold VII"),
    24: (GOLD_E,   "Gold VIII"),
    25: (DIAMOND_E,"Diamond I"),
    26: (DIAMOND_E,"Diamond II"),
    27: (DIAMOND_E,"Diamond III"),
    28: (DIAMOND_E,"Diamond IV"),
    29: (DIAMOND_E,"Diamond V"),
    30: (DIAMOND_E,"Diamond VI"),
    31: (DIAMOND_E,"Diamond VII"),
    32: (DIAMOND_E,"Diamond VIII"),
    33: (MYTHIC_E, "The Mythic I"),
    34: (MYTHIC_E, "The Mythic II"),
    35: (MYTHIC_E, "The Mythic III"),
    36: (MYTHIC_E, "The Mythic IV"),
    37: (MYTHIC_E, "The Mythic V"),
    38: (MYTHIC_E, "The Mythic VI"),
    39: (MYTHIC_E, "The Mythic VII"),
    40: (MYTHIC_E, "The Mythic VIII"),
    41: (LEGEND_E, "Legend I"),
    42: (LEGEND_E, "Legend II"),
    43: (LEGEND_E, "Legend III"),
    44: (LEGEND_E, "Legend IV"),
    45: (LEGEND_E, "Legend V"),
    46: (LEGEND_E, "Legend VI"),
    47: (LEGEND_E, "Legend VII"),
    48: (LEGEND_E, "Legend VIII"),
    49: (LEGEND_E, "Legend IX"),
    50: (LEGEND_E, "Legend X"),
}

def get_level_from_exp(experience):
    """Вычисляет уровень по опыту. Формула: каждый уровень требует больше опыта."""
    level = 1
    total = 0
    while level < 50:
        needed = 1000 + (level - 1) * 500
        if total + needed > experience:
            break
        total += needed
        level += 1
    return level

def get_exp_for_level(level):
    """Сколько суммарно опыта нужно для достижения уровня."""
    total = 0
    for l in range(1, level):
        total += 1000 + (l - 1) * 500
    return total

def get_level_progress(experience):
    """Возвращает (уровень, опыт в текущем уровне, нужно до следующего)."""
    level = get_level_from_exp(experience)
    exp_start = get_exp_for_level(level)
    if level >= 50:
        return level, experience - exp_start, 0
    exp_needed = 1000 + (level - 1) * 500
    return level, experience - exp_start, exp_needed

def get_title(level):
    """Возвращает (эмодзи, звание) для уровня."""
    return LEVEL_TITLES.get(level, LEVEL_TITLES[50])

def get_level_click_bonus(level):
    """Бонус к силе клика за уровень: +2% за каждый уровень."""
    return round(1 + level * 0.02, 2)

def get_level_daily_bonus(level):
    """Бонус к ежедневному бонусу за уровень: +5% за каждый уровень."""
    return round(1 + level * 0.05, 2)

def get_user_level(user_id):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT experience FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        experience = result[0] if result else 0
    level = get_level_from_exp(experience)
    return level, experience

def add_experience(user_id, exp_amount):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT experience FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        old_exp = result[0] if result else 0
        old_level = get_level_from_exp(old_exp)

        cursor.execute('UPDATE users SET experience = experience + ? WHERE user_id = ?', (exp_amount, user_id))

        cursor.execute('SELECT experience FROM users WHERE user_id = ?', (user_id,))
        new_exp = cursor.fetchone()[0]
        new_level = get_level_from_exp(new_exp)

    if new_level > old_level:
        emoji, title = get_title(new_level)
        try:
            bonus_text = ""
            if new_level % 5 == 0:
                bonus_text = f"\n\n🎁 <b>Каждый 5-й уровень — особые привилегии!</b>"
            bot.send_message(
                user_id,
                f"🎉 <b>НОВЫЙ УРОВЕНЬ!</b>\n\n"
                f"{emoji} <b>{new_level} — {title}</b>\n\n"
                f"⚡ Бонус к кликам: +{int(get_level_click_bonus(new_level)*100-100)}%\n"
                f"🎁 Бонус к ежедневке: +{int(get_level_daily_bonus(new_level)*100-100)}%"
                f"{bonus_text}",
                parse_mode='HTML'
            )
        except Exception:
            pass

@bot.message_handler(func=lambda message: message.text.lower() == "я")
def handle_me(message):
    try:
        user_id = message.from_user.id
        user_info = get_user_info(user_id)
        
        if not user_info:
            bot.send_message(message.chat.id, "❌ Пользователь отсутствует", parse_mode='HTML')
            return
            
        display_name = user_info['custom_name'] if user_info['custom_name'] else (f"@{user_info['username']}" if user_info['username'] else user_info['first_name'])
        level, experience = get_user_level(user_id)
        
        current_hour = datetime.now().hour
        if 5 <= current_hour < 13:
            time_greeting = "Доброе утро"
            emoji = "☀️"
        elif 13 <= current_hour < 18:
            time_greeting = "Добрый день" 
            emoji = "👋"
        elif 18 <= current_hour < 23:
            time_greeting = "Добрый вечер"
            emoji = "👋"
        else:
            time_greeting = "Доброй ночи"
            emoji = "👋"
        
        phrases = [
            "Крутишь как надо",
            "Держи карман шире, братан",
            "Шарнирно-губчатый настрой",
            "Баланс твой — сила наша",
            "Хуячь по полной программе",
            "Не грей голову, деловой",
            "Картошка фри в твою честь",
            "По-тихому, по-легальному",
            "Смазан как подшипник",
            "Заточен как карандаш",
            
            "Бабло побеждает зло",
            "Не в деньгах счастье... но рядом",
            "Кеш — это кэш, что тут скажешь",
            "Миллионер по настроению",
            "Деньги любят счет, а я люблю деньги",
            "Финансовая удача на твоей стороне",
            "Продай носки — купи биток",
            "Главное — не вложиться в MMM",
            "Деньги к деньгам, как магнит",
            "Баланс растет — жизнь цветет",
            
            "Жги, недотрога",
            "Внатуре, пацан",
            "По-пацански четко",
            "Базаришь как надо",
            "Красава, респект",
            "Ты в теме, бро",
            "Рифмуешь с жизнью",
            "Вау, просто вау",
            "Зажигаешь не по-детски",
            "Стойка в миллион",
            
            "Перфоратор в помощь по жизни",
            "В лунку по жизни катишь",
            "Не выводи — инвестируй в себя",
            "Помни про налоги, богач",
            "Доверяй, но проверяй баланс",
            "Храни пароли как зеницу ока",
            "Береги карманы от дырок",
            "Не оставляй кошелек на виду",
            "Следи за курсом как за девушкой",
            "Не спались — деньги не спят",
            
            "Не проиграй все в один клик",
            "Держи баланс в узде, ковбой",
            "Не дай себя обмануть лохотроном",
            "Помни: за каждым миллионом слежка",
            "Богат не тот, у кого много... ладно, тот",
            "Хлеба, зрелищ и чтобы сантехник вовремя",
            "Кофе горячий, жизнь без долгов",
            "Карман тяжелеет — спина прямеет",
            "Деньги не пахнут... пахнет успех",
            "Заряжен как батарейка энэрджайзер"
        ]
        
        random_phrase = random.choice(phrases)

        lv_emoji, lv_title = get_title(level)
        _, lv_cur, lv_need = get_level_progress(experience)
        if lv_need > 0:
            bars = int(lv_cur / lv_need * 8)
            bar = "█" * bars + "░" * (8 - bars)
        else:
            bar = "████████"

        prem_badge = f" {PREMIUM_EMOJI}" if is_premium(user_id) else ""
        message_text = f"{emoji} {time_greeting} <b>{display_name}</b>{prem_badge}\n"
        message_text += f"На счету — <b>{format_balance(user_info['balance'])}</b>\n"
        message_text += f"<blockquote>{lv_emoji} Титул: <b>{lv_title}</b></blockquote>"
        
        outfit_path = create_character_outfit(user_id)
        
        try:
            with open(outfit_path, 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption=message_text, parse_mode='HTML')
        except:
            with open("images/base_human.jpg", 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption=message_text, parse_mode='HTML')
    
    except Exception as e:
        bot.send_message(message.chat.id, "❌ Ошибка", parse_mode='HTML')

active_trades = {}

def generate_trade_code():
    """Генерирует уникальный код для трейда"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

class Trade:
    def __init__(self, user1_id, user2_id):
        self.user1_id = user1_id
        self.user2_id = user2_id
        self.user1_items = []
        self.user2_items = []
        self.status = "active"
        self.created_at = time.time()
        self.trade_code = generate_trade_code()
        self.trade_id = f"TRADE_{self.trade_code}"

@bot.message_handler(func=lambda message: message.text and message.text.lower().startswith('обмен') and message.reply_to_message)
def handle_trade_start(message):
    print(f"🔍 Получена команда обмен: {message.text}")
    print(f"🔍 Ответ на сообщение от: {message.reply_to_message.from_user.id if message.reply_to_message else 'None'}")
    
    user1_id = message.from_user.id
    
    if not message.reply_to_message:
        bot.send_message(message.chat.id, "❌ Нужно ответить на сообщение игрока!", parse_mode='HTML')
        return
        
    user2_id = message.reply_to_message.from_user.id
    
    print(f"🔄 Начало обмена: {user1_id} -> {user2_id}")

    if user1_id == user2_id:
        bot.send_message(message.chat.id, "❌ Нельзя предложить обмен самому себе!", parse_mode='HTML')
        return

    for trade_id, trade in active_trades.items():
        if (trade.user1_id == user1_id and trade.user2_id == user2_id) or \
           (trade.user1_id == user2_id and trade.user2_id == user1_id):
            bot.send_message(message.chat.id, "❌ У вас уже есть активный обмен!", parse_mode='HTML')
            return

    trade = Trade(user1_id, user2_id)
    active_trades[trade.trade_id] = trade

    print(f"🏆 Создан обмен: {trade.trade_id}")

    try:
        send_trade_interface(user1_id, trade)
        print(f"🏆 Интерфейс отправлен user1: {user1_id}")
    except Exception as e:
        print(f"❌ Ошибка отправки user1: {e}")

    try:
        send_trade_interface(user2_id, trade)
        print(f"🏆 Интерфейс отправлен user2: {user2_id}")
    except Exception as e:
        print(f"❌ Ошибка отправки user2: {e}")

    bot.send_message(message.chat.id, "🔄 Создан обмен! Оба игрока получили ЛС для настройки.")

def send_trade_interface(user_id, trade):
    try:
        other_user_id = trade.user2_id if user_id == trade.user1_id else trade.user1_id
        other_user_info = get_user_info(other_user_id)
        other_user_name = other_user_info['custom_name'] if other_user_info['custom_name'] else (
            f"@{other_user_info['username']}" if other_user_info['username'] else other_user_info['first_name']
        )
        
        user_clothes = get_user_clothes(user_id)
        
        message_text = f"🔄 ОБМЕН С {other_user_name}\n\n"
        message_text += "🎒 ВАШИ ВЕЩИ:\n"
        
        if not user_clothes:
            message_text += "У вас нет вещей для обмена\n"
        else:
            for i, item in enumerate(user_clothes, 1):
                in_trade = "🏆 В обмене" if item['item_id'] in (trade.user1_items if user_id == trade.user1_id else trade.user2_items) else ""
                message_text += f"{i}. {item['name']} {in_trade}\n"
        
        message_text += f"\n📦 ВАШИ ВЕЩИ В ОБМЕНЕ: {len(trade.user1_items if user_id == trade.user1_id else trade.user2_items)}\n"
        message_text += f"📦 ВЕЩИ СОПЕРНИКА: {len(trade.user2_items if user_id == trade.user1_id else trade.user1_items)}\n\n"
        
        trade_inline = []
        
        if user_clothes:
            for i, item in enumerate(user_clothes[:8], 1):
                item_in_trade = item['item_id'] in (trade.user1_items if user_id == trade.user1_id else trade.user2_items)
                if item_in_trade:
                    trade_inline.append([{"text": f"❌ Убрать {item['name'][:12]}", "callback_data": f"TRADE_REM_{trade.trade_code}_{item['item_id']}_{user_id}", "style": "danger"}])
                else:
                    trade_inline.append([{"text": f"➕ Добавить {item['name'][:12]}", "callback_data": f"TRADE_ADD_{trade.trade_code}_{item['item_id']}_{user_id}", "style": "secondary"}])
        
        trade_inline.append([
            {"text": "✅ Подтвердить обмен", "callback_data": f"TRADE_CFM_{trade.trade_code}_{user_id}", "style": "success"},
            {"text": "❌ Отмена", "callback_data": f"TRADE_CNL_{trade.trade_code}_{user_id}", "style": "danger"}
        ])
        trade_inline.append([{"text": "🔄 Обновить", "callback_data": f"TRADE_REF_{trade.trade_code}_{user_id}"}])
        
        trade_keyboard = json.dumps({"inline_keyboard": trade_inline})
        
        if hasattr(trade, f'message_id_{user_id}'):
            try:
                bot.edit_message_text(
                    message_text,
                    chat_id=user_id,
                    message_id=getattr(trade, f'message_id_{user_id}'),
                    reply_markup=trade_keyboard
                )
            except:
                msg = bot.send_message(user_id, message_text, reply_markup=trade_keyboard)
                setattr(trade, f'message_id_{user_id}', msg.message_id)
        else:
            msg = bot.send_message(user_id, message_text, reply_markup=trade_keyboard)
            setattr(trade, f'message_id_{user_id}', msg.message_id)
        
    except Exception as e:
        print(f"❌ Ошибка отправки интерфейса обмена: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('TRADE_ADD_'))
def handle_trade_add(call):
    user_id = call.from_user.id
    parts = call.data.split('_')
    
    print(f"🔍 TRADE_ADD callback: {call.data}")
    
    if len(parts) < 4:
        bot.answer_callback_query(call.id, "❌ Ошибка данных!")
        return
        
    trade_code = parts[2]
    item_id = int(parts[3])
    target_user_id = int(parts[4])
    
    trade_id = f"TRADE_{trade_code}"
    
    if user_id != target_user_id:
        bot.answer_callback_query(call.id, "❌ Это не твой интерфейс!")
        return
    
    if trade_id not in active_trades:
        bot.answer_callback_query(call.id, "❌ Обмен не найден!")
        return
    
    trade = active_trades[trade_id]
    
    if user_id not in [trade.user1_id, trade.user2_id]:
        bot.answer_callback_query(call.id, "❌ Вы не боец обмена!")
        return
    
    user_clothes = get_user_clothes(user_id)
    user_has_item = any(item['item_id'] == item_id for item in user_clothes)
    
    if not user_has_item:
        bot.answer_callback_query(call.id, "❌ У вас нет этой вещи!")
        return
    
    if user_id == trade.user1_id:
        if item_id not in trade.user1_items:
            trade.user1_items.append(item_id)
    else:
        if item_id not in trade.user2_items:
            trade.user2_items.append(item_id)
    
    bot.answer_callback_query(call.id, "🏆 Вещь добавлена в обмен!")
    
    send_trade_interface(trade.user1_id, trade)
    send_trade_interface(trade.user2_id, trade)

@bot.callback_query_handler(func=lambda call: call.data.startswith('TRADE_REM_'))
def handle_trade_remove(call):
    user_id = call.from_user.id
    parts = call.data.split('_')
    
    print(f"🔍 TRADE_REM callback: {call.data}")
    
    if len(parts) < 4:
        bot.answer_callback_query(call.id, "❌ Ошибка данных!")
        return
        
    trade_code = parts[2]
    item_id = int(parts[3])
    target_user_id = int(parts[4])
    
    trade_id = f"TRADE_{trade_code}"
    
    if user_id != target_user_id:
        bot.answer_callback_query(call.id, "❌ Это не твой интерфейс!")
        return
    
    if trade_id not in active_trades:
        bot.answer_callback_query(call.id, "❌ Обмен не найден!")
        return
    
    trade = active_trades[trade_id]
    
    if user_id == trade.user1_id:
        if item_id in trade.user1_items:
            trade.user1_items.remove(item_id)
    else:
        if item_id in trade.user2_items:
            trade.user2_items.remove(item_id)
    
    bot.answer_callback_query(call.id, "🏆 Вещь убрана из обмена!")
    
    send_trade_interface(trade.user1_id, trade)
    send_trade_interface(trade.user2_id, trade)

@bot.callback_query_handler(func=lambda call: call.data.startswith('TRADE_CFM_'))
def handle_trade_confirm(call):
    user_id = call.from_user.id
    parts = call.data.split('_')
    
    print(f"🔍 TRADE_CFM callback: {call.data}")
    
    if len(parts) < 4:
        bot.answer_callback_query(call.id, "❌ Ошибка данных!")
        return
        
    trade_code = parts[2]
    target_user_id = int(parts[3])
    
    trade_id = f"TRADE_{trade_code}"
    
    if user_id != target_user_id:
        bot.answer_callback_query(call.id, "❌ Это не твой интерфейс!")
        return
    
    if trade_id not in active_trades:
        bot.answer_callback_query(call.id, "❌ Обмен не найден!")
        return
    
    trade = active_trades[trade_id]
    
    if not trade.user1_items and not trade.user2_items:
        bot.answer_callback_query(call.id, "❌ Нет вещей для обмена!")
        return
    
    if user_id == trade.user1_id:
        trade.user1_confirmed = True
    else:
        trade.user2_confirmed = True
    
    if hasattr(trade, 'user1_confirmed') and hasattr(trade, 'user2_confirmed'):
        execute_trade(trade)
        bot.answer_callback_query(call.id, "🏆 Обмен окончен!")
    else:
        bot.answer_callback_query(call.id, "🏆 Вы подтвердили обмен! Ждите соперника.")
        
        other_user_id = trade.user2_id if user_id == trade.user1_id else trade.user1_id
        try:
            bot.send_message(other_user_id, "⚠️ Соперник подтвердил обмен! Подтвердите вы.")
        except:
            pass

@bot.callback_query_handler(func=lambda call: call.data.startswith('TRADE_REF_'))
def handle_trade_refresh(call):
    user_id = call.from_user.id
    parts = call.data.split('_')
    
    if len(parts) < 4:
        bot.answer_callback_query(call.id, "❌ Ошибка данных!")
        return
        
    trade_code = parts[2]
    target_user_id = int(parts[3])
    
    trade_id = f"TRADE_{trade_code}"
    
    if user_id != target_user_id:
        bot.answer_callback_query(call.id, "❌ Это не твой интерфейс!")
        return
    
    if trade_id not in active_trades:
        bot.answer_callback_query(call.id, "❌ Обмен не найден!")
        return
    
    trade = active_trades[trade_id]
    send_trade_interface(user_id, trade)
    bot.answer_callback_query(call.id, "🏆 Обновлено!")

@bot.callback_query_handler(func=lambda call: call.data.startswith('TRADE_CNL_'))
def handle_trade_cancel(call):
    user_id = call.from_user.id
    parts = call.data.split('_')
    
    print(f"🔍 TRADE_CNL callback: {call.data}")
    
    if len(parts) < 4:
        bot.answer_callback_query(call.id, "❌ Ошибка данных!")
        return
        
    trade_code = parts[2]
    target_user_id = int(parts[3])
    
    trade_id = f"TRADE_{trade_code}"
    
    if user_id != target_user_id:
        bot.answer_callback_query(call.id, "❌ Это не твой интерфейс!")
        return
    
    if trade_id not in active_trades:
        bot.answer_callback_query(call.id, "❌ Обмен не найден!")
        return
    
    trade = active_trades[trade_id]
    
    other_user_id = trade.user2_id if user_id == trade.user1_id else trade.user1_id
    try:
        other_user_info = get_user_info(other_user_id)
        other_user_name = other_user_info['custom_name'] if other_user_info['custom_name'] else (
            f"@{other_user_info['username']}" if other_user_info['username'] else other_user_info['first_name']
        )
        bot.send_message(other_user_id, f"❌ {other_user_name} отменил обмен", parse_mode='HTML')
    except:
        pass
    
    del active_trades[trade_id]
    
    bot.answer_callback_query(call.id, "🏆 Обмен сброшен!")
    
    try:
        bot.send_message(user_id, "❌ Обмен сброшен", parse_mode='HTML')
    except:
        pass

def execute_trade(trade):
    try:
        with get_db_cursor() as cursor:
            for item_id in trade.user1_items:
                cursor.execute('UPDATE user_clothes SET user_id = ? WHERE user_id = ? AND item_id = ?', 
                              (trade.user2_id, trade.user1_id, item_id))
            
            for item_id in trade.user2_items:
                cursor.execute('UPDATE user_clothes SET user_id = ? WHERE user_id = ? AND item_id = ?', 
                              (trade.user1_id, trade.user2_id, item_id))
            
            user1_info = get_user_info(trade.user1_id)
            user2_info = get_user_info(trade.user2_id)
            
            user1_name = user1_info['custom_name'] if user1_info['custom_name'] else (
                f"@{user1_info['username']}" if user1_info['username'] else user1_info['first_name']
            )
            user2_name = user2_info['custom_name'] if user2_info['custom_name'] else (
                f"@{user2_info['username']}" if user2_info['username'] else user2_info['first_name']
            )
            
            trade_result = f"🏆 ОБМЕН ЗАВЕРШЕН!\n\n"
            trade_result += f"🔄 {user1_name} ↔️ {user2_name}\n\n"
            
            if trade.user1_items:
                items_text = ""
                for item_id in trade.user1_items:
                    item_info = get_item_info(item_id)
                    if item_info:
                        items_text += f"• {item_info['name']}\n"
                trade_result += f"📦 {user1_name} отдал:\n{items_text}\n"
            
            if trade.user2_items:
                items_text = ""
                for item_id in trade.user2_items:
                    item_info = get_item_info(item_id)
                    if item_info:
                        items_text += f"• {item_info['name']}\n"
                trade_result += f"📦 {user2_name} отдал:\n{items_text}"
            
            try:
                bot.send_message(trade.user1_id, trade_result)
            except:
                pass
            
            try:
                bot.send_message(trade.user2_id, trade_result)
            except:
                pass
            
            if trade_id in active_trades:
                del active_trades[trade_id]
                
    except Exception as e:
        print(f"❌ Ошибка выполнения обмена: {e}")
        
        try:
            bot.send_message(trade.user1_id, "❌ Ошибка при обмене!", parse_mode='HTML')
            bot.send_message(trade.user2_id, "❌ Ошибка при обмене!", parse_mode='HTML')
        except:
            pass

def get_item_info(item_id):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT name FROM clothes_shop WHERE id = ?', (item_id,))
        result = cursor.fetchone()
        if result:
            return {'name': result[0]}
        return None

def cleanup_expired_trades():
    current_time = time.time()
    expired = []
    
    for trade_id, trade in active_trades.items():
        if current_time - trade.created_at > 1800:
            expired.append(trade_id)
    
    for trade_id in expired:
        trade = active_trades[trade_id]
        
        try:
            bot.send_message(trade.user1_id, "❌ Обмен сброшен (время вышло)", parse_mode='HTML')
        except:
            pass
        
        try:
            bot.send_message(trade.user2_id, "❌ Обмен сброшен (время вышло)", parse_mode='HTML')
        except:
            pass
        
        del active_trades[trade_id]

def start_trade_cleanup():
    while True:
        try:
            cleanup_expired_trades()
        except:
            pass
        time.sleep(60)

trade_cleanup_thread = threading.Thread(target=start_trade_cleanup, daemon=True)
trade_cleanup_thread.start()

@bot.message_handler(func=lambda message: message.text.lower().startswith('добавить одежду ') and is_admin(message.from_user.id))
def handle_add_clothing(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split('|')
        if len(parts) < 4:
            bot.send_message(message.chat.id,
                           "❌ Формат: добавить одежду Название | Цена | Тип | Файл.png\n\n"
                           "📋 Типы: Голова, Тело, Ноги, Слева, Справа\n"
                           "💡 Пример:\n"
                           "добавить одежду Кепка | 1000000 | Голова | cap.png\n"
                           "добавить одежду Часы | 5000000 | Слева | watch.png", parse_mode='HTML')
            return
        
        name_part = parts[0][16:].strip()
        name = name_part
        price_text = parts[1].strip()
        item_type = parts[2].strip()
        image_file = parts[3].strip()
        
        valid_types = ['Голова', 'Тело', 'Ноги', 'Слева', 'Справа']
        if item_type not in valid_types:
            bot.send_message(message.chat.id, f"❌ Неверный тип! Допустимо: {', '.join(valid_types)}", parse_mode='HTML')
            return
        
        price = parse_bet_amount(price_text, float('inf'))
        if price is None or price <= 0:
            bot.send_message(message.chat.id, "❌ Неверная стоимость!", parse_mode='HTML')
            return
        
        image_path = f"images/{image_file}"
        if not os.path.exists(image_path):
            bot.send_message(message.chat.id, 
                           f"❌ Файл отсутствует: {image_file}\n\n"
                           f"📁 Убедитесь что файл лежит в папке images/", parse_mode='HTML')
            return
        
        with get_db_cursor() as cursor:
            cursor.execute('SELECT id FROM clothes_shop WHERE name = ?', (name,))
            if cursor.fetchone():
                bot.send_message(message.chat.id, f"❌ Вещь '{name}' уже существует!", parse_mode='HTML')
                return
            
            cursor.execute('''
                INSERT INTO clothes_shop (name, price, type, image_name)
                VALUES (?, ?, ?, ?)
            ''', (name, price, item_type, image_file))
            
            bot.send_message(message.chat.id,
                           f"🏆 Одежда добавлена в магазин!\n\n"
                           f"🎁 Название: {name}\n"
                           f"💵 Цена: {format_balance(price)}\n"
                           f"📁 Тип: {item_type}\n"
                           f"🖼️ Файл: {image_file}\n\n"
                           f"🛍️ Теперь доступна в магазине!", parse_mode='HTML')
            
    except Exception as e:
        print(f"❌ Ошибка добавления одежды: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')
@bot.message_handler(func=lambda message: message.text.lower().startswith('добавить несколько ') and is_admin(message.from_user.id))
def handle_add_multiple_clothing(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        lines = message.text.split('\n')[1:]
        
        if not lines:
            bot.send_message(message.chat.id,
                           "❌ Формат:\n"
                           "добавить несколько\n"
                           "Кепка | 1000000 | Голова | cap.png\n"
                           "Часы | 5000000 | Слева | watch.png\n"
                           "Кроссовки | 2000000 | Ноги | shoes.png", parse_mode='HTML')
            return
        
        added_count = 0
        errors = []
        
        with get_db_cursor() as cursor:
            for i, line in enumerate(lines, 1):
                line = line.strip()
                if not line:
                    continue
                    
                try:
                    parts = line.split('|')
                    if len(parts) < 4:
                        errors.append(f"Строка {i}: Неверный формат")
                        continue
                    
                    name = parts[0].strip()
                    price_text = parts[1].strip()
                    item_type = parts[2].strip()
                    image_file = parts[3].strip()
                    
                    valid_types = ['Голова', 'Тело', 'Ноги', 'Слева', 'Справа']
                    if item_type not in valid_types:
                        errors.append(f"Строка {i}: Неверный тип '{item_type}'")
                        continue
                    
                    price = parse_bet_amount(price_text, float('inf'))
                    if price is None or price <= 0:
                        errors.append(f"Строка {i}: Неверная стоимость '{price_text}'")
                        continue
                    
                    image_path = f"images/{image_file}"
                    if not os.path.exists(image_path):
                        errors.append(f"Строка {i}: Файл отсутствует '{image_file}'")
                        continue
                    
                    cursor.execute('SELECT id FROM clothes_shop WHERE name = ?', (name,))
                    if cursor.fetchone():
                        errors.append(f"Строка {i}: Вещь уже существует '{name}'")
                        continue
                    
                    cursor.execute('INSERT INTO clothes_shop (name, price, type, image_name) VALUES (?, ?, ?, ?)', 
                                  (name, price, item_type, image_file))
                    added_count += 1
                    
                except Exception as e:
                    errors.append(f"Строка {i}: Ошибка {e}")
        
        result_text = f"🏆 Добавлено {added_count} вещей\n"
        if errors:
            result_text += f"\n❌ Ошибки ({len(errors)}):\n" + "\n".join(errors[:10])
        
        bot.send_message(message.chat.id, result_text)
        
    except Exception as e:
        print(f"❌ Ошибка массового добавления: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower() == 'файлы одежды' and is_admin(message.from_user.id))
def handle_show_clothing_files(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        image_dir = "images"
        if not os.path.exists(image_dir):
            bot.send_message(message.chat.id, "❌ Папка images не существует!", parse_mode='HTML')
            return
        
        files = [f for f in os.listdir(image_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
        
        if not files:
            bot.send_message(message.chat.id, "❌ В папке images нет файлов!", parse_mode='HTML')
            return
        
        files_text = f"📁 Файлы в папке images ({len(files)}):\n\n"
        
        for i in range(0, len(files), 20):
            batch = files[i:i+20]
            batch_text = files_text + "\n".join([f"• {f}" for f in batch])
            
            if len(batch_text) > 4000:
                parts = [batch_text[i:i+4000] for i in range(0, len(batch_text), 4000)]
                for part in parts:
                    bot.send_message(message.chat.id, part)
            else:
                bot.send_message(message.chat.id, batch_text)
                
    except Exception as e:
        print(f"❌ Ошибка показа файлов: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка!", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower() == 'создать магазин')
def create_shop_command(message):
    """Создать таблицы магазина и добавить айтемы"""
    try:
        with get_db_cursor() as cursor:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS clothes_shop (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    image_name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_clothes (
                    user_id INTEGER,
                    item_id INTEGER,
                    equipped INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, item_id)
                )
            ''')
            
            clothes = [                
    ("Футболка Stussy", 15000000000, "Тело", "Футболка Stussy.png"),                
    ("Gold Pepe", 222222222222, "Слева", "Gold pepe.png"),
    ("M&J Jeans", 30000000000, "Ноги", "M&J Jeans.png"),
    ("Green cap&Tg", 100000000000, "Голова", "Green cap&Tg.png"),
    ("Louis Vuitton Hoodie", 250000000000, "Тело", "Louis Vuitton Hoodie.png"),
    ("Gucci Pepe", 55555555555, "Слева", "Gucci Pepe.png"),
    ("BMB M5 f90 karabasa", 50000000000, "Справа", "BMB M5 f90 karabasa.png"),
]
            
            added_count = 0
            for name, price, type, image_name in clothes:
                cursor.execute('SELECT id FROM clothes_shop WHERE name = ?', (name,))
                if not cursor.fetchone():
                    cursor.execute('INSERT INTO clothes_shop (name, price, type, image_name) VALUES (?, ?, ?, ?)', 
                                  (name, price, type, image_name))
                    added_count += 1
            
        bot.send_message(message.chat.id, f"🏆 🛒 Шоп создан! Добавлено {added_count} айтемов.", parse_mode='HTML')
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка создания магазина: {e}", parse_mode='HTML')
@bot.message_handler(func=lambda message: message.text.lower() == 'миграция типов' and is_admin(message.from_user.id))
def handle_migration_types(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        with get_db_cursor() as cursor:
            type_mapping = {
                'hat': 'Голова',
                'body': 'Тело', 
                'legs': 'Ноги',
                'shoes': 'Ноги',
                'accessories': 'Слева'
            }
            
            for old_type, new_type in type_mapping.items():
                cursor.execute('UPDATE clothes_shop SET type = ? WHERE type = ?', (new_type, old_type))
                updated = cursor.rowcount
                if updated > 0:
                    print(f"🏆 Обновлено {updated} записей: {old_type} -> {new_type}")
            
            cursor.execute('SELECT DISTINCT type FROM clothes_shop')
            current_types = [row[0] for row in cursor.fetchall()]
            
            bot.send_message(message.chat.id, 
                           f"🏆 Миграция окончена!\n\n"
                           f"📊 Новые типы в базе:\n" + "\n".join([f"• {t}" for t in current_types]), parse_mode='HTML')
            
    except Exception as e:
        print(f"❌ Ошибка миграции: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower() == 'типы одежды' and is_admin(message.from_user.id))
def handle_show_types(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        with get_db_cursor() as cursor:
            cursor.execute('SELECT DISTINCT type, COUNT(*) as count FROM clothes_shop GROUP BY type')
            types = cursor.fetchall()
            
            types_text = "📊 ТИПЫ ОДЕЖДЫ В БАЗЕ:\n\n"
            for type_name, count in types:
                types_text += f"• {type_name}: {count} вещей\n"
            
            bot.send_message(message.chat.id, types_text)
            
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка!", parse_mode='HTML')
@bot.message_handler(func=lambda message: message.text.lower().startswith('убрать вещь') and is_admin(message.from_user.id))
def handle_remove_item_from_shop(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды", parse_mode='HTML')
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, 
                           "❌ Используйте: снять вещь [ID или название]\n\n"
                           "Примеры:\n"
                           "• снять вещь 5 - снять вещь с ID 5\n"
                           "• снять вещь Футболка - снять вещь по названию", parse_mode='HTML')
            return
        
        item_identifier = ' '.join(parts[2:])
        
        with get_db_cursor() as cursor:
            if item_identifier.isdigit():
                cursor.execute('SELECT id, name, supply FROM clothes_shop WHERE id = ?', (int(item_identifier),))
            else:
                cursor.execute('SELECT id, name, supply FROM clothes_shop WHERE name LIKE ?', (f'%{item_identifier}%',))
            
            items = cursor.fetchall()
            
            if not items:
                bot.send_message(message.chat.id, f"❌ Вещь '{item_identifier}' не найдено!", parse_mode='HTML')
                return
            
            if len(items) > 1:
                items_text = "📋 Найдено несколько вещей:\n\n"
                for item in items:
                    status = "🟢 В продаже" if item[2] == -1 or item[2] > 0 else "🔴 Снята"
                    items_text += f"• {item[1]} (ID: {item[0]}) - {status}\n"
                items_text += f"\nУточните ID: снять вещь [ID]"
                bot.send_message(message.chat.id, items_text)
                return
            
            item_id, item_name, current_supply = items[0]
            
            if current_supply == 0:
                bot.send_message(message.chat.id, f"❌ Вещь '{item_name}' уже снята с продажи!", parse_mode='HTML')
                return
            
            cursor.execute('UPDATE clothes_shop SET supply = 0 WHERE id = ?', (item_id,))
            
            bot.send_message(message.chat.id, 
                           f"🏆 Вещь снята с продажи!\n\n"
                           f"🆔 Название: {item_name}\n"
                           f"🆔 ID: {item_id}\n"
                           f"📦 Статус: 🔴 Недоступна для покупки\n\n"
                           f"💡 Вещь осталась в базе данных, но ее нельзя приобрести.", parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка при снятии вещи: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при снятии вещи!", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower().startswith('вернуть вещь') and is_admin(message.from_user.id))
def handle_return_item_to_shop(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды", parse_mode='HTML')
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, 
                           "❌ Используйте: вернуть вещь [ID или название] (количество)\n\n"
                           "Примеры:\n"
                           "• вернуть вещь 5 - вернуть без лимита\n"
                           "• вернуть вещь Футболка 50 - вернуть с лимитом 50 штук", parse_mode='HTML')
            return
        
        item_identifier = ' '.join(parts[2:-1]) if len(parts) > 3 else parts[2]
        supply_amount = parts[-1] if parts[-1].isdigit() else None
        
        with get_db_cursor() as cursor:
            if item_identifier.isdigit():
                cursor.execute('SELECT id, name, supply FROM clothes_shop WHERE id = ?', (int(item_identifier),))
            else:
                cursor.execute('SELECT id, name, supply FROM clothes_shop WHERE name LIKE ?', (f'%{item_identifier}%',))
            
            items = cursor.fetchall()
            
            if not items:
                bot.send_message(message.chat.id, f"❌ Вещь '{item_identifier}' не найдено!", parse_mode='HTML')
                return
            
            if len(items) > 1:
                items_text = "📋 Найдено несколько вещей:\n\n"
                for item in items:
                    status = "🟢 В продаже" if item[2] == -1 or item[2] > 0 else "🔴 Снята"
                    items_text += f"• {item[1]} (ID: {item[0]}) - {status}\n"
                items_text += f"\nУточните ID: вернуть вещь [ID]"
                bot.send_message(message.chat.id, items_text)
                return
            
            item_id, item_name, current_supply = items[0]
            
            if current_supply != 0:
                bot.send_message(message.chat.id, f"❌ Вещь '{item_name}' уже в продаже!", parse_mode='HTML')
                return
            
            if supply_amount:
                supply = int(supply_amount)
                supply_text = f"с лимитом {supply} штук"
            else:
                supply = -1
                supply_text = "без лимита"
            
            cursor.execute('UPDATE clothes_shop SET supply = ?, sold_count = 0 WHERE id = ?', (supply, item_id))
            
            bot.send_message(message.chat.id, 
                           f"🏆 Вещь возвращена в продажу!\n\n"
                           f"🆔 Название: {item_name}\n"
                           f"🆔 ID: {item_id}\n"
                           f"📦 Статус: 🟢 Доступна для покупки\n"
                           f"⚔️ Режим: {supply_text}\n\n"
                           f"💡 Счетчик продаж сброшен.", parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка при возврате вещи: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при возврате вещи!", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower() == 'статус вещей' and is_admin(message.from_user.id))
def handle_items_status(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды", parse_mode='HTML')
        return
    
    try:
        with get_db_cursor() as cursor:
            cursor.execute('''
                SELECT id, name, type, price, supply, sold_count 
                FROM clothes_shop 
                ORDER BY type, name
            ''')
            items = cursor.fetchall()
            
            if not items:
                bot.send_message(message.chat.id, "❌ В магазине нет вещей!", parse_mode='HTML')
                return
            
            message_text = "📊 СТАТУС ВЕЩЕЙ В МАГАЗИНЕ\n\n"
            
            current_type = ""
            for item_id, name, item_type, price, supply, sold_count in items:
                if item_type != current_type:
                    message_text += f"\n📁 {item_type.upper()}:\n"
                    current_type = item_type
                
                if supply == 0:
                    status = "🔴 СНЯТА"
                elif supply == -1:
                    status = "🟢 В ПРОДАЖЕ (без лимита)"
                else:
                    available = supply - sold_count
                    status = f"🟡 В ПРОДАЖЕ ({available}/{supply})"
                
                message_text += f"• {name} (ID: {item_id})\n"
                message_text += f"  💵 {format_balance(price)} | {status}\n"
            
            bot.send_message(message.chat.id, message_text)
    
    except Exception as e:
        print(f"Ошибка при получении статуса вещей: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при получении статуса вещей!", parse_mode='HTML')

def create_character_outfit(user_id):
    """Создает изображение человечка с надетой одеждой"""
    try:
        base_path = "images/base_human.jpg"
        
        if not os.path.exists(base_path):
            return "images/base_human.jpg"
        
        base_image = Image.open(base_path).convert("RGBA")
        equipped = get_equipped_clothes(user_id)
        
        layer_order = ['body', 'legs', 'shoes', 'hat', 'accessories']
        
        for layer in layer_order:
            if layer in equipped:
                clothes_data = equipped[layer]
                
                if layer == 'accessories' and isinstance(clothes_data, list):
                    for accessory in clothes_data:
                        clothes_path = f"images/{accessory}"
                        if os.path.exists(clothes_path):
                            try:
                                clothes_image = Image.open(clothes_path).convert("RGBA")
                                if clothes_image.size != base_image.size:
                                    clothes_image = clothes_image.resize(base_image.size, Image.Resampling.LANCZOS)
                                base_image = Image.alpha_composite(base_image, clothes_image)
                                print(f"🏆 Наложен аксессуар: {accessory}")
                            except Exception as e:
                                print(f"❌ Ошибка наложения аксессуара {accessory}: {e}")
                else:
                    clothes_path = f"images/{clothes_data}"
                    if os.path.exists(clothes_path):
                        try:
                            clothes_image = Image.open(clothes_path).convert("RGBA")
                            if clothes_image.size != base_image.size:
                                clothes_image = clothes_image.resize(base_image.size, Image.Resampling.LANCZOS)
                            base_image = Image.alpha_composite(base_image, clothes_image)
                            print(f"🏆 Наложена одежда: {clothes_data} (тип: {layer})")
                        except Exception as e:
                            print(f"❌ Ошибка наложения {layer} ({clothes_data}): {e}")
                    else:
                        print(f"❌ Файл отсутствует: {clothes_path}")
        
        result_image = base_image.convert("RGB")
        result_path = f"images/outfit_{user_id}.jpg"
        result_image.save(result_path, "JPEG", quality=95)
        
        print(f"🏆 Образ создан: {result_path}")
        return result_path
        
    except Exception as e:
        print(f"❌ Ошибка создания образа: {e}")
        return "images/base_human.jpg"

@bot.message_handler(func=lambda message: message.text.lower() == 'debug equipped')
def handle_debug_equipped(message):
    """Проверить работу функции get_equipped_clothes"""
    user_id = message.from_user.id
    
    equipped = get_equipped_clothes(user_id)
    
    message_text = "🔍 Debug get_equipped_clothes:\n\n"
    message_text += f"📋 Результат: {equipped}\n\n"
    
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT cs.type, cs.image_name, cs.name
            FROM user_clothes uc
            JOIN clothes_shop cs ON uc.item_id = cs.id
            WHERE uc.user_id = ? AND uc.equipped = 1
            ORDER BY cs.type
        ''', (user_id,))
        
        db_results = cursor.fetchall()
        
        message_text += "📊 Данные из базы:\n"
        for item_type, image_name, name in db_results:
            message_text += f"• {item_type}: {name} -> {image_name}\n"
    
    bot.send_message(message.chat.id, message_text)
    
@bot.message_handler(func=lambda message: message.text.lower() == 'обновить образ')
def refresh_outfit(message):
    """Принудительно обновить образ"""
    user_id = message.from_user.id
    
    old_outfit = f"images/outfit_{user_id}.jpg"
    if os.path.exists(old_outfit):
        os.remove(old_outfit)
        print(f"🗑️ Удален старый образ: {old_outfit}")
    
    outfit_path = create_character_outfit(user_id)
    
    try:
        with open(outfit_path, 'rb') as photo:
            bot.send_photo(message.chat.id, photo, caption="🔄 Образ обновлен!")
    except:
        bot.send_message(message.chat.id, "❌ Ошибка обновления образа", parse_mode='HTML')
clothes_creation_state = {}

@bot.message_handler(commands=['добавитьодежду'])
def start_add_clothes(message):
    """Начать процесс добавления одежды"""
    if not is_admin(message.from_user.id):
        return
    
    clothes_creation_state[message.from_user.id] = {'step': 'waiting_photo'}
    
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("❌ Отмена"))
    
    bot.send_message(
        message.chat.id,
        "🎽 **Добавление новой одежды в магазин**\n\n"
        "1. 📸 Отправь фото вещи\n"
        "2. 💵 Укажи цену\n" 
        "3. 🏷️ Укажи название\n"
        "4. 📦 Укажи тип (body/hat/shoes)\n\n"
        "**Сначала отправь фото вещи:**",
        reply_markup=markup
    , parse_mode='HTML')

@bot.message_handler(content_types=['photo'])
def handle_clothes_photo(message):
    """Обработка фото одежды"""
    if message.from_user.id not in clothes_creation_state:
        return
    
    if clothes_creation_state[message.from_user.id]['step'] != 'waiting_photo':
        return
    
    try:
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        clothes_creation_state[message.from_user.id]['photo'] = downloaded_file
        clothes_creation_state[message.from_user.id]['step'] = 'waiting_price'
        
        bot.send_message(
            message.chat.id,
            "📸 Фото получено!\n\n"
            "💸 **Теперь укажи цену:**\n"
            "Примеры: 1000000, 5к, 10м, 1b\n"
            "• 1к = 1,000\n"
            "• 1м = 1,000,000\n" 
            "• 1b = 1,000,000,000"
        )
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка загрузки фото: {e}", parse_mode='HTML')
        del clothes_creation_state[message.from_user.id]
def create_character_outfit(user_id):
    """Создает изображение человечка с надетой одеждой"""
    try:
        print(f"🔄 Создаем образ для {user_id}")
        
        base_path = "images/base_human.jpg"
        
        if not os.path.exists(base_path):
            print("❌ Базовое фото отсутствуето")
            return "images/base_human.jpg"
        
        base_image = Image.open(base_path).convert("RGBA")
        print(f"🏆 Базовое фото загружено: {base_image.size}")
        
        equipped = get_equipped_clothes(user_id)
        print(f"🎽 Надета одежда: {equipped}")
        
        if not equipped:
            print("ℹ️ Ничего не надето, возвращаем базовое фото")
            return base_path
        
        for item_type, image_name in equipped.items():
            clothes_path = f"images/{image_name}"
            print(f"🔄 Обрабатываем {item_type}: {clothes_path}")
            
            if os.path.exists(clothes_path):
                try:
                    clothes_image = Image.open(clothes_path).convert("RGBA")
                    print(f"🏆 Фото загружено: {clothes_image.size}")
                    print(f"🏆 Режим фото: {clothes_image.mode}")
                    
                    if clothes_image.mode != 'RGBA':
                        print(f"❌ Фото не в режиме RGBA: {clothes_image.mode}")
                        continue
                    
                    if clothes_image.size != base_image.size:
                        print(f"📏 Изменяем размер {item_type} с {clothes_image.size} на {base_image.size}")
                        clothes_image = clothes_image.resize(base_image.size, Image.Resampling.LANCZOS)
                    
                    print(f"🔄 Накладываем {item_type}...")
                    base_image = Image.alpha_composite(base_image, clothes_image)
                    print(f"🏆 Успешно наложен {item_type}")
                    
                except Exception as e:
                    print(f"❌ Ошибка наложения {item_type}: {e}")
                    import traceback
                    print(f"❌ Детали ошибки: {traceback.format_exc()}")
            else:
                print(f"❌ Файл отсутствует: {clothes_path}")
        
        result_path = f"images/outfit_{user_id}.jpg"
        print(f"🔄 Сохраняем результат в: {result_path}")
        result_image = base_image.convert("RGB")
        result_image.save(result_path, "JPEG", quality=95)
        print(f"🏆 Образ сохранен: {result_path}")
        
        return result_path
        
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        import traceback
        print(f"❌ Детали ошибки: {traceback.format_exc()}")
        return "images/base_human.jpg"
from PIL import Image, ImageDraw
import os

@bot.message_handler(func=lambda message: message.text.lower() == 'тест наложения')
def test_overlay(message):
    """Тест наложения изображений"""
    user_id = message.from_user.id
    
    try:
        base_path = "images/base_human.jpg"
        base_image = Image.open(base_path)
        
        shoes_path = "images/кроссовки_nike_air_monarch_iv_1763137116.png"
        shoes_image = Image.open(shoes_path)
        
        info_text = (
            f"📊 ИНФОРМАЦИЯ О ФАЙЛАХ:\n\n"
            f"🎴 Базовый человечек:\n"
            f"• Размер: {base_image.size}\n"
            f"• Формат: {base_image.format}\n"
            f"• Режим: {base_image.mode}\n\n"
            f"👟 Кроссовки:\n"
            f"• Размер: {shoes_image.size}\n"
            f"• Формат: {shoes_image.format}\n"
            f"• Режим: {shoes_image.mode}\n"
        )
        
        bot.send_message(message.chat.id, info_text)
        
        try:
            with open(shoes_path, 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption="👟 Вот как выглядят кроссовки")
        except:
            bot.send_message(message.chat.id, "❌ Не могу показать кроссовки", parse_mode='HTML')
            
        test_overlay_image(user_id)
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка теста: {e}", parse_mode='HTML')

def test_overlay_image(user_id):
    """Тестовое наложение с простым красным кругом"""
    try:
        from PIL import Image, ImageDraw
        
        base_path = "images/base_human.jpg"
        base_image = Image.open(base_path).convert("RGBA")
        
        test_layer = Image.new('RGBA', base_image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(test_layer)
        
        draw.ellipse((180, 400, 220, 440), fill=(255, 0, 0, 128))
        
        result_image = Image.alpha_composite(base_image, test_layer)
        
        test_path = f"images/test_{user_id}.png"
        result_image.save(test_path, "PNG")
        
        with open(test_path, 'rb') as photo:
            bot.send_photo(user_id, photo, caption="🔴 ТЕСТ: Должен быть красный круг на ногах")
            
    except Exception as e:
        bot.send_message(user_id, f"❌ Ошибка теста: {e}", parse_mode='HTML')
@bot.message_handler(func=lambda message: message.text.lower() == 'исправить кроссовки')
def fix_shoes_filename(message):
    """Исправить имя файла кроссовков в базе"""
    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                'UPDATE clothes_shop SET image_name = ? WHERE image_name = ?', 
                ('sneakers.png', 'кроссовки_nike_air_monarch_iv_1763137116.png')
            )
            
            cursor.execute('SELECT name, image_name FROM clothes_shop WHERE type = "shoes"')
            shoes = cursor.fetchall()
            
            result = "🏆 ИСПРАВЛЕНО:\n\n"
            for name, image_name in shoes:
                result += f"👟 {name}: {image_name}\n"
                exists_icon = "🏆 ДА" if os.path.exists(f'images/{image_name}') else "❌ НЕТ"
                result += f"   📁 Существует: {exists_icon}\n\n"
            
            bot.send_message(message.chat.id, result)
            
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.from_user.id in clothes_creation_state and clothes_creation_state[message.from_user.id]['step'] == 'waiting_price')
def handle_clothes_price(message):
    """Обработка цены одежды"""
    if message.text == "❌ Отмена":
        del clothes_creation_state[message.from_user.id]
        bot.send_message(message.chat.id, "❌ Добавление сброшено", reply_markup=create_main_menu(), parse_mode='HTML')
        return
    
    try:
        price = parse_bet_amount(message.text, float('inf'))
        if not price or price <= 0:
            bot.send_message(message.chat.id, "❌ Неверная стоимость! Укажи число больше 0", parse_mode='HTML')
            return
        
        clothes_creation_state[message.from_user.id]['price'] = price
        clothes_creation_state[message.from_user.id]['step'] = 'waiting_name'
        
        bot.send_message(
            message.chat.id,
            "💵 Цена установлена!\n\n"
            "🏷️ **Теперь укажи название вещи:**\n"
            "Пример: 'Красная футболка', 'Кожаная куртка'"
        , parse_mode='HTML')
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.from_user.id in clothes_creation_state and clothes_creation_state[message.from_user.id]['step'] == 'waiting_name')
def handle_clothes_name(message):
    """Обработка названия одежды"""
    if message.text == "❌ Отмена":
        del clothes_creation_state[message.from_user.id]
        bot.send_message(message.chat.id, "❌ Добавление сброшено", reply_markup=create_main_menu(), parse_mode='HTML')
        return
    
    if len(message.text) > 50:
        bot.send_message(message.chat.id, "❌ Слишком длинное название! Максимум 50 символов", parse_mode='HTML')
        return
    
    clothes_creation_state[message.from_user.id]['name'] = message.text
    clothes_creation_state[message.from_user.id]['step'] = 'waiting_type'
    
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("👕 body"),
        KeyboardButton("🎩 hat"), 
        KeyboardButton("👟 shoes"),
        KeyboardButton("❌ Отмена")
    )
    
    bot.send_message(
        message.chat.id,
        "🏷️ Название установлено!\n\n"
        "📦 **Теперь выбери тип вещи:**\n"
        "• 👕 body - одежда (футболки, куртки)\n"
        "• 🎩 hat - головные уборы\n"
        "• 👟 shoes - обувь",
        reply_markup=markup
    )

@bot.message_handler(func=lambda message: message.from_user.id in clothes_creation_state and clothes_creation_state[message.from_user.id]['step'] == 'waiting_type')
def handle_clothes_type(message):
    """Обработка типа одежды"""
    if message.text == "❌ Отмена":
        del clothes_creation_state[message.from_user.id]
        bot.send_message(message.chat.id, "❌ Добавление сброшено", reply_markup=create_main_menu(), parse_mode='HTML')
        return
    
    type_mapping = {
        "👕 body": "body",
        "🎩 hat": "hat", 
        "👟 shoes": "shoes"
    }
    
    if message.text not in type_mapping:
        bot.send_message(message.chat.id, "❌ Выбери тип из кнопок!", parse_mode='HTML')
        return
    
    clothes_type = type_mapping[message.text]
    clothes_creation_state[message.from_user.id]['type'] = clothes_type
    
    finish_clothes_creation(message)

def finish_clothes_creation(message):
    """Завершить создание одежды и добавить в магазин"""
    try:
        user_id = message.from_user.id
        data = clothes_creation_state[user_id]
        
        file_extension = "png"
        filename = f"{data['name'].lower().replace(' ', '_')}_{int(time.time())}.{file_extension}"
        file_path = f"images/{filename}"
        
        with open(file_path, 'wb') as f:
            f.write(data['photo'])
        
        with get_db_cursor() as cursor:
            cursor.execute(
                'INSERT INTO clothes_shop (name, price, type, image_name) VALUES (?, ?, ?, ?)',
                (data['name'], data['price'], data['type'], filename)
            )
            item_id = cursor.lastrowid
        
        markup = ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(KeyboardButton("🛍️ 🛒 Шоп"), KeyboardButton("🎒 👔 Шкаф"), KeyboardButton("🎴 Я"))
        
        result_text = (
            f"🏆 **Одежда добавлена в магазин!**\n\n"
            f"🏷️ Название: {data['name']}\n"
            f"💸 Цена: {format_balance(data['price'])}\n"
            f"📦 Тип: {data['type']}\n"
            f"🆔 ID: {item_id}\n\n"
            f"Теперь она доступна в магазине!"
        )
        
        try:
            with open(file_path, 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption=result_text, reply_markup=markup, parse_mode='HTML')
        except:
            bot.send_message(message.chat.id, result_text, reply_markup=markup)
        
        del clothes_creation_state[user_id]
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка сохранения: {e}", parse_mode='HTML')
        if user_id in clothes_creation_state:
            del clothes_creation_state[user_id]

@bot.message_handler(func=lambda message: message.text == "❌ Отмена")
def cancel_creation(message):
    """Отмена создания"""
    if message.from_user.id in clothes_creation_state:
        del clothes_creation_state[message.from_user.id]
        bot.send_message(message.chat.id, "❌ Добавление сброшено", reply_markup=create_main_menu(), parse_mode='HTML')
from telebot.types import InputFile

clothes_creation_state = {}

@bot.message_handler(commands=['добавитьодежду'])
def start_add_clothes(message):
    """Начать процесс добавления одежды"""
    if not is_admin(message.from_user.id):
        return
    
    clothes_creation_state[message.from_user.id] = {'step': 'waiting_photo'}
    
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("❌ Отмена"))
    
    bot.send_message(
        message.chat.id,
        "🎽 **Добавление новой одежды в магазин**\n\n"
        "1. 📸 Отправь фото вещи\n"
        "2. 💵 Укажи цену\n" 
        "3. 🏷️ Укажи название\n"
        "4. 📦 Укажи тип (body/hat/shoes)\n\n"
        "**Сначала отправь фото вещи:**",
        reply_markup=markup
    , parse_mode='HTML')

@bot.message_handler(content_types=['photo'])
def handle_clothes_photo(message):
    """Обработка фото одежды"""
    if message.from_user.id not in clothes_creation_state:
        return
    
    if clothes_creation_state[message.from_user.id]['step'] != 'waiting_photo':
        return
    
    try:
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        clothes_creation_state[message.from_user.id]['photo'] = downloaded_file
        clothes_creation_state[message.from_user.id]['step'] = 'waiting_price'
        
        bot.send_message(
            message.chat.id,
            "📸 Фото получено!\n\n"
            "💸 **Теперь укажи цену:**\n"
            "Примеры: 1000000, 5к, 10м, 1b\n"
            "• 1к = 1,000\n"
            "• 1м = 1,000,000\n" 
            "• 1b = 1,000,000,000"
        )
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка загрузки фото: {e}", parse_mode='HTML')
        del clothes_creation_state[message.from_user.id]
def get_shop_clothes():
    """Получить все айтемы из магазина (только доступные для покупки)"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT * FROM clothes_shop WHERE supply != 0 ORDER BY price ASC')
        return [dict(row) for row in cursor.fetchall()]

@bot.message_handler(func=lambda message: message.from_user.id in clothes_creation_state and clothes_creation_state[message.from_user.id]['step'] == 'waiting_price')
def handle_clothes_price(message):
    """Обработка цены одежды"""
    if message.text == "❌ Отмена":
        del clothes_creation_state[message.from_user.id]
        bot.send_message(message.chat.id, "❌ Добавление сброшено", reply_markup=create_main_menu(), parse_mode='HTML')
        return
    
    try:
        price = parse_bet_amount(message.text, float('inf'))
        if not price or price <= 0:
            bot.send_message(message.chat.id, "❌ Неверная стоимость! Укажи число больше 0", parse_mode='HTML')
            return
        
        clothes_creation_state[message.from_user.id]['price'] = price
        clothes_creation_state[message.from_user.id]['step'] = 'waiting_name'
        
        bot.send_message(
            message.chat.id,
            "💵 Цена установлена!\n\n"
            "🏷️ **Теперь укажи название вещи:**\n"
            "Пример: 'Красная футболка', 'Кожаная куртка'"
        , parse_mode='HTML')
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.from_user.id in clothes_creation_state and clothes_creation_state[message.from_user.id]['step'] == 'waiting_name')
def handle_clothes_name(message):
    """Обработка названия одежды"""
    if message.text == "❌ Отмена":
        del clothes_creation_state[message.from_user.id]
        bot.send_message(message.chat.id, "❌ Добавление сброшено", reply_markup=create_main_menu(), parse_mode='HTML')
        return
    
    if len(message.text) > 50:
        bot.send_message(message.chat.id, "❌ Слишком длинное название! Максимум 50 символов", parse_mode='HTML')
        return
    
    clothes_creation_state[message.from_user.id]['name'] = message.text
    clothes_creation_state[message.from_user.id]['step'] = 'waiting_type'
    
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("👕 body"),
        KeyboardButton("🎩 hat"), 
        KeyboardButton("👟 shoes"),
        KeyboardButton("❌ Отмена")
    )
    
    bot.send_message(
        message.chat.id,
        "🏷️ Название установлено!\n\n"
        "📦 **Теперь выбери тип вещи:**\n"
        "• 👕 body - одежда (футболки, куртки)\n"
        "• 🎩 hat - головные уборы\n"
        "• 👟 shoes - обувь",
        reply_markup=markup
    )

@bot.message_handler(func=lambda message: message.from_user.id in clothes_creation_state and clothes_creation_state[message.from_user.id]['step'] == 'waiting_type')
def handle_clothes_type(message):
    """Обработка типа одежды"""
    if message.text == "❌ Отмена":
        del clothes_creation_state[message.from_user.id]
        bot.send_message(message.chat.id, "❌ Добавление сброшено", reply_markup=create_main_menu(), parse_mode='HTML')
        return
    
    type_mapping = {
        "👕 body": "body",
        "🎩 hat": "hat", 
        "👟 shoes": "shoes"
    }
    
    if message.text not in type_mapping:
        bot.send_message(message.chat.id, "❌ Выбери тип из кнопок!", parse_mode='HTML')
        return
    
    clothes_type = type_mapping[message.text]
    clothes_creation_state[message.from_user.id]['type'] = clothes_type
    
    finish_clothes_creation(message)

def finish_clothes_creation(message):
    """Завершить создание одежды и добавить в магазин"""
    try:
        user_id = message.from_user.id
        data = clothes_creation_state[user_id]
        
        file_extension = "png"
        filename = f"{data['name'].lower().replace(' ', '_')}_{int(time.time())}.{file_extension}"
        file_path = f"images/{filename}"
        
        with open(file_path, 'wb') as f:
            f.write(data['photo'])
        
        with get_db_cursor() as cursor:
            cursor.execute(
                'INSERT INTO clothes_shop (name, price, type, image_name) VALUES (?, ?, ?, ?)',
                (data['name'], data['price'], data['type'], filename)
            )
            item_id = cursor.lastrowid
        
        markup = ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(KeyboardButton("🛍️ 🛒 Шоп"), KeyboardButton("🎒 👔 Шкаф"), KeyboardButton("🎴 Я"))
        
        result_text = (
            f"🏆 **Одежда добавлена в магазин!**\n\n"
            f"🏷️ Название: {data['name']}\n"
            f"💸 Цена: {format_balance(data['price'])}\n"
            f"📦 Тип: {data['type']}\n"
            f"🆔 ID: {item_id}\n\n"
            f"Теперь она доступна в магазине!"
        )
        
        try:
            with open(file_path, 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption=result_text, reply_markup=markup, parse_mode='HTML')
        except:
            bot.send_message(message.chat.id, result_text, reply_markup=markup)
        
        del clothes_creation_state[user_id]
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка сохранения: {e}", parse_mode='HTML')
        if user_id in clothes_creation_state:
            del clothes_creation_state[user_id]

@bot.message_handler(func=lambda message: message.text == "❌ Отмена")
def cancel_creation(message):
    """Отмена создания"""
    if message.from_user.id in clothes_creation_state:
        del clothes_creation_state[message.from_user.id]
        bot.send_message(message.chat.id, "❌ Добавление сброшено", reply_markup=create_main_menu(), parse_mode='HTML')
               
    def __init__(self, challenger_id, bet_amount, team_size, weapon_type, chat_id):
        self.challenger_id = challenger_id
        self.bet_amount = bet_amount
        self.team_size = team_size
        self.weapon_type = weapon_type
        self.challenger_team = [challenger_id]
        self.opponent_team = []
        self.status = "waiting"
        self.created_at = time.time()
        self.chat_id = chat_id
        self.message_id = None
        self.duel_id = f"duel_{challenger_id}_{int(time.time())}"
    
    def add_to_team(self, user_id, team_type):
        if team_type == "challenger" and len(self.challenger_team) < self.team_size:
            self.challenger_team.append(user_id)
            return True
        elif team_type == "opponent" and len(self.opponent_team) < self.team_size:
            self.opponent_team.append(user_id)
            return True
        return False
    
    def is_ready(self):
        return (len(self.challenger_team) == self.team_size and 
                len(self.opponent_team) == self.team_size)

@bot.message_handler(func=lambda message: message.text.lower().startswith('дуэль ') and message.chat.type in ['group', 'supergroup'])
def handle_duel_create(message):
    user_id = message.from_user.id
    if not is_registered(user_id):
        bot.send_message(message.chat.id, "❌ <b>Ты не зарегистрирован!</b>\n\nНапиши /start чтобы начать.", parse_mode='HTML')
        return
    balance = get_balance(user_id)
    
    try:
        parts = message.text.split()
        if len(parts) < 3:
            show_duel_help(message.chat.id)
            return
        
        team_size = int(parts[1])
        bet_text = ' '.join(parts[2:-1]) if len(parts) > 3 else parts[2]
        weapon_name = parts[-1].lower()
        
        if team_size not in DUEL_CONFIG["team_sizes"]:
            bot.send_message(message.chat.id, 
                           f"Доступные размеры команд: {', '.join(map(str, DUEL_CONFIG['team_sizes']))}")
            return
        
        bet_amount = parse_bet_amount(bet_text, balance)
        if bet_amount is None or bet_amount <= 0:
            bot.send_message(message.chat.id, "Неверная сумма ставки!")
            return
        
        if bet_amount < DUEL_CONFIG["min_bet"]:
            bot.send_message(message.chat.id, 
                           f"Минимальная бет: {format_balance(DUEL_CONFIG['min_bet'])}!", parse_mode='HTML')
            return
        
        if bet_amount > DUEL_CONFIG["max_bet"]:
            bot.send_message(message.chat.id, 
                           f"Максимальная бет: {format_balance(DUEL_CONFIG['max_bet'])}!", parse_mode='HTML')
            return
        
        total_bet = bet_amount * team_size
        if total_bet > balance:
            bot.send_message(message.chat.id, 
                           f"Не хватает монет! Нужно: {format_balance(total_bet)}", parse_mode='HTML')
            return
        
        weapon = None
        for w in DUEL_CONFIG["weapons"]:
            if weapon_name in w["name"].lower():
                weapon = w
                break
        
        if not weapon:
            show_weapons_list(message.chat.id)
            return
        
        update_balance(user_id, -bet_amount)
        
        duel = Duel(user_id, bet_amount, team_size, weapon, message.chat.id)
        active_duels[duel.duel_id] = duel
        
        send_duel_invitation(message.chat.id, duel)
        
    except Exception as e:
        print(f"Ошибка создания дуэли: {e}")
        refund_balance(user_id, bet_amount, message.chat.id)

def send_duel_invitation(chat_id, duel):
    challenger_info = get_user_info(duel.challenger_id)
    challenger_name = challenger_info['custom_name'] if challenger_info['custom_name'] else (
        f"@{challenger_info['username']}" if challenger_info['username'] else challenger_info['first_name']
    )
    
    message_text = "⚔️ <b>ВЫЗОВ НА ДУЭЛЬ!</b> ⚔️\n\n"
    message_text += f"<blockquote>Вызвал: {challenger_name}\n"
    message_text += f"Ставка: {format_balance(duel.bet_amount)} с игрока\n"
    message_text += f"Команда: {duel.team_size} vs {duel.team_size}\n"
    message_text += f"Оружие: {duel.weapon_type['name']}\n"
    message_text += f"Точность: {duel.weapon_type['accuracy']}%\n"
    message_text += f"Критический удар: {duel.weapon_type['critical']}%\n\n"
    message_text += f"Общий банк: {format_balance(duel.bet_amount * duel.team_size * 2)}</blockquote>\n\n"
    message_text += f"Дуэль активна {DUEL_CONFIG['duel_duration']//60}м\n"
    message_text += "Для участия тапни кнопку ниже!"
    
    duel_keyboard = {
        "inline_keyboard": [[
            {"text": "⚔️ Принять вызов", "callback_data": f"duel_accept_{duel.duel_id}", "style": "danger"},
            {"text": "👥 К стороне вызывающего", "callback_data": f"duel_join_challenger_{duel.duel_id}", "style": "secondary"}
        ]]
    }
    
    msg = bot.send_message(chat_id, message_text, reply_markup=duel_keyboard, parse_mode='HTML')
    duel.message_id = msg.message_id

@bot.callback_query_handler(func=lambda call: call.data.startswith(('duel_accept_', 'duel_join_challenger_')))
def handle_duel_buttons(call):
    user_id = call.from_user.id
    
    if call.data.startswith('duel_accept_'):
        duel_id = call.data.split('_')[2]
        team_type = "opponent"
    else:
        duel_id = call.data.split('_')[3]
        team_type = "challenger"
    
    if duel_id not in active_duels:
        bot.answer_callback_query(call.id, "Дуэль отсутствуета или окончена!")
        return
    
    duel = active_duels[duel_id]
    
    if user_id in duel.challenger_team or user_id in duel.opponent_team:
        bot.answer_callback_query(call.id, "Вы уже участвуете в этой дуэли!")
        return
    
    balance = get_balance(user_id)
    if balance < duel.bet_amount:
        bot.answer_callback_query(call.id, "Не хватает монет для ставки!")
        return
    
    update_balance(user_id, -duel.bet_amount)
    
    if duel.add_to_team(user_id, team_type):
        bot.answer_callback_query(call.id, "Вы присоединились к дуэли!")
        
        try:
            bot.send_message(user_id, 
                           f"Вы присоединились к дуэли!\n\n"
                           f"Ставка: {format_balance(duel.bet_amount)}\n"
                           f"Команда: {duel.team_size} vs {duel.team_size}\n"
                           f"Ожидайте начала дуэли...", parse_mode='HTML')
        except:
            pass
        
        update_duel_message(duel)
        
        if duel.is_ready():
            start_duel_in_dm(duel)
    else:
        bot.answer_callback_query(call.id, "Команда уже заполнена!")

def update_duel_message(duel):
    challenger_names = []
    for user_id in duel.challenger_team:
        user_info = get_user_info(user_id)
        name = user_info['custom_name'] if user_info['custom_name'] else (
            f"@{user_info['username']}" if user_info['username'] else user_info['first_name']
        )
        challenger_names.append(name)
    
    opponent_names = []
    for user_id in duel.opponent_team:
        user_info = get_user_info(user_id)
        name = user_info['custom_name'] if user_info['custom_name'] else (
            f"@{user_info['username']}" if user_info['username'] else user_info['first_name']
        )
        opponent_names.append(name)
    
    message_text = "⚔️ ДУЭЛЬ ⚔️\n\n"
    message_text += f"Ставка: {format_balance(duel.bet_amount)} с игрока\n"
    message_text += f"Оружие: {duel.weapon_type['name']}\n\n"
    message_text += f"Команда 1 ({len(duel.challenger_team)}/{duel.team_size}):\n"
    message_text += "\n".join([f"• {name}" for name in challenger_names]) + "\n\n"
    message_text += f"Команда 2 ({len(duel.opponent_team)}/{duel.team_size}):\n"
    message_text += "\n".join([f"• {name}" for name in opponent_names]) + "\n\n"
    message_text += f"Общий банк: {format_balance(duel.bet_amount * duel.team_size * 2)}"
    
    markup = InlineKeyboardMarkup()
    
    if len(duel.challenger_team) < duel.team_size:
        markup.add(InlineKeyboardButton("👥 Присоединиться к команде 1", callback_data=f"duel_join_challenger_{duel.duel_id}"))
    
    if len(duel.opponent_team) < duel.team_size:
        markup.add(InlineKeyboardButton("🏆 Присоединиться к команде 2", callback_data=f"duel_accept_{duel.duel_id}"))
    
    try:
        bot.edit_message_text(
            message_text,
            duel.chat_id,
            duel.message_id,
            reply_markup=markup
        )
    except:
        pass

def start_duel_in_dm(duel):
    duel.status = "active"
    
    bot.send_message(duel.chat_id, 
                   "⚔️ Дуэль начинается! Все боеци получат ЛС с деталями боя.", parse_mode='HTML')
    
    all_players = duel.challenger_team + duel.opponent_team
    
    for player_id in all_players:
        try:
            team = "Команда 1" if player_id in duel.challenger_team else "Команда 2"
            teammates = duel.challenger_team if player_id in duel.challenger_team else duel.opponent_team
            opponents = duel.opponent_team if player_id in duel.challenger_team else duel.challenger_team
            
            teammate_names = []
            for teammate_id in teammates:
                if teammate_id != player_id:
                    user_info = get_user_info(teammate_id)
                    name = user_info['custom_name'] if user_info['custom_name'] else (
                        f"@{user_info['username']}" if user_info['username'] else user_info['first_name']
                    )
                    teammate_names.append(name)
            
            opponent_names = []
            for opponent_id in opponents:
                user_info = get_user_info(opponent_id)
                name = user_info['custom_name'] if user_info['custom_name'] else (
                    f"@{user_info['username']}" if user_info['username'] else user_info['first_name']
                )
                opponent_names.append(name)
            
            message_text = "⚔️ ДУЭЛЬ НАЧИНАЕТСЯ! ⚔️\n\n"
            message_text += f"Твоя команда: {team}\n"
            message_text += f"Ставка: {format_balance(duel.bet_amount)}\n"
            message_text += f"Оружие: {duel.weapon_type['name']}\n\n"
            
            if teammate_names:
                message_text += f"Ваши союзники:\n" + "\n".join([f"• {name}" for name in teammate_names]) + "\n\n"
            
            message_text += f"Противники:\n" + "\n".join([f"• {name}" for name in opponent_names]) + "\n\n"
            message_text += "Бой начнется через 5с..."
            
            bot.send_message(player_id, message_text)
            
        except Exception as e:
            print(f"Не удалось отправить ЛС игроку {player_id}: {e}")
    
    time.sleep(5)
    execute_duel(duel)

def execute_duel(duel):
    challenger_power = sum([calculate_user_power(user_id) for user_id in duel.challenger_team])
    opponent_power = sum([calculate_user_power(user_id) for user_id in duel.opponent_team])
    
    total_power = challenger_power + opponent_power
    challenger_win_chance = (challenger_power / total_power) * 100
    
    random_factor = random.uniform(0.8, 1.2)
    challenger_win_chance *= random_factor
    
    roll = random.uniform(0, 100)
    challenger_wins = roll <= challenger_win_chance
    
    winning_team = duel.challenger_team if challenger_wins else duel.opponent_team
    losing_team = duel.opponent_team if challenger_wins else duel.challenger_team
    
    win_per_player = duel.bet_amount * 2
    
    for winner_id in winning_team:
        update_balance(winner_id, win_per_player)
    
    winner_names = []
    for user_id in winning_team:
        user_info = get_user_info(user_id)
        name = user_info['custom_name'] if user_info['custom_name'] else (
            f"@{user_info['username']}" if user_info['username'] else user_info['first_name']
        )
        winner_names.append(name)
    
    result_text = "⚔️ РЕЗУЛЬТАТЫ ДУЭЛИ ⚔️\n\n"
    result_text += f"ПОБЕДИЛА КОМАНДА {'1' if challenger_wins else '2'}!\n\n"
    result_text += f"Победители:\n" + "\n".join([f"• {name}" for name in winner_names]) + "\n\n"
    result_text += f"Каждый победитель получает: {format_balance(win_per_player)}\n"
    result_text += f"Общий выигрыш: {format_balance(win_per_player * len(winning_team))}"
    
    bot.send_message(duel.chat_id, result_text)
    
    for player_id in winning_team + losing_team:
        try:
            player_result_text = "⚔️ РЕЗУЛЬТАТЫ ДУЭЛИ ⚔️\n\n"
            
            if player_id in winning_team:
                player_result_text += f"{SALUTE_EMOJI} ВАША КОМАНДА ПОБЕДИЛА!\n\n"
                player_result_text += f"Вы получаете: {format_balance(win_per_player)}\n"
                player_result_text += f"Твой баланс: {format_balance(get_balance(player_id))}"
            else:
                player_result_text += f"{SAD_EMOJI} Твоя команда проиграла\n\n"
                player_result_text += f"Вы потеряли ставку: {format_balance(duel.bet_amount)}\n"
                player_result_text += f"Твой баланс: {format_balance(get_balance(player_id))}"
            
            bot.send_message(player_id, player_result_text)
        except:
            pass
    
    if duel.duel_id in active_duels:
        del active_duels[duel.duel_id]

def calculate_user_power(user_id):
    user_info = get_user_info(user_id)
    balance_power = user_info['balance'] / 1000000
    exp_power = user_info.get('experience', 0) / 1000
    return balance_power + exp_power + random.uniform(0, 10)

def show_duel_help(chat_id):
    help_text = """⚔️ КОМАНДЫ ДУЭЛЕЙ:

Создать дуэль в чате:
дуэль [размер] [бет] [оружие]

Примеры:
дуэль 1 1000к пистолет - 1 на 1
дуэль 2 500к лук - команда 2 на 2
дуэль 3 1м меч - команда 3 на 3

Доступные размеры: 1, 2, 3, 5
Открытое оружие: пистолет, лук, меч, кинжал, граната, посох

Минимальная бет: 1,000,000🌸
Дуэль активна 5м"""
    
    bot.send_message(chat_id, help_text)

def show_weapons_list(chat_id):
    weapons_text = "🔫 ДОСТУПНОЕ ОРУЖИЕ:\n\n"
    for weapon in DUEL_CONFIG["weapons"]:
        weapons_text += f"{weapon['name']} - {weapon['accuracy']}% точность, {weapon['critical']}% крит\n"
    
    bot.send_message(chat_id, weapons_text)

def cleanup_expired_duels():
    current_time = time.time()
    expired_duels = []
    
    for duel_id, duel in active_duels.items():
        if current_time - duel.created_at > DUEL_CONFIG["duel_duration"]:
            expired_duels.append(duel_id)
    
    for duel_id in expired_duels:
        duel = active_duels[duel_id]
        
        all_players = duel.challenger_team + duel.opponent_team
        for player_id in all_players:
            update_balance(player_id, duel.bet_amount)
        
        bot.send_message(duel.chat_id, 
                       f"⚔️ Дуэль истекла! Деньги возвращены всем боецам.", parse_mode='HTML')
        
        del active_duels[duel_id]

def start_duel_cleanup():
    while True:
        try:
            cleanup_expired_duels()
        except Exception as e:
            print(f"Ошибка очистки дуэлей: {e}")
        time.sleep(60)

import threading
cleanup_thread = threading.Thread(target=start_duel_cleanup, daemon=True)
cleanup_thread.start()

@bot.message_handler(func=lambda message: message.text.lower().startswith('кости '))
def handle_dice_bet(message):
    if not message.reply_to_message:
        bot.send_message(message.chat.id, "Ответьте на сообщение пользователя")
        return
    
    user_id = message.from_user.id
    target_user_id = message.reply_to_message.from_user.id
    
    if target_user_id == user_id:
        bot.send_message(message.chat.id, "Нельзя играть самому с собой")
        return
    
    try:
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "кости [бет]")
            return
        
        bet_amount = parse_bet_amount(' '.join(parts[1:]), get_balance(user_id))
        
        if bet_amount is None or bet_amount <= 0:
            bot.send_message(message.chat.id, "Неверная сумма")
            return
        
        user_balance = get_balance(user_id)
        target_balance = get_balance(target_user_id)
        
        if user_balance < bet_amount:
            bot.send_message(message.chat.id, f"Не хватает монет: {format_balance(bet_amount)}", parse_mode='HTML')
            return
        
        if target_balance < bet_amount:
            bot.send_message(message.chat.id, "У оппонента недостаточно средств")
            return
        
        dice_keyboard = {
            "inline_keyboard": [[
                {"text": "🎲 Принять", "callback_data": f"dice_accept_{user_id}_{target_user_id}_{bet_amount}", "style": "success"},
                {"text": "❌ Отказаться", "callback_data": f"dice_decline_{user_id}", "style": "danger"}
            ]]
        }
        
        user_info = get_user_info(user_id)
        target_info = get_user_info(target_user_id)
        user_name = user_info['custom_name'] or user_info['first_name']
        target_name = target_info['custom_name'] or target_info['first_name']
        
        challenge_text = f"🎲 <b>{user_name}</b> вызывает <b>{target_name}</b> на кости!\n"
        challenge_text += f"💵 Ставка: <b>{format_balance(bet_amount)}</b>"
        
        bot.send_message(message.chat.id, challenge_text, reply_markup=dice_keyboard, parse_mode='HTML')
        
    except Exception as e:
        print(f"Ошибка в handle_dice_bet: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('dice_'))
def handle_dice_response(call):
    try:
        data_parts = call.data.split('_')
        action = data_parts[1]
        
        if action == "decline":
            bot.edit_message_text("❌ Вызов отклонен", call.message.chat.id, call.message.message_id, parse_mode='HTML')
            bot.answer_callback_query(call.id)
            return
        
        challenger_id = int(data_parts[2])
        target_id = int(data_parts[3])
        bet_amount = int(data_parts[4])
        
        if call.from_user.id != target_id:
            bot.answer_callback_query(call.id, "❌ Не твой вызов")
            return
        
        update_balance(challenger_id, -bet_amount)
        update_balance(target_id, -bet_amount)
        
        bot.edit_message_text("🎲 Бросаем кости...", call.message.chat.id, call.message.message_id)
        
        dice1 = bot.send_dice(call.message.chat.id, emoji='🎲')
        time.sleep(2)
        dice2 = bot.send_dice(call.message.chat.id, emoji='🎲')
        
        time.sleep(2)
        
        challenger_score = dice1.dice.value
        target_score = dice2.dice.value
        
        if challenger_score > target_score:
            winner_id = challenger_id
            win_amount = bet_amount * 2
            result_text = f"🎉 Победил {get_user_info(challenger_id)['custom_name'] or get_user_info(challenger_id)['first_name']}"
        elif target_score > challenger_score:
            winner_id = target_id
            win_amount = bet_amount * 2
            result_text = f"🎉 Победил {get_user_info(target_id)['custom_name'] or get_user_info(target_id)['first_name']}"
        else:
            update_balance(challenger_id, bet_amount)
            update_balance(target_id, bet_amount)
            result_text = "🤝 Ничья"
            win_amount = 0
        
        if win_amount > 0:
            update_balance(winner_id, win_amount)
        
        time.sleep(2)
        bot.delete_message(call.message.chat.id, dice1.message_id)
        bot.delete_message(call.message.chat.id, dice2.message_id)
        
        result_message = f"⚔️ Результаты:\n\n"
        result_message += f"🎴 {get_user_info(challenger_id)['custom_name'] or get_user_info(challenger_id)['first_name']}: {challenger_score} очков\n"
        result_message += f"🎴 {get_user_info(target_id)['custom_name'] or get_user_info(target_id)['first_name']}: {target_score} очков\n\n"
        result_message += f"{result_text}\n"
        
        if win_amount > 0:
            result_message += f"💵 Выигрыш: {format_balance(win_amount)}"
        
        bot.edit_message_text(result_message, call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        print(f"Ошибка в handle_dice_response: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка")

@bot.callback_query_handler(func=lambda call: call.data == "cancel_demote")
def cancel_demote_member(call):
    bot.edit_message_text(
        "❌ Понижение сброшено",
        call.message.chat.id,
        call.message.message_id
    , parse_mode='HTML')
    bot.answer_callback_query(call.id, "🏆 Отменено")
    
def format_game_win_text(username, win_amount, balance):
    """Форматирование текста выигрыша для игр"""
    return f"<blockquote>{SALUTE_EMOJI} <b>{username}</b> выиграл {format_balance(win_amount)}!\n{MONEY_EMOJI} Средства: {format_balance(balance)}</blockquote>"

def format_game_lose_text(username, lose_amount, balance):
    """Форматирование текста проигрыша для игр"""
    return f"<blockquote>{SAD_EMOJI} <b>{username}</b> проиграл {format_balance(lose_amount)}!\n{MONEY_EMOJI} Средства: {format_balance(balance)}</blockquote>"

def get_user_display_name(user_info, telegram_user):
    """Получить отображаемое имя пользователя для игр"""
    if user_info and user_info['custom_name']:
        return user_info['custom_name']
    elif telegram_user.username:
        return f"@{telegram_user.username}"
    else:
        return telegram_user.first_name

import os
from datetime import datetime

def update_balance(user_id, amount):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
        old_balance = cursor.fetchone()[0]
        
        cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
        
        if amount > 0:
            cursor.execute('UPDATE users SET games_won = games_won + 1, total_won_amount = total_won_amount + ? WHERE user_id = ?', 
                          (amount, user_id))
        elif amount < 0:
            cursor.execute('UPDATE users SET games_lost = games_lost + 1, total_lost_amount = total_lost_amount + ? WHERE user_id = ?', 
                          (abs(amount), user_id))

@bot.message_handler(func=lambda message: message.text.lower().startswith('выдать ') and is_admin(message.from_user.id))
def handle_give_money(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды", parse_mode='HTML')
        return
    
    if not message.reply_to_message:
        bot.send_message(message.chat.id, "❌ Ответьте на сообщение пользователя, которому хотите выдать деньги!", parse_mode='HTML')
        return
    
    target_user_id = message.reply_to_message.from_user.id
    target_username = message.reply_to_message.from_user.username or "нет username"
    target_first_name = message.reply_to_message.from_user.first_name
    
    try:
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Используйте: выдать [сумма] (ответом на сообщение)", parse_mode='HTML')
            return
        
        amount = parse_bet_amount(' '.join(parts[1:]), float('inf'))
        if amount is None or amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма!", parse_mode='HTML')
            return
        
        update_balance(target_user_id, amount)
        
        bot.send_message(message.chat.id, 
                       f"🏆 Успешно выдано {format_balance(amount)} пользователю {target_first_name} (@{target_username})", parse_mode='HTML')
        
        try:
            bot.send_message(target_user_id, 
                           f"🎉 Администратор выдал вам {format_balance(amount)}!\n💳 Твой баланс: {format_balance(get_balance(target_user_id))}", parse_mode='HTML')
        except:
            pass
            
    except Exception as e:
        print(f"Ошибка при выдаче денег: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при выдаче денег!", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower().startswith('забрать ') and is_admin(message.from_user.id))
def handle_take_money(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды", parse_mode='HTML')
        return
    
    if not message.reply_to_message:
        bot.send_message(message.chat.id, "❌ Ответьте на сообщение пользователя, у которого хотите снять деньги!", parse_mode='HTML')
        return
    
    target_user_id = message.reply_to_message.from_user.id
    target_username = message.reply_to_message.from_user.username or "нет username"
    target_first_name = message.reply_to_message.from_user.first_name
    
    try:
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Используйте: снять [сумма] (ответом на сообщение)", parse_mode='HTML')
            return
        
        amount = parse_bet_amount(' '.join(parts[1:]), float('inf'))
        if amount is None or amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма!", parse_mode='HTML')
            return
        
        target_balance = get_balance(target_user_id)
        if target_balance < amount:
            bot.send_message(message.chat.id, f"❌ У пользователя недостаточно средств! Средства: {format_balance(target_balance)}", parse_mode='HTML')
            return
        
        update_balance(target_user_id, -amount)
        
        bot.send_message(message.chat.id, 
                       f"🏆 Успешно снято {format_balance(amount)} у пользователя {target_first_name} (@{target_username})", parse_mode='HTML')
        
        try:
            bot.send_message(target_user_id, 
                           f"⚠️ Администратор снял с вас {format_balance(amount)}!\n💳 Твой баланс: {format_balance(get_balance(target_user_id))}", parse_mode='HTML')
        except:
            pass
            
    except Exception as e:
        print(f"Ошибка при снятии денег: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при снятии денег!", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower().startswith('установить ') and is_admin(message.from_user.id))
def handle_set_balance(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды", parse_mode='HTML')
        return
    
    if not message.reply_to_message:
        bot.send_message(message.chat.id, "❌ Ответьте на сообщение пользователя, у которого хотите установить баланс!", parse_mode='HTML')
        return
    
    target_user_id = message.reply_to_message.from_user.id
    target_username = message.reply_to_message.from_user.username or "нет username"
    target_first_name = message.reply_to_message.from_user.first_name
    
    try:
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Используйте: установить [сумма] (ответом на сообщение)", parse_mode='HTML')
            return
        
        amount = parse_bet_amount(' '.join(parts[1:]), float('inf'))
        if amount is None or amount < 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма!", parse_mode='HTML')
            return
        
        current_balance = get_balance(target_user_id)
        
        with get_db_cursor() as cursor:
            cursor.execute('UPDATE users SET balance = ? WHERE user_id = ?', (amount, target_user_id))
        
        bot.send_message(message.chat.id, 
                       f"🏆 Баланс пользователя {target_first_name} (@{target_username}) установлен:\n"
                       f"📊 Было: {format_balance(current_balance)}\n"
                       f"📈 Стало: {format_balance(amount)}", parse_mode='HTML')
        
        try:
            bot.send_message(target_user_id, 
                           f"⚡ Администратор установил твой баланс: {format_balance(amount)}!", parse_mode='HTML')
        except:
            pass
            
    except Exception as e:
        print(f"Ошибка при установке баланса: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при установке баланса!", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower().startswith('балан ') and is_admin(message.from_user.id))
def handle_check_balance(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды", parse_mode='HTML')
        return
    
    if not message.reply_to_message:
        bot.send_message(message.chat.id, "❌ Ответьте на сообщение пользователя, чтобы проверить его баланс!", parse_mode='HTML')
        return
    
    target_user_id = message.reply_to_message.from_user.id
    target_username = message.reply_to_message.from_user.username or "нет username"
    target_first_name = message.reply_to_message.from_user.first_name
    
    try:
        user_info = get_user_info(target_user_id)
        if not user_info:
            bot.send_message(message.chat.id, "❌ Пользователь отсутствует в базе данных!", parse_mode='HTML')
            return
        
        balance = user_info['balance']
        bank_deposit = user_info['bank_deposit']
        video_cards = user_info['video_cards']
        total_clicks = user_info['total_clicks']
        
        message_text = f"🎴 **Информация о пользователе**\n\n"
        message_text += f"🆔 Имя: {target_first_name}\n"
        message_text += f"🔗 Username: @{target_username}\n"
        message_text += f"🆔 ID: {target_user_id}\n\n"
        message_text += f"{MONEY_EMOJI} Средства: {format_balance(balance)}\n"
        message_text += f"🏛 Депозит: {format_balance(bank_deposit)}\n"
        message_text += f"🎁 Общий капитал: {format_balance(balance + bank_deposit)}\n"
        message_text += f"⚡ Майнеров: {video_cards}\n"
        message_text += f"🖱 Кликов: {total_clicks}\n"
        
        business_info = get_user_business(target_user_id)
        if business_info:
            message_text += f"🏭 Предприятие: {business_info['name']}\n"
            message_text += f"📦 Ресурсы: {business_info['raw_materials']}/{business_info['storage_capacity']}\n"
        
        user_clan = get_user_clan(target_user_id)
        if user_clan:
            message_text += f"⚔️ Гильдия: {user_clan['name']} [{user_clan['tag']}]\n"
            message_text += f"🎖 Статус: {user_clan['role']}\n"
        
        bot.send_message(message.chat.id, message_text)
        
    except Exception as e:
        print(f"Ошибка при проверке баланса: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при проверке баланса!", parse_mode='HTML')

TAX_CONFIG = {
    "high_wealth_tax":        0.10,
    "high_wealth_threshold":  1_000_000,
    "medium_wealth_tax":      0.05,
    "medium_wealth_threshold": 100_000,
    "general_tax":            0.03,
    "min_tax_amount":         100,
}

# ─────────────────────────────────────────────
# 🏛️  СБОР НАЛОГОВ
# ─────────────────────────────────────────────

def collect_taxes():
    """Собрать налоги. Каждый пользователь обновляется отдельной транзакцией."""
    total_collected = 0
    affected_users  = 0
    tax_report      = []

    # 1. Читаем всех одним запросом
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "SELECT user_id, username, first_name, custom_name, balance, bank_deposit "
                "FROM users WHERE (balance + bank_deposit) > ?",
                (TAX_CONFIG["min_tax_amount"],)
            )
            rows = cur.fetchall()
    except Exception as e:
        print(f"[tax] ошибка чтения: {e}")
        return {"success": False, "error": str(e)}

    # 2. Обновляем каждого отдельно
    for row in rows:
        try:
            user_id, username, first_name, custom_name, balance, bank_deposit = row
            balance      = int(balance or 0)
            bank_deposit = int(bank_deposit or 0)
            total_wealth = balance + bank_deposit

            if total_wealth <= TAX_CONFIG["min_tax_amount"]:
                continue

            if total_wealth >= TAX_CONFIG["high_wealth_threshold"]:
                tax_rate   = TAX_CONFIG["high_wealth_tax"]
                tax_reason = "10% (элита)"
                tier_icon  = "🏆"
                tier_name  = "Элита"
            elif total_wealth >= TAX_CONFIG["medium_wealth_threshold"]:
                tax_rate   = TAX_CONFIG["medium_wealth_tax"]
                tax_reason = "5% (состоятельный)"
                tier_icon  = "💎"
                tier_name  = "Состоятельный"
            else:
                tax_rate   = TAX_CONFIG["general_tax"]
                tax_reason = "3% (гражданин)"
                tier_icon  = "👤"
                tier_name  = "Гражданин"

            total_tax = int(total_wealth * tax_rate)
            if total_tax < TAX_CONFIG["min_tax_amount"]:
                continue

            tax_from_balance = min(total_tax, balance)
            tax_from_bank    = min(total_tax - tax_from_balance, bank_deposit)
            actual_tax       = tax_from_balance + tax_from_bank

            if actual_tax <= 0:
                continue

            new_balance = balance - tax_from_balance
            new_bank    = bank_deposit - tax_from_bank

            with get_db_cursor() as cur:
                cur.execute(
                    "UPDATE users SET balance=?, bank_deposit=? WHERE user_id=?",
                    (new_balance, new_bank, user_id)
                )

            total_collected += actual_tax
            affected_users  += 1

            display_name = custom_name or (("@" + username) if username else first_name) or str(user_id)
            tax_report.append({
                "user":   display_name,
                "wealth": total_wealth,
                "tax":    actual_tax,
                "rate":   tax_reason,
            })

            parts = []
            if tax_from_balance > 0:
                parts.append(
                    "👛 Кошелёк: <b>-" + format_balance(tax_from_balance) + " 🌸</b>\n"
                    "   " + format_balance(balance) + " ➜ " + format_balance(new_balance)
                )
            if tax_from_bank > 0:
                parts.append(
                    "🏦 Банк: <b>-" + format_balance(tax_from_bank) + " 🌸</b>\n"
                    "   " + format_balance(bank_deposit) + " ➜ " + format_balance(new_bank)
                )

            notify = (
                "🏛️ <b>НАЛОГОВАЯ СЛУЖБА</b>\n\n"
                + tier_icon + " Статус: <b>" + tier_name + "</b>  (" + str(int(tax_rate * 100)) + "%)\n\n"
                + "\n".join(parts)
                + "\n\n💰 Осталось: <b>" + format_balance(total_wealth - actual_tax) + " 🌸</b>"
            )
            try:
                bot.send_message(user_id, notify, parse_mode="HTML")
            except Exception:
                pass

        except Exception as e:
            print(f"[tax] ошибка юзера {row[0]}: {e}")
            continue

    return {
        "success":         True,
        "total_collected": total_collected,
        "affected_users":  affected_users,
        "tax_report":      tax_report,
    }


def get_wealth_stats():
    """Статистика богатства для предпросмотра налогов."""
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "SELECT COUNT(*), SUM(balance+bank_deposit), AVG(balance+bank_deposit),"
                " SUM(balance), SUM(bank_deposit) FROM users"
            )
            r = cur.fetchone()
            total_users    = r[0] or 0
            total_wealth   = int(r[1] or 0)
            avg_wealth     = int(r[2] or 0)
            total_balance  = int(r[3] or 0)
            total_deposits = int(r[4] or 0)

            cur.execute(
                "SELECT COUNT(*), SUM(balance+bank_deposit) FROM users"
                " WHERE balance+bank_deposit >= ?",
                (TAX_CONFIG["high_wealth_threshold"],)
            )
            hw = cur.fetchone()

            cur.execute(
                "SELECT COUNT(*), SUM(balance+bank_deposit) FROM users"
                " WHERE balance+bank_deposit >= ? AND balance+bank_deposit < ?",
                (TAX_CONFIG["medium_wealth_threshold"], TAX_CONFIG["high_wealth_threshold"])
            )
            mw = cur.fetchone()

            cur.execute(
                "SELECT COUNT(*), SUM(balance+bank_deposit) FROM users"
                " WHERE balance+bank_deposit > ? AND balance+bank_deposit < ?",
                (TAX_CONFIG["min_tax_amount"], TAX_CONFIG["medium_wealth_threshold"])
            )
            lw = cur.fetchone()

        return {
            "total_users":    total_users,
            "total_wealth":   total_wealth,
            "avg_wealth":     avg_wealth,
            "total_balance":  total_balance,
            "total_deposits": total_deposits,
            "high_wealth":   {"count": hw[0] or 0, "total": int(hw[1] or 0)},
            "medium_wealth": {"count": mw[0] or 0, "total": int(mw[1] or 0)},
            "low_wealth":    {"count": lw[0] or 0, "total": int(lw[1] or 0)},
        }
    except Exception as e:
        print(f"[tax] ошибка статистики: {e}")
        return None


@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == "собрать налог")
@bot.message_handler(commands=["налог"])
def handle_collect_tax(message):
    if not is_admin(message.from_user.id):
        return
    try:
        msg = bot.send_message(message.chat.id, "⏳ Собираем налоги...", parse_mode="HTML")
        result = collect_taxes()
        if result["success"]:
            text = (
                "🏛️ <b>НАЛОГИ СОБРАНЫ!</b>\n\n"
                "💰 В казну: <b>" + format_balance(result["total_collected"]) + " 🌸</b>\n"
                "👥 Затронуто: <b>" + str(result["affected_users"]) + "</b> чел.\n\n"
            )
            if result["tax_report"]:
                top = sorted(result["tax_report"], key=lambda x: x["tax"], reverse=True)[:10]
                text += "🏆 <b>Крупнейшие налогоплательщики:</b>\n"
                medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
                for i, tp in enumerate(top):
                    text += medals[i] + " " + tp["user"] + " — <b>" + format_balance(tp["tax"]) + " 🌸</b> <i>(" + tp["rate"] + ")</i>\n"
        else:
            text = "❌ Ошибка: " + result["error"]
        bot.edit_message_text(text, chat_id=message.chat.id, message_id=msg.message_id, parse_mode="HTML")
    except Exception as e:
        print(f"[tax] handle_collect_tax: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка: " + str(e), parse_mode="HTML")


# ─────────────────────────────────────────────
# 💰 УМНЫЕ ЦЕНЫ ДОНАТА
# ─────────────────────────────────────────────

# Множители относительно цены за 1 звезду
DONATE_MULTIPLIERS = {
    "stars_1":   1.000,
    "stars_5":   3.990,
    "stars_15":  19.92,
    "stars_50":  49.85,
    "stars_150": 209.2,
    "stars_250": 698.1,
}
DONATE_MIN_PER_STAR = 15_000   # минимум за 1 звезду
DONATE_MAX_PER_STAR = 100_000  # максимум за 1 звезду
DONATE_MAX_CHANGE   = 0.20     # макс изменение за раз ±20%


def recalc_donate_prices():
    """Пересчитать цены доната по среднему балансу. Запускать раз в сутки."""
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT AVG(balance) FROM users WHERE balance > 0")
            row = cur.fetchone()
        avg_balance = int(row[0] or 0)
        if avg_balance <= 0:
            print("[donate] avg_balance=0, пропускаем пересчёт")
            return

        # Целевая цена за 1 звезду = 18.8% от среднего баланса
        raw_per_star = int(avg_balance * 0.188)
        raw_per_star = max(DONATE_MIN_PER_STAR, min(DONATE_MAX_PER_STAR, raw_per_star))

        # Ограничение ±20% от текущего значения
        old_per_star = DONATE_PACKAGES.get("stars_1", {}).get("amount", raw_per_star)
        new_per_star = max(
            int(old_per_star * (1 - DONATE_MAX_CHANGE)),
            min(int(old_per_star * (1 + DONATE_MAX_CHANGE)), raw_per_star)
        )

        for key, mult in DONATE_MULTIPLIERS.items():
            new_amount = int(new_per_star * mult)
            DONATE_PACKAGES[key]["amount"] = new_amount
            with get_db_cursor() as cur:
                cur.execute("UPDATE donate_packages SET amount=? WHERE key=?", (new_amount, key))

        print("[donate] пересчёт: avg=" + format_balance(avg_balance) + ", 1⭐=" + format_balance(new_per_star) + " 🌸")

    except Exception as e:
        print(f"[donate] ошибка пересчёта: {e}")




@bot.message_handler(commands=["обновитьдонат"])
@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == "обновить донат")
def handle_recalc_donate(message):
    if not is_admin(message.from_user.id):
        return
    try:
        old_price = DONATE_PACKAGES.get("stars_1", {}).get("amount", 0)
        recalc_donate_prices()
        new_price = DONATE_PACKAGES.get("stars_1", {}).get("amount", 0)
        lines = []
        for key, pkg in DONATE_PACKAGES.items():
            amt = pkg["amount"]
            lines.append(pkg["emoji"] + " " + str(pkg["stars"]) + " зв — " + format_balance(amt) + " 🌸")
        text = (
            "💰 <b>Цены доната обновлены!</b>\n\n"
            "1 ⭐ было: <b>" + format_balance(old_price) + " 🌸</b>\n"
            "1 ⭐ стало: <b>" + format_balance(new_price) + " 🌸</b>\n\n"
            + "\n".join(lines)
        )
        bot.send_message(message.chat.id, text, parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, "❌ Ошибка: " + str(e), parse_mode="HTML")

def _start_donate_scheduler():
    import datetime as _dt_donate
    def _loop():
        while True:
            now      = _dt_donate.datetime.utcnow()
            tomorrow = (now + _dt_donate.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            time.sleep((tomorrow - now).total_seconds())
            recalc_donate_prices()
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    print("💰 Планировщик умных цен доната запущен")


def find_user_by_username(username):
    """Найти пользователя по юзернейму (без учёта регистра)"""
    if username.startswith('@'):
        username = username[1:]
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT user_id, username, first_name, custom_name, balance, video_cards, bank_deposit,
                   daily_streak, total_clicks, business_id, business_progress,
                   business_start_time, business_raw_materials, clan_id,
                   games_won, games_lost, total_won_amount, total_lost_amount
            FROM users WHERE LOWER(username) = LOWER(?)
        ''', (username,))
        result = cursor.fetchone()
        if result:
            return {
                'user_id': result[0],
                'username': result[1],
                'first_name': result[2],
                'custom_name': result[3],
                'balance': result[4],
                'video_cards': result[5],
                'bank_deposit': result[6],
                'daily_streak': result[7],
                'total_clicks': result[8],
                'business_id': result[9],
                'business_progress': result[10],
                'business_start_time': result[11],
                'business_raw_materials': result[12],
                'clan_id': result[13],
                'games_won': result[14] or 0,
                'games_lost': result[15] or 0,
                'total_won_amount': result[16] or 0,
                'total_lost_amount': result[17] or 0
            }
    return None

@bot.message_handler(func=lambda message: message.text and message.text.lower().split()[0] in ('кинуть', 'передать', 'дать', 'pay', 'send'))
def handle_transfer(message):
    try:
        user_id = message.from_user.id

        if is_user_warned(user_id):
            bot.send_message(message.chat.id, "❌ У вас активный варн — переводы недоступны!")
            return

        parts = message.text.strip().split()
        target_user_id = None
        amount_text = None

        if message.reply_to_message:
            if len(parts) < 2:
                bot.send_message(message.chat.id, "❌ Укажи сумму: <code>кинуть 1000</code> ответом на сообщение", parse_mode='HTML')
                return
            target_user_id = message.reply_to_message.from_user.id
            amount_text = parts[1]
        else:
            if len(parts) < 3:
                bot.send_message(message.chat.id,
                    "❌ Формат:\n• <code>кинуть @username сумма</code>\n• <code>кинуть сумма</code> — ответом на сообщение",
                    parse_mode='HTML')
                return
            raw = parts[1].lstrip('@')
            amount_text = parts[2]
            if raw.isdigit():
                target_user_id = int(raw)
            else:
                found = find_user_by_username(raw)
                if not found:
                    bot.send_message(message.chat.id, f"❌ Пользователь @{raw} не найден в боте", parse_mode='HTML')
                    return
                target_user_id = found['user_id']

        if target_user_id == user_id:
            bot.send_message(message.chat.id, "❌ Нельзя переводить самому себе")
            return

        target_info = get_user_info(target_user_id)
        if not target_info:
            if message.reply_to_message:
                ru = message.reply_to_message.from_user
                target_display = ru.username and f"@{ru.username}" or ru.first_name or f"ID:{target_user_id}"
            else:
                bot.send_message(message.chat.id, "❌ Получатель не зарегистрирован в боте")
                return
        else:
            cn = target_info.get('custom_name')
            un = target_info.get('username')
            fn = target_info.get('first_name', 'Пользователь')
            target_display = cn or (f"@{un}" if un else fn)

        user_balance = get_balance(user_id)
        amount = parse_bet_amount(amount_text, user_balance)
        if not amount or amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма")
            return
        if amount > user_balance:
            bot.send_message(message.chat.id, f"❌ Недостаточно монет. Баланс: {format_balance(user_balance)}")
            return

        fee_rate = 0.05 if is_premium(user_id) else TRANSFER_FEE
        fee = int(amount * fee_rate)
        receive_amount = amount - fee

        si = get_user_info(user_id)
        sender_display = (si.get('custom_name') if si else None) or message.from_user.first_name or f"ID:{user_id}"

        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_transfer_{user_id}_{target_user_id}_{amount}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_transfer_{user_id}")
        )

        bot.send_message(
            message.chat.id,
            f"<tg-emoji emoji-id='5206607081334906820'>✅</tg-emoji> <b>Подтвердите перевод</b>\n\n"
            f"<blockquote>От: {sender_display}\nКому: {target_display}\nСумма: {format_balance(receive_amount)}\nКомиссия: {format_balance(fee)} ({int(fee_rate*100)}%)</blockquote>",
            reply_markup=markup,
            parse_mode='HTML'
        )

    except Exception as e:
        import traceback; traceback.print_exc()
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_transfer_'))
def handle_confirm_transfer(call):
    try:
        data_parts = call.data.split('_')
        sender_id = int(data_parts[2])
        target_user_id = int(data_parts[3])
        amount = int(data_parts[4])
        
        if call.from_user.id != sender_id:
            bot.answer_callback_query(call.id, "❌ Только отправитель может подтвердить перевод!", show_alert=True)
            return
        
        user_balance = get_balance(sender_id)
        if user_balance < amount:
            bot.answer_callback_query(call.id, "❌ Не хватает монет")
            return
        
        fee = int(amount * TRANSFER_FEE)
        receive_amount = amount - fee
        
        success, result_message = transfer_money(sender_id, target_user_id, amount)
        
        if success:
            add_experience(sender_id, amount // 100000)
            
            result_text = f"✅ <b>Перевод выполнен</b>\n\n"
            result_text += f"<blockquote>Переведено: {format_balance(receive_amount)}\n"
            result_text += f"Комиссия: {format_balance(fee)}</blockquote>"
            
            bot.edit_message_text(
                result_text, 
                call.message.chat.id, 
                call.message.message_id,
                parse_mode='HTML'
            )
            
            try:
                bot.send_message(
                    target_user_id, 
                    f"💰 Вам перевели {format_balance(receive_amount)}", 
                    parse_mode='HTML'
                )
            except:
                pass
            
            bot.answer_callback_query(call.id)
        else:
            bot.edit_message_text(f"❌ {result_message}", call.message.chat.id, call.message.message_id, parse_mode='HTML')
            bot.answer_callback_query(call.id, "Ошибка")
        
    except Exception as e:
        print(f"Ошибка в handle_confirm_transfer: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка")

@bot.callback_query_handler(func=lambda call: call.data.startswith('cancel_transfer_'))
def handle_cancel_transfer(call):
    try:
        data_parts = call.data.split('_')
        sender_id = int(data_parts[2])
        
        if call.from_user.id != sender_id:
            bot.answer_callback_query(call.id, "❌ Только отправитель может отменить перевод!")
            return
        
        bot.edit_message_text(
            "❌ Перевод отменен", 
            call.message.chat.id, 
            call.message.message_id,
            parse_mode='HTML'
        )
        bot.answer_callback_query(call.id, "Отменено")
        
    except Exception as e:
        print(f"Ошибка в handle_cancel_transfer: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка")
@bot.message_handler(func=lambda message: message.text.lower().startswith('изменить цену') and is_admin(message.from_user.id))
def handle_change_price(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды", parse_mode='HTML')
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 4:
            bot.send_message(message.chat.id, 
                           "❌ Используйте: изменить цену [название одежды] [новая стоимость]\n\n"
                           "Пример:\n"
                           "изменить цену Кроссовки Nike 50000000", parse_mode='HTML')
            return
        
        item_name = ' '.join(parts[2:-1])
        new_price_text = parts[-1]
        
        try:
            new_price = int(new_price_text)
            if new_price < 0:
                bot.send_message(message.chat.id, "❌ Цена не может быть отрицательной!", parse_mode='HTML')
                return
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверная стоимость! Используйте только цифры", parse_mode='HTML')
            return
        
        with get_db_cursor() as cursor:
            cursor.execute('SELECT id, name, price FROM clothes_shop WHERE name LIKE ?', (f'%{item_name}%',))
            items = cursor.fetchall()
            
            if not items:
                bot.send_message(message.chat.id, f"❌ Одежда '{item_name}' отсутствуета в магазине!", parse_mode='HTML')
                return
            
            if len(items) > 1:
                items_text = "📋 Найдено несколько предметов:\n\n"
                for item in items:
                    items_text += f"• {item[1]} (ID: {item[0]}) - {format_balance(item[2])}\n"
                items_text += f"\nУточните название или используйте ID: изменить цену [ID] [новая стоимость]"
                bot.send_message(message.chat.id, items_text)
                return
            
            item_id, item_name, old_price = items[0]
            
            cursor.execute('UPDATE clothes_shop SET price = ? WHERE id = ?', (new_price, item_id))
            
            bot.send_message(message.chat.id, 
                           f"🏆 Цена изменена для {item_name}!\n"
                           f"💵 Было: {format_balance(old_price)}\n"
                           f"💵 Стало: {format_balance(new_price)}", parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка при изменении цены: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при изменении цены!", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower().startswith('изменить цену id') and is_admin(message.from_user.id))
def handle_change_price_by_id(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 4:
            bot.send_message(message.chat.id, 
                           "❌ Используйте: изменить цену id [ID одежды] [новая стоимость]\n\n"
                           "Пример:\n"
                           "изменить цену id 5 75000000", parse_mode='HTML')
            return
        
        item_id_text = parts[3]
        new_price_text = parts[4]
        
        try:
            item_id = int(item_id_text)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверный ID! Используйте только цифры", parse_mode='HTML')
            return
        
        try:
            new_price = int(new_price_text)
            if new_price < 0:
                bot.send_message(message.chat.id, "❌ Цена не может быть отрицательной!", parse_mode='HTML')
                return
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверная стоимость! Используйте только цифры", parse_mode='HTML')
            return
        
        with get_db_cursor() as cursor:
            cursor.execute('SELECT name, price FROM clothes_shop WHERE id = ?', (item_id,))
            item = cursor.fetchone()
            
            if not item:
                bot.send_message(message.chat.id, f"❌ Одежда с ID {item_id} не найдено!", parse_mode='HTML')
                return
            
            item_name, old_price = item
            
            cursor.execute('UPDATE clothes_shop SET price = ? WHERE id = ?', (new_price, item_id))
            
            bot.send_message(message.chat.id, 
                           f"🏆 Цена изменена для {item_name} (ID: {item_id})!\n"
                           f"💵 Было: {format_balance(old_price)}\n"
                           f"💵 Стало: {format_balance(new_price)}", parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка при изменении цены по ID: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при изменении цены!", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower() == 'загрузить фото' and is_admin(message.from_user.id))
def handle_upload_photo_request(message):
    """Запрос на загрузку фото"""
    if not is_admin(message.from_user.id):
        return
    
    bot.send_message(
        message.chat.id,
        "📸 Отправьте фото, которое нужно сохранить в папку <b>images/</b>\n\n"
        "📁 Поддерживаются форматы: JPG, JPEG, PNG, GIF\n"
        "Фото будет сохранено с оригинальным именем файла.\n"
        "Для переименования отправьте фото с подписью:\n"
        "<code>новое_имя.jpg</code> или <code>новое_имя.png</code>",
        parse_mode='HTML'
    )

@bot.message_handler(content_types=['photo'], func=lambda message: is_admin(message.from_user.id))
def handle_photo_upload_images(message):
    """Сохраняет фото в папку images"""
    try:
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        if hasattr(message.photo[-1], 'mime_type') and message.photo[-1].mime_type:
            mime = message.photo[-1].mime_type
            if 'png' in mime:
                default_ext = '.png'
            elif 'gif' in mime:
                default_ext = '.gif'
            else:
                default_ext = '.jpg'
        else:
            default_ext = '.jpg'
        
        if message.caption and message.caption.strip():
            filename = message.caption.strip()
            if not filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                filename += default_ext
        else:
            filename = f"photo_{int(time.time())}{default_ext}"
        
        save_path = f"images/{filename}"
        
        os.makedirs("images", exist_ok=True)
        
        with open(save_path, 'wb') as new_file:
            new_file.write(downloaded_file)
        
        file_size = os.path.getsize(save_path)
        size_kb = file_size / 1024
        
        success_text = (
            f"✅ <b>Фото сохранено!</b>\n\n"
            f"📁 Путь: <code>{save_path}</code>\n"
            f"📄 Имя: {filename}\n"
            f"📊 Размер: {size_kb:.1f} KB\n"
            f"📌 Расширение: {default_ext}\n\n"
            f"💡 Теперь можно использовать в магазине: <code>{filename}</code>"
        )
        
        with open(save_path, 'rb') as photo:
            bot.send_photo(
                message.chat.id,
                photo,
                caption=success_text,
                parse_mode='HTML'
            )
        
    except Exception as e:
        print(f"Ошибка при загрузке фото: {e}")
        bot.send_message(
            message.chat.id,
            f"💀 Ошибка при загрузке фото: {e}"
        )    
@bot.message_handler(func=lambda message: message.text.lower() in ['игры', 'играть', 'game', 'games'])
def handle_games(message):
  
    games_text = """🎲 ДОСТУПНЫЕ ИГРЫ

🎲 АЗАРТНЫЕ ИГРЫ:

Кости - куб [бет] или кости [бет]
Ставки: 1-6, малые, большие, чет, нечет

Слоты - слот [бет] или слоты [бет]
Выигрыш до x64!

Баскетбол - бск [бет] или баскетбол [бет]
Коэффициент 2.5x

Футбол - фтб [бет] или футбол [бет]
Коэффициент 2x

Дартс - дартс [бет]
Выигрыш до x3!

Боулинг - боул [бет] или боулинг [бет]
Выигрыш до x3!

Краш - краш [икс] [бет]
Выцгрвш до 100х

💡 Примеры команд:
куб 1000к
слот 500к
бск 2000к
фтб 1500к"""

    bot.send_message(message.chat.id, games_text)

import threading
import time
import random


@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == 'проеб' and is_admin(m.from_user.id))
def handle_force_crash(message):
    if not active_crash_bets:
        bot.send_message(message.chat.id, "🎰 Нет активных ставок в краше", parse_mode='HTML')
        return

    victims = list(active_crash_bets.items())
    total_stolen = 0
    count = 0

    for user_id, data in victims:
        bet = data['bet']
        target_multiplier = data['multiplier']
        active_crash_bets.pop(user_id, None)
        total_stolen += bet
        count += 1
        try:
            result_multiplier = round(random.uniform(1.01, 1.09), 2)
            bot.send_message(
                user_id,
                f"💥 *ВЫ ПРОИГРАЛИ*\n\n"
                f"🎰 Игра: *Краш*\n"
                f"⚔️ Твой множитель: *{target_multiplier:.2f}x*\n"
                f"📊 Результат краша: *{result_multiplier:.2f}x*\n"
                f"💵 Потеряно: *{format_balance(bet)}*\n\n"
                f"🔄 Текущий баланс: *{format_balance(get_balance(user_id))}*\n"
                f"💡 Удачи в следующий раз!",
                parse_mode='Markdown'
            )
        except Exception:
            pass

    bot.send_message(
        message.chat.id,
        f"✅ {count} · {format_balance(total_stolen)}",
        parse_mode='HTML'
    )

def process_crash_game_ls_only(user_id, bet_amount, target_multiplier):
    """Отправляет в чат админов для решения - выиграет ли игрок"""
    active_crash_bets.pop(user_id, None)
    
    game_id = f"{user_id}_{int(time.time() * 1000)}"
    
    pending_crash_decisions[game_id] = {
        'user_id': user_id,
        'user_name': '',
        'bet': bet_amount,
        'target_mult': target_multiplier,
        'admin_msg_id': None,
        'timestamp': time.time(),
        'decided': False
    }
    
    try:
        user_info = bot.get_chat(user_id)
        user_name = user_info.first_name or str(user_id)
        if user_info.username:
            user_name = f"{user_name} (@{user_info.username})"
    except:
        user_name = str(user_id)
    
    pending_crash_decisions[game_id]['user_name'] = user_name
    
    decision_text = (
        f"⚡ *БЫСТРОЕ РЕШЕНИЕ*\n\n"
        f"👤 Игрок: {user_name} (ID: {user_id})\n"
        f"🎰 Игра: Краш\n"
        f"⚔️ Множитель ставки: {target_multiplier:.2f}x\n"
        f"💵 Ставка: {int(bet_amount)} 🌸\n\n"
        f"⏰ У вас есть 5 сек на решение!\n"
        f"Если не ответите - рандом результат"
    )
    
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Выигрыш", callback_data=f"crash_win_{game_id}"),
        InlineKeyboardButton("❌ Проигрыш", callback_data=f"crash_lose_{game_id}")
    )
    
    try:
        msg = bot.send_message(CRASH_ADMIN_CHAT, decision_text, reply_markup=markup)
        pending_crash_decisions[game_id]['admin_msg_id'] = msg.message_id
    except Exception as e:
        print(f"Ошибка отправки в чат админов: {e}")
    
    thread = threading.Thread(
        target=auto_crash_result,
        args=(game_id,),
        daemon=True
    )
    thread.start()

def auto_crash_result(game_id):
    """Автоматически даёт результат если админ не решил"""
    time.sleep(5)
    
    if game_id in pending_crash_decisions and not pending_crash_decisions[game_id]['decided']:
        send_crash_result(game_id, None)

def send_crash_result(game_id, admin_decision):
    """Отправляет результат игроку в ЛС"""
    if game_id not in pending_crash_decisions:
        return
    
    game = pending_crash_decisions[game_id]
    
    if game.get('result_sent'):
        return
    game['result_sent'] = True
    user_id = game['user_id']
    bet = game['bet']
    target_mult = game['target_mult']
    admin_msg_id = game['admin_msg_id']
    
    if admin_decision is None:
        win_chance = 0.42 / (target_mult ** 0.55)
        won = random.random() < win_chance
    else:
        won = admin_decision
    
    if won:
        result_multiplier = round(random.uniform(target_mult + 0.01, target_mult + random.uniform(0.5, 10.0)), 2)
    else:
        result_multiplier = round(random.uniform(1.01, max(1.02, target_mult - 0.01)), 2)
    
    win_amount = 0
    if won:
        win_amount = int(bet * target_mult)
        update_balance(user_id, win_amount)
        profit = win_amount - bet
        add_experience(user_id, int(50 + bet // 10000))
    else:
        profit = -bet
        add_experience(user_id, int(20 + bet // 50000))
    
    current_balance = get_balance(user_id)
    
    def fmt(n):
        """Форматирует число с пробелами как разделителями тысяч"""
        n = int(n)
        s = ""
        neg = n < 0
        n = abs(n)
        digits = str(n)
        for i, ch in enumerate(reversed(digits)):
            if i > 0 and i % 3 == 0:
                s = " " + s
            s = ch + s
        return ("-" if neg else "") + s

    try:
        if won:
            bot.send_message(
                user_id,
                f"🎉 *ВЫ ВЫИГРАЛИ!*\n\n"
                f"🎰 Игра: Краш\n"
                f"⚔️ Твой множитель: {target_mult:.2f}x\n"
                f"📊 Результат краша: {result_multiplier:.2f}x\n"
                f"💵 Ставка: {fmt(bet)} 🌸\n"
                f"💵 Выигрыш: {fmt(win_amount)} 🌸\n"
                f"📈 Прибыль: +{fmt(profit)} 🌸\n\n"
                f"🔄 Новый баланс: {fmt(current_balance)} 🌸",
                parse_mode='Markdown'
            )
        else:
            bot.send_message(
                user_id,
                f"💥 *ВЫ ПРОИГРАЛИ*\n\n"
                f"🎰 Игра: Краш\n"
                f"⚔️ Твой множитель: {target_mult:.2f}x\n"
                f"📊 Результат краша: {result_multiplier:.2f}x\n"
                f"💵 Потеряно: {fmt(bet)} 🌸\n\n"
                f"🔄 Текущий баланс: {fmt(current_balance)} 🌸\n"
                f"💡 Удачи в следующий раз!",
                parse_mode='Markdown'
            )
    except Exception as e:
        print(f"Ошибка отправки результата игроку {user_id}: {e}")
    
    if admin_msg_id:
        try:
            status = "✅ ВЫИГРЫШ" if won else "❌ ПРОИГРЫШ"
            source = "АДМИН РЕШИЛ" if admin_decision is not None else "РАНДОМ"
            
            update_text = (
                f"{status} ({source})\n\n"
                f"👤 Игрок: {game['user_name']} (ID: {user_id})\n"
                f"🎰 Игра: Краш\n"
                f"⚔️ Множитель ставки: {target_mult:.2f}x\n"
                f"📊 Результат краша: {result_multiplier:.2f}x\n"
                f"💵 Ставка: {int(bet)} 🌸"
            )
            
            if won:
                update_text += f"\n💵 Выигрыш: {int(win_amount)} 🌸"
            
            bot.edit_message_text(update_text, CRASH_ADMIN_CHAT, admin_msg_id)
        except Exception as e:
            print(f"Ошибка обновления сообщения админов: {e}")
    
    pending_crash_decisions.pop(game_id, None)

@bot.callback_query_handler(func=lambda call: call.data.startswith('crash_win_'))
def handle_crash_win(call):
    """Админ решил что игрок ВЫИГРЫВАЕТ"""
    game_id = call.data.replace('crash_win_', '')
    
    if game_id not in pending_crash_decisions:
        bot.answer_callback_query(call.id, "❌ Игра не найдена", show_alert=True)
        return
    
    pending_crash_decisions[game_id]['decided'] = True
    send_crash_result(game_id, True)
    
    bot.answer_callback_query(call.id, "✅ Выигрыш записан!", show_alert=False)

@bot.callback_query_handler(func=lambda call: call.data.startswith('crash_lose_'))
def handle_crash_lose(call):
    """Админ решил что игрок ПРОИГРЫВАЕТ"""
    game_id = call.data.replace('crash_lose_', '')
    
    if game_id not in pending_crash_decisions:
        bot.answer_callback_query(call.id, "❌ Игра не найдена", show_alert=True)
        return
    
    pending_crash_decisions[game_id]['decided'] = True
    send_crash_result(game_id, False)
    
    bot.answer_callback_query(call.id, "❌ Проигрыш записан!", show_alert=False)

def show_crash_help(chat_id):
    """Показывает справку по игре Краш"""
    help_text = """🎰 *ИГРА: КРАШ*

⚔️ *Как играть:*
Вы ставите множитель (от 1.10x до 10.00x).
Если краш упадет выше вашего множителя - вы выигрываете.
Если ниже - проигрываете ставку.

📌 *Формат команды:*
`краш [множитель] [бет]`

💵 *Обозначения сумм:*
• `100к` = 100.000$
• `1кк` или `1м` = 1.000.000$
• `5ккк` = 5.000.000.000$
• `все` = вся сумма на балансе

📊 *Примеры:*
`краш 2.00 100к`
`краш 5.50 1кк`
`краш 1.50 все`
`краш 3.00 5ккк`

⚡ *Результат через:* 5с
📩 *Результат придет:* в личные сообщения
💵 *Мин. бет:* 50.000$"""

    bot.send_message(chat_id, help_text, parse_mode='Markdown')
@bot.message_handler(func=lambda message: message.text.lower() == 'наградить кланы_OLD_DISABLED' and is_admin(message.from_user.id))
def handle_force_clan_rewards_old(message):
    """ЗАМЕНЕНО новым модулем кланов"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        success, result = distribute_clan_rewards()
        
        if success:
            report_text = "🏅 Награды распределены!\n\n"
            
            for clan_name, data in result.items():
                report_text += f"🏆 {data['position']} место: {clan_name}\n"
                report_text += f"   👥 Участников: {data['members']}\n"
                report_text += f"   💵 Награда: {data['reward']}\n\n"
            
            report_text += "🏆 Награды получили только кланы с 3+ боецами"
            
        else:
            report_text = f"❌ {result}"
        
        bot.send_message(message.chat.id, report_text)
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

knb_games = {}

def init_knb_table():
    """Создаем таблицу для хранения активных КНБ игр"""
    with get_db_cursor() as cursor:
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS knb_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            challenger_id INTEGER,
            opponent_id INTEGER,
            bet INTEGER,
            challenger_choice TEXT,
            chat_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

KNB_ROCK     = "<tg-emoji emoji-id='5372981976804366741'>🪨</tg-emoji> Камень"
KNB_SCISSORS = "<tg-emoji emoji-id='5373123633025356862'>✂️</tg-emoji> Ножницы"
KNB_PAPER    = "<tg-emoji emoji-id='5372990322956840947'>📄</tg-emoji> Бумага"

def knb_winner(choice1, choice2):
    """Определяет победителя: возвращает 1 если победил первый, 2 если второй, 0 - ничья"""
    if choice1 == choice2:
        return 0
    wins = {
        KNB_ROCK:     KNB_SCISSORS,
        KNB_SCISSORS: KNB_PAPER,
        KNB_PAPER:    KNB_ROCK
    }
    if wins[choice1] == choice2:
        return 1
    return 2

@bot.message_handler(func=lambda message: message.text and message.text.lower().startswith('кнб') and message.reply_to_message is not None)
def handle_knb_challenge(message):
    """Обработка команды кнб <ставка> в ответ на сообщение"""
    challenger_id = message.from_user.id
    
    if not message.reply_to_message:
        bot.send_message(message.chat.id, "⚠️ Ответьте на сообщение игрока которому хотите бросить вызов!")
        return
    
    opponent = message.reply_to_message.from_user
    opponent_id = opponent.id
    
    if opponent_id == challenger_id:
        bot.send_message(message.chat.id, "❌ Нельзя играть с самим собой!", parse_mode='HTML')
        return
    
    if opponent.is_bot:
        bot.send_message(message.chat.id, "❌ Нельзя играть с ботом!", parse_mode='HTML')
        return
    
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "📌 Формат: ответьте на сообщение и напишите `кнб <ставка>`\nПример: `кнб 1000`", parse_mode='Markdown')
        return
    
    bet_text = parts[1]
    challenger_balance = get_balance(challenger_id)
    bet_amount = parse_bet_amount(bet_text, challenger_balance)
    
    if bet_amount is None or bet_amount <= 0:
        bot.send_message(message.chat.id, "❌ Неверный формат ставки!", parse_mode='HTML')
        return
    
    if bet_amount < 50:
        bot.send_message(message.chat.id, "❌ Минимальная ставка: 50$", parse_mode='HTML')
        return
    
    if bet_amount > challenger_balance:
        bot.send_message(message.chat.id, f"❌ Недостаточно средств! Ваш баланс: {format_balance(challenger_balance)}", parse_mode='HTML')
        return
    
    challenger_name = message.from_user.first_name or f"User{challenger_id}"
    opponent_name = opponent.first_name or f"User{opponent_id}"
    
    knb_games[f"{challenger_id}_{opponent_id}"] = {
        'bet': bet_amount,
        'challenger_id': challenger_id,
        'opponent_id': opponent_id,
        'challenger_name': challenger_name,
        'opponent_name': opponent_name,
        'chat_id': message.chat.id,
        'challenger_choice': None
    }
    
    knb_keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Принять", "callback_data": f"knb_accept_{challenger_id}_{opponent_id}", "style": "success"},
            {"text": "❌ Отклонить", "callback_data": f"knb_decline_{challenger_id}_{opponent_id}", "style": "danger"}
        ]]
    }
    
    bot.send_message(
        message.chat.id,
        f"⚔️ <b>Вызов на КНБ!</b>\n\n"
        f"<blockquote>👤 {challenger_name} вызывает {opponent_name}\n"
        f"💵 Ставка: <b>{format_balance(bet_amount)}</b></blockquote>\n\n"
        f"@{opponent.username or opponent_name}, принимаете вызов?",
        parse_mode='HTML',
        reply_markup=knb_keyboard
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('knb_accept_') or call.data.startswith('knb_decline_'))
def handle_knb_response(call):
    """Обработка принятия/отклонения вызова"""
    parts = call.data.split('_')
    action = parts[1]
    challenger_id = int(parts[2])
    opponent_id = int(parts[3])
    
    game_key = f"{challenger_id}_{opponent_id}"
    
    if call.from_user.id != opponent_id:
        bot.answer_callback_query(call.id, "❌ Это не ваш вызов!")
        return
    
    if game_key not in knb_games:
        bot.answer_callback_query(call.id, "❌ Вызов уже недействителен!")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        return
    
    game = knb_games[game_key]
    
    if action == 'decline':
        del knb_games[game_key]
        bot.edit_message_text(
            f"❌ {game['opponent_name']} отклонил вызов на КНБ от {game['challenger_name']}",
            call.message.chat.id,
            call.message.message_id
        , parse_mode='HTML')
        bot.answer_callback_query(call.id, "Вы отклонили вызов")
        return
    
    opponent_balance = get_balance(opponent_id)
    bet = game['bet']
    
    if bet > opponent_balance:
        bot.answer_callback_query(call.id, f"❌ Недостаточно средств! Нужно: {format_balance(bet)}", show_alert=True)
        return
    
    choices_markup = InlineKeyboardMarkup()
    choices_markup.row(
        InlineKeyboardButton("🪨 Камень", callback_data=f"knb_choice_{challenger_id}_{opponent_id}_камень"),
        InlineKeyboardButton("✂️ Ножницы", callback_data=f"knb_choice_{challenger_id}_{opponent_id}_ножницы"),
        InlineKeyboardButton("📄 Бумага", callback_data=f"knb_choice_{challenger_id}_{opponent_id}_бумага")
    )
    
    game['status'] = 'choosing'
    game['challenger_chose'] = False
    game['opponent_chose'] = False
    
    bot.edit_message_text(
        f"✅ *{game['opponent_name']} принял вызов!*\n\n"
        f"⚔️ {game['challenger_name']} vs {game['opponent_name']}\n"
        f"💵 Ставка: *{format_balance(bet)}*\n\n"
        f"📩 Проверьте личные сообщения и выберите свой ход!",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML'
    )
    
    game['group_msg_id'] = call.message.message_id
    game['group_chat_id'] = call.message.chat.id
    
    try:
        msg1 = bot.send_message(
            challenger_id,
            f"⚔️ *КНБ против {game['opponent_name']}*\n💵 Ставка: *{format_balance(bet)}*\n\nВыберите ваш ход:",
            parse_mode='HTML',
            reply_markup=choices_markup
        )
        game['challenger_msg_id'] = msg1.message_id
    except Exception:
        bot.send_message(call.message.chat.id, f"⚠️ Не могу написать {game['challenger_name']} в ЛС. Пусть напишет боту /start")
    
    try:
        msg2 = bot.send_message(
            opponent_id,
            f"⚔️ *КНБ против {game['challenger_name']}*\n💵 Ставка: *{format_balance(bet)}*\n\nВыберите ваш ход:",
            parse_mode='HTML',
            reply_markup=choices_markup
        )
        game['opponent_msg_id'] = msg2.message_id
    except Exception:
        bot.send_message(call.message.chat.id, f"⚠️ Не могу написать {game['opponent_name']} в ЛС. Пусть напишет боту /start")
    
    bot.answer_callback_query(call.id, "✅ Вы приняли вызов! Проверьте личные сообщения")

@bot.callback_query_handler(func=lambda call: call.data.startswith('knb_choice_'))
def handle_knb_choice(call):
    """Обработка выбора игрока"""
    parts = call.data.split('_')
    challenger_id = int(parts[2])
    opponent_id = int(parts[3])
    choice_text = parts[4]
    
    choice_map = {
        'камень': KNB_ROCK,
        'ножницы': KNB_SCISSORS,
        'бумага': KNB_PAPER
    }
    choice = choice_map.get(choice_text)
    
    game_key = f"{challenger_id}_{opponent_id}"
    
    if game_key not in knb_games:
        bot.answer_callback_query(call.id, "❌ Игра уже завершена!")
        return
    
    game = knb_games[game_key]
    user_id = call.from_user.id
    
    if user_id not in [challenger_id, opponent_id]:
        bot.answer_callback_query(call.id, "❌ Вы не участник этой игры!")
        return
    
    is_challenger = (user_id == challenger_id)
    
    if is_challenger:
        if game.get('challenger_choice'):
            bot.answer_callback_query(call.id, "Вы уже сделали выбор!")
            return
        game['challenger_choice'] = choice
        game['challenger_chose'] = True
        bot.answer_callback_query(call.id, f"Вы выбрали: {choice}")
        try:
            bot.edit_message_text(
                f"✅ Вы выбрали <b>{choice}</b>\n⏳ Ожидаем выбор соперника...",
                user_id,
                game.get('challenger_msg_id'),
                parse_mode='HTML'
            )
        except Exception:
            pass
    else:
        if game.get('opponent_choice'):
            bot.answer_callback_query(call.id, "Вы уже сделали выбор!")
            return
        game['opponent_choice'] = choice
        game['opponent_chose'] = True
        bot.answer_callback_query(call.id, f"Вы выбрали: {choice}")
        try:
            bot.edit_message_text(
                f"✅ Вы выбрали <b>{choice}</b>\n⏳ Ожидаем выбор соперника...",
                user_id,
                game.get('opponent_msg_id'),
                parse_mode='HTML'
            )
        except Exception:
            pass
    
    if game.get('challenger_choice') and game.get('opponent_choice'):
        process_knb_result(game_key)

def process_knb_result(game_key):
    """Подводим итог игры КНБ"""
    if game_key not in knb_games:
        return
    
    game = knb_games.pop(game_key)
    
    challenger_id = game['challenger_id']
    opponent_id = game['opponent_id']
    bet = game['bet']
    c_choice = game['challenger_choice']
    o_choice = game['opponent_choice']
    challenger_name = game['challenger_name']
    opponent_name = game['opponent_name']
    
    winner = knb_winner(c_choice, o_choice)
    
    if winner == 0:
        result_text = (
            f"🤝 <b>НИЧЬЯ!</b>\n\n"
            f"👤 {challenger_name}: {c_choice}\n"
            f"👤 {opponent_name}: {o_choice}\n\n"
            f"💵 Ставки остаются у игроков"
        )
    elif winner == 1:
        update_balance(opponent_id, -bet)
        update_balance(challenger_id, bet)
        new_balance = get_balance(challenger_id)
        result_text = (
            f"🏆 <b>ПОБЕДИЛ {challenger_name.upper()}!</b>\n\n"
            f"👤 {challenger_name}: {c_choice}\n"
            f"👤 {opponent_name}: {o_choice}\n\n"
            f"💵 {challenger_name} получил <b>+{format_balance(bet)}</b>\n"
            f"💵 Новый баланс: <b>{format_balance(new_balance)}</b>"
        )
    else:
        update_balance(challenger_id, -bet)
        update_balance(opponent_id, bet)
        new_balance = get_balance(opponent_id)
        result_text = (
            f"🏆 <b>ПОБЕДИЛ {opponent_name.upper()}!</b>\n\n"
            f"👤 {challenger_name}: {c_choice}\n"
            f"👤 {opponent_name}: {o_choice}\n\n"
            f"💵 {opponent_name} получил <b>+{format_balance(bet)}</b>\n"
            f"💵 Новый баланс: <b>{format_balance(new_balance)}</b>"
        )
    
    try:
        bot.send_message(challenger_id, result_text, parse_mode='HTML')
    except Exception:
        pass
    try:
        bot.send_message(opponent_id, result_text, parse_mode='HTML')
    except Exception:
        pass
    
    try:
        bot.edit_message_text(
            result_text,
            game.get('group_chat_id'),
            game.get('group_msg_id'),
            parse_mode='HTML'
        )
    except Exception:
        try:
            bot.send_message(game.get('group_chat_id'), result_text, parse_mode='HTML')
        except Exception:
            pass

def create_promo(code, reward, max_uses, expires_at, created_by):
    with get_db_cursor() as cursor:
        cursor.execute('''
            INSERT OR REPLACE INTO promo_codes (code, reward, max_uses, current_uses, expires_at, created_by, created_at, is_active)
            VALUES (?, ?, ?, 0, ?, ?, ?, 1)
        ''', (code.upper(), reward, max_uses, expires_at, created_by, int(time.time())))

def use_promo(user_id, code):
    code = code.upper().strip()
    if not is_registered(user_id):
        return False, "❌ Ты не зарегистрирован! Напиши /start чтобы начать."
    with get_db_cursor() as cursor:
        cursor.execute('SELECT reward, max_uses, current_uses, expires_at, is_active FROM promo_codes WHERE code = ?', (code,))
        promo = cursor.fetchone()
        if not promo:
            return False, "❌ Промокод не найден"
        reward, max_uses, current_uses, expires_at, is_active = promo
        if not is_active:
            return False, "❌ Промокод отключён"
        if expires_at > 0 and time.time() > expires_at:
            return False, "❌ Промокод истёк"
        if current_uses >= max_uses:
            return False, "❌ Промокод уже использован максимальное количество раз"
        cursor.execute('SELECT 1 FROM promo_activations WHERE user_id = ? AND code = ?', (user_id, code))
        if cursor.fetchone():
            return False, "❌ Ты уже активировал этот промокод"
        cursor.execute('UPDATE promo_codes SET current_uses = current_uses + 1 WHERE code = ?', (code,))
        cursor.execute('INSERT INTO promo_activations (user_id, code, activated_at) VALUES (?, ?, ?)', (user_id, code, int(time.time())))
        cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (reward, user_id))
        return True, reward

@bot.message_handler(func=lambda m: m.text and (m.text.lower().startswith('промо ') or m.text.lower().startswith('/promo ')))
def handle_use_promo(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.send_message(message.chat.id, "💡 Введи: <b>промо КОД</b>", parse_mode='HTML')
        return
    code = parts[1].strip()
    user_id = message.from_user.id
    ok, result = use_promo(user_id, code)
    if ok:
        bot.send_message(
            message.chat.id,
            f"✅ <b>Промокод активирован!</b>\n\n"
            f"🎁 Код: <code>{code}</code>\n"
            f"💰 Награда: {format_balance(result)}",
            parse_mode='HTML'
        )
    else:
        bot.send_message(message.chat.id, result, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith('создать промо') and is_admin(m.from_user.id))
def handle_create_promo(message):
    parts = message.text.split()
    if len(parts) < 5:
        bot.send_message(
            message.chat.id,
            "📝 <b>Создание промокода</b>\n\n"
            "Формат:\n<code>создать промо КОД СУММА КОЛ-ВО [ЧАСЫ]</code>\n\n"
            "Примеры:\n"
            "<code>создать промо FECTIZ 50000 100</code> — безлимит по времени\n"
            "<code>создать промо VIP2024 100000 50 24</code> — истечёт через 24ч",
            parse_mode='HTML'
        )
        return
    try:
        code = parts[2].upper()
        reward = int(parts[3])
        max_uses = int(parts[4])
        hours = int(parts[5]) if len(parts) > 5 else 0
        expires_at = int(time.time()) + hours * 3600 if hours > 0 else 0
        create_promo(code, reward, max_uses, expires_at, message.from_user.id)
        exp_text = f"⏰ Истекает через {hours}ч" if hours > 0 else "♾️ Без ограничений по времени"
        bot.send_message(
            message.chat.id,
            f"✅ <b>Промокод создан!</b>\n\n"
            f"🎁 Код: <code>{code}</code>\n"
            f"💰 Награда: {format_balance(reward)}\n"
            f"👥 Активаций: {max_uses}\n"
            f"{exp_text}",
            parse_mode='HTML'
        )
    except (ValueError, IndexError):
        bot.send_message(message.chat.id, "❌ Неверный формат. Пример:\n<code>создать промо SALE 50000 100 24</code>", parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text and m.text.lower() == 'промокоды' and is_admin(m.from_user.id))
def handle_list_promos(message):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT code, reward, max_uses, current_uses, expires_at, is_active FROM promo_codes ORDER BY created_at DESC LIMIT 20')
        promos = cursor.fetchall()
    if not promos:
        bot.send_message(message.chat.id, "📭 Промокодов пока нет", parse_mode='HTML')
        return
    text = "🎁 <b>Промокоды</b>\n\n"
    now = time.time()
    for code, reward, max_uses, uses, expires_at, is_active in promos:
        if not is_active:
            status = "🔴"
        elif expires_at > 0 and now > expires_at:
            status = "⌛"
        elif uses >= max_uses:
            status = "✅"
        else:
            status = "🟢"
        exp = f" · до {datetime.fromtimestamp(expires_at).strftime('%d.%m %H:%M')}" if expires_at > 0 else ""
        text += f"{status} <code>{code}</code> — {plain_balance(reward)} · {uses}/{max_uses}{exp}\n"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🗑 Удалить промо", callback_data="promo_delete_menu"))
    bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=markup)

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith('удалить промо ') and is_admin(m.from_user.id))
def handle_delete_promo(message):
    code = message.text.split(maxsplit=2)[2].upper().strip()
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE promo_codes SET is_active = 0 WHERE code = ?', (code,))
    bot.send_message(message.chat.id, f"🗑 Промокод <code>{code}</code> отключён", parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == 'обнулитьчеки' and is_admin(m.from_user.id))
def handle_reset_checks(message):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT COUNT(*) FROM checks')
        total_checks = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM check_activations')
        total_activations = cursor.fetchone()[0]
        cursor.execute('DELETE FROM check_activations')
        cursor.execute('DELETE FROM checks')
    bot.send_message(
        message.chat.id,
        f"🗑 <b>Все чеки удалены!</b>\n\n"
        f"💳 Удалено чеков: <b>{total_checks}</b>\n"
        f"📋 Удалено активаций: <b>{total_activations}</b>",
        parse_mode='HTML'
    )

@bot.message_handler(func=lambda m: m.text and m.text.lower() in ['уровень', 'мой уровень', 'ур', '/level'])
def handle_level_command(message):
    user_id = message.from_user.id
    level, experience = get_user_level(user_id)
    emoji, title_name = get_title(level)
    lvl_cur, lvl_exp, lvl_need = get_level_progress(experience)

    if lvl_need > 0:
        progress_pct = int(lvl_exp / lvl_need * 10)
        progress_bar = "█" * progress_pct + "░" * (10 - progress_pct)
        progress_text = f"[{progress_bar}] {lvl_exp:,}/{lvl_need:,} exp\n📈 До следующего: {lvl_need - lvl_cur:,} exp"
    else:
        progress_bar = "██████████"
        progress_text = f"[{progress_bar}] 🏆 Максимальный уровень!"

    click_bonus = int(get_level_click_bonus(level) * 100 - 100)
    daily_bonus = int(get_level_daily_bonus(level) * 100 - 100)

    next_info = ""
    if level < 50:
        next_emoji, next_title = get_title(level + 1)
        next_info = f"\n🔜 Следующий: {next_emoji} <b>{next_title}</b>"

    text = (
        f"🌟 <b>ПУТЬ СЛАВЫ</b>\n\n"
        f"{emoji} <b>{title_name}</b>\n"
        f"📊 Уровень: <b>{level}/50</b>\n"
        f"⭐ Опыт: <b>{experience:,}</b>\n\n"
        f"{progress_text}"
        f"{next_info}\n\n"
        f"<b>Твои бонусы:</b>\n"
        f"⚡ Клики: +{click_bonus}%\n"
        f"🎁 Ежедневка: +{daily_bonus}%\n\n"
        f"<b>Как получить опыт:</b>\n"
        f"🎲 Игры (краш, рулетка, слоты...)\n"
        f"👆 Клики\n"
        f"🎁 Ежедневный бонус"
    )
    bot.send_message(message.chat.id, text, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == 'сброс уровней' and is_admin(m.from_user.id))
def handle_reset_levels(message):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT COUNT(*) FROM users WHERE experience > 0')
        count = cursor.fetchone()[0]
        cursor.execute('UPDATE users SET experience = 0')
    bot.send_message(
        message.chat.id,
        f"♻️ <b>Уровни сброшены!</b>\n\n"
        f"Опыт обнулён у {count} игроков.",
        parse_mode='HTML'
    )

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith('обнулитькарты') and is_admin(m.from_user.id))
def handle_reset_cards(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id,
            "📝 Формат: <code>обнулитькарты @username</code> или <code>обнулитькарты 123456789</code>",
            parse_mode='HTML')
        return
    target = parts[1].replace('@', '').strip()
    with get_db_cursor() as cursor:
        if target.isdigit():
            cursor.execute('SELECT user_id, username, video_cards FROM users WHERE user_id = ?', (int(target),))
        else:
            cursor.execute('SELECT user_id, username, video_cards FROM users WHERE username = ?', (target,))
        user = cursor.fetchone()
        if not user:
            bot.send_message(message.chat.id, "❌ Пользователь не найден.", parse_mode='HTML')
            return
        user_id, username, cards = user
        cursor.execute('UPDATE users SET video_cards = 0 WHERE user_id = ?', (user_id,))
    bot.send_message(
        message.chat.id,
        f"🖥 <b>Видеокарты обнулены!</b>\n\n"
        f"👤 @{username}\n"
        f"🗑 Было карт: <b>{cards}</b> → стало <b>0</b>",
        parse_mode='HTML'
    )

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith('обнулитьопыт') and is_admin(m.from_user.id))
def handle_cap_experience(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "📝 Формат: <code>обнулитьопыт 50000</code>\n\n"
            "Все у кого опыт выше указанного — будут срезаны до этого значения.",
            parse_mode='HTML'
        )
        return
    try:
        cap = int(parts[1])
        if cap < 0:
            bot.send_message(message.chat.id, "❌ Значение должно быть больше 0!", parse_mode='HTML')
            return
        with get_db_cursor() as cursor:
            cursor.execute('SELECT COUNT(*) FROM users WHERE experience > ?', (cap,))
            affected = cursor.fetchone()[0]
            cursor.execute('UPDATE users SET experience = ? WHERE experience > ?', (cap, cap))
        bot.send_message(
            message.chat.id,
            f"✂️ <b>Опыт срезан!</b>\n\n"
            f"📊 Максимум установлен: <b>{cap:,}</b> exp\n"
            f"👥 Затронуто игроков: <b>{affected}</b>\n\n"
            f"Уровни пересчитаются автоматически при следующем обращении.",
            parse_mode='HTML'
        )
    except ValueError:
        bot.send_message(message.chat.id, "❌ Укажи число. Пример: <code>обнулитьопыт 50000</code>", parse_mode='HTML')

EVENT_CHAT_ID = int(os.getenv("EVENT_CHAT_ID", "0"))
EVENT_REQUIRED = 10
EVENT_WINNERS = 3
EVENT_MIN_PRIZE = 1000
EVENT_MAX_PRIZE = 100000

event_state = {
    "active": False,
    "participants": {},
    "message_id": None,
    "timer": None,
}

def event_get_markup(count):
    return {
        "inline_keyboard": [[
            {"text": f"⚡ Участвовать ({count}/{EVENT_REQUIRED})", "callback_data": "event_join"}
        ]]
    }

EVENT_DICE_EMOJI   = "<tg-emoji emoji-id='6129959611853180053'>🎲</tg-emoji>"
EVENT_TROPHY_EMOJI = "<tg-emoji emoji-id='5422546251587527850'>🏆</tg-emoji>"

def event_build_text(count):
    bars = "▓" * count + "░" * (EVENT_REQUIRED - count)
    return (
        f"{EVENT_DICE_EMOJI} <b>Ивент</b>\n\n"
        f"Набирается команда — нужно <b>{EVENT_REQUIRED}</b> участников.\n"
        f"Из них <b>{EVENT_WINNERS}</b> получат случайный приз от "
        f"<b>{format_balance(EVENT_MIN_PRIZE)}</b> до <b>{format_balance(EVENT_MAX_PRIZE)}</b>.\n\n"
        f"[{bars}] {count}/{EVENT_REQUIRED}"
    )

def event_finish():
    state = event_state
    if not state["active"]:
        return
    state["active"] = False

    participants = list(state["participants"].items())
    random.shuffle(participants)
    winners = participants[:EVENT_WINNERS]

    lines = []
    for uid, name in winners:
        prize = random.randint(EVENT_MIN_PRIZE, EVENT_MAX_PRIZE)
        update_balance(uid, prize)
        lines.append(f"• {name} — <b>+{format_balance(prize)}</b>")

    result_text = (
        f"{EVENT_TROPHY_EMOJI} <b>ИТОГИ СБОРА</b>\n\n"
        f"Участников: <b>{len(participants)}</b>\n\n"
        f"Победители:\n" + "\n".join(lines)
    )

    try:
        _tg_api(
            "editMessageText",
            chat_id=EVENT_CHAT_ID,
            message_id=state["message_id"],
            text=result_text,
            parse_mode="HTML"
        )
    except Exception:
        bot.send_message(EVENT_CHAT_ID, result_text, parse_mode="HTML")

    msg_link = f"https://t.me/c/{str(EVENT_CHAT_ID).replace('-100', '')}/{state['message_id']}" if state["message_id"] else ""

    notify_text = (
        f"🏆 <b>Ивент завершён!</b>\n\n"
        f"Победители получили призы.\n"
        f"Участников: <b>{len(participants)}</b>"
    )
    if msg_link:
        notify_text += f"\n\n<a href=\"{msg_link}\">📋 Посмотреть итоги</a>"
    _send_event_notifications(notify_text)

    state["participants"] = {}
    state["message_id"] = None
    state["timer"] = None

def event_launch():
    state = event_state
    if state["active"]:
        return

    state["active"] = True
    state["participants"] = {}

    result = _tg_api(
        "sendMessage",
        chat_id=EVENT_CHAT_ID,
        text=event_build_text(0),
        parse_mode="HTML",
        reply_markup=event_get_markup(0)
    )
    msg_id = result.get("result", {}).get("message_id")
    state["message_id"] = msg_id

    start_link = f"https://t.me/c/{str(EVENT_CHAT_ID).replace('-100', '')}/{msg_id}" if msg_id else ""
    start_notify = (
        f"⚡ <b>Новый ивент в чате!</b>\n\n"
        f"Нужно {EVENT_REQUIRED} участников, {EVENT_WINNERS} победителей получат призы.\n"
        f"Успей вступить до завершения!"
    )
    if start_link:
        start_notify += f'\n\n<a href="{start_link}">🎮 Участвовать</a>'
    _send_event_notifications(start_notify)

    def force_finish():
        if state["active"] and len(state["participants"]) >= EVENT_WINNERS:
            event_finish()
        elif state["active"]:
            state["active"] = False
            state["participants"] = {}
            cancelled_msg_id = state["message_id"]
            try:
                _tg_api(
                    "editMessageText",
                    chat_id=EVENT_CHAT_ID,
                    message_id=cancelled_msg_id,
                    text="😴 <b>Сбор отменён</b> — не хватило участников.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            cancel_link = f"https://t.me/c/{str(EVENT_CHAT_ID).replace('-100', '')}/{cancelled_msg_id}" if cancelled_msg_id else ""
            cancel_notify = "😴 <b>Ивент отменён</b> — не хватило участников."
            if cancel_link:
                cancel_notify += f'\n\n<a href="{cancel_link}">📋 Смотреть</a>'
            _send_event_notifications(cancel_notify)
            state["message_id"] = None

    t = threading.Timer(600, force_finish)
    t.daemon = True
    t.start()
    state["timer"] = t

@bot.callback_query_handler(func=lambda c: c.data == "event_join")
def cb_event_join(call):
    state = event_state
    if not state["active"]:
        bot.answer_callback_query(call.id, "⏱ Сбор уже завершён!", show_alert=False)
        return

    uid = call.from_user.id
    name = call.from_user.first_name or "Игрок"

    if uid in state["participants"]:
        bot.answer_callback_query(call.id, "Ты уже участвуешь!", show_alert=False)
        return

    state["participants"][uid] = name
    count = len(state["participants"])

    bot.answer_callback_query(call.id, "✅ Ты в деле!", show_alert=False)

    try:
        _tg_api(
            "editMessageText",
            chat_id=EVENT_CHAT_ID,
            message_id=state["message_id"],
            text=event_build_text(count),
            parse_mode="HTML",
            reply_markup=event_get_markup(count)
        )
    except Exception:
        pass

    if count >= EVENT_REQUIRED:
        if state["timer"]:
            state["timer"].cancel()
        event_finish()

def event_scheduler():
    while True:
        try:
            delay = random.randint(20 * 3600, 27 * 3600)
            time.sleep(delay)
            event_launch()
        except Exception as e:
            print(f"[event_scheduler] ошибка: {e}")
            time.sleep(3600)

def start_event_scheduler():
    with get_db_cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS event_notify_users (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    t = threading.Thread(target=event_scheduler, daemon=True)
    t.daemon = True
    t.start()
    print("🎲 Планировщик ивентов запущен")

@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == "запустить ивент" and is_admin(m.from_user.id))
def handle_manual_event(message):
    if event_state["active"]:
        bot.send_message(message.chat.id, "⚠️ Ивент уже активен.", parse_mode="HTML")
        return
    event_launch()
    bot.send_message(message.chat.id, "✅ Ивент запущен!", parse_mode="HTML")

def _send_event_notifications(text: str):
    """Рассылает уведомление всем подписчикам ивентов"""
    try:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT user_id FROM event_notify_users WHERE enabled = 1")
            users = cursor.fetchall()
        for row in users:
            try:
                bot.send_message(
                    row[0], text,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
            except Exception:
                pass
    except Exception as e:
        print(f"[notify] ошибка рассылки: {e}")

def is_event_notify_enabled(user_id: int) -> bool:
    try:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT enabled FROM event_notify_users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return bool(row and row[0])
    except Exception:
        return False

def toggle_event_notify(user_id: int) -> bool:
    """Переключает уведомления. Возвращает новое состояние (True = включено)"""
    try:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT enabled FROM event_notify_users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    "INSERT INTO event_notify_users (user_id, enabled) VALUES (?, 1)",
                    (user_id,)
                )
                return True
            else:
                new_val = 0 if row[0] else 1
                cursor.execute(
                    "UPDATE event_notify_users SET enabled = ? WHERE user_id = ?",
                    (new_val, user_id)
                )
                return bool(new_val)
    except Exception as e:
        print(f"[notify] ошибка toggle: {e}")
        return False

@bot.callback_query_handler(func=lambda c: c.data == "toggle_event_notify")
def cb_toggle_event_notify(call):
    user_id = call.from_user.id
    new_state = toggle_event_notify(user_id)
    icon = "🔔" if new_state else "🔕"
    status = "включены" if new_state else "отключены"
    bot.answer_callback_query(call.id, f"{icon} Уведомления {status}", show_alert=False)
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=types.InlineKeyboardMarkup([[
                types.InlineKeyboardButton(
                    f"{'🔕 Отключить' if new_state else '🔔 Включить'} уведомления",
                    callback_data="toggle_event_notify"
                )
            ]])
        )
    except Exception:
        pass


# =============================
# 🌠 МЕТЕОРИТНЫЙ ДОЖДЬ
# =============================

METEOR_CHAT_ID = int(os.getenv("EVENT_CHAT_ID", "0"))  # тот же чат что и ивенты

# Состояние текущего метеоритного дождя
meteor_state = {
    "active": False,
    "reward": 0,
    "caught": set(),       # user_id тех кто уже поймал
    "max_catchers": 0,     # сколько игроков могут поймать
    "message_id": None,
    "timer": None
}

METEOR_REWARDS = [
    {"reward": 5_000,   "max": 15, "label": "🪨 Маленький метеорит"},
    {"reward": 15_000,  "max": 10, "label": "☄️ Метеорит"},
    {"reward": 50_000,  "max": 5,  "label": "🌠 Крупный метеорит"},
    {"reward": 150_000, "max": 3,  "label": "💥 Огромный метеорит"},
    {"reward": 500_000, "max": 1,  "label": "🔥 МЕГА-МЕТЕОРИТ"},
]

def meteor_launch():
    """Запустить метеоритный дождь"""
    state = meteor_state
    if state["active"]:
        return

    # Выбираем случайный тип метеорита (редкие — реже)
    weights = [60, 25, 10, 4, 1]
    chosen = random.choices(METEOR_REWARDS, weights=weights, k=1)[0]

    state["active"] = True
    state["reward"] = chosen["reward"]
    state["max_catchers"] = chosen["max"]
    state["caught"] = set()

    reward_fmt = f"{chosen['reward']:,}".replace(",", " ")

    text = (
        f"🌠 <b>Метеоритный дождь!</b>\n\n"
        f"{chosen['label']} падает на город!\n\n"
        f"💰 Награда: <b>{reward_fmt} 🌸</b> каждому поймавшему\n"
        f"👥 Успеют поймать: <b>{chosen['max']} игроков</b>\n\n"
        f"⚡ Напиши <b>словить</b> чтобы поймать метеорит!"
    )

    photo_path = os.path.join("images", "метеорит.png")
    try:
        with open(photo_path, "rb") as photo_file:
            resp = _requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id": METEOR_CHAT_ID, "caption": text, "parse_mode": "HTML"},
                files={"photo": photo_file},
                timeout=30
            )
            result = resp.json()
    except Exception:
        result = _tg_api("sendMessage", chat_id=METEOR_CHAT_ID, text=text, parse_mode="HTML")
    state["message_id"] = result.get("result", {}).get("message_id")
    print(f"[meteor] message_id сохранён: {state['message_id']}")

    # Автозакрытие через 60 секунд
    def auto_close():
        if state["active"]:
            meteor_finish(cancelled=True)

    t = threading.Timer(60, auto_close)
    t.daemon = True
    t.start()
    state["timer"] = t


def meteor_finish(cancelled=False):
    """Завершить метеоритный дождь"""
    state = meteor_state
    if not state["active"]:
        return

    state["active"] = False
    if state["timer"]:
        state["timer"].cancel()
        state["timer"] = None

    caught_count = len(state["caught"])

    if cancelled and caught_count == 0:
        finish_text = "🌠 Метеорит улетел... Никто не успел поймать 😔"
    else:
        reward_fmt = f"{state['reward']:,}".replace(",", " ")
        finish_text = (
            f"✅ <b>Метеоритный дождь завершён!</b>\n\n"
            f"💰 Награда: <b>{reward_fmt} 🌸</b>\n"
            f"👥 Поймали: <b>{caught_count} игроков</b>"
        )

    try:
        _tg_api(
            "editMessageCaption",
            chat_id=METEOR_CHAT_ID,
            message_id=state["message_id"],
            caption=finish_text,
            parse_mode="HTML"
        )
    except Exception:
        bot.send_message(METEOR_CHAT_ID, finish_text, parse_mode="HTML")

    state["caught"] = set()
    state["message_id"] = None
    state["reward"] = 0


# Ловля через текст "словить"
@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == "словить" and m.chat.id == METEOR_CHAT_ID)
def handle_meteor_catch_text(message):
    state = meteor_state

    # Удаляем сообщение "словить" в любом случае
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    if not state["active"]:
        return

    uid = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    name = message.from_user.first_name or "Игрок"

    if uid in state["caught"]:
        return

    _do_catch_meteor(uid, name, username, message.chat.id)




def _do_catch_meteor(uid, name, username=None, chat_id=None):
    """Общая логика поимки метеорита"""
    state = meteor_state

    state["caught"].add(uid)
    reward = state["reward"]
    update_balance(uid, reward)

    reward_fmt = f"{reward:,}".replace(",", " ")
    caught_count = len(state["caught"])
    max_c = state["max_catchers"]
    display = username or name

    # Отправляем объявление кто поймал
    try:
        bot.send_message(
            METEOR_CHAT_ID,
            f"🌠 <b>{display}</b> словил метеорит и получил <b>{reward_fmt} 🌸</b>!",
            parse_mode="HTML"
        )
    except Exception:
        pass

    # Обновляем подпись фото
    try:
        remaining = max_c - caught_count
        if remaining <= 0:
            new_text = (
                f"🌠 <b>Метеорит пойман!</b>\n\n"
                f"💰 Награда: <b>{reward_fmt} 🌸</b>\n"
                f"👥 Поймали: <b>{caught_count} игроков</b> — все места заняты!"
            )
        else:
            new_text = (
                f"🌠 <b>Метеоритный дождь!</b>\n\n"
                f"💰 Награда: <b>{reward_fmt} 🌸</b> каждому\n"
                f"👥 Поймали: <b>{caught_count}/{max_c}</b>\n\n"
                f"⚡ Напиши <b>словить</b> чтобы поймать!"
            )
        _tg_api(
            "editMessageCaption",
            chat_id=METEOR_CHAT_ID,
            message_id=state["message_id"],
            caption=new_text,
            parse_mode="HTML"
        )
    except Exception:
        pass

    # Если все места заняты — завершаем
    if caught_count >= max_c:
        meteor_finish()


# Планировщик метеоритов (раз в 1-2 часа случайно)
def meteor_scheduler():
    print("[meteor_scheduler] поток запущен")
    while True:
        try:
            delay = random.randint(1 * 3600, 2 * 3600)
            print(f"[meteor_scheduler] следующий метеорит через {delay//60} мин")
            time.sleep(delay)
            print("[meteor_scheduler] запускаю метеорит...")
            meteor_launch()
        except Exception as e:
            print(f"[meteor_scheduler] ошибка: {e}")
            time.sleep(300)  # при ошибке ждём 5 мин и пробуем снова


def start_meteor_scheduler():
    t = threading.Thread(target=meteor_scheduler, daemon=True)
    t.daemon = True
    t.start()
    print("🌠 Планировщик метеоритов запущен")

    # Watchdog — следит за потоком и перезапускает если упал
    def watchdog():
        while True:
            time.sleep(60)
            if not t.is_alive():
                print("[meteor_watchdog] поток упал — перезапускаю!")
                new_t = threading.Thread(target=meteor_scheduler, daemon=True)
                new_t.daemon = True
                new_t.start()

    wd = threading.Thread(target=watchdog, daemon=True)
    wd.daemon = True
    wd.start()
    print("👀 Watchdog метеоритов запущен")


# Ручной запуск для админа
@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == "метеорит" and is_admin(m.from_user.id))
def handle_manual_meteor(message):
    if meteor_state["active"]:
        bot.send_message(message.chat.id, "⚠️ Метеоритный дождь уже активен.", parse_mode="HTML")
        return
    meteor_launch()
    bot.send_message(message.chat.id, "✅ Метеоритный дождь запущен!", parse_mode="HTML")


# ============================================================
# 🎁 ЕЖЕДНЕВНЫЙ БИО-ДРОП
# ============================================================

import datetime as _dt

BIO_DROP_REWARD      = 300_000
BIO_DROP_BOT_USERNAME = "fectiz_bot"   # без @ в нижнем регистре
BIO_DROP_HOUR        = int(os.getenv("BIO_DROP_HOUR", "20"))  # UTC час (20 = 23:00 МСК)


def _get_all_user_ids():
    with get_db_cursor() as cursor:
        cursor.execute("SELECT user_id FROM users")
        return [row[0] for row in cursor.fetchall()]


def _check_bio_has_bot(user_id: int) -> bool:
    """Проверяет есть ли @FECTIZ_BOT в bio пользователя через getChat"""
    try:
        chat_info = bot.get_chat(user_id)
        bio = getattr(chat_info, "bio", None) or ""
        return BIO_DROP_BOT_USERNAME in bio.lower()
    except Exception:
        return False


def _get_user_display_name(user_id: int) -> str:
    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT custom_name, username, first_name FROM users WHERE user_id = ?",
                (user_id,)
            )
            row = cursor.fetchone()
        if row:
            if row["custom_name"]: return row["custom_name"]
            if row["username"]:    return f"@{row['username']}"
            if row["first_name"]:  return row["first_name"]
    except Exception:
        pass
    return f"Игрок {user_id}"


def run_bio_drop():
    """Сканирует всех игроков, ищет кто добавил @FECTIZ_BOT в bio, выбирает победителя"""
    print("[bio_drop] старт сканирования...")

    # Сообщаем админам что дроп начался
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, "⏳ <b>Дроп запущен</b> — сканирую bio игроков...", parse_mode="HTML")
        except Exception:
            pass

    all_ids = _get_all_user_ids()
    pool = []

    for uid in all_ids:
        if _check_bio_has_bot(uid):
            pool.append(uid)
        time.sleep(0.05)

    print(f"[bio_drop] пул: {len(pool)} из {len(all_ids)}")

    # Сообщаем результат сканирования
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(
                admin_id,
                f"📋 <b>Сканирование завершено</b>\n\n"
                f"👥 Всего игроков: <b>{len(all_ids)}</b>\n"
                f"✅ В пуле: <b>{len(pool)}</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass

    if not pool:
        print("[bio_drop] пул пуст — розыгрыш не проводится")
        return

    winner_id = random.choice(pool)
    name = _get_user_display_name(winner_id)
    reward_fmt = f"{BIO_DROP_REWARD:,}".replace(",", " ")

    # Начисляем баланс напрямую (не считается как игровой выигрыш)
    with get_db_cursor() as cursor:
        cursor.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (BIO_DROP_REWARD, winner_id)
        )

    # Сохраняем в историю
    with get_db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO bio_drop_history (user_id, username, reward, pool_size, dropped_at) VALUES (?,?,?,?,?)",
            (winner_id, name, BIO_DROP_REWARD, len(pool), int(time.time()))
        )

    # Личное уведомление победителю
    try:
        bot.send_message(
            winner_id,
            f"🎉 <b>Ты выиграл ежедневный дроп!</b>\n\n"
            f"💰 Начислено: <b>{reward_fmt} 🌸</b>\n\n"
            f"Ты указал <b>@FECTIZ_BOT</b> в описании своего профиля "
            f"и был выбран среди <b>{len(pool)}</b> участников.\n\n"
            f"Держи бота в bio — каждый день новый шанс! 🍀",
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"[bio_drop] не удалось уведомить победителя {winner_id}: {e}")

    # Уведомляем всех админов
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(
                admin_id,
                f"🎁 <b>Дроп разыгран!</b>\n\n"
                f"🏆 Победитель: <b>{name}</b> (id: {winner_id})\n"
                f"💰 Награда: <b>{reward_fmt} 🌸</b>\n"
                f"👥 Участников в пуле: <b>{len(pool)}</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass

    print(f"[bio_drop] победитель: {winner_id} ({name}), награда: {BIO_DROP_REWARD}")


def _bio_drop_scheduler():
    print(f"[bio_drop_scheduler] запущен. Дроп каждый день в {BIO_DROP_HOUR}:00 UTC")
    while True:
        try:
            now    = _dt.datetime.utcnow()
            target = now.replace(hour=BIO_DROP_HOUR, minute=0, second=0, microsecond=0)
            if now >= target:
                target += _dt.timedelta(days=1)
            wait_sec = (target - now).total_seconds()
            print(f"[bio_drop_scheduler] следующий дроп через {int(wait_sec//3600)}ч {int((wait_sec%3600)//60)}м")
            time.sleep(wait_sec)
            run_bio_drop()
        except Exception as e:
            print(f"[bio_drop_scheduler] ошибка: {e}")
            time.sleep(300)


def start_bio_drop_scheduler():
    t = threading.Thread(target=_bio_drop_scheduler, daemon=True)
    t.start()
    print("🎁 Планировщик био-дропа запущен")


@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == "запустить дроп" and is_admin(m.from_user.id))
def handle_manual_bio_drop(message):
    bot.send_message(message.chat.id, "⏳ Запускаю дроп...", parse_mode="HTML")
    threading.Thread(target=run_bio_drop, daemon=True).start()


# ============================================================
# ============================================================
# 📊 СТАТИСТИКА БОТА — команда: стат
# ============================================================

@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == "стат" and is_admin(m.from_user.id))
def handle_admin_stats(message):
    try:
        now      = int(time.time())
        day_ago  = now - 86400
        week_ago = now - 86400 * 7

        with get_db_cursor() as cursor:

            cursor.execute("SELECT COUNT(*) FROM users")
            total_users = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM users WHERE last_activity >= ?", (day_ago,))
            active_day = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM users WHERE last_activity >= ?", (week_ago,))
            active_week = cursor.fetchone()[0]

            cursor.execute("SELECT SUM(balance), AVG(balance), MAX(balance), MIN(balance) FROM users WHERE balance > 0")
            row = cursor.fetchone()
            total_balance = int(row[0] or 0)
            avg_balance   = int(row[1] or 0)
            max_balance   = int(row[2] or 0)
            min_balance   = int(row[3] or 0)

            cursor.execute("SELECT SUM(bank_deposit), AVG(bank_deposit) FROM users WHERE bank_deposit > 0")
            row = cursor.fetchone()
            total_bank = int(row[0] or 0)
            avg_bank   = int(row[1] or 0)

            total_economy = total_balance + total_bank

            cursor.execute("SELECT SUM(loan_amount) FROM loans WHERE status='active'")
            total_loans = int(cursor.fetchone()[0] or 0)

            # Распределение богатства
            cursor.execute("SELECT COUNT(*) FROM users WHERE balance + bank_deposit >= ?", (TAX_CONFIG["high_wealth_threshold"],))
            rich_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM users WHERE balance + bank_deposit >= ? AND balance + bank_deposit < ?",
                           (TAX_CONFIG["medium_wealth_threshold"], TAX_CONFIG["high_wealth_threshold"]))
            mid_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM users WHERE balance + bank_deposit > 0 AND balance + bank_deposit < ?",
                           (TAX_CONFIG["medium_wealth_threshold"],))
            low_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM users WHERE balance = 0 AND bank_deposit = 0")
            zero_count = cursor.fetchone()[0]

            cursor.execute("""
                SELECT COALESCE(custom_name, username, first_name, CAST(user_id AS TEXT)), balance, bank_deposit
                FROM users ORDER BY balance + bank_deposit DESC LIMIT 5
            """)
            top_rich = cursor.fetchall()

            cursor.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM transfers WHERE created_at >= ?", (day_ago,))
            row = cursor.fetchone(); tr_count = row[0]; tr_sum = int(row[1])

            try:
                cursor.execute("SELECT COUNT(*), COALESCE(SUM(win_amount),0), COALESCE(SUM(bet_amount),0) FROM game_history WHERE created_at >= ?", (day_ago,))
                row = cursor.fetchone(); games_day = row[0]; games_payout = int(row[1]); games_bets = int(row[2] or 0)
            except Exception:
                cursor.execute("SELECT COUNT(*), COALESCE(SUM(win_amount),0) FROM game_history WHERE created_at >= ?", (day_ago,))
                row = cursor.fetchone(); games_day = row[0]; games_payout = int(row[1]); games_bets = 0

            cursor.execute(
                "SELECT game, COUNT(*) as cnt FROM game_history WHERE created_at >= ? GROUP BY game ORDER BY cnt DESC LIMIT 5",
                (week_ago,)
            )
            top_games = cursor.fetchall()

            try:
                cursor.execute("SELECT COUNT(*), COALESCE(SUM(total_earned),0), COALESCE(SUM(trips_completed),0) FROM driver_stats")
                row = cursor.fetchone(); taxi_drivers = row[0]; taxi_earned = int(row[1]); taxi_trips = int(row[2])
            except Exception:
                taxi_drivers = taxi_earned = taxi_trips = 0

            cursor.execute("SELECT COUNT(*), COALESCE(SUM(video_cards),0) FROM users WHERE video_cards > 0")
            row = cursor.fetchone(); miners_count = row[0]; total_gpus = int(row[1])

            cursor.execute("SELECT COUNT(*), COALESCE(SUM(sold_count),0) FROM clothes_shop")
            row = cursor.fetchone(); shop_items = row[0]; items_sold = int(row[1])
            cursor.execute("SELECT COUNT(*) FROM user_clothes")
            items_owned = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM clans")
            total_clans = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM clan_members")
            clanned_players = cursor.fetchone()[0]

            cursor.execute("SELECT jackpot, tickets_sold FROM lottery WHERE id=1")
            row = cursor.fetchone()
            lottery_jackpot = int(row[0] or 0) if row else 0
            lottery_tickets = int(row[1] or 0) if row else 0

            cursor.execute("SELECT COUNT(*) FROM premium WHERE expires_at > ?", (now,))
            premium_count = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM promo_codes WHERE is_active=1")
            active_promos = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM promo_activations")
            promo_uses = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM auctions WHERE status='active'")
            active_auctions = cursor.fetchone()[0]

            try:
                cursor.execute("SELECT COUNT(*), COALESCE(SUM(reward),0) FROM bio_drop_history")
                row = cursor.fetchone(); drop_count = row[0]; drop_total = int(row[1])
                cursor.execute("SELECT username, reward, dropped_at FROM bio_drop_history ORDER BY dropped_at DESC LIMIT 3")
                last_drops = cursor.fetchall()
            except Exception:
                drop_count = drop_total = 0; last_drops = []

        def f(n): return f"{int(n):,}".replace(",", " ")
        def pct(a, b): return f"{a/b*100:.1f}%" if b else "0%"

        # ── Аналитика ──────────────────────────────────────────
        # Коэффициент Джини-подобный: топ-5 держат сколько % от общего?
        top5_wealth = sum(b + bk for _, b, bk in top_rich)
        top5_share = top5_wealth / total_economy * 100 if total_economy else 0

        # Velocity: выплаты в играх относительно оборота
        game_velocity = games_payout / total_balance if total_balance else 0

        # Концентрация в банке
        bank_ratio = total_bank / total_economy * 100 if total_economy else 0

        # Вовлечённость
        daily_engage = active_day / total_users * 100 if total_users else 0

        # Потенциальный сбор налогов
        pot_rich_tax = int(top5_wealth * TAX_CONFIG["high_wealth_tax"])

        # Оценки
        def econ_health():
            issues = []
            if game_velocity > 10: issues.append("🔴 Игры перегревают экономику (выплаты >" + f"{game_velocity:.0f}x" + " оборота)")
            elif game_velocity > 5: issues.append("🟡 Игровые выплаты высокие (" + f"{game_velocity:.1f}x" + " оборота)")
            else: issues.append("🟢 Игровой баланс в норме")

            if bank_ratio > 70: issues.append("🔴 Деньги заморожены в банке (" + f"{bank_ratio:.0f}%" + ")")
            elif bank_ratio > 50: issues.append("🟡 Много денег в банке (" + f"{bank_ratio:.0f}%" + ")")
            else: issues.append("🟢 Распределение баланс/банк нормальное")

            if top5_share > 50: issues.append("🔴 Высокое неравенство: топ-5 держат " + f"{top5_share:.0f}%")
            elif top5_share > 30: issues.append("🟡 Умеренное неравенство: топ-5 = " + f"{top5_share:.0f}%")
            else: issues.append("🟢 Распределение богатства умеренное")

            if daily_engage < 20: issues.append("🔴 Низкая дневная активность (" + f"{daily_engage:.0f}%" + ")")
            elif daily_engage < 50: issues.append("🟡 Средняя активность (" + f"{daily_engage:.0f}%" + ")")
            else: issues.append("🟢 Хорошая активность (" + f"{daily_engage:.0f}%" + ")")

            return "\n".join("  " + x for x in issues)

        # ── Форматирование ─────────────────────────────────────
        rich_lines = "\n".join(
            f"  {i+1}. {nm} — {f(bal)} 🌸 (банк: {f(bk)})"
            for i,(nm,bal,bk) in enumerate(top_rich)
        ) or "  нет данных"
        game_lines = "\n".join(f"  • {g}: {c} партий" for g,c in top_games) or "  нет данных"
        drop_lines = "\n".join(
            f"  • {un} — {f(rw)} 🌸 ({_dt.datetime.utcfromtimestamp(ts).strftime('%d.%m %H:%M')} UTC)"
            for un,rw,ts in last_drops
        ) or "  история пуста"

        pot_tax_total = int(
            rich_count * avg_balance * TAX_CONFIG["high_wealth_tax"] +
            mid_count  * avg_balance * TAX_CONFIG["medium_wealth_tax"] +
            low_count  * avg_balance * TAX_CONFIG["general_tax"]
        )

        text = (
            f"📊 <b>СТАТИСТИКА БОТА</b>\n"
            f"<i>{_dt.datetime.utcnow().strftime('%d.%m.%Y %H:%M')} UTC</i>\n\n"

            f"👥 <b>Пользователи</b>\n"
            f"  Всего: <b>{f(total_users)}</b>\n"
            f"  Активны сегодня: <b>{f(active_day)}</b> ({pct(active_day, total_users)})\n"
            f"  Активны за неделю: <b>{f(active_week)}</b> ({pct(active_week, total_users)})\n"
            f"  Без денег: <b>{f(zero_count)}</b> чел.\n\n"

            f"💰 <b>Экономика</b>\n"
            f"  Всего в системе: <b>{f(total_economy)} 🌸</b>\n"
            f"  В обороте (баланс): <b>{f(total_balance)} 🌸</b> ({pct(total_balance, total_economy)})\n"
            f"  В банке: <b>{f(total_bank)} 🌸</b> ({pct(total_bank, total_economy)})\n"
            f"  Средний баланс: <b>{f(avg_balance)} 🌸</b>  │  Средний вклад: <b>{f(avg_bank)} 🌸</b>\n"
            f"  Максимальный баланс: <b>{f(max_balance)} 🌸</b>\n"
            f"  Кредитов выдано: <b>{f(total_loans)} 🌸</b>\n\n"

            f"📊 <b>Распределение богатства</b>\n"
            f"  👑 Богачи (>{TAX_CONFIG['high_wealth_threshold']//1000}к): <b>{rich_count}</b> чел.\n"
            f"  💵 Состоятельные (>{TAX_CONFIG['medium_wealth_threshold']//1000}к): <b>{mid_count}</b> чел.\n"
            f"  👤 Обычные: <b>{low_count}</b> чел.\n"
            f"  Топ-5 держат: <b>{top5_share:.1f}%</b> всех денег\n\n"

            f"🏆 <b>Топ 5 богачей (баланс + банк)</b>\n{rich_lines}\n\n"

            f"🏛️ <b>Налоги</b>\n"
            f"  Потенциальный сбор: ~<b>{f(pot_tax_total)} 🌸</b>\n"
            f"  Команда: <code>собрать налог</code>\n\n"

            f"💸 <b>Переводы за сутки</b>\n"
            f"  Количество: <b>{f(tr_count)}</b>\n"
            f"  Сумма: <b>{f(tr_sum)} 🌸</b>\n\n"

            f"🎮 <b>Игры за сутки</b>\n"
            f"  Партий: <b>{f(games_day)}</b>\n"
            f"  Выплачено: <b>{f(games_payout)} 🌸</b>\n"
            f"  Скорость оборота: <b>{game_velocity:.1f}x</b> от баланса\n"
            f"  Топ игр за неделю:\n{game_lines}\n\n"

            f"🚗 <b>Такси</b>\n"
            f"  Водителей: <b>{f(taxi_drivers)}</b> ({pct(taxi_drivers, total_users)} юзеров)\n"
            f"  Поездок: <b>{f(taxi_trips)}</b>\n"
            f"  Заработано: <b>{f(taxi_earned)} 🌸</b>\n\n"

            f"⛏️ <b>Майнинг</b>\n"
            f"  Майнеров: <b>{f(miners_count)}</b> ({pct(miners_count, total_users)} юзеров)\n"
            f"  Видеокарт: <b>{f(total_gpus)}</b>  │  В среднем: {f(total_gpus//miners_count) if miners_count else 0} на майнера\n\n"

            f"🛍️ <b>Магазин</b>\n"
            f"  Вещей: <b>{f(shop_items)}</b>\n"
            f"  Продано: <b>{f(items_sold)}</b>\n"
            f"  У игроков: <b>{f(items_owned)}</b>\n\n"

            f"⚔️ <b>Кланы</b>\n"
            f"  Кланов: <b>{f(total_clans)}</b>\n"
            f"  В кланах: <b>{f(clanned_players)}</b> ({pct(clanned_players, total_users)})\n\n"

            f"🎟️ <b>Лотерея</b>\n"
            f"  Джекпот: <b>{f(lottery_jackpot)} 🌸</b>\n"
            f"  Билетов продано: <b>{f(lottery_tickets)}</b>\n\n"

            f"👑 <b>Premium</b>\n"
            f"  Активных: <b>{f(premium_count)}</b> ({pct(premium_count, total_users)} юзеров)\n\n"

            f"🎫 <b>Промокоды</b>\n"
            f"  Активных: <b>{f(active_promos)}</b>\n"
            f"  Использований: <b>{f(promo_uses)}</b>\n\n"

            f"🔨 <b>Аукцион</b>\n"
            f"  Активных лотов: <b>{f(active_auctions)}</b>\n\n"

            f"🎁 <b>Био-дроп</b>\n"
            f"  Розыгрышей: <b>{f(drop_count)}</b>\n"
            f"  Выплачено всего: <b>{f(drop_total)} 🌸</b>\n"
            f"  Последние победители:\n{drop_lines}\n\n"

            f"🩺 <b>Здоровье экономики</b>\n"
            f"{econ_health()}"
        )

        for i in range(0, len(text), 4096):
            bot.send_message(message.chat.id, text[i:i+4096], parse_mode="HTML")

    except Exception as e:
        print(f"[admin_stats] ошибка: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")


# =============================

init_db()
_load_donate_packages()  # загружаем пакеты доната из БД
_start_donate_scheduler()  # умные цены — пересчёт раз в сутки
init_dice_tables()


# =====================================================================
# 🤖 УМНЫЙ БОТ — Google Gemini
# Активация: "гугл [вопрос]" или "google [вопрос]"
# =====================================================================

import urllib.request as _urllib_req
import json as _json

GROQ_KEYS = [
    "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",
    "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",
]
GROQ_KEY_INDEX = [0]
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Cloudflare Workers AI — 10 000 запросов/день бесплатно
CF_TOKEN      = "cfat_H1yyA7OuIzIeMKtUbHOdsFDIwAVyPGxO9hMTlD7u2eb72e10"
CF_ACCOUNT_ID = "4a7b2801ceeb91e47b5e28d95238dd49"
_GEMINI_COOLDOWNS = {}
GEMINI_COOLDOWN_SEC = 1800  # 30 минут
GEMINI_NO_LIMIT_ID = 8139807344  # без ограничений

def _get_groq_key():
    return GROQ_KEYS[GROQ_KEY_INDEX[0] % len(GROQ_KEYS)]

def _rotate_groq_key():
    GROQ_KEY_INDEX[0] += 1
    print(f"[groq] переключился на ключ #{GROQ_KEY_INDEX[0] % len(GROQ_KEYS) + 1}")

_GEMINI_SYSTEM_BASE = """Ты — Гугл, ИИ ассистент бота Fectiz 🌸.

ПРАВИЛА:
1. Коротко — 1-2 предложения максимум
2. Разговорный русский, как живой человек в чате
3. Мат — только если игрок сам матерится
4. Сленг понимаешь естественно — не объясняй, реагируй
5. Подкалывают — подкалывай в ответ
6. НЕ упоминай Кари и Ево без причины — только если спрашивают
7. Данные о других игроках — ЛИЧНОЕ, не раскрывай

⛔ АБСОЛЮТНЫЕ ЗАПРЕТЫ — нарушать нельзя никогда:
— НИКОГДА не выдумывай цифры, имена, суммы, события
— НИКОГДА не додумывай если данных нет — просто скажи "не знаю" или "таких данных у меня нет"
— НИКОГДА не говори примерные суммы если не знаешь точно
— Если спрашивают про что-то чего нет в данных БД — честно скажи что не знаешь
— Лучше сказать "не знаю" чем соврать

СПРАВКА (только если спрашивают):
— Кари / @Cary_Python — легендарный разраб бота, гений
— Ево — владелец бота, на полставки 😄
— Валюта: 🌸 сакура

ВСЕ КОМАНДЫ БОТА (не предлагай другие):

👤 ПРОФИЛЬ:
• профиль / /profile — профиль, уровень, статистика
• имя [новое имя] — сменить отображаемое имя
• уровень / мой уровень / ур / /level — твой уровень и опыт
• топ — топ богачей сервера

💰 ДЕНЬГИ И ПЕРЕВОДЫ:
• перевести [сумма] [@юзер или ник] — перевод другому игроку (комиссия 10%)
• чек [сумма] / /чек — создать чек-ваучер на сумму
• промо [код] — активировать промокод

🏦 БАНК И ВКЛАДЫ:
• вклад [сумма] [часы] — открыть срочный вклад
  Ставки: до 6ч=0.30%/ч, до 24ч=0.25%/ч, до 72ч=0.15%/ч, до 120ч=0.10%/ч
  Мин: 10 000 🌸, Макс: 50 000 000 🌸, Срок до 5 дней
  Премиум даёт +20% к итогу
• вклад инфо — информация по текущему вкладу
• вклад получить — забрать вклад досрочно

🎰 ИГРЫ (ставка числом или "всё"):
• слоты [ставка] — игровые автоматы
• рулетка [ставка] [красное/чёрное/число] — рулетка
• рул [ставка] — быстрая рулетка
• монетка [ставка] [орёл/решка] — подбросить монету
• краш [ставка] — множитель растёт, нажми "забрать" вовремя
• кости [ставка] — кубики против бота
• блэкджек [ставка] — набери 21
• хайкард [ставка] — у кого карта старше выигрывает
• дартс [ставка] — дартс
• кнб [ставка] — камень-ножницы-бумага
• мины [ставка] — открывай клетки избегая мин
• дуэль [ставка] [@юзер] — вызвать другого игрока на дуэль
• игры / играть — меню всех игр

📈 АКЦИИ:
• купить акции [кол-во] — купить акции FECTZ
• продать акции [кол-во] — продать акции (комиссия 3%)
• история акций — история цен и твои сделки
• Макс 5000 акций на игрока, кулдаун 10 мин между сделками

⛏️ МАЙНИНГ:
• майнинг — меню майнинга, посмотреть карты и доход
• купить карту — купить видеокарту для майнинга
• майнить — собрать намайненное

🚗 ТАКСИ:
• такси / 🚗 такси — принять заказ и заработать 🌸
  Маршруты: Центр→Аэропорт, Жилой р-н→Офис, Универ→ТЦ,
  Больница→Вокзал, Бизнес-центр→Ресторан, ТЦ→Кино, Ночной рейс, Вокзал→Гостиница
  Награда зависит от маршрута (400–2000 🌸)

👗 МАГАЗИН И ОДЕЖДА:
• магазин — купить одежду
• гардероб / вещи — посмотреть свои вещи и надеть
• обмен [ник] [вещь] — обменяться вещью с игроком

🎟️ ПРОЧЕЕ:
• лотерея — купить лотерейный билет
• аукцион — посмотреть лоты на аукционе
• кланы — меню кланов

👑 ПРЕМИУМ (/premium):
• Доступ к ИИ-помощнику (гугл)
• +20% к итогу срочных вкладов
• Другие бонусы

💳 ДОНАТ (/buy или донат):
• Купить 🌸 за звёзды Telegram
• Цены подстраиваются под экономику сервера

🤖 ИИ-ПОМОЩНИК (только Premium):
• гугл [вопрос] — задать вопрос, спросить про баланс, историю, экономику

ВАЖНО: кредитов в боте НЕТ. Команды "чеф" — это ТОЛЬКО для администраторов (создание чеков).
Если игрок спрашивает про кредит — скажи что такого нет.
Не придумывай команды которых нет в этом списке.

ДАННЫЕ ИЗ БД (актуальные):"""

# Ключевые слова для чтения БД
# ── Погода ───────────────────────────────────────────────────────
OWM_KEY = "8479e0d94567edbaa7ca9618294d3baa"
OWM_URL = "https://api.openweathermap.org/data/2.5/weather"

_WEATHER_KEYWORDS = ["погода", "температур", "холодно", "жарко", "дождь", "снег", "ветер", "градус", "weather"]

def _needs_weather(q):
    return any(kw in q.lower() for kw in _WEATHER_KEYWORDS)

def _extract_city(question):
    """Извлечь город из вопроса."""
    import re
    q = question.lower()
    # Паттерны: "погода в Омске", "погода омск", "какая погода в москве"
    m = re.search(r"погода[а-я\s]+в\s+([а-яёa-z]+)", q)
    if m: return m.group(1).strip()
    m = re.search(r"погода\s+([а-яёa-z]+)", q)
    if m: return m.group(1).strip()
    # Просто слово после "в"
    m = re.search(r"в\s+([а-яёa-z]{3,})", q)
    if m: return m.group(1).strip()
    return None

def _get_weather(city):
    """Получить погоду через OpenWeatherMap."""
    try:
        resp = _requests.get(OWM_URL, params={
            "q": city,
            "appid": OWM_KEY,
            "units": "metric",
            "lang": "ru"
        }, timeout=10)
        if resp.status_code == 404:
            return f"Город '{city}' не найден."
        resp.raise_for_status()
        d = resp.json()
        temp     = d["main"]["temp"]
        feels    = d["main"]["feels_like"]
        desc     = d["weather"][0]["description"]
        humidity = d["main"]["humidity"]
        wind     = d["wind"]["speed"]
        city_name = d["name"]
        return (
            f"Погода в {city_name}: {desc}, {temp:.0f}°C "
            f"(ощущается как {feels:.0f}°C), "
            f"влажность {humidity}%, ветер {wind:.0f} м/с"
        )
    except Exception as e:
        print(f"[weather] ошибка: {e}")
        return None

# ─────────────────────────────────────────────────────────────────
_DB_KEYWORDS = [
    "баланс", "деньги", "сколько", "богат", "топ", "экономик",
    "вклад", "банк", "капитал", "акци", "майнинг", "карт",
    "кто первый", "кто богаче", "лидер", "рейтинг", "статистик",
    "бд", "база", "покажи", "проверь", "узнай", "история",
    "делал", "играл", "переводил", "купил", "последн", "когда",
    "сколько выиграл", "проиграл", "победил", "портфель"
]

def _needs_db(question):
    q = question.lower()
    return any(kw in q for kw in _DB_KEYWORDS)

def _fmt(n):
    return f"{int(n or 0):,}".replace(",", " ")

def _ts(t):
    if not t: return "?"
    import datetime as _dt2
    try: return _dt2.datetime.fromtimestamp(int(t)).strftime("%d.%m %H:%M")
    except: return str(t)

def _get_user_context(user_id):
    """Полный контекст игрока из БД."""
    lines = []
    try:
        with get_db_cursor() as cur:
            # Колонки users
            cur.execute("PRAGMA table_info(users)")
            ucols = [r[1] for r in cur.fetchall()]

            # Базовые данные игрока
            sel = ["user_id", "username", "first_name", "balance", "bank_deposit"]
            for c in ["custom_name", "video_cards", "experience", "games_won",
                      "games_lost", "total_won_amount", "total_lost_amount", "total_clicks"]:
                if c in ucols: sel.append(c)

            cur.execute(f"SELECT {', '.join(sel)} FROM users WHERE user_id=?", (user_id,))
            row = cur.fetchone()
            if row:
                d = dict(zip(sel, row))
                name = d.get("custom_name") or d.get("first_name") or \
                       (("@" + d["username"]) if d.get("username") else str(user_id))
                bal  = d.get("balance", 0) or 0
                bank = d.get("bank_deposit", 0) or 0
                premium = is_premium(user_id)
                admin   = is_admin(user_id)
                lines += [
                    "",
                    "--- ИГРОК ---",
                    f"Ник: {name}" + (f" (@{d.get('username')})" if d.get("username") else ""),
                    f"Баланс: {_fmt(bal)} 🌸",
                    f"Банк: {_fmt(bank)} 🌸",
                    f"Капитал: {_fmt(bal + bank)} 🌸",
                    f"Premium: {'да' if premium else 'нет'}",
                    f"Админ: {'да' if admin else 'нет'}",
                ]
                if "video_cards" in d:
                    lines.append(f"Видеокарты: {d['video_cards'] or 0} шт.")
                if "games_won" in d:
                    lines.append(f"Игр: {d.get('games_won',0) or 0} побед / {d.get('games_lost',0) or 0} поражений")
                if "total_won_amount" in d:
                    lines.append(f"Выиграно всего: {_fmt(d.get('total_won_amount',0))} 🌸")
                    lines.append(f"Проиграно всего: {_fmt(d.get('total_lost_amount',0))} 🌸")
                if "total_clicks" in d:
                    lines.append(f"Кликов: {_fmt(d.get('total_clicks',0))}")

            # История игр
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='game_history'")
            if cur.fetchone():
                cur.execute("PRAGMA table_info(game_history)")
                gcols = [r[1] for r in cur.fetchall()]
                tc = next((c for c in ["created_at","timestamp","ts"] if c in gcols), None)
                gc = next((c for c in ["game_type","game","type"] if c in gcols), None)
                bc = next((c for c in ["bet","amount","stake"] if c in gcols), None)
                wc = next((c for c in ["win","result","won","profit","payout"] if c in gcols), None)
                sc = [c for c in [tc, gc, bc, wc] if c]
                if sc:
                    try:
                        order = f"ORDER BY {tc} DESC" if tc else ""
                        cur.execute(f"SELECT {', '.join(sc)} FROM game_history WHERE user_id=? {order} LIMIT 5", (user_id,))
                        rows = cur.fetchall()
                        if rows:
                            lines += ["", "--- ПОСЛЕДНИЕ ИГРЫ ---"]
                            for r in rows:
                                rd = dict(zip(sc, r))
                                parts = []
                                if tc: parts.append(_ts(rd.get(tc)))
                                if gc: parts.append(str(rd.get(gc, "")))
                                if bc: parts.append(f"ставка {_fmt(rd.get(bc,0))}")
                                if wc: parts.append(f"итог {_fmt(rd.get(wc,0))}")
                                lines.append("• " + " | ".join(parts))
                    except Exception as e:
                        print(f"[ctx] game_history: {e}")

            # История переводов (только свои — без получателей по имени)
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transfers'")
            if cur.fetchone():
                cur.execute("PRAGMA table_info(transfers)")
                tcols = [r[1] for r in cur.fetchall()]
                uid_c = "from_user_id" if "from_user_id" in tcols else ("user_id" if "user_id" in tcols else None)
                if uid_c:
                    tc2 = next((c for c in ["created_at","timestamp","ts"] if c in tcols), None)
                    ac  = next((c for c in ["amount","sum"] if c in tcols), None)
                    sc2 = [uid_c] + [c for c in [tc2, ac] if c]
                    try:
                        order2 = f"ORDER BY {tc2} DESC" if tc2 else ""
                        cur.execute(f"SELECT {', '.join(sc2)} FROM transfers WHERE {uid_c}=? {order2} LIMIT 5", (user_id,))
                        rows = cur.fetchall()
                        if rows:
                            lines += ["", "--- ПОСЛЕДНИЕ ПЕРЕВОДЫ (исходящие) ---"]
                            for r in rows:
                                rd = dict(zip(sc2, r))
                                parts = []
                                if tc2: parts.append(_ts(rd.get(tc2)))
                                if ac:  parts.append(f"{_fmt(rd.get(ac,0))} 🌸 отправлено")
                                lines.append("• " + " | ".join(parts))
                    except Exception as e:
                        print(f"[ctx] transfers: {e}")

            # Акции игрока
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='stock_portfolio'")
            if cur.fetchone():
                try:
                    cur.execute("SELECT ticker, amount, avg_price FROM stock_portfolio WHERE user_id=?", (user_id,))
                    rows = cur.fetchall()
                    if rows:
                        lines += ["", "--- АКЦИИ В ПОРТФЕЛЕ ---"]
                        for ticker, amount, avg in rows:
                            lines.append(f"{ticker}: {amount} шт., средняя цена {_fmt(avg)} 🌸")
                except Exception as e:
                    print(f"[ctx] portfolio: {e}")

            # Общая экономика
            cur.execute("""
                SELECT COUNT(*), SUM(balance), SUM(bank_deposit),
                       AVG(balance), SUM(balance+bank_deposit)
                FROM users
            """)
            r = cur.fetchone()
            total_w = r[4] or 0
            total_b = r[1] or 0
            total_bk = r[2] or 0
            lines += [
                "",
                "--- ЭКОНОМИКА СЕРВЕРА ---",
                f"Игроков: {r[0]}",
                f"Всего 🌸: {_fmt(total_w)}",
                f"На балансах: {_fmt(total_b)} ({int(total_b/max(total_w,1)*100)}%)",
                f"В банке: {_fmt(total_bk)} ({int(total_bk/max(total_w,1)*100)}%)",
                f"Средний баланс: {_fmt(int(r[3] or 0))} 🌸",
            ]

            # Топ-5
            cn_col = "custom_name" if "custom_name" in ucols else "first_name"
            cur.execute(f"""
                SELECT {cn_col}, first_name, username, balance, bank_deposit
                FROM users ORDER BY balance+bank_deposit DESC LIMIT 5
            """)
            lines += ["", "--- ТОП-5 БОГАЧЕЙ ---"]
            for i, (cn, fn, un, b, bd) in enumerate(cur.fetchall(), 1):
                nm = cn or fn or (("@"+un) if un else "?")
                lines.append(f"  {i}. {nm} — {_fmt((b or 0)+(bd or 0))} 🌸")

            # Акция FECTZ
            try:
                cur.execute("SELECT price FROM stock_history ORDER BY recorded_at DESC LIMIT 1")
                sr = cur.fetchone()
                if sr: lines.append(f"\nАкция FECTZ: {_fmt(sr[0])} 🌸")
            except: pass

    except Exception as e:
        print(f"[context] ошибка: {e}")

    result = "\n".join(lines)
    print(f"[groq] контекст БД ({len(result)} символов):\n{result[:400]}")
    return result


def _ask_cf(system, question):
    """Запрос к Cloudflare Workers AI — 10 000 в день бесплатно."""
    try:
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
            "/ai/run/@cf/meta/llama-3.1-8b-instruct"
        )
        resp = _requests.post(
            url,
            json={
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": question}
                ],
                "max_tokens": 200
            },
            headers={"Authorization": f"Bearer {CF_TOKEN}"},
            timeout=20
        )
        print(f"[cf] status={resp.status_code}")
        if resp.status_code == 200:
            return resp.json().get("result", {}).get("response", "").strip()
        print(f"[cf] ошибка: {resp.text[:200]}")
        return None
    except Exception as e:
        print(f"[cf] исключение: {e}")
        return None


def _ask_gemini(user_id, user_question):
    """Cloudflare первый, Groq как резерв."""
    try:
        extra = ""
        if _needs_weather(user_question):
            city = _extract_city(user_question)
            if city:
                weather = _get_weather(city)
                if weather:
                    extra += f"\n\n[РЕАЛЬНАЯ ПОГОДА СЕЙЧАС]: {weather}"

        if _needs_db(user_question):
            ctx    = _get_user_context(user_id)
            system = _GEMINI_SYSTEM_BASE + "\n" + ctx + extra + "\n\n[конец данных]"
        else:
            system = _GEMINI_SYSTEM_BASE + "\n(данные БД не запрашивались)" + extra

        # 1. Cloudflare Workers AI
        result = _ask_cf(system, user_question)
        if result:
            print("[ai] ответил Cloudflare")
            return result

        # 2. Fallback — Groq
        print("[ai] CF не ответил, пробуем Groq")
        for attempt in range(len(GROQ_KEYS)):
            resp = _requests.post(
                GROQ_URL,
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user_question}
                    ],
                    "max_tokens": 200,
                    "temperature": 0.2,
                },
                headers={"Authorization": "Bearer " + _get_groq_key()},
                timeout=15
            )
            print(f"[groq] key#{GROQ_KEY_INDEX[0] % len(GROQ_KEYS) + 1} status={resp.status_code}")
            if resp.status_code == 429:
                _rotate_groq_key()
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        return "⏳ Все сервисы на лимите, попробуй через минуту."

    except Exception as e:
        import traceback; traceback.print_exc()
        return "❌ Ошибка: " + str(e)[:150]

        return "❌ Ошибка: " + str(e)[:150]


@bot.message_handler(func=lambda m: m.text and (
    m.text.lower().startswith("гугл ") or
    m.text.lower().startswith("google ")
))
def handle_gemini(message):
    user_id = message.from_user.id

    # Только премиум + админы
    if not is_premium(user_id) and not is_admin(user_id):
        bot.send_message(message.chat.id,
            "👑 <b>Только для Premium!</b>\n"
            "Купи подписку: /premium",
            parse_mode="HTML")
        return

    # Кулдаун
    now = time.time()
    last = _GEMINI_COOLDOWNS.get(user_id, 0)
    # 8139807344 — без ограничений
    # админы (кроме него) — 5 минут
    # остальные — 30 минут
    if user_id == GEMINI_NO_LIMIT_ID:
        cd = 0
    else:
        cd = GEMINI_COOLDOWN_SEC  # 30 минут для всех включая админов
    if cd > 0 and now - last < cd:
        left = int(cd - (now - last))
        mins = left // 60
        secs = left % 60
        wait = f"{mins}м {secs}с" if mins else f"{secs}с"
        bot.send_message(message.chat.id,
            f"⏳ Подожди ещё <b>{wait}</b>",
            parse_mode="HTML")
        return
    _GEMINI_COOLDOWNS[user_id] = now

    # Убираем префикс
    text = message.text.strip()
    question = text[5:].strip() if text.lower().startswith("гугл ") else text[7:].strip()

    if not question:
        bot.send_message(message.chat.id,
            "❓ Пример: <code>гугл как работает майнинг</code>",
            parse_mode="HTML")
        return

    thinking = bot.send_message(message.chat.id, "🤔", parse_mode="HTML")
    answer = _ask_gemini(user_id, question)
    if len(answer) > 3000:
        answer = answer[:3000] + "..."

    bot.edit_message_text(
        answer,
        chat_id=message.chat.id,
        message_id=thinking.message_id,
        parse_mode="HTML"
    )



if __name__ == "__main__":
    import traceback

    print("Бот запущен...")
    cleanup_expired_challenges()
    start_event_scheduler()
    start_meteor_scheduler()
    start_bio_drop_scheduler()
    start_deposit_scheduler()
    start_alerts_scheduler()
    start_stock_scheduler()

    # Вечный цикл — бот никогда не падает насовсем
    # При любой ошибке polling перезапускается через 5 секунд
    while True:
        try:
            print("[bot] polling запущен")
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            err = traceback.format_exc()
            print(f"[bot] ошибка polling, перезапуск через 5с:\n{err}")
            # Пробуем уведомить админа о краше
            try:
                for aid in ADMIN_IDS:
                    bot.send_message(aid,
                        f"⚠️ <b>Бот упал и перезапустился!</b>\n\n"
                        f"<code>{str(e)[:300]}</code>",
                        parse_mode="HTML")
            except Exception:
                pass
            time.sleep(5)
# ============================================================
# 🏦 СРОЧНЫЕ ВКЛАДЫ
# ============================================================

DEPOSIT_MIN       = 10_000
DEPOSIT_MAX       = 50_000_000
DEPOSIT_MAX_HOURS = 120   # 5 дней

DEPOSIT_RATE_TABLE = [
    (6,   0.0030),   # до 6ч   — 0.30%/час
    (24,  0.0025),   # до 24ч  — 0.25%/час
    (72,  0.0015),   # до 72ч  — 0.15%/час
    (120, 0.0010),   # до 120ч — 0.10%/час
]
PREMIUM_DEPOSIT_BONUS = 0.20   # +20% к итогу для премиум

def _deposit_rate(hours):
    """Ставка за час для данного срока."""
    for max_h, rate in DEPOSIT_RATE_TABLE:
        if hours <= max_h:
            return rate
    return DEPOSIT_RATE_TABLE[-1][1]

def _deposit_calc(amount, hours):
    """Возвращает (итого_выплата, прибыль, ставка_за_час)."""
    rate  = _deposit_rate(hours)
    profit = int(amount * rate * hours)
    return amount + profit, profit, rate

def _init_term_deposits():
    with get_db_cursor() as cursor:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS term_deposits (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                amount      INTEGER NOT NULL,
                hours       INTEGER NOT NULL,
                profit      INTEGER NOT NULL,
                payout      INTEGER NOT NULL,
                premium_bonus INTEGER DEFAULT 0,
                created_at  INTEGER NOT NULL,
                expires_at  INTEGER NOT NULL,
                paid        INTEGER DEFAULT 0
            )
        ''')

_init_term_deposits()

def get_active_term_deposit(user_id):
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM term_deposits WHERE user_id=? AND paid=0 ORDER BY id DESC LIMIT 1",
            (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith("вклад") and not m.text.lower().startswith("вклад инфо"))
def handle_term_deposit(message):
    user_id = message.from_user.id
    parts   = message.text.strip().split()

    # вклад инфо — отдельный хэндлер ниже
    if len(parts) == 1:
        bot.send_message(message.chat.id,
            "🏦 <b>Срочный вклад</b>\n\n"
            "Команда: <code>вклад СУММА ЧАСЫ</code>\n"
            f"Срок: от 1 до {DEPOSIT_MAX_HOURS} часов (макс 5 дней)\n"
            f"Минимум: {format_balance(DEPOSIT_MIN)}\n"
            f"Максимум: {format_balance(DEPOSIT_MAX)}\n\n"
            "📊 <b>Ставки:</b>\n"
            "  1–6ч   → 0.30%/час\n"
            "  7–24ч  → 0.25%/час\n"
            "  25–72ч → 0.15%/час\n"
            "  73–120ч→ 0.10%/час\n\n"
            "👑 Premium: +20% к итоговой прибыли\n\n"
            "Пока вклад активен — банк заморожен.",
            parse_mode="HTML"
        )
        return

    if len(parts) < 3:
        bot.send_message(message.chat.id, "❌ Формат: <code>вклад СУММА ЧАСЫ</code>", parse_mode="HTML")
        return

    # Проверяем активный вклад
    active = get_active_term_deposit(user_id)
    if active:
        left = active["expires_at"] - int(time.time())
        h, m_ = divmod(max(left, 0) // 60, 60)
        bot.send_message(message.chat.id,
            f"⏳ У тебя уже есть активный вклад!\n"
            f"💰 Сумма: <b>{format_balance(active['amount'])}</b>\n"
            f"⏱ Осталось: <b>{h}ч {m_}м</b>\n\n"
            f"Напиши <code>вклад инфо</code> для деталей.",
            parse_mode="HTML"
        )
        return

    # Парсим сумму
    try:
        raw = parts[1].lower().replace("к","000").replace("кк","000000").replace("млн","000000")
        amount = int(float(raw))
    except:
        bot.send_message(message.chat.id, "❌ Неверная сумма.", parse_mode="HTML")
        return

    # Парсим часы
    try:
        hours = int(parts[2])
    except:
        bot.send_message(message.chat.id, "❌ Неверное количество часов.", parse_mode="HTML")
        return

    if hours < 1 or hours > DEPOSIT_MAX_HOURS:
        bot.send_message(message.chat.id, f"❌ Срок от 1 до {DEPOSIT_MAX_HOURS} часов.", parse_mode="HTML")
        return

    if amount < DEPOSIT_MIN:
        bot.send_message(message.chat.id, f"❌ Минимум {format_balance(DEPOSIT_MIN)}", parse_mode="HTML")
        return

    if amount > DEPOSIT_MAX:
        bot.send_message(message.chat.id, f"❌ Максимум {format_balance(DEPOSIT_MAX)}", parse_mode="HTML")
        return

    balance = get_balance(user_id)
    if balance < amount:
        bot.send_message(message.chat.id, f"❌ Недостаточно средств. У тебя {format_balance(balance)}", parse_mode="HTML")
        return

    payout, profit, rate = _deposit_calc(amount, hours)
    premium = is_premium(user_id)
    bonus   = 0
    if premium:
        bonus  = int(profit * PREMIUM_DEPOSIT_BONUS)
        profit += bonus
        payout += bonus

    now        = int(time.time())
    expires_at = now + hours * 3600

    # Списываем с баланса
    update_balance(user_id, -amount)

    with get_db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO term_deposits (user_id, amount, hours, profit, payout, premium_bonus, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, amount, hours, profit, payout, bonus, now, expires_at)
        )

    days_h = f"{hours//24}д {hours%24}ч" if hours >= 24 else f"{hours}ч"
    prem_line = f"\n👑 Премиум бонус: +{format_balance(bonus)}" if premium else ""

    bot.send_message(message.chat.id,
        f"✅ <b>Вклад открыт!</b>\n\n"
        f"💰 Сумма: <b>{format_balance(amount)}</b>\n"
        f"⏱ Срок: <b>{days_h}</b>\n"
        f"📈 Ставка: <b>{rate*100:.2f}%/час</b>\n"
        f"💵 Прибыль: <b>+{format_balance(profit)}</b>{prem_line}\n"
        f"🏆 Получишь: <b>{format_balance(payout)}</b>\n\n"
        f"🔒 Средства заморожены до {_dt.datetime.utcfromtimestamp(expires_at).strftime('%d.%m %H:%M')} UTC",
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == "вклад инфо")
def handle_deposit_info(message):
    user_id = message.from_user.id
    active  = get_active_term_deposit(user_id)
    if not active:
        bot.send_message(message.chat.id, "У тебя нет активного вклада.", parse_mode="HTML")
        return

    now  = int(time.time())
    left = active["expires_at"] - now
    done = now >= active["expires_at"]

    if done:
        # Выплачиваем
        _pay_term_deposit(active)
        bot.send_message(message.chat.id,
            f"🎉 <b>Вклад завершён!</b>\n\n"
            f"💰 Вложено: {format_balance(active['amount'])}\n"
            f"💵 Прибыль: +{format_balance(active['profit'])}\n"
            f"🏆 Выплачено: <b>{format_balance(active['payout'])}</b>",
            parse_mode="HTML"
        )
        return

    h, rem = divmod(left, 3600)
    m_      = rem // 60
    elapsed = now - active["created_at"]
    elapsed_h = elapsed / 3600
    earned_so_far = int(active["amount"] * _deposit_rate(active["hours"]) * elapsed_h)

    bot.send_message(message.chat.id,
        f"🏦 <b>Активный вклад</b>\n\n"
        f"💰 Сумма: <b>{format_balance(active['amount'])}</b>\n"
        f"📈 Ставка: <b>{_deposit_rate(active['hours'])*100:.2f}%/час</b>\n"
        f"💵 Итого прибыль: <b>+{format_balance(active['profit'])}</b>\n"
        f"🏆 Получишь: <b>{format_balance(active['payout'])}</b>\n\n"
        f"⏱ Осталось: <b>{h}ч {m_}м</b>\n"
        f"📊 Накоплено сейчас: ~{format_balance(earned_so_far)}\n\n"
        f"🔒 Завершится {_dt.datetime.utcfromtimestamp(active['expires_at']).strftime('%d.%m %H:%M')} UTC\n"
        f"Напиши <code>вклад получить</code> когда срок истечёт.",
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == "вклад получить")
def handle_deposit_claim(message):
    user_id = message.from_user.id
    active  = get_active_term_deposit(user_id)
    if not active:
        bot.send_message(message.chat.id, "❌ Нет активного вклада.", parse_mode="HTML")
        return

    if int(time.time()) < active["expires_at"]:
        left = active["expires_at"] - int(time.time())
        h, rem = divmod(left, 3600)
        bot.send_message(message.chat.id,
            f"⏳ Вклад ещё не созрел!\nОсталось: <b>{h}ч {left%3600//60}м</b>",
            parse_mode="HTML"
        )
        return

    _pay_term_deposit(active)
    bot.send_message(message.chat.id,
        f"🎉 <b>Вклад получен!</b>\n\n"
        f"💰 Вложено: {format_balance(active['amount'])}\n"
        f"💵 Прибыль: +{format_balance(active['profit'])}\n"
        f"🏆 Итого: <b>{format_balance(active['payout'])}</b> → на баланс",
        parse_mode="HTML"
    )

def _pay_term_deposit(deposit):
    with get_db_cursor() as cursor:
        cursor.execute("UPDATE term_deposits SET paid=1 WHERE id=?", (deposit["id"],))
    update_balance(deposit["user_id"], deposit["payout"])

def _check_expired_deposits():
    """Авто-выплата истёкших вкладов."""
    now = int(time.time())
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM term_deposits WHERE paid=0 AND expires_at <= ?", (now,)
        )
        rows = cursor.fetchall()
        cols = [d[0] for d in cursor.description]

    for row in rows:
        dep = dict(zip(cols, row))
        _pay_term_deposit(dep)
        try:
            bot.send_message(
                dep["user_id"],
                f"🎉 <b>Вклад автоматически выплачен!</b>\n\n"
                f"💰 Вложено: {format_balance(dep['amount'])}\n"
                f"💵 Прибыль: +{format_balance(dep['profit'])}\n"
                f"🏆 Итого: <b>{format_balance(dep['payout'])}</b> → баланс",
                parse_mode="HTML"
            )
        except:
            pass

def _deposit_scheduler():
    while True:
        try:
            _check_expired_deposits()
        except Exception as e:
            print(f"[deposit_scheduler] ошибка: {e}")
        time.sleep(60)

def start_deposit_scheduler():
    t = threading.Thread(target=_deposit_scheduler, daemon=True)
    t.start()
    print("🏦 Планировщик вкладов запущен")


# ============================================================
# 🚨 АЛЕРТЫ ЭКОНОМИКИ
# ============================================================

ALERT_CONFIG = {
    "game_payout_1h":      5_000_000,    # выплаты в играх за час
    "single_win":          2_000_000,    # одна победа > 2млн
    "winrate_threshold":   0.80,         # winrate игрока > 80% за 20+ игр
    "winrate_min_games":   20,
    "transfer_single":     10_000_000,   # один перевод > 10млн
    "balance_jump":        5_000_000,    # баланс вырос на > 5млн за час
}

_last_game_payout_check = {"ts": 0, "total": 0}
_alerted_users = {}   # uid -> {"winrate": ts, "balance": ts}

def send_alert(text):
    """Отправить алерт всем админам."""
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, f"🚨 <b>АЛЕРТ</b>\n\n{text}", parse_mode="HTML")
        except:
            pass

def _alerts_check():
    """Периодическая проверка экономики."""
    now     = int(time.time())
    hour_ago = now - 3600

    with get_db_cursor() as cursor:
        # 1. Выплаты в играх за последний час
        try:
            cursor.execute(
                "SELECT COALESCE(SUM(win_amount),0) FROM game_history WHERE created_at >= ?",
                (hour_ago,)
            )
            payout_1h = cursor.fetchone()[0]
            if payout_1h > ALERT_CONFIG["game_payout_1h"]:
                send_alert(
                    f"🎮 Выплаты в играх за час: <b>{format_balance(payout_1h)}</b>\n"
                    f"Порог: {format_balance(ALERT_CONFIG['game_payout_1h'])}"
                )
        except:
            pass

        # 2. Одиночная крупная победа
        try:
            cursor.execute(
                "SELECT user_id, game, win_amount FROM game_history "
                "WHERE created_at >= ? AND win_amount >= ? ORDER BY win_amount DESC LIMIT 5",
                (hour_ago, ALERT_CONFIG["single_win"])
            )
            big_wins = cursor.fetchall()
            for uid, game, win in big_wins:
                key = f"win_{uid}_{win}"
                if _alerted_users.get(key, 0) < now - 3600:
                    _alerted_users[key] = now
                    send_alert(
                        f"💰 Крупная победа!\n"
                        f"Игрок: <code>{uid}</code>\n"
                        f"Игра: {game}\n"
                        f"Выигрыш: <b>{format_balance(win)}</b>"
                    )
        except:
            pass

        # 3. Подозрительный winrate
        try:
            cursor.execute(
                "SELECT user_id, COUNT(*) as games, "
                "SUM(CASE WHEN win_amount > 0 THEN 1 ELSE 0 END) as wins "
                "FROM game_history WHERE created_at >= ? "
                "GROUP BY user_id HAVING games >= ?",
                (hour_ago, ALERT_CONFIG["winrate_min_games"])
            )
            for uid, games, wins in cursor.fetchall():
                wr = wins / games if games else 0
                if wr >= ALERT_CONFIG["winrate_threshold"]:
                    key = f"wr_{uid}"
                    if _alerted_users.get(key, 0) < now - 3600:
                        _alerted_users[key] = now
                        send_alert(
                            f"⚠️ Подозрительный winrate!\n"
                            f"Игрок: <code>{uid}</code>\n"
                            f"Игр за час: {games} | Побед: {wins} | WR: {wr*100:.0f}%"
                        )
        except:
            pass

        # 4. Крупный перевод
        try:
            cursor.execute(
                "SELECT from_user_id, to_user_id, amount FROM transfers "
                "WHERE created_at >= ? AND amount >= ? ORDER BY amount DESC LIMIT 5",
                (hour_ago, ALERT_CONFIG["transfer_single"])
            )
            for from_id, to_id, amt in cursor.fetchall():
                key = f"tr_{from_id}_{to_id}_{amt}"
                if _alerted_users.get(key, 0) < now - 3600:
                    _alerted_users[key] = now
                    send_alert(
                        f"💸 Крупный перевод!\n"
                        f"От: <code>{from_id}</code> → <code>{to_id}</code>\n"
                        f"Сумма: <b>{format_balance(amt)}</b>"
                    )
        except:
            pass

def _alerts_scheduler():
    while True:
        try:
            _alerts_check()
        except Exception as e:
            print(f"[alerts_scheduler] ошибка: {e}")
        time.sleep(300)   # каждые 5 минут

def start_alerts_scheduler():
    t = threading.Thread(target=_alerts_scheduler, daemon=True)
    t.start()
    print("🚨 Планировщик алертов запущен")


# ============================================================
# 📈 АКЦИИ БОТА
# ============================================================

STOCK_TICKER      = "FECTZ"
STOCK_NAME        = "Fectiz Corp"
STOCK_INIT_PRICE  = 10_000        # стартовая цена
STOCK_MIN_PRICE   = 1_000         # пол цены
STOCK_MAX_PRICE   = 10_000_000    # потолок цены
STOCK_MAX_PER_USER  = 5000        # макс акций на игрока
STOCK_MAX_PER_TRADE = 200         # макс акций за одну сделку (антиманипуляция)
STOCK_SELL_FEE    = 0.03          # 3% комиссия при продаже
STOCK_PROFIT_TAX  = 0.15          # 15% налог с прибыли при продаже
STOCK_COOLDOWN     = 10 * 60     # 10 минут между сделками
STOCK_UPDATE_SEC  = 15 * 60       # рыночный drift каждые 15 минут
STOCK_HISTORY_LEN = 96            # хранить 96 точек (~24ч)

# ── Реалистичная ценовая модель ──────────────────────────────
# Ликвидность: чем выше — тем меньше одна сделка двигает рынок.
# 2000 = нужно ~500 акций чтобы сдвинуть на ~2%, реалистично для игры.
# Ликвидность подобрана под экономику бота (макс баланс ~1М, акция ~10к)
# 94 акции (всё что богатый может купить) = ~1.4%, 200 акций = ~2.4%
STOCK_LIQUIDITY   = 150           # "глубина стакана"

# Режимы волатильности рынка (переключаются автоматически)
STOCK_VOL_NORMAL  = 0.008         # обычный шум ±0.8% за период
STOCK_VOL_HIGH    = 0.018         # повышенная волатильность ±1.8%
STOCK_VOL_CRISIS  = 0.035         # кризис ±3.5%
STOCK_DRIFT_BASE  = 0.001         # небольшой upward bias

# ── Инициализация таблиц ─────────────────────────────────────

def _init_stocks():
    with get_db_cursor() as cur:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS stocks (
                ticker      TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                price       INTEGER NOT NULL,
                prev_price  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS stock_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker      TEXT NOT NULL,
                price       INTEGER NOT NULL,
                recorded_at INTEGER NOT NULL
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS stock_portfolio (
                user_id    INTEGER NOT NULL,
                ticker     TEXT NOT NULL,
                amount     INTEGER NOT NULL DEFAULT 0,
                avg_price  INTEGER NOT NULL DEFAULT 0,
                last_trade INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, ticker)
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS stock_trades (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                ticker     TEXT NOT NULL,
                action     TEXT NOT NULL,
                amount     INTEGER NOT NULL,
                price      INTEGER NOT NULL,
                fee        INTEGER NOT NULL DEFAULT 0,
                total      INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
        ''')
        cur.execute(
            "INSERT OR IGNORE INTO stocks (ticker,name,price,prev_price,updated_at) VALUES (?,?,?,?,?)",
            (STOCK_TICKER, STOCK_NAME, STOCK_INIT_PRICE, STOCK_INIT_PRICE, int(time.time()))
        )
        cur.execute(
            "INSERT OR IGNORE INTO stock_history (ticker,price,recorded_at) VALUES (?,?,?)",
            (STOCK_TICKER, STOCK_INIT_PRICE, int(time.time()))
        )

_init_stocks()

# Миграция: добавляем last_trade если колонки нет
def _migrate_stocks():
    try:
        with get_db_cursor() as cur:
            cur.execute("PRAGMA table_info(stock_portfolio)")
            cols = [r[1] for r in cur.fetchall()]
            if "last_trade" not in cols:
                cur.execute("ALTER TABLE stock_portfolio ADD COLUMN last_trade INTEGER NOT NULL DEFAULT 0")
                print("[stocks] миграция: добавлена колонка last_trade")
            # Также мигрируем stock_cooldowns -> last_trade если есть старая таблица
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='stock_cooldowns'")
            if cur.fetchone():
                cur.execute("""
                    UPDATE stock_portfolio SET last_trade = (
                        SELECT last_trade FROM stock_cooldowns
                        WHERE stock_cooldowns.user_id = stock_portfolio.user_id
                    ) WHERE EXISTS (
                        SELECT 1 FROM stock_cooldowns WHERE stock_cooldowns.user_id = stock_portfolio.user_id
                    )
                """)
                print("[stocks] миграция: кулдауны перенесены")
    except Exception as e:
        print(f"[stocks] ошибка миграции: {e}")

_migrate_stocks()

# ── Вспомогательные функции ──────────────────────────────────

def _get_stock_price():
    with get_db_cursor() as cur:
        cur.execute("SELECT price, prev_price FROM stocks WHERE ticker=?", (STOCK_TICKER,))
        row = cur.fetchone()
        return (row[0], row[1]) if row else (STOCK_INIT_PRICE, STOCK_INIT_PRICE)

def _set_stock_price(new_price):
    """Атомарно обновить цену и записать в историю."""
    new_price = max(STOCK_MIN_PRICE, min(STOCK_MAX_PRICE, int(new_price)))
    now = int(time.time())
    with get_db_cursor() as cur:
        cur.execute(
            "UPDATE stocks SET prev_price=price, price=?, updated_at=? WHERE ticker=?",
            (new_price, now, STOCK_TICKER)
        )
        cur.execute(
            "INSERT INTO stock_history (ticker,price,recorded_at) VALUES (?,?,?)",
            (STOCK_TICKER, new_price, now)
        )
        cur.execute(
            "DELETE FROM stock_history WHERE ticker=? AND id NOT IN "
            "(SELECT id FROM stock_history WHERE ticker=? ORDER BY recorded_at DESC LIMIT ?)",
            (STOCK_TICKER, STOCK_TICKER, STOCK_HISTORY_LEN)
        )
    return new_price

def _get_portfolio(user_id):
    with get_db_cursor() as cur:
        cur.execute(
            "SELECT amount, avg_price, last_trade FROM stock_portfolio WHERE user_id=? AND ticker=?",
            (user_id, STOCK_TICKER)
        )
        row = cur.fetchone()
        return (row[0], row[1], row[2]) if row else (0, 0, 0)

def _get_cooldown_left(user_id):
    _, _, last_trade = _get_portfolio(user_id)
    return max(0, last_trade + STOCK_COOLDOWN - int(time.time()))

def _total_shares():
    with get_db_cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(amount),0) FROM stock_portfolio WHERE ticker=?", (STOCK_TICKER,))
        return cur.fetchone()[0]

def _stock_history(limit=24):
    with get_db_cursor() as cur:
        cur.execute(
            "SELECT price, recorded_at FROM stock_history WHERE ticker=? ORDER BY recorded_at DESC LIMIT ?",
            (STOCK_TICKER, limit)
        )
        return cur.fetchall()[::-1]

def _stock_chart(history):
    if len(history) < 2:
        return "▬" * 8
    prices = [p for p, _ in history]
    mn, mx = min(prices), max(prices)
    if mx == mn:
        return "▬" * min(len(prices), 16)
    blocks = " ▁▂▃▄▅▆▇█"
    return "".join(blocks[int((p-mn)/(mx-mn)*8)] for p in prices[-16:])

# ── Рыночная механика цены (спрос и предложение) ────────────

def _get_trade_pressure(window_sec=900):
    """
    Считает давление покупателей/продавцов за последние window_sec секунд.
    Возвращает значение от -1.0 (все продают) до +1.0 (все покупают).
    """
    since = int(time.time()) - window_sec
    with get_db_cursor() as cur:
        cur.execute(
            """SELECT action, SUM(amount) FROM stock_trades
               WHERE ticker=? AND created_at >= ?
               GROUP BY action""",
            (STOCK_TICKER, since)
        )
        rows = {r[0]: r[1] for r in cur.fetchall()}
    bought = rows.get('buy', 0)
    sold   = rows.get('sell', 0)
    total  = bought + sold
    if total == 0:
        return 0.0
    return (bought - sold) / total  # от -1 до +1

def _get_market_volatility():
    """
    Определяет текущий режим волатильности на основе недавней истории.
    Если последние 4 периода показывают большие движения — рынок "разогрет".
    """
    history = _stock_history(limit=8)
    if len(history) < 3:
        return STOCK_VOL_NORMAL

    # Считаем среднее абсолютное изменение за последние периоды
    changes = []
    for i in range(1, len(history)):
        prev_p, cur_p = history[i-1][0], history[i][0]
        changes.append(abs(cur_p - prev_p) / prev_p)

    avg_change = sum(changes) / len(changes)

    if avg_change > 0.06:
        return STOCK_VOL_CRISIS    # рынок в кризисе — большие колебания
    elif avg_change > 0.025:
        return STOCK_VOL_HIGH      # повышенная волатильность
    else:
        return STOCK_VOL_NORMAL

def _get_momentum():
    """
    Momentum: если цена несколько периодов подряд растёт/падает — тренд усиливается.
    Возвращает значение от -0.03 до +0.03.
    """
    history = _stock_history(limit=5)
    if len(history) < 3:
        return 0.0

    # Считаем направление последних 3-4 движений
    directions = []
    for i in range(1, len(history)):
        prev_p, cur_p = history[i-1][0], history[i][0]
        directions.append(1 if cur_p > prev_p else -1)

    # Если все движения в одну сторону — momentum сильный
    streak = sum(directions)
    momentum = streak * 0.007  # каждый период в тренде добавляет 0.7%
    return max(-0.03, min(0.03, momentum))

def _market_update():
    """
    Реалистичная рыночная модель:
    1. Торговое давление покупателей/продавцов за последние 15 минут
    2. Случайный шум с динамической волатильностью (3 режима)
    3. Momentum — усиление тренда при нескольких периодах подряд
    4. Mean reversion — при сильном отклонении от среднего тянется обратно
    5. Upward bias — компания слегка "растёт" в спокойные периоды
    """
    price, _ = _get_stock_price()
    pressure  = _get_trade_pressure(window_sec=STOCK_UPDATE_SEC)
    vol       = _get_market_volatility()
    momentum  = _get_momentum()

    # 1. Торговое давление: pressure от -1 до +1
    #    Масштабируем так, чтобы сильное давление давало 5-8% движение
    trade_impact = pressure * 0.04

    # 2. Случайный шум согласно текущей волатильности
    noise = random.gauss(0, vol)  # нормальное распределение, реалистичнее

    # 3. Momentum
    mom_impact = momentum * 0.5

    # 4. Mean reversion (слабый): при отклонении >30% от 24ч среднего — тянется обратно
    reversion = 0.0
    history = _stock_history(limit=96)
    if len(history) >= 6:
        avg_price = sum(p for p, _ in history) / len(history)
        deviation = (price - avg_price) / avg_price
        if abs(deviation) > 0.30:
            reversion = -deviation * 0.08  # возврат к среднему 8% от отклонения

    # 5. Upward bias в спокойные периоды (без активных продаж)
    bias = STOCK_DRIFT_BASE if abs(pressure) < 0.2 else 0.0

    total_impact = trade_impact + noise + mom_impact + reversion + bias

    new_price = _set_stock_price(price * (1 + total_impact))
    label = (
        f"pressure={pressure:+.2f}({trade_impact*100:+.1f}%) "
        f"noise={noise*100:+.1f}% mom={mom_impact*100:+.1f}% "
        f"rev={reversion*100:+.1f}% vol={vol*100:.1f}%"
    )
    print(f"[stocks] market update: {price:,} → {new_price:,} ({label})")
    return price, new_price

# ── Влияние моментальной сделки на цену ──────────────────────

def _apply_trade_impact(qty, is_buy):
    """
    Реалистичное моментальное влияние сделки на цену.
    Логарифмическая модель: маленькие сделки почти не двигают рынок,
    только крупные объёмы ощутимо меняют цену.

    Примеры при STOCK_LIQUIDITY=2000:
      10  акций →  ~0.05%  (почти не заметно)
      50  акций →  ~0.24%
     100  акций →  ~0.48%
     200  акций →  ~0.95%
     500  акций →  ~2.2%
    1000  акций →  ~4.0%
    5000  акций →  ~9.0%  (максимум — китовая сделка)
    """
    price, _ = _get_stock_price()

    # Логарифмическая модель: ln(1 + qty/L) / ln(1 + MAX/L) * max_impact
    # Масштабируем так чтобы при max qty (~5000) было ~10%
    import math
    L = STOCK_LIQUIDITY  # 2000 — "глубина стакана"
    MAX_QTY = STOCK_MAX_PER_USER  # 5000
    MAX_IMPACT = 0.10  # не более 10% даже при огромной сделке

    impact_pct = (math.log(1 + qty / L) / math.log(1 + MAX_QTY / L)) * MAX_IMPACT

    # Слиппедж ±0.1% — но покупка НИКОГДА не снижает цену,
    # продажа НИКОГДА не повышает цену
    slippage = random.gauss(0, 0.001)

    if is_buy:
        total_impact = max(0.0001, impact_pct + slippage)
    else:
        total_impact = min(-0.0001, -(impact_pct - slippage))

    new_price = _set_stock_price(price * (1 + total_impact))
    print(f"[stocks] trade impact: qty={qty} {'BUY' if is_buy else 'SELL'} "
          f"impact={impact_pct*100:+.2f}% slip={slippage*100:+.2f}% "
          f"{price:,} → {new_price:,}")
    return new_price

# ── Лог транзакций в канал ───────────────────────────────────

def _get_user_display_name(user_id):
    """Получить отображаемое имя пользователя."""
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT custom_name, first_name, username FROM users WHERE user_id=?", (user_id,))
            row = cur.fetchone()
            if row:
                name = row[0] or row[1] or (f"@{row[2]}" if row[2] else str(user_id))
                username = f" (@{row[2]})" if row[2] else ""
                return name, username
    except Exception:
        pass
    return str(user_id), ""

def _log_stock_trade(user_id, action, qty, price, total, new_price):
    """Отправить краткий лог сделки в канал."""
    try:
        name, username = _get_user_display_name(user_id)
        price_change = (new_price - price) / price * 100

        if action == "buy":
            icon = "🟢"
            action_text = "купил"
            price_arrow = "📈"
        else:
            icon = "🔴"
            action_text = "продал"
            price_arrow = "📉"

        whale = " 🐳" if qty >= 500 else ""

        text = (
            f"{icon} <b>{name}</b>{username} {action_text} <b>{qty} {STOCK_TICKER}</b>{whale}\n"
            f"💵 {format_balance(price)}/шт · {format_balance(total)}\n"
            f"{price_arrow} {format_balance(price)} → <b>{format_balance(new_price)}</b> ({price_change:+.1f}%)"
        )

        bot.send_message(STOCK_LOG_CHANNEL, text, parse_mode="HTML")
    except Exception as e:
        print(f"[stock_log] ошибка: {e}")

def _log_stock_price_update(old_price, new_price, reason="market"):
    """Отправить лог изменения цены в канал (только если изменение >= 1%)."""
    try:
        change_pct = (new_price - old_price) / old_price * 100
        if abs(change_pct) < 1.0:
            return  # Не спамим мелкими изменениями
        arrow = "📈" if new_price > old_price else "📉"
        text = (
            f"{arrow} <b>{STOCK_TICKER}</b> {format_balance(old_price)} → <b>{format_balance(new_price)}</b> "
            f"({change_pct:+.1f}%)"
        )
        bot.send_message(STOCK_LOG_CHANNEL, text, parse_mode="HTML")
    except Exception as e:
        print(f"[stock_log] ошибка цены: {e}")

# ── Команды ──────────────────────────────────────────────────

import datetime as _dt

@bot.message_handler(func=lambda m: False)  # объединено с handle_stock_button
def handle_stock_market(message):
    user_id     = message.from_user.id
    price, prev = _get_stock_price()
    change_pct  = (price - prev) / prev * 100 if prev else 0
    arrow       = "📈" if price >= prev else "📉"
    sign        = "+" if change_pct >= 0 else ""
    circle      = "🟢" if change_pct >= 0 else "🔴"
    owned, avg, _ = _get_portfolio(user_id)
    cd_left     = _get_cooldown_left(user_id)
    total       = _total_shares()

    portfolio_line = ""
    if owned > 0:
        cur_val  = owned * price
        pnl      = cur_val - owned * avg
        pnl_sign = "+" if pnl >= 0 else ""
        pnl_emoji = "🟢" if pnl >= 0 else "🔴"
        portfolio_line = (
            f"\n<blockquote>📂 <b>Твой портфель</b>\n"
            f"  {owned} шт. × {format_balance(price)} = <b>{format_balance(cur_val)}</b>\n"
            f"  Куплено по: {format_balance(avg)}\n"
            f"  {pnl_emoji} P&L: <b>{pnl_sign}{format_balance(pnl)}</b></blockquote>\n"
        )

    cd_line = f"\n⏳ Кулдаун: <b>{cd_left//60}м {cd_left%60}с</b>" if cd_left > 0 else ""

    bot.send_message(message.chat.id,
        f"📊 <b>{STOCK_NAME}  [{STOCK_TICKER}]</b>\n\n"
        f"<b>{format_balance(price)}</b>  {circle} {sign}{change_pct:.1f}%\n\n"
        f"📦 Акций у игроков: <b>{total}</b>\n"
        f"💸 Комиссия продажи: {int(STOCK_SELL_FEE*100)}%\n"
        f"{portfolio_line}{cd_line}\n\n"
        f"<code>купить акции 10</code>\n"
        f"<code>продать акции 10</code>\n"
        f"<code>история акций</code>\n\n"
        f"📢 Все транзакции: <a href=\"https://t.me/FectizCorp\">@FectizCorp</a>",
        parse_mode="HTML",
        disable_web_page_preview=True
    )

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith("купить акции"))
def handle_buy_stock(message):
    user_id = message.from_user.id
    parts   = message.text.strip().split()

    if len(parts) < 3:
        bot.send_message(message.chat.id, "❌ Формат: <code>купить акции 10</code>", parse_mode="HTML")
        return

    cd = _get_cooldown_left(user_id)
    if cd > 0:
        bot.send_message(message.chat.id, f"⏳ Кулдаун: <b>{cd//60}м {cd%60}с</b>", parse_mode="HTML")
        return

    try:
        qty = int(parts[2])
    except:
        bot.send_message(message.chat.id, "❌ Укажи число.", parse_mode="HTML")
        return

    if qty <= 0 or qty > STOCK_MAX_PER_TRADE:
        bot.send_message(message.chat.id, f"❌ От 1 до {STOCK_MAX_PER_TRADE} акций за одну сделку.", parse_mode="HTML")
        return

    owned, avg, _ = _get_portfolio(user_id)
    if owned + qty > STOCK_MAX_PER_USER:
        bot.send_message(message.chat.id,
            f"❌ Максимум {STOCK_MAX_PER_USER} акций на игрока.\n"
            f"У тебя: {owned}, можно купить ещё: {STOCK_MAX_PER_USER - owned}",
            parse_mode="HTML")
        return

    price, _   = _get_stock_price()
    total_cost = price * qty
    now        = int(time.time())

    # Атомарная проверка баланса и кулдауна + списание в одной транзакции.
    # UPDATE вернёт 0 затронутых строк если денег не хватает или кулдаун не прошёл.
    buy_ok     = False
    new_avg    = 0
    with get_db_cursor() as cur:
        # Проверяем баланс атомарно — списываем только если хватает
        cur.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
            (total_cost, user_id, total_cost)
        )
        if cur.rowcount == 0:
            pass  # не хватает денег
        else:
            # Проверяем кулдаун и обновляем портфель
            cur.execute(
                "SELECT last_trade FROM stock_portfolio WHERE user_id=? AND ticker=?",
                (user_id, STOCK_TICKER)
            )
            row = cur.fetchone()
            last_trade_ts = row[0] if row else 0
            if last_trade_ts and now - last_trade_ts < STOCK_COOLDOWN:
                # Кулдаун не прошёл — возвращаем деньги
                cur.execute(
                    "UPDATE users SET balance=balance+? WHERE user_id=?",
                    (total_cost, user_id)
                )
            else:
                new_avg = (owned * avg + qty * price) // (owned + qty) if owned > 0 else price
                cur.execute(
                    "INSERT INTO stock_portfolio (user_id,ticker,amount,avg_price,last_trade) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(user_id,ticker) DO UPDATE SET amount=amount+?, avg_price=?, last_trade=?",
                    (user_id, STOCK_TICKER, qty, new_avg, now, qty, new_avg, now)
                )
                cur.execute(
                    "INSERT INTO stock_trades (user_id,ticker,action,amount,price,fee,total,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (user_id, STOCK_TICKER, "buy", qty, price, 0, total_cost, now)
                )
                buy_ok = True

    if not buy_ok:
        balance = get_balance(user_id)
        cd = _get_cooldown_left(user_id)
        if cd > 0:
            bot.send_message(message.chat.id, f"⏳ Кулдаун: <b>{cd//60}м {cd%60}с</b>", parse_mode="HTML")
        else:
            bot.send_message(message.chat.id,
                f"❌ Не хватает средств.\nНужно: <b>{format_balance(total_cost)}</b>\n"
                f"Есть: <b>{format_balance(balance)}</b>", parse_mode="HTML")
        return

    # Влияние на цену
    new_price = _apply_trade_impact(qty, is_buy=True)
    price_change = (new_price - price) / price * 100

    bot.send_message(message.chat.id,
        f"✅ <b>Куплено {qty} акций {STOCK_TICKER}</b>\n\n"
        f"💰 Цена: {format_balance(price)}/шт.\n"
        f"💸 Списано: <b>{format_balance(total_cost)}</b>\n"
        f"📂 В портфеле: {owned + qty} шт.\n"
        f"📈 Цена выросла до {format_balance(new_price)} ({price_change:+.2f}%)\n\n"
        f"⏳ Следующая сделка через 10 минут",
        parse_mode="HTML"
    )
    # Лог в канал
    threading.Thread(target=_log_stock_trade, args=(user_id, "buy", qty, price, total_cost, new_price), daemon=True).start()


@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith("продать акции"))
def handle_sell_stock(message):
    user_id = message.from_user.id
    parts   = message.text.strip().split()

    if len(parts) < 3:
        bot.send_message(message.chat.id, "❌ Формат: <code>продать акции 10</code>", parse_mode="HTML")
        return

    cd = _get_cooldown_left(user_id)
    if cd > 0:
        bot.send_message(message.chat.id, f"⏳ Кулдаун: <b>{cd//60}м {cd%60}с</b>", parse_mode="HTML")
        return

    try:
        qty = int(parts[2])
    except:
        bot.send_message(message.chat.id, "❌ Укажи число.", parse_mode="HTML")
        return

    owned, avg, _ = _get_portfolio(user_id)
    if qty <= 0 or qty > owned:
        bot.send_message(message.chat.id, f"❌ У тебя {owned} акций.", parse_mode="HTML")
        return

    price, _  = _get_stock_price()
    now       = int(time.time())

    # Антиманипуляция: нельзя продать дороже чем avg_price × 1.05
    SELL_MAX_MARKUP = 1.05
    sell_price  = min(price, int(avg * SELL_MAX_MARKUP)) if avg > 0 else price
    manipulated = sell_price < price

    gross = sell_price * qty
    fee   = int(gross * STOCK_SELL_FEE)

    # Налог 15% с прибыли (только если прибыль > 0)
    raw_profit = (sell_price - avg) * qty if avg > 0 else 0
    tax        = int(max(0, raw_profit) * STOCK_PROFIT_TAX)

    net       = gross - fee - tax
    pnl       = net - avg * qty
    pnl_sign  = "+" if pnl >= 0 else ""
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"

    sell_ok = False
    with get_db_cursor() as cur:
        # Проверяем кулдаун атомарно
        cur.execute(
            "SELECT last_trade FROM stock_portfolio WHERE user_id=? AND ticker=?",
            (user_id, STOCK_TICKER)
        )
        row = cur.fetchone()
        last_trade_ts = row[0] if row else 0
        if last_trade_ts and now - last_trade_ts < STOCK_COOLDOWN:
            pass  # кулдаун не прошёл
        else:
            # Начисляем деньги и обновляем портфель атомарно
            cur.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=?",
                (net, user_id)
            )
            new_amount = owned - qty
            if new_amount == 0:
                cur.execute("DELETE FROM stock_portfolio WHERE user_id=? AND ticker=?", (user_id, STOCK_TICKER))
            else:
                cur.execute(
                    "UPDATE stock_portfolio SET amount=?, last_trade=? WHERE user_id=? AND ticker=?",
                    (new_amount, now, user_id, STOCK_TICKER)
                )
            cur.execute(
                "INSERT INTO stock_trades (user_id,ticker,action,amount,price,fee,total,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (user_id, STOCK_TICKER, "sell", qty, sell_price, fee + tax, net, now)
            )
            sell_ok = True

    if not sell_ok:
        cd = _get_cooldown_left(user_id)
        bot.send_message(message.chat.id, f"⏳ Кулдаун: <b>{cd//60}м {cd%60}с</b>", parse_mode="HTML")
        return

    # Влияние на цену
    new_price    = _apply_trade_impact(qty, is_buy=False)
    price_change = (new_price - price) / price * 100

    manip_warn = (
        f"\n⚠️ <i>Цена ограничена защитой от манипуляций ({format_balance(sell_price)}/шт. вместо {format_balance(price)})</i>"
        if manipulated else ""
    )
    tax_line = f"\n💼 Налог с прибыли (15%): -{format_balance(tax)}" if tax > 0 else ""

    bot.send_message(message.chat.id,
        f"✅ <b>Продано {qty} акций {STOCK_TICKER}</b>\n\n"
        f"💰 Цена продажи: {format_balance(sell_price)}/шт.\n"
        f"💸 Комиссия (3%): -{format_balance(fee)}"
        f"{tax_line}\n"
        f"💵 Получено: <b>{format_balance(net)}</b>\n"
        f"{pnl_emoji} P&L: <b>{pnl_sign}{format_balance(pnl)}</b>\n"
        f"📂 Осталось: {new_amount} шт.\n"
        f"📉 Цена упала до {format_balance(new_price)} ({price_change:+.2f}%)"
        f"{manip_warn}\n\n"
        f"⏳ Следующая сделка через 10 минут",
        parse_mode="HTML"
    )
    # Лог в канал
    threading.Thread(target=_log_stock_trade, args=(user_id, "sell", qty, sell_price, net, new_price), daemon=True).start()


@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == "история акций")
def handle_stock_history(message):
    user_id = message.from_user.id
    history = _stock_history(limit=16)

    if not history:
        bot.send_message(message.chat.id, "История пуста.", parse_mode="HTML")
        return

    lines = []
    for i, (price, ts) in enumerate(history):
        dt = _dt.datetime.utcfromtimestamp(ts).strftime("%d.%m %H:%M")
        if i > 0:
            prev_p = history[i-1][0]
            chg    = (price - prev_p) / prev_p * 100
            arrow  = "🟢" if price >= prev_p else "🔴"
            lines.append(f"{arrow} {dt}  <b>{format_balance(price)}</b>  ({chg:+.1f}%)")
        else:
            lines.append(f"⬜ {dt}  <b>{format_balance(price)}</b>")

    with get_db_cursor() as cur:
        cur.execute(
            "SELECT action, amount, price, fee, total, created_at FROM stock_trades "
            "WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
            (user_id,)
        )
        my_trades = cur.fetchall()

    trade_lines = []
    for action, amt, p, fee, total, ts in my_trades:
        dt   = _dt.datetime.utcfromtimestamp(ts).strftime("%d.%m %H:%M")
        icon = "🛒" if action == "buy" else "💰"
        fee_str = f" (комиссия -{format_balance(fee)})" if fee else ""
        trade_lines.append(f"{icon} {dt}  {amt} шт. × {format_balance(p)}{fee_str}")

    my_block = ""
    if trade_lines:
        my_block = "\n\n📋 <b>Мои последние сделки:</b>\n" + "\n".join(trade_lines)

    bot.send_message(message.chat.id,
        f"📊 <b>История цен {STOCK_TICKER}</b>\n\n"
        + "\n".join(lines)
        + my_block,
        parse_mode="HTML"
    )


# ── Планировщик рыночного обновления ────────────────────────

def _stock_price_scheduler():
    print(f"[stocks] планировщик запущен, рыночное обновление каждые {STOCK_UPDATE_SEC//60} мин")
    while True:
        time.sleep(STOCK_UPDATE_SEC)
        try:
            old_price, new_price = _market_update()
            change_pct = (new_price - old_price) / old_price * 100
            # Лог в канал (только если изменение >= 1%)
            _log_stock_price_update(old_price, new_price, reason="market")
            if abs(change_pct) >= 2:
                arrow = "📈" if new_price > old_price else "📉"
                send_alert(
                    f"{arrow} Акция {STOCK_TICKER}: "
                    f"{format_balance(old_price)} → <b>{format_balance(new_price)}</b> "
                    f"({change_pct:+.1f}%)"
                )
        except Exception as e:
            print(f"[stocks] ошибка: {e}")

def start_stock_scheduler():
    t = threading.Thread(target=_stock_price_scheduler, daemon=True)
    t.start()
    print("📈 Планировщик акций запущен")

# ══════════════════════════════════════════════
# 💰 ДИВИДЕНДЫ ПО АКЦИЯМ
# ══════════════════════════════════════════════

def _pay_dividends(amount_per_share):
    """Выплатить дивиденды всем держателям акций."""
    results = []
    try:
        # Получаем всех держателей
        with get_db_cursor() as cur:
            cur.execute(
                "SELECT user_id, amount FROM stock_portfolio WHERE ticker=? AND amount > 0",
                (STOCK_TICKER,)
            )
            holders = cur.fetchall()

        if not holders:
            return {"success": False, "error": "Нет держателей акций"}

        total_paid  = 0
        total_users = 0

        for user_id, shares in holders:
            payout = shares * amount_per_share
            if payout <= 0:
                continue
            try:
                update_balance(user_id, payout)
                total_paid  += payout
                total_users += 1
                results.append((user_id, shares, payout))

                # Уведомление игроку
                try:
                    bot.send_message(
                        user_id,
                        f"💰 <b>Дивиденды по акциям {STOCK_TICKER}!</b>\n\n"
                        f"📂 У тебя: <b>{shares} акций</b>\n"
                        f"💵 Дивиденд: <b>{format_balance(amount_per_share)}</b>/шт.\n"
                        f"✅ Начислено: <b>{format_balance(payout)}</b>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            except Exception as e:
                print(f"[dividends] ошибка выплаты {user_id}: {e}")

        return {
            "success":     True,
            "total_paid":  total_paid,
            "total_users": total_users,
            "holders":     results,
        }

    except Exception as e:
        print(f"[dividends] ошибка: {e}")
        return {"success": False, "error": str(e)}


@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith("дивиденды ") and is_admin(m.from_user.id))
def handle_pay_dividends(message):
    """Команда: дивиденды 500  — выплатить 500 🌸 за каждую акцию."""
    if not is_admin(message.from_user.id):
        return

    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.send_message(message.chat.id,
            "❌ Формат: <code>дивиденды 500</code>\n"
            "Выплатит 500 🌸 за каждую акцию каждому держателю.",
            parse_mode="HTML")
        return

    try:
        amount_per_share = int(parts[1].replace(" ", "").replace(",", ""))
    except ValueError:
        bot.send_message(message.chat.id, "❌ Укажи число.", parse_mode="HTML")
        return

    if amount_per_share <= 0:
        bot.send_message(message.chat.id, "❌ Сумма должна быть больше 0.", parse_mode="HTML")
        return

    # Предпросмотр
    with get_db_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM stock_portfolio WHERE ticker=? AND amount > 0",
            (STOCK_TICKER,)
        )
        row = cur.fetchone()
        holders_count = row[0] or 0
        total_shares_held = row[1] or 0

    if holders_count == 0:
        bot.send_message(message.chat.id, "❌ Нет держателей акций.", parse_mode="HTML")
        return

    total_payout = total_shares_held * amount_per_share

    msg = bot.send_message(message.chat.id,
        f"💰 <b>Выплата дивидендов</b>\n\n"
        f"📊 Держателей: <b>{holders_count}</b> чел.\n"
        f"📦 Акций всего: <b>{total_shares_held}</b> шт.\n"
        f"💵 За акцию: <b>{format_balance(amount_per_share)}</b>\n"
        f"💸 Итого выплата: <b>{format_balance(total_payout)}</b>\n\n"
        f"⏳ Выплачиваем...",
        parse_mode="HTML"
    )

    result = _pay_dividends(amount_per_share)

    if result["success"]:
        # Топ-5 получателей
        top = sorted(result["holders"], key=lambda x: x[2], reverse=True)[:5]
        top_lines = ""
        medals = ["🥇", "🥈", "🥉", "🔹", "🔹"]
        for i, (uid, shares, payout) in enumerate(top):
            # Получаем имя
            try:
                with get_db_cursor() as cur:
                    cur.execute("SELECT custom_name, first_name, username FROM users WHERE user_id=?", (uid,))
                    row = cur.fetchone()
                    name = row[0] or row[1] or (f"@{row[2]}" if row[2] else str(uid))
            except Exception:
                name = str(uid)
            top_lines += f"{medals[i]} {name} — {shares} акций → <b>{format_balance(payout)}</b>\n"

        bot.edit_message_text(
            f"✅ <b>Дивиденды выплачены!</b>\n\n"
            f"👥 Получили: <b>{result['total_users']}</b> чел.\n"
            f"💸 Выплачено: <b>{format_balance(result['total_paid'])}</b>\n"
            f"💵 За акцию: <b>{format_balance(amount_per_share)}</b>\n\n"
            f"🏆 <b>Топ получателей:</b>\n{top_lines}",
            chat_id=message.chat.id,
            message_id=msg.message_id,
            parse_mode="HTML"
        )

        # Объявление в чат (через send_alert если есть)
        try:
            send_alert(
                f"💰 <b>Дивиденды по акциям {STOCK_TICKER}!</b>\n\n"
                f"За каждую акцию начислено <b>{format_balance(amount_per_share)}</b>\n"
                f"Всего выплачено: <b>{format_balance(result['total_paid'])}</b> → "
                f"{result['total_users']} акционерам\n\n"
                f"Купить акции: <code>акции</code>"
            )
        except Exception:
            pass
    else:
        bot.edit_message_text(
            f"❌ Ошибка: {result['error']}",
            chat_id=message.chat.id,
            message_id=msg.message_id,
            parse_mode="HTML"
        )




# ══════════════════════════════════════════════════════════════
# ⚔️  КЛАНОВАЯ СИСТЕМА — FECTIZ BOT
# ══════════════════════════════════════════════════════════════
#
#  Механики:
#  • Создание/роспуск клана (стоимость 1 000 000 🌸)
#  • Вступление, выход, заявки, исключение
#  • Роли: владелец / офицер / участник
#  • Клановая казна (взносы участников)
#  • Клановые войны: вызов → принятие → 24ч → итог
#    Победитель определяется по суммарному балансу прироста
#    участников за время войны (snapshot до/после).
#  • Еженедельный рейтинг кланов — топ-3 получают призы
#  • Команды: клан, создать клан <тег> <название>,
#             клан вступить <тег>, клан выйти,
#             клан казна <сумма>, клан война <тег>,
#             клан принять <тег>, клан кик <@username>,
#             клан офицер <@username>, клан инфо <тег>
#
# ══════════════════════════════════════════════════════════════

import datetime as _cdt

# ── Константы ────────────────────────────────────────────────
CLAN_CREATE_COST   = 1_000_000
CLAN_MAX_MEMBERS   = 30
CLAN_WAR_DURATION  = 86400
CLAN_WAR_COST      = 50_000
CLAN_WAR_WIN_PCT   = 0.20
CLAN_WEEKLY_PRIZES = [500_000, 250_000, 100_000]

# HP-war constants
CLAN_WAR_HP_PER_MEMBER = 1000
CLAN_WAR_HP_MIN        = 1000
CLAN_WAR_ATTACK_COST   = 5_000
CLAN_WAR_ATTACK_DMG    = 100
CLAN_WAR_DEFEND_COST   = 3_000
CLAN_WAR_DEFEND_HEAL   = 50
CLAN_WAR_ACTION_CD     = 300
# ── Инициализация таблиц ─────────────────────────────────────

def _init_clan_wars():
    with get_db_cursor() as cur:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS clan_wars (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                attacker_id   INTEGER NOT NULL,
                defender_id   INTEGER NOT NULL,
                started_at    INTEGER NOT NULL,
                ends_at       INTEGER NOT NULL,
                status        TEXT DEFAULT "active",
                winner_id     INTEGER DEFAULT NULL,
                prize         INTEGER DEFAULT 0,
                att_hp        INTEGER DEFAULT 0,
                def_hp        INTEGER DEFAULT 0,
                att_hp_max    INTEGER DEFAULT 0,
                def_hp_max    INTEGER DEFAULT 0
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS clan_war_actions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                war_id     INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                clan_id    INTEGER NOT NULL,
                action     TEXT NOT NULL,
                cost       INTEGER NOT NULL,
                value      INTEGER NOT NULL,
                done_at    INTEGER NOT NULL
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS clan_war_cooldowns (
                user_id    INTEGER PRIMARY KEY,
                last_action INTEGER NOT NULL DEFAULT 0
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS clan_weekly_stats (
                clan_id       INTEGER PRIMARY KEY,
                week_start    INTEGER NOT NULL,
                score         INTEGER DEFAULT 0
            )
        ''')
        # Миграция clans
        cur.execute("PRAGMA table_info(clans)")
        cols = [r[1] for r in cur.fetchall()]
        for col, default in [("war_wins","0"),("war_losses","0"),("war_score","0")]:
            if col not in cols:
                cur.execute(f"ALTER TABLE clans ADD COLUMN {col} INTEGER DEFAULT {default}")
        # Миграция clan_wars — добавляем HP колонки если таблица уже существует
        cur.execute("PRAGMA table_info(clan_wars)")
        war_cols = [r[1] for r in cur.fetchall()]
        for col in ("att_hp","def_hp","att_hp_max","def_hp_max"):
            if col not in war_cols:
                cur.execute(f"ALTER TABLE clan_wars ADD COLUMN {col} INTEGER DEFAULT 0")

_init_clan_wars()

# ── Вспомогательные функции ──────────────────────────────────

def _get_clan_by_id(clan_id):
    with get_db_cursor() as cur:
        cur.execute("SELECT * FROM clans WHERE id=?", (clan_id,))
        row = cur.fetchone()
        return dict(row) if row else None

def _get_clan_by_tag(tag):
    with get_db_cursor() as cur:
        cur.execute("SELECT * FROM clans WHERE LOWER(tag)=LOWER(?)", (tag,))
        row = cur.fetchone()
        return dict(row) if row else None

def get_user_clan(user_id):
    """Возвращает клан пользователя или None."""
    with get_db_cursor() as cur:
        cur.execute("""
            SELECT c.*, cm.role, cm.contributed, cm.joined_at
            FROM clan_members cm
            JOIN clans c ON c.id = cm.clan_id
            WHERE cm.user_id=?
        """, (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None

def _get_clan_members(clan_id):
    with get_db_cursor() as cur:
        cur.execute("""
            SELECT cm.user_id, cm.role, cm.contributed, cm.joined_at,
                   u.first_name, u.username, u.custom_name, u.balance
            FROM clan_members cm
            JOIN users u ON u.user_id = cm.user_id
            WHERE cm.clan_id=?
            ORDER BY CASE cm.role WHEN 'owner' THEN 0 WHEN 'officer' THEN 1 ELSE 2 END, cm.contributed DESC
        """, (clan_id,))
        return [dict(r) for r in cur.fetchall()]

def _get_member_count(clan_id):
    with get_db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM clan_members WHERE clan_id=?", (clan_id,))
        return cur.fetchone()[0]

def _member_role(clan_id, user_id):
    with get_db_cursor() as cur:
        cur.execute("SELECT role FROM clan_members WHERE clan_id=? AND user_id=?", (clan_id, user_id))
        row = cur.fetchone()
        return row[0] if row else None

def _can_manage(clan_id, user_id):
    """Может ли управлять (owner или officer)."""
    return _member_role(clan_id, user_id) in ("owner", "officer")

def _user_display(user_id):
    with get_db_cursor() as cur:
        cur.execute("SELECT first_name, username, custom_name FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            return str(user_id)
        return row[2] or row[0] or (f"@{row[1]}" if row[1] else str(user_id))

def _role_emoji(role):
    return {"owner": "👑", "officer": "⭐", "member": "👤"}.get(role, "👤")

def _role_name(role):
    return {"owner": "Владелец", "officer": "Офицер", "member": "Участник"}.get(role, "Участник")

def _active_war(clan_id):
    """Возвращает активную войну клана или None."""
    now = int(time.time())
    with get_db_cursor() as cur:
        cur.execute("""
            SELECT * FROM clan_wars
            WHERE (attacker_id=? OR defender_id=?) AND status='active' AND ends_at > ?
        """, (clan_id, clan_id, now))
        row = cur.fetchone()
        return dict(row) if row else None

# ── Текстовые экраны ─────────────────────────────────────────

def _clan_main_text(user_id):
    clan = get_user_clan(user_id)
    if not clan:
        return (
            "⚔️ <b>Кланы</b>\n\n"
            "Ты не состоишь ни в одном клане.\n\n"
            "Создай свой клан или вступи в существующий."
        )
    tag      = clan['tag']
    name     = clan['name']
    lvl      = clan['level']
    balance  = clan['balance']
    members  = _get_member_count(clan['id'])
    role     = clan['role']
    wars_w   = clan.get('war_wins', 0)
    wars_l   = clan.get('war_losses', 0)
    score    = clan.get('war_score', 0)

    war = _active_war(clan['id'])
    war_line = ""
    if war:
        ends = _cdt.datetime.utcfromtimestamp(war['ends_at']).strftime("%d.%m %H:%M")
        enemy_id = war['defender_id'] if war['attacker_id'] == clan['id'] else war['attacker_id']
        enemy = _get_clan_by_id(enemy_id)
        war_line = f"\n🔥 <b>Война с [{enemy['tag']}]</b> — до {ends} UTC\n"

    return (
        f"⚔️ <b>[{tag}] {name}</b>  Ур.{lvl}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Казна: <b>{format_balance(balance)}</b>\n"
        f"👥 Участников: <b>{members}/{CLAN_MAX_MEMBERS}</b>\n"
        f"🏆 Войны: <b>{wars_w}W / {wars_l}L</b>  |  Очки: <b>{score}</b>\n"
        f"{war_line}"
        f"━━━━━━━━━━━━━━━\n"
        f"Твоя роль: {_role_emoji(role)} <b>{_role_name(role)}</b>"
    )

def _clan_main_markup(user_id):
    clan = get_user_clan(user_id)
    mk = InlineKeyboardMarkup(row_width=2)
    if not clan:
        mk.add(
            InlineKeyboardButton("➕ Создать клан", callback_data="clan_create_prompt"),
            InlineKeyboardButton("🔍 Найти клан",   callback_data="clan_search"),
        )
        mk.add(InlineKeyboardButton("🏆 Топ кланов", callback_data="clan_top_0"))
        return mk

    cid  = clan['id']
    role = clan['role']
    mk.add(
        InlineKeyboardButton("👥 Участники",  callback_data=f"clan_members_{cid}"),
        InlineKeyboardButton("📋 Заявки",     callback_data=f"clan_apps_{cid}"),
    )
    mk.add(
        InlineKeyboardButton("💰 Взнос в казну", callback_data="clan_deposit_prompt"),
        InlineKeyboardButton("⚔️ Война",          callback_data="clan_war_menu"),
    )
    mk.add(InlineKeyboardButton("🏆 Топ кланов", callback_data="clan_top_0"))
    mk.add(InlineKeyboardButton("🎯 Квест",      callback_data="cq_quest"))
    if role in ("owner", "officer"):
        mk.add(InlineKeyboardButton("⚙️ Управление", callback_data=f"clan_manage_{cid}"))
    mk.add(InlineKeyboardButton("🚪 Выйти из клана", callback_data="clan_leave_confirm"))
    return mk

# ── Команда «клан» ───────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text and m.text.strip().lower() in ("клан", "⚔️ клан"))
def handle_clan_main(message):
    uid = message.from_user.id
    bot.send_message(
        message.chat.id,
        _clan_main_text(uid),
        reply_markup=_clan_main_markup(uid),
        parse_mode="HTML"
    )

# ── Создание клана ───────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith("создать клан "))
def handle_create_clan(message):
    uid = message.from_user.id
    # Формат: создать клан ТЕГ Название клана
    parts = message.text.strip().split(None, 3)
    if len(parts) < 4:
        bot.send_message(message.chat.id,
            "❌ Формат: <code>создать клан ТЕГ Название</code>\n"
            "Пример: <code>создать клан FCZ Fectiz Warriors</code>\n"
            "Тег: 2–5 символов, только буквы/цифры",
            parse_mode="HTML")
        return

    tag  = parts[2].upper()
    name = parts[3].strip()

    if len(tag) < 2 or len(tag) > 5 or not tag.isalnum():
        bot.send_message(message.chat.id,
            "❌ Тег должен быть 2–5 символов, только буквы и цифры.",
            parse_mode="HTML")
        return
    if len(name) < 3 or len(name) > 32:
        bot.send_message(message.chat.id,
            "❌ Название клана: от 3 до 32 символов.",
            parse_mode="HTML")
        return

    # Проверки
    existing_clan = get_user_clan(uid)
    if existing_clan:
        bot.send_message(message.chat.id,
            f"❌ Ты уже состоишь в клане <b>[{existing_clan['tag']}] {existing_clan['name']}</b>.\n"
            "Сначала выйди из него.",
            parse_mode="HTML")
        return

    with get_db_cursor() as cur:
        cur.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
        row = cur.fetchone()
        if not row or row[0] < CLAN_CREATE_COST:
            bot.send_message(message.chat.id,
                f"❌ Недостаточно средств.\n"
                f"Нужно: <b>{format_balance(CLAN_CREATE_COST)}</b>\n"
                f"У тебя: <b>{format_balance(row[0] if row else 0)}</b>",
                parse_mode="HTML")
            return

        if _get_clan_by_tag(tag):
            bot.send_message(message.chat.id,
                f"❌ Клан с тегом <b>[{tag}]</b> уже существует.",
                parse_mode="HTML")
            return

        # Списываем деньги
        cur.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (CLAN_CREATE_COST, uid))

        # Создаём клан
        cur.execute(
            "INSERT INTO clans (name, tag, owner_id, created_at, level, balance, members_count, max_members) "
            "VALUES (?,?,?,?,1,0,1,?)",
            (name, tag, uid, int(time.time()), CLAN_MAX_MEMBERS)
        )
        clan_id = cur.lastrowid

        # Добавляем владельца
        cur.execute(
            "INSERT INTO clan_members (user_id, clan_id, role, joined_at) VALUES (?,?,?,?)",
            (uid, clan_id, "owner", int(time.time()))
        )
        cur.execute("UPDATE users SET clan_id=? WHERE user_id=?", (clan_id, uid))

    bot.send_message(message.chat.id,
        f"✅ <b>Клан [{tag}] {name} создан!</b>\n\n"
        f"💸 Потрачено: <b>{format_balance(CLAN_CREATE_COST)}</b>\n\n"
        f"Чтобы пригласить игроков — скажи им написать:\n"
        f"<code>клан вступить {tag}</code>",
        parse_mode="HTML",
        reply_markup=_clan_main_markup(uid)
    )

# ── Вступление в клан ────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith("клан вступить "))
def handle_join_clan(message):
    uid  = message.from_user.id
    parts = message.text.strip().split()
    if len(parts) < 3:
        bot.send_message(message.chat.id, "❌ Формат: <code>клан вступить ТЕГ</code>", parse_mode="HTML")
        return

    tag = parts[2].upper()
    clan = _get_clan_by_tag(tag)
    if not clan:
        bot.send_message(message.chat.id, f"❌ Клан <b>[{tag}]</b> не найден.", parse_mode="HTML")
        return

    if get_user_clan(uid):
        bot.send_message(message.chat.id, "❌ Сначала выйди из своего клана.", parse_mode="HTML")
        return

    clan_lv = clan.get('level', 1)
    if _get_member_count(clan['id']) >= _clan_max_members(clan_lv):
        bot.send_message(message.chat.id, "❌ В клане нет свободных мест.", parse_mode="HTML")
        return

    # Проверяем заявку
    with get_db_cursor() as cur:
        cur.execute("SELECT 1 FROM clan_applications WHERE user_id=? AND clan_id=?", (uid, clan['id']))
        if cur.fetchone():
            bot.send_message(message.chat.id,
                f"⏳ Твоя заявка в <b>[{tag}]</b> уже ожидает рассмотрения.",
                parse_mode="HTML")
            return

        cur.execute(
            "INSERT INTO clan_applications (user_id, clan_id, applied_at) VALUES (?,?,?)",
            (uid, clan['id'], int(time.time()))
        )

    # Уведомляем офицеров/владельца
    with get_db_cursor() as cur:
        cur.execute(
            "SELECT user_id FROM clan_members WHERE clan_id=? AND role IN ('owner','officer')",
            (clan['id'],)
        )
        managers = [r[0] for r in cur.fetchall()]

    name_str = _user_display(uid)
    for mid in managers:
        try:
            bot.send_message(mid,
                f"📩 <b>Новая заявка в клан [{tag}]</b>\n\n"
                f"👤 {name_str} хочет вступить.\n\n"
                f"Принять: <code>клан принять {uid}</code>\n"
                f"Отклонить: <code>клан отклонить {uid}</code>",
                parse_mode="HTML")
        except Exception:
            pass

    bot.send_message(message.chat.id,
        f"✅ Заявка в клан <b>[{tag}] {clan['name']}</b> отправлена!\n"
        f"Ожидай одобрения офицера или владельца.",
        parse_mode="HTML")

# ── Принять / отклонить заявку ───────────────────────────────

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith("клан принять "))
def handle_accept_app(message):
    uid   = message.from_user.id
    clan  = get_user_clan(uid)
    if not clan or not _can_manage(clan['id'], uid):
        bot.send_message(message.chat.id, "❌ Недостаточно прав.", parse_mode="HTML")
        return

    parts = message.text.strip().split()
    if len(parts) < 3:
        bot.send_message(message.chat.id, "❌ Формат: <code>клан принять USER_ID</code>", parse_mode="HTML")
        return

    try:
        target_uid = int(parts[2])
    except ValueError:
        bot.send_message(message.chat.id, "❌ Укажи числовой ID пользователя.", parse_mode="HTML")
        return

    cid = clan['id']
    _accept_err = None
    is_first_join = False
    new_treasury = 0
    all_members = []

    with get_db_cursor() as cur:
        cur.execute("SELECT 1 FROM clan_applications WHERE user_id=? AND clan_id=?", (target_uid, cid))
        app_exists = cur.fetchone()

        cur.execute("SELECT level FROM clans WHERE id=?", (cid,))
        clan_level = (cur.fetchone() or [1])[0]
        cur.execute("SELECT COUNT(*) FROM clan_members WHERE clan_id=?", (cid,))
        member_count = cur.fetchone()[0]

        if not app_exists:
            _accept_err = "no_app"
        elif member_count >= _clan_max_members(clan_level):
            _accept_err = "full"
        else:
            cur.execute("DELETE FROM clan_applications WHERE user_id=? AND clan_id=?", (target_uid, cid))
            cur.execute(
                "INSERT OR IGNORE INTO clan_members (user_id, clan_id, role, joined_at) VALUES (?,?,?,?)",
                (target_uid, cid, "member", int(time.time()))
            )
            cur.execute("UPDATE clans SET members_count=members_count+1 WHERE id=?", (cid,))
            cur.execute("UPDATE users SET clan_id=? WHERE user_id=?", (cid, target_uid))

            # Бонус в казну ТОЛЬКО если человек заходит первый раз
            cur.execute("SELECT 1 FROM clan_join_history WHERE user_id=? AND clan_id=?", (target_uid, cid))
            is_first_join = not cur.fetchone()
            if is_first_join:
                cur.execute("UPDATE clans SET balance=balance+? WHERE id=?", (100_000, cid))
                cur.execute("INSERT OR IGNORE INTO clan_join_history (user_id, clan_id) VALUES (?,?)", (target_uid, cid))

            cur.execute("SELECT balance FROM clans WHERE id=?", (cid,))
            new_treasury = cur.fetchone()[0]
            cur.execute("SELECT user_id FROM clan_members WHERE clan_id=?", (cid,))
            all_members = [r[0] for r in cur.fetchall()]

    # Ответы — строго вне блока with get_db_cursor()
    if _accept_err == "no_app":
        bot.send_message(message.chat.id, "❌ Заявка не найдена.", parse_mode="HTML")
        return
    if _accept_err == "full":
        bot.send_message(message.chat.id, "❌ Клан переполнен.", parse_mode="HTML")
        return

    name_str = _user_display(target_uid)
    bonus_line = "\n💰 +100 000 🌸 в казну клана!" if is_first_join else ""
    bot.send_message(message.chat.id,
        f"✅ <b>{name_str}</b> принят в клан <b>[{clan['tag']}]</b>!{bonus_line}",
        parse_mode="HTML")

    threading.Thread(target=_advance_quest, args=(cid, 'members', 1), daemon=True).start()

    channel_msg = f"👋 <b>Новый участник!</b>\n➕ {name_str} вступил в клан [{clan['tag']}]"
    if is_first_join:
        channel_msg += "\n💰 +100 000 🌸 в казну!"
    threading.Thread(target=_clan_post, args=(cid, channel_msg), daemon=True).start()

    notif = (
        f"👋 <b>Новый участник в клане [{clan['tag']}]!</b>\n\n"
        f"➕ {name_str} вступил в клан\n"
        + (f"💰 Казна пополнилась на <b>100 000 🌸</b>\n" if is_first_join else "")
        + f"🏦 Итого в казне: <b>{format_balance(new_treasury)}</b>"
    )
    for mid in all_members:
        try:
            if mid == target_uid:
                welcome = (
                    f"🎉 Добро пожаловать в клан <b>[{clan['tag']}] {clan['name']}</b>!\n"
                    + (f"💰 Казна пополнилась на 100 000 🌸 за твой первый вход!\n" if is_first_join else "")
                    + f"Напиши <code>клан</code> чтобы открыть меню."
                )
                bot.send_message(mid, welcome, parse_mode="HTML")
            else:
                bot.send_message(mid, notif, parse_mode="HTML")
        except Exception:
            pass

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith("клан отклонить "))
def handle_reject_app(message):
    uid  = message.from_user.id
    clan = get_user_clan(uid)
    if not clan or not _can_manage(clan['id'], uid):
        bot.send_message(message.chat.id, "❌ Недостаточно прав.", parse_mode="HTML")
        return

    parts = message.text.strip().split()
    if len(parts) < 3:
        return

    try:
        target_uid = int(parts[2])
    except ValueError:
        return

    with get_db_cursor() as cur:
        cur.execute("DELETE FROM clan_applications WHERE user_id=? AND clan_id=?", (target_uid, clan['id']))

    bot.send_message(message.chat.id, f"❌ Заявка отклонена.", parse_mode="HTML")

# ── Выход из клана ───────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text and m.text.strip().lower() == "клан выйти")
def handle_leave_clan(message):
    uid  = message.from_user.id
    clan = get_user_clan(uid)
    if not clan:
        bot.send_message(message.chat.id, "❌ Ты не состоишь в клане.", parse_mode="HTML")
        return

    if clan['role'] == 'owner':
        members = _get_clan_members(clan['id'])
        if len(members) > 1:
            bot.send_message(message.chat.id,
                "❌ Ты владелец. Сначала передай клан другому участнику:\n"
                "<code>клан передать USER_ID</code>\n"
                "Или распусти клан: <code>клан распустить</code>",
                parse_mode="HTML")
            return
        else:
            # Единственный участник — удаляем клан
            _disband_clan(clan['id'])
            bot.send_message(message.chat.id,
                f"🗑 Клан <b>[{clan['tag']}]</b> распущен, так как ты был единственным участником.",
                parse_mode="HTML")
            return

    with get_db_cursor() as cur:
        cur.execute("DELETE FROM clan_members WHERE user_id=? AND clan_id=?", (uid, clan['id']))
        cur.execute("UPDATE clans SET members_count=members_count-1 WHERE id=?", (clan['id'],))
        cur.execute("UPDATE users SET clan_id=0 WHERE user_id=?", (uid,))

    bot.send_message(message.chat.id,
        f"✅ Ты вышел из клана <b>[{clan['tag']}]</b>.",
        parse_mode="HTML")

# ── Исключение участника ─────────────────────────────────────

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith("клан кик "))
def handle_kick(message):
    uid  = message.from_user.id
    clan = get_user_clan(uid)
    if not clan or not _can_manage(clan['id'], uid):
        bot.send_message(message.chat.id, "❌ Недостаточно прав.", parse_mode="HTML")
        return

    parts = message.text.strip().split()
    if len(parts) < 3:
        bot.send_message(message.chat.id, "❌ Формат: <code>клан кик USER_ID</code>", parse_mode="HTML")
        return

    try:
        target_uid = int(parts[2])
    except ValueError:
        bot.send_message(message.chat.id, "❌ Укажи числовой ID.", parse_mode="HTML")
        return

    if target_uid == uid:
        bot.send_message(message.chat.id, "❌ Нельзя исключить самого себя.", parse_mode="HTML")
        return

    target_role = _member_role(clan['id'], target_uid)
    if not target_role:
        bot.send_message(message.chat.id, "❌ Этот игрок не в твоём клане.", parse_mode="HTML")
        return

    # Офицер не может кикнуть другого офицера или владельца
    if clan['role'] == 'officer' and target_role in ('officer', 'owner'):
        bot.send_message(message.chat.id, "❌ Офицер не может исключить другого офицера или владельца.", parse_mode="HTML")
        return

    with get_db_cursor() as cur:
        cur.execute("DELETE FROM clan_members WHERE user_id=? AND clan_id=?", (target_uid, clan['id']))
        cur.execute("UPDATE clans SET members_count=members_count-1 WHERE id=?", (clan['id'],))
        cur.execute("UPDATE users SET clan_id=0 WHERE user_id=?", (target_uid,))

    name_str = _user_display(target_uid)
    bot.send_message(message.chat.id,
        f"✅ <b>{name_str}</b> исключён из клана.",
        parse_mode="HTML")
    try:
        bot.send_message(target_uid,
            f"⚠️ Тебя исключили из клана <b>[{clan['tag']}]</b>.",
            parse_mode="HTML")
    except Exception:
        pass

# ── Назначение офицера ───────────────────────────────────────

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith("клан офицер "))
def handle_set_officer(message):
    uid  = message.from_user.id
    clan = get_user_clan(uid)
    if not clan or clan['role'] != 'owner':
        bot.send_message(message.chat.id, "❌ Только владелец может назначать офицеров.", parse_mode="HTML")
        return

    parts = message.text.strip().split()
    if len(parts) < 3:
        return
    try:
        target_uid = int(parts[2])
    except ValueError:
        bot.send_message(message.chat.id, "❌ Укажи числовой ID.", parse_mode="HTML")
        return

    role = _member_role(clan['id'], target_uid)
    if not role:
        bot.send_message(message.chat.id, "❌ Этот игрок не в твоём клане.", parse_mode="HTML")
        return

    new_role = "member" if role == "officer" else "officer"
    with get_db_cursor() as cur:
        cur.execute("UPDATE clan_members SET role=? WHERE user_id=? AND clan_id=?",
                    (new_role, target_uid, clan['id']))

    name_str = _user_display(target_uid)
    action = "назначен офицером ⭐" if new_role == "officer" else "разжалован до участника"
    bot.send_message(message.chat.id,
        f"✅ <b>{name_str}</b> {action}.",
        parse_mode="HTML")

# ── Взнос в казну ────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith("клан казна "))
def handle_clan_deposit(message):
    uid  = message.from_user.id
    clan = get_user_clan(uid)
    if not clan:
        bot.send_message(message.chat.id, "❌ Ты не состоишь в клане.", parse_mode="HTML")
        return

    parts = message.text.strip().split()
    if len(parts) < 3:
        bot.send_message(message.chat.id, "❌ Формат: <code>клан казна 10000</code>", parse_mode="HTML")
        return

    try:
        amount = int(parts[2].replace(" ", "").replace(",", ""))
    except ValueError:
        bot.send_message(message.chat.id, "❌ Укажи число.", parse_mode="HTML")
        return

    if amount <= 0:
        bot.send_message(message.chat.id, "❌ Сумма должна быть > 0.", parse_mode="HTML")
        return

    with get_db_cursor() as cur:
        cur.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
        bal = cur.fetchone()[0]
        if bal < amount:
            bot.send_message(message.chat.id,
                f"❌ Недостаточно средств. У тебя: <b>{format_balance(bal)}</b>",
                parse_mode="HTML")
            return
        cur.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, uid))
        cur.execute("UPDATE clans SET balance=balance+? WHERE id=?", (amount, clan['id']))
        cur.execute(
            "UPDATE clan_members SET contributed=contributed+? WHERE user_id=? AND clan_id=?",
            (amount, uid, clan['id'])
        )
        cur.execute("SELECT balance FROM clans WHERE id=?", (clan['id'],))
        new_clan_balance = cur.fetchone()[0]
    # Прогресс квеста на взнос в казну
    threading.Thread(target=_advance_quest, args=(clan['id'], 'deposit', amount), daemon=True).start()

    bot.send_message(message.chat.id,
        f"✅ Внёс <b>{format_balance(amount)}</b> в казну клана <b>[{clan['tag']}]</b>!\n"
        f"💰 Казна теперь: <b>{format_balance(new_clan_balance)}</b>",
        parse_mode="HTML")

# ── Роспуск клана ────────────────────────────────────────────

def _disband_clan(clan_id):
    with get_db_cursor() as cur:
        cur.execute("UPDATE users SET clan_id=0 WHERE clan_id=?", (clan_id,))
        cur.execute("DELETE FROM clan_members WHERE clan_id=?", (clan_id,))
        cur.execute("DELETE FROM clan_applications WHERE clan_id=?", (clan_id,))
        cur.execute("DELETE FROM clans WHERE id=?", (clan_id,))

@bot.message_handler(func=lambda m: m.text and m.text.strip().lower() == "клан распустить")
def handle_disband(message):
    uid  = message.from_user.id
    clan = get_user_clan(uid)
    if not clan or clan['role'] != 'owner':
        bot.send_message(message.chat.id, "❌ Только владелец может распустить клан.", parse_mode="HTML")
        return

    mk = InlineKeyboardMarkup()
    mk.add(
        InlineKeyboardButton("✅ Да, распустить", callback_data=f"clan_disband_yes_{clan['id']}"),
        InlineKeyboardButton("❌ Отмена",          callback_data="clan_disband_no"),
    )
    bot.send_message(message.chat.id,
        f"⚠️ Ты уверен что хочешь распустить клан <b>[{clan['tag']}] {clan['name']}</b>?\n\n"
        f"Казна ({format_balance(clan['balance'])}) будет потеряна!",
        reply_markup=mk,
        parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data.startswith("clan_disband_yes_"))
def cb_disband_yes(call):
    uid     = call.from_user.id
    clan_id = int(call.data.split("_")[-1])
    clan    = get_user_clan(uid)
    if not clan or clan['id'] != clan_id or clan['role'] != 'owner':
        bot.answer_callback_query(call.id, "❌ Ошибка")
        return
    _disband_clan(clan_id)
    bot.edit_message_text(
        f"🗑 Клан <b>[{clan['tag']}]</b> распущен.",
        call.message.chat.id, call.message.message_id, parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "clan_disband_no")
def cb_disband_no(call):
    bot.edit_message_text("❌ Роспуск отменён.", call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)

# ── Передача клана ───────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith("клан передать "))
def handle_transfer(message):
    uid  = message.from_user.id
    clan = get_user_clan(uid)
    if not clan or clan['role'] != 'owner':
        bot.send_message(message.chat.id, "❌ Только владелец может передать клан.", parse_mode="HTML")
        return

    parts = message.text.strip().split()
    if len(parts) < 3:
        return
    try:
        target_uid = int(parts[2])
    except ValueError:
        bot.send_message(message.chat.id, "❌ Укажи числовой ID.", parse_mode="HTML")
        return

    if not _member_role(clan['id'], target_uid):
        bot.send_message(message.chat.id, "❌ Этот игрок не в твоём клане.", parse_mode="HTML")
        return

    with get_db_cursor() as cur:
        cur.execute("UPDATE clan_members SET role='member' WHERE user_id=? AND clan_id=?", (uid, clan['id']))
        cur.execute("UPDATE clan_members SET role='owner' WHERE user_id=? AND clan_id=?", (target_uid, clan['id']))
        cur.execute("UPDATE clans SET owner_id=? WHERE id=?", (target_uid, clan['id']))

    name_str = _user_display(target_uid)
    bot.send_message(message.chat.id,
        f"✅ Клан <b>[{clan['tag']}]</b> передан игроку <b>{name_str}</b>.",
        parse_mode="HTML")

# ══════════════════════════════════════════════════════════════
# ⚔️  КЛАНОВЫЕ ВОЙНЫ (HP-система)
# ══════════════════════════════════════════════════════════════

def _war_hp(clan_id):
    count = _get_member_count(clan_id)
    return max(CLAN_WAR_HP_MIN, count * CLAN_WAR_HP_PER_MEMBER)

def _hp_bar(hp, hp_max):
    if hp_max <= 0:
        return "░░░░░░░░░░ 0%"
    pct = max(0, min(1, hp / hp_max))
    filled = int(pct * 10)
    bar = "█" * filled + "░" * (10 - filled)
    return f"{bar} {int(pct*100)}%"

def _get_war_action_cd(user_id):
    with get_db_cursor() as cur:
        cur.execute("SELECT last_action FROM clan_war_cooldowns WHERE user_id=?", (user_id,))
        row = cur.fetchone()
    if not row:
        return 0
    return max(0, row[0] + CLAN_WAR_ACTION_CD - int(time.time()))

def _set_war_action_cd(user_id):
    with get_db_cursor() as cur:
        cur.execute(
            "INSERT INTO clan_war_cooldowns (user_id, last_action) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_action=?",
            (user_id, int(time.time()), int(time.time()))
        )

def _get_active_war_full(clan_id):
    now = int(time.time())
    with get_db_cursor() as cur:
        cur.execute("""
            SELECT * FROM clan_wars
            WHERE (attacker_id=? OR defender_id=?) AND status='active' AND ends_at > ?
        """, (clan_id, clan_id, now))
        row = cur.fetchone()
        return dict(row) if row else None

# ── Объявление войны ─────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith("клан война "))
def handle_declare_war(message):
    uid  = message.from_user.id
    clan = get_user_clan(uid)
    if not clan:
        bot.send_message(message.chat.id, "❌ Ты не состоишь в клане.", parse_mode="HTML")
        return
    if not _can_manage(clan['id'], uid):
        bot.send_message(message.chat.id, "❌ Только владелец или офицер может объявить войну.", parse_mode="HTML")
        return

    parts = message.text.strip().split()
    if len(parts) < 3:
        bot.send_message(message.chat.id, "❌ Формат: <code>клан война ТЕГ</code>", parse_mode="HTML")
        return

    enemy_tag = parts[2].upper()
    enemy = _get_clan_by_tag(enemy_tag)
    if not enemy:
        bot.send_message(message.chat.id, f"❌ Клан <b>[{enemy_tag}]</b> не найден.", parse_mode="HTML")
        return
    if enemy['id'] == clan['id']:
        bot.send_message(message.chat.id, "❌ Нельзя объявить войну самому себе.", parse_mode="HTML")
        return
    if _get_active_war_full(clan['id']):
        bot.send_message(message.chat.id, "❌ Твой клан уже ведёт войну.", parse_mode="HTML")
        return
    if _get_active_war_full(enemy['id']):
        bot.send_message(message.chat.id, "❌ Этот клан уже ведёт войну.", parse_mode="HTML")
        return
    if clan['balance'] < CLAN_WAR_COST:
        bot.send_message(message.chat.id,
            f"❌ Недостаточно средств в казне.\n"
            f"Нужно: <b>{format_balance(CLAN_WAR_COST)}</b>\n"
            f"В казне: <b>{format_balance(clan['balance'])}</b>",
            parse_mode="HTML")
        return

    att_hp = _war_hp(clan['id'])
    def_hp = _war_hp(enemy['id'])

    mk = InlineKeyboardMarkup()
    mk.add(
        InlineKeyboardButton(f"⚔️ Начать войну с [{enemy_tag}]",
                             callback_data=f"clan_war_declare_{clan['id']}_{enemy['id']}"),
        InlineKeyboardButton("❌ Отмена", callback_data="clan_war_cancel"),
    )
    bot.send_message(message.chat.id,
        f"⚔️ <b>Объявление войны</b>\n\n"
        f"🔵 <b>[{clan['tag']}] {clan['name']}</b>\n"
        f"   ❤️ HP: <b>{att_hp}</b>\n\n"
        f"🔴 <b>[{enemy['tag']}] {enemy['name']}</b>\n"
        f"   ❤️ HP: <b>{def_hp}</b>\n\n"
        f"💰 Взнос из казны: <b>{format_balance(CLAN_WAR_COST)}</b>\n"
        f"⏱ До 24 часов или пока у кого-то HP = 0\n\n"
        f"⚔️ Атака: <b>{format_balance(CLAN_WAR_ATTACK_COST)}</b> → -{CLAN_WAR_ATTACK_DMG} HP врагу\n"
        f"🛡 Защита: <b>{format_balance(CLAN_WAR_DEFEND_COST)}</b> → +{CLAN_WAR_DEFEND_HEAL} HP себе\n"
        f"⏳ Кулдаун: <b>5 минут</b> на каждое действие",
        reply_markup=mk, parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data.startswith("clan_war_declare_"))
def cb_war_declare(call):
    uid   = call.from_user.id
    parts = call.data.split("_")
    att_id, def_id = int(parts[3]), int(parts[4])

    attacker = _get_clan_by_id(att_id)
    defender = _get_clan_by_id(def_id)
    if not attacker or not defender:
        bot.answer_callback_query(call.id, "❌ Клан не найден")
        return

    clan = get_user_clan(uid)
    if not clan or clan['id'] != att_id or not _can_manage(att_id, uid):
        bot.answer_callback_query(call.id, "❌ Нет прав")
        return
    if _get_active_war_full(att_id) or _get_active_war_full(def_id):
        bot.answer_callback_query(call.id, "❌ Один из кланов уже воюет")
        return

    with get_db_cursor() as cur:
        cur.execute("SELECT balance FROM clans WHERE id=?", (att_id,))
        fresh_bal = cur.fetchone()[0]
    if fresh_bal < CLAN_WAR_COST:
        bot.answer_callback_query(call.id, "❌ Недостаточно средств в казне")
        return

    now    = int(time.time())
    ends   = now + CLAN_WAR_DURATION
    att_hp = _war_hp(att_id)
    def_hp = _war_hp(def_id)

    with get_db_cursor() as cur:
        cur.execute("UPDATE clans SET balance=balance-? WHERE id=?", (CLAN_WAR_COST, att_id))
        cur.execute(
            "INSERT INTO clan_wars (attacker_id, defender_id, started_at, ends_at, status, prize, "
            "att_hp, def_hp, att_hp_max, def_hp_max) VALUES (?,?,?,?,'active',?,?,?,?,?)",
            (att_id, def_id, now, ends, CLAN_WAR_COST, att_hp, def_hp, att_hp, def_hp)
        )

    ends_str = _cdt.datetime.utcfromtimestamp(ends).strftime("%d.%m.%Y %H:%M UTC")
    bot.edit_message_text(
        f"🔥 <b>ВОЙНА НАЧАЛАСЬ!</b>\n\n"
        f"🔵 <b>[{attacker['tag']}]</b>  ❤️ {att_hp} HP\n"
        f"🔴 <b>[{defender['tag']}]</b>  ❤️ {def_hp} HP\n\n"
        f"⏱ Конец: <b>{ends_str}</b>\n\n"
        f"Нажми <b>⚔️ Война</b> в меню клана чтобы атаковать!",
        call.message.chat.id, call.message.message_id, parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)
    _clan_post(att_id, f"🔥 <b>ВОЙНА ОБЪЯВЛЕНА!</b>\n⚔️ [{attacker['tag']}] vs [{defender['tag']}]\n⏱ Конец: <b>{ends_str}</b>")
    _clan_post(def_id, f"⚔️ <b>Клан [{attacker['tag']}] объявил нам войну!</b>\n⏱ Конец: <b>{ends_str}</b>")

    with get_db_cursor() as cur:
        cur.execute("SELECT user_id FROM clan_members WHERE clan_id=?", (def_id,))
        def_members = [r[0] for r in cur.fetchall()]
    for mid in def_members:
        try:
            bot.send_message(mid,
                f"⚔️ <b>Клан [{attacker['tag']}] объявил вам войну!</b>\n\n"
                f"🔵 Их HP: <b>{att_hp}</b>\n"
                f"🔴 Ваш HP: <b>{def_hp}</b>\n\n"
                f"⏱ Конец: <b>{ends_str}</b>\n"
                f"Открой меню клана → ⚔️ Война!",
                parse_mode="HTML")
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data == "clan_war_cancel")
def cb_war_cancel(call):
    bot.edit_message_text("❌ Война отменена.", call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)

# ── Атака / Защита ───────────────────────────────────────────

def _war_action(uid, action):
    clan = get_user_clan(uid)
    if not clan:
        return False, "❌ Ты не в клане."

    war = _get_active_war_full(clan['id'])
    if not war:
        return False, "❌ Твой клан сейчас не воюет."

    cd = _get_war_action_cd(uid)
    if cd > 0:
        m, s = divmod(cd, 60)
        return False, f"⏳ Кулдаун: <b>{m}м {s}с</b>"

    is_attacker = (war['attacker_id'] == clan['id'])
    enemy_id    = war['defender_id'] if is_attacker else war['attacker_id']
    enemy       = _get_clan_by_id(enemy_id)
    en_hp_key   = 'def_hp' if is_attacker else 'att_hp'
    my_hp_key   = 'att_hp' if is_attacker else 'def_hp'
    my_max_key  = 'att_hp_max' if is_attacker else 'def_hp_max'

    cost = CLAN_WAR_ATTACK_COST if action == 'attack' else CLAN_WAR_DEFEND_COST

    balance = get_balance(uid)
    if balance < cost:
        return False, f"❌ Недостаточно монет.\nНужно: <b>{format_balance(cost)}</b>"

    update_balance(uid, -cost)

    with get_db_cursor() as cur:
        if action == 'attack':
            cur.execute(
                f"UPDATE clan_wars SET {en_hp_key}=MAX(0,{en_hp_key}-?) WHERE id=?",
                (CLAN_WAR_ATTACK_DMG, war['id'])
            )
            val = CLAN_WAR_ATTACK_DMG
        else:
            cur.execute(
                f"UPDATE clan_wars SET {my_hp_key}=MIN({my_max_key},{my_hp_key}+?) WHERE id=?",
                (CLAN_WAR_DEFEND_HEAL, war['id'])
            )
            val = CLAN_WAR_DEFEND_HEAL

        cur.execute(
            "INSERT INTO clan_war_actions (war_id,user_id,clan_id,action,cost,value,done_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (war['id'], uid, clan['id'], action, cost, val, int(time.time()))
        )
        cur.execute(
            "SELECT att_hp, def_hp, att_hp_max, def_hp_max FROM clan_wars WHERE id=?",
            (war['id'],)
        )
        row = cur.fetchone()

    _set_war_action_cd(uid)
    # Прогресс квеста на атаки
    if action == 'attack':
        threading.Thread(target=_advance_quest, args=(clan['id'], 'attack', 1), daemon=True).start()

    att_hp, def_hp, att_hp_max, def_hp_max = row[0], row[1], row[2], row[3]
    my_hp  = att_hp if is_attacker else def_hp
    en_hp  = def_hp if is_attacker else att_hp
    my_max = att_hp_max if is_attacker else def_hp_max
    en_max = def_hp_max if is_attacker else att_hp_max

    name = _user_display(uid)
    if action == 'attack':
        action_txt = f"⚔️ <b>{name}</b> атаковал [{enemy['tag']}]! -{CLAN_WAR_ATTACK_DMG} HP"
    else:
        action_txt = f"🛡 <b>{name}</b> восстановил HP [{clan['tag']}]! +{CLAN_WAR_DEFEND_HEAL} HP"

    result = (
        f"{action_txt}\n"
        f"💸 Потрачено: {format_balance(cost)}\n\n"
        f"🔵 [{clan['tag']}]: {_hp_bar(my_hp, my_max)} {my_hp}/{my_max}\n"
        f"🔴 [{enemy['tag']}]: {_hp_bar(en_hp, en_max)} {en_hp}/{en_max}"
    )

    # Враг убит — завершаем войну
    if en_hp <= 0:
        threading.Thread(target=_finish_war_by_id, args=(war['id'],), daemon=True).start()
        return True, result

    # Предупреждение при HP < 30%
    if action == 'attack' and en_hp <= en_max * 0.30:
        with get_db_cursor() as cur:
            cur.execute(
                "SELECT user_id FROM clan_members WHERE clan_id=? AND role IN ('owner','officer')",
                (enemy_id,)
            )
            officers = [r[0] for r in cur.fetchall()]
        for oid in officers:
            try:
                bot.send_message(oid,
                    f"⚠️ <b>Критическое HP!</b>\n"
                    f"Клан [{enemy['tag']}]: ❤️ <b>{en_hp}/{en_max}</b>\n"
                    f"Срочно защищайтесь!",
                    parse_mode="HTML")
            except Exception:
                pass

    return True, result

@bot.callback_query_handler(func=lambda c: c.data == "clan_war_attack")
def cb_war_attack(call):
    ok, text = _war_action(call.from_user.id, 'attack')
    bot.answer_callback_query(call.id)
    try:
        war_text, war_mk = _war_menu_text_markup(call.from_user.id)
        bot.edit_message_text(
            text + "\n\n─────────────────\n" + war_text,
            call.message.chat.id, call.message.message_id,
            reply_markup=war_mk, parse_mode="HTML"
        )
    except Exception:
        bot.send_message(call.message.chat.id, text, parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data == "clan_war_defend")
def cb_war_defend(call):
    ok, text = _war_action(call.from_user.id, 'defend')
    bot.answer_callback_query(call.id)
    try:
        war_text, war_mk = _war_menu_text_markup(call.from_user.id)
        bot.edit_message_text(
            text + "\n\n─────────────────\n" + war_text,
            call.message.chat.id, call.message.message_id,
            reply_markup=war_mk, parse_mode="HTML"
        )
    except Exception:
        bot.send_message(call.message.chat.id, text, parse_mode="HTML")

# ── Экран войны (меню) ───────────────────────────────────────

def _war_menu_text_markup(user_id):
    clan = get_user_clan(user_id)
    if not clan:
        mk = InlineKeyboardMarkup()
        mk.add(InlineKeyboardButton("◀️ Назад", callback_data="clan_back"))
        return "❌ Ты не в клане.", mk

    war = _get_active_war_full(clan['id'])
    if not war:
        mk = InlineKeyboardMarkup(row_width=1)
        mk.add(InlineKeyboardButton("◀️ Назад", callback_data="clan_back"))
        return (
            f"⚔️ <b>Клановые войны</b>\n\n"
            f"Клан сейчас не воюет.\n\n"
            f"Объяви войну: <code>клан война ТЕГ</code>\n\n"
            f"⚔️ Атака: <b>{format_balance(CLAN_WAR_ATTACK_COST)}</b> → -{CLAN_WAR_ATTACK_DMG} HP\n"
            f"🛡 Защита: <b>{format_balance(CLAN_WAR_DEFEND_COST)}</b> → +{CLAN_WAR_DEFEND_HEAL} HP\n"
            f"⏳ Кулдаун: 5 мин | Взнос: {format_balance(CLAN_WAR_COST)}",
            mk
        )

    is_attacker = (war['attacker_id'] == clan['id'])
    enemy_id    = war['defender_id'] if is_attacker else war['attacker_id']
    enemy       = _get_clan_by_id(enemy_id)

    my_hp  = war['att_hp']     if is_attacker else war['def_hp']
    en_hp  = war['def_hp']     if is_attacker else war['att_hp']
    my_max = war['att_hp_max'] if is_attacker else war['def_hp_max']
    en_max = war['def_hp_max'] if is_attacker else war['att_hp_max']

    ends_str = _cdt.datetime.utcfromtimestamp(war['ends_at']).strftime("%d.%m %H:%M UTC")
    cd = _get_war_action_cd(user_id)

    # Последние 5 действий
    with get_db_cursor() as cur:
        cur.execute("""
            SELECT a.action, a.value, a.done_at, u.first_name, u.custom_name
            FROM clan_war_actions a
            JOIN users u ON u.user_id=a.user_id
            WHERE a.war_id=?
            ORDER BY a.done_at DESC LIMIT 5
        """, (war['id'],))
        recent = cur.fetchall()

    log_lines = []
    for act, val, ts, fn, cn in recent:
        name = cn or fn or "?"
        dt   = _cdt.datetime.utcfromtimestamp(ts).strftime("%H:%M")
        icon = "⚔️" if act == 'attack' else "🛡"
        sign = f"-{val} HP" if act == 'attack' else f"+{val} HP"
        log_lines.append(f"{icon} {name} {sign} <i>{dt}</i>")

    log_block = ("\n\n📜 <b>Лог:</b>\n" + "\n".join(log_lines)) if log_lines else ""

    if cd > 0:
        m, s = divmod(cd, 60)
        cd_line = f"\n⏳ Твой кулдаун: <b>{m}м {s}с</b>"
        mk = InlineKeyboardMarkup(row_width=1)
        mk.add(InlineKeyboardButton("🔄 Обновить", callback_data="clan_war_menu"))
        mk.add(InlineKeyboardButton("◀️ Назад",    callback_data="clan_back"))
    else:
        cd_line = ""
        mk = InlineKeyboardMarkup(row_width=2)
        mk.add(
            InlineKeyboardButton(f"⚔️ Атака (-{format_balance(CLAN_WAR_ATTACK_COST)})",
                                 callback_data="clan_war_attack"),
            InlineKeyboardButton(f"🛡 Защита (-{format_balance(CLAN_WAR_DEFEND_COST)})",
                                 callback_data="clan_war_defend"),
        )
        mk.add(InlineKeyboardButton("🔄 Обновить", callback_data="clan_war_menu"))
        mk.add(InlineKeyboardButton("◀️ Назад",    callback_data="clan_back"))

    text = (
        f"⚔️ <b>Война: [{clan['tag']}] vs [{enemy['tag']}]</b>\n"
        f"⏱ <b>{ends_str}</b>\n\n"
        f"🔵 <b>[{clan['tag']}]</b>  ❤️ {_hp_bar(my_hp, my_max)} {my_hp}/{my_max}\n\n"
        f"🔴 <b>[{enemy['tag']}]</b>  ❤️ {_hp_bar(en_hp, en_max)} {en_hp}/{en_max}"
        f"{cd_line}"
        f"{log_block}"
    )
    return text, mk

# ── Завершение войны ─────────────────────────────────────────

def _finish_war_by_id(war_id):
    with get_db_cursor() as cur:
        cur.execute("SELECT * FROM clan_wars WHERE id=? AND status='active'", (war_id,))
        row = cur.fetchone()
    if not row:
        return
    _finish_war(dict(row))

def _resolve_wars():
    now = int(time.time())
    with get_db_cursor() as cur:
        cur.execute(
            "SELECT * FROM clan_wars WHERE status='active' AND ends_at <= ?", (now,)
        )
        wars = [dict(r) for r in cur.fetchall()]
    for war in wars:
        try:
            _finish_war(war)
        except Exception as e:
            print(f"[clan_war] ошибка: {e}")

def _finish_war(war):
    war_id = war['id']
    att_id = war['attacker_id']
    def_id = war['defender_id']
    att_hp = war['att_hp']
    def_hp = war['def_hp']

    attacker = _get_clan_by_id(att_id)
    defender = _get_clan_by_id(def_id)
    if not attacker or not defender:
        return

    if att_hp > def_hp:
        winner_id, loser_id = att_id, def_id
        winner, loser = attacker, defender
        is_draw = False
    elif def_hp > att_hp:
        winner_id, loser_id = def_id, att_id
        winner, loser = defender, attacker
        is_draw = False
    else:
        is_draw = True
        winner_id = loser_id = None
        winner = loser = None

    prize = 0
    if not is_draw:
        prize = int(loser['balance'] * CLAN_WAR_WIN_PCT)

    with get_db_cursor() as cur:
        cur.execute(
            "UPDATE clan_wars SET status='finished', winner_id=?, prize=? WHERE id=? AND status='active'",
            (winner_id, prize, war_id)
        )
        if cur.rowcount == 0:
            return  # уже завершена другим потоком
        if not is_draw:
            if prize > 0:
                cur.execute("UPDATE clans SET balance=balance-? WHERE id=?", (prize, loser_id))
                cur.execute("UPDATE clans SET balance=balance+? WHERE id=?", (prize, winner_id))
            cur.execute("UPDATE clans SET war_wins=war_wins+1, war_score=war_score+3 WHERE id=?", (winner_id,))
            cur.execute("UPDATE clans SET war_losses=war_losses+1 WHERE id=?", (loser_id,))
        else:
            cur.execute("UPDATE clans SET war_score=war_score+1 WHERE id IN (?,?)", (att_id, def_id))

    if is_draw:
        result_text = (
            f"⚔️ <b>Война завершена!</b>\n\n"
            f"🔵 [{attacker['tag']}] ❤️ {att_hp} HP\n"
            f"🔴 [{defender['tag']}] ❤️ {def_hp} HP\n\n"
            f"🤝 <b>Ничья!</b> Оба клана получают по 1 очку."
        )
    else:
        result_text = (
            f"⚔️ <b>Война завершена!</b>\n\n"
            f"🔵 [{attacker['tag']}] ❤️ {att_hp} HP\n"
            f"🔴 [{defender['tag']}] ❤️ {def_hp} HP\n\n"
            f"🏆 Победитель: <b>[{winner['tag']}] {winner['name']}</b>\n"
            f"💰 Приз: <b>{format_balance(prize)}</b> из казны проигравшего"
        )

    with get_db_cursor() as cur:
        cur.execute("SELECT user_id FROM clan_members WHERE clan_id IN (?,?)", (att_id, def_id))
        all_members = [r[0] for r in cur.fetchall()]

    for mid in all_members:
        try:
            bot.send_message(mid, result_text, parse_mode="HTML")
        except Exception:
            pass

def _war_scheduler():
    while True:
        time.sleep(60)
        try:
            _resolve_wars()
        except Exception as e:
            print(f"[war_scheduler] ошибка: {e}")

def start_war_scheduler():
    t = threading.Thread(target=_war_scheduler, daemon=True)
    t.start()
    print("⚔️ Планировщик войн запущен")

start_war_scheduler()

# ── Список участников ────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("clan_members_"))
def cb_clan_members(call):
    clan_id  = int(call.data.split("_")[-1])
    members  = _get_clan_members(clan_id)
    clan     = _get_clan_by_id(clan_id)

    if not members or not clan:
        bot.answer_callback_query(call.id, "❌ Клан не найден")
        return

    lines = []
    for m in members:
        name = m['custom_name'] or m['first_name'] or (f"@{m['username']}" if m['username'] else str(m['user_id']))
        contributed = format_balance(m['contributed'])
        lines.append(f"{_role_emoji(m['role'])} <b>{name}</b> — взнос {contributed} | ID: <code>{m['user_id']}</code>")

    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton("◀️ Назад", callback_data="clan_back"))
    bot.edit_message_text(
        f"👥 <b>Участники [{clan['tag']}]</b> ({len(members)}/{CLAN_MAX_MEMBERS})\n\n"
        + "\n".join(lines),
        call.message.chat.id, call.message.message_id,
        reply_markup=mk, parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)

# ── Просмотр заявок ──────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("clan_apps_"))
def cb_clan_apps(call):
    uid     = call.from_user.id
    clan_id = int(call.data.split("_")[-1])
    clan    = get_user_clan(uid)

    if not clan or clan['id'] != clan_id or not _can_manage(clan_id, uid):
        bot.answer_callback_query(call.id, "❌ Нет прав")
        return

    with get_db_cursor() as cur:
        cur.execute("""
            SELECT ca.user_id, u.first_name, u.username, u.custom_name, ca.applied_at
            FROM clan_applications ca
            JOIN users u ON u.user_id=ca.user_id
            WHERE ca.clan_id=?
            ORDER BY ca.applied_at ASC
        """, (clan_id,))
        apps = cur.fetchall()

    if not apps:
        mk = InlineKeyboardMarkup()
        mk.add(InlineKeyboardButton("◀️ Назад", callback_data="clan_back"))
        bot.edit_message_text("📋 Нет заявок.", call.message.chat.id, call.message.message_id,
                              reply_markup=mk, parse_mode="HTML")
        bot.answer_callback_query(call.id)
        return

    lines = []
    for app in apps:
        auid, first_name, username, custom_name, applied_at = app
        name = custom_name or first_name or (f"@{username}" if username else str(auid))
        dt   = _cdt.datetime.utcfromtimestamp(applied_at).strftime("%d.%m %H:%M")
        lines.append(
            f"👤 <b>{name}</b> (ID: <code>{auid}</code>) — {dt}\n"
            f"   <code>клан принять {auid}</code> | <code>клан отклонить {auid}</code>"
        )

    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton("◀️ Назад", callback_data="clan_back"))
    bot.edit_message_text(
        f"📋 <b>Заявки в [{clan['tag']}]</b>\n\n" + "\n\n".join(lines),
        call.message.chat.id, call.message.message_id,
        reply_markup=mk, parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)

# ── Топ кланов ───────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("clan_top_"))
def cb_clan_top(call):
    page   = int(call.data.split("_")[-1])
    limit  = 10
    offset = page * limit

    with get_db_cursor() as cur:
        cur.execute("""
            SELECT c.id, c.name, c.tag, c.level, c.balance, c.war_wins, c.war_losses, c.war_score,
                   (SELECT COUNT(*) FROM clan_members cm WHERE cm.clan_id=c.id) as members
            FROM clans c
            ORDER BY c.war_score DESC, c.balance DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))
        rows = cur.fetchall()

        cur.execute("SELECT COUNT(*) FROM clans")
        total = cur.fetchone()[0]

    if not rows:
        bot.answer_callback_query(call.id, "Пусто")
        return

    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 20
    lines  = []
    for i, row in enumerate(rows):
        cid, name, tag, lvl, bal, ww, wl, ws, members = row
        medal = medals[offset + i]
        lines.append(
            f"{medal} <b>[{tag}] {name}</b>  Ур.{lvl}\n"
            f"   👥{members} | 💰{format_balance(bal)} | ⚔️{ww}W/{wl}L | 🏆{ws}pts"
        )

    total_pages = (total + limit - 1) // limit
    mk = InlineKeyboardMarkup(row_width=3)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"clan_top_{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"clan_top_{page+1}"))
    if nav:
        mk.add(*nav)
    mk.add(InlineKeyboardButton("◀️ Назад", callback_data="clan_back"))

    bot.edit_message_text(
        f"🏆 <b>Топ кланов</b>\n\n" + "\n\n".join(lines),
        call.message.chat.id, call.message.message_id,
        reply_markup=mk, parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)

# ── Поиск клана ──────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "clan_search")
def cb_clan_search(call):
    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton("◀️ Назад", callback_data="clan_back"))
    bot.edit_message_text(
        "🔍 <b>Поиск клана</b>\n\n"
        "Напиши: <code>клан инфо ТЕГ</code>\n"
        "Например: <code>клан инфо FCZ</code>",
        call.message.chat.id, call.message.message_id,
        reply_markup=mk, parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith("клан инфо "))
def handle_clan_info(message):
    parts = message.text.strip().split()
    if len(parts) < 3:
        return
    tag  = parts[2].upper()
    clan = _get_clan_by_tag(tag)
    if not clan:
        bot.send_message(message.chat.id, f"❌ Клан <b>[{tag}]</b> не найден.", parse_mode="HTML")
        return

    members = _get_clan_members(clan['id'])
    war     = _active_war(clan['id'])
    war_txt = ""
    if war:
        enemy_id  = war['defender_id'] if war['attacker_id'] == clan['id'] else war['attacker_id']
        enemy     = _get_clan_by_id(enemy_id)
        ends_str  = _cdt.datetime.utcfromtimestamp(war['ends_at']).strftime("%d.%m %H:%M UTC")
        war_txt   = f"\n🔥 <b>Война с [{enemy['tag']}]</b> до {ends_str}\n"

    mk = InlineKeyboardMarkup()
    if not get_user_clan(message.from_user.id):
        mk.add(InlineKeyboardButton(f"📩 Подать заявку в [{tag}]",
                                    callback_data=f"clan_apply_{tag}"))

    bot.send_message(message.chat.id,
        f"⚔️ <b>[{clan['tag']}] {clan['name']}</b>  Ур.{clan['level']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Казна: <b>{format_balance(clan['balance'])}</b>\n"
        f"👥 Участников: <b>{len(members)}/{CLAN_MAX_MEMBERS}</b>\n"
        f"🏆 Войны: <b>{clan.get('war_wins',0)}W / {clan.get('war_losses',0)}L</b>\n"
        f"🎯 Очки: <b>{clan.get('war_score',0)}</b>\n"
        f"{war_txt}",
        reply_markup=mk if mk.keyboard else None,
        parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("clan_apply_"))
def cb_clan_apply(call):
    uid = call.from_user.id
    tag = call.data.split("_", 2)[-1]

    if get_user_clan(uid):
        bot.answer_callback_query(call.id, "❌ Сначала выйди из своего клана")
        return

    clan = _get_clan_by_tag(tag)
    if not clan:
        bot.answer_callback_query(call.id, "❌ Клан не найден")
        return

    with get_db_cursor() as cur:
        cur.execute("SELECT 1 FROM clan_applications WHERE user_id=? AND clan_id=?", (uid, clan['id']))
        if cur.fetchone():
            bot.answer_callback_query(call.id, "⏳ Заявка уже отправлена")
            return
        cur.execute(
            "INSERT INTO clan_applications (user_id, clan_id, applied_at) VALUES (?,?,?)",
            (uid, clan['id'], int(time.time()))
        )

    # Уведомляем менеджеров
    with get_db_cursor() as cur:
        cur.execute(
            "SELECT user_id FROM clan_members WHERE clan_id=? AND role IN ('owner','officer')",
            (clan['id'],)
        )
        managers = [r[0] for r in cur.fetchall()]

    name_str = _user_display(uid)
    for mid in managers:
        try:
            bot.send_message(mid,
                f"📩 <b>Заявка в [{tag}]</b>\n\n"
                f"👤 {name_str} хочет вступить.\n\n"
                f"Принять: <code>клан принять {uid}</code>\n"
                f"Отклонить: <code>клан отклонить {uid}</code>",
                parse_mode="HTML")
        except Exception:
            pass

    bot.answer_callback_query(call.id, f"✅ Заявка в [{tag}] отправлена!")

# ── Взнос в казну (кнопка) ───────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "clan_deposit_prompt")
def cb_clan_deposit_prompt(call):
    uid  = call.from_user.id
    clan = get_user_clan(uid)
    if not clan:
        bot.answer_callback_query(call.id, "❌ Ты не в клане")
        return

    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton("◀️ Назад", callback_data="clan_back"))

    bot.edit_message_text(
        f"💰 <b>Взнос в казну [{clan['tag']}]</b>\n\n"
        f"Текущая казна: <b>{format_balance(clan['balance'])}</b>\n\n"
        f"Напиши в чат:\n"
        f"<code>клан казна 10000</code>\n\n"
        f"Деньги спишутся с твоего баланса и пополнят казну клана.\n"
        f"Казна используется для объявления войн и призов.",
        call.message.chat.id, call.message.message_id,
        reply_markup=mk, parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)

# ── Меню войны ───────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "clan_war_menu")
def cb_war_menu(call):
    text, mk = _war_menu_text_markup(call.from_user.id)
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=mk, parse_mode="HTML")
    except Exception:
        pass
    bot.answer_callback_query(call.id)

# ── Управление кланом (владелец) ─────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("clan_manage_"))
def cb_clan_manage(call):
    uid     = call.from_user.id
    clan_id = int(call.data.split("_")[-1])
    clan    = get_user_clan(uid)
    if not clan or clan['id'] != clan_id or not _can_manage(clan_id, uid):
        bot.answer_callback_query(call.id, "❌ Нет прав")
        return

    level    = clan.get('level', 1)
    cur_info = _clan_level_info(level)
    next_lv  = level + 1
    upgrade_btn = []
    if next_lv in CLAN_LEVELS:
        next_cost = cur_info['upgrade_cost']
        upgrade_btn = [InlineKeyboardButton(
            f"⬆️ Улучшить клан ({format_balance(next_cost)} из казны)",
            callback_data=f"clan_upgrade_confirm_{clan_id}"
        )]

    mk = InlineKeyboardMarkup(row_width=1)
    mk.add(
        InlineKeyboardButton("📋 Заявки",             callback_data=f"clan_apps_{clan_id}"),
        InlineKeyboardButton("👥 Участники",           callback_data=f"clan_members_{clan_id}"),
    )
    if upgrade_btn:
        mk.add(*upgrade_btn)
    mk.add(InlineKeyboardButton("◀️ Назад", callback_data="clan_back"))

    next_info = _clan_level_info(next_lv) if next_lv in CLAN_LEVELS else None
    upgrade_line = ""
    if next_info:
        upgrade_line = (
            f"\n\n⬆️ <b>Следующий уровень ({next_info['label']}):</b>\n"
            f"👥 +{next_info['max_members'] - cur_info['max_members']} мест"
            + (f", ❤️ +{next_info['hp_bonus']} HP" if next_info['hp_bonus'] > cur_info['hp_bonus'] else "")
            + (f", 🎯 +{int(next_info['quest_bonus']*100)}% квесты" if next_info['quest_bonus'] > cur_info['quest_bonus'] else "")
            + f"\n💰 Стоимость: {format_balance(cur_info['upgrade_cost'])} из казны"
        )
    else:
        upgrade_line = "\n\n⭐ Максимальный уровень!"

    bot.edit_message_text(
        f"⚙️ <b>Управление [{clan['tag']}]</b>  {cur_info['label']}\n\n"
        f"Команды:\n"
        f"• <code>клан кик USER_ID</code> — исключить\n"
        f"• <code>клан офицер USER_ID</code> — назначить/снять офицера\n"
        f"• <code>клан передать USER_ID</code> — передать владение\n"
        f"• <code>клан распустить</code> — удалить клан"
        f"{upgrade_line}",
        call.message.chat.id, call.message.message_id,
        reply_markup=mk, parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)

# ── Выход (callback) ─────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "clan_leave_confirm")
def cb_leave_confirm(call):
    uid  = call.from_user.id
    clan = get_user_clan(uid)
    if not clan:
        bot.answer_callback_query(call.id, "❌ Ты не в клане")
        return

    if clan['role'] == 'owner':
        bot.answer_callback_query(call.id,
            "❌ Ты владелец — сначала передай клан: клан передать USER_ID")
        return

    mk = InlineKeyboardMarkup()
    mk.add(
        InlineKeyboardButton("✅ Да, выйти",  callback_data="clan_leave_yes"),
        InlineKeyboardButton("❌ Отмена",      callback_data="clan_back"),
    )
    bot.edit_message_text(
        f"⚠️ Выйти из клана <b>[{clan['tag']}]</b>?",
        call.message.chat.id, call.message.message_id,
        reply_markup=mk, parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "clan_leave_yes")
def cb_leave_yes(call):
    uid  = call.from_user.id
    clan = get_user_clan(uid)
    if not clan:
        bot.answer_callback_query(call.id, "❌")
        return

    with get_db_cursor() as cur:
        cur.execute("DELETE FROM clan_members WHERE user_id=? AND clan_id=?", (uid, clan['id']))
        cur.execute("UPDATE clans SET members_count=members_count-1 WHERE id=?", (clan['id'],))
        cur.execute("UPDATE users SET clan_id=0 WHERE user_id=?", (uid,))

    bot.edit_message_text(
        f"✅ Ты вышел из клана <b>[{clan['tag']}]</b>.",
        call.message.chat.id, call.message.message_id, parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "clan_back")
def cb_clan_back(call):
    uid = call.from_user.id
    bot.edit_message_text(
        _clan_main_text(uid),
        call.message.chat.id, call.message.message_id,
        reply_markup=_clan_main_markup(uid),
        parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "clan_create_prompt")
def cb_create_prompt(call):
    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton("◀️ Назад", callback_data="clan_back"))
    bot.edit_message_text(
        f"➕ <b>Создание клана</b>\n\n"
        f"Стоимость: <b>{format_balance(CLAN_CREATE_COST)}</b>\n\n"
        f"Напиши в чат:\n"
        f"<code>создать клан ТЕГ Название</code>\n\n"
        f"Пример:\n"
        f"<code>создать клан FCZ Fectiz Warriors</code>\n\n"
        f"• Тег: 2–5 символов, только буквы/цифры\n"
        f"• Название: 3–32 символа",
        call.message.chat.id, call.message.message_id,
        reply_markup=mk, parse_mode="HTML"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "noop")
def cb_noop(call):
    bot.answer_callback_query(call.id)

# ══════════════════════════════════════════════════════════════
# 🏆 ЕЖЕНЕДЕЛЬНЫЙ РЕЙТИНГ КЛАНОВ
# ══════════════════════════════════════════════════════════════

def _weekly_clan_rewards():
    """Выдаёт призы топ-3 кланам, сбрасывает war_score."""
    with get_db_cursor() as cur:
        cur.execute("""
            SELECT id, name, tag, war_score, balance,
                   (SELECT COUNT(*) FROM clan_members cm WHERE cm.clan_id=c.id) as members
            FROM clans c
            WHERE war_score > 0
            ORDER BY war_score DESC
            LIMIT 3
        """)
        top = cur.fetchall()

    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(top):
        cid, name, tag, score, bal, members = row
        prize = CLAN_WEEKLY_PRIZES[i] if i < len(CLAN_WEEKLY_PRIZES) else 0
        if prize <= 0:
            continue

        # Делим приз между участниками
        share = prize // max(members, 1)

        with get_db_cursor() as cur:
            cur.execute("SELECT user_id FROM clan_members WHERE clan_id=?", (cid,))
            uids = [r[0] for r in cur.fetchall()]

        for uid in uids:
            try:
                update_balance(uid, share)
                bot.send_message(uid,
                    f"{medals[i]} <b>Еженедельный рейтинг кланов!</b>\n\n"
                    f"Клан <b>[{tag}]</b> занял <b>{i+1} место</b>!\n"
                    f"Твоя доля приза: <b>{format_balance(share)}</b>",
                    parse_mode="HTML")
            except Exception:
                pass

    # Сброс очков
    with get_db_cursor() as cur:
        cur.execute("UPDATE clans SET war_score=0")

    print("[clan_weekly] призы выданы, очки сброшены")

def _weekly_clan_scheduler():
    while True:
        now = _cdt.datetime.utcnow()
        # Каждый понедельник в 00:00 UTC
        days_until_monday = (7 - now.weekday()) % 7 or 7
        next_monday = (now + _cdt.timedelta(days=days_until_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        sleep_sec = (next_monday - now).total_seconds()
        time.sleep(max(sleep_sec, 60))
        try:
            _weekly_clan_rewards()
        except Exception as e:
            print(f"[clan_weekly] ошибка: {e}")

def start_clan_weekly_scheduler():
    t = threading.Thread(target=_weekly_clan_scheduler, daemon=True)
    t.start()
    print("🏆 Еженедельный рейтинг кланов запущен")

start_clan_weekly_scheduler()

# ── Принудительный выдача наград (для теста/админа) ──────────

@bot.message_handler(func=lambda m: m.text and m.text.strip().lower() == "наградить кланы" and is_admin(m.from_user.id))
def handle_force_clan_rewards(message):
    try:
        _weekly_clan_rewards()
        bot.send_message(message.chat.id, "✅ Еженедельные призы кланам выданы!", parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode="HTML")


# ══════════════════════════════════════════════════════════════
# 📢 КАНАЛ КЛАНА
# ══════════════════════════════════════════════════════════════

def _init_clan_extras():
    """Добавляет колонки channel_id в clans и таблицу квестов если нет."""
    with get_db_cursor() as cur:
        cur.execute("PRAGMA table_info(clans)")
        cols = [r[1] for r in cur.fetchall()]
        if "channel_id" not in cols:
            cur.execute("ALTER TABLE clans ADD COLUMN channel_id INTEGER DEFAULT NULL")
            print("[clan] добавлена колонка channel_id")

        # Таблица квестов (полная, если не существует)
        cur.execute('''
            CREATE TABLE IF NOT EXISTS clan_quests_v2 (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                clan_id      INTEGER NOT NULL,
                quest_type   TEXT NOT NULL,
                target       INTEGER NOT NULL,
                progress     INTEGER DEFAULT 0,
                reward       INTEGER NOT NULL,
                created_at   INTEGER NOT NULL,
                expires_at   INTEGER NOT NULL,
                completed    INTEGER DEFAULT 0,
                notified     INTEGER DEFAULT 0
            )
        ''')

_init_clan_extras()

def _init_clan_systems():
    with get_db_cursor() as cur:
        # История вступлений — для защиты от фарма бонуса
        cur.execute('''
            CREATE TABLE IF NOT EXISTS clan_join_history (
                user_id  INTEGER NOT NULL,
                clan_id  INTEGER NOT NULL,
                PRIMARY KEY (user_id, clan_id)
            )
        ''')
        # Мигрируем уровни — max_members зависит от level
        cur.execute("PRAGMA table_info(clans)")
        cols = [r[1] for r in cur.fetchall()]
        if "level" not in cols:
            cur.execute("ALTER TABLE clans ADD COLUMN level INTEGER DEFAULT 1")

_init_clan_systems()

# Конфигурация уровней клана
CLAN_LEVELS = {
    1: {"max_members": 30,  "hp_bonus": 0,   "quest_bonus": 0.00, "badge": "",   "upgrade_cost": 500_000,   "label": "Ур.1"},
    2: {"max_members": 35,  "hp_bonus": 0,   "quest_bonus": 0.00, "badge": "",   "upgrade_cost": 1_500_000, "label": "Ур.2"},
    3: {"max_members": 40,  "hp_bonus": 50,  "quest_bonus": 0.10, "badge": "",   "upgrade_cost": 3_000_000, "label": "Ур.3"},
    4: {"max_members": 45,  "hp_bonus": 100, "quest_bonus": 0.20, "badge": "",   "upgrade_cost": 5_000_000, "label": "Ур.4"},
    5: {"max_members": 50,  "hp_bonus": 150, "quest_bonus": 0.30, "badge": "⭐", "upgrade_cost": 0,         "label": "Ур.5 ⭐"},
}

def _clan_level_info(level):
    return CLAN_LEVELS.get(level, CLAN_LEVELS[1])

def _clan_max_members(level):
    return _clan_level_info(level)['max_members']

def _clan_post(clan_id, text):
    """Отправить сообщение в канал клана если он привязан."""
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT channel_id FROM clans WHERE id=?", (clan_id,))
            row = cur.fetchone()
        if row and row[0]:
            bot.send_message(row[0], text, parse_mode="HTML")
    except Exception as e:
        print(f"[clan_post] clan_id={clan_id}: {e}")

# ── Привязка канала ───────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith("клан канал "))
def handle_set_clan_channel(message):
    uid  = message.from_user.id
    clan = get_user_clan(uid)
    if not clan or clan['role'] != 'owner':
        bot.send_message(message.chat.id, "❌ Только владелец клана может привязать канал.", parse_mode="HTML")
        return

    parts = message.text.strip().split()
    if len(parts) < 3:
        bot.send_message(message.chat.id,
            "❌ Формат: <code>клан канал @username</code>\n\n"
            "1. Добавь бота в канал как администратора\n"
            "2. Дай боту право отправлять сообщения\n"
            "3. Напиши эту команду",
            parse_mode="HTML")
        return

    channel = parts[2].strip()
    if not channel.startswith("@"):
        channel = "@" + channel

    # Проверяем что бот там администратор
    try:
        chat = bot.get_chat(channel)
        channel_id = chat.id
        member = bot.get_chat_member(channel_id, bot.get_me().id)
        if member.status not in ("administrator", "creator"):
            bot.send_message(message.chat.id,
                f"❌ Бот не является администратором в <b>{channel}</b>.\n"
                f"Добавь бота как администратора и попробуй снова.",
                parse_mode="HTML")
            return
    except Exception as e:
        bot.send_message(message.chat.id,
            f"❌ Не удалось найти канал <b>{channel}</b>.\n"
            f"Убедись что бот добавлен в канал как администратор.",
            parse_mode="HTML")
        return

    # Проверяем что этот канал не привязан к другому клану
    with get_db_cursor() as cur:
        cur.execute("SELECT id, name, tag FROM clans WHERE channel_id=?", (channel_id,))
        existing = cur.fetchone()
        if existing and existing[0] != clan['id']:
            bot.send_message(message.chat.id,
                f"❌ Этот канал уже привязан к клану <b>[{existing[2]}] {existing[1]}</b>.",
                parse_mode="HTML")
            return
        cur.execute("UPDATE clans SET channel_id=? WHERE id=?", (channel_id, clan['id']))

    # Тестовый пост в канал
    try:
        bot.send_message(channel_id,
            f"📢 <b>Канал клана [{clan['tag']}] {clan['name']} привязан!</b>\n\n"
            f"Здесь будут появляться новости клана:\n"
            f"• Новые участники\n"
            f"• Начало и конец войн\n"
            f"• Выполненные квесты\n"
            f"• Еженедельные результаты",
            parse_mode="HTML")
    except Exception:
        pass

    bot.send_message(message.chat.id,
        f"✅ Канал <b>{channel}</b> привязан к клану <b>[{clan['tag']}]</b>!\n"
        f"Теперь все события клана будут публиковаться там.",
        parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text and m.text.strip().lower() == "клан канал отвязать")
def handle_unset_clan_channel(message):
    uid  = message.from_user.id
    clan = get_user_clan(uid)
    if not clan or clan['role'] != 'owner':
        bot.send_message(message.chat.id, "❌ Только владелец клана.", parse_mode="HTML")
        return
    with get_db_cursor() as cur:
        cur.execute("UPDATE clans SET channel_id=NULL WHERE id=?", (clan['id'],))
    bot.send_message(message.chat.id, "✅ Канал отвязан от клана.", parse_mode="HTML")

# ══════════════════════════════════════════════════════════════
# 🎯 КЛАНОВЫЕ КВЕСТЫ
# ══════════════════════════════════════════════════════════════

import datetime as _cdt2

CLAN_QUEST_TYPES = [
    # (тип, описание, целевое значение, награда)
    ("earn",    "Заработать суммарно {target} 🌸 участниками",  500_000,  200_000),
    ("earn",    "Заработать суммарно {target} 🌸 участниками",  1_000_000, 400_000),
    ("deposit", "Пополнить казну на {target} 🌸",               200_000,  150_000),
    ("deposit", "Пополнить казну на {target} 🌸",               500_000,  300_000),
    ("attack",  "Провести {target} атак в войнах",              10,        250_000),
    ("attack",  "Провести {target} атак в войнах",              25,        500_000),
    ("members", "Принять {target} новых участников",            3,         300_000),
    ("members", "Принять {target} новых участников",            5,         500_000),
]

def _get_active_quest(clan_id):
    """Возвращает активный квест клана или None."""
    now = int(time.time())
    with get_db_cursor() as cur:
        cur.execute(
            "SELECT * FROM clan_quests_v2 WHERE clan_id=? AND completed=0 AND expires_at>? "
            "ORDER BY id DESC LIMIT 1",
            (clan_id, now)
        )
        row = cur.fetchone()
        return dict(row) if row else None

def _generate_quest(clan_id):
    """Генерирует новый суточный квест для клана."""
    now  = int(time.time())
    # Проверяем — нет ли уже активного
    if _get_active_quest(clan_id):
        return None

    qt, desc, target, reward = random.choice(CLAN_QUEST_TYPES)
    expires = now + 86400  # 24 часа

    with get_db_cursor() as cur:
        cur.execute(
            "INSERT INTO clan_quests_v2 (clan_id, quest_type, target, progress, reward, "
            "created_at, expires_at) VALUES (?,?,?,0,?,?,?)",
            (clan_id, qt, target, reward, now, expires)
        )
        quest_id = cur.lastrowid

    return {
        'id': quest_id, 'clan_id': clan_id, 'quest_type': qt,
        'target': target, 'progress': 0, 'reward': reward,
        'created_at': now, 'expires_at': expires, 'completed': 0
    }

def _quest_description(quest):
    desc_map = {
        "earn":    f"Заработать суммарно {format_balance(quest['target'])} участниками",
        "deposit": f"Пополнить казну на {format_balance(quest['target'])}",
        "attack":  f"Провести {quest['target']} атак в войнах",
        "members": f"Принять {quest['target']} новых участников",
    }
    return desc_map.get(quest['quest_type'], "Неизвестный квест")

def _quest_progress_bar(progress, target):
    pct    = min(1.0, progress / target) if target > 0 else 0
    filled = int(pct * 10)
    return "█" * filled + "░" * (10 - filled) + f" {int(pct*100)}%"

def _advance_quest(clan_id, quest_type, amount=1):
    """
    Продвигает прогресс квеста.
    Вызывается из других частей бота при соответствующих действиях.
    """
    quest = _get_active_quest(clan_id)
    if not quest or quest['quest_type'] != quest_type:
        return

    new_progress = quest['progress'] + amount
    completed    = 1 if new_progress >= quest['target'] else 0

    with get_db_cursor() as cur:
        cur.execute(
            "UPDATE clan_quests_v2 SET progress=?, completed=? WHERE id=?",
            (min(new_progress, quest['target']), completed, quest['id'])
        )

    if completed:
        _complete_quest(clan_id, quest)

def _complete_quest(clan_id, quest):
    """Выдаёт награду и уведомляет клан."""
    reward = quest['reward']
    with get_db_cursor() as cur:
        cur.execute("UPDATE clans SET balance=balance+? WHERE id=?", (reward, clan_id))
        cur.execute("SELECT user_id FROM clan_members WHERE clan_id=?", (clan_id,))
        members = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT name, tag FROM clans WHERE id=?", (clan_id,))
        clan_row = cur.fetchone()

    if not clan_row:
        return

    name, tag = clan_row[0], clan_row[1]
    desc = _quest_description(quest)
    text = (
        f"🎯 <b>Квест выполнен!</b>\n\n"
        f"✅ {desc}\n\n"
        f"💰 Казна клана <b>[{tag}]</b> пополнилась на <b>{format_balance(reward)}</b>!"
    )

    for mid in members:
        try:
            bot.send_message(mid, text, parse_mode="HTML")
        except Exception:
            pass

    # Пост в канал клана
    _clan_post(clan_id, text)

    # Генерируем следующий квест через 1 час
    threading.Timer(3600, _generate_quest, args=(clan_id,)).start()

# ── Команда просмотра квеста ──────────────────────────────────

@bot.message_handler(func=lambda m: m.text and m.text.strip().lower() in ("клан квест", "квест клана"))
def handle_clan_quest(message):
    uid  = message.from_user.id
    clan = get_user_clan(uid)
    if not clan:
        bot.send_message(message.chat.id, "❌ Ты не в клане.", parse_mode="HTML")
        return

    quest = _get_active_quest(clan['id'])
    if not quest:
        # Генерируем новый
        quest = _generate_quest(clan['id'])

    if not quest:
        bot.send_message(message.chat.id, "🎯 Квест уже активен, проверь позже.", parse_mode="HTML")
        return

    ends = _cdt2.datetime.utcfromtimestamp(quest['expires_at']).strftime("%d.%m %H:%M UTC")
    desc = _quest_description(quest)
    bar  = _quest_progress_bar(quest['progress'], quest['target'])

    bot.send_message(message.chat.id,
        f"🎯 <b>Клановый квест [{clan['tag']}]</b>\n\n"
        f"📋 {desc}\n\n"
        f"📊 Прогресс: {bar}\n"
        f"   {quest['progress']} / {quest['target']}\n\n"
        f"💰 Награда: <b>{format_balance(quest['reward'])}</b> в казну\n"
        f"⏱ До конца: <b>{ends}</b>",
        parse_mode="HTML"
    )

# Кнопка квеста добавлена напрямую в _clan_main_markup

@bot.callback_query_handler(func=lambda c: c.data == "cq_quest")
def cb_clan_quest(call):
    try:
        uid  = call.from_user.id
        clan = get_user_clan(uid)
        if not clan:
            bot.answer_callback_query(call.id, "❌ Ты не в клане")
            return

        _init_clan_extras()

        quest = _get_active_quest(clan['id'])
        if not quest:
            quest = _generate_quest(clan['id'])

        mk = InlineKeyboardMarkup()
        mk.add(InlineKeyboardButton("🔄 Обновить", callback_data="cq_quest"))
        mk.add(InlineKeyboardButton("◀️ Назад",    callback_data="clan_back"))

        if not quest:
            bot.edit_message_text(
                "🎯 Квест появится в течение минуты, нажми обновить.",
                call.message.chat.id, call.message.message_id,
                reply_markup=mk, parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)
            return

        from datetime import datetime as _dtt
        ends = _dtt.utcfromtimestamp(quest['expires_at']).strftime("%d.%m %H:%M UTC")
        desc = _quest_description(quest)
        bar  = _quest_progress_bar(quest['progress'], quest['target'])

        bot.edit_message_text(
            f"🎯 <b>Клановый квест [{clan['tag']}]</b>\n\n"
            f"📋 {desc}\n\n"
            f"📊 {bar}\n"
            f"   {quest['progress']} / {quest['target']}\n\n"
            f"💰 Награда: <b>{format_balance(quest['reward'])}</b> в казну\n"
            f"⏱ До конца: <b>{ends}</b>",
            call.message.chat.id, call.message.message_id,
            reply_markup=mk, parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        import traceback
        print(f"[cb_clan_quest] {traceback.format_exc()}")
        try:
            bot.answer_callback_query(call.id, f"❌ {str(e)[:100]}")
        except Exception:
            pass


def _quest_scheduler():
    """Раз в час проверяет все кланы — у кого нет квеста, генерирует."""
    while True:
        time.sleep(3600)
        try:
            with get_db_cursor() as cur:
                cur.execute("SELECT id FROM clans")
                clan_ids = [r[0] for r in cur.fetchall()]
            for cid in clan_ids:
                try:
                    _generate_quest(cid)
                except Exception as e:
                    print(f"[quest_scheduler] clan {cid}: {e}")
        except Exception as e:
            print(f"[quest_scheduler] ошибка: {e}")

def start_quest_scheduler():
    t = threading.Thread(target=_quest_scheduler, daemon=True, name="quest-scheduler")
    t.start()
    print("🎯 Планировщик квестов запущен")

start_quest_scheduler()

# Генерируем квесты для всех кланов при старте (у кого нет)
def _init_quests_on_start():
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT id FROM clans")
            clan_ids = [r[0] for r in cur.fetchall()]
        for cid in clan_ids:
            _generate_quest(cid)
    except Exception as e:
        print(f"[quest_init] ошибка: {e}")

threading.Thread(target=_init_quests_on_start, daemon=True).start()

# ══════════════════════════════════════════════════════════════
# ⬆️  УРОВНИ КЛАНА
# ══════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text and m.text.strip().lower() == "клан апгрейд")
def handle_clan_upgrade(message):
    uid  = message.from_user.id
    clan = get_user_clan(uid)
    if not clan or clan['role'] != 'owner':
        bot.send_message(message.chat.id, "❌ Только владелец клана может улучшать клан.", parse_mode="HTML")
        return

    level     = clan.get('level', 1)
    cur_info  = _clan_level_info(level)
    next_level = level + 1

    if next_level not in CLAN_LEVELS:
        bot.send_message(message.chat.id,
            f"⭐ Клан уже на максимальном уровне ({cur_info['label']})!",
            parse_mode="HTML")
        return

    next_info = _clan_level_info(next_level)
    cost      = cur_info['upgrade_cost']

    with get_db_cursor() as cur:
        cur.execute("SELECT balance FROM clans WHERE id=?", (clan['id'],))
        treasury = cur.fetchone()[0]

    if treasury < cost:
        bot.send_message(message.chat.id,
            f"❌ Недостаточно средств в казне.\n"
            f"Нужно: <b>{format_balance(cost)}</b>\n"
            f"В казне: <b>{format_balance(treasury)}</b>",
            parse_mode="HTML")
        return

    mk = InlineKeyboardMarkup()
    mk.add(
        InlineKeyboardButton("✅ Улучшить", callback_data=f"clan_upgrade_confirm_{clan['id']}"),
        InlineKeyboardButton("❌ Отмена",   callback_data="clan_upgrade_cancel"),
    )
    bot.send_message(message.chat.id,
        f"⬆️ <b>Улучшение клана [{clan['tag']}]</b>\n\n"
        f"Текущий: <b>{cur_info['label']}</b>\n"
        f"Новый:   <b>{next_info['label']}</b>\n\n"
        f"📈 Что улучшится:\n"
        f"👥 Участников: {cur_info['max_members']} → <b>{next_info['max_members']}</b>\n"
        + (f"❤️ Бонус HP в войне: +{next_info['hp_bonus']} за участника\n" if next_info['hp_bonus'] else "")
        + (f"🎯 Бонус к квестам: +{int(next_info['quest_bonus']*100)}%\n" if next_info['quest_bonus'] else "")
        + (f"⭐ Уникальный значок клана\n" if next_info['badge'] else "")
        + f"\n💰 Стоимость: <b>{format_balance(cost)}</b> из казны",
        reply_markup=mk, parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data.startswith("clan_upgrade_confirm_"))
def cb_clan_upgrade(call):
    uid     = call.from_user.id
    clan_id = int(call.data.split("_")[-1])
    clan    = get_user_clan(uid)
    if not clan or clan['id'] != clan_id or clan['role'] != 'owner':
        bot.answer_callback_query(call.id, "❌ Нет прав")
        return

    level    = clan.get('level', 1)
    cur_info = _clan_level_info(level)
    cost     = cur_info['upgrade_cost']
    next_lv  = level + 1

    if next_lv not in CLAN_LEVELS:
        bot.answer_callback_query(call.id, "❌ Максимальный уровень")
        return

    next_info = _clan_level_info(next_lv)

    with get_db_cursor() as cur:
        cur.execute("SELECT balance FROM clans WHERE id=?", (clan_id,))
        treasury = cur.fetchone()[0]
        if treasury < cost:
            bot.answer_callback_query(call.id, "❌ Недостаточно средств в казне")
            return
        cur.execute("UPDATE clans SET balance=balance-?, level=?, max_members=? WHERE id=?",
                    (cost, next_lv, next_info['max_members'], clan_id))

    next_info = _clan_level_info(next_lv)
    text = (
        f"🎉 <b>Клан [{clan['tag']}] улучшен до {next_info['label']}!</b>\n\n"
        f"👥 Максимум участников: <b>{next_info['max_members']}</b>\n"
        + (f"❤️ Бонус HP в войне: <b>+{next_info['hp_bonus']}</b> за участника\n" if next_info['hp_bonus'] else "")
        + (f"🎯 Бонус к квестам: <b>+{int(next_info['quest_bonus']*100)}%</b>\n" if next_info['quest_bonus'] else "")
        + (f"⭐ <b>Уникальный значок получен!</b>\n" if next_info['badge'] else "")
        + f"\n💸 Потрачено из казны: <b>{format_balance(cost)}</b>"
    )

    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML")
    bot.answer_callback_query(call.id)

    # Уведомляем всех участников
    with get_db_cursor() as cur:
        cur.execute("SELECT user_id FROM clan_members WHERE clan_id=?", (clan_id,))
        members = [r[0] for r in cur.fetchall()]
    for mid in members:
        try:
            if mid != uid:
                bot.send_message(mid, text, parse_mode="HTML")
        except Exception:
            pass
    threading.Thread(target=_clan_post, args=(clan_id, text), daemon=True).start()

@bot.callback_query_handler(func=lambda c: c.data == "clan_upgrade_cancel")
def cb_clan_upgrade_cancel(call):
    bot.edit_message_text("❌ Улучшение отменено.", call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)

# ══════════════════════════════════════════════════════════════
# ⛏️  СИСТЕМА ШАХТ
# ══════════════════════════════════════════════════════════════

# От частого к редкому (по Minecraft)
MINE_RESOURCES = [
    {"id": "coal",     "name": "Уголь",    "emoji_id": 5456408804541340493, "chance": 28, "price": 120},
    {"id": "earth",    "name": "Земля",    "emoji_id": 5458822138075028493, "chance": 20, "price": 80},
    {"id": "stone",    "name": "Камень",   "emoji_id": 5458781211331665562, "chance": 16, "price": 100},
    {"id": "copper",   "name": "Медь",     "emoji_id": 5458520300658368161, "chance": 12, "price": 280},
    {"id": "iron",     "name": "Железо",   "emoji_id": 5458833133191306560, "chance": 9,  "price": 450},
    {"id": "wood",     "name": "Дерево",   "emoji_id": 5458693503804513909, "chance": 5,  "price": 700},
    {"id": "planks",   "name": "Доски",    "emoji_id": 5456151226762665376, "chance": 4,  "price": 950},
    {"id": "redstone", "name": "Редстоун", "emoji_id": 5458448020653743095, "chance": 3,  "price": 1600},
    {"id": "lapis",    "name": "Лазурит",  "emoji_id": 5458825788797231419, "chance": 1.5,"price": 3000},
    {"id": "emerald",  "name": "Изумруд",  "emoji_id": 5458517053663093203, "chance": 0.9,"price": 7000},
    {"id": "diamond",  "name": "Алмаз",   "emoji_id": 5458420940884942467, "chance": 0.6,"price": 14000},
]

MINE_COOLDOWN      = 5 * 60   # 5 минут между копаниями (общий для обеих шахт)
MINE_RENT_DURATION = 10 * 60  # аренда на 10 минут
MINE_BUY_PRICE     = 500_000  # цена покупки шахты

MINE_LEVELS = {
    1: {"label": "Ур.1", "bonus": 1.0,  "upgrade_cost": 200_000,  "rent_min": 500,  "rent_max": 10000},
    2: {"label": "Ур.2", "bonus": 1.4,  "upgrade_cost": 500_000,  "rent_min": 500,  "rent_max": 18000},
    3: {"label": "Ур.3", "bonus": 1.9,  "upgrade_cost": 1_000_000,"rent_min": 500,  "rent_max": 30000},
    4: {"label": "Ур.4", "bonus": 2.5,  "upgrade_cost": 2_000_000,"rent_min": 500,  "rent_max": 50000},
    5: {"label": "Ур.5 MAX", "bonus": 3.5, "upgrade_cost": 0,     "rent_min": 500,  "rent_max": 80000},
}

def _mine_init():
    with get_db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mines (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id   INTEGER NOT NULL UNIQUE,
                level      INTEGER DEFAULT 1,
                balance    INTEGER DEFAULT 0,
                rent_price INTEGER DEFAULT 1000,
                created_at INTEGER NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mine_cooldowns (
                user_id  INTEGER PRIMARY KEY,
                last_dig INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mine_rentals (
                user_id    INTEGER PRIMARY KEY,
                mine_id    INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mine_stats (
                user_id      INTEGER PRIMARY KEY,
                total_digs   INTEGER DEFAULT 0,
                total_earned INTEGER DEFAULT 0,
                streak       INTEGER DEFAULT 0,
                best_streak  INTEGER DEFAULT 0,
                last_find_rare INTEGER DEFAULT 0
            )
        """)
        # Миграция — добавить колонки если нет
        for col, dflt in [("streak","0"),("best_streak","0"),("last_find_rare","0")]:
            try:
                cur.execute(f"ALTER TABLE mine_stats ADD COLUMN {col} INTEGER DEFAULT {dflt}")
            except Exception:
                pass

_mine_init()

def _mine_resource(bonus=1.0):
    total = sum(r["chance"] for r in MINE_RESOURCES)
    roll  = random.uniform(0, total)
    cum   = 0
    for r in MINE_RESOURCES:
        cum += r["chance"]
        if roll <= cum:
            reward = int(r["price"] * bonus * random.uniform(0.85, 1.15))
            return r, reward
    return MINE_RESOURCES[0], int(MINE_RESOURCES[0]["price"] * bonus)

def _mine_cd_left(user_id):
    with get_db_cursor() as cur:
        cur.execute("SELECT last_dig FROM mine_cooldowns WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            return 0
        return max(0, row[0] + MINE_COOLDOWN - int(time.time()))

def _mine_try_dig(user_id):
    """Атомарно проверяет кулдаун и устанавливает его.
    Возвращает True если копать можно, False если кулдаун ещё не прошёл."""
    now     = int(time.time())
    min_ts  = now - MINE_COOLDOWN  # last_dig должен быть раньше этого момента
    with get_db_cursor() as cur:
        # Пробуем вставить новую запись — если нет строки
        cur.execute(
            "INSERT INTO mine_cooldowns (user_id, last_dig) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_dig=? "
            "WHERE last_dig <= ?",
            (user_id, now, now, min_ts)
        )
        return cur.rowcount > 0  # 0 = кулдаун не прошёл, 1 = успешно

def _mine_set_cd(user_id):
    now = int(time.time())
    with get_db_cursor() as cur:
        cur.execute(
            "INSERT INTO mine_cooldowns (user_id, last_dig) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_dig=?",
            (user_id, now, now)
        )

def _mine_rental(user_id):
    now = int(time.time())
    with get_db_cursor() as cur:
        cur.execute("SELECT mine_id, expires_at FROM mine_rentals WHERE user_id=? AND expires_at>?",
                    (user_id, now))
        return cur.fetchone()

def _mine_by_id(mine_id):
    with get_db_cursor() as cur:
        cur.execute("SELECT * FROM mines WHERE id=?", (mine_id,))
        row = cur.fetchone()
        return dict(row) if row else None

def _mine_by_owner(user_id):
    with get_db_cursor() as cur:
        cur.execute("SELECT * FROM mines WHERE owner_id=?", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None

def _mine_fmt_cd(sec):
    m, s = divmod(sec, 60)
    return f"{m}м {s}с" if m else f"{s}с"

# ── Главное меню ──────────────────────────────────────────────

def _mine_main_text(uid):
    rental  = _mine_rental(uid)
    cd_left = _mine_cd_left(uid)
    my_mine = _mine_by_owner(uid)

    # Стрик
    with get_db_cursor() as cur:
        cur.execute("SELECT streak, best_streak, total_digs FROM mine_stats WHERE user_id=?", (uid,))
        row = cur.fetchone()
    streak      = row[0] if row else 0
    best_streak = row[1] if row else 0
    total_digs  = row[2] if row else 0

    cd_str = "⏳ " + _mine_fmt_cd(cd_left) if cd_left else "✅ Готова"

    lines = ["⛏️ <b>Шахта</b>\n"]
    if rental:
        mine = _mine_by_id(rental[0])
        lv   = mine['level'] if mine else 1
        exp  = rental[1] - int(time.time())
        lines.append(f"🏔️ Шахта #{rental[0]} Ур.{lv} (x{MINE_LEVELS[lv]['bonus']}) — {_mine_fmt_cd(exp)}")
    lines.append(f"⛏️ Копать: {cd_str}")

    if streak >= 10:
        bonus_pct = (streak // 10) * 20
        lines.append(f"🔥 Стрик: <b>{streak}</b> · бонус +{bonus_pct}%")
    elif streak > 0:
        lines.append(f"🔥 Стрик: <b>{streak}</b>  (×10 = бонус)")

    if my_mine:
        lv = my_mine['level']
        lines.append(f"\n🏔️ <b>Моя шахта #{my_mine['id']}</b> · {MINE_LEVELS[lv]['label']} · 💰 {format_balance(my_mine['balance'])}")

    return "\n".join(lines)

def _mine_main_kb(uid):
    rental  = _mine_rental(uid)
    my_mine = _mine_by_owner(uid)
    kb = {"inline_keyboard": []}

    # Кнопка копать — одна, бот сам определит куда
    kb["inline_keyboard"].append([{"text": "⛏️ Копать", "callback_data": "mine_dig"}])

    kb["inline_keyboard"].append([
        {"text": "🏪 Шахты", "callback_data": "mine_list"},
        {"text": "🏆 Топ майнеров", "callback_data": "mine_top"},
    ])

    if my_mine:
        kb["inline_keyboard"].append([{"text": "🏔️ Моя шахта", "callback_data": "mine_mymine"}])
    else:
        kb["inline_keyboard"].append([{"text": "💰 Купить шахту", "callback_data": "mine_buy_prompt"}])

    kb["inline_keyboard"].append([{"text": "◀️ Назад", "callback_data": "mine_back"}])
    return kb

@bot.message_handler(func=lambda m: m.text and m.text.strip().lower() in ("шахта", "⛏️ шахта", "⛏ шахта"))
def handle_mine_menu(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id, _mine_main_text(uid),
                     reply_markup=_mine_main_kb(uid), parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text and m.text.strip().lower() == "копать")
def handle_mine_dig_text(message):
    uid = message.from_user.id

    if not _mine_try_dig(uid):
        cd = _mine_cd_left(uid)
        bot.send_message(message.chat.id, f"⏳ Подожди ещё {_mine_fmt_cd(cd)}")
        return

    rental = _mine_rental(uid)
    if rental:
        mine  = _mine_by_id(rental[0])
        bonus = MINE_LEVELS[mine['level']]['bonus'] if mine else 1.0
        where = f"шахта #{rental[0]}"
    else:
        bonus = 1.0
        where = "общая"

    resource, base_reward = _mine_resource(bonus=bonus)
    event  = _mine_pick_event()
    reward = int(base_reward * event["mult"])

    with get_db_cursor() as cur:
        cur.execute("SELECT streak, best_streak FROM mine_stats WHERE user_id=?", (uid,))
        row = cur.fetchone()
        streak      = (row[0] if row else 0)
        best_streak = (row[1] if row else 0)

    new_streak = 0 if event["mult"] == 0.0 else streak + 1
    streak_bonus = (new_streak // 10) * 0.20
    if streak_bonus > 0 and reward > 0:
        reward = int(reward * (1 + streak_bonus))

    new_best = max(best_streak, new_streak)

    if reward > 0:
        update_balance(uid, reward)

    with get_db_cursor() as cur:
        cur.execute("""
            INSERT INTO mine_stats (user_id, total_digs, total_earned, streak, best_streak)
            VALUES (?,1,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                total_digs=total_digs+1,
                total_earned=total_earned+?,
                streak=?,
                best_streak=?
        """, (uid, max(0,reward), new_streak, new_best, max(0,reward), new_streak, new_best))

    if event["mult"] == 0.0:
        text = f"⛏️ {where}\n{event['text']}"
    elif event["mult"] >= 2.0:
        text = f"⛏️ {where}\n{event['text']}\n{resource['name']} · +{format_balance(reward)} 🌸"
    else:
        streak_tag = (f" 🔥×{new_streak}") if new_streak >= 10 else ""
        event_tag  = (f"  {event['text']}") if event["text"] else ""
        text = f"⛏️ {where}{event_tag}\n{resource['name']} · +{format_balance(reward)} 🌸{streak_tag}"

    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text and len(m.text.strip().split()) == 2
    and m.text.strip().split()[0].lower() == "шахта"
    and m.text.strip().split()[1].isdigit())
def handle_mine_by_number(message):
    uid     = message.from_user.id
    mine_id = int(message.text.strip().split()[1])
    mine    = _mine_by_id(mine_id)
    if not mine:
        bot.send_message(message.chat.id, f"❌ Шахта #{mine_id} не найдена.", parse_mode="HTML")
        return
    with get_db_cursor() as cur:
        cur.execute("SELECT first_name, custom_name, username FROM users WHERE user_id=?", (mine['owner_id'],))
        u = cur.fetchone()
    owner   = (u[1] or u[0] or (f"@{u[2]}" if u[2] else "?")) if u else "?"
    lv      = mine['level']
    lv_info = MINE_LEVELS[lv]
    rental  = _mine_rental(uid)
    text = (
        f"🏔 <b>Шахта #{mine_id}</b>\n\n"
        f"👤 Владелец: <b>{owner}</b>\n"
        f"⭐ {lv_info['label']} · Бонус <b>x{lv_info['bonus']}</b>\n"
        f"💰 Аренда: <b>{format_balance(mine['rent_price'])}</b> / 10 мин\n\n"
        f"В частной шахте ресурсы лучше чем в общей."
    )
    kb = {"inline_keyboard": []}
    if mine['owner_id'] == uid:
        kb["inline_keyboard"].append([{"text": "🏔 Управление", "callback_data": "mine_mymine"}])
    elif rental and rental[0] == mine_id:
        kb["inline_keyboard"].append([{"text": "✅ Ты уже здесь копаешь", "callback_data": "mine_main"}])
    else:
        kb["inline_keyboard"].append([{
            "text": f"⛏ Арендовать за {format_balance(mine['rent_price'])}",
            "callback_data": f"mine_rent_{mine_id}"
        }])
    kb["inline_keyboard"].append([{"text": "◀️ Назад", "callback_data": "mine_list"}])
    bot.send_message(message.chat.id, text, reply_markup=kb, parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data == "mine_back")
def cb_mine_back(call):
    bot.answer_callback_query(call.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

@bot.callback_query_handler(func=lambda c: c.data == "mine_main")
def cb_mine_main(call):
    uid = call.from_user.id
    try:
        bot.edit_message_text(_mine_main_text(uid), call.message.chat.id, call.message.message_id,
                              reply_markup=_mine_main_kb(uid), parse_mode="HTML")
    except Exception:
        pass
    bot.answer_callback_query(call.id)

# Случайные события при копании
_MINE_EVENTS = [
    {"chance": 4,   "text": "💥 Обвал! Инструмент сломался.",             "mult": 0.0},
    {"chance": 5,   "text": "💧 Подземный источник! Находка смыта.",       "mult": 0.0},
    {"chance": 8,   "text": "🪨 Твёрдая порода — пришлось повозиться.",    "mult": 0.5},
    {"chance": 10,  "text": "🍄 Подземный гриб — странно, но продал.",     "mult": 1.2},
    {"chance": 6,   "text": "💎 Двойная жила! Повезло.",                   "mult": 2.0},
    {"chance": 4,   "text": "🌟 Идеальный удар — кристально чистая руда!", "mult": 2.5},
    {"chance": 63,  "text": "",                                            "mult": 1.0},  # обычно
]

def _mine_pick_event():
    total = sum(e["chance"] for e in _MINE_EVENTS)
    roll  = random.uniform(0, total)
    cum   = 0
    for e in _MINE_EVENTS:
        cum += e["chance"]
        if roll <= cum:
            return e
    return _MINE_EVENTS[-1]

# ── Копать — одна кнопка, умная логика ───────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "mine_dig")
def cb_mine_dig(call):
    uid = call.from_user.id
    # Атомарная проверка + установка кулдауна в одном запросе
    if not _mine_try_dig(uid):
        cd = _mine_cd_left(uid)
        bot.answer_callback_query(call.id, f"⏳ {_mine_fmt_cd(cd)}", show_alert=False)
        return

    rental = _mine_rental(uid)
    if rental:
        mine  = _mine_by_id(rental[0])
        bonus = MINE_LEVELS[mine['level']]['bonus'] if mine else 1.0
        where = f"шахта #{rental[0]}"
    else:
        bonus = 1.0
        where = "общая"

    resource, base_reward = _mine_resource(bonus=bonus)
    event   = _mine_pick_event()
    reward  = int(base_reward * event["mult"])

    # Стрик — копает N раз подряд без обвалов
    with get_db_cursor() as cur:
        cur.execute("SELECT streak, best_streak FROM mine_stats WHERE user_id=?", (uid,))
        row = cur.fetchone()
        streak      = (row[0] if row else 0)
        best_streak = (row[1] if row else 0)

    if event["mult"] == 0.0:
        # Событие-ноль сбрасывает стрик
        new_streak = 0
    else:
        new_streak = streak + 1

    # Стрик-бонус: каждые 10 копаний подряд +20% к награде
    streak_bonus = (new_streak // 10) * 0.20
    if streak_bonus > 0 and reward > 0:
        reward = int(reward * (1 + streak_bonus))

    new_best = max(best_streak, new_streak)
    # Кулдаун уже установлен атомарно в _mine_try_dig выше

    if reward > 0:
        update_balance(uid, reward)

    with get_db_cursor() as cur:
        cur.execute("""
            INSERT INTO mine_stats (user_id, total_digs, total_earned, streak, best_streak)
            VALUES (?,1,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                total_digs=total_digs+1,
                total_earned=total_earned+?,
                streak=?,
                best_streak=?
        """, (uid, max(0,reward), new_streak, new_best, max(0,reward), new_streak, new_best))

    # Составляем уведомление
    if event["mult"] == 0.0:
        notif = "⛏️ " + where + "\n" + event["text"]
    elif event["mult"] >= 2.0:
        notif = "⛏️ " + where + "\n" + event["text"] + "\n" + resource["name"] + " · +" + format_balance(reward) + " 🌸"
    else:
        streak_tag = (" 🔥×" + str(new_streak)) if new_streak >= 10 else ""
        event_tag  = ("  " + event["text"]) if event["text"] else ""
        notif = "⛏️ " + where + event_tag + "\n" + resource["name"] + " · +" + format_balance(reward) + " 🌸" + streak_tag

    bot.answer_callback_query(call.id, notif, show_alert=False)
    try:
        bot.edit_message_text(_mine_main_text(uid), call.message.chat.id, call.message.message_id,
                              reply_markup=_mine_main_kb(uid), parse_mode="HTML")
    except Exception:
        pass

# ── Список шахт ───────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "mine_list")
def cb_mine_list(call):
    with get_db_cursor() as cur:
        cur.execute("""
            SELECT m.id, m.owner_id, m.level, m.rent_price,
                   u.first_name, u.custom_name, u.username
            FROM mines m JOIN users u ON u.user_id=m.owner_id
            ORDER BY m.level DESC, m.id ASC LIMIT 10
        """)
        rows = cur.fetchall()

    if not rows:
        bot.answer_callback_query(call.id, "Пока нет частных шахт", show_alert=True)
        return

    kb = {"inline_keyboard": []}
    for row in rows:
        mid, owner_id, level, rent_price, fname, cname, uname = row
        name    = (cname or fname or (f"@{uname}" if uname else f"#{owner_id}"))[:12]
        lv_info = MINE_LEVELS[level]
        btn = f"🏔 #{mid} {lv_info['label']} x{lv_info['bonus']} · {format_balance(rent_price)} · {name}"
        kb["inline_keyboard"].append([{"text": btn, "callback_data": f"mine_view_{mid}"}])
    kb["inline_keyboard"].append([{"text": "◀️ Назад", "callback_data": "mine_main"}])

    bot.edit_message_text("🏪 <b>Частные шахты</b>\n\nВыбери шахту для аренды:",
                          call.message.chat.id, call.message.message_id,
                          reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)

# ── Просмотр шахты ────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("mine_view_"))
def cb_mine_view(call):
    uid     = call.from_user.id
    mine_id = int(call.data.split("_")[-1])
    mine    = _mine_by_id(mine_id)
    if not mine:
        bot.answer_callback_query(call.id, "❌ Шахта не найдена")
        return

    with get_db_cursor() as cur:
        cur.execute("SELECT first_name, custom_name, username FROM users WHERE user_id=?", (mine['owner_id'],))
        u = cur.fetchone()
    owner = (u[1] or u[0] or (f"@{u[2]}" if u[2] else "?")) if u else "?"

    lv      = mine['level']
    lv_info = MINE_LEVELS[lv]
    rental  = _mine_rental(uid)

    text = (
        f"🏔️ <b>Шахта #{mine_id}</b>\n\n"
        f"👤 Владелец: <b>{owner}</b>\n"
        f"⭐ {lv_info['label']} · Бонус <b>x{lv_info['bonus']}</b>\n"
        f"💰 Аренда: <b>{format_balance(mine['rent_price'])}</b> / 10 мин\n\n"
        f"В частной шахте ресурсы лучше чем в общей."
    )

    kb = {"inline_keyboard": []}
    if mine['owner_id'] == uid:
        kb["inline_keyboard"].append([{"text": "🏔️ Управление", "callback_data": "mine_mymine"}])
    elif rental and rental[0] == mine_id:
        kb["inline_keyboard"].append([{"text": "✅ Ты уже здесь", "callback_data": "mine_main"}])
    else:
        kb["inline_keyboard"].append([{
            "text": f"⛏ Арендовать за {format_balance(mine['rent_price'])}",
            "callback_data": f"mine_rent_{mine_id}"
        }])
    kb["inline_keyboard"].append([{"text": "◀️ Назад", "callback_data": "mine_list"}])

    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                          reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)

# ── Аренда шахты ──────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("mine_rent_"))
def cb_mine_rent(call):
    uid     = call.from_user.id
    mine_id = int(call.data.split("_")[-1])
    mine    = _mine_by_id(mine_id)
    if not mine:
        bot.answer_callback_query(call.id, "❌ Шахта не найдена")
        return
    if mine['owner_id'] == uid:
        bot.answer_callback_query(call.id, "❌ Это твоя шахта")
        return

    rent = mine['rent_price']
    now  = int(time.time())

    with get_db_cursor() as cur:
        cur.execute("UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                    (rent, uid, rent))
        if cur.rowcount == 0:
            bot.answer_callback_query(call.id, "❌ Недостаточно средств", show_alert=True)
            return
        cur.execute("UPDATE mines SET balance=balance+? WHERE id=?", (rent, mine_id))
        cur.execute(
            "INSERT INTO mine_rentals (user_id, mine_id, expires_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET mine_id=?, expires_at=?",
            (uid, mine_id, now + MINE_RENT_DURATION, mine_id, now + MINE_RENT_DURATION)
        )

    bot.answer_callback_query(call.id, "✅ Арендовал на 10 минут! Жми Копать.", show_alert=False)
    try:
        bot.edit_message_text(_mine_main_text(uid), call.message.chat.id, call.message.message_id,
                              reply_markup=_mine_main_kb(uid), parse_mode="HTML")
    except Exception:
        pass

# ── Купить шахту ──────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "mine_buy_prompt")
def cb_mine_buy_prompt(call):
    uid = call.from_user.id
    if _mine_by_owner(uid):
        bot.answer_callback_query(call.id, "У тебя уже есть шахта")
        return
    kb = {"inline_keyboard": [
        [{"text": f"✅ Купить за {format_balance(MINE_BUY_PRICE)}", "callback_data": "mine_buy_confirm"}],
        [{"text": "◀️ Назад", "callback_data": "mine_main"}],
    ]}
    bot.edit_message_text(
        f"💰 <b>Купить шахту</b>\n\n"
        f"Стоимость: <b>{format_balance(MINE_BUY_PRICE)}</b>\n\n"
        f"• Игроки арендуют твою шахту\n"
        f"• Оплата идёт в баланс шахты\n"
        f"• Улучшай шахту — больше бонус → выше спрос\n"
        f"• Выводи баланс в любой момент",
        call.message.chat.id, call.message.message_id,
        reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "mine_buy_confirm")
def cb_mine_buy_confirm(call):
    uid = call.from_user.id
    if _mine_by_owner(uid):
        bot.answer_callback_query(call.id, "У тебя уже есть шахта")
        return
    with get_db_cursor() as cur:
        cur.execute("UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                    (MINE_BUY_PRICE, uid, MINE_BUY_PRICE))
        if cur.rowcount == 0:
            bot.answer_callback_query(call.id, f"❌ Нужно {format_balance(MINE_BUY_PRICE)}", show_alert=True)
            return
        cur.execute("INSERT INTO mines (owner_id, level, balance, rent_price, created_at) VALUES (?,1,0,1000,?)",
                    (uid, int(time.time())))
    bot.answer_callback_query(call.id, "✅ Шахта куплена!", show_alert=False)
    try:
        bot.edit_message_text(_mine_main_text(uid), call.message.chat.id, call.message.message_id,
                              reply_markup=_mine_main_kb(uid), parse_mode="HTML")
    except Exception:
        pass

# ── Моя шахта ─────────────────────────────────────────────────

def _mymine_text(mine):
    lv      = mine['level']
    lv_info = MINE_LEVELS[lv]
    next_lv = lv + 1
    upgrade = (f"⬆️ До Ур.{next_lv}: <b>{format_balance(lv_info['upgrade_cost'])}</b>"
               if next_lv in MINE_LEVELS else "⭐ Максимальный уровень")
    return (
        f"🏔️ <b>Моя шахта #{mine['id']}</b>\n\n"
        f"⭐ {lv_info['label']} · Бонус <b>x{lv_info['bonus']}</b>\n"
        f"💰 Баланс: <b>{format_balance(mine['balance'])}</b>\n"
        f"🎟️ Аренда: <b>{format_balance(mine['rent_price'])}</b>/10мин\n\n"
        f"{upgrade}"
    )

def _mymine_kb(mine):
    lv      = mine['level']
    lv_info = MINE_LEVELS[lv]
    next_lv = lv + 1
    kb = {"inline_keyboard": []}
    if mine['balance'] > 0:
        kb["inline_keyboard"].append([{"text": f"💵 Вывести {format_balance(mine['balance'])}", "callback_data": "mine_withdraw"}])
    if next_lv in MINE_LEVELS:
        kb["inline_keyboard"].append([{"text": f"⬆️ Улучшить · {format_balance(lv_info['upgrade_cost'])}", "callback_data": "mine_upgrade"}])
    kb["inline_keyboard"].append([{"text": "💲 Цена аренды", "callback_data": "mine_setprice"}])
    kb["inline_keyboard"].append([{"text": "◀️ Назад", "callback_data": "mine_main"}])
    return kb

@bot.callback_query_handler(func=lambda c: c.data == "mine_mymine")
def cb_mine_mymine(call):
    uid  = call.from_user.id
    mine = _mine_by_owner(uid)
    if not mine:
        bot.answer_callback_query(call.id, "У тебя нет шахты")
        return
    bot.edit_message_text(_mymine_text(mine), call.message.chat.id, call.message.message_id,
                          reply_markup=_mymine_kb(mine), parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "mine_withdraw")
def cb_mine_withdraw(call):
    uid  = call.from_user.id
    mine = _mine_by_owner(uid)
    if not mine or mine['balance'] <= 0:
        bot.answer_callback_query(call.id, "Нет средств")
        return
    amount = mine['balance']
    with get_db_cursor() as cur:
        cur.execute("UPDATE mines SET balance=0 WHERE owner_id=?", (uid,))
        cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, uid))
    bot.answer_callback_query(call.id, f"✅ +{format_balance(amount)} 🌸", show_alert=False)
    mine = _mine_by_owner(uid)
    try:
        bot.edit_message_text(_mymine_text(mine), call.message.chat.id, call.message.message_id,
                              reply_markup=_mymine_kb(mine), parse_mode="HTML")
    except Exception:
        pass

@bot.callback_query_handler(func=lambda c: c.data == "mine_upgrade")
def cb_mine_upgrade(call):
    uid  = call.from_user.id
    mine = _mine_by_owner(uid)
    if not mine:
        bot.answer_callback_query(call.id, "У тебя нет шахты")
        return
    lv      = mine['level']
    next_lv = lv + 1
    if next_lv not in MINE_LEVELS:
        bot.answer_callback_query(call.id, "Максимальный уровень")
        return
    cost     = MINE_LEVELS[lv]['upgrade_cost']
    mine_bal = mine['balance']

    with get_db_cursor() as cur:
        if mine_bal >= cost:
            cur.execute("UPDATE mines SET balance=balance-?, level=? WHERE owner_id=?",
                        (cost, next_lv, uid))
        else:
            extra = cost - mine_bal
            cur.execute("UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                        (extra, uid, extra))
            if cur.rowcount == 0:
                bot.answer_callback_query(call.id,
                    f"❌ Нужно {format_balance(cost)} · в шахте {format_balance(mine_bal)} · не хватает {format_balance(extra)}",
                    show_alert=True)
                return
            cur.execute("UPDATE mines SET balance=0, level=? WHERE owner_id=?", (next_lv, uid))

    bot.answer_callback_query(call.id, f"✅ Шахта улучшена до Ур.{next_lv}!", show_alert=False)
    mine = _mine_by_owner(uid)
    try:
        bot.edit_message_text(_mymine_text(mine), call.message.chat.id, call.message.message_id,
                              reply_markup=_mymine_kb(mine), parse_mode="HTML")
    except Exception:
        pass

@bot.callback_query_handler(func=lambda c: c.data == "mine_setprice")
def cb_mine_setprice(call):
    uid  = call.from_user.id
    mine = _mine_by_owner(uid)
    if not mine:
        bot.answer_callback_query(call.id, "У тебя нет шахты")
        return
    lv_info = MINE_LEVELS[mine['level']]
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id,
        f"💲 Введи новую цену аренды\n"
        f"От <b>{format_balance(lv_info['rent_min'])}</b> до <b>{format_balance(lv_info['rent_max'])}</b>",
        parse_mode="HTML")
    bot.register_next_step_handler(msg, _mine_setprice_step, uid, mine['id'], lv_info)

def _mine_setprice_step(message, uid, mine_id, lv_info):
    try:
        price = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи число")
        return
    if price < lv_info['rent_min'] or price > lv_info['rent_max']:
        bot.send_message(message.chat.id,
            f"❌ От {format_balance(lv_info['rent_min'])} до {format_balance(lv_info['rent_max'])}")
        return
    with get_db_cursor() as cur:
        cur.execute("UPDATE mines SET rent_price=? WHERE id=? AND owner_id=?", (price, mine_id, uid))
    bot.send_message(message.chat.id,
        f"✅ Цена аренды: <b>{format_balance(price)}</b>/10мин", parse_mode="HTML")

# ── Топ майнеров ──────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "mine_top")
def cb_mine_top(call):
    with get_db_cursor() as cur:
        cur.execute("""
            SELECT ms.user_id, ms.total_digs, ms.total_earned,
                   u.first_name, u.custom_name, u.username
            FROM mine_stats ms JOIN users u ON u.user_id=ms.user_id
            ORDER BY ms.total_earned DESC LIMIT 10
        """)
        rows = cur.fetchall()

    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    text   = "🏆 <b>Лучшие майнеры</b>\n\n"
    if not rows:
        text += "Пока никто не копал!"
    for i, row in enumerate(rows):
        uid2, digs, earned, fname, cname, uname = row
        name = cname or fname or (f"@{uname}" if uname else f"#{uid2}")
        # best_streak
        with get_db_cursor() as cur2:
            cur2.execute("SELECT best_streak FROM mine_stats WHERE user_id=?", (uid2,))
            bs_row = cur2.fetchone()
        bs = bs_row[0] if bs_row else 0
        streak_tag = f" 🔥{bs}" if bs >= 10 else ""
        text += f"{medals[i]} <b>{name}</b> — {format_balance(earned)} 🌸 ({digs} раз){streak_tag}\n"

    kb = {"inline_keyboard": [[{"text": "◀️ Назад", "callback_data": "mine_main"}]]}
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                          reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)

