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


active_snowman_bosses = {}
player_cooldowns = {}

class DatabasePool:
    def __init__(self, db_name='game.db', pool_size=10):
        self.db_name = db_name
        self.pool = []
        self.pool_size = pool_size
        self._lock = threading.Lock()
    def get_connection(self):
        with self._lock:
            if self.pool:
                return self.pool.pop()
            return sqlite3.connect(self.db_name, timeout=60.0, check_same_thread=False)
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
    conn.row_factory = sqlite3.Row
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
        except:
            conn.rollback()
            raise

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
    
    print("🏆 База данных проверена и обновлена")
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
        dangerous = ['DROP ', 'TRUNCATE ', 'ALTER ', 'ATTACH ', 'DETACH ']
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
    flower = "<tg-emoji emoji-id='5440748683765227563'>🌸</tg-emoji>"
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
            db_path = 'game.db'
            backup_path = None
            if os.path.exists(db_path):
                backup_path = f'game_auto_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
                shutil.copy2(db_path, backup_path)
                bot.send_message(message.chat.id, f"📦 Старая база сохранена как: <code>{backup_path}</code>", parse_mode='HTML')
                
                with db_pool._lock:
                    for conn in db_pool.pool:
                        try:
                            conn.close()
                        except:
                            pass
                    db_pool.pool = []
            
            with open(db_path, 'wb') as new_file:
                new_file.write(downloaded_file)
            
            try:
                test_conn = sqlite3.connect(db_path)
                test_conn.cursor().execute("SELECT name FROM sqlite_master LIMIT 1")
                test_conn.close()
                
                init_db()
                init_dice_tables()
                init_taxi_database()
                
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
                    
            except sqlite3.DatabaseError:
                bot.send_message(message.chat.id, "💀 Ошибка: загруженный файл не является базой данных SQLite!", parse_mode='HTML')
                
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
        "⚔️ Гильдия", "🏆 Топ", "💎 Донат",
        "🎁 Бонус"
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
    
    bot.edit_message_text(
        "🔄 <b>Начинаю рассылку...</b>",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML'
    )
    
    sent = 0
    failed = 0
    
    try:
        with get_db_cursor() as cursor:
            cursor.execute('SELECT user_id FROM users')
            users = cursor.fetchall()
            
            total = len(users)
            
            for i, (user_id,) in enumerate(users, 1):
                try:
                    bot.send_message(
                        user_id,
                        f"📢 <b>ОБЪЯВЛЕНИЕ</b>\n\n{pending_broadcast['text']}",
                        parse_mode='HTML'
                    )
                    sent += 1
                    
                    if i % 50 == 0:
                        progress = int((i / total) * 100)
                        bot.edit_message_text(
                            f"🔄 <b>Рассылка...</b>\n\n"
                            f"📊 Прогресс: {progress}%\n"
                            f"🏆 Отправлено: {sent}\n"
                            f"❌ Ошибок: {failed}",
                            call.message.chat.id,
                            call.message.message_id,
                            parse_mode='HTML'
                        )
                    
                    time.sleep(0.05)
                    
                except Exception as e:
                    failed += 1
        
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
@bot.message_handler(commands=['buy'])
def handle_buy(message):
    user_id = message.from_user.id
    
    buy_markup = InlineKeyboardMarkup()
    buy_markup.row(
        InlineKeyboardButton("⭐ 1 зв — 15,000", callback_data="stars_1"),
        InlineKeyboardButton("⭐ 5 зв — 166,000", callback_data="stars_5")
    )
    buy_markup.row(
        InlineKeyboardButton("🔥 15 зв — 466,000", callback_data="stars_15"),
        InlineKeyboardButton("🔥 50 зв — 1,500,000", callback_data="stars_50")
    )
    buy_markup.row(
        InlineKeyboardButton("⭐️ 150 — 5,000,000", callback_data="stars_150"),
        InlineKeyboardButton("⭐️ 250 — 10,000,000", callback_data="stars_250")
    )

    bot.send_message(
        message.chat.id,
        "🛒 <b>Магазин валюты</b>\n\n"
        "<blockquote>"
        "⭐ 1 зв — 15,000  (~1.5₽)\n"
        "⭐ 5 зв — 66,000  (~7.5₽)\n"
        "⭐ 15 зв — 366,000  (~22.5₽)\n"
        "⭐ 50 зв — 1,500,000  (~75₽)\n"
        "⭐️ 150 — 5,000,000  (~225₽)\n"
        "⭐️ 250 — 10,000,000  (~375₽)"
        "</blockquote>\n\n"
        "🔥 — самое выгодное соотношение\n"
        "Лучше спросить @Cary_Python,может напрямую будет небольшой бонус",
        reply_markup=buy_markup,
        parse_mode='HTML'
    )

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
            f"<tg-emoji emoji-id='5440748683765227563'>🌸</tg-emoji> <b>Специальное предложение</b>\n\n"
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
    
    packages = {
        "stars_1":   {"amount": 15000,    "stars": 1,   "title": "15,000"},
        "stars_5":   {"amount": 166000,    "stars": 5,   "title": "166,000"},
        "stars_15":  {"amount": 366000,   "stars": 15,  "title": "366,000"},
        "stars_50":  {"amount": 1000000,  "stars": 50,  "title": "1,000,000"},
        "stars_150": {"amount": 5000000,  "stars": 150, "title": "5,000,000"},
        "stars_250": {"amount": 10000000,  "stars": 250, "title": "10,000,000"},
    }
    
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
                f"<tg-emoji emoji-id='5440748683765227563'>🌸</tg-emoji> <b>Баланс пополнен</b>\n"
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
            f"<tg-emoji emoji-id='5440748683765227563'>🌸</tg-emoji> Скидка {percent}% отправлена пользователю {target}",
            parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка в handle_admin_discount: {e}")
        bot.reply_to(message, "❌ Ошибка при выдаче скидки")

@bot.message_handler(func=lambda message: message.text in ["💎 Донат", "Донат"])
def handle_buy_currency_button(message):
    handle_buy(message)

@bot.message_handler(func=lambda message: message.text.lower() == 'актив')
def handle_active(message):
    try:
        with get_db_cursor() as cursor:
            cursor.execute('SELECT SUM(balance + bank_deposit) FROM users')
            total_capital = cursor.fetchone()[0] or 0
            
            cursor.execute('SELECT COUNT(*) FROM users')
            user_count = cursor.fetchone()[0]

        message_text = f"📊 <b>Статистика</b>\n\n💸 Экономика: {format_balance(total_capital)}\n\n🟢 Пользователей: {user_count}"
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔄 Обновить", callback_data="refresh_active_stats"))
        
        bot.send_message(message.chat.id, message_text, reply_markup=markup, parse_mode='HTML')
    
    except:
        bot.send_message(message.chat.id, "⚠️ Что-то пошло не так. Попробуй ещё раз.")

@bot.callback_query_handler(func=lambda call: call.data == "refresh_active_stats")
def handle_refresh_active_stats(call):
    try:
        with get_db_cursor() as cursor:
            cursor.execute('SELECT SUM(balance + bank_deposit) FROM users')
            total_capital = cursor.fetchone()[0] or 0
            
            cursor.execute('SELECT COUNT(*) FROM users')
            user_count = cursor.fetchone()[0]

        message_text = f"📊 <b>Статистика</b>\n\n💸 Экономика: {format_balance(total_capital)}\n\n🟢 Пользователей: {user_count}"
        
        bot.edit_message_text(message_text, call.message.chat.id, call.message.message_id, parse_mode='HTML')
        bot.answer_callback_query(call.id, "🏆 Обновлено!")
        
    except:
        bot.answer_callback_query(call.id, "❌ Ошибка!")

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

            message_text = f"{user_display}, поздравляем, ты выиграл\n"
            message_text += f"<tg-emoji emoji-id='5440748683765227563'>🌸</tg-emoji><b>{_fmt_num(bet_amount)} (x{multiplier}).</b>\n"
            message_text += f"Выпало - {winning_color}<b>{winning_number}</b>\n"
            message_text += f"<blockquote><tg-emoji emoji-id='5375296873982604963'>💰</tg-emoji> — {_fmt_num(new_balance)}</blockquote>"
        else:
            new_balance = get_balance(user_id)

            message_text = f"{user_display}, к сожалению ты проиграл\n"
            message_text += f"<tg-emoji emoji-id='5440748683765227563'>🌸</tg-emoji><b>{_fmt_num(bet_amount)} (x0).</b>\n"
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
        result_text = f"🎉 <b>Победа!</b>\n\n"
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
                            bot.send_message(message.chat.id, "🎉 Ты получил 1 000🌸 за приглашение друга!", parse_mode='HTML')
                            add_experience(referred_by, referrer_exp)
                            try:
                                bot.send_message(referred_by, f"👥 Друг принял твоё приглашение!\n⭐ +{referrer_exp} опыта", parse_mode='HTML')
                            except Exception:
                                pass
        
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
                        msg = bot.send_message(user_id, f"🔐 У этого чека есть пароль. Введите пароль для активации:")
                        bot.register_next_step_handler(msg, process_check_password, ref_code, user_id, amount, max_activations, current_activations, password, target_username)
                    else:
                        success, result_message = activate_check(user_id, ref_code, amount, max_activations, current_activations, password, target_username)
                        bot.send_message(user_id, result_message, parse_mode='HTML')
                elif already_activated:
                    bot.send_message(user_id, "❌ Вы уже активировали этот чек!", parse_mode='HTML')
                else:
                    bot.send_message(user_id, "❌ Чек уже использован максимальное количество раз!", parse_mode='HTML')
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
                        bot.send_message(user_id, "🎉 Ты получил 1 000🌸 за регистрацию по реферальной ссылке!", parse_mode='HTML')
                        
    except Exception as e:
        print(f"Ошибка в handle_referral_code: {e}")
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

    return True, f"<tg-emoji emoji-id='5440748683765227563'>🌸</tg-emoji> <b>Вы активировали чек на {format_balance(amount)}</b> <tg-emoji emoji-id='5440748683765227563'>🌸</tg-emoji>!"

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
@bot.message_handler(func=lambda message: message.text.lower() == 'актив')
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
        with get_db_cursor() as cursor:
            cursor.execute('SELECT COUNT(*) FROM users WHERE last_activity > ?', (time.time() - 86400,))
            active_users_24h = cursor.fetchone()[0] or 0
            
            cursor.execute('SELECT COUNT(*) FROM users WHERE last_activity > ?', (time.time() - 604800,))
            active_users_7d = cursor.fetchone()[0] or 0
            
            cursor.execute('SELECT COUNT(*) FROM users')
            total_users = cursor.fetchone()[0] or 0
            
            cursor.execute('SELECT COUNT(*) FROM users WHERE last_activity > ? AND last_activity > ?', 
                          (time.time() - 86400, time.time() - 172800))
            new_users_today = cursor.fetchone()[0] or 0
            
            cursor.execute('SELECT SUM(balance + bank_deposit) FROM users')
            total_economy = cursor.fetchone()[0] or 0
            
            cursor.execute('SELECT SUM(balance) FROM users')
            total_balance = cursor.fetchone()[0] or 0
            
            cursor.execute('SELECT SUM(bank_deposit) FROM users')
            total_deposits = cursor.fetchone()[0] or 0

        message_text = "<b>📊 АКТИВНОСТЬ БОТА</b>\n\n"
        
        message_text += "<b>👥 ПОЛЬЗОВАТЕЛИ</b>\n"
        message_text += f"<blockquote>🟢 Онлайн (24ч): {active_users_24h:,}\n"
        message_text += f"📈 Активных (7д): {active_users_7d:,}\n"
        message_text += f"🎴 Всего: {total_users:,}\n"
        message_text += f"🆕 Новых сегодня: {new_users_today:,}</blockquote>\n\n"
        
        message_text += "💵<b> ЭКОНОМИКА</b>\n"
        message_text += f"<blockquote>💸 Общий капитал: {format_balance(total_economy)}\n"
        message_text += f"💳 На руках: {format_balance(total_balance)}\n"
        message_text += f"🏛 Депозит: {format_balance(total_deposits)}\n"
        message_text += f"📊 Средний баланс: {format_balance(total_economy // total_users if total_users > 0 else 0)}</blockquote>\n\n"
        
        message_text += f"<i>🕒 Обновлено: {datetime.now().strftime('%H:%M:%S')}</i>"

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
@bot.message_handler(func=lambda message: bot.get_me().username.lower() in message.text.lower() and not message.text.startswith('/'))
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
            f"<tg-emoji emoji-id='5278467510604160626'>💵</tg-emoji> +{format_balance(bonus_amount)}\n"
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
            f"<tg-emoji emoji-id='5278467510604160626'>💵</tg-emoji> +{format_balance(bonus_amount)}\n"
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
        other_user_id = t