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

# Глобальные переменные для пагинации (добавь после других глобальных переменных)
shop_pages = {}
wardrobe_pages = {}
active_crash_bets = {}  # user_id -> {bet, multiplier, chat_id, started_at}
pending_crash_decisions = {}  # game_id -> {user_id, user_name, bet, target_mult, admin_msg_id, timestamp}
REAL_ADMIN_IDS = list(ADMIN_IDS)  # Оригинальный список — не меняется
disabled_admins = set()  # Временно отключённые админы

bot = telebot.TeleBot(BOT_TOKEN)


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

# ── Monkey-patch: перехватываем bot.send_message и bot.reply_to ──────────────
# Если reply_markup — dict (наши цветные клавиатуры), отправляем через прямой API
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
        # Возвращаем заглушку с message_id чтобы код не падал на .message_id
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
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────






# Система займов
LOAN_CONFIG = {
    "max_loan": 100000,      # Максимум 10B
    "interest_rate": 0.1,         # 10% в день
    "max_term": 3,                # Максимум 3 дня
    "penalty_rate": 0.2,          # 20% штраф за просрочку
    "min_balance_for_loan": 500  # 500M минимальный баланс
}

# Комиссия за транзакция
TRANSFER_FEE = 0.1  # 5% процент

# КОНФИГУРАЦИЯ КЕЙСОВ
CASE_SYSTEM = {
    "case_price": 1000,  # 10ккк
    "components": {
        "common": [
            {"name": "Dildack", "price": 100},
            {"name": "⌚ Часы Casio", "price": 150},
            {"name": "📱 Старый телефон", "price": 200},
            {"name": "🎧 Наушники", "price": 180},
            {"name": "👟 Кроссовки Nike Air Force", "price": 120},
            {"name": "👟 Кроссовки Adidas Superstar", "price": 110},
            {"name": "👟 Кроссовки Reebok Classic", "price": 100}
        ],
        "rare": [
            {"name": "💍 Золотое кольцо", "price": 500},
            {"name": "🕶️ Дизайнерские очки", "price": 700},
            {"name": "👔 Брендовая рубашка", "price": 600},
            {"name": "💻 Ноутбук", "price": 800},
            {"name": "📸 Фотоаппарат", "price": 750},
            {"name": "👟 Кроссовки Nike Dunk Low", "price": 500},
            {"name": "👟 Кроссовки Adidas Yeezy 350", "price": 700},
            {"name": "👟 Кроссовки Jordan 1 Mid", "price": 600}
        ],
        "epic": [
            {"name": "🚗 Ключи от машины", "price": 1500},
            {"name": "🎁 Бриллиантовые серьги", "price": 2000},
            {"name": "🛳️ Яхта на выходные", "price": 2500},
            {"name": "🎲 Игровая консоль", "price": 1800},
            {"name": "🧳 Дизайнерский чемодан", "price": 2200},
            {"name": "👟 Кроссовки Nike Travis Scott", "price": 1500},
            {"name": "👟 Кроссовки Adidas Yeezy 750", "price": 2000},
            {"name": "👟 Кроссовки Jordan 1 Retro High", "price": 1800},
            {"name": "👟 Кроссовки Balenciaga Triple S", "price": 2200}
        ],
        "mythic": [
            {"name": "🏠 Ключи от квартиры", "price": 5000},
            {"name": "🚀 Билет в космос", "price": 7500},
            {"name": "⚡ Электронный спорткар", "price": 10000},
            {"name": "🛩️ Частный самолет", "price": 9000},
            {"name": "🏝️ Остров в аренду", "price": 8500},
            {"name": "👟 Кроссовки Nike Mag Back to the Future", "price": 5000},
            {"name": "👟 Кроссовки Adidas Yeezy Boost 750 Glow", "price": 7500},
            {"name": "👟 Кроссовки Air Jordan 1 OG Chicago", "price": 6000},
            {"name": "👟 Кроссовки Louis Vuitton Trainer", "price": 8000}
        ],
        "legendary": [
            {"name": "👑 Королевская корона", "price": 25000},
            {"name": "🚁 Личный вертолет", "price": 50000},
            {"name": "🏰 Замок в Шотландии", "price": 75000},
            {"name": "💼 Портфель с акциями", "price": 60000},
            {"name": "🎨 Картина Ван Гога", "price": 100000},
            {"name": "👟 Кроссовки Nike Moon Shoe 1972", "price": 25000},
            {"name": "👟 Кроссовки Air Jordan 12 OVO", "price": 50000},
            {"name": "👟 Кроссовки Adidas Yeezy 1 Prototype", "price": 75000},
            {"name": "👟 Кроссовки Diamond Dunk SB", "price": 100000}
        ]
    },
  "chances": {
    "common": 64,      # 70% - обычные шмотки
    "rare": 24,        # 22% - редкие
    "epic": 8,         # 6% - эпические
    "mythic": 3.5,     # 1.5% - мифические
    "legendary": 0.5   # 0.5% - легендарные (1 из 200 кейсов)

    }
}

# Глобальные переменные (добавьте после других глобальных переменных):
active_snowman_bosses = {}
player_cooldowns = {}

# Оптимизация: пул соединений
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
        # 1. Таблица пользователей
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
        # 20. Таблица варнов
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
        # 2. Таблица бизнесов
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
        
        # 3. Таблица чеков
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
        
        # 4. Таблица активаций чеков
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS check_activations (
            user_id INTEGER,
            check_code TEXT,
            activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, check_code)
        )
        ''')
        
        # 5. Таблица лотереи
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
        
        # 6. Таблица билетов лотереи
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS lottery_tickets (
            user_id INTEGER,
            tickets INTEGER DEFAULT 0,
            PRIMARY KEY (user_id)
        )
        ''')
        
        # 7. Таблица магазина одежды
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
        
        # 8. Таблица инвентаря одежды
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_clothes (
            user_id INTEGER,
            item_id INTEGER,
            equipped INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, item_id)
        )
        ''')
        
        # 9. Таблица займов
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS loans (
            user_id INTEGER PRIMARY KEY,
            loan_amount INTEGER DEFAULT 0,
            taken_at TIMESTAMP DEFAULT 0,
            interest_paid INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active'
        )
        ''')
        
        # Premium подписки
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS premium (
            user_id INTEGER PRIMARY KEY,
            expires_at TIMESTAMP DEFAULT 0
        )
        ''')

        # 10. Таблица транзакцияов
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
        
        # 11. Таблица для хранения ID сообщений топа
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS top_messages (
            chat_id INTEGER PRIMARY KEY,
            message_id INTEGER
        )
        ''')
        
        # 12. Таблица кланов
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
        
        # 13. Таблица боецов кланов
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
        
        # 14. Таблица заявок в кланы
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS clan_applications (
            user_id INTEGER,
            clan_id INTEGER,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, clan_id)
        )
        ''')
        
        # 15. Таблица инвентаря кейсов
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_bag (
            user_id INTEGER,
            component_name TEXT,
            component_price INTEGER,
            obtained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, component_name)
        )
        ''')
        
        # 16. Таблица клановых заданий
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
        
        # 17. Таблица вызовов в кости (dice)
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
            
        
        # 18. Таблица аукционов
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
        
        # 19. Таблица ставок
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
        
        
        # 21. Таблица снеговика-босса (добавьте в конец существующих таблиц)
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
        
        # Индексы
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_snowman_chat ON snowman_battles(chat_id, status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_snowman_damage ON snowman_damage(battle_id, total_damage DESC)')
        
        # ... остальной существующий код init_db ...
        # Создаем индексы для аукционов
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_auctions_status ON auctions(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_auctions_ends_at ON auctions(ends_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_auction_bids_auction ON auction_bids(auction_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_auction_bids_user ON auction_bids(user_id)')
        
        print("🏆 Таблицы аукционов созданы")
        
        # Проверяем и добавляем отсутствующие колонки
        # Проверяем существование колонки target_username в таблице checks
        cursor.execute("PRAGMA table_info(checks)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'target_username' not in columns:
            cursor.execute('ALTER TABLE checks ADD COLUMN target_username TEXT')
            print("🏆 Добавлена колонка target_username в таблицу checks")
            
        # Проверяем существование колонки experience в users
        cursor.execute("PRAGMA table_info(users)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'experience' not in columns:
            cursor.execute('ALTER TABLE users ADD COLUMN experience INTEGER DEFAULT 0')
            print("🏆 Добавлена колонка experience в таблицу users")
            
        # Проверяем остальные необходимые колонки в users
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
        
        # Проверяем колонки supply и sold_count в clothes_shop
        cursor.execute("PRAGMA table_info(clothes_shop)")
        shop_columns = [column[1] for column in cursor.fetchall()]
        
        if 'supply' not in shop_columns:
            cursor.execute('ALTER TABLE clothes_shop ADD COLUMN supply INTEGER DEFAULT -1')
            print("🏆 Добавлена колонка supply в таблицу clothes_shop")
            
        if 'sold_count' not in shop_columns:
            cursor.execute('ALTER TABLE clothes_shop ADD COLUMN sold_count INTEGER DEFAULT 0')
            print("🏆 Добавлена колонка sold_count в таблицу clothes_shop")
        
        # Обновляем мощность клика
        cursor.execute('UPDATE users SET click_power = 200 WHERE click_power < 100')
        cursor.execute('UPDATE users SET last_interest_calc = ? WHERE last_interest_calc = 0', (int(time.time()),))
        
        # Инициализируем лотерею
        cursor.execute('INSERT OR IGNORE INTO lottery (id, jackpot) VALUES (1, 0)')
        
        # Создаем индексы для оптимизации
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
        # Таблица скидок
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS discounts (
                user_id INTEGER PRIMARY KEY,
                percent INTEGER DEFAULT 50,
                used INTEGER DEFAULT 0,
                auto_given INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Миграция: добавить auto_given если нет
        try:
            cursor.execute('ALTER TABLE discounts ADD COLUMN auto_given INTEGER DEFAULT 0')
        except:
            pass
        
        # Индексы для кланов
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_clans_owner ON clans(owner_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_clans_level ON clans(level)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_clan_members_clan ON clan_members(clan_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_clan_members_user ON clan_members(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_clan_applications_clan ON clan_applications(clan_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_clan_quests_clan ON clan_quests(clan_id)')
        
        # Индекс для dice_challenges
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_dice_challenges_expires ON dice_challenges(expires_at)')
        
        # Таблица промокодов
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
        
        # Таблица истории игр (для мини-апп казино)
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
    
    print("🏆 База данных проверена и обновлена")
# Функция для проверки прав админа
def is_admin(user_id):
    return user_id in ADMIN_IDS and user_id not in disabled_admins

@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == 'toggleadmin')
def handle_toggle_admin(message):
    user_id = message.from_user.id
    # Только реальные владельцы могут использовать команду
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


# ══════════════════════════════════════════════════════
# СИСТЕМА ПРЕМИУМА
# ══════════════════════════════════════════════════════

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

# ── /premium — информация и покупка ──────────────────

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
        f"💰 Бонус каждые 10 мин вместо 30\n"
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

# ── Успешная оплата премиума ──────────────────────────

_orig_successful_payment = None  # will chain if needed

# premium payment handled inside handle_successful_payment below

# ── Админ команды выдачи премиума ────────────────────

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
        # Ищем пользователя
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
        # Уведомляем пользователя
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

# ══════════════════════════════════════════════════════
# КОНЕЦ СИСТЕМЫ ПРЕМИУМА
# ══════════════════════════════════════════════════════

# Безопасная инициализация таблиц для костей
def init_dice_tables():
    try:
        with get_db_cursor() as cursor:
            # Проверяем существование таблицы
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dice_challenges'")
            table_exists = cursor.fetchone()
            
            if not table_exists:
                # Создаем таблицу с всеми колонками
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
                # Проверяем существование всех колонок
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
            
            # Создаем индекс если его нет
            cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_dice_challenges_expires'")
            index_exists = cursor.fetchone()
            if not index_exists:
                cursor.execute('CREATE INDEX idx_dice_challenges_expires ON dice_challenges(expires_at)')
                print("🏆 Индекс для dice_challenges создан")
                
    except Exception as e:
        print(f"❌ Ошибка инициализации таблиц костей: {e}")

# Очистка просроченных вызовов с проверкой таблицы
def cleanup_expired_challenges():
    try:
        with get_db_cursor() as cursor:
            # Проверяем существование таблицы
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dice_challenges'")
            table_exists = cursor.fetchone()
            
            if not table_exists:
                print("⚠️ Таблица dice_challenges не существует, пропускаем очистку")
                return
                
            # Проверяем существование колонки expires_at
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
        
# Функция для парсинза суммы ставки
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


# Функция для форматирования суммы
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


# Функции для системы варнов
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
# Команда для полного удаления вещи из БД
@bot.message_handler(func=lambda message: message.text.lower().startswith('удалить вещь ') and is_admin(message.from_user.id))
def handle_delete_item(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        # Получаем ID или название вещи
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
            # Пробуем найти вещь по ID или названию
            if target.isdigit():
                # Поиск по ID
                cursor.execute('SELECT id, name, image_name FROM clothes_shop WHERE id = ?', (int(target),))
            else:
                # Поиск по названию
                cursor.execute('SELECT id, name, image_name FROM clothes_shop WHERE name LIKE ?', (f'%{target}%',))
            
            items = cursor.fetchall()
            
            if not items:
                bot.send_message(message.chat.id, f"❌ Вещь '{target}' не найдено!", parse_mode='HTML')
                return
            
            if len(items) > 1:
                # Показываем список найденных вещей
                items_text = "🔍 Найдено несколько вещей:\n\n"
                for item_id, name, image_name in items:
                    items_text += f"🆔 {item_id} - {name} (файл: {image_name})\n"
                
                items_text += "\n💡 Уточните ID или название"
                bot.send_message(message.chat.id, items_text)
                return
            
            # Найден один предмет
            item_id, item_name, image_name = items[0]
            
            # Создаем кнопку подтверждения с цветами
            del_item_keyboard = {
                "inline_keyboard": [[
                    {"text": "🗑️ ДА, УДАЛИТЬ ВЕЩЬ", "callback_data": f"confirm_delete_item_{item_id}", "style": "danger"},
                    {"text": "❌ Отмена", "callback_data": "cancel_delete_item", "style": "secondary"}
                ]]
            }
            
            # Проверяем, у кого есть эта вещь
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



# Обработчик подтверждения удаления вещи
@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_delete_item_'))
def confirm_delete_item(call):
    try:
        user_id = call.from_user.id
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "❌ Недостаточно прав!")
            return
        
        item_id = int(call.data.split('_')[3])
        
        with get_db_cursor() as cursor:
            # Получаем информацию о вещи перед удалением
            cursor.execute('SELECT name, image_name FROM clothes_shop WHERE id = ?', (item_id,))
            item_info = cursor.fetchone()
            
            if not item_info:
                bot.answer_callback_query(call.id, "❌ Вещь не найдено!")
                return
            
            item_name, image_name = item_info
            
            # Получаем статистику перед удалением
            cursor.execute('SELECT COUNT(*) FROM user_clothes WHERE item_id = ?', (item_id,))
            owners_count = cursor.fetchone()[0]
            
            # Удаляем вещь из всех таблиц
            # 1. Удаляем из инвентарей пользователей
            cursor.execute('DELETE FROM user_clothes WHERE item_id = ?', (item_id,))
            deleted_from_inventory = cursor.rowcount
            
            # 2. Удаляем из магазина
            cursor.execute('DELETE FROM clothes_shop WHERE id = ?', (item_id,))
            deleted_from_shop = cursor.rowcount
            
        # Формируем отчет
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

# Обработчик отмены удаления вещи
@bot.callback_query_handler(func=lambda call: call.data == "cancel_delete_item")
def cancel_delete_item(call):
    bot.edit_message_text(
        "🏆 Удаление вещи сброшено",
        call.message.chat.id,
        call.message.message_id
    , parse_mode='HTML')
    bot.answer_callback_query(call.id, "Отменено")
# Добавьте этот код в твой файл после системы такси


# ===================== СИСТЕМА МАТЕМАТИКА =====================
# Конфигурация математика
MATH_SYSTEM = {
    "job_id": 3,
    "name": "🧮 Математик",
    "reward_per_correct": 100,  # 100кк за правильный ответ
    "penalty_per_wrong": 50,    # 50кк штраф за неправильный
    "time_limit": 7,                  # 7с на ответ
    "max_daily_attempts": 333,        # максимум 333 примера в день
    "exp_per_solve": 150              # 150 очкиа за пример
}

# Глобальные переменные для активных игр математика
active_math_games = {}

# ===================== ИНИЦИАЛИЗАЦИЯ ТАБЛИЦ (ВСТРОЕННАЯ В INIT_DB) =====================
# Эта функция должна быть добавлена в существующую функцию init_db()
def init_math_tables_in_db():
    """Инициализация таблиц математика внутри init_db()"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS math_stats (
                user_id INTEGER PRIMARY KEY,
                problems_solved INTEGER DEFAULT 0,
                correct_answers INTEGER DEFAULT 0,
                wrong_answers INTEGER DEFAULT 0,
                total_earned INTEGER DEFAULT 0,
                best_time REAL DEFAULT 0,
                total_time_spent REAL DEFAULT 0,
                last_solve_time TIMESTAMP DEFAULT 0,
                daily_attempts_today INTEGER DEFAULT 0,
                last_reset_date TEXT DEFAULT ''
            )
        ''')
        
        # Создаем индексы для оптимизации
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_math_stats_correct ON math_stats(correct_answers DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_math_stats_time ON math_stats(best_time ASC)')
        
        print("🏆 Таблицы математика созданы")

# ===================== ГЕНЕРАЦИЯ ПРИМЕРОВ =====================
def generate_math_problem(difficulty=1):
    """Генерирует математический пример"""
    if difficulty == 1:
        # Простые операции: +, -
        a = random.randint(10, 99)
        b = random.randint(10, 99)
        ops = ['+', '-']
        op = random.choice(ops)
        if op == '-':
            a, b = max(a, b), min(a, b)
    elif difficulty == 2:
        # Умножение, деление
        if random.random() > 0.5:
            a = random.randint(2, 15)
            b = random.randint(2, 15)
            op = '*'
        else:
            b = random.randint(2, 10)
            a = b * random.randint(2, 10)
            op = '/'
    else:
        # Комбинированные
        ops = ['+', '-', '*', '/']
        op = random.choice(ops)
        if op == '/':
            b = random.randint(2, 10)
            a = b * random.randint(2, 20)
        else:
            a = random.randint(10, 100)
            b = random.randint(10, 100)
            if op == '-':
                a, b = max(a, b), min(a, b)
    
    # Вычисляем ответ
    if op == '+':
        answer = a + b
    elif op == '-':
        answer = a - b
    elif op == '*':
        answer = a * b
    elif op == '/':
        answer = a // b
    
    problem = f"{a} {op} {b}"
    return problem, answer

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================
def delete_message(chat_id, message_id):
    """Безопасное удаление сообщения"""
    try:
        bot.delete_message(chat_id, message_id)
    except:
        pass

def get_math_stats(user_id):
    """Получает статистику математика"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT problems_solved, correct_answers, wrong_answers, 
                   total_earned, best_time, total_time_spent, daily_attempts_today
            FROM math_stats 
            WHERE user_id = ?
        ''', (user_id,))
        
        result = cursor.fetchone()
        if result:
            return {
                'problems_solved': result[0],
                'correct_answers': result[1],
                'wrong_answers': result[2],
                'total_earned': result[3],
                'best_time': result[4] if result[4] else 0,
                'total_time_spent': result[5] if result[5] else 0,
                'daily_attempts_today': result[6]
            }
        else:
            # Создаём запись
            cursor.execute('''
                INSERT INTO math_stats (user_id) VALUES (?)
            ''', (user_id,))
            return {
                'problems_solved': 0,
                'correct_answers': 0,
                'wrong_answers': 0,
                'total_earned': 0,
                'best_time': 0,
                'total_time_spent': 0,
                'daily_attempts_today': 0
            }

def get_daily_attempts(user_id, today):
    """Получает количество попыток за сегодня"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT last_reset_date, daily_attempts_today FROM math_stats WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if result:
            last_reset_date, attempts = result
            if last_reset_date != today:
                # Сбрасываем счётчик на новый день
                cursor.execute('''
                    UPDATE math_stats SET 
                        daily_attempts_today = 0,
                        last_reset_date = ?
                    WHERE user_id = ?
                ''', (today, user_id))
                return 0
            return attempts or 0
        return 0

def update_math_stats(user_id, is_correct, earned, answer_time):
    """Обновляет статистику математика"""
    today = datetime.now().strftime("%Y-%m-%d")
    
    with get_db_cursor() as cursor:
        # Увеличиваем счётчик попыток за день
        cursor.execute('''
            UPDATE math_stats SET
                daily_attempts_today = daily_attempts_today + 1,
                last_reset_date = ?
            WHERE user_id = ?
        ''', (today, user_id))
        
        # Обновляем основную статистику
        if is_correct:
            cursor.execute('''
                UPDATE math_stats SET
                    problems_solved = problems_solved + 1,
                    correct_answers = correct_answers + 1,
                    total_earned = total_earned + ?,
                    total_time_spent = total_time_spent + ?,
                    last_solve_time = ?
                WHERE user_id = ?
            ''', (earned, answer_time, int(time.time()), user_id))
            
            # Проверяем лучшее время
            cursor.execute('SELECT best_time FROM math_stats WHERE user_id = ?', (user_id,))
            best_time = cursor.fetchone()[0]
            if not best_time or answer_time < best_time:
                cursor.execute('UPDATE math_stats SET best_time = ? WHERE user_id = ?', (answer_time, user_id))
        else:
            cursor.execute('''
                UPDATE math_stats SET
                    problems_solved = problems_solved + 1,
                    wrong_answers = wrong_answers + 1,
                    total_earned = total_earned + ?,
                    last_solve_time = ?
                WHERE user_id = ?
            ''', (earned, int(time.time()), user_id))

# ===================== ОСНОВНОЙ ГЕЙМПЛЕЙ =====================
def start_math_game(user_id, chat_id):
    """Начинает новую игру математика"""
    # Генерируем пример
    problem, answer = generate_math_problem()
    
    # Сохраняем игру
    active_math_games[user_id] = {
        'problem': problem,
        'answer': answer,
        'start_time': time.time(),
        'chat_id': chat_id,
        'solved_count': 0,
        'correct_count': 0,
        'total_earned': 0,
        'game_start_time': time.time(),
        'current_message_id': None
    }
    
    # Отправляем первый пример
    send_math_problem(user_id)

def send_math_problem(user_id):
    """Отправляет математический пример"""
    if user_id not in active_math_games:
        return
    
    game_data = active_math_games[user_id]
    problem = game_data['problem']
    start_time = game_data['start_time']
    
    # Вычисляем оставшееся время
    time_passed = time.time() - start_time
    time_left = max(0, MATH_SYSTEM["time_limit"] - time_passed)
    
    # Создаем сообщение с кнопкой выхода
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🚪 Выйти", callback_data=f"math_exit_{user_id}"))
    
    message_text = (
        f"🧮 <b>Решите пример:</b>\n\n"
        f"<code>{problem} = ?</code>\n\n"
        f"⏱ <b>Осталось: {time_left:.1f} сек</b>\n"
        f"🏆 Решено: {game_data['solved_count']}\n"
        f"💵 Заработано: {format_balance(game_data['total_earned'])}\n\n"
        f"📝 <b>Просто напишите ответ числом в чат!</b>\n"
        f"💡 Пример: если ответ 15, напишите '15'"
    )
    
    chat_id = game_data['chat_id']
    old_message_id = game_data.get('current_message_id')
    
    # Отправляем новое сообщение
    try:
        msg = bot.send_message(chat_id, message_text, reply_markup=markup, parse_mode='HTML')
        game_data['current_message_id'] = msg.message_id
        
        # Удаляем старое сообщение с примером через 1су (если оно есть)
        if old_message_id:
            threading.Timer(1, delete_message, args=[chat_id, old_message_id]).start()
    except Exception as e:
        print(f"Ошибка отправки примера: {e}")
        return
    
    # Запускаем таймер
    if user_id in active_math_games:
        threading.Timer(time_left, check_math_timeout, args=[user_id]).start()

def check_math_timeout(user_id):
    """Проверка истечения времени"""
    if user_id not in active_math_games:
        return
    
    game_data = active_math_games[user_id]
    chat_id = game_data['chat_id']
    
    # Обновляем статистику
    game_data['solved_count'] += 1
    update_math_stats(user_id, False, 0, 0)
    
    # Отправляем временное сообщение о тайм-ауте
    timeout_message = None
    try:
        timeout_message = bot.send_message(
            chat_id,
            f"⏰ <b>Время вышло!</b>\n\n"
            f"⚔️ Ответ: {game_data['answer']}\n"
            f"💡 Слишком медленно!\n\n"
            f"<i>Новый пример черезсу...</i>",
            parse_mode='HTML'
        )
    except:
        pass
    
    # Запланировать удаление этого сообщения через 2сы
    if timeout_message:
        threading.Timer(2, delete_message, args=[chat_id, timeout_message.message_id]).start()
    
    # Следующий пример через 1су
    threading.Timer(1, next_math_problem, args=[user_id]).start()

def handle_correct_answer(user_id, answer_time):
    """Обработка правильного ответа"""
    if user_id not in active_math_games:
        return
    
    game_data = active_math_games[user_id]
    chat_id = game_data['chat_id']
    
    # Начисляем награду
    reward = MATH_SYSTEM["reward_per_correct"]
    update_balance(user_id, reward)
    add_experience(user_id, MATH_SYSTEM["exp_per_solve"])
    
    # Обновляем статистику игры
    game_data['solved_count'] += 1
    game_data['correct_count'] += 1
    game_data['total_earned'] += reward
    
    # Обновляем общую статистику
    update_math_stats(user_id, True, reward, answer_time)
    
    # Отправляем временное уведомление
    try:
        correct_message = bot.send_message(
            chat_id,
            f"🏆 <b>Правильно!</b>\n\n"
            f"⚔️ Ответ: {game_data['answer']}\n"
            f"⏱ Время: {answer_time:.2f} сек\n"
            f"💵 +{format_balance(reward)}\n\n"
            f"<i>Следующий пример черезсу...</i>",
            parse_mode='HTML'
        )
        # Удалить через 2сы
        threading.Timer(2, delete_message, args=[chat_id, correct_message.message_id]).start()
    except:
        pass

def handle_wrong_answer(user_id):
    """Обработка неправильного ответа"""
    if user_id not in active_math_games:
        return
    
    game_data = active_math_games[user_id]
    chat_id = game_data['chat_id']
    
    # Штраф
    penalty = MATH_SYSTEM["penalty_per_wrong"]
    current_balance = get_balance(user_id)
    
    penalty_applied = False
    if current_balance >= penalty:
        update_balance(user_id, -penalty)
        penalty_applied = True
    
    # Обновляем статистику
    game_data['solved_count'] += 1
    update_math_stats(user_id, False, -penalty if penalty_applied else 0, 0)
    
    # Отправляем временное уведомление
    penalty_text = f"💵 Штраф: -{format_balance(penalty)}" if penalty_applied else "💡 Штраф не применён (недостаточно средств)"
    
    try:
        wrong_message = bot.send_message(
            chat_id,
            f"❌ <b>Неправильно!</b>\n\n"
            f"⚔️ Правильный ответ: {game_data['answer']}\n"
            f"{penalty_text}\n\n"
            f"<i>Новый пример черезсу...</i>",
            parse_mode='HTML'
        )
        # Удалить через 2сы
        threading.Timer(2, delete_message, args=[chat_id, wrong_message.message_id]).start()
    except:
        pass

def next_math_problem(user_id):
    """Генерирует следующий пример"""
    if user_id not in active_math_games:
        return
    
    # Проверяем дневной лимит
    today = datetime.now().strftime("%Y-%m-%d")
    attempts_today = get_daily_attempts(user_id, today)
    
    if attempts_today >= MATH_SYSTEM["max_daily_attempts"]:
        # Лимит исчерпан
        end_math_game(user_id, "Дневной лимит исчерпан!")
        return
    
    # Генерируем новый пример
    difficulty = min(3, 1 + active_math_games[user_id]['solved_count'] // 10)
    problem, answer = generate_math_problem(difficulty)
    
    # Обновляем игру
    active_math_games[user_id].update({
        'problem': problem,
        'answer': answer,
        'start_time': time.time()
    })
    
    # Отправляем новый пример
    send_math_problem(user_id)

def end_math_game(user_id, reason=""):
    """Завершает игру"""
    if user_id not in active_math_games:
        return
    
    game_data = active_math_games[user_id]
    chat_id = game_data['chat_id']
    current_message_id = game_data.get('current_message_id')
    
    # Удаляем текущее сообщение с примером
    if current_message_id:
        try:
            bot.delete_message(chat_id, current_message_id)
        except:
            pass
    
    total_time = time.time() - game_data['game_start_time']
    
    # Отправляем итоги (это сообщение останется)
    try:
        bot.send_message(
            chat_id,
            f"🚪 <b>Игра окончена!</b>\n\n"
            f"📝 Причина: {reason}\n\n"
            f"📊 Результаты:\n"
            f"• Примеров решено: {game_data['solved_count']}\n"
            f"• Правильно: {game_data['correct_count']}\n"
            f"• Заработано: {format_balance(game_data['total_earned'])}\n"
            f"• Время игры: {total_time:.1f} сек\n\n"
            f"💡 Возвращайтесь в любое время!",
            parse_mode='HTML'
        )
    except:
        pass
    
    # Очищаем данные
    if user_id in active_math_games:
        del active_math_games[user_id]

# ===================== ОБРАБОТЧИКИ КОМАНД =====================
@bot.message_handler(func=lambda message: message.text == "Математик")
def handle_math_start(message):
    user_id = message.from_user.id
    
    # Проверяем, не играет ли уже
    if user_id in active_math_games:
        bot.send_message(message.chat.id, "❌ У вас уже есть активная игра! Используйте /выход чтобы выйти.", parse_mode='HTML')
        return
    
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("▶️ Начать"),
        KeyboardButton("📊 Статистика"),
        KeyboardButton("🏆 Топ математиков"),
        KeyboardButton("🔙 Назад")
    )
    
    # Получаем статистику
    stats = get_math_stats(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    attempts_today = get_daily_attempts(user_id, today)
    
    info_text = (
        f"🧮 <b>Математик</b>\n\n"
        f"Решай примеры и зарабатывай:\n"
        f"✅ Правильно → +{format_balance(MATH_SYSTEM['reward_per_correct'])}\n"
        f"❌ Ошибка → -{format_balance(MATH_SYSTEM['penalty_per_wrong'])}\n"
        f"⏱ Лимит: {MATH_SYSTEM['time_limit']}с на ответ · {MATH_SYSTEM['max_daily_attempts']} в день\n\n"
        f"📊 <b>Твоя статистика:</b>\n"
        f"Решено: {stats['problems_solved']} · Верно: {stats['correct_answers']}\n"
        f"Заработано: {format_balance(stats['total_earned'])}\n"
        f"Сегодня: {attempts_today}/{MATH_SYSTEM['max_daily_attempts']}"
    )
    
    bot.send_message(message.chat.id, info_text, reply_markup=markup, parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text in ["▶️ Начать", "▶️ Начать решение"])
def handle_math_begin(message):
    user_id = message.from_user.id
    
    if user_id in active_math_games:
        bot.send_message(message.chat.id, "❌ У вас уже есть активная игра! Введите ответ в чат.", parse_mode='HTML')
        return
    
    # Проверяем лимит
    today = datetime.now().strftime("%Y-%m-%d")
    attempts_today = get_daily_attempts(user_id, today)
    
    if attempts_today >= MATH_SYSTEM["max_daily_attempts"]:
        bot.send_message(message.chat.id, "❌ Дневной лимит исчерпан! Завтра можно снова.", parse_mode='HTML')
        return
    
    # Начинаем игру
    start_math_game(user_id, message.chat.id)

@bot.message_handler(func=lambda message: message.from_user.id in active_math_games and message.text.strip().isdigit())
def handle_math_answer(message):
    user_id = message.from_user.id
    
    if user_id not in active_math_games:
        return
    
    # Планируем удаление сообщения пользователя через 2сы
    threading.Timer(2, delete_message, args=[message.chat.id, message.message_id]).start()
    
    game_data = active_math_games[user_id]
    
    try:
        user_answer = int(message.text.strip())
        correct_answer = game_data['answer']
        answer_time = time.time() - game_data['start_time']
        
        # Проверяем не истекло ли время
        if answer_time > MATH_SYSTEM["time_limit"] + 1:  # +1са на задержку
            bot.reply_to(message, "❌ Время вышло! Слишком медленно.", parse_mode='HTML')
            # Удалим это сообщение тоже через 2сы
            threading.Timer(2, delete_message, args=[message.chat.id, message.message_id + 1]).start()
            next_math_problem(user_id)
            return
        
        # Проверяем правильность
        if user_answer == correct_answer:
            # Правильный ответ
            handle_correct_answer(user_id, answer_time)
        else:
            # Неправильный ответ
            handle_wrong_answer(user_id)
            
        # Следующий пример через 1су
        threading.Timer(1, next_math_problem, args=[user_id]).start()
        
    except ValueError:
        bot.reply_to(message, "❌ Незачто, впиши только число (например: 42)", parse_mode='HTML')
        threading.Timer(2, delete_message, args=[message.chat.id, message.message_id + 1]).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith('math_exit_'))
def handle_math_exit(call):
    try:
        user_id = int(call.data.split('_')[2])
        
        if call.from_user.id != user_id:
            bot.answer_callback_query(call.id, "❌ Это не твоя игра!")
            return
        
        end_math_game(user_id, "Игрок вышел")
        bot.answer_callback_query(call.id, "🏆 Игра окончена!")
        
    except:
        bot.answer_callback_query(call.id, "❌ Ошибка!")

@bot.message_handler(commands=['выход', 'exit', 'стоп'])
def handle_math_exit_command(message):
    user_id = message.from_user.id
    
    if user_id in active_math_games:
        end_math_game(user_id, "Игрок вышел по команде")
        # Удалим команду пользователя через 2сы
        threading.Timer(2, delete_message, args=[message.chat.id, message.message_id]).start()
    else:
        bot.send_message(message.chat.id, "❌ У вас нет активной игры.", parse_mode='HTML')

# ===================== СТАТИСТИКА И ТОП =====================
@bot.message_handler(func=lambda message: message.text in ["📊 Статистика", "Моя статистика"])
def handle_math_stats(message):
    user_id = message.from_user.id
    stats = get_math_stats(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    attempts_today = get_daily_attempts(user_id, today)
    
    # Рассчитываем точность
    total = stats['correct_answers'] + stats['wrong_answers']
    accuracy = (stats['correct_answers'] / total * 100) if total > 0 else 0
    
    # Среднее время
    avg_time = stats['total_time_spent'] / stats['correct_answers'] if stats['correct_answers'] > 0 else 0
    
    message_text = (
        f"📊 <b>Твоя статистика математика</b>\n\n"
        f"⚔️ Общая:\n"
        f"• Решено примеров: {stats['problems_solved']}\n"
        f"• Правильных: {stats['correct_answers']}\n"
        f"• Ошибок: {stats['wrong_answers']}\n"
        f"• Точность: {accuracy:.1f}%\n\n"
        f"💵 Финансы:\n"
        f"• Заработано: {format_balance(stats['total_earned'])}\n"
        f"• Средний заработок: {format_balance(stats['total_earned'] // max(1, stats['problems_solved']))}\n\n"
        f"⏱ Рекорды:\n"
        f"• Лучшее время: {stats['best_time']:.2f} сек\n"
        f"• Среднее время: {avg_time:.2f} сек\n"
        f"• Всего времени: {stats['total_time_spent']:.0f} сек\n\n"
        f"📆 Сегодня:\n"
        f"• Примеров решено: {attempts_today}/{MATH_SYSTEM['max_daily_attempts']}\n\n"
        f"💡 Продолжайте тренироваться!"
    )
    
    bot.send_message(message.chat.id, message_text, parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text == "🏆 Топ математиков")
def handle_math_top(message):
    show_math_top(message.chat.id)

def show_math_top(chat_id, page=0):
    """Показывает топ математиков"""
    limit = 10
    offset = page * limit
    
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT ms.user_id, ms.correct_answers, ms.total_earned, ms.best_time,
                   u.username, u.first_name, u.custom_name
            FROM math_stats ms
            JOIN users u ON ms.user_id = u.user_id
            WHERE ms.correct_answers > 0
            ORDER BY ms.correct_answers DESC, ms.best_time ASC
            LIMIT ? OFFSET ?
        ''', (limit, offset))
        
        top_math = cursor.fetchall()
        
        cursor.execute('SELECT COUNT(*) FROM math_stats WHERE correct_answers > 0')
        total_players = cursor.fetchone()[0]
    
    message_text = f"🏆 <b>Топ математиков</b>\n\n"
    
    if not top_math:
        message_text += "Пока нет статистики!\nСтаньте первым математиком!"
    else:
        for i, (user_id, correct, earned, best_time, username, first_name, custom_name) in enumerate(top_math, 1):
            display_name = custom_name if custom_name else (f"@{username}" if username else first_name)
            best_time_display = f"{best_time:.2f} сек" if best_time else "—"
            
            message_text += f"{i}. {display_name}\n"
            message_text += f"   🏆 {correct} примеров | 🏆 {best_time_display}\n"
            message_text += f"   💵 {format_balance(earned)}\n\n"
    
    message_text += f"\n📊 Всего математиков: {total_players}"
    
    # Кнопки навигации
    markup = InlineKeyboardMarkup()
    nav_buttons = []
    
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"math_top_{page-1}"))
    
    nav_buttons.append(InlineKeyboardButton(f"Страница {page+1}", callback_data="math_top_current"))
    
    if (page + 1) * limit < total_players:
        nav_buttons.append(InlineKeyboardButton("➡️ Вперед", callback_data=f"math_top_{page+1}"))
    
    if nav_buttons:
        markup.row(*nav_buttons)
    
    bot.send_message(chat_id, message_text, reply_markup=markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith('math_top_'))
def handle_math_top_nav(call):
    try:
        if call.data == "math_top_current":
            bot.answer_callback_query(call.id, "Текущая страница")
            return
            
        page = int(call.data.split('_')[2])
        show_math_top(call.message.chat.id, page)
        bot.answer_callback_query(call.id)
    except:
        bot.answer_callback_query(call.id, "❌ Ошибка!")

# ===================== ИНИЦИАЛИЗАЦИЯ В ОДНОМ ФАЙЛЕ =====================
# Добавьте эту функцию в существующую функцию init_db():
def add_math_tables_to_init_db():
    """Добавляет таблицы математика в init_db()"""
    # Найдите функцию init_db() в вашем коде и добавьте в неё:
    with get_db_cursor() as cursor:
        # ... существующий код init_db() ...
        
        # После создания других таблиц добавьте:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS math_stats (
                user_id INTEGER PRIMARY KEY,
                problems_solved INTEGER DEFAULT 0,
                correct_answers INTEGER DEFAULT 0,
                wrong_answers INTEGER DEFAULT 0,
                total_earned INTEGER DEFAULT 0,
                best_time REAL DEFAULT 0,
                total_time_spent REAL DEFAULT 0,
                last_solve_time TIMESTAMP DEFAULT 0,
                daily_attempts_today INTEGER DEFAULT 0,
                last_reset_date TEXT DEFAULT ''
            )
        ''')
        
        # Создаем индексы для оптимизации
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_math_stats_correct ON math_stats(correct_answers DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_math_stats_time ON math_stats(best_time ASC)')
        
        print("🏆 Таблицы математика созданы")

# И добавьте этот вызов в самое начало файла (после определения функций):
# Это нужно добавить только если у вас нет автоматической инициализации
try:
    init_math_tables_in_db()
    print("🏆 Система математика загружена!")
except:
    print("⚠️ Таблицы математика уже существуют")
# Команда для просмотра информации о вещи
@bot.message_handler(func=lambda message: message.text.lower().startswith('инфо вещь ') and is_admin(message.from_user.id))
def handle_item_info(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        target = message.text[10:].strip()  # "инфо вещь " = 10 символов
        
        with get_db_cursor() as cursor:
            if target.isdigit():
                # Поиск по ID
                cursor.execute('''
                    SELECT cs.*, COUNT(uc.user_id) as owners_count
                    FROM clothes_shop cs
                    LEFT JOIN user_clothes uc ON cs.id = uc.item_id
                    WHERE cs.id = ?
                    GROUP BY cs.id
                ''', (int(target),))
            else:
                # Поиск по названию
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
                # Показываем список найденных вещей
                items_text = f"🔍 Найдено {len(items)} вещей:\n\n"
                for item in items:
                    items_text += f"🆔 {item['id']} - {item['name']} - {format_balance(item['price'])} - владельцев: {item['owners_count']}\n"
                
                bot.send_message(message.chat.id, items_text)
                return
            
            # Найден один предмет
            item = items[0]
            
            # Получаем список владельцев
            cursor.execute('''
                SELECT u.user_id, u.username, u.first_name, u.custom_name, uc.equipped
                FROM user_clothes uc
                JOIN users u ON uc.user_id = u.user_id
                WHERE uc.item_id = ?
                ORDER BY uc.equipped DESC, u.user_id
            ''', (item['id'],))
            
            owners = cursor.fetchall()
            
            # Формируем информацию
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
                for owner in owners[:10]:  # Ограничиваем 10 владельцами
                    user_id, username, first_name, custom_name, equipped = owner
                    display_name = custom_name if custom_name else (f"@{username}" if username else first_name)
                    status = "🏆 Надето" if equipped else "👕 В инвентаре"
                    info_text += f"• {display_name} ({status})\n"
                
                if len(owners) > 10:
                    info_text += f"... и еще {len(owners) - 10} владельцев"
            else:
                info_text += "🎴 Владельцев нет"
            
            # Добавляем кнопку удаления
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🗑️ Удалить вещь", callback_data=f"confirm_delete_item_{item['id']}"))
            
            bot.send_message(message.chat.id, info_text, reply_markup=markup)
            
    except Exception as e:
        print(f"❌ Ошибка получения информации о вещи: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка!", parse_mode='HTML')
# Улучшенная админ команда выдачи вещи
@bot.message_handler(func=lambda message: message.text.lower().startswith('хуй вещь ') and message.reply_to_message and is_admin(message.from_user.id))
def handle_give_item(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        # Получаем ID целевого пользователя
        target_user_id = message.reply_to_message.from_user.id
        
        # Парсим название вещи (берем всё после "выдать вещь ")
        full_text = message.text
        item_name = full_text[12:].strip()  # "выдать вещь " = 12 символов
        
        print(f"🔍 Ищем вещь: '{item_name}'")  # Дебаг
        
        if not item_name:
            bot.send_message(message.chat.id, "❌ Укажите название вещи!", parse_mode='HTML')
            return
        
        # Ищем вещь в магазине (более гибкий поиск)
        with get_db_cursor() as cursor:
            # Пробуем разные варианты поиска
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
                f'%{item_name}%',      # Любая часть названия
                f'{item_name}%',       # Начинается с
                f'%{item_name}',       # Заканчивается на
                item_name,             # Точное совпадение
                f'{item_name}%'        # Начинается с (для приоритета)
            ))
            
            items = cursor.fetchall()
            
            print(f"🔍 Найдено {len(items)} вещей")  # Дебаг
            
            if not items:
                # Показываем доступные вещи для помощи
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
                # Показываем список найденных вещей с кнопками
                items_text = f"🔍 Найдено {len(items)} вещей:\n\n"
                markup = InlineKeyboardMarkup()
                
                for i, (item_id, name, price, item_type) in enumerate(items[:10]):  # Ограничиваем 10 вещами
                    items_text += f"{i+1}. {name} ({format_balance(price)})\n"
                    markup.add(InlineKeyboardButton(
                        f"🎁 {name}", 
                        callback_data=f"give_item_{target_user_id}_{item_id}"
                    ))
                
                bot.send_message(message.chat.id, items_text, reply_markup=markup)
                return
            
            # Найден один предмет - выдаем сразу
            item_id, item_name, item_price, item_type = items[0]
            give_item_to_user(target_user_id, item_id, item_name, item_price, message.chat.id)
            
    except Exception as e:
        print(f"❌ Ошибка выдачи вещи: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

# Функция выдачи вещи пользователю
def give_item_to_user(user_id, item_id, item_name, item_price, admin_chat_id=None):
    try:
        with get_db_cursor() as cursor:
            # Проверяем, есть ли уже эта вещь у пользователя
            cursor.execute('SELECT * FROM user_clothes WHERE user_id = ? AND item_id = ?', (user_id, item_id))
            if cursor.fetchone():
                if admin_chat_id:
                    bot.send_message(admin_chat_id, f"❌ У пользователя уже есть {item_name}!", parse_mode='HTML')
                return False
            
            # Выдаем вещь
            cursor.execute('INSERT INTO user_clothes (user_id, item_id) VALUES (?, ?)', (user_id, item_id))
            
            # Получаем информацию о пользователе
            user_info = get_user_info(user_id)
            user_name = user_info['custom_name'] if user_info['custom_name'] else (
                f"@{user_info['username']}" if user_info['username'] else user_info['first_name']
            )
            
            # Уведомляем админа
            if admin_chat_id:
                bot.send_message(admin_chat_id, 
                               f"🏆 Вещь выдана!\n\n"
                               f"🎴 Пользователь: {user_name}\n"
                               f"🎁 Вещь: {item_name}\n"
                               f"💵 Стоимость: {format_balance(item_price)}", parse_mode='HTML')
            
            # Уведомляем пользователя
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

# Обработчик кнопок выдачи вещей
@bot.callback_query_handler(func=lambda call: call.data.startswith('give_item_'))
def handle_give_item_button(call):
    try:
        parts = call.data.split('_')
        target_user_id = int(parts[2])
        item_id = int(parts[3])
        
        # Получаем информацию о вещи
        with get_db_cursor() as cursor:
            cursor.execute('SELECT name, price FROM clothes_shop WHERE id = ?', (item_id,))
            item = cursor.fetchone()
            
            if not item:
                bot.answer_callback_query(call.id, "❌ Вещь не найдено!")
                return
            
            item_name, item_price = item
            
            # Выдаем вещь
            success = give_item_to_user(target_user_id, item_id, item_name, item_price, call.message.chat.id)
            
            if success:
                bot.answer_callback_query(call.id, f"🏆 Выдано: {item_name}")
                # Удаляем кнопки
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            else:
                bot.answer_callback_query(call.id, "❌ Ошибка!")
                
    except Exception as e:
        print(f"❌ Ошибка в handle_give_item_button: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка!")
# Новогодний ивент
NEW_YEAR_EVENT = {
    "active": True,
    "start_date": "2024-12-01",
    "end_date": "2025-01-10",
    "snowball_damage": 10,  # 100М урона за снежок
    "snowball_cooldown": 300,  # 5м кулдаун
    "max_snowballs_per_day": 50
}
# Адвент-календарь награды за каждый день декабря (х5)
ADVENT_CALENDAR = {
    1: 500000000,    # 500М (было 100М)
    2: 750000000,    # 750М (было 150М)  
    3: 1000000000,   # 1ккк (было 200М)
    4: 1250000000,   # 1.25ккк (было 250М)
    5: 1500000000,   # 1.5ккк (было 300М)
    6: 2000000000,   # 2ккк (было 400М)
    7: 2500000000,   # 2.5ккк (было 500М)
    8: 3000000000,   # 3ккк (было 600М)
    9: 3500000000,   # 3.5ккк (было 700М)
    10: 4000000000,  # 4ккк (было 800М)
    11: 4500000000,  # 4.5ккк (было 900М)
    12: 5000000000,  # 5ккк (было 1ккк)
    13: 6000000000,  # 6ккк (было 1.2ккк)
    14: 7000000000,  # 7ккк (было 1.4ккк)
    15: 8000000000,  # 8ккк (было 1.6ккк)
    16: 9000000000,  # 9ккк (было 1.8ккк)
    17: 10000000000, # 10ккк (было 2ккк)
    18: 12500000000, # 12.5ккк (было 2.5ккк)
    19: 15000000000, # 15ккк (было 3ккк)
    20: 17500000000, # 17.5ккк (было 3.5ккк)
    21: 20000000000, # 20ккк (было 4ккк)
    22: 22500000000, # 22.5ккк (было 4.5ккк)
    23: 25000000000, # 25ккк (было 5ккк)
    24: 50000000000, # 50ккк - Сочельник (было 10ккк)
    25: 75000000000, # 75ккк - Рождество (было 15ккк)
    26: 10000000000, # 10ккк (было 2ккк)
    27: 12500000000, # 12.5ккк (было 2.5ккк)
    28: 15000000000, # 15ккк (было 3ккк)
    29: 17500000000, # 17.5ккк (было 3.5ккк)
    30: 20000000000, # 20ккк (было 4ккк)
    31: 25000000000  # 25ккк - Новый год (было 5ккк)
}

# Таблицы для новогоднего ивента
def init_new_year_tables():
    """Инициализация всех таблиц новогоднего ивента"""
    with get_db_cursor() as cursor:
        # Таблица адвент-календаря
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS advent_calendar (
                user_id INTEGER PRIMARY KEY,
                claimed_days TEXT DEFAULT '[]',
                last_claim_date TEXT,
                total_claimed INTEGER DEFAULT 0,
                total_rewards INTEGER DEFAULT 0
            )
        ''')
        
        # Таблица снежков
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS snowball_fight (
                user_id INTEGER PRIMARY KEY,
                snowballs_thrown INTEGER DEFAULT 0,
                snowballs_received INTEGER DEFAULT 0,
                damage_dealt INTEGER DEFAULT 0,
                damage_received INTEGER DEFAULT 0,
                last_snowball_time INTEGER DEFAULT 0,
                snowballs_today INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0
            )
        ''')
        
        print("🏆 Таблицы новогоднего ивента созданы")

def get_advent_calendar_text(user_id):
    """Получить текст адвент-календаря"""
    current_day = datetime.now().day
    current_month = datetime.now().month
    
    # Проверяем работает ли ивент
    if current_month != 12:
        return "🎄 Адвент-календарь доступен только в декабре!"
    
    with get_db_cursor() as cursor:
        cursor.execute('SELECT claimed_days, total_rewards FROM advent_calendar WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        claimed_days = json.loads(result[0]) if result and result[0] else []
        total_rewards = result[1] if result and result[1] else 0
    
    text = "🎄 <b>НОВОГОДНИЙ АДВЕНТ-КАЛЕНДАРЬ</b>\n\n"
    text += f"💵 <b>Всего получено: {format_balance(total_rewards)}</b>\n\n"
    
    # Определяем текущую неделю
    if current_day <= 7:
        week_start, week_end = 1, 7
    elif current_day <= 14:
        week_start, week_end = 8, 14
    elif current_day <= 21:
        week_start, week_end = 15, 21
    elif current_day <= 28:
        week_start, week_end = 22, 28
    else:
        week_start, week_end = 29, 31
    
    text += f"📆 <b>Текущая неделя (дни {week_start}-{week_end}):</b>\n"
    
    for day in range(week_start, week_end + 1):
        emoji = "🏆" if day in claimed_days else "❌"
        if day == current_day:
            emoji = "🎁"
        elif day > current_day:
            emoji = "🔒"
        
        # Не показываем сумму награды, только эмодзи и день
        reward_placeholder = "???"  # Скрываем сумму для интриги
        text += f"{emoji} День {day}: {reward_placeholder}\n"
    
    text += f"\n📆 Сегодня: {current_day} декабря\n"
    
    if current_day not in claimed_days and current_day <= 31:
        text += f"🎁 Сегодняшняя награда: ???\n"  # Скрываем сумму
        text += "\n✨ Нажми 'Забрать награду' чтобы получить подарок!"
    elif current_day > 31:
        text += "\n🎉 Адвент-календарь окончен! Благодарим за участие!"
    
    # Добавляем информацию о других неделях
    other_weeks = []
    if week_start != 1:
        other_weeks.append("1-7")
    if week_start != 8 and current_day > 7:
        other_weeks.append("8-14")
    if week_start != 15 and current_day > 14:
        other_weeks.append("15-21")
    if week_start != 22 and current_day > 21:
        other_weeks.append("22-28")
    if week_start != 29 and current_day > 28:
        other_weeks.append("29-31")
    
    if other_weeks:
        text += f"\n📋 Другие недели: {', '.join(other_weeks)}"
    
    return text

def handle_claim_advent(call):
    """Забрать награду адвент-календаря"""
    user_id = call.from_user.id
    current_day = datetime.now().day
    current_month = datetime.now().month
    
    if current_month != 12:
        bot.answer_callback_query(call.id, "❌ Адвент-календарь доступен только в декабре!")
        return
    
    if current_day > 31:
        bot.answer_callback_query(call.id, "❌ Адвент-календарь закончился!")
        return
    
    with get_db_cursor() as cursor:
        cursor.execute('SELECT claimed_days, total_rewards FROM advent_calendar WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if result:
            claimed_days = json.loads(result[0])
            total_rewards = result[1]
            if current_day in claimed_days:
                bot.answer_callback_query(call.id, "❌ Вы уже забрали награду за сегодня!")
                return
        else:
            claimed_days = []
            total_rewards = 0
        
        # Добавляем день в забранные
        claimed_days.append(current_day)
        reward = ADVENT_CALENDAR[current_day]
        new_total_rewards = total_rewards + reward
        
        # Начисляем награду
        update_balance(user_id, reward)
        
        # Обновляем базу данных
        cursor.execute('''
            INSERT OR REPLACE INTO advent_calendar 
            (user_id, claimed_days, last_claim_date, total_claimed, total_rewards) 
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, json.dumps(claimed_days), datetime.now().strftime("%Y-%m-%d"), len(claimed_days), new_total_rewards))
        
        # Обновляем сообщение
        text = get_advent_calendar_text(user_id)
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔄 Обновить", callback_data="refresh_advent"))
        
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='HTML')
        # В уведомлении показываем реальную сумму
        bot.answer_callback_query(call.id, f"🎁 Получено: {format_balance(reward)}!")

@bot.message_handler(func=lambda message: message.text.lower() == 'стата адвент')
def handle_advent_stats(message):
    """Показать статистику адвент-календаря (только в ЛС)"""
    # Проверяем что это личные сообщения
    if message.chat.type != 'private':
        bot.reply_to(message, "❌ Статистика адвент-календаря доступна только в личных сообщениях с ботом!", parse_mode='HTML')
        return
        
    user_id = message.from_user.id
    current_day = datetime.now().day
    current_month = datetime.now().month
    
    if current_month != 12:
        bot.send_message(message.chat.id, "🎄 Адвент-календарь доступен только в декабре!")
        return
    
    with get_db_cursor() as cursor:
        cursor.execute('SELECT claimed_days, total_claimed, total_rewards FROM advent_calendar WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if not result:
            text = "📊 <b>СТАТИСТИКА АДВЕНТ-КАЛЕНДАРЯ</b>\n\n"
            text += "Ты еще не получал наград из календаря!\n"
            text += "🎁 Используй команду 'адвент' чтобы начать!"
        else:
            claimed_days_json, total_claimed, total_rewards = result
            claimed_days = json.loads(claimed_days_json) if claimed_days_json else []
            
            text = "📊 <b>ТВОЯ СТАТИСТИКА АДВЕНТ-КАЛЕНДАРЯ</b>\n\n"
            text += f"📆 Полученод: {total_claimed}/31\n"
            text += f"💵 Всего наград: {format_balance(total_rewards)}\n"
            text += f"📈 Средняя награда: {format_balance(total_rewards // max(1, total_claimed))}\n\n"
            
            # Пропущенные дни (не показываем суммы)
            missed_days = [day for day in range(1, min(current_day, 32)) if day not in claimed_days]
            if missed_days:
                text += f"❌ Пропущенод: {len(missed_days)}\n\n"
            
            # Следующие награды (не показываем суммы)
            if current_day < 31:
                upcoming_days = [day for day in range(current_day + 1, min(current_day + 4, 32))]
                if upcoming_days:
                    text += "🔮 Ближайшие награды:\n"
                    for day in upcoming_days:
                        text += f"🎁 День {day}: ???\n"  # Скрываем сумму
    
    bot.send_message(message.chat.id, text, parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower() in ['адвент', 'календарь', 'адвенткалендарь'])
def handle_advent_calendar(message):
    """Показать адвент-календарь (только в ЛС)"""
    # Проверяем что это личные сообщения
    if message.chat.type != 'private':
        bot.reply_to(message, "❌ Адвент-календарь доступен только в личных сообщениях с ботом!", parse_mode='HTML')
        return
    
    user_id = message.from_user.id
    
    text = get_advent_calendar_text(user_id)
    markup = InlineKeyboardMarkup()
    
    current_day = datetime.now().day
    current_month = datetime.now().month
    
    if current_month == 12:
        with get_db_cursor() as cursor:
            cursor.execute('SELECT claimed_days FROM advent_calendar WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            claimed_days = json.loads(result[0]) if result and result[0] else []
            
            if current_day not in claimed_days and current_day <= 31:
                markup.add(InlineKeyboardButton("🎁 Забрать награду", callback_data="claim_advent"))
    
    markup.add(InlineKeyboardButton("🔄 Обновить", callback_data="refresh_advent"))
    
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data in ["claim_advent", "refresh_advent"])
def handle_advent_callbacks(call):
    """Обработчик callback'ов адвент-календаря"""
    # Проверяем что это личные сообщения
    if call.message.chat.type != 'private':
        bot.answer_callback_query(call.id, "❌ Адвент-календарь доступен только в ЛС!")
        return
        
    user_id = call.from_user.id
    
    if call.data == "claim_advent":
        handle_claim_advent(call)
    elif call.data == "refresh_advent":
        handle_refresh_advent(call)





# Обработчик для групп - подсказка про ЛС
@bot.message_handler(func=lambda message: message.chat.type != 'private' and message.text.lower() in ['адвент', 'календарь', 'адвенткалендарь', 'стата адвент'])
def handle_advent_in_group(message):
    """Обработчик команд адвента в группах"""
    bot.reply_to(message, 
                "🎄 Адвент-календарь доступен только в личных сообщениях с ботом!\n\n"
                "💌 Напиши мне в ЛС команду 'адвент' чтобы получить новогодние подарки!",
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("📨 Написать в ЛС", url=f"https://t.me/{bot.get_me().username}")
                ))

# Система снежков
NEW_YEAR_EVENT = {
    "active": True,
    "snowball_damage": 50,  # 100М урона за снежок
    "snowball_cooldown": 300,  # 5м кулдаун
    "max_snowballs_per_day": 50
}



@bot.message_handler(func=lambda message: message.text.lower() == 'новый год')
def handle_new_year_info(message):
    """Показать информацию о новогоднем ивенте"""
    current_day = datetime.now().day
    current_month = datetime.now().month
    
    text = "🎄 <b>НОВОГОДНИЙ ИВЕНТ</b>\n\n"
    
    if current_month == 12:
        text += "❄️ <b>Активные события:</b>\n"
        text += "• 🎄 Адвент-календарь - забирай награды каждый день! (только в ЛС)\n"
        text += "• ❄️ Снежные бои - кидай снежки в друзей!\n"
        text += "• 🏆 Топ снежкометов - стань лучшим!\n\n"
        
        text += f"📆 Сегодня: {current_day} декабря\n"
        text += "⏰ Ивент до: 10 января\n\n"
        
        text += "⚔️ <b>Команды:</b>\n"
        text += "<code>адвент</code> - открыть календарь (только в ЛС)\n"
        text += "<code>снежок</code> - кинуть снежок (ответом на сообщение)\n"
        text += "<code>снежки</code> - твоя статистика\n"
        text += "<code>топ снежки</code> - топ игроков\n"
    else:
        text += "❄️ Новогодний ивент окончен!\n"
        text += "Следите за анонсами в следующем году! 🎅"
    
    bot.send_message(message.chat.id, text, parse_mode='HTML')

# Инициализация таблиц при старте
init_new_year_tables()
print("🏆 Новогодний ивент с адвент-календарем загружен!")
# Простая команда для поиска вещей
@bot.message_handler(func=lambda message: message.text.lower().startswith('найти вещь ') and is_admin(message.from_user.id))
def handle_find_item(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        search_term = message.text[11:].strip()  # "найти вещь " = 11 символов
        
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
# Глобальное хранилище забаненных пользователей
banned_users = set()

# Функция для бана пользователя
def ban_user(user_id, reason="Нарушение правил"):
    # Админов нельзя забанить
    if is_admin(user_id):
        return False
    banned_users.add(user_id)
    print(f"🔨 Пользователь {user_id} забанен. Причина: {reason}")
    return True

# Функция для разбана пользователя
def unban_user(user_id):
    if user_id in banned_users:
        banned_users.remove(user_id)
        print(f"🔓 Пользователь {user_id} разбанен")

# Функция проверки бана (админы не могут быть забанены)
def is_user_banned(user_id):
    if is_admin(user_id):
        return False
    return user_id in banned_users

# Обработчик для проверки бана перед основными командами
@bot.message_handler(func=lambda message: is_user_banned(message.from_user.id))
def handle_banned_user(message):
    user_id = message.from_user.id
    
    # Удаляем сообщение забаненного пользователя в группах
    if message.chat.type != 'private':
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
    
    # Отправляем сообщение о бане (только в ЛС, чтобы не спамить в группах)
    if message.chat.type == 'private':
        bot.send_message(
            user_id,
            "🚫 <b>Вы забанены в боте!</b>\n\n"
            "❌ Вы не можете использовать никакие функции бота.\n"
            "📞 Для разбана обратитесь к админу.",
            parse_mode='HTML'
        )
    
    return True  # Останавливаем обработку

# Обработчик callback-запросов для забаненных пользователей
@bot.callback_query_handler(func=lambda call: is_user_banned(call.from_user.id))
def handle_banned_user_callback(call):
    bot.answer_callback_query(call.id, "🚫 Вы забанены в боте!", show_alert=True)
    return True

# Админ команды для бана/разбана
@bot.message_handler(func=lambda message: message.text.lower().startswith('бан ') and is_admin(message.from_user.id))
def handle_ban_user(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Используйте: бан [user_id/@username] [причина]", parse_mode='HTML')
            return
        
        target = parts[1]
        reason = ' '.join(parts[2:]) if len(parts) > 2 else "Нарушение правил"
        
        # Определяем ID пользователя
        target_user_id = None
        
        if target.startswith('@'):
            # Поиск по username
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
            # По ID
            try:
                target_user_id = int(target)
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный формат ID пользователя!", parse_mode='HTML')
                return
        
        # Проверяем, не админ ли
        if is_admin(target_user_id):
            bot.send_message(message.chat.id, "❌ Нельзя забанить админа!", parse_mode='HTML')
            return
        
        # Баним пользователя
        success = ban_user(target_user_id, reason)
        
        if success:
            # Уведомляем админа
            bot.send_message(
                message.chat.id,
                f"🔨 Пользователь {target_user_id} забанен!\n"
                f"📝 Причина: {reason}"
            )
            
            # Уведомляем пользователя
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
        
        # Определяем ID пользователя
        target_user_id = None
        
        if target.startswith('@'):
            # Поиск по username
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
            # По ID
            try:
                target_user_id = int(target)
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный формат ID пользователя!", parse_mode='HTML')
                return
        
        # Разбаниваем пользователя
        unban_user(target_user_id)
        
        # Уведомляем админа
        bot.send_message(
            message.chat.id,
            f"🔓 Пользователь {target_user_id} разбанен!"
        )
        
        # Уведомляем пользователя
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

# Словарь для хранения активных рейсов
active_rides = {}  # {user_id: timer_object}

# Настройки классов водителей
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

# Настройки заказов такси
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

# Создание таблиц для такси
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
        
        # Таблица активных заказов - пересоздаем с правильной структурой
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
        
        # Добавляем недостающие поля
        try:
            cursor.execute('ALTER TABLE driver_stats ADD COLUMN driver_class TEXT DEFAULT "economy"')
        except:
            pass
            
        try:
            cursor.execute('ALTER TABLE driver_stats ADD COLUMN unlocked_classes TEXT DEFAULT "economy"')
        except:
            pass

# Работа с активными заказами в БД
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
    # Останавливаем таймер если есть
    if user_id in active_rides:
        try:
            active_rides[user_id].cancel()
        except:
            pass
        del active_rides[user_id]
    
    with get_db_cursor() as cursor:
        trip_json = json.dumps(trip_data)
        # Удаляем старую запись если есть
        cursor.execute('DELETE FROM active_trips WHERE user_id = ?', (user_id,))
        # Вставляем новую
        cursor.execute('''
            INSERT INTO active_trips 
            (user_id, trip_data, chat_id, message_id, start_time, finish_time) 
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, trip_json, chat_id, message_id, int(time.time()), None))

def cancel_active_trip(user_id):
    """Отменить активную поездку"""
    # Останавливаем таймер
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
    # Останавливаем все таймеры
    for user_id in list(active_rides.keys()):
        try:
            active_rides[user_id].cancel()
        except:
            pass
    active_rides.clear()
    
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE active_trips SET finish_time = ? WHERE finish_time IS NULL', (int(time.time()),))
        return cursor.rowcount

# Функции для работы с классами водителей
def get_driver_class_info(user_id):
    """Получить информацию о классе водителя"""
    stats = get_driver_stats(user_id)
    trips_done = stats['trips_completed']
    
    # Определяем класс по количеству поездок
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
        
        # Обновляем класс если изменился
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
    
    # Определяем следующий класс
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

# Обработчик команды "Такси"
@bot.message_handler(func=lambda message: message.text.lower() in ["такси", "🚗 такси"])
def handle_taxi_command(message):
    user_id = message.from_user.id
    show_taxi_menu(message.chat.id, user_id)

# Обработчик кнопки Такси
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

# Обработчик кнопки "Выехать на линию"
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
    
    # Фильтруем заказы по классу
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
    
    # Выбираем случайный маршрут
    route = random.choice(available)
    
    # Добавляем вариативность цены
    variation = route["variation"]
    random_factor = 1 + random.uniform(-variation, variation)
    base = int(route["base_price"] * random_factor)
    
    # Применяем классовый бонус
    final_price = apply_class_bonus(base, current_class)
    
    # Считаем опыт с бонусом
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

# Обработчик кнопки "Рейтинг водителей"
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

# Обработчик кнопки "Моя статистика"
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

# Обработчик кнопки "Мой уровень"
@bot.message_handler(func=lambda message: message.text == "⭐ Мой уровень")
def handle_my_level(message):
    user_id = message.from_user.id
    show_driver_class(message.chat.id, user_id)

# Обработчик кнопки "Назад"
@bot.message_handler(func=lambda message: message.text in ["🔙 Назад", "◀️ Назад"])
def handle_back_from_taxi(message):
    user_id = message.from_user.id
    markup = create_main_menu()
    bot.send_message(message.chat.id, "🔙 Возвращаюсь в главное меню", reply_markup=markup)

# Обработчик инлайн кнопок
@bot.callback_query_handler(func=lambda call: call.data.startswith('taxi_'))
def handle_taxi_actions(call):
    user_id = call.from_user.id
    
    if call.data == "taxi_accept":
        accept_order(call)
        
    elif call.data == "taxi_decline":
        # Отклоняем заказ
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
    
    # Останавливаем старый таймер если есть
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
    
    # Создаем таймер
    timer = threading.Timer(minutes * 60, finish_ride, 
                           [user_id, call.message.chat.id, call.message.message_id, order])
    active_rides[user_id] = timer
    timer.start()

def finish_ride(user_id, chat_id, message_id, order):
    """Завершить поездку"""
    # Удаляем таймер
    if user_id in active_rides:
        del active_rides[user_id]
    
    # Проверяем, активна ли еще поездка
    active = get_active_trip(user_id)
    if not active:
        return
    
    try:
        # Начисляем награду
        update_balance(user_id, order["price"])
        add_experience(user_id, order["experience"])
        
        # Обновляем статистику
        update_driver_stats(user_id, order["price"])
        
        # Удаляем активную поездку
        cancel_active_trip(user_id)
        
        # Получаем обновленные данные
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

# Админ команда для очистки
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
            threshold = now - 7200  # 2 часа
            
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

# Мониторинг активных поездок
def monitor_active_trips():
    """Проверка активных поездок"""
    while True:
        try:
            with get_db_cursor() as cursor:
                now = int(time.time())
                # Ищем поездки, которые должны были завершиться
                cursor.execute('''
                    SELECT user_id, trip_data FROM active_trips 
                    WHERE finish_time IS NULL
                ''')
                
                for row in cursor.fetchall():
                    uid, data_json = row
                    data = json.loads(data_json)
                    minutes = int(data['time'].split()[0])
                    start = cursor.execute('SELECT start_time FROM active_trips WHERE user_id = ?', (uid,)).fetchone()[0]
                    
                    # Если поездка длится дольше нормы + 5 минут
                    if now - start > (minutes * 60) + 300:
                        print(f"🧹 Найдена зависшая поездка {uid}")
                        finish_ride(uid, None, None, data)
        except Exception as e:
            print(f"⚠️ Ошибка мониторинга: {e}")
        
        time.sleep(60)
# Инициализация
init_taxi_database()
cleanup_stuck_trips()

# Запускаем мониторинг
monitor_thread = threading.Thread(target=monitor_active_trips, daemon=True)
monitor_thread.start()

print("✅ Система такси готова к работе!")
# Команда для удаления всех займов
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
    interval = 10800  # 3 часа
    users_to_notify = []

    with get_db_cursor() as cursor:
        cursor.execute(
            'SELECT user_id, bank_deposit FROM users WHERE bank_deposit > 0 AND last_interest_calc <= ? - ?',
            (current_time, interval)
        )
        rows = cursor.fetchall()
        for uid, deposit in rows:
            rate = 0.01 if is_premium(uid) else 0.005  # 1% / 0.5% каждые 3 часа
            earned = max(1, int(deposit * rate))
            new_deposit = deposit + earned
            cursor.execute(
                'UPDATE users SET bank_deposit = ?, last_interest_calc = ? WHERE user_id = ?',
                (new_deposit, current_time, uid)
            )
            users_to_notify.append((uid, earned, new_deposit, is_premium(uid)))

    # Уведомления вне транзакции
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
                time.sleep(3600)  # Проверяем каждый час, начисляем каждые 3
            except Exception as e:
                print(f"Ошибка в расчете процентов: {e}")
                time.sleep(300)

    thread = threading.Thread(target=interest_calculator, daemon=True)
    thread.start()
    print("🏆 Расчет процентов запущен (0.5%/3ч | премиум 1%/3ч)")

# Добавьте этот вызов в init_db() или после инициализации
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

# ── Прямая команда: вклад (сумма) ────────────────────────────────────────────
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
    # Сбрасываем таймер — отсчёт 3 часов идёт заново с момента пополнения
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

# ── Прямая команда: снять (сумма) ────────────────────────────────────────────
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

# ─────────────────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda message: message.text == "🏛 Банк")
def handle_bank(message):
    user_id = message.from_user.id
    balance = get_balance(user_id)
    deposit = get_bank_deposit(user_id)

    text = (
        f"🏛 <b>Банк</b>\n\n"
        f"💰 Баланс: {format_balance(balance)}\n"
        f"🏦 Депозит: {format_balance(deposit)}\n\n"
        f"<i>Депозит приносит 0.5% в день (1% для Premium)</i>"
    )
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("💰 Внести", callback_data="show_deposit_menu"),
        InlineKeyboardButton("💸 Снять", callback_data="show_withdraw_menu")
    )
    bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=markup)

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
        # Сбрасываем таймер — отсчёт 3 часов идёт заново с момента пополнения
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

# (дублирующий запуск потока удалён)

@bot.message_handler(func=lambda message: message.text.lower() == 'банлист' and is_admin(message.from_user.id))
def handle_ban_list(message):
    if not banned_users:
        bot.send_message(message.chat.id, "📋 Список забаненных пуст")
        return
    
    ban_list = "📋 <b>Забаненные пользователи:</b>\n\n"
    for user_id in banned_users:
        ban_list += f"• {user_id}\n"
    
    bot.send_message(message.chat.id, ban_list, parse_mode='HTML')
# Команда для просмотра всех вещей
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
            
            # Группируем по типам
            items_by_type = {}
            for item in items:
                item_id, name, price, item_type = item
                if item_type not in items_by_type:
                    items_by_type[item_type] = []
                items_by_type[item_type].append((item_id, name, price))
            
            # Формируем сообщение
            items_text = "📦 ВСЕ ВЕЩИ В МАГАЗИНЕ:\n\n"
            
            for item_type, type_items in items_by_type.items():
                items_text += f"📁 {item_type.upper()}:\n"
                for item_id, name, price in type_items:
                    items_text += f"  🆔 {item_id} - {name} - {format_balance(price)}\n"
                items_text += "\n"
            
            # Разбиваем на части если сообщение слишком длинное
            if len(items_text) > 4000:
                parts = [items_text[i:i+4000] for i in range(0, len(items_text), 4000)]
                for part in parts:
                    bot.send_message(message.chat.id, part)
            else:
                bot.send_message(message.chat.id, items_text)
                
    except Exception as e:
        print(f"❌ Ошибка показа вещей: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка!", parse_mode='HTML')
# Команда для выдачи варна
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
        duration_hours = 24  # по умолчанию 24 часа
        
        # Проверяем, указано ли время
        reason_parts = reason.split(' ')
        if reason_parts[-1].isdigit():
            duration_hours = int(reason_parts[-1])
            reason = ' '.join(reason_parts[:-1])
        
        # Определяем ID пользователя
        target_user_id = None
        
        if target.startswith('@'):
            # Поиск по username
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
            # По ID
            try:
                target_user_id = int(target)
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный формат ID пользователя!", parse_mode='HTML')
                return
        
        # Проверяем существование пользователя
        if not get_user_info(target_user_id):
            bot.send_message(message.chat.id, "❌ Пользователь отсутствует в базе данных!", parse_mode='HTML')
            return
        
        # Выдаем варн
        success = add_warn(target_user_id, reason, message.from_user.id, duration_hours)
        
        if success:
            expires_time = datetime.fromtimestamp(time.time() + (duration_hours * 3600)).strftime("%d.%m.%Y %H:%M")
            
            # Уведомляем пользователя
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

# Команда для снятия варна
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
            # Поиск по username
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
            # По ID
            try:
                target_user_id = int(target)
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный формат ID пользователя!", parse_mode='HTML')
                return
        
        # Снимаем варн
        success = remove_warn(target_user_id)
        
        if success:
            # Уведомляем пользователя
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

# Команда для проверки варна
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
            # Поиск по username
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
            # По ID
            try:
                target_user_id = int(target)
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный формат ID пользователя!", parse_mode='HTML')
                return
        
        # Проверяем варн
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
# Функция для получения или создания пользователя
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

# ===== КОМАНДЫ ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ =====
import os
import shutil
from datetime import datetime

# ===== КОМАНДА: СТАТИСТИКА БД =====
@bot.message_handler(func=lambda message: message.text.lower() == 'бд' and is_admin(message.from_user.id))
def handle_db_stats(message):
    """Показывает статистику базы данных"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        with get_db_cursor() as cursor:
            # Количество пользователей
            cursor.execute('SELECT COUNT(*) FROM users')
            total_users = cursor.fetchone()[0]
            
            # Количество забаненных
            banned_users_count = len(banned_users)
            
            # Количество чеков
            cursor.execute('SELECT COUNT(*) FROM checks')
            total_checks = cursor.fetchone()[0]
            
            # Общий баланс всех пользователей
            cursor.execute('SELECT SUM(balance) FROM users')
            total_balance = cursor.fetchone()[0] or 0
            
            # Всего на вкладах
            cursor.execute('SELECT SUM(bank_deposit) FROM users')
            total_deposits = cursor.fetchone()[0] or 0
        
        # Размер файла базы данных
        db_path = 'game.db'
        if os.path.exists(db_path):
            db_size_bytes = os.path.getsize(db_path)
            db_size_mb = db_size_bytes / (1024 * 1024)
        else:
            db_size_mb = 0
        
        # Текущая дата и время
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Формируем сообщение
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

# ===== КОМАНДА: СКАЧАТЬ БАЗУ =====
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
        
        # Создаем временную копию с датой в имени
        backup_name = f"game_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(db_path, backup_name)
        
        # Отправляем файл
        with open(backup_name, 'rb') as db_file:
            bot.send_document(
                message.chat.id,
                db_file,
                caption=f"📦 Резервная копия базы данных\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
        
        # Удаляем временную копию
        os.remove(backup_name)
        
        bot.send_message(message.chat.id, "✅ База данных отправлена", parse_mode='HTML')
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')
        print(f"Ошибка в handle_download_db: {e}")

# ===== КОМАНДА: ЗАГРУЗИТЬ БАЗУ =====
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

# ===== ОБРАБОТЧИК ПОЛУЧЕНИЯ ФАЙЛА БАЗЫ (ИСПРАВЛЕННЫЙ) =====
# ===== ОБРАБОТЧИК ПОЛУЧЕНИЯ ФАЙЛОВ (ИСПРАВЛЕННЫЙ) =====
@bot.message_handler(content_types=['document'])
def handle_document_upload(message):
    """Обрабатывает загрузку файлов: .db в базу, картинки в папку images"""
    # Проверяем права админа
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "💀 У вас нет прав для этой команды")
        return
    
    try:
        file_info = bot.get_file(message.document.file_id)
        file_name = message.document.file_name
        file_ext = file_name.lower().split('.')[-1] if '.' in file_name else ''
        
        # Скачиваем файл
        downloaded_file = bot.download_file(file_info.file_path)
        
        # ===== ЕСЛИ ЭТО БАЗА ДАННЫХ (.db) =====
        if file_ext == 'db':
            # Сохраняем старую базу как backup
            db_path = 'game.db'
            backup_path = None
            if os.path.exists(db_path):
                backup_path = f'game_auto_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
                shutil.copy2(db_path, backup_path)
                bot.send_message(message.chat.id, f"📦 Старая база сохранена как: <code>{backup_path}</code>", parse_mode='HTML')
                
                # Закрываем все соединения с БД перед заменой
                with db_pool._lock:
                    for conn in db_pool.pool:
                        try:
                            conn.close()
                        except:
                            pass
                    db_pool.pool = []
            
            # Сохраняем новую базу
            with open(db_path, 'wb') as new_file:
                new_file.write(downloaded_file)
            
            # Проверяем что файл действительно является SQLite базой
            try:
                test_conn = sqlite3.connect(db_path)
                test_conn.cursor().execute("SELECT name FROM sqlite_master LIMIT 1")
                test_conn.close()
                
                # ИНИЦИАЛИЗИРУЕМ СТРУКТУРУ ТАБЛИЦ В НОВОЙ БАЗЕ
                init_db()
                init_dice_tables()
                init_taxi_database()
                init_new_year_tables()
                init_clan_tables()
                init_math_tables_in_db()
                
                # Очищаем кэшированные данные в памяти
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
                
                # Проверяем, есть ли пользователи в новой базе
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
                    
            except sqlite3.DatabaseError:
                # Если файл не является базой SQLite - восстанавливаем старую
                bot.send_message(message.chat.id, "💀 Ошибка: загруженный файл не является базой данных SQLite!", parse_mode='HTML')
                
                if backup_path and os.path.exists(backup_path):
                    shutil.copy2(backup_path, db_path)
                    bot.send_message(message.chat.id, "✅ Старая база восстановлена", parse_mode='HTML')
        
        # ===== ЕСЛИ ЭТО КАРТИНКА (png, jpg, jpeg, gif) =====
        elif file_ext in ['png', 'jpg', 'jpeg', 'gif']:
            # Определяем имя файла (можно переименовать через подпись)
            if message.caption and message.caption.strip():
                filename = message.caption.strip()
                # Добавляем расширение если его нет
                if not filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                    filename += f".{file_ext}"
            else:
                filename = file_name
            
            # Путь для сохранения
            save_path = f"images/{filename}"
            
            # Создаем папку если её нет
            os.makedirs("images", exist_ok=True)
            
            # Сохраняем файл
            with open(save_path, 'wb') as new_file:
                new_file.write(downloaded_file)
            
            # Проверяем размер файла
            file_size = os.path.getsize(save_path)
            size_kb = file_size / 1024
            
            # Отправляем подтверждение
            success_text = (
                f"✅ <b>Изображение сохранено!</b>\n\n"
                f"📁 Путь: <code>{save_path}</code>\n"
                f"📄 Имя: {filename}\n"
                f"📊 Размер: {size_kb:.1f} KB\n\n"
                f"💡 Теперь можно использовать в магазине: <code>{filename}</code>"
            )
            
            # Показываем превью
            try:
                with open(save_path, 'rb') as photo:
                    bot.send_photo(
                        message.chat.id,
                        photo,
                        caption=success_text,
                        parse_mode='HTML'
                    )
            except:
                # Если не удалось отправить как фото, отправляем как документ
                with open(save_path, 'rb') as photo:
                    bot.send_document(
                        message.chat.id,
                        photo,
                        caption=success_text,
                        parse_mode='HTML'
                    )
        
        # ===== ЕСЛИ НЕПОДДЕРЖИВАЕМЫЙ ФОРМАТ =====
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
    
    # Закрываем все соединения с БД
    with db_pool._lock:
        for conn in db_pool.pool:
            try:
                conn.close()
            except:
                pass
        db_pool.pool = []
    
    bot.send_message(message.chat.id, "🧹 Кэш очищен!")
# Функция для получения баланса
def get_balance(user_id):
    """Получает баланс напрямую из БД БЕЗ кэширования"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result[0] if result else 0

# Функция для получения банковского вклада
def get_bank_deposit(user_id):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT bank_deposit FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result[0] if result else 0

# Функция для обновления банковского вклада
def update_bank_deposit(user_id, amount):
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE users SET bank_deposit = bank_deposit + ? WHERE user_id = ?', (amount, user_id))

# Функция для расчета и начисления процентов по вкладу


# Функция для получения информации о пользователе (ОБНОВЛЕННАЯ)
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






            
# ПЕРЕВОДЫ: Перевод денег другому пользователю
def transfer_money(from_user_id, to_user_id, amount):
    # Проверка варна
    if is_user_warned(from_user_id):
        return False, "❌ Вы не можете транзакцияить деньги, так как у вас активный варн!"
    
    with get_db_cursor() as cursor:
        # Проверяем существование получателя
        cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (to_user_id,))
        if not cursor.fetchone():
            return False, "❌ Пользователь не найден!"
        
        # Проверяем баланс отправителя
        balance = get_balance(from_user_id)
        if balance < amount:
            return False, "❌ Не хватает монет для транзакцияа!"
        
        if amount <= 0:
            return False, "❌ Сумма должна быть больше 0!"
        
        # Рассчитываем комиссию
        fee = int(amount * TRANSFER_FEE)
        net_amount = amount - fee
        
        # Выполняем транзакция
        cursor.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, from_user_id))
        cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (net_amount, to_user_id))
        
        # Записываем в историю
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
os.makedirs(CLAN_AVATARS_DIR, exist_ok=True)

user_data = {}

def init_clan_tables():
    with get_db_cursor() as cursor:
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS clan_active_wars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attacker_clan_id INTEGER NOT NULL,
            defender_clan_id INTEGER NOT NULL,
            start_time INTEGER NOT NULL,
            end_time INTEGER NOT NULL,
            status TEXT DEFAULT 'active',
            attacker_score INTEGER DEFAULT 0,
            defender_score INTEGER DEFAULT 0,
            result TEXT,
            FOREIGN KEY (attacker_clan_id) REFERENCES clans (id),
            FOREIGN KEY (defender_clan_id) REFERENCES clans (id)
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS clan_donations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clan_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            datetime INTEGER DEFAULT (strftime('%s', 'now')),
            FOREIGN KEY (clan_id) REFERENCES clans (id),
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS clan_war_participants (
            war_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            clan_id INTEGER NOT NULL,
            points_contributed INTEGER DEFAULT 0,
            PRIMARY KEY (war_id, user_id),
            FOREIGN KEY (war_id) REFERENCES clan_active_wars (id)
        )
        ''')
        
        try:
            cursor.execute('ALTER TABLE clans ADD COLUMN wars_won INTEGER DEFAULT 0')
        except:
            pass
        
        try:
            cursor.execute('ALTER TABLE clans ADD COLUMN wars_lost INTEGER DEFAULT 0')
        except:
            pass
        
        try:
            cursor.execute('ALTER TABLE clans ADD COLUMN wars_draw INTEGER DEFAULT 0')
        except:
            pass
        
        try:
            cursor.execute('ALTER TABLE clans ADD COLUMN avatar TEXT')
        except:
            pass

init_clan_tables()

def save_clan_avatar(clan_id, photo_file_id):
    file_info = bot.get_file(photo_file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    
    avatar_path = os.path.join(CLAN_AVATARS_DIR, f'{clan_id}.jpg')
    with open(avatar_path, 'wb') as new_file:
        new_file.write(downloaded_file)
    
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE clans SET avatar = ? WHERE id = ?', (avatar_path, clan_id))
        cursor.connection.commit()
    
    return avatar_path

def get_clan_avatar_path(clan_id):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT avatar FROM clans WHERE id = ?', (clan_id,))
        result = cursor.fetchone()
        return result[0] if result and result[0] else None

def set_clan_avatar(user_id, clan_id, photo_file_id=None):
    user_clan = get_user_clan(user_id)
    if not user_clan or user_clan['id'] != clan_id or user_clan['role'] != 'leader':
        return False, 'Только глава может менять аватарку'
    
    clan_info = get_clan_info(clan_id)
    if clan_info['balance'] < CLAN_CONFIG['avatar_price']:
        return False, f'Не хватает монет в казне клана\n💵 Требуется: {format_balance(CLAN_CONFIG["avatar_price"])}'
    
    if photo_file_id:
        try:
            avatar_path = save_clan_avatar(clan_id, photo_file_id)
            
            with get_db_cursor() as cursor:
                cursor.execute('UPDATE clans SET balance = balance - ? WHERE id = ?', 
                              (CLAN_CONFIG['avatar_price'], clan_id))
                cursor.connection.commit()
            
            return True, f'🏆 Аватарка установлена\n💵 Списано: {format_balance(CLAN_CONFIG["avatar_price"])}'
        
        except Exception as e:
            print(f'Ошибка сохранения аватарки: {e}')
            return False, '❌ Ошибка при установке аватарки'
    else:
        old_avatar = get_clan_avatar_path(clan_id)
        if old_avatar and os.path.exists(old_avatar):
            os.remove(old_avatar)
        
        with get_db_cursor() as cursor:
            cursor.execute('UPDATE clans SET avatar = NULL WHERE id = ?', (clan_id,))
            cursor.connection.commit()
        
        return True, '🏆 Аватарка удалена'

def send_clan_avatar(chat_id, clan_id, caption=None):
    avatar_path = get_clan_avatar_path(clan_id)
    
    if avatar_path and os.path.exists(avatar_path):
        with open(avatar_path, 'rb') as photo:
            if caption:
                bot.send_photo(chat_id, photo, caption=caption, parse_mode='HTML')
            else:
                bot.send_photo(chat_id, photo)
        return True
    return False

def create_clan(user_id, name, tag, description='', photo_file_id=None):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT clan_id FROM users WHERE user_id = ?', (user_id,))
        user_clan = cursor.fetchone()
        
        if user_clan and user_clan[0]:
            return False, '❌ Вы уже состоите в клане'
        
        cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
        balance = cursor.fetchone()[0]
        
        total_cost = CLAN_CONFIG['create_price']
        if photo_file_id:
            total_cost += CLAN_CONFIG['avatar_price']
        
        if balance < total_cost:
            return False, f'❌ Не хватает монет\n💵 Требуется: {format_balance(total_cost)}'
        
        cursor.execute('SELECT id FROM clans WHERE tag = ?', (tag,))
        if cursor.fetchone():
            return False, f'❌ Тег [{tag}] уже занят'
        
        if len(name) > CLAN_CONFIG['max_name_length']:
            return False, f'❌ Слишком длинное название\n📏 Максимум: {CLAN_CONFIG["max_name_length"]} символов'
        
        if len(tag) < 2:
            return False, '❌ Тег слишком короткий\n📏 Минимум: 2 символа'
        
        if len(tag) > CLAN_CONFIG['max_tag_length']:
            return False, f'❌ Тег слишком длинный\n📏 Максимум: {CLAN_CONFIG["max_tag_length"]} символов'
        
        cursor.execute('''
            INSERT INTO clans (name, tag, description, owner_id, max_members, balance, level, experience)
            VALUES (?, ?, ?, ?, ?, 0, 1, 0)
        ''', (name, tag, description, user_id, CLAN_CONFIG['max_members']))
        
        clan_id = cursor.lastrowid
        
        if photo_file_id:
            try:
                save_clan_avatar(clan_id, photo_file_id)
            except:
                pass
        
        cursor.execute('INSERT INTO clan_members (user_id, clan_id, role) VALUES (?, ?, ?)', 
                      (user_id, clan_id, 'leader'))
        
        cursor.execute('UPDATE users SET clan_id = ?, balance = balance - ? WHERE user_id = ?',
                      (clan_id, total_cost, user_id))
        
        cursor.connection.commit()
        
        result_msg = f'🏆 Клан создан\n🏰 {name} [{tag}]\n💵 Потрачено: {format_balance(total_cost)}'
        if photo_file_id:
            result_msg += f'\n🖼️ Аватарка установлена'
        
        return True, result_msg

def get_clan_info(clan_id):
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT c.*, u.username as owner_name
            FROM clans c
            LEFT JOIN users u ON c.owner_id = u.user_id
            WHERE c.id = ?
        ''', (clan_id,))
        
        result = cursor.fetchone()
        if result:
            columns = [column[0] for column in cursor.description]
            info = dict(zip(columns, result))
            
            cursor.execute('SELECT COUNT(*) FROM clan_members WHERE clan_id = ?', (clan_id,))
            info['members_count'] = cursor.fetchone()[0]
            
            info['has_avatar'] = get_clan_avatar_path(clan_id) is not None
            
            return info
        return None

def get_user_clan(user_id):
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT c.*, cm.role, cm.contributed
            FROM clans c
            JOIN clan_members cm ON c.id = cm.clan_id
            WHERE cm.user_id = ?
        ''', (user_id,))
        
        result = cursor.fetchone()
        if result:
            columns = [column[0] for column in cursor.description]
            return dict(zip(columns, result))
        return None

def get_clan_members(clan_id, limit=None):
    with get_db_cursor() as cursor:
        query = '''
            SELECT u.user_id, u.username, u.first_name, u.custom_name, 
                   cm.role, cm.contributed, cm.joined_at
            FROM clan_members cm
            JOIN users u ON cm.user_id = u.user_id
            WHERE cm.clan_id = ?
            ORDER BY 
                CASE cm.role 
                    WHEN 'leader' THEN 1
                    WHEN 'officer' THEN 2
                    ELSE 3 
                END,
                cm.contributed DESC
        '''
        
        if limit:
            query += f' LIMIT {limit}'
            
        cursor.execute(query, (clan_id,))
        
        members = []
        columns = [column[0] for column in cursor.description]
        for row in cursor.fetchall():
            members.append(dict(zip(columns, row)))
        return members

def donate_to_clan(user_id, amount):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT clan_id FROM users WHERE user_id = ?', (user_id,))
        user_clan = cursor.fetchone()
        
        if not user_clan or not user_clan[0]:
            return False, '❌ Вы не состоите в клане'
        
        cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
        balance = cursor.fetchone()[0]
        
        if balance < amount:
            return False, f'❌ Не хватает монет\n💵 Твой баланс: {format_balance(balance)}'
        
        if amount < 1000:
            return False, '❌ Минимальная сумма: 1,000'
        
        clan_id = user_clan[0]
        
        cursor.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, user_id))
        cursor.execute('UPDATE clans SET balance = balance + ? WHERE id = ?', (amount, clan_id))
        cursor.execute('UPDATE clan_members SET contributed = contributed + ? WHERE user_id = ?', (amount, user_id))
        
        exp_gained = amount // 10000
        if exp_gained > 0:
            cursor.execute('UPDATE clans SET experience = experience + ? WHERE id = ?', (exp_gained, clan_id))
        
        cursor.execute('''
            INSERT INTO clan_donations (clan_id, user_id, amount, datetime)
            VALUES (?, ?, ?, ?)
        ''', (clan_id, user_id, amount, int(time.time())))
        
        cursor.connection.commit()
        
        return True, f'🏆 Внесено: {format_balance(amount)}\n⭐ Получено опыта: {exp_gained}'

# ===== СИСТЕМА ВОЙН =====

def declare_war(attacker_clan_id, defender_clan_id, declared_by):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT role FROM clan_members WHERE user_id = ? AND clan_id = ?', 
                      (declared_by, attacker_clan_id))
        role = cursor.fetchone()
        
        if not role or role[0] != 'leader':
            return False, '❌ Только глава может объявлять войну'
        
        cursor.execute('SELECT id, name, tag, level FROM clans WHERE id IN (?, ?)', 
                      (attacker_clan_id, defender_clan_id))
        clans = cursor.fetchall()
        
        if len(clans) != 2:
            return False, '❌ Один из кланов отсутствует'
        
        attacker_clan = next(c for c in clans if c[0] == attacker_clan_id)
        defender_clan = next(c for c in clans if c[0] == defender_clan_id)
        
        if attacker_clan[3] < CLAN_CONFIG['war_min_level']:
            return False, f'❌ Твой клан должен быть {CLAN_CONFIG["war_min_level"]}+ уровня'
        
        if defender_clan[3] < CLAN_CONFIG['war_min_level']:
            return False, f'❌ Целевой клан должен быть {CLAN_CONFIG["war_min_level"]}+ уровня'
        
        if attacker_clan_id == defender_clan_id:
            return False, '❌ Нельзя объявить войну своему клану'
        
        cursor.execute('SELECT balance FROM clans WHERE id = ?', (attacker_clan_id,))
        balance = cursor.fetchone()[0]
        
        if balance < CLAN_CONFIG['war_cost']:
            return False, f'❌ Не хватает монет в казне\n💵 Требуется: {format_balance(CLAN_CONFIG["war_cost"])}'
        
        cursor.execute('''
            SELECT MAX(end_time) FROM clan_active_wars 
            WHERE (attacker_clan_id = ? OR defender_clan_id = ?)
            AND status = 'active'
        ''', (attacker_clan_id, attacker_clan_id))
        
        last_war = cursor.fetchone()[0]
        if last_war and time.time() < last_war + CLAN_CONFIG['war_cooldown']:
            cooldown_left = last_war + CLAN_CONFIG['war_cooldown'] - time.time()
            hours = int(cooldown_left // 3600)
            return False, f'⏳ Кулдаун: {hours}ч'
        
        cursor.execute('SELECT COUNT(*) FROM clan_active_wars WHERE attacker_clan_id = ? AND status = "active"', 
                      (attacker_clan_id,))
        active_wars = cursor.fetchone()[0]
        
        if active_wars >= CLAN_CONFIG['war_max_active']:
            return False, f'❌ Максимум {CLAN_CONFIG["war_max_active"]} активных войн'
        
        cursor.execute('''
            SELECT id FROM clan_active_wars 
            WHERE ((attacker_clan_id = ? AND defender_clan_id = ?) 
                   OR (attacker_clan_id = ? AND defender_clan_id = ?))
            AND status = 'active'
        ''', (attacker_clan_id, defender_clan_id, defender_clan_id, attacker_clan_id))
        
        if cursor.fetchone():
            return False, '❌ Война уже идет'
        
        cursor.execute('UPDATE clans SET balance = balance - ? WHERE id = ?', 
                      (CLAN_CONFIG['war_cost'], attacker_clan_id))
        
        start_time = int(time.time())
        end_time = start_time + CLAN_CONFIG['war_duration']
        
        cursor.execute('''
            INSERT INTO clan_active_wars 
            (attacker_clan_id, defender_clan_id, start_time, end_time, status, attacker_score, defender_score)
            VALUES (?, ?, ?, ?, 'active', 0, 0)
        ''', (attacker_clan_id, defender_clan_id, start_time, end_time))
        
        war_id = cursor.lastrowid
        
        cursor.connection.commit()
        
        attacker_clan_info = get_clan_info(attacker_clan_id)
        defender_clan_info = get_clan_info(defender_clan_id)
        
        notify_clan_war_started(attacker_clan_id, defender_clan_id, war_id)
        
        return True, {
            'war_id': war_id,
            'attacker': attacker_clan[1],
            'defender': defender_clan[1],
            'end_time': end_time,
            'cost': CLAN_CONFIG['war_cost']
        }

def notify_clan_war_started(attacker_id, defender_id, war_id):
    attacker_clan = get_clan_info(attacker_id)
    defender_clan = get_clan_info(defender_id)
    
    attacker_members = get_clan_members(attacker_id)
    for member in attacker_members:
        try:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton('⚔️ К войне', callback_data=f'war_info_{war_id}'))
            
            bot.send_message(
                member['user_id'],
                f'⚔️ ВОЙНА ОБЪЯВЛЕНА\n━━━━━━━━━━━━━━━\n'
                f'⚔️ Цель: {defender_clan["name"]} [{defender_clan["tag"]}]\n'
                f'⏰ Длительность: 24 часа\n'
                f'💵 Ставка: {format_balance(CLAN_CONFIG["war_cost"])}\n\n'
                f'🏆 Награда: {format_balance(CLAN_CONFIG["war_victory_reward"])}',
                reply_markup=markup
            , parse_mode='HTML')
        except:
            pass
    
    defender_members = get_clan_members(defender_id)
    for member in defender_members:
        try:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton('🛡️ К обороне', callback_data=f'war_info_{war_id}'))
            
            bot.send_message(
                member['user_id'],
                f'⚠️ ВОЙНА ОБЪЯВЛЕНА\n━━━━━━━━━━━━━━━\n'
                f'⚔️ Атакующий: {attacker_clan["name"]} [{attacker_clan["tag"]}]\n'
                f'⏰ Длительность: 24 часа\n'
                f'🛡️ Защищайтесь!\n\n'
                f'🏆 Награда: {format_balance(CLAN_CONFIG["war_victory_reward"])}',
                reply_markup=markup
            , parse_mode='HTML')
        except:
            pass

def add_war_score(user_id, war_id, points):
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT attacker_clan_id, defender_clan_id, end_time, status 
            FROM clan_active_wars 
            WHERE id = ?
        ''', (war_id,))
        
        war = cursor.fetchone()
        if not war:
            return False, '❌ Война отсутствуета'
        
        attacker_id, defender_id, end_time, status = war
        
        if status != 'active':
            return False, '❌ Война уже окончена'
        
        if time.time() > end_time:
            return end_war(war_id)
        
        cursor.execute('SELECT clan_id FROM users WHERE user_id = ?', (user_id,))
        user_clan = cursor.fetchone()
        
        if not user_clan or not user_clan[0]:
            return False, '❌ Вы не состоите в клане'
        
        clan_id = user_clan[0]
        
        if clan_id not in [attacker_id, defender_id]:
            return False, '❌ Твой клан не участвует в этой войне'
        
        if clan_id == attacker_id:
            cursor.execute('UPDATE clan_active_wars SET attacker_score = attacker_score + ? WHERE id = ?', 
                          (points, war_id))
        else:
            cursor.execute('UPDATE clan_active_wars SET defender_score = defender_score + ? WHERE id = ?', 
                          (points, war_id))
        
        cursor.execute('''
            INSERT OR REPLACE INTO clan_war_participants 
            (war_id, user_id, clan_id, points_contributed)
            VALUES (?, ?, ?, COALESCE(
                (SELECT points_contributed FROM clan_war_participants WHERE war_id = ? AND user_id = ?), 0
            ) + ?)
        ''', (war_id, user_id, clan_id, war_id, user_id, points))
        
        cursor.connection.commit()
        
        return True, f'🏆 +{points} очков'

def end_war(war_id):
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT attacker_clan_id, defender_clan_id, attacker_score, defender_score
            FROM clan_active_wars 
            WHERE id = ? AND status = 'active'
        ''', (war_id,))
        
        war = cursor.fetchone()
        if not war:
            return False, '❌ Война отсутствуета'
        
        attacker_id, defender_id, attacker_score, defender_score = war
        
        if attacker_score > defender_score:
            winner_id = attacker_id
            loser_id = defender_id
            result = 'attacker_win'
            winner_text = '⚔️ АТАКУЮЩИЙ ПОБЕДИЛ'
        elif defender_score > attacker_score:
            winner_id = defender_id
            loser_id = attacker_id
            result = 'defender_win'
            winner_text = '🛡️ ЗАЩИТА ПОБЕДИЛА'
        else:
            winner_id = None
            result = 'draw'
            winner_text = '🤝 НИЧЬЯ'
        
        cursor.execute('UPDATE clan_active_wars SET status = ?, result = ?, end_time = ? WHERE id = ?',
                      ('finished', result, int(time.time()), war_id))
        
        if winner_id:
            cursor.execute('UPDATE clans SET balance = balance + ?, wars_won = wars_won + 1 WHERE id = ?',
                          (CLAN_CONFIG['war_victory_reward'], winner_id))
            
            cursor.execute('UPDATE clans SET balance = balance - ?, wars_lost = wars_lost + 1 WHERE id = ?',
                          (CLAN_CONFIG['war_defeat_penalty'], loser_id))
        else:
            cursor.execute('UPDATE clans SET balance = balance + ? WHERE id = ?',
                          (CLAN_CONFIG['war_cost'], attacker_id))
            cursor.execute('UPDATE clans SET wars_draw = wars_draw + 1 WHERE id IN (?, ?)',
                          (attacker_id, defender_id))
        
        cursor.connection.commit()
        
        notify_war_result(war_id, attacker_id, defender_id, attacker_score, defender_score, winner_text)
        
        return True, {
            'result': result,
            'winner_text': winner_text
        }

def notify_war_result(war_id, attacker_id, defender_id, attacker_score, defender_score, winner_text):
    attacker_clan = get_clan_info(attacker_id)
    defender_clan = get_clan_info(defender_id)
    
    result_message = f'🎖️ ВОЙНА ЗАВЕРШЕНА\n━━━━━━━━━━━━━━━\n'
    result_message += f'⚔️ {attacker_clan["name"]}: {attacker_score} очков\n'
    result_message += f'🛡️ {defender_clan["name"]}: {defender_score} очков\n\n'
    result_message += f'🏆 {winner_text}\n\n'
    
    if winner_text == '🤝 НИЧЬЯ':
        result_message += f'💵 Ставки возвращены'
    elif 'АТАКУЮЩИЙ' in winner_text:
        result_message += f'⚔️ Победитель: +{format_balance(CLAN_CONFIG["war_victory_reward"])}\n'
        result_message += f'💥 Проигравший: -{format_balance(CLAN_CONFIG["war_defeat_penalty"])}'
    else:
        result_message += f'⚔️ Победитель: +{format_balance(CLAN_CONFIG["war_victory_reward"])}\n'
        result_message += f'💥 Проигравший: -{format_balance(CLAN_CONFIG["war_defeat_penalty"])}'
    
    for clan_id in [attacker_id, defender_id]:
        members = get_clan_members(clan_id)
        for member in members:
            try:
                bot.send_message(member['user_id'], result_message)
            except:
                pass

def get_active_wars(clan_id=None):
    with get_db_cursor() as cursor:
        if clan_id:
            cursor.execute('''
                SELECT w.*, 
                       a.name as attacker_name, a.tag as attacker_tag,
                       d.name as defender_name, d.tag as defender_tag
                FROM clan_active_wars w
                JOIN clans a ON w.attacker_clan_id = a.id
                JOIN clans d ON w.defender_clan_id = d.id
                WHERE (w.attacker_clan_id = ? OR w.defender_clan_id = ?)
                AND w.status = 'active'
                ORDER BY w.end_time ASC
            ''', (clan_id, clan_id))
        else:
            cursor.execute('''
                SELECT w.*, 
                       a.name as attacker_name, a.tag as attacker_tag,
                       d.name as defender_name, d.tag as defender_tag
                FROM clan_active_wars w
                JOIN clans a ON w.attacker_clan_id = a.id
                JOIN clans d ON w.defender_clan_id = d.id
                WHERE w.status = 'active'
                ORDER BY w.end_time ASC
            ''')
        
        wars = []
        columns = [column[0] for column in cursor.description]
        for row in cursor.fetchall():
            wars.append(dict(zip(columns, row)))
        return wars

def get_war_info(war_id):
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT w.*, 
                   a.name as attacker_name, a.tag as attacker_tag, a.level as attacker_level,
                   d.name as defender_name, d.tag as defender_tag, d.level as defender_level
            FROM clan_active_wars w
            JOIN clans a ON w.attacker_clan_id = a.id
            JOIN clans d ON w.defender_clan_id = d.id
            WHERE w.id = ?
        ''', (war_id,))
        
        result = cursor.fetchone()
        if result:
            columns = [column[0] for column in cursor.description]
            return dict(zip(columns, result))
        return None

def get_war_participants(war_id, clan_id=None, limit=10):
    with get_db_cursor() as cursor:
        query = '''
            SELECT p.*, u.username, u.first_name, u.custom_name
            FROM clan_war_participants p
            JOIN users u ON p.user_id = u.user_id
            WHERE p.war_id = ?
        '''
        
        params = [war_id]
        if clan_id:
            query += ' AND p.clan_id = ?'
            params.append(clan_id)
        
        query += ' ORDER BY p.points_contributed DESC LIMIT ?'
        params.append(limit)
        
        cursor.execute(query, params)
        
        participants = []
        columns = [column[0] for column in cursor.description]
        for row in cursor.fetchall():
            participants.append(dict(zip(columns, row)))
        return participants

def get_clan_war_history(clan_id, limit=10):
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT w.*, 
                   a.name as attacker_name, a.tag as attacker_tag,
                   d.name as defender_name, d.tag as defender_tag
            FROM clan_active_wars w
            JOIN clans a ON w.attacker_clan_id = a.id
            JOIN clans d ON w.defender_clan_id = d.id
            WHERE (w.attacker_clan_id = ? OR w.defender_clan_id = ?)
            AND w.status = 'finished'
            ORDER BY w.end_time DESC
            LIMIT ?
        ''', (clan_id, clan_id, limit))
        
        wars = []
        columns = [column[0] for column in cursor.description]
        for row in cursor.fetchall():
            wars.append(dict(zip(columns, row)))
        return wars
# ===================== ВСТУПЛЕНИЕ В КЛАН (ДОПОЛНЕНИЕ) =====================

# Поиск кланов по названию или тегу
@bot.message_handler(func=lambda message: message.text.lower().startswith('поиск клан'))
def handle_clan_search(message):
    user_id = message.from_user.id
    
    # Проверяем, не состоит ли уже в клане
    user_clan = get_user_clan(user_id)
    if user_clan:
        bot.send_message(message.chat.id, 
                       f"❌ Вы уже в клане {user_clan['name']}!\n"
                       f"Используйте 'Выйти из клана' чтобы вступить в новый.", parse_mode='HTML')
        return
    
    try:
        search_query = message.text[10:].strip()
        
        if len(search_query) < 2:
            bot.send_message(message.chat.id, "❌ Слишком короткий запрос. Минимум 2 символа.", parse_mode='HTML')
            return
        
        with get_db_cursor() as cursor:
            # Ищем по названию или тегу
            cursor.execute('''
                SELECT id, name, tag, level, members_count, max_members, description, balance
                FROM clans
                WHERE LOWER(name) LIKE LOWER(?) OR LOWER(tag) LIKE LOWER(?)
                ORDER BY level DESC, members_count DESC
                LIMIT 10
            ''', (f'%{search_query}%', f'%{search_query}%'))
            
            clans = cursor.fetchall()
            
            if not clans:
                bot.send_message(message.chat.id, f"❌ Кланы по запросу '{search_query}' не найдены", parse_mode='HTML')
                return
            
            for clan in clans:
                clan_id, name, tag, level, members, max_members, description, balance = clan
                
                # Определяем статус
                if members >= max_members:
                    status = "🔴 ПОЛОН"
                    action_text = "Клан полон"
                else:
                    status = "🟢 ОТКРЫТ"
                    action_text = "Вступить"
                
                text = f"🏰 <b>{name}</b> [{tag}]\n"
                text += f"⭐ Уровень: {level} | 👥 {members}/{max_members}\n"
                text += f"💵 Казна: {format_balance(balance)}\n"
                text += f"📝 {description if description else 'Нет описания'}\n"
                text += f"Статус: {status}"
                
                markup = InlineKeyboardMarkup()
                if members < max_members:
                    markup.add(InlineKeyboardButton(
                        f"📥 {action_text}", 
                        callback_data=f"join_clan_{clan_id}"
                    ))
                markup.add(InlineKeyboardButton(
                    "ℹ️ Инфо", 
                    callback_data=f"clan_info_{clan_id}"
                ))
                
                bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML')
                
    except Exception as e:
        print(f"Ошибка в поиске кланов: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при поиске кланов", parse_mode='HTML')

# Информация о клане для не участников
@bot.callback_query_handler(func=lambda call: call.data.startswith('clan_info_'))
def handle_clan_info_external(call):
    try:
        clan_id = int(call.data.split('_')[2])
        
        with get_db_cursor() as cursor:
            cursor.execute('''
                SELECT name, tag, level, members_count, max_members, description, balance,
                       (SELECT username FROM users WHERE user_id = clans.owner_id) as owner_name,
                       (SELECT first_name FROM users WHERE user_id = clans.owner_id) as owner_first,
                       (SELECT custom_name FROM users WHERE user_id = clans.owner_id) as owner_custom
                FROM clans WHERE id = ?
            ''', (clan_id,))
            
            clan = cursor.fetchone()
            if not clan:
                bot.answer_callback_query(call.id, "❌ Клан не найден!")
                return
            
            name, tag, level, members, max_members, description, balance, owner_name, owner_first, owner_custom = clan
            
            owner_display = owner_custom or (f"@{owner_name}" if owner_name else owner_first)
            
            text = f"🏰 <b>{name}</b> [{tag}]\n\n"
            text += f"👑 Глава: {owner_display}\n"
            text += f"⭐ Уровень: {level}\n"
            text += f"💵 Казна: {format_balance(balance)}\n"
            text += f"👥 Участников: {members}/{max_members}\n\n"
            
            if description:
                text += f"📝 {description}"
            
            markup = InlineKeyboardMarkup()
            if members < max_members:
                markup.add(InlineKeyboardButton("📥 Вступить", callback_data=f"join_clan_{clan_id}"))
            markup.add(InlineKeyboardButton("◀️ Назад", callback_data="back_to_clans_menu"))
            
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, 
                                 reply_markup=markup, parse_mode='HTML')
            
    except Exception as e:
        print(f"Ошибка в clan_info: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка!")

# Улучшенный обработчик вступления (используйте существующий join_clan_)
# Найдите в коде функцию handle_clan_callbacks и дополните секцию join_clan_:

"""
В функции handle_clan_callbacks найдите секцию:
elif action.startswith('join_clan_'):

Замените её на этот улучшенный код:
"""

# Замените существующий обработчик join_clan_ на этот:
        


# Добавьте команду для поиска
@bot.message_handler(commands=['поискклана'])
def search_clan_command(message):
    msg = bot.send_message(message.chat.id, "🔍 Введите название или тег клана для поиска:")
    bot.register_next_step_handler(msg, process_clan_search_command)

def process_clan_search_command(message):
    # Создаем временное сообщение для обработки
    message.text = f"поиск клан {message.text}"
    handle_clan_search(message)
# ===== МЕНЮ И КНОПКИ =====

def create_clans_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton('⚔️ Основать гильдию', callback_data='create_clan'),
        types.InlineKeyboardButton('🔍 Поиск гильдий', callback_data='search_clans'),
        types.InlineKeyboardButton('🏆 Топ гильдий', callback_data='top_clans'),
        types.InlineKeyboardButton('⚔️ Войны', callback_data='active_wars')
    )
    return markup

def create_clan_management_menu(clan_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    markup.add(
        types.InlineKeyboardButton('👥 Бойцы', callback_data=f'clan_members_{clan_id}'),
        types.InlineKeyboardButton('💵 Казна', callback_data=f'clan_treasury_{clan_id}'),
        types.InlineKeyboardButton('📊 Статистика', callback_data=f'clan_stats_{clan_id}'),
        types.InlineKeyboardButton('⚔️ Войны', callback_data=f'clan_wars_{clan_id}')
    )
    
    clan_info = get_clan_info(clan_id)
    if clan_info and clan_info.get('has_avatar'):
        markup.add(
            types.InlineKeyboardButton('🖼️ Сменить фото', callback_data=f'clan_avatar_set_{clan_id}'),
            types.InlineKeyboardButton('🗑️ Удалить фото', callback_data=f'clan_avatar_remove_{clan_id}')
        )
    else:
        markup.add(types.InlineKeyboardButton('🖼️ Установить фото', callback_data=f'clan_avatar_set_{clan_id}'))
    
    user_clan = get_user_clan(clan_id)
    if user_clan and user_clan['role'] in ['leader', 'officer']:
        markup.add(types.InlineKeyboardButton('📨 Заявки', callback_data=f'clan_applications_{clan_id}'))
    
    markup.add(
        types.InlineKeyboardButton('🎖️ Лучшие', callback_data=f'top_members_{clan_id}'),
        types.InlineKeyboardButton('❌ Выйти', callback_data='leave_clan_confirm')
    )
    
    return markup

def create_clan_wars_menu(clan_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    active_wars = get_active_wars(clan_id)
    
    if active_wars:
        markup.add(types.InlineKeyboardButton('⚔️ Активные войны', callback_data=f'view_active_wars_{clan_id}'))
    
    markup.add(
        types.InlineKeyboardButton('⚔️ Объявить войну', callback_data=f'declare_war_{clan_id}'),
        types.InlineKeyboardButton('📊 История войн', callback_data=f'war_history_{clan_id}'),
        types.InlineKeyboardButton('🏆 Лучшие воины', callback_data=f'war_leaders_{clan_id}')
    )
    
    markup.add(types.InlineKeyboardButton('◀️ Назад', callback_data=f'clan_main_{clan_id}'))
    
    return markup

def create_clan_stats_menu(clan_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    markup.add(
        types.InlineKeyboardButton('📈 Общая статистика', callback_data=f'stats_general_{clan_id}'),
        types.InlineKeyboardButton('💵 Финансы', callback_data=f'stats_finance_{clan_id}'),
        types.InlineKeyboardButton('⚔️ Боевая статистика', callback_data=f'stats_war_{clan_id}'),
        types.InlineKeyboardButton('👥 Активность', callback_data=f'stats_activity_{clan_id}')
    )
    
    markup.add(types.InlineKeyboardButton('◀️ Назад', callback_data=f'clan_main_{clan_id}'))
    
    return markup

def create_top_members_menu(clan_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    markup.add(
        types.InlineKeyboardButton('💵 По вкладам', callback_data=f'top_contrib_{clan_id}'),
        types.InlineKeyboardButton('⚔️ По войнам', callback_data=f'top_war_{clan_id}'),
        types.InlineKeyboardButton('📈 По активности', callback_data=f'top_active_{clan_id}'),
        types.InlineKeyboardButton('🏆 Общий топ', callback_data=f'top_overall_{clan_id}')
    )
    
    markup.add(types.InlineKeyboardButton('◀️ Назад', callback_data=f'clan_main_{clan_id}'))
    
    return markup

# ===== ОБРАБОТЧИКИ =====

@bot.message_handler(func=lambda message: message.text in ['🏰 Кланы', '⚔️ Гильдия'])
def handle_clans_button(message):
    bot.send_message(
        message.chat.id,
        "⚔️ <b>Гильдия</b>\n\n🔒 Выйдет в следующем обновлении",
        parse_mode='HTML'
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('clan_') or call.data in ['create_clan', 'search_clans', 'top_clans', 'active_wars', 'leave_clan_confirm'])
def handle_clan_callbacks(call):
    user_id = call.from_user.id
    action = call.data
    
    try:
        # Создание клана
        if action == 'create_clan':
            user_clan = get_user_clan(user_id)
            if user_clan:
                bot.answer_callback_query(call.id, '⚠️ Ты уже в гильдии!', show_alert=True)
                return
            
            balance = get_balance(user_id)
            price = CLAN_CONFIG['create_price']
            
            if balance < price:
                bot.answer_callback_query(call.id, f'❌ Нужно {format_balance(price)}', show_alert=True)
                return
            
            msg = bot.send_message(
                call.message.chat.id,
                f'🏰 СОЗДАНИЕ ГИЛЬДИИ\n━━━━━━━━━━━━━━━\n\n'
                f'💵 Стоимость: {format_balance(price)}\n'
                f'💼 Твой баланс: {format_balance(balance)}\n\n'
                f'Введи название гильдии (макс. 20 символов):'
            , parse_mode='HTML')
            bot.register_next_step_handler(msg, process_clan_name_step)
            return
        
        # Поиск кланов
        elif action == 'search_clans':
            msg = bot.send_message(
                call.message.chat.id,
                '🔍 ПОИСК ГИЛЬДИЙ\n━━━━━━━━━━━━━━━\n\n'
                'Введи тег или название гильдии для поиска:'
            )
            bot.register_next_step_handler(msg, process_clan_search)
            return
        
        # Топ кланов
        elif action == 'top_clans':
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT name, tag, level, experience, members_count, balance
                    FROM clans
                    ORDER BY experience DESC
                    LIMIT 10
                ''')
                clans = cursor.fetchall()
            
            if not clans:
                text = '🏆 ТОП ГИЛЬДИЙ\n━━━━━━━━━━━━━━━\n\nПока нет гильдий'
            else:
                text = '🏆 ТОП ГИЛЬДИЙ\n━━━━━━━━━━━━━━━\n\n'
                medals = ['🥇', '🥈', '🥉']
                for i, clan in enumerate(clans):
                    medal = medals[i] if i < 3 else f'{i+1}.'
                    text += f'{medal} {clan[0]} `[{clan[1]}]`\n'
                    text += f'   ⭐ Ур.{clan[2]} | 👥 {clan[4]} чел.\n'
                    text += f'   💵 {format_balance(clan[5])}\n━━━━━━━━━━━━━━━\n'
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton('◀️ Назад', callback_data='back_to_clans'))
            
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, 
                                reply_markup=markup, parse_mode='Markdown')
        
        # Активные войны
        elif action == 'active_wars':
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT w.*, 
                           c1.name as attacker_name, c1.tag as attacker_tag,
                           c2.name as defender_name, c2.tag as defender_tag
                    FROM clan_wars w
                    JOIN clans c1 ON w.attacker_clan_id = c1.id
                    JOIN clans c2 ON w.defender_clan_id = c2.id
                    WHERE w.status = 'active'
                    ORDER BY w.start_time DESC
                    LIMIT 10
                ''')
                wars = cursor.fetchall()
            
            if not wars:
                text = '⚔️ АКТИВНЫЕ ВОЙНЫ\n━━━━━━━━━━━━━━━\n\nНет активных войн'
            else:
                text = '⚔️ АКТИВНЫЕ ВОЙНЫ\n━━━━━━━━━━━━━━━\n\n'
                for war in wars:
                    time_left = war[4] - time.time()
                    hours = int(time_left // 3600)
                    minutes = int((time_left % 3600) // 60)
                    
                    text += f'⚔️ {war[6]} vs {war[8]}\n'
                    text += f'📊 {war[5]} : {war[6]}\n'
                    text += f'⏰ {hours}ч {minutes}м\n━━━━━━━━━━━━━━━\n'
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton('◀️ Назад', callback_data='back_to_clans'))
            
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        
        # Выход из клана
        elif action == 'leave_clan_confirm':
            user_clan = get_user_clan(user_id)
            if not user_clan:
                bot.answer_callback_query(call.id, '⚠️ Ты не в гильдии')
                return
            
            if user_clan['role'] == 'leader':
                bot.answer_callback_query(call.id, '⚠️ Глава не может выйти. Передай лидерство или распусти гильдию', show_alert=True)
                return
            
            leave_clan_keyboard = {
                "inline_keyboard": [[
                    {"text": "✅ Да, выйти", "callback_data": "leave_clan_yes", "style": "danger"},
                    {"text": "❌ Отмена", "callback_data": "back_to_clans", "style": "secondary"}
                ]]
            }
            
            bot.edit_message_text(
                f'⚠️ <b>ВЫХОД ИЗ ГИЛЬДИИ</b>\n\n'
                f'<blockquote>Точно хочешь покинуть <b>{user_clan["name"]}</b>?</blockquote>',
                call.message.chat.id,
                call.message.message_id,
                reply_markup=leave_clan_keyboard,
                parse_mode='HTML'
            )
        
        # Подтверждение выхода
        elif action == 'leave_clan_yes':
            user_clan = get_user_clan(user_id)
            if user_clan:
                with get_db_cursor() as cursor:
                    cursor.execute('DELETE FROM clan_members WHERE user_id = ?', (user_id,))
                    cursor.execute('UPDATE clans SET members_count = members_count - 1 WHERE id = ?', (user_clan['id'],))
                
                bot.edit_message_text(
                    f'✅ Ты вышел из {user_clan["name"]}',
                    call.message.chat.id,
                    call.message.message_id
                )
            else:
                bot.answer_callback_query(call.id, '⚠️ Ты не в гильдии')
        
        # Назад в меню кланов
        elif action == 'back_to_clans':
            user_clan = get_user_clan(user_id)
            
            if user_clan:
                clan_info = get_clan_info(user_clan['id'])
                message_text = f'🏰 {clan_info["name"]} [{clan_info["tag"]}]\n━━━━━━━━━━━━━━━\n'
                message_text += f'⭐ Уровень: {clan_info["level"]}\n'
                message_text += f'📈 Опыт: {format_balance(clan_info["experience"])}\n'
                message_text += f'💵 Казна: {format_balance(clan_info["balance"])}\n'
                message_text += f'👥 Участники: {clan_info["members_count"]}/{clan_info["max_members"]}'
                
                if clan_info['description']:
                    message_text += f'\n\n📝 {clan_info["description"]}'
                
                markup = create_clan_management_menu(user_clan['id'])
            else:
                message_text = '🏰 СИСТЕМА КЛАНОВ\n━━━━━━━━━━━━━━━\n'
                message_text += 'Объединяйтесь с друзьями для достижения общих целей\n\n'
                message_text += '⚔️ Преимущества:\n'
                message_text += '• Общая казна\n'
                message_text += '• Клановые войны\n'
                message_text += '• Фото клана\n'
                message_text += '• Еженедельные награды\n\n'
                message_text += f'💵 Создание: {format_balance(CLAN_CONFIG["create_price"])}\n'
                message_text += f'🖼️ Фото: {format_balance(CLAN_CONFIG["avatar_price"])}'
                
                markup = create_clans_menu()
            
            bot.edit_message_text(message_text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        
        # Участники клана
        elif action.startswith('clan_members_'):
            clan_id = int(action.split('_')[2])
            members = get_clan_members(clan_id)
            
            if not members:
                text = '👥 УЧАСТНИКИ\n━━━━━━━━━━━━━━━\n\nНет участников'
            else:
                text = '👥 УЧАСТНИКИ\n━━━━━━━━━━━━━━━\n\n'
                role_emoji = {'leader': '👑', 'officer': '⚔️', 'member': '🗡'}
                
                for member in members[:20]:
                    name = member[3] or (f'@{member[2]}' if member[2] else member[1])
                    role = role_emoji.get(member[4], '🗡')
                    text += f'{role} {name[:20]}\n'
                    text += f'   💵 Вклад: {format_balance(member[5])}\n━━━━━━━━━━━━━━━\n'
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton('◀️ Назад', callback_data=f'clan_main_{clan_id}'))
            
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        
        # Казна клана
        elif action.startswith('clan_treasury_'):
            clan_id = int(action.split('_')[2])
            clan_info = get_clan_info(clan_id)
            
            text = f'💵 КАЗНА ГИЛЬДИИ\n━━━━━━━━━━━━━━━\n\n'
            text += f'💵 Баланс: {format_balance(clan_info["balance"])}\n\n'
            text += f'Введи сумму для пожертвования:'
            
            msg = bot.send_message(call.message.chat.id, text)
            bot.register_next_step_handler(msg, lambda m: process_clan_donate(m, clan_id))
        
        # Главное меню клана
        elif action.startswith('clan_main_'):
            clan_id = int(action.split('_')[2])
            clan_info = get_clan_info(clan_id)
            
            message_text = f'🏰 {clan_info["name"]} [{clan_info["tag"]}]\n━━━━━━━━━━━━━━━\n'
            message_text += f'⭐ Уровень: {clan_info["level"]}\n'
            message_text += f'📈 Опыт: {format_balance(clan_info["experience"])}\n'
            message_text += f'💵 Казна: {format_balance(clan_info["balance"])}\n'
            message_text += f'👥 Участники: {clan_info["members_count"]}/{clan_info["max_members"]}'
            
            if clan_info['description']:
                message_text += f'\n\n📝 {clan_info["description"]}'
            
            markup = create_clan_management_menu(clan_id)
            bot.edit_message_text(message_text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        
        # Войны клана
        elif action.startswith('clan_wars_'):
            clan_id = int(action.split('_')[2])
            markup = create_clan_wars_menu(clan_id)
            
            active_wars = get_active_wars(clan_id)
            
            if active_wars:
                message_text = '⚔️ ВОЙНЫ КЛАНА\n━━━━━━━━━━━━━━━\n\n'
                message_text += '⚔️ Выберите действие:'
            else:
                message_text = '⚔️ ВОЙНЫ КЛАНА\n━━━━━━━━━━━━━━━\n\n'
                message_text += '⚔️ У вашего клана нет активных войн\n'
                message_text += 'Объявите войну другому клану!'
            
            bot.edit_message_text(
                message_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
        
        # Статистика клана
        elif action.startswith('clan_stats_'):
            clan_id = int(action.split('_')[2])
            markup = create_clan_stats_menu(clan_id)
            
            message_text = '📊 СТАТИСТИКА КЛАНА\n━━━━━━━━━━━━━━━\n\n'
            message_text += 'Выберите тип статистики:'
            
            bot.edit_message_text(
                message_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
        
        # Просмотр активных войн
        elif action.startswith('view_active_wars_'):
            clan_id = int(action.split('_')[3])
            active_wars = get_active_wars(clan_id)
            
            if not active_wars:
                message_text = '⚔️ АКТИВНЫЕ ВОЙНЫ\n━━━━━━━━━━━━━━━\n\n'
                message_text += 'Нет активных войн'
            else:
                message_text = '⚔️ АКТИВНЫЕ ВОЙНЫ\n━━━━━━━━━━━━━━━\n\n'
                
                for war in active_wars:
                    time_left = war['end_time'] - time.time()
                    hours = int(time_left // 3600)
                    minutes = int((time_left % 3600) // 60)
                    
                    if war['attacker_clan_id'] == clan_id:
                        side = '⚔️ АТАКУЮЩИЙ'
                        opponent = war['defender_name']
                    else:
                        side = '🛡️ ЗАЩИТА'
                        opponent = war['attacker_name']
                    
                    message_text += f'{side}\n'
                    message_text += f'⚔️ Против: {opponent}\n'
                    message_text += f'📊 {war["attacker_score"]} : {war["defender_score"]}\n'
                    message_text += f'⏰ {hours}ч {minutes}м\n━━━━━━━━━━━━━━━\n'
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton('◀️ Назад', callback_data=f'clan_wars_{clan_id}'))
            
            bot.edit_message_text(
                message_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
        
        # История войн
        elif action.startswith('war_history_'):
            clan_id = int(action.split('_')[2])
            war_history = get_clan_war_history(clan_id, 10)
            
            if not war_history:
                message_text = '📊 ИСТОРИЯ ВОЙН\n━━━━━━━━━━━━━━━\n\n'
                message_text += 'У вашего клана еще не было войн'
            else:
                message_text = '📊 ИСТОРИЯ ВОЙН\n━━━━━━━━━━━━━━━\n\n'
                
                for war in war_history[:5]:
                    if war['attacker_clan_id'] == clan_id:
                        side = '⚔️ АТАКУЮЩИЙ'
                        opponent = war['defender_name']
                    else:
                        side = '🛡️ ЗАЩИТА'
                        opponent = war['attacker_name']
                    
                    result = '🏆 ПОБЕДА' if (war['result'] == 'attacker_win' and war['attacker_clan_id'] == clan_id) or (war['result'] == 'defender_win' and war['defender_clan_id'] == clan_id) else '💥 ПОРАЖЕНИЕ' if war['result'] == 'draw' else '🤝 НИЧЬЯ'
                    
                    message_text += f'{side}\n'
                    message_text += f'⚔️ Против: {opponent}\n'
                    message_text += f'📊 {war["attacker_score"]} : {war["defender_score"]}\n'
                    message_text += f'{result}\n━━━━━━━━━━━━━━━\n'
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton('◀️ Назад', callback_data=f'clan_wars_{clan_id}'))
            
            bot.edit_message_text(
                message_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
        
        # Лучшие воины
        elif action.startswith('war_leaders_'):
            clan_id = int(action.split('_')[2])
            
            message_text = '🏆 ЛУЧШИЕ ВОИНЫ\n━━━━━━━━━━━━━━━\n\n'
            message_text += 'Топ боецов по очкам в войнах:\n\n'
            
            with get_db_cursor() as cursor:
                cursor.execute('''
                    SELECT p.user_id, SUM(p.points_contributed) as total_points,
                           u.username, u.first_name, u.custom_name
                    FROM clan_war_participants p
                    JOIN users u ON p.user_id = u.user_id
                    WHERE p.clan_id = ?
                    GROUP BY p.user_id
                    ORDER BY total_points DESC
                    LIMIT 10
                ''', (clan_id,))
                
                leaders = cursor.fetchall()
                
                if leaders:
                    for i, leader in enumerate(leaders, 1):
                        user_id, points, username, first_name, custom_name = leader
                        name = custom_name or (f'@{username}' if username else first_name)
                        
                        medal = '🥇' if i == 1 else '🥈' if i == 2 else '🥉' if i == 3 else f'{i}.'
                        message_text += f'{medal} {name[:15]}\n'
                        message_text += f'   ⚔️ Очков: {points}\n━━━━━━━━━━━━━━━\n'
                else:
                    message_text += 'Нет данных о воинах'
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton('◀️ Назад', callback_data=f'clan_wars_{clan_id}'))
            
            bot.edit_message_text(
                message_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
        
        # Объявление войны
        elif action.startswith('declare_war_'):
            clan_id = int(action.split('_')[2])
            user_clan = get_user_clan(user_id)
            
            if not user_clan or user_clan['id'] != clan_id or user_clan['role'] != 'leader':
                bot.answer_callback_query(call.id, '❌ Только глава может объявлять войну')
                return
            
            msg = bot.send_message(
                call.message.chat.id,
                f'⚔️ ОБЪЯВЛЕНИЕ ВОЙНЫ\n━━━━━━━━━━━━━━━\n\n'
                f'💵 Стоимость: {format_balance(CLAN_CONFIG["war_cost"])}\n'
                f'⏰ Длительность: 24 часа\n'
                f'🏆 Награда: {format_balance(CLAN_CONFIG["war_victory_reward"])}\n\n'
                f'Введите тег клана для объявления войны:'
            , parse_mode='HTML')
            
            bot.register_next_step_handler(msg, lambda m: process_declare_war(m, clan_id))
        
        # Вступление в клан
        elif action.startswith('join_clan_'):
            target_clan_id = int(action.split('_')[2])
            user_clan = get_user_clan(user_id)
            
            if user_clan:
                bot.answer_callback_query(call.id, '⚠️ Ты уже в гильдии!', show_alert=True)
                return
            
            clan_info = get_clan_info(target_clan_id)
            if not clan_info:
                bot.answer_callback_query(call.id, '❌ Гильдия не найдена', show_alert=True)
                return
            
            if clan_info['members_count'] >= clan_info['max_members']:
                bot.answer_callback_query(call.id, '⚠️ Гильдия переполнена', show_alert=True)
                return
            
            try:
                with get_db_cursor() as cursor:
                    cursor.execute('SELECT * FROM clan_applications WHERE user_id = ? AND clan_id = ?', 
                                  (user_id, target_clan_id))
                    if cursor.fetchone():
                        bot.answer_callback_query(call.id, '⚠️ Вы уже подали заявку!', show_alert=True)
                        return
            except:
                pass
            
            with get_db_cursor() as cursor:
                cursor.execute('''
                    INSERT INTO clan_members (user_id, clan_id, role, contributed)
                    VALUES (?, ?, 'member', 0)
                ''', (user_id, target_clan_id))
                
                cursor.execute('UPDATE clans SET members_count = members_count + 1 WHERE id = ?', (target_clan_id,))
            
            bot.answer_callback_query(call.id, f'✅ Ты вступил в {clan_info["name"]}!', show_alert=True)
            
            text = f"🎉 ДОБРО ПОЖАЛОВАТЬ!\n━━━━━━━━━━━━━━━\n\n"
            text += f"🏰 Гильдия: {clan_info['name']} [{clan_info['tag']}]\n"
            text += f"⭐ Уровень: {clan_info['level']}\n"
            text += f"👥 Участников: {clan_info['members_count']}/{clan_info['max_members']}\n"
            text += f"💵 Казна: {format_balance(clan_info['balance'])}\n\n"
            text += f"Теперь ты часть команды!"
            
            bot.send_message(call.message.chat.id, text, parse_mode='Markdown')
        
        # ✅ ЗАКРЫВАЕМ ВСЕ ОБРАБОТЧИКИ
        bot.answer_callback_query(call.id)
    
    except Exception as e:
        print(f'Ошибка в handle_clan_callbacks: {e}')
        bot.answer_callback_query(call.id, '❌ Ошибка')


# ===== ФУНКЦИИ ОБРАБОТКИ СОЗДАНИЯ КЛАНА =====

def process_clan_name_step(message):
    """Обработка названия клана"""
    user_id = message.from_user.id
    clan_name = message.text.strip()
    
    if len(clan_name) < 3 or len(clan_name) > 20:
        bot.send_message(message.chat.id, '❌ Название должно быть от 3 до 20 символов', parse_mode='HTML')
        return
    
    # Проверяем занятость
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM clans WHERE name = ?', (clan_name,))
        if cursor.fetchone():
            bot.send_message(message.chat.id, '❌ Это название уже занято', parse_mode='HTML')
            return
    
    msg = bot.send_message(
        message.chat.id,
        f'✅ Название: {clan_name}\n\n'
        f'Теперь введи тег гильдии (2-5 символов, заглавные буквы):\n'
        f'Пример: KING, FIRE, TOP'
    )
    bot.register_next_step_handler(msg, lambda m: process_clan_tag_step(m, clan_name))

def process_clan_tag_step(message, clan_name):
    """Обработка тега клана"""
    user_id = message.from_user.id
    clan_tag = message.text.strip().upper()
    
    if len(clan_tag) < 2 or len(clan_tag) > 5:
        bot.send_message(message.chat.id, '❌ Тег должен быть от 2 до 5 символов', parse_mode='HTML')
        return
    
    if not clan_tag.isalnum():
        bot.send_message(message.chat.id, '❌ Тег может содержать только буквы и цифры', parse_mode='HTML')
        return
    
    # Проверяем занятость
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM clans WHERE tag = ?', (clan_tag,))
        if cursor.fetchone():
            bot.send_message(message.chat.id, '❌ Этот тег уже занят', parse_mode='HTML')
            return
    
    msg = bot.send_message(
        message.chat.id,
        f'✅ Тег: [{clan_tag}]\n\n'
        f'Введи описание гильдии (до 100 символов) или отправь "-" чтобы пропустить:'
    )
    bot.register_next_step_handler(msg, lambda m: process_clan_description_step(m, clan_name, clan_tag))

def process_clan_description_step(message, clan_name, clan_tag):
    """Обработка описания клана"""
    user_id = message.from_user.id
    description = message.text.strip()
    
    if description == '-':
        description = ''
    elif len(description) > 100:
        bot.send_message(message.chat.id, '❌ Описание слишком длинное (макс. 100 символов)', parse_mode='HTML')
        return
    
    # Создаём клан
    price = CLAN_CONFIG['create_price']
    balance = get_balance(user_id)
    
    if balance < price:
        bot.send_message(message.chat.id, f'❌ Не хватает средств! Нужно: {format_balance(price)}', parse_mode='HTML')
        return
    
    # Списываем деньги
    update_balance(user_id, -price)
    
    # Создаём клан
    with get_db_cursor() as cursor:
        cursor.execute('''
            INSERT INTO clans (name, tag, description, owner_id, level, experience, balance, members_count, max_members)
            VALUES (?, ?, ?, ?, 1, 0, 0, 1, 20)
        ''', (clan_name, clan_tag, description, user_id))
        
        clan_id = cursor.lastrowid
        
        # Добавляем создателя как лидера
        cursor.execute('''
            INSERT INTO clan_members (user_id, clan_id, role, contributed)
            VALUES (?, ?, 'leader', 0)
        ''', (user_id, clan_id))
    
    text = f'🎉 ГИЛЬДИЯ СОЗДАНА!\n━━━━━━━━━━━━━━━\n\n'
    text += f'🏰 Название: {clan_name}\n'
    text += f'🏷 Тег: [{clan_tag}]\n'
    
    if description:
        text += f'📝 Описание: {description}\n'
    
    text += f'\n💵 Потрачено: {format_balance(price)}\n'
    text += f'💼 Остаток: {format_balance(get_balance(user_id))}\n\n'
    text += f'✅ Ты глава гильдии!'
    
    bot.send_message(message.chat.id, text)

def process_clan_search(message):
    """Обработка поиска клана"""
    search_query = message.text.strip()
    
    if len(search_query) < 2:
        bot.send_message(message.chat.id, '❌ Запрос слишком короткий', parse_mode='HTML')
        return
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, name, tag, level, members_count, max_members, balance, description
            FROM clans
            WHERE LOWER(name) LIKE ? OR UPPER(tag) LIKE ?
            ORDER BY level DESC, experience DESC
            LIMIT 10
        ''', (f'%{search_query.lower()}%', f'%{search_query.upper()}%'))
        
        clans = cursor.fetchall()
    
    if not clans:
        bot.send_message(message.chat.id, f'❌ Гильдии по запросу "{search_query}" не найдены', parse_mode='HTML')
        return
    
    text = f'🔍 РЕЗУЛЬТАТЫ ПОИСКА\n━━━━━━━━━━━━━━━\n\n'
    text += f'Найдено: {len(clans)}\n\n'
    
    for clan in clans:
        text += f'🏰 {clan[1]} `[{clan[2]}]`\n'
        text += f'   ⭐ Ур.{clan[3]} | 👥 {clan[4]}/{clan[5]}\n'
        text += f'   💵 {format_balance(clan[6])}\n'
        
        if clan[7]:
            text += f'   📝 {clan[7][:30]}...\n'
        
        # Кнопка вступления
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton('📥 Вступить', callback_data=f'join_clan_{clan[0]}'))
        
        text += '━━━━━━━━━━━━━━━\n'
    
    bot.send_message(message.chat.id, text, parse_mode='Markdown')

def process_clan_donate(message, clan_id):
    """Обработка пожертвования в казну"""
    user_id = message.from_user.id
    
    try:
        amount_text = message.text.strip()
        amount = parse_bet_amount(amount_text, get_balance(user_id))
        
        if not amount or amount < 1000:
            bot.send_message(message.chat.id, '❌ Минимальная сумма: 1K$', parse_mode='HTML')
            return
        
        balance = get_balance(user_id)
        if amount > balance:
            bot.send_message(message.chat.id, '❌ Не хватает средств', parse_mode='HTML')
            return
        
        # Списываем у игрока
        update_balance(user_id, -amount)
        
        # Добавляем в казну
        with get_db_cursor() as cursor:
            cursor.execute('UPDATE clans SET balance = balance + ? WHERE id = ?', (amount, clan_id))
            cursor.execute('UPDATE clan_members SET contributed = contributed + ? WHERE user_id = ? AND clan_id = ?',
                         (amount, user_id, clan_id))
        
        bot.send_message(
            message.chat.id,
            f'✅ ПОЖЕРТВОВАНИЕ\n━━━━━━━━━━━━━━━\n\n'
            f'💵 Сумма: {format_balance(amount)}\n'
            f'💼 Остаток: {format_balance(get_balance(user_id))}\n\n'
            f'Спасибо за вклад в гильдию! 🎉'
        , parse_mode='HTML')
        
    except:
        bot.send_message(message.chat.id, '❌ Неверная сумма', parse_mode='HTML')

def process_declare_war(message, attacker_clan_id):
    user_id = message.from_user.id
    target_tag = message.text.strip().upper()
    
    if len(target_tag) < 2:
        bot.send_message(message.chat.id, '❌ Тег должен быть минимум 2 символа', parse_mode='HTML')
        return
    
    with get_db_cursor() as cursor:
        cursor.execute('SELECT id, name FROM clans WHERE tag = ?', (target_tag,))
        target = cursor.fetchone()
        
        if not target:
            bot.send_message(message.chat.id, f'❌ Клан с тегом [{target_tag}] отсутствует', parse_mode='HTML')
            return
        
        target_id, target_name = target
        
        success, result = declare_war(attacker_clan_id, target_id, user_id)
        
        if success:
            bot.send_message(
                message.chat.id,
                f'⚔️ ВОЙНА ОБЪЯВЛЕНА\n━━━━━━━━━━━━━━━\n'
                f'⚔️ Цель: {target_name} [{target_tag}]\n'
                f'💵 Потрачено: {format_balance(CLAN_CONFIG["war_cost"])}\n'
                f'⏰ Длительность: 24 часа\n\n'
                f'🎖️ Собирайте очки для победы!'
            , parse_mode='HTML')
        else:
            bot.send_message(message.chat.id, result)

# ===== ДОПОЛНИТЕЛЬНЫЕ ФУНКЦИИ =====

def get_top_clans(limit=10):
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT c.*, 
                   (SELECT COUNT(*) FROM clan_members WHERE clan_id = c.id) as members_count,
                   (SELECT COALESCE(SUM(contributed), 0) FROM clan_members WHERE clan_id = c.id) as total_contrib
            FROM clans c
            ORDER BY c.level DESC, c.experience DESC, total_contrib DESC
            LIMIT ?
        ''', (limit,))
        
        clans = []
        columns = [column[0] for column in cursor.description]
        for row in cursor.fetchall():
            clans.append(dict(zip(columns, row)))
        return clans

def search_clans(query):
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT c.*, 
                   (SELECT COUNT(*) FROM clan_members WHERE clan_id = c.id) as members_count
            FROM clans c
            WHERE LOWER(c.name) LIKE LOWER(?) OR LOWER(c.tag) LIKE LOWER(?)
            ORDER BY c.level DESC
            LIMIT 15
        ''', (f'%{query}%', f'%{query}%'))
        
        clans = []
        columns = [column[0] for column in cursor.description]
        for row in cursor.fetchall():
            clans.append(dict(zip(columns, row)))
        return clans

# ===== КОМАНДЫ =====

@bot.message_handler(commands=['клан', 'clan'])
def clan_command(message):
    handle_clans_button(message)

@bot.message_handler(commands=['войны', 'wars'])
def wars_command(message):
    user_id = message.from_user.id
    user_clan = get_user_clan(user_id)
    
    if not user_clan:
        bot.send_message(message.chat.id, '❌ Вы не состоите в клане', parse_mode='HTML')
        return
    
    markup = create_clan_wars_menu(user_clan['id'])
    bot.send_message(message.chat.id, '⚔️ СИСТЕМА ВОЙН\n━━━━━━━━━━━━━━━\n\nВыберите действие:', reply_markup=markup, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith('клан донат'))
def clan_donate_command(message):
    user_id = message.from_user.id
    
    try:
        parts = message.text.split()
        if len(parts) < 3:
            bot.send_message(message.chat.id, '❌ Используйте: клан донат [сумма]', parse_mode='HTML')
            return
        
        amount_str = parts[2].lower()
        amount = parse_bet_amount(amount_str, get_balance(user_id))
        
        if not amount or amount < 1000:
            bot.send_message(message.chat.id, '❌ Минимальная сумма: 1,000', parse_mode='HTML')
            return
        
        success, result = donate_to_clan(user_id, amount)
        bot.send_message(message.chat.id, result)
        
    except Exception as e:
        print(f'Ошибка в донате: {e}')
        bot.send_message(message.chat.id, '❌ Ошибка', parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith('война '))
def war_declare_command(message):
    user_id = message.from_user.id
    user_clan = get_user_clan(user_id)
    
    if not user_clan:
        bot.send_message(message.chat.id, '❌ Вы не в клане', parse_mode='HTML')
        return
    
    if user_clan['role'] != 'leader':
        bot.send_message(message.chat.id, '❌ Только глава может объявлять войну', parse_mode='HTML')
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, '❌ Используйте: война [тег_клана]', parse_mode='HTML')
            return
        
        target_tag = parts[1].upper()
        
        with get_db_cursor() as cursor:
            cursor.execute('SELECT id, name FROM clans WHERE tag = ?', (target_tag,))
            target = cursor.fetchone()
            
            if not target:
                bot.send_message(message.chat.id, f'❌ Клан с тегом [{target_tag}] отсутствует', parse_mode='HTML')
                return
            
            target_id, target_name = target
            
            success, result = declare_war(user_clan['id'], target_id, user_id)
            
            if success:
                bot.send_message(
                    message.chat.id,
                    f'⚔️ ВОЙНА ОБЪЯВЛЕНА\n━━━━━━━━━━━━━━━\n'
                    f'⚔️ Цель: {target_name} [{target_tag}]\n'
                    f'💵 Потрачено: {format_balance(CLAN_CONFIG["war_cost"])}\n'
                    f'⏰ Длительность: 24 часа\n\n'
                    f'🎖️ Собирайте очки для победы!'
                , parse_mode='HTML')
            else:
                bot.send_message(message.chat.id, result)
    
    except Exception as e:
        print(f'Ошибка в объявлении войны: {e}')
        bot.send_message(message.chat.id, '❌ Ошибка', parse_mode='HTML')
# Главное меню (3 ряда)
def create_main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    
    buttons = [
        "🏙️ Город", "📞 Помощь", "🏛 Банк",
        "⚔️ Гильдия", "🏆 Топ", "💎 Донат",
        "🎁 Бонус"
    ]
    
    # Добавляем кнопки по 3 в ряд
    for i in range(0, len(buttons), 3):
        row = buttons[i:i+3]
        markup.add(*[KeyboardButton(btn) for btn in row])
    
    return markup
# Меню города
def create_city_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("🛍️ Шоп"),
        KeyboardButton("👔 Шкаф"),
        KeyboardButton("💼 Работа"),
        KeyboardButton("🔙 Назад")
    )
    return markup
# Клавиатура для работы
def create_work_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("👆 Кликер"),
        KeyboardButton("🎭 Скам"),
        KeyboardButton("🚗 Такси"),
        KeyboardButton("⛏️ Майнинг"),
        KeyboardButton("🔙 Назад")
    )
    return markup

# Клавиатура для бизнеса
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

# Клавиатура для майнинга
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
    
    # 4 неправильные кнопки (крестики) и 1 правильная (галочка)
    symbols = ["❌", "❌", "❌", "❌", "✅"]
    random.shuffle(symbols)
    
    # Создаем JSON клавиатуру
    keyboard = {
        "inline_keyboard": []
    }
    
    row = []
    for i, symbol in enumerate(symbols):
        # Определяем цвет кнопки
        if symbol == "✅":
            # Правильная кнопка - зеленая
            button = {
                "text": symbol,
                "callback_data": f"clicker_{symbol}",
                "style": "success"  # 🟢 Зеленая
            }
        else:
            # Неправильные кнопки - красные
            button = {
                "text": symbol,
                "callback_data": f"clicker_{symbol}",
                "style": "danger"  # 🔴 Красная
            }
        
        row.append(button)
        
        # По 3 кнопки в ряд
        if len(row) == 3:
            keyboard["inline_keyboard"].append(row)
            row = []
    
    # Добавляем оставшиеся кнопки (2 штуки)
    if row:
        keyboard["inline_keyboard"].append(row)
    
    return json.dumps(keyboard)



# Клавиатура для топа с навигацией
def create_top_menu(top_type="balance", page=0):
    markup = InlineKeyboardMarkup()
    
    # Кнопки переключения между типами топа
    type_buttons = [
        InlineKeyboardButton("💵 Баланс", callback_data=f"top_type_balance_{page}"),
        InlineKeyboardButton("🌟 Опыт", callback_data=f"top_type_exp_{page}"),
        InlineKeyboardButton("👥 Рефералы", callback_data=f"top_type_referrals_{page}")
    ]
    markup.add(*type_buttons)
    
    # Кнопки навигации по страницам
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

    # 1. Очистка игр в минах (старше 10м) с возвратом денег
    expired_mines = []
    for user_id, game_data in active_mines_games.items():
        if current_time - game_data.get('start_time', 0) > 600:  # 10м
            expired_mines.append(user_id)
    
    for user_id in expired_mines:
        try:
            game_data = active_mines_games[user_id]
            # Возвращаем ставку
            update_balance(user_id, game_data['bet_amount'])
            del active_mines_games[user_id]
            cleaned_count += 1
            
            # Уведомляем пользователя
            try:
                bot.send_message(
                    user_id, 
                    f"🕒 Игра в 'Мины' автоматически окончена\n💵 Возвращено: {format_balance(game_data['bet_amount'])}"
                , parse_mode='HTML')
            except:
                pass  # Пользователь заблокировал бота
                
            logger.info(f"🧹 Очищена игра в минах для {user_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка очистки мины для {user_id}: {e}")

    # 2. Очистка капч (старше 30м)
    expired_captchas = []
    for user_id, captcha_data in active_captchas.items():
        if current_time - captcha_data.get('created_at', 0) > 1800:  # 30м
            expired_captchas.append(user_id)
    
    for user_id in expired_captchas:
        try:
            del active_captchas[user_id]
            cleaned_count += 1
            
            # Уведомляем пользователя
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

    # 3. Очистка сессий магазина/гардероба (неактивные более 2ч)
    expired_sessions = []
    for user_id, data in list(shop_pages.items()) + list(wardrobe_pages.items()):
        last_activity = data.get('last_activity', 0)
        if current_time - last_activity > 7200:  # 2 часа
            expired_sessions.append(user_id)
    
    for user_id in set(expired_sessions):  # Уникальные пользователи
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
        # Пытаемся отправить фото city.jpg из корневой папки
        with open('city.jpg', 'rb') as photo:
            bot.send_photo(
                chat_id=message.chat.id,
                photo=photo,
                caption=city_text,
                reply_markup=markup,
                parse_mode='HTML'
            )
    except FileNotFoundError:
        # Если файл отсутствует, отправляем только текст
        bot.send_message(message.chat.id, city_text, reply_markup=markup, parse_mode='HTML')
        print("Файл city.jpg отсутствует в корневой папке!")
    except Exception as e:
        # Другие возможные ошибки
        bot.send_message(message.chat.id, city_text, reply_markup=markup, parse_mode='HTML')
        print(f"Ошибка при отправке фото: {e}")
# Функция для получения ссылки на пользователя
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
            # Получаем топ пользователей
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
            
            # Получаем общее количество пользователей с балансом > 0
            cursor.execute('SELECT COUNT(*) FROM users WHERE balance > 0')
            total_users = cursor.fetchone()[0]
            
            # Получаем позицию текущего пользователя
            cursor.execute('''
            SELECT COUNT(*) + 1 FROM users 
            WHERE balance > (SELECT balance FROM users WHERE user_id = ?)
            ''', (user_id,))
            user_position_result = cursor.fetchone()
            user_position = user_position_result[0] if user_position_result else None
            
            # Получаем баланс текущего пользователя
            cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
            user_balance_result = cursor.fetchone()
            user_balance = user_balance_result[0] if user_balance_result else 0

        title = "<b>Список Forbs💸</b>\n\n"
        
        if not top_users:
            message_text = f"{title}Топ пока пуст! Станьте первым мажором!"
        else:
            message_text = title
            # ИСПРАВЛЕНИЕ: правильные номера для каждой страницы
            start_number = page * limit + 1
            
            for i, (user_id_db, username, first_name, custom_name, balance) in enumerate(top_users):
                user_link = get_user_link(user_id_db, username, first_name, custom_name)
                prem_icon = f" {PREMIUM_EMOJI}" if is_premium(user_id_db) else ""
                message_text += f"{start_number + i}. {user_link}{prem_icon} ⟨{format_balance(balance)}⟩\n"
        
        # Добавляем позицию пользователя
        if user_balance > 0 and user_position:
            message_text += f"\nТы находишься на {user_position} месте"
        
        # Создаем клавиатуру с учетом наличия следующих страниц
        final_markup = create_top_menu("balance", page)
        
        if message_id:
            # Редактируем существующее сообщение
            bot.edit_message_text(
                message_text, 
                chat_id, 
                message_id,
                reply_markup=final_markup, 
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        else:
            # Отправляем новое сообщение (только при первом вызове)
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
            # ИСПРАВЛЕНИЕ: правильные номера для каждой страницы
            start_number = page * limit + 1
            
            for i, clan in enumerate(top_clans):
                message_text += f"{start_number + i}. 🔰 <b>{clan['name']}</b> [{clan['tag']}] ({format_balance(clan['balance'])})\n"
                message_text += f"   Уровень: {clan['level']} | Участники: {clan['actual_members']}\n"
        
        # Получаем позицию клана пользователя
        user_clan = get_user_clan(user_id)
        if user_clan:
            clan_position = None
            for i, clan in enumerate(all_clans):
                if clan['id'] == user_clan['id']:
                    clan_position = i + 1
                    break
            
            if clan_position:
                message_text += f"\nТвой клан на {clan_position} месте"
        
        # Создаем клавиатуру
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
            
            # Получаем общее количество
            cursor.execute('SELECT COUNT(*) FROM users WHERE (SELECT COUNT(*) FROM users WHERE referred_by = user_id) > 0')
            total_refs_result = cursor.fetchone()
            total_refs = total_refs_result[0] if total_refs_result else 0
            
            # Получаем позицию текущего пользователя
            cursor.execute('''
            SELECT COUNT(*) + 1 FROM users u1
            WHERE (SELECT COUNT(*) FROM users WHERE referred_by = u1.user_id) > 
                  (SELECT COUNT(*) FROM users WHERE referred_by = ?)
            ''', (user_id,))
            user_position_result = cursor.fetchone()
            user_position = user_position_result[0] if user_position_result else None
            
            # Получаем количество рефералов пользователя
            cursor.execute('SELECT COUNT(*) FROM users WHERE referred_by = ?', (user_id,))
            user_ref_count_result = cursor.fetchone()
            user_ref_count = user_ref_count_result[0] if user_ref_count_result else 0

        title = "<b>Топ рефералов</b>\n\n"
        
        if not top_refs:
            message_text = f"{title}Пока никто не пригласил рефералов!"
        else:
            message_text = title
            # ИСПРАВЛЕНИЕ: правильные номера для каждой страницы
            start_number = page * limit + 1
            
            for i, (user_id_db, username, first_name, custom_name, ref_count) in enumerate(top_refs):
                user_link = get_user_link(user_id_db, username, first_name, custom_name)
                prem_icon = f" {PREMIUM_EMOJI}" if is_premium(user_id_db) else ""
                message_text += f"{start_number + i}. {user_link}{prem_icon} ({ref_count} рефералов)\n"
        
        # Добавляем позицию пользователя
        if user_ref_count > 0 and user_position:
            message_text += f"\nТы находишься на {user_position} месте"
        
        # Создаем клавиатуру
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
# ===================== АДМИН КОМАНДЫ =====================

@bot.message_handler(func=lambda message: message.text.lower().startswith('рассылка') and is_admin(message.from_user.id))
def handle_broadcast(message):
    """Админ команда для рассылки"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        # Парсим текст рассылки
        parts = message.text.split(' ', 1)
        if len(parts) < 2:
            bot.reply_to(message, 
                        "📢 <b>Используйте:</b>\n"
                        "<code>рассылка [текст]</code>\n\n"
                        "Или ответьте на сообщение:\n"
                        "<code>рассылка</code>",
                        parse_mode='HTML')
            return
        
        # Получаем текст
        if message.reply_to_message:
            broadcast_text = message.reply_to_message.text
        else:
            broadcast_text = parts[1]
        
        # Подтверждение
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
        
        # Сохраняем текст для рассылки
        message_id = message.message_id
        chat_id = message.chat.id
        
        # Просто сохраняем в памяти
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
    
    # Начинаем рассылку
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
            # Получаем всех пользователей
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
                    
                    # Обновляем прогресс каждые 50 пользователей
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
                    
                    # Небольшая пауза
                    time.sleep(0.05)
                    
                except Exception as e:
                    failed += 1
        
        # Финальный результат
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

# ===================== ЗАПУСК =====================

def auto_reminder():
    """Автоматические напоминания"""
    import schedule
    import time as ttime
    
    # Напоминания в 14:00 и 20:00
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
        
        for user_id, in users[:100]:  # Ограничим 100 пользователями
            try:
                if send_reminder(user_id):
                    sent += 1
                ttime.sleep(0.2)
            except:
                pass
    
    print(f"🏆 Отправлено {sent} напоминаний")

# Запускаем в фоне
try:
    import threading
    reminder_thread = threading.Thread(target=auto_reminder, daemon=True)
    reminder_thread.start()
except:
    print("⚠️ Автонапоминания не запущены")

# Инициализация


print("🏆 Ежедневные задания загружены!")
@bot.message_handler(commands=['buy'])
def handle_buy(message):
    user_id = message.from_user.id
    
    buy_markup = InlineKeyboardMarkup()
    buy_markup.row(
        InlineKeyboardButton("⭐ 1 зв — 10,000", callback_data="stars_1"),
        InlineKeyboardButton("⭐ 5 зв — 66,000", callback_data="stars_5")
    )
    buy_markup.row(
        InlineKeyboardButton("🔥 15 зв — 266,000", callback_data="stars_15"),
        InlineKeyboardButton("🔥 50 зв — 1,000,000", callback_data="stars_50")
    )
    buy_markup.row(
        InlineKeyboardButton("⭐️ 150 — 4,000,000", callback_data="stars_150"),
        InlineKeyboardButton("⭐️ 250 — 8,000,000", callback_data="stars_250")
    )

    bot.send_message(
        message.chat.id,
        "🛒 <b>Магазин валюты</b>\n\n"
        "<blockquote>"
        "⭐ 1 зв — 10,000  (~1.5₽)\n"
        "⭐ 5 зв — 66,000  (~7.5₽)\n"
        "🔥 15 зв — 266,000  (~22.5₽)\n"
        "🔥 50 зв — 1,000,000  (~75₽)\n"
        "⭐️ 150 — 4,000,000  (~225₽)\n"
        "⭐️ 250 — 8,000,000  (~375₽)"
        "</blockquote>\n\n"
        "🔥 — самое выгодное соотношение\n"
        "Лучше спросить @Cary_Python,может напрямую будет небольшой бонус",
        reply_markup=buy_markup,
        parse_mode='HTML'
    )


# ── Система скидок ────────────────────────────────────────────────────────────
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
            # Админ выдаёт всегда
            if existing is None:
                cursor.execute('INSERT INTO discounts (user_id, percent, used, auto_given) VALUES (?, ?, 0, 0)', (user_id, percent))
            else:
                cursor.execute('UPDATE discounts SET percent = ?, used = 0, created_at = CURRENT_TIMESTAMP WHERE user_id = ?', (percent, user_id))
            return True
        else:
            # Авто: только если записи нет вообще (первый и единственный раз)
            if existing is None:
                cursor.execute('INSERT INTO discounts (user_id, percent, used, auto_given) VALUES (?, ?, 0, 1)', (user_id, percent))
                return True
            # Уже была авто-скидка (использована или нет) — больше не выдаём
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
        # deep link открывает бота и сразу запускает команду доната
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
# ─────────────────────────────────────────────────────────────────────────────

# Обработчик выбора пакета
@bot.callback_query_handler(func=lambda call: call.data.startswith('stars_'))
def handle_stars_selection(call):
    user_id = call.from_user.id
    package = call.data
    
    # Только в личных сообщениях с ботом
    if call.message.chat.type != 'private':
        bot.answer_callback_query(call.id, "💳 Покупки доступны только в личных сообщениях с ботом!", show_alert=True)
        return
    
    packages = {
        "stars_1":   {"amount": 10000,    "stars": 1,   "title": "10,000"},
        "stars_5":   {"amount": 66000,    "stars": 5,   "title": "66,000"},
        "stars_15":  {"amount": 266000,   "stars": 15,  "title": "266,000"},
        "stars_50":  {"amount": 1000000,  "stars": 50,  "title": "1,000,000"},
        "stars_150": {"amount": 4000000,  "stars": 150, "title": "4,000,000"},
        "stars_250": {"amount": 8000000,  "stars": 250, "title": "8,000,000"},
    }
    
    if package in packages:
        pkg = packages[package]
        
        # Создаем инвойс для Stars
        # Форматируем сумму без эмодзи для инвойса
        clean_amount = f"{pkg['amount']:,}".replace(',', ' ')
        
        # Проверяем наличие скидки (НЕ списываем до реальной оплаты)
        discount_pct = get_discount(user_id)
        actual_stars = pkg['stars']
        discount_line = ''
        if discount_pct:
            actual_stars = max(1, int(pkg['stars'] * (1 - discount_pct / 100)))
            discount_line = f" (-{discount_pct}%)"
        
        prices = [LabeledPrice(label=pkg["title"], amount=actual_stars)]
        # Передаём скидку через payload чтобы списать только после оплаты
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

# Обработчик предварительной проверки
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


# Обработчик успешного платежа
@bot.message_handler(content_types=['successful_payment'])
def handle_successful_payment(message):
    try:
        user_id = message.from_user.id
        payment_info = message.successful_payment
        
        # Парсим payload для получения суммы
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
            # Если в payload есть скидка — списываем её (оплата прошла)
            if len(payload_parts) >= 4 and int(payload_parts[3]) > 0:
                use_discount(user_id)
            
            # Начисляем валюту пользователю
            update_balance(user_id, amount)
            
            # Получаем новый баланс
            new_balance = get_balance(user_id)
            
            # Отправляем подтверждение
            bot.send_message(
                message.chat.id,
                f"<tg-emoji emoji-id='5440354006335495210'>🌸</tg-emoji> <b>Баланс пополнен</b>\n"
                f"<blockquote>Начислено: {format_balance(amount)}\n"
                f"Баланс: {format_balance(new_balance)}</blockquote>",
                parse_mode='HTML')
            
            # Скидка 50% после крупных покупок (15/50/150/250 звёзд) — только 1 раз
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
        
        # Находим пользователя
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
        
        # Выдаём скидку
        set_discount(target_user_id, percent, admin=True)
        send_discount_message(target_user_id, percent)
        
        bot.reply_to(message,
            f"<tg-emoji emoji-id='5440354006335495210'>🌸</tg-emoji> Скидка {percent}% отправлена пользователю {target}",
            parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка в handle_admin_discount: {e}")
        bot.reply_to(message, "❌ Ошибка при выдаче скидки")

# Обработчик кнопки покупки валюты
@bot.message_handler(func=lambda message: message.text in ["💎 Донат", "Донат"])
def handle_buy_currency_button(message):
    handle_buy(message)



# Обработчик команды "Актив"
@bot.message_handler(func=lambda message: message.text.lower() == 'актив')
def handle_active(message):
    try:
        with get_db_cursor() as cursor:
            # Общий капитал
            cursor.execute('SELECT SUM(balance + bank_deposit) FROM users')
            total_capital = cursor.fetchone()[0] or 0
            
            # Количество пользователей
            cursor.execute('SELECT COUNT(*) FROM users')
            user_count = cursor.fetchone()[0]

        message_text = f"📊 <b>Статистика</b>\n\n💸 Экономика: {format_balance(total_capital)}\n\n🟢 Пользователей: {user_count}"
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔄 Обновить", callback_data="refresh_active_stats"))
        
        bot.send_message(message.chat.id, message_text, reply_markup=markup, parse_mode='HTML')
    
    except:
        bot.send_message(message.chat.id, "⚠️ Что-то пошло не так. Попробуй ещё раз.")

# Обработчик кнопки обновления
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



# Обработчик кнопки "Топ"
@bot.message_handler(func=lambda message: message.text in ["🏆 Топ", "🏆", "Топ", "топ", "/top"])
def handle_top(message):
    try:
        user_id = message.from_user.id
        show_top_balance(message.chat.id, user_id, page=0)
    except Exception as e:
        print(f"Ошибка в handle_top: {e}")
        bot.send_message(message.chat.id, "⚠️ Что-то пошло не так. Попробуй ещё раз.")

# Обработчик callback для топа
@bot.callback_query_handler(func=lambda call: call.data.startswith('top_'))
def top_callback_handler(call):
    user_id = call.from_user.id
    data = call.data
    message_id = call.message.message_id
    
    try:
        if data.startswith('top_type_'):
            # Переключение между типами топа
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
            # Навигация по страницам
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
        
# Функция для получения серии кликов
def get_click_streak(user_id):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT click_streak, total_clicks FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result if result else (0, 0)

# Функция для обновления серии кликов
def update_click_streak(user_id, amount):
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE users SET click_streak = click_streak + ?, total_clicks = total_clicks + 1 WHERE user_id = ?', (amount, user_id))

# Функция для получения информации о ежедневном бонусе
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

# Функция для расчета дохода майнинга
def calculate_mining_income(video_cards):
    base_income = 250
    income = int(base_income * (1.6 ** (video_cards - 1))) if video_cards > 0 else 0
    return income

# Функция для расчета цены видеокарты
def calculate_video_card_price(video_cards):
    base_price = 5000
    return base_price * (2 ** video_cards)

def get_roulette_photo_path(winning_number):
    """Найти файл изображения для числа рулетки"""
    base_path = f"рулетка/{winning_number}"
    
    # Проверяем разные форматы
    formats = ['.png', '.jpg', '.jpeg', '.PNG', '.JPG', '.JPEG']
    
    for fmt in formats:
        test_path = base_path + fmt
        if os.path.exists(test_path):
            print(f"🏆 Найден файл: {test_path}")
            return test_path
    
    print(f"❌ Файл для числа {winning_number} отсутствует в форматах: {formats}")
    return None


# Команда для админа чтобы добавить/изменить фото рулетки
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
        
        # Проверяем что число валидное
        if not number.isdigit() or not (0 <= int(number) <= 36):
            bot.send_message(message.chat.id, "❌ Число должно быть от 0 до 36", parse_mode='HTML')
            return
        
        # Сохраняем информацию о числе для следующего шага
        bot.register_next_step_handler(message, process_roulette_photo, number)
        
        bot.send_message(message.chat.id, f"📸 Отправьте фото для числа {number}\n\n⚠️ Фото будет сохранено как: рулетка/{number}.png")
        
    except Exception as e:
        print(f"Ошибка в handle_roulette_photo_add: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка!", parse_mode='HTML')

def process_roulette_photo(message, number):
    """Обработать полученное фото"""
    try:
        # Проверяем что это фото
        if not message.photo:
            bot.send_message(message.chat.id, "❌ Это не фото! Отправьте изображение.", parse_mode='HTML')
            return
        
        # Получаем файл фото
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # Создаем папку если нет
        if not os.path.exists("рулетка"):
            os.makedirs("рулетка")
        
        # Сохраняем фото
        photo_path = f"рулетка/{number}.png"
        with open(photo_path, 'wb') as new_file:
            new_file.write(downloaded_file)
        
        # Отправляем подтверждение
        bot.send_message(message.chat.id, f"🏆 Фото для числа {number} успешно сохранено!\n\n📁 Путь: {photo_path}", parse_mode='HTML')
        
        # Показываем превью
        with open(photo_path, 'rb') as photo:
            bot.send_photo(message.chat.id, photo, caption=f"🎰 Превью для числа {number}", parse_mode='HTML')
            
    except Exception as e:
        print(f"Ошибка в process_roulette_photo: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка при сохранении фото: {e}", parse_mode='HTML')

# Команда для просмотра всех фото рулетки
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
        
        # Сортируем по числу
        png_files.sort(key=lambda x: int(x.split('.')[0]) if x.split('.')[0].isdigit() else 0)
        
        message_text = f"📁 Фото рулетки ({len(png_files)} файлов):\n\n"
        
        for file in png_files:
            number = file.split('.')[0]
            file_path = f"рулетка/{file}"
            file_size = os.path.getsize(file_path)
            message_text += f"🎰 {number}: {file} ({file_size} байт)\n"
        
        bot.send_message(message.chat.id, message_text)
        
        # Показываем несколько превью
        for file in png_files[:3]:  # Первые 3 файла
            number = file.split('.')[0]
            file_path = f"рулетка/{file}"
            with open(file_path, 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption=f"🎰 Число {number}", parse_mode='HTML')
                
    except Exception as e:
        print(f"Ошибка в handle_roulette_photos_list: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

# Команда для удаления фото рулетки
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
        
        # Создаем кнопку подтверждения
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

# Обработчик подтверждения удаления
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
# Глобальное хранилище активных игр
active_mines_games = {}

# ID запрещённого чата (FECTIZ_CHAT)
BANNED_CHAT_ID = int(os.getenv("BANNED_CHAT_ID", "0"))

# Функция для проверки, можно ли играть в этом чате
def can_play_in_chat(chat_id, chat_username=None):
    """Проверяет, разрешено ли играть в этом чате"""
    # Если это личные сообщения - всегда можно
    if str(chat_id).startswith('-100'):
        # Это группа или супергруппа
        # Проверяем по username если есть
        if chat_username and chat_username.lower() == 'fectiz_chat':
            return False
        # Проверяем по ID
        if chat_id == BANNED_CHAT_ID:
            return False
    # Во всех остальных случаях можно
    return True

# Декоратор для проверки перед играми
def check_game_allowed(func):
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
        
        return func(message)
    return wrapper

# ===================== ОБРАБОТЧИКИ ИГР =====================

# РУЛЕТКА
@bot.message_handler(func=lambda message: message.text.lower().startswith('рул '))
@check_game_allowed
def handle_roulette(message):
    user_id = message.from_user.id
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
        
        # Получаем информацию о пользователе
        user_info = get_user_info(user_id)
        custom_name = user_info['custom_name'] if user_info else None
        user_display = custom_name if custom_name else (f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name)
        
        # Крутим рулетку
        winning_number = random.randint(0, 36)
        
        # Определяем цвет
        red_numbers = [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]
        if winning_number == 0:
            winning_color = "🟢"
        elif winning_number in red_numbers:
            winning_color = "🔴"
        else:
            winning_color = "⚫️"
        
        # Проверяем ставку
        is_winner = False
        multiplier = 1
        bet_symbol = ""
        
        # Типы ставок
        if bet_type in ['красное', 'крас', 'кра', 'к', 'red']:
            is_winner = winning_color == "🔴"
            multiplier = 2
            bet_symbol = "🔴"
        elif bet_type in ['черное', 'чёр', 'чер', 'ч', 'black']:
            is_winner = winning_color == "⚫️"
            multiplier = 2
            bet_symbol = "⚫️"
        elif bet_type in ['зеленое', 'зелен', 'зел', 'з', 'green']:
            is_winner = winning_color == "🟢"
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
        
        # Списываем ставку
        update_balance(user_id, -bet_amount)
        
        if is_winner:
            win_amount = int(bet_amount * multiplier)
            update_balance(user_id, win_amount)
            new_balance = get_balance(user_id)
            
            message_text = f"<b>🎉 {user_display} залет!</b>\n"
            message_text += f"<i>Ставка {format_balance(bet_amount)} на {bet_symbol}</i>\n"
            message_text += f"<blockquote>💵 Твой баланс: {format_balance(new_balance)}</blockquote>"
        else:
            sad_emojis = ["😕", "😟", "😩", "☹️", "😫", "😢"]
            random_sad_emoji = random.choice(sad_emojis)
            new_balance = get_balance(user_id)
            
            message_text = f"<b>{random_sad_emoji} {user_display} неудача(</b>\n"
            message_text += f"<i>Ставка {format_balance(bet_amount)} на {bet_symbol}</i>\n"
            message_text += f"<blockquote>💵 Твой баланс: {format_balance(new_balance)}</blockquote>"
        
        message_text += f"\n⚔️ Выпало: <b>{winning_number} {winning_color}</b>"
        
        # Ищем фото для выпавшего числа
        photo_path = get_roulette_photo_path(winning_number)

        if photo_path and os.path.exists(photo_path):
            with open(photo_path, 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption=message_text, parse_mode='HTML')
        else:
            bot.send_message(message.chat.id, message_text, parse_mode='HTML')
        
    except Exception as e:
        print(f"Ошибка в рулетке: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка в игре!", parse_mode='HTML')

# БАСКЕТБОЛ
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

        # Сразу списываем ставку с баланса
        update_balance(user_id, -bet_amount)

        dice_message = bot.send_dice(message.chat.id, emoji='🏀')
        time.sleep(1)

        result = dice_message.dice.value

        win = False
        multiplier = 1

        if result == 4 or result == 5:  # Попадание
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
        bot.send_message(message.chat.id, "⚠️ Ошибка в игре. Попробуй ещё раз.")

# ФУТБОЛ
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
        
        # Сразу списываем ставку с баланса
        update_balance(user_id, -bet_amount)
        
        dice_message = bot.send_dice(message.chat.id, emoji='⚽')
        time.sleep(1)
        
        result = dice_message.dice.value
        
        win = False
        multiplier = 1
        
        if result == 3 or result == 4:  # Гол
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
        bot.send_message(message.chat.id, "⚠️ Ошибка в игре. Попробуй ещё раз.")

# ДАРТС
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
        
        # Для промаха нужно в 2 раза больше денег
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
        
        # Списываем базовую ставку
        update_balance(user_id, -bet_amount)
        
        # Бросаем дротик
        dice_message = bot.send_dice(message.chat.id, emoji='🎯')
        time.sleep(1)
        
        result = dice_message.dice.value
        new_balance = get_balance(user_id)
        
        # Логика выигрыша
        if result == 6:
            # ЦЕНТР - x5 выигрыш
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
            # БЛИЗКОЕ КОЛЬЦО - x1 возврат ставки
            update_balance(user_id, bet_amount)  # Возвращаем ставку
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
            # ПРОМАХ (1-3) - минус еще одна ставка (итого -x2)
            update_balance(user_id, -bet_amount)  # Списываем еще раз
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
        bot.send_message(message.chat.id, "⚠️ Ошибка в игре. Попробуй ещё раз.")

# БОУЛИНГ
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
        
        # Сразу списываем ставку с баланса
        update_balance(user_id, -bet_amount)
        
        dice_message = bot.send_dice(message.chat.id, emoji='🎳')
        time.sleep(1)
        
        result = dice_message.dice.value
        
        win = False
        multiplier = 1
        
        if result == 6:  # Страйк (все кегли)
            win = True
            multiplier = 3
        elif result == 5:  # 9 кеглей
            win = True
            multiplier = 1.5
        elif result == 4:  # 7-8 кеглей
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
        bot.send_message(message.chat.id, "⚠️ Ошибка в игре. Попробуй ещё раз.")

# СЛОТЫ
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
        
        # Сразу списываем ставку с баланса
        update_balance(user_id, -bet_amount)
        
        dice_message = bot.send_dice(message.chat.id, emoji='🎰')
        time.sleep(1)
        
        result = dice_message.dice.value
        
        win = False
        multiplier = 1
        
        if result == 1:  # Джекпот
            win = True
            multiplier = 15
        elif result == 22:  # Три семерки
            win = True
            multiplier = 30
        elif result == 43:  # Три вишни
            win = True
            multiplier = 15
        elif result == 64:  # Три одинаковых символа
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
        bot.send_message(message.chat.id, "⚠️ Ошибка в игре. Попробуй ещё раз.")

# БАШНЯ
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
        
        # Парсим ставку
        bet_text = parts[1] if parts[0] == 'башня' else parts[1]
        bet_amount = parse_bet_amount(bet_text, balance)
        
        if bet_amount is None or bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки!", parse_mode='HTML')
            return
        
        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Не хватает монет!", parse_mode='HTML')
            return
        
        # Списываем ставку
        update_balance(user_id, -bet_amount)
        
        # Начинаем игру
        game_id = start_tower_game(user_id, bet_amount)
        game = tower_games[game_id]
        
        # Создаем сообщение с игрой
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
        
        # Сохраняем ID сообщения для редактирования
        game['message_id'] = sent_message.message_id
        game['chat_id'] = message.chat.id
        
    except Exception as e:
        print(f"Ошибка в игре Башня: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при запуске игры!", parse_mode='HTML')

# КОСТИ (куб)
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
        
        # Сразу списываем ставку с баланса
        update_balance(user_id, -bet_amount)
        
        dice_message = bot.send_dice(message.chat.id, emoji='🎲')
        time.sleep(1)
        
        result = dice_message.dice.value
        
        win = False
        multiplier = 1
        
        # Обработка разных типов ставок
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
                    update_balance(user_id, bet_amount)  # Возвращаем деньги
                    return
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный тип ставки! Используйте: 1-6, малые, большие, чет, нечет", parse_mode='HTML')
                update_balance(user_id, bet_amount)  # Возвращаем деньги
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
        bot.send_message(message.chat.id, "⚠️ Ошибка в игре. Попробуй ещё раз.")

# МИНЫ
@bot.message_handler(func=lambda message: message.text.lower().startswith('мины'))
@check_game_allowed
def handle_mines(message):
    try:
        user_id = message.from_user.id
        
        # Проверяем, не играет ли игрок уже
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
        
        # Парсим ставку
        bet_amount = parse_bet_amount(parts[1], get_balance(user_id))
        if bet_amount is None or bet_amount < 100:
            bot.send_message(message.chat.id, f"❌ Мин. бет: {format_balance(100)}", parse_mode='HTML')
            return
        
        # Проверяем баланс
        balance = get_balance(user_id)
        if bet_amount > balance:
            bot.send_message(message.chat.id, f"❌ Не хватает монет!", parse_mode='HTML')
            return
        
        # Парсим количество мин
        try:
            mines_count = int(parts[2])
            if mines_count < 1 or mines_count > 24:
                bot.send_message(message.chat.id, "❌ Мин: 1-24", parse_mode='HTML')
                return
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверное количество!", parse_mode='HTML')
            return
        
        # Списываем ставку
        update_balance(user_id, -bet_amount)
        
        # Создаем игру
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
        
        # Показываем игровое поле
        show_mines_game(message.chat.id, user_id)
        
    except Exception as e:
        print(f"Ошибка в игре мины: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при запуске!", parse_mode='HTML')

# ЛОТЕРЕЯ
@bot.message_handler(func=lambda message: message.text.lower().startswith('лот '))
@check_game_allowed
def handle_lottery(message):
    user_id = message.from_user.id
    
    if user_id in lottery_games:
        bot.reply_to(message, "🎰 Уже есть активный билет!")
        return
    
    # Парсим ставку
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
    
    # Проверяем лимиты
    if bet < LOTTERY_MACHINE_CONFIG["min_bet"]:
        bot.reply_to(message, f"❌ Мин. бет: {format_balance(LOTTERY_MACHINE_CONFIG['min_bet'])}", parse_mode='HTML')
        return
    
    if bet > LOTTERY_MACHINE_CONFIG["max_bet"]:
        bot.reply_to(message, f"❌ Макс. бет: {format_balance(LOTTERY_MACHINE_CONFIG['max_bet'])}", parse_mode='HTML')
        return
    
    if bet > balance:
        bot.reply_to(message, f"❌ Не хватает монет!", parse_mode='HTML')
        return
    
    # Снимаем ставку
    update_balance(user_id, -bet)
    
    # Создаем билет
    ticket = create_lottery_ticket(bet)
    lottery_games[user_id] = ticket
    
    # Получаем инфо пользователя
    user_info = get_user_info(user_id)
    name = user_info['custom_name'] or user_info['username'] or user_info['first_name']
    
    # Создаем клавиатуру с 3 кнопками
    markup = InlineKeyboardMarkup(row_width=3)
    
    # Создаем кнопки для 3 ячеек
    buttons = []
    for i in range(3):
        if ticket["revealed"][i]:
            buttons.append(InlineKeyboardButton(ticket["symbols"][i], callback_data=f"lot_{i}_done"))
        else:
            buttons.append(InlineKeyboardButton("⬜", callback_data=f"lot_{user_id}_{i}"))
    
    markup.row(*buttons)
    markup.row(InlineKeyboardButton("⚔️ ОТКРЫТЬ ВСЕ", callback_data=f"lot_{user_id}_all"))
    
    # Отправляем сообщение
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
    total_cells = 25  # 5x5 поле
    board = [False] * total_cells
    
    # Расставляем мины
    mine_positions = random.sample(range(total_cells), mines_count)
    for pos in mine_positions:
        board[pos] = True
    
    return board

def show_mines_game(chat_id, user_id, message_id=None):
    """Показывает игровое поле"""
    if user_id not in active_mines_games:
        return
    
    game_data = active_mines_games[user_id]
    
    # Проверяем не истекло ли время
    time_passed = time.time() - game_data['start_time']
    if time_passed > 240:
        refund_expired_mines_games()
        return
    
    time_left = 240 - time_passed
    minutes_left = int(time_left // 60)
    seconds_left = int(time_left % 60)
    
    # Создаем клавиатуру
    markup = create_mines_keyboard(game_data)
    
    # Информация об игре
    info_text = f"🎲 <b>Мины</b>\n\n"
    info_text += f"💵 Ставка: {format_balance(game_data['bet_amount'])}\n"
    info_text += f"💣 Мин: {game_data['mines_count']}\n"
    info_text += f"🏆 Открыто: {game_data['opened_cells']}/25\n"
    info_text += f"⏰ Возврат: {minutes_left}:{seconds_left:02d}\n\n"
    
    # Всегда показываем текущий множитель
    multiplier = calculate_multiplier(game_data['mines_count'], game_data['opened_cells'])
    
    info_text += f"📈 Множитель: <b>{multiplier:.2f}x</b>\n"
    
    # Показываем выигрыш только если есть открытые клетки
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
    
    # Кнопки действий
    markup.row(
        InlineKeyboardButton("💵 Забрать", callback_data="mines_cashout"),
        InlineKeyboardButton("❌ Выйти", callback_data="mines_exit")
    )
    
    return markup

# Обработчик нажатий на клетки
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

# Обработчик для новой игры
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
    
    # Реалистичные множители (основано на реальных казино)
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

# Запускаем проверку
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
                # Для аксессуаров создаем список
                if item_type not in equipped_dict:
                    equipped_dict[item_type] = []
                equipped_dict[item_type].append(image_name)
            else:
                # Для основной одежды просто сохраняем
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
    
    # Проверяем файлы
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
# Команда для изменения типа конкретной вещи
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
            # Проверяем существование вещи
            cursor.execute("SELECT name FROM clothes_shop WHERE id = ?", (item_id,))
            item = cursor.fetchone()
            
            if not item:
                bot.send_message(message.chat.id, "❌ Вещь не найдено!", parse_mode='HTML')
                return
            
            # Меняем тип
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
        
        # Объединяем название одежды (может состоять из нескольких слов)
        item_name = ' '.join(parts[2:-1])
        supply_amount = parts[-1]
        
        # Проверяем количество
        try:
            supply = int(supply_amount)
            if supply < 1:
                bot.send_message(message.chat.id, "❌ Количество должно быть больше 0!", parse_mode='HTML')
                return
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверное количество!", parse_mode='HTML')
            return
        
        # Ищем одежду в магазине
        with get_db_cursor() as cursor:
            cursor.execute('SELECT id, name FROM clothes_shop WHERE name LIKE ?', (f'%{item_name}%',))
            items = cursor.fetchall()
            
            if not items:
                bot.send_message(message.chat.id, f"❌ Одежда '{item_name}' отсутствуета в магазине!", parse_mode='HTML')
                return
            
            if len(items) > 1:
                # Показываем список найденных предметов
                items_text = "📋 Найдено несколько предметов:\n\n"
                for item in items:
                    items_text += f"• {item[1]} (ID: {item[0]})\n"
                items_text += f"\nУточните название или используйте ID: добавить саплай [ID] [количество]"
                bot.send_message(message.chat.id, items_text)
                return
            
            # Найден один предмет
            item_id, item_name = items[0]
            
            # Устанавливаем саплай
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
        
        # Проверяем лимит
        if supply != -1 and sold_count >= supply:
            return False, f"❌ {name} распродан!"
        
        balance = get_balance(user_id)
        
        if balance < price:
            return False, f"❌ Не хватает монет! Нужно: {format_balance(price)}"
        
        cursor.execute('SELECT * FROM user_clothes WHERE user_id = ? AND item_id = ?', (user_id, item_id))
        if cursor.fetchone():
            return False, f"❌ У вас уже есть {name}!"
        
        # Покупаем и обновляем счетчик
        cursor.execute('INSERT INTO user_clothes (user_id, item_id) VALUES (?, ?)', (user_id, item_id))
        cursor.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (price, user_id))
        
        # Увеличиваем счетчик продаж если есть лимит
        if supply != -1:
            cursor.execute('UPDATE clothes_shop SET sold_count = sold_count + 1 WHERE id = ?', (item_id,))
        
        return True, f"🏆 {name} куплен!"

def equip_clothes(user_id, item_id):
    """Надеть одежду с обновлением образа"""
    with get_db_cursor() as cursor:
        # Получаем информацию о вещи
        cursor.execute('SELECT type, name FROM clothes_shop WHERE id = ?', (item_id,))
        item = cursor.fetchone()
        if not item:
            return False, "Вещь отсутствуета"
        
        item_type, name = item
        
        # Лимиты для каждого типа одежды
        type_limits = {
            'Голова': 1,      # 1 вещь на голову
            'Тело': 1,        # 1 вещь на тело  
            'Ноги': 1,        # 1 вещь на ноги
            'Слева': 2,       # 2 аксессуара слева
            'Справа': 2,      # 2 аксессуара справа
            'accessories': 2  # 2 аксессуара (совместимость)
        }
        
        # Получаем текущее количество надетых вещей этого типа
        cursor.execute('''
            SELECT COUNT(*) 
            FROM user_clothes uc 
            JOIN clothes_shop cs ON uc.item_id = cs.id 
            WHERE uc.user_id = ? AND uc.equipped = 1 AND cs.type = ?
        ''', (user_id, item_type))
        
        current_equipped = cursor.fetchone()[0]
        max_allowed = type_limits.get(item_type, 1)
        
        # Проверяем не превышен ли лимит
        if current_equipped >= max_allowed:
            # Для аксессуаров НЕ снимаем старые, просто сообщаем о лимите
            if item_type in ['Слева', 'Справа', 'accessories']:
                return False, f"❌ Достигнут лимит аксессуаров в {item_type}! Можно надеть только {max_allowed}."
            else:
                # Для всех остальных типов снимаем старую вещь
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
        
        # Надеваем выбранную вещь
        cursor.execute('UPDATE user_clothes SET equipped = 1 WHERE user_id = ? AND item_id = ?', (user_id, item_id))
        
        # Удаляем старый файл образа
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
        
        # Удаляем старый файл образа
        outfit_path = f"images/outfit_{user_id}.jpg"
        if os.path.exists(outfit_path):
            os.remove(outfit_path)
            print(f"🗑️ Удален старый образ после снятия: {name}")
        
        return True, f"🏆 {name} снят!"
# Обновляем функцию get_equipment_limits_info
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
        
        # Открываем базовое изображение
        base_image = Image.open(base_path).convert("RGBA")
        equipped = get_equipped_clothes(user_id)
        
        # Простой порядок отрисовки
        for layer_type in ['Ноги', 'Тело', 'Голова', 'Слева', 'Справа']:
            if layer_type in equipped:
                if layer_type in ['Слева', 'Справа']:
                    # Аксессуары
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
                    # Основная одежда
                    image_path = f"images/{equipped[layer_type]}"
                    if os.path.exists(image_path):
                        try:
                            layer_image = Image.open(image_path).convert("RGBA")
                            if layer_image.size != base_image.size:
                                layer_image = layer_image.resize(base_image.size, Image.Resampling.LANCZOS)
                            base_image = Image.alpha_composite(base_image, layer_image)
                        except:
                            continue
        
        # Сохраняем результат
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
            
            # Проверяем размеры
            if clothes_image.size != base_image.size:
                clothes_image = clothes_image.resize(base_image.size, Image.Resampling.LANCZOS)
            
            # Накладываем слой
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
            
            # Проверяем файл
            file_path = f"images/{item[3]}"
            if os.path.exists(file_path):
                message_text += f"\n🏆 Файл существует: {file_path}"
                
                # Проверяем размер файла
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
            # Возвращаем оригинальное имя файла
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
# Глобальные переменные для хранения текущих страниц
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
        clothes = get_clothes_shop()  # Исправлено на get_clothes_shop()
        
        if not clothes:
            bot.send_message(chat_id, "🛍️ 🛒 Шоп пока пуст!")
            return
        
        # Получаем уникальные категории из одежды
        categories = list(set([item['type'] for item in clothes]))
        
        # Русские названия для категорий
        category_names = {
            'body': '👕 Одежда для тела',
            'hat': '🧢 Головные уборы', 
            'shoes': '👟 Обувь',
            'accessories': '💍 Аксессуары'
        }
        
        markup = InlineKeyboardMarkup(row_width=2)
        
        # Добавляем кнопки категорий
        buttons = []
        for category in categories:
            display_name = category_names.get(category, category)
            buttons.append(InlineKeyboardButton(display_name, callback_data=f"shop_category_{category}"))
        
        # Распределяем кнопки по 2 в ряд
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
        all_clothes = get_clothes_shop()  # Исправлено на get_clothes_shop()
        
        # Фильтруем по категории если указана
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
        
        # Добавляем информацию о саплае
        supply_info = ""
        if item.get('supply', -1) != -1:
            available = item['supply'] - item.get('sold_count', 0)
            supply_info = f"\n📦 Осталось: {available}/{item['supply']}"
        
        # Информация о категории
        category_info = f" | 📁 {item['type']}" if category == 'all' else ""
        
        caption = f"👕 {item['name']}\n💵 {format_balance(item['price'])}{category_info}{supply_info}\n\n📄 Страница {page + 1} из {total_pages}"
        markup = create_shop_markup(item['id'], page, total_pages, category)
        photo_path = f"images/{item['image_name']}"
        
        current_data = shop_pages.get(user_id, {'page': 0, 'message_id': None, 'category': None})
        message_id = current_data.get('message_id')
        
        # Если это первое сообщение
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
            # Редактируем существующее сообщение
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
    
    # Собираем клавиатуру как JSON
    keyboard = {
        "inline_keyboard": [
            # Первый ряд - зеленая кнопка покупки
            [
                {
                    "text": f"Купить за  {price} 🌸",
                    "callback_data": f"buy_{item_id}",
                    "style": "success"  # 🟢 Зеленая!
                }
            ],
            # Второй ряд - навигация
            [],
            # Третий ряд - категории
            [
                {
                    "text": "📂 Каталог",
                    "callback_data": "shop_categories"
                }
            ]
        ]
    }
    
    # Заполняем навигацию
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
    
    return json.dumps(keyboard)  # Возвращаем JSON строку

@bot.callback_query_handler(func=lambda call: call.data.startswith('shop_category_'))
def handle_shop_category(call):
    """Обработчик выбора категории"""
    user_id = call.from_user.id
    category = call.data.split('_')[2]  # all, body, hat, shoes, accessories
    
    # Сбрасываем страницу на 0 при смене категории
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
    clothes = get_clothes_shop()  # Исправлено на get_clothes_shop()
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
        
        # Если это первое сообщение
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
            # Редактируем существующее сообщение
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
            # Финальная покупка после подтверждения
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
            # Показываем подтверждение покупки
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
                
                # Обновляем образ после надевания
                outfit_path = create_character_outfit(user_id)
                print(f"🔄 Образ обновлен после надевания: {outfit_path}")
            
        elif call.data.startswith('unequip_'):
            item_id = int(call.data.split('_')[1])
            success, msg = unequip_clothes(user_id, item_id)
            bot.answer_callback_query(call.id, msg)
            if success and user_id in wardrobe_pages:
                current_page = wardrobe_pages[user_id]['page']
                show_wardrobe_page(call.message.chat.id, user_id, current_page)
                
                # Обновляем образ после снятия
                outfit_path = create_character_outfit(user_id)
                print(f"🔄 Образ обновлен после снятия: {outfit_path}")
                
    except Exception as e:
        print(f"Ошибка в handle_clothes_actions: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка!")

def get_item_price(item_id):
    """Получить цену айтема по ID"""
    clothes = get_shop_clothes()  # Было get_clothes_shop()
    for item in clothes:
        if item['id'] == item_id:
            return item['price']
    return 0
    
# Хранилище активных капч
active_captchas = {}

def generate_math_captcha():
    """Сгенерировать математическую капчу"""
    a = random.randint(1, 15)
    b = random.randint(1, 15)
    operation = random.choice(['+', '-'])
    
    if operation == '+':
        answer = a + b
        question = f"{a} + {b}"
    else:  # '-'
        a, b = max(a, b), min(a, b)
        answer = a - b
        question = f"{a} - {b}"
    
    return question, str(answer)

def is_new_user(user_id):
    """Проверить, новый ли игрок"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
        return cursor.fetchone() is None

# Обработчик команды /start
@bot.message_handler(commands=['start'])
def start(message):
    try:
        user_id = message.from_user.id
        username = message.from_user.username
        first_name = message.from_user.first_name
        
        # Проверяем, новый ли игрок
        if is_new_user(user_id):
            # НОВЫЙ игрок - показываем капчу
            if user_id in active_captchas:
                bot.send_message(message.chat.id, "⏳ Вы уже проходите регистрацию! Решите капчу.")
                return
            
            # Генерируем капчу
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
            
            markup = ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add(KeyboardButton("🔄 Новая капча"))
            
            captcha_text = f"""🔐 <b>Проверка безопасности</b>

Реши пример, чтобы начать:
<b>{question} = ?</b>

У тебя 3 попытки — введи ответ числом."""

            bot.send_message(message.chat.id, captcha_text, reply_markup=markup, parse_mode='HTML')
            
        else:
            # СТАРЫЙ игрок - сразу показываем меню
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
                # Проверяем чек
                cursor.execute('SELECT amount, max_activations, current_activations, password, target_username FROM checks WHERE code = ?', (ref_code,))
                check_data = cursor.fetchone()
                
                if check_data:
                    amount, max_activations, current_activations, password, target_username = check_data
                    
                    # Проверяем, активировал ли игрок уже этот чек
                    cursor.execute('SELECT * FROM check_activations WHERE user_id = ? AND check_code = ?', (user_id, ref_code))
                    already_activated = cursor.fetchone()
                    
                    if already_activated:
                        bot.send_message(message.chat.id, "❌ Вы уже активировали этот чек!", parse_mode='HTML')
                    elif current_activations < max_activations:
                        # Если у чека есть пароль, запрашиваем его
                        if password:
                            msg = bot.send_message(message.chat.id, f"🔐 У этого чека есть пароль. Введите пароль для активации:")
                            bot.register_next_step_handler(msg, process_check_password, ref_code, user_id, amount, max_activations, current_activations, password, target_username)
                        else:
                            # Активируем чек
                            success, result_message = activate_check(user_id, ref_code, amount, max_activations, current_activations, password, target_username)
                            bot.send_message(message.chat.id, result_message, parse_mode='HTML')
                    else:
                        bot.send_message(message.chat.id, "❌ Чек уже использован максимальное количество раз!", parse_mode='HTML')
                else:
                    # Проверяем реферальную ссылку
                    cursor.execute('SELECT user_id FROM users WHERE referral_code = ?', (ref_code,))
                    ref_user = cursor.fetchone()
                    
                    if ref_user and ref_user[0] != user_id:
                        referred_by = ref_user[0]
                        cursor.execute('SELECT referred_by FROM users WHERE user_id = ?', (user_id,))
                        current_ref = cursor.fetchone()
                        
                        if not current_ref or not current_ref[0]:
                            referrer_bonus = 5000
                            user_bonus = 1000
                            referrer_exp = 500  # приятный exp за приглашение
                            
                            cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (referrer_bonus, referred_by))
                            cursor.execute('UPDATE users SET referred_by = ? WHERE user_id = ?', (referred_by, user_id))
                            cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (user_bonus, user_id))
                            bot.send_message(message.chat.id, "🎉 Ты получил 1 000🌸 за приглашение друга!", parse_mode='HTML')
                            add_experience(referred_by, referrer_exp)
                            try:
                                bot.send_message(referred_by, f"👥 Друг принял твоё приглашение!\n⭐ +{referrer_exp} опыта", parse_mode='HTML')
                            except Exception:
                                pass
        
        # Deep link: если пришёл по ссылке скидки — сразу открываем донат
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

# Обработчик ответов на капчу (только для новых пользователей)
@bot.message_handler(func=lambda message: message.from_user.id in active_captchas and not message.text.startswith('/'))
def handle_captcha_answer(message):
    user_id = message.from_user.id
    captcha_data = active_captchas[user_id]
    
    # Обработка кнопки "Новая капча"
    if message.text == "🔄 Новая капча":
        refresh_captcha(message)
        return
    
    try:
        user_answer = message.text.strip()
        correct_answer = captcha_data['answer']
        
        if user_answer == correct_answer:
            # Капча пройдена - регистрируем пользователя
            del active_captchas[user_id]
            complete_new_user_registration(
                user_id, 
                captcha_data['username'], 
                captcha_data['first_name'], 
                captcha_data['ref_code']
            )
        else:
            captcha_data['attempts'] += 1
            remaining_attempts = captcha_data['max_attempts'] - captcha_data['attempts']
            
            if remaining_attempts > 0:
                bot.send_message(message.chat.id, f"❌ Неверно! Осталось попыток: {remaining_attempts}", parse_mode='HTML')
            else:
                # Превышено количество попыток
                del active_captchas[user_id]
                bot.send_message(message.chat.id, "🚫 Превышено количество попыток. Попробуйте позже.", reply_markup=ReplyKeyboardRemove())
                
    except Exception as e:
        bot.send_message(message.chat.id, "❌ Введите число!", parse_mode='HTML')

def complete_new_user_registration(user_id, username, first_name, ref_code):
    """Завершение регистрации нового пользователя после капчи"""
    try:
        # Создаем пользователя
        get_or_create_user(user_id, username, first_name)
        
        # Обрабатываем реферальный код если есть
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

def handle_referral_code(user_id, ref_code):
    """Обработка реферального кода"""
    try:
        with get_db_cursor() as cursor:
            # Проверяем чек
            cursor.execute('SELECT amount, max_activations, current_activations, password, target_username FROM checks WHERE code = ?', (ref_code,))
            check_data = cursor.fetchone()
            
            if check_data:
                amount, max_activations, current_activations, password, target_username = check_data
                
                # Проверяем, активировал ли игрок уже этот чек
                cursor.execute('SELECT * FROM check_activations WHERE user_id = ? AND check_code = ?', (user_id, ref_code))
                already_activated = cursor.fetchone()
                
                if not already_activated and current_activations < max_activations:
                    # Чек с паролем нельзя активировать автоматически — просим ввести пароль
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
                # Проверяем реферальную ссылку
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
# Функция активации чека
def activate_check(user_id, ref_code, amount, max_activations, current_activations, password, target_username):
    with get_db_cursor() as cursor:
        # Проверяем, предназначен ли чек для конкретного пользователя
        if target_username:
            cursor.execute('SELECT username FROM users WHERE user_id = ?', (user_id,))
            user_data = cursor.fetchone()
            current_username = f"@{user_data[0]}" if user_data and user_data[0] else None
            if (current_username or '').lower() != (target_username or '').lower():
                return False, f"❌ Этот чек предназначен для {target_username}!"

        # Проверяем, не активировал ли уже этот пользователь чек (защита от гонки)
        cursor.execute('SELECT 1 FROM check_activations WHERE user_id = ? AND check_code = ?', (user_id, ref_code))
        if cursor.fetchone():
            return False, "❌ Вы уже активировали этот чек!"

        # Перепроверяем лимит активаций прямо внутри транзакции (защита от race condition)
        cursor.execute('SELECT current_activations, max_activations FROM checks WHERE code = ?', (ref_code,))
        row = cursor.fetchone()
        if not row:
            return False, "❌ Чек не найден!"
        real_current, real_max = row
        if real_current >= real_max:
            return False, "❌ Чек уже использован максимальное количество раз!"

        # Добавляем запись об активации
        cursor.execute('INSERT INTO check_activations (user_id, check_code) VALUES (?, ?)', (user_id, ref_code))

        # Обновляем счетчик активаций
        cursor.execute('UPDATE checks SET current_activations = current_activations + 1 WHERE code = ?', (ref_code,))

        # Начисляем деньги пользователю
        cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))

    return True, f"<tg-emoji emoji-id='5440354006335495210'>🌸</tg-emoji> <b>Вы активировали чек на {format_balance(amount)}</b> <tg-emoji emoji-id='5440354006335495210'>🌸</tg-emoji>!"

# Функция обработки пароля
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
        if current_time - captcha_data['created_at'] > 300:  # 5м
            expired_users.append(user_id)
    
    for user_id in expired_users:
        del active_captchas[user_id]
        print(f"🧹 Удалена просроченная капча для {user_id}")

@bot.message_handler(func=lambda message: message.text in ["📞 Помощь", "Помощь"])
def handle_help(message):
    """Показать справку и контакты"""
    
    help_text = """📞 <b>Помощь</b>"""


    # Создаем инлайн-клавиатуру с ссылками
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
# Обработчик команды "имя"
@bot.message_handler(func=lambda message: message.text.lower().startswith('имя '))
def handle_name_change(message):
    # Проверяем, что сообщение из личного чата
    
        
    try:
        user_id = message.from_user.id
        new_name = message.text[4:].strip()  # Убираем "имя " из начала
        
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
        

# Команда для пользователей: чек
@bot.message_handler(func=lambda message: message.text.lower().startswith('чек '))
def handle_user_check_command(message):
    user_id = message.from_user.id
    
    # Проверка варна
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
        
        # Общая сумма для списания
        total_amount = amount * max_activations
        
        if total_amount > balance:
            bot.send_message(message.chat.id, f"❌ Не хватает монет! Нужно: {format_balance(total_amount)}")
            return
        
        # Списываем деньги
        update_balance(user_id, -total_amount)
        
        # Генерируем код чека
        check_code = f"user{user_id}{random.randint(1000, 9999)}"
        
        # Сохраняем в БД
        with get_db_cursor() as cursor:
            cursor.execute('''
                INSERT INTO checks (code, amount, max_activations, password, created_by)
                VALUES (?, ?, ?, ?, ?)
            ''', (check_code, amount, max_activations, password, user_id))
        
        # Создаем ссылку
        bot_username = (bot.get_me()).username
        check_link = f"https://t.me/{bot_username}?start={check_code}"
        
        # Кнопка
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🎫 Активировать чек", url=check_link))
        
        # Сообщение с HTML
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

# Обработчик команды /чек в каналах и чатах
@bot.message_handler(commands=['чек'])
def handle_cheque_command(message):
    try:
        # Проверяем права (только админы могут создавать чеки)
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
        
        # Генерируем уникальный код чека
        check_code = f"cheque{random.randint(100000, 999999)}"
        
        # Сохраняем чек в БД
        with get_db_cursor() as cursor:
            cursor.execute('''
                INSERT INTO checks (code, amount, max_activations, password, created_by)
                VALUES (?, ?, ?, ?, ?)
            ''', (check_code, amount, max_activations, password, message.from_user.id))
        
        # Создаем ссылку для активации
        bot_username = (bot.get_me()).username
        check_link = f"https://t.me/{bot_username}?start={check_code}"
        
        # Создаем кнопку
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🎫 Активировать чек", url=check_link))
        
        # Формируем сообщение с HTML для премиум-эмодзи
        total_amount = amount * max_activations
        message_text = f"🎫 <b>Ваучер создан!</b>\n\n"
        message_text += f"💵 Сумма за активацию: {format_balance(amount)}\n"
        message_text += f"🔢 Активаций: {max_activations}\n"
        message_text += f"🔐 Пароль: {'есть' if password else 'нет'}"
        
        # Отправляем сообщение с HTML-разметкой
        bot.send_message(message.chat.id, message_text, reply_markup=markup, parse_mode='HTML')
        
        # Если есть пароль, отправляем его отдельным сообщением
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
# Обработчик инлайн-запросов для создания чеков - СПИСЫВАЕТСЯ С БАЛАНСА
@bot.inline_handler(func=lambda query: True)
def handle_inline_query(query):
    try:
        user_id = query.from_user.id
        query_text = query.query.strip()
        
        # Если пустой запрос - показываем инструкцию
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
        
        # Парсим параметры из запроса
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
        
        # Парсим параметры
        amount_str = parts[0]
        activations_str = parts[1]
        password = parts[2] if len(parts) > 2 else None
        
        # Проверяем сумму
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
        
        # Проверяем количество активаций
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
        
        # РАССЧИТЫВАЕМ ОБЩУЮ СУММУ ДЛЯ СПИСАНИЯ
        total_cost = amount * max_activations
        
        # ПРОВЕРЯЕМ БАЛАНС ПОЛЬЗОВАТЕЛЯ
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
        
        # СПИСЫВАЕМ ДЕНЬГИ С БАЛАНСА
        update_balance(user_id, -total_cost)
        
        # Создаем чек
        cheque_result = create_inline_cheque_result(amount_str, activations_str, password, 
                                                   f"{format_balance(amount)}, {max_activations} активаций")
        
        bot.answer_inline_query(query.id, [cheque_result], cache_time=1)
        
    except Exception as e:
        print(f"Ошибка в инлайн-запросе: {e}")

# Вспомогательная функция для создания инлайн-результата
def create_inline_cheque_result(amount_str, activations_str, password, description):
    # Генерируем уникальный код чека
    check_code = f"inline{random.randint(100000, 999999)}"
    
    amount = parse_bet_amount(amount_str, float('inf'))
    max_activations = int(activations_str)
    
    # Сохраняем чек в БД с ID создателя
    with get_db_cursor() as cursor:
        cursor.execute('''
            INSERT INTO checks (code, amount, max_activations, password, created_by)
            VALUES (?, ?, ?, ?, ?)
        ''', (check_code, amount, max_activations, password, user_id))
    
    # Создаем ссылку для активации
    bot_username = (bot.get_me()).username
    check_link = f"https://t.me/{bot_username}?start={check_code}"
    
    # Формируем сообщение
    message_text = f"🎫 Создан чек!\n\n"
    message_text += f"💵 Сумма за активацию: {format_balance(amount)}\n"
    message_text += f"🔢 Активаций: {max_activations}\n" 
    message_text += f"🔐 Пароль: {'есть' if password else 'нет'}\n\n"

    
    # Создаем кнопку
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
            # Активные пользователи (за последние 24 часа)
            cursor.execute('SELECT COUNT(*) FROM users WHERE last_activity > ?', (time.time() - 86400,))
            active_users_24h = cursor.fetchone()[0] or 0
            
            # Активные пользователи (за последние 7д)
            cursor.execute('SELECT COUNT(*) FROM users WHERE last_activity > ?', (time.time() - 604800,))
            active_users_7d = cursor.fetchone()[0] or 0
            
            # Общее количество пользователей
            cursor.execute('SELECT COUNT(*) FROM users')
            total_users = cursor.fetchone()[0] or 0
            
            # Новые пользователи за сегодня
            cursor.execute('SELECT COUNT(*) FROM users WHERE last_activity > ? AND last_activity > ?', 
                          (time.time() - 86400, time.time() - 172800))
            new_users_today = cursor.fetchone()[0] or 0
            
            # Общая экономика (балансы + вклады)
            cursor.execute('SELECT SUM(balance + bank_deposit) FROM users')
            total_economy = cursor.fetchone()[0] or 0
            
            # Общие балансы
            cursor.execute('SELECT SUM(balance) FROM users')
            total_balance = cursor.fetchone()[0] or 0
            
            # Общие вклады
            cursor.execute('SELECT SUM(bank_deposit) FROM users')
            total_deposits = cursor.fetchone()[0] or 0

        # Форматируем сообщение
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

        # Создаем клавиатуру с кнопкой обновления
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔄 Обновить", callback_data="refresh_active_stats"))
        
        if message_id:
            # Редактируем существующее сообщение
            bot.edit_message_text(
                message_text, 
                chat_id, 
                message_id,
                reply_markup=markup, 
                parse_mode='HTML'
            )
        else:
            # Отправляем новое сообщение
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

# Обработчик кнопки обновления
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
# Обработчик упоминания бота @netroon_bot
@bot.message_handler(func=lambda message: bot.get_me().username.lower() in message.text.lower() and not message.text.startswith('/'))
def handle_bot_mention(message):
    try:
        # Проверяем права (только админы)
        if not is_admin(message.from_user.id):
            return
        
        # Проверяем, что сообщение содержит ключевые слова для создания чека
        text_lower = message.text.lower()
        bot_username = bot.get_me().username.lower()
        
        # Если просто упомянули бота без параметров
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
        
        # Если упомянули бота с параметрами
        if f"@{bot_username}" in text_lower:
            # Извлекаем параметры после упоминания бота
            parts = message.text.split()
            bot_index = None
            
            # Находим индекс упоминания бота
            for i, part in enumerate(parts):
                if f"@{bot_username}" in part.lower():
                    bot_index = i
                    break
            
            if bot_index is not None and len(parts) > bot_index + 2:
                # Берем параметры после упоминания бота
                cheque_params = parts[bot_index + 1:]
                
                # Создаем временную команду и обрабатываем её
                fake_message = type('obj', (object,), {
                    'chat': message.chat,
                    'from_user': message.from_user,
                    'text': f"/чек {' '.join(cheque_params)}"
                })
                
                handle_cheque_command(fake_message)
                return
        
    except Exception as e:
        print(f"Ошибка при обработке упоминания: {e}")
                        
@bot.message_handler(func=lambda message: message.text.lower().startswith('клан разделить'))
def handle_clan_distribute(message):
    user_id = message.from_user.id
    user_clan = get_user_clan(user_id)
    
    if not user_clan:
        bot.send_message(message.chat.id, "❌ Вы не состоите в клане!", parse_mode='HTML')
        return
    
    if user_clan['role'] != 'leader':
        bot.send_message(message.chat.id, "❌ Только глава может распределять деньги!", parse_mode='HTML')
        return
    
    try:
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Используйте: клан разделить [сумма]", parse_mode='HTML')
            return
        
        amount = parse_bet_amount(' '.join(parts[2:]), user_clan['balance'])
        if amount is None or amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма!", parse_mode='HTML')
            return
        
        if amount > user_clan['balance']:
            bot.send_message(message.chat.id, f"❌ В казне недостаточно средств! Открыто: {format_balance(user_clan['balance'])}", parse_mode='HTML')
            return
        
        # Получаем боецов клана
        members = get_clan_members(user_clan['id'])
        share_per_member = amount // len(members)
        
        if share_per_member == 0:
            bot.send_message(message.chat.id, "❌ Сумма слишком мала для распределения между боецами!", parse_mode='HTML')
            return
        
        # Распределяем деньги
        with get_db_cursor() as cursor:
            cursor.execute('UPDATE clans SET balance = balance - ? WHERE id = ?', (amount, user_clan['id']))
            
            for member in members:
                cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', 
                             (share_per_member, member['user_id']))
        
        bot.send_message(message.chat.id,
                       f"🏆 Распределено {format_balance(amount)} между {len(members)} боецами!\n"
                       f"💵 Каждый получил: {format_balance(share_per_member)}\n"
                       f"🏛 Остаток в казне: {format_balance(user_clan['balance'] - amount)}", parse_mode='HTML')
        
    except Exception as e:
        print(f"Ошибка при распределении денег: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при распределении денег!", parse_mode='HTML')
        
@bot.message_handler(func=lambda message: message.text.lower().startswith('клан улучшить'))
def handle_clan_upgrade(message):
    user_id = message.from_user.id
    user_clan = get_user_clan(user_id)
    
    if not user_clan:
        bot.send_message(message.chat.id, "❌ Вы не состоите в клане!", parse_mode='HTML')
        return
    
    if user_clan['role'] not in ['leader', 'officer']:
        bot.send_message(message.chat.id, "❌ Только глава и офицеры могут улучшать клан!", parse_mode='HTML')
        return
    
    upgrade_cost = user_clan['level'] * 5000  # 5B за ранг
    
    if user_clan['balance'] < upgrade_cost:
        bot.send_message(message.chat.id,
                       f"❌ Не хватает монет для улучшения!\n"
                       f"💵 Нужно: {format_balance(upgrade_cost)}\n"
                       f"🏛 В казне: {format_balance(user_clan['balance'])}", parse_mode='HTML')
        return
    
    clan_upgrade_keyboard = {
        "inline_keyboard": [[
            {"text": "🏆 Улучшить", "callback_data": f"clan_upgrade_confirm_{user_clan['id']}", "style": "success"},
            {"text": "❌ Отмена", "callback_data": "clan_upgrade_cancel", "style": "secondary"}
        ]]
    }
    
    bot.send_message(message.chat.id,
                   f"🏰 <b>Улучшение клана</b>\n\n"
                   f"<blockquote>📈 Текущий ранг: {user_clan['level']}\n"
                   f"📈 Новый ранг: {user_clan['level'] + 1}\n"
                   f"💵 Стоимость: {format_balance(upgrade_cost)}\n"
                   f"🏛 В казне: {format_balance(user_clan['balance'])}</blockquote>\n\n"
                   f"Увеличивает максимальное количество боецов и престиж клана!",
                   reply_markup=clan_upgrade_keyboard, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith('clan_upgrade_confirm_'))
def confirm_clan_upgrade(call):
    user_id = call.from_user.id
    clan_id = int(call.data.split('_')[3])
    
    user_clan = get_user_clan(user_id)
    if not user_clan or user_clan['id'] != clan_id:
        bot.answer_callback_query(call.id, "❌ Ошибка доступа!")
        return
    
    upgrade_cost = user_clan['level'] * 5000
    
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE clans SET level = level + 1, balance = balance - ?, max_members = max_members + 5 WHERE id = ?',
                      (upgrade_cost, clan_id))
    
    bot.edit_message_text(f"🎉 Клан улучшен до уровня {user_clan['level'] + 1}!\n"
                         f"👥 Максимум боецов: {user_clan['max_members'] + 5}",
                         call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id, "🏆 Клан улучшен!")
# Функции для аукционов
def create_auction(title, description, created_by, duration_hours):
    """Создать новый аукцион"""
    with get_db_cursor() as cursor:
        ends_at = time.time() + (duration_hours * 3600)
        
        cursor.execute('''
            INSERT INTO auctions (title, description, created_by, ends_at)
            VALUES (?, ?, ?, ?)
        ''', (title, description, created_by, ends_at))
        
        return cursor.lastrowid

def place_bid(auction_id, user_id, bid_amount):
    """Сделать ставку на аукционе - СУММИРОВАНИЕ СТАВОК"""
    with get_db_cursor() as cursor:
        # Проверяем существующий аукцион
        cursor.execute('SELECT status, ends_at FROM auctions WHERE id = ?', (auction_id,))
        auction = cursor.fetchone()
        
        if not auction:
            return False, "Аукцион не найден!"
        
        status, ends_at = auction
        if status != 'active':
            return False, "Аукцион окончен!"
        
        if time.time() > ends_at:
            return False, "Время аукциона истекло!"
        
        # Проверяем баланс
        balance = get_balance(user_id)
        if balance < bid_amount:
            return False, f"❌ Не хватает монет! Нужно: {format_balance(bid_amount)}, у вас: {format_balance(balance)}"
        
        # Получаем ВСЕ ставки пользователя на этом аукционе
        cursor.execute('''
            SELECT SUM(bid_amount) FROM auction_bids WHERE auction_id = ? AND user_id = ?
        ''', (auction_id, user_id))
        
        user_total_bid = cursor.fetchone()[0] or 0
        
        # Получаем максимальную ставку среди всех боецов
        cursor.execute('''
            SELECT user_id, SUM(bid_amount) as total_bid
            FROM auction_bids 
            WHERE auction_id = ? 
            GROUP BY user_id 
            ORDER BY total_bid DESC 
            LIMIT 1
        ''', (auction_id,))
        
        top_bidder = cursor.fetchone()
        max_bid = top_bidder[1] if top_bidder else 0
        top_user_id = top_bidder[0] if top_bidder else None
        
        # Новая общая бет пользователя
        new_total_bid = user_total_bid + bid_amount
        
        # Проверяем становится ли игрок главаом
        if top_user_id == user_id:
            # Пользователь уже глава - просто увеличиваем ставку
            is_leader = True
            needed_to_win = 0
        else:
            # Пользователь не глава - проверяем обгоняет ли он текущего главаа
            is_leader = new_total_bid > max_bid
            needed_to_win = (max_bid - user_total_bid) + 1 if user_total_bid < max_bid else 0
        
        # Списываем деньги
        cursor.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (bid_amount, user_id))
        
        # Добавляем новую ставку (не заменяем старые!)
        cursor.execute('''
            INSERT INTO auction_bids (auction_id, user_id, bid_amount)
            VALUES (?, ?, ?)
        ''', (auction_id, user_id, bid_amount))
        
        # Формируем сообщение
        if is_leader:
            message = f"🏆 Добавлено {format_balance(bid_amount)}!\n🏆 Вы глава с общей ставкой: {format_balance(new_total_bid)}"
        else:
            message = f"🏆 Добавлено {format_balance(bid_amount)}!\n📊 Твоя общая бет: {format_balance(new_total_bid)}\n⚔️ До главаства нужно добавить: {format_balance(needed_to_win)}"
        
        return True, message

def get_user_auction_bid(auction_id, user_id):
    """Получить общую ставку пользователя на аукционе"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT SUM(bid_amount) FROM auction_bids 
            WHERE auction_id = ? AND user_id = ?
        ''', (auction_id, user_id))
        
        result = cursor.fetchone()
        return result[0] if result and result[0] else 0

def get_auction_info(auction_id):
    """Получить информацию об аукционе - ИСПРАВЛЕННАЯ ВЕРСИЯ"""
    with get_db_cursor() as cursor:
        # Основная информация об аукционе
        cursor.execute('''
            SELECT a.*, u.username as creator_name
            FROM auctions a
            LEFT JOIN users u ON a.created_by = u.user_id
            WHERE a.id = ?
        ''', (auction_id,))
        
        result = cursor.fetchone()
        if not result:
            return None
        
        auction = dict(result)
        
        # Получаем количество боецов
        cursor.execute('SELECT COUNT(DISTINCT user_id) FROM auction_bids WHERE auction_id = ?', (auction_id,))
        auction['bidders_count'] = cursor.fetchone()[0] or 0
        
        # Получаем общее количество ставок
        cursor.execute('SELECT COUNT(*) FROM auction_bids WHERE auction_id = ?', (auction_id,))
        auction['bids_count'] = cursor.fetchone()[0] or 0
        
        # Получаем текущую максимальную ставку и главаа
        cursor.execute('''
            SELECT user_id, SUM(bid_amount) as total_bid
            FROM auction_bids 
            WHERE auction_id = ? 
            GROUP BY user_id 
            ORDER BY total_bid DESC 
            LIMIT 1
        ''', (auction_id,))
        
        leader = cursor.fetchone()
        if leader:
            auction['leader_id'] = leader[0]
            auction['current_bid'] = leader[1]
        else:
            auction['leader_id'] = None
            auction['current_bid'] = 0
        
        return auction
def get_auction_bids(auction_id, limit=10):
    """Получить топ ставок аукциона"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT u.user_id, u.username, u.first_name, u.custom_name, 
                   SUM(ab.bid_amount) as total_bid,
                   COUNT(ab.id) as bids_count
            FROM auction_bids ab
            JOIN users u ON ab.user_id = u.user_id
            WHERE ab.auction_id = ?
            GROUP BY u.user_id
            ORDER BY total_bid DESC
            LIMIT ?
        ''', (auction_id, limit))
        
        bids = []
        for row in cursor.fetchall():
            user_link = get_user_link(row[0], row[1], row[2], row[3])
            bids.append({
                'user_id': row[0],
                'user_display': user_link,
                'total_amount': row[4],
                'bids_count': row[5]
            })
        return bids

def get_active_auctions():
    """Получить активные аукционы - УПРОЩЕННАЯ ВЕРСИЯ"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT a.*, u.username as creator_name
            FROM auctions a
            LEFT JOIN users u ON a.created_by = u.user_id
            WHERE a.status = 'active' AND a.ends_at > ?
            ORDER BY a.ends_at ASC
        ''', (time.time(),))
        
        auctions = []
        for row in cursor.fetchall():
            auction = dict(row)
            
            # Получаем дополнительную информацию для каждого аукциона
            cursor.execute('SELECT COUNT(DISTINCT user_id) FROM auction_bids WHERE auction_id = ?', (auction['id'],))
            auction['bidders_count'] = cursor.fetchone()[0] or 0
            
            cursor.execute('SELECT COUNT(*) FROM auction_bids WHERE auction_id = ?', (auction['id'],))
            auction['bids_count'] = cursor.fetchone()[0] or 0
            
            cursor.execute('''
                SELECT SUM(bid_amount) as total_bid
                FROM auction_bids 
                WHERE auction_id = ? 
                GROUP BY user_id 
                ORDER BY total_bid DESC 
                LIMIT 1
            ''', (auction['id'],))
            
            max_bid = cursor.fetchone()
            auction['current_bid'] = max_bid[0] if max_bid else 0
            
            auctions.append(auction)
        
        return auctions
def delete_auction(auction_id, deleted_by):
    """Удалить аукцион и вернуть все ставки"""
    with get_db_cursor() as cursor:
        # Проверяем существование аукциона
        cursor.execute('SELECT status, created_by FROM auctions WHERE id = ?', (auction_id,))
        auction = cursor.fetchone()
        
        if not auction:
            return False, "Аукцион не найден!"
        
        status, created_by = auction
        
        # Проверяем права (только создатель или админ)
        if deleted_by != created_by and not is_admin(deleted_by):
            return False, "❌ Вы можете удалять только свои аукционы!"
        
        # Получаем все ставки для возврата денег
        cursor.execute('''
            SELECT user_id, SUM(bid_amount) as total_bid
            FROM auction_bids 
            WHERE auction_id = ?
            GROUP BY user_id
        ''', (auction_id,))
        
        all_bids = cursor.fetchall()
        
        # Возвращаем деньги всем боецам
        total_returned = 0
        participants_count = 0
        
        for user_id, bid_amount in all_bids:
            cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', 
                          (bid_amount, user_id))
            total_returned += bid_amount
            participants_count += 1
        
        # Удаляем ставки
        cursor.execute('DELETE FROM auction_bids WHERE auction_id = ?', (auction_id,))
        
        # Удаляем аукцион
        cursor.execute('DELETE FROM auctions WHERE id = ?', (auction_id,))
        
        return True, {
            'total_returned': total_returned,
            'participants_count': participants_count,
            'bids_count': len(all_bids)
        }
def complete_auction(auction_id):
    """Завершить аукцион и определить победителя"""
    with get_db_cursor() as cursor:
        # Получаем победителя (по СУММЕ всех его ставок)
        cursor.execute('''
            SELECT user_id, SUM(bid_amount) as total_bid
            FROM auction_bids 
            WHERE auction_id = ? 
            GROUP BY user_id 
            ORDER BY total_bid DESC 
            LIMIT 1
        ''', (auction_id,))
        
        winner = cursor.fetchone()
        
        if winner:
            winner_id, winner_bid = winner
            
            # Обновляем аукцион
            cursor.execute('''
                UPDATE auctions 
                SET status = 'completed', winner_id = ?, winner_bid = ?
                WHERE id = ?
            ''', (winner_id, winner_bid, auction_id))
            
            # Возвращаем деньги всем боецам кроме победителя
            cursor.execute('''
                SELECT user_id, SUM(bid_amount) as total_bid
                FROM auction_bids 
                WHERE auction_id = ? AND user_id != ?
                GROUP BY user_id
            ''', (auction_id, winner_id))
            
            for user_id, bid_amount in cursor.fetchall():
                cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', 
                              (bid_amount, user_id))
            
            return winner_id, winner_bid
        else:
            # Нет ставок - возвращаем всем
            cursor.execute('''
                SELECT user_id, SUM(bid_amount) as total_bid 
                FROM auction_bids 
                WHERE auction_id = ?
                GROUP BY user_id
            ''', (auction_id,))
            
            for user_id, bid_amount in cursor.fetchall():
                cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', 
                              (bid_amount, user_id))
            
            cursor.execute('UPDATE auctions SET status = "completed" WHERE id = ?', (auction_id,))
            return None, 0
# ДУЭЛИ КОНФИГУРАЦИЯ
DUEL_CONFIG = {
    "min_bet": 1,  # 1M минимальная бет
    "max_bet": 100000000000,  # 100B максимальная бет
    "weapons": [
        {"name": "🔫 Пистолет", "accuracy": 70, "critical": 10, "price_multiplier": 1.0},
        {"name": "🏹 Лук", "accuracy": 60, "critical": 20, "price_multiplier": 1.2},
        {"name": "⚔️ Меч", "accuracy": 80, "critical": 5, "price_multiplier": 1.5},
        {"name": "🔪 Кинжал", "accuracy": 75, "critical": 15, "price_multiplier": 1.3},
        {"name": "💣 Граната", "accuracy": 50, "critical": 30, "price_multiplier": 2.0},
        {"name": "🪄 Магический посох", "accuracy": 65, "critical": 25, "price_multiplier": 1.8}
    ],
    "team_sizes": [1, 2, 3, 5],
    "duel_duration": 300  # 5м на принятие дуэли
}

# Хранилище активных дуэлей
active_duels = {}

class Duel:
    def __init__(self, challenger_id, bet_amount, team_size, weapon_type, chat_id):
        self.challenger_id = challenger_id
        self.bet_amount = bet_amount
        self.team_size = team_size
        self.weapon_type = weapon_type
        self.challenger_team = [challenger_id]
        self.opponent_team = []
        self.status = "waiting"
        self.created_at = time.time()
        self.chat_id = chat_id  # ID чата где создана дуэль
        self.message_id = None  # ID сообщения с дуэлью
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

# Обработчик создания дуэли в чате
@bot.message_handler(func=lambda message: message.text.lower().startswith('дуэль ') and message.chat.type in ['group', 'supergroup'])
def handle_duel_create(message):
    user_id = message.from_user.id
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
        bot.send_message(message.chat.id, "Ошибка при создании дуэли!")

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

# Обработчик кнопок в чате
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
        
        # Отправляем ЛС с инструкцией
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
    
    # Уведомляем в чате
    bot.send_message(duel.chat_id, 
                   "⚔️ Дуэль начинается! Все боеци получат ЛС с деталями боя.", parse_mode='HTML')
    
    # Отправляем ЛС всем боецам
    all_players = duel.challenger_team + duel.opponent_team
    
    for player_id in all_players:
        try:
            # Определяем команду игрока
            team = "Команда 1" if player_id in duel.challenger_team else "Команда 2"
            teammates = duel.challenger_team if player_id in duel.challenger_team else duel.opponent_team
            opponents = duel.opponent_team if player_id in duel.challenger_team else duel.challenger_team
            
            # Получаем имена айтемищей по команде
            teammate_names = []
            for teammate_id in teammates:
                if teammate_id != player_id:
                    user_info = get_user_info(teammate_id)
                    name = user_info['custom_name'] if user_info['custom_name'] else (
                        f"@{user_info['username']}" if user_info['username'] else user_info['first_name']
                    )
                    teammate_names.append(name)
            
            # Получаем имена противников
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
    
    # Запускаем бой через 5с
    time.sleep(5)
    execute_duel(duel)

def execute_duel(duel):
    # Симуляция боя
    challenger_power = sum([calculate_user_power(user_id) for user_id in duel.challenger_team])
    opponent_power = sum([calculate_user_power(user_id) for user_id in duel.opponent_team])
    
    total_power = challenger_power + opponent_power
    challenger_win_chance = (challenger_power / total_power) * 100
    
    # Добавляем случайность
    random_factor = random.uniform(0.8, 1.2)
    challenger_win_chance *= random_factor
    
    # Проверяем победу
    roll = random.uniform(0, 100)
    challenger_wins = roll <= challenger_win_chance
    
    winning_team = duel.challenger_team if challenger_wins else duel.opponent_team
    losing_team = duel.opponent_team if challenger_wins else duel.challenger_team
    
    # Выигрыш = бет × 2 (проигравшие теряют, победители получают обратно + выигрыш)
    win_per_player = duel.bet_amount * 2
    
    # Распределяем выигрыш
    for winner_id in winning_team:
        update_balance(winner_id, win_per_player)
    
    # Формируем результаты
    winner_names = []
    for user_id in winning_team:
        user_info = get_user_info(user_id)
        name = user_info['custom_name'] if user_info['custom_name'] else (
            f"@{user_info['username']}" if user_info['username'] else user_info['first_name']
        )
        winner_names.append(name)
    
    # Отправляем результаты в чат
    result_text = "⚔️ РЕЗУЛЬТАТЫ ДУЭЛИ ⚔️\n\n"
    result_text += f"ПОБЕДИЛА КОМАНДА {'1' if challenger_wins else '2'}!\n\n"
    result_text += f"Победители:\n" + "\n".join([f"• {name}" for name in winner_names]) + "\n\n"
    result_text += f"Каждый победитель получает: {format_balance(win_per_player)}\n"
    result_text += f"Общий выигрыш: {format_balance(win_per_player * len(winning_team))}"
    
    bot.send_message(duel.chat_id, result_text)
    
    # Отправляем ЛС с результатами
    for player_id in winning_team + losing_team:
        try:
            player_result_text = "⚔️ РЕЗУЛЬТАТЫ ДУЭЛИ ⚔️\n\n"
            
            if player_id in winning_team:
                player_result_text += "🎉 ВАША КОМАНДА ПОБЕДИЛА!\n\n"
                player_result_text += f"Вы получаете: {format_balance(win_per_player)}\n"
                player_result_text += f"Твой баланс: {format_balance(get_balance(player_id))}"
            else:
                player_result_text += "😔 Твоя команда проиграла\n\n"
                player_result_text += f"Вы потеряли ставку: {format_balance(duel.bet_amount)}\n"
                player_result_text += f"Твой баланс: {format_balance(get_balance(player_id))}"
            
            bot.send_message(player_id, player_result_text)
        except:
            pass
    
    # Удаляем дуэль из активных
    if duel.duel_id in active_duels:
        del active_duels[duel.duel_id]

def calculate_user_power(user_id):
    user_info = get_user_info(user_id)
    # Мощность игрока = баланс / 1M + очки / 1000
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

# Очистка просроченных дуэлей
def cleanup_expired_duels():
    current_time = time.time()
    expired_duels = []
    
    for duel_id, duel in active_duels.items():
        if current_time - duel.created_at > DUEL_CONFIG["duel_duration"]:
            expired_duels.append(duel_id)
    
    for duel_id in expired_duels:
        duel = active_duels[duel_id]
        
        # Возвращаем деньги всем боецам
        all_players = duel.challenger_team + duel.opponent_team
        for player_id in all_players:
            update_balance(player_id, duel.bet_amount)
        
        # Уведомляем в чате
        bot.send_message(duel.chat_id, 
                       f"⚔️ Дуэль истекла! Деньги возвращены всем боецам.", parse_mode='HTML')
        
        del active_duels[duel_id]

# Запускаем очистку каждуюму
def start_duel_cleanup():
    while True:
        try:
            cleanup_expired_duels()
        except Exception as e:
            print(f"Ошибка очистки дуэлей: {e}")
        time.sleep(60)

# Запускаем в отдельном потоке
import threading
cleanup_thread = threading.Thread(target=start_duel_cleanup, daemon=True)
cleanup_thread.start()
# Админ команда удаления аукциона
@bot.message_handler(func=lambda message: message.text.lower().startswith('удалить аук') and is_admin(message.from_user.id))
def handle_delete_auction(message):
    """Удалить аукцион"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 3:
            bot.send_message(message.chat.id, 
                           "❌ Используйте: удалить аук [id]\n\n"
                           "Пример:\n"
                           "удалить аук 1", parse_mode='HTML')
            return
        
        auction_id = int(parts[2])
        
        # Создаем кнопку подтверждения с цветами
        del_auc_keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Да, удалить", "callback_data": f"confirm_delete_auction_{auction_id}", "style": "danger"},
                {"text": "❌ Отмена", "callback_data": "cancel_delete_auction", "style": "secondary"}
            ]]
        }
        
        # Получаем информацию об аукционе для подтверждения
        auction = get_auction_info(auction_id)
        if not auction:
            bot.send_message(message.chat.id, "❌ Аукцион не найден!", parse_mode='HTML')
            return
        
        bot.send_message(message.chat.id,
                       f"🗑️ <b>ПОДТВЕРЖДЕНИЕ УДАЛЕНИЯ</b>\n\n"
                       f"<blockquote>🏷️ Название: {auction['title']}\n"
                       f"🆔 ID: {auction_id}\n"
                       f"💵 Текущая бет: {format_balance(auction['current_bid'] or 0)}\n"
                       f"👥 Участников: {auction['bidders_count']}\n"
                       f"⚔️ Ставок: {auction['bids_count']}</blockquote>\n\n"
                       f"⚠️ <b>Все ставки будут возвращены боецам!</b>\n"
                       f"Подтвердить удаление?",
                       reply_markup=del_auc_keyboard,
                       parse_mode='HTML')
    
    except ValueError:
        bot.send_message(message.chat.id, "❌ Неверный формат ID!", parse_mode='HTML')
    except Exception as e:
        print(f"Ошибка удаления аукциона: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при удалении аукциона!", parse_mode='HTML')
@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_delete_auction_'))
def confirm_delete_auction(call):
    """Подтверждение удаления аукциона"""
    user_id = call.from_user.id
    
    if not is_admin(user_id):
        bot.answer_callback_query(call.id, "❌ Недостаточно прав!")
        return
    
    try:
        auction_id = int(call.data.split('_')[3])
        
        # Удаляем аукцион
        success, result = delete_auction(auction_id, user_id)
        
        if success:
            bot.edit_message_text(
                f"🏆 Аукцион удален!\n\n"
                f"💵 Возвращено: {format_balance(result['total_returned'])}\n"
                f"👥 Участников: {result['participants_count']}\n"
                f"⚔️ Ставок: {result['bids_count']}",
                call.message.chat.id,
                call.message.message_id
            , parse_mode='HTML')
            bot.answer_callback_query(call.id, "🏆 Аукцион удален!")
        else:
            bot.edit_message_text(
                f"❌ {result}",
                call.message.chat.id,
                call.message.message_id
            , parse_mode='HTML')
            bot.answer_callback_query(call.id, "❌ Ошибка!")
    
    except Exception as e:
        print(f"Ошибка подтверждения удаления: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка при удалении!")

@bot.callback_query_handler(func=lambda call: call.data == "cancel_delete_auction")
def cancel_delete_auction(call):
    """Отмена удаления аукциона"""
    bot.edit_message_text(
        "🏆 Удаление сброшено",
        call.message.chat.id,
        call.message.message_id
    , parse_mode='HTML')
    bot.answer_callback_query(call.id, "🏆 Отменено")
# Админ команда создания аукциона
@bot.message_handler(func=lambda message: message.text.lower().startswith('создать аук') and is_admin(message.from_user.id))
def handle_create_auction(message):
    """Создать новый аукцион"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split(' ', 3)
        if len(parts) < 4:
            bot.send_message(message.chat.id,
                           "❌ Используйте: создать аук [время_часы] [название] | [описание]\n\n"
                           "Пример:\n"
                           "создать аук 24 Крутой предмет | Очень редкий и ценный предмет", parse_mode='HTML')
            return
        
        duration_hours = int(parts[2])
        title_desc = parts[3].split('|', 1)
        title = title_desc[0].strip()
        description = title_desc[1].strip() if len(title_desc) > 1 else ""
        
        if duration_hours < 1 or duration_hours > 168:
            bot.send_message(message.chat.id, "❌ Время должно быть от 1 до 168ч (7д)", parse_mode='HTML')
            return
        
        auction_id = create_auction(title, description, message.from_user.id, duration_hours)
        
        ends_at = time.time() + (duration_hours * 3600)
        ends_time = datetime.fromtimestamp(ends_at).strftime("%d.%m.%Y %H:%M")
        
        bot.send_message(message.chat.id,
                       f"🎉 Аукцион создан!\n\n"
                       f"🏷️ Название: {title}\n"
                       f"📝 Описание: {description}\n"
                       f"⏰ Завершится: {ends_time}\n"
                       f"🆔 ID: {auction_id}\n\n"
                       f"💬 Участники могут делать ставки командой:\n"
                       f"<code>аук {auction_id} [сумма]</code>",
                       parse_mode='HTML')
        
    except Exception as e:
        print(f"Ошибка создания аукциона: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при создании аукциона!", parse_mode='HTML')

# Команда для ставок
@bot.message_handler(func=lambda message: message.text.lower().startswith('аук '))
def handle_auction_bid(message):
    """Сделать ставку на аукционе"""
    user_id = message.from_user.id
    
    try:
        parts = message.text.split()
        if len(parts) < 3:
            show_auction_help(message.chat.id)
            return
        
        auction_id = int(parts[1])
        bid_amount = parse_bet_amount(parts[2], get_balance(user_id))
        
        if bid_amount is None or bid_amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки!", parse_mode='HTML')
            return
        
        success, result_message = place_bid(auction_id, user_id, bid_amount)
        bot.send_message(message.chat.id, result_message)
        
        if success:
            # Показываем обновленную информацию об аукционе
            show_auction_info(message.chat.id, auction_id, user_id)
    
    except ValueError:
        show_auction_help(message.chat.id)
    except Exception as e:
        print(f"Ошибка ставки: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при выполнении ставки!", parse_mode='HTML')

# Команда просмотра аукциона
@bot.message_handler(func=lambda message: message.text.lower().startswith('аук'))
def handle_auction_info(message):
    try:
        parts = message.text.split()
        
        if len(parts) == 1:
            show_active_auctions(message.chat.id)
        elif len(parts) == 2:
            auction_id = int(parts[1])
            show_auction_info(message.chat.id, auction_id, message.from_user.id)
        elif len(parts) >= 3:
            auction_id = int(parts[1])
            bet_text = ' '.join(parts[2:])
            handle_auction_bid(message, auction_id, bet_text)
        else:
            show_auction_help(message.chat.id)
            
    except ValueError:
        show_auction_help(message.chat.id)
    except Exception as e:
        bot.send_message(message.chat.id, "❌ Ошибка", parse_mode='HTML')

def show_auction_help(chat_id):
    help_text = "⚔️ <b>АУКЦИОН</b>\n\n"
    help_text += "📋 <b>аук</b> - все аукционы\n"
    help_text += "📋 <b>аук [id]</b> - информация\n"
    help_text += "💵 <b>аук [id] [сумма]</b> - бет\n\n"
    help_text += "💡 Ставки суммируются"
    
    bot.send_message(chat_id, help_text, parse_mode='HTML')

def show_active_auctions(chat_id):
    auctions = get_active_auctions()
    
    if not auctions:
        bot.send_message(chat_id, "📭 Нет активных")
        return
    
    message_text = "⚔️ <b>АУКЦИОНЫ</b>\n\n"
    
    for auction in auctions:
        time_left = auction['ends_at'] - time.time()
        if time_left <= 0:
            continue
            
        hours_left = int(time_left // 3600)
        minutes_left = int((time_left % 3600) // 60)
        
        message_text += f"🏷️ {auction['title']}\n"
        message_text += f"🆔 {auction['id']}\n"
        message_text += f"💵 {format_balance(auction['current_bid'] or 0)}\n"
        message_text += f"👥 {auction['bidders_count']}\n"
        message_text += f"⏰ {hours_left}ч {minutes_left}м\n"
        message_text += f"💬 <code>аук {auction['id']}</code>\n\n"
    
    bot.send_message(chat_id, message_text, parse_mode='HTML')

def show_auction_info(chat_id, auction_id, user_id=None):
    auction = get_auction_info(auction_id)
    
    if not auction:
        bot.send_message(chat_id, "❌ Не найден", parse_mode='HTML')
        return
    
    time_left = auction['ends_at'] - time.time()
    
    if time_left <= 0:
        status = "🏁 ЗАВЕРШЕН"
    else:
        hours_left = int(time_left // 3600)
        minutes_left = int((time_left % 3600) // 60)
        status = f"⏰ {hours_left}ч {minutes_left}м"
    
    message_text = f"⚔️ <b>{auction['title']}</b>\n\n"
    if auction['description']:
        message_text += f"{auction['description']}\n\n"
    message_text += f"💵 {format_balance(auction['current_bid'] or 0)}\n"
    message_text += f"👥 {auction['bidders_count']} боецов\n"
    message_text += f"⚔️ {auction['bids_count']} ставок\n"
    message_text += f"⏰ {status}\n\n"
    
    # Ставка пользователя
    if user_id:
        user_bid = get_user_auction_bid(auction_id, user_id)
        if user_bid > 0:
            message_text += f"💳 Твой бет: {format_balance(user_bid)}\n\n"
    
    # Топ боецов
    bids = get_auction_bids(auction_id, 5)
    if bids:
        message_text += "🏆 <b>ТОП:</b>\n"
        for i, bid in enumerate(bids, 1):
            message_text += f"{i}. {bid['user_display']} - {format_balance(bid['total_amount'])}\n"
    
    message_text += f"\n💸 <code>аук {auction_id} [сумма]</code>"
    
    bot.send_message(chat_id, message_text, parse_mode='HTML')

def handle_auction_bid(message, auction_id, bet_text):
    """Обработчик ставки на аукционе"""
    user_id = message.from_user.id
    auction = get_auction_info(auction_id)
    
    if not auction:
        bot.reply_to(message, "❌ Аукцион отсутствует", parse_mode='HTML')
        return
    
    time_left = auction['ends_at'] - time.time()
    if time_left <= 0:
        bot.reply_to(message, "⏰ Аукцион окончен")
        return
    
    # Парсим сумму ставки
    balance = get_balance(user_id)
    amount = parse_bet_amount(bet_text, balance)
    
    if not amount or amount <= 0:
        bot.reply_to(message, "❌ Неверная сумма", parse_mode='HTML')
        return
    
    if amount > balance:
        bot.reply_to(message, f"❌ Не хватает монет\n💳 {format_balance(balance)}", parse_mode='HTML')
        return
    
    # Минимальная бет - 100М
    min_bid = 100000000
    if amount < min_bid:
        bot.reply_to(message, f"❌ Минимум {format_balance(min_bid)}", parse_mode='HTML')
        return
    
    # Делаем ставку
    success, msg = place_auction_bid(user_id, auction_id, amount)
    
    if success:
        new_balance = get_balance(user_id)
        bot.reply_to(message, f"🏆 {msg}\n💳 Осталось: {format_balance(new_balance)}", parse_mode='HTML')
    else:
        bot.reply_to(message, f"❌ {msg}", parse_mode='HTML')
    
# Глобальные переменные для бонусов
active_bonus_posts = {}
bonus_handlers = {}

# Админ-команда для создания бонус-поста
@bot.message_handler(func=lambda message: message.text.lower() == 'бонуска' and is_admin(message.from_user.id))
def handle_create_bonus(message):
    """Создать пост с бонусом в канале"""
    if not is_admin(message.from_user.id):
        return
    
    # ID канала (замени на свой)
    CHANNEL_ID = "@FECTIZ"  # Например: "@Netron_channel" или "-100123456789"
    
    # Генерируем случайный бонус для примера (каждый получит свой рандомный)
    example_bonus = random.randint(100, 70000)
    expires_at = time.time() + 1600  # 10м
    
    # Создаем уникальный ID для бонуса
    bonus_id = f"bonus_{int(time.time())}_{random.randint(1000, 9999)}"
    
    # Сохраняем бонус
    active_bonus_posts[bonus_id] = {
        'amount': example_bonus,  # Только для примера в тексте
        'expires_at': expires_at,
        'created_by': message.from_user.id,
        'claimed_by': set(),
        'created_at': time.time(),
        'channel_id': CHANNEL_ID
    }
    
    # Создаем индивидуальный обработчик для этого бонуса
    def create_bonus_handler(bonus_id):
        @bot.callback_query_handler(func=lambda call: call.data == f"claim_bonus_{bonus_id}")
        def handle_specific_bonus(call):
            user_id = call.from_user.id
            
            # Проверяем существование бонуса
            if bonus_id not in active_bonus_posts:
                bot.answer_callback_query(call.id, "❌ Бонус уже закончился!", show_alert=True)
                return
            
            bonus_data = active_bonus_posts[bonus_id]
            
            # Проверяем время
            if time.time() > bonus_data['expires_at']:
                bot.answer_callback_query(call.id, "❌ Время получения бонуса истекло!", show_alert=True)
                del active_bonus_posts[bonus_id]
                if bonus_id in bonus_handlers:
                    del bonus_handlers[bonus_id]
                return
            
            # Проверяем, не получал ли игрок уже этот бонус
            if user_id in bonus_data['claimed_by']:
                bot.answer_callback_query(call.id, "❌ Вы уже получили этот бонус!", show_alert=True)
                return
            
            # Генерируем РАНДОМНУЮ сумму для каждого пользователя
            user_bonus_amount = random.randint(1000, 70000)  # 1ккк - 50ккк
            
            # Начисляем бонус
            update_balance(user_id, user_bonus_amount)
            
            # Добавляем пользователя в список получивших
            bonus_data['claimed_by'].add(user_id)
            
            # Получаем информацию о пользователе
            user_info = get_user_info(user_id)
            user_name = user_info['custom_name'] if user_info and user_info['custom_name'] else call.from_user.first_name
            
            # Отправляем уведомление пользователю
            try:
                bot.send_message(
                    user_id,
                    f"🎉 <b>Вы зачислен подарок!</b>\n\n"
                    f"💵 <b>Сумма:</b> {format_balance(user_bonus_amount)}\n"
                    f"🎴 <b>Получатель:</b> {user_name}",
                    parse_mode='HTML'
                )
            except:
                pass
            
            bot.answer_callback_query(
                call.id, 
                f"🎁 Бонус получен! {format_balance(user_bonus_amount)}", 
                show_alert=True
            )
            
            print(f"🏆 Пользователь {user_id} получил бонус: {format_balance(user_bonus_amount)}")
        
        return handle_specific_bonus
    
    # Регистрируем обработчик для этого бонуса
    bonus_handler = create_bonus_handler(bonus_id)
    bonus_handlers[bonus_id] = bonus_handler
    
    # Стильный текст поста
    bonus_text = f"""🎁 <b>бонус пост!</b>


⏰ <b>Успей за 10м!</b>"""


    # Создаем инлайн-кнопку
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(
        "🎁 Забрать", 
        callback_data=f"claim_bonus_{bonus_id}"
    ))
    
    # Отправляем пост в КАНАЛ
    try:
        sent_message = bot.send_message(
            CHANNEL_ID,  # Отправляем в канал, а не в ЛС
            bonus_text,
            reply_markup=markup,
            parse_mode='HTML'
        )
        
        bot.reply_to(message, f"🏆 Бонус-пост создан в канале!\n💵 Пример: {format_balance(example_bonus)}", parse_mode='HTML')
        
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка отправки в канал: {e}", parse_mode='HTML')

# Функция для очистки просроченных бонусов и их обработчиков
def cleanup_expired_bonuses():
    """Очистить просроченные бонусы и их обработчики"""
    current_time = time.time()
    expired_bonuses = []
    
    for bonus_id, bonus_data in active_bonus_posts.items():
        if current_time > bonus_data['expires_at']:
            expired_bonuses.append(bonus_id)
    
    for bonus_id in expired_bonuses:
        # Удаляем бонус
        del active_bonus_posts[bonus_id]
        # Удаляем обработчик (хотя он останется в памяти, но не будет вызываться)
        if bonus_id in bonus_handlers:
            del bonus_handlers[bonus_id]
        print(f"🗑️ Удален просроченный бонус {bonus_id}")

# Запускаем очистку каждуюму
def start_bonus_cleanup():
    def cleanup_loop():
        while True:
            try:
                cleanup_expired_bonuses()
                time.sleep(60)  # Проверяем каждуюму
            except Exception as e:
                print(f"❌ Ошибка в cleanup_loop: {e}")
                time.sleep(60)
    
    thread = threading.Thread(target=cleanup_loop, daemon=True)
    thread.start()

# Запускаем очистку при старте бота
start_bonus_cleanup()
# Админ команда окончения аукциона
@bot.message_handler(func=lambda message: message.text.lower().startswith('завершить аук') and is_admin(message.from_user.id))
def handle_complete_auction(message):
    """Завершить аукцион досрочно"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 3:
            bot.send_message(message.chat.id, "❌ Используйте: завершить аук [id]", parse_mode='HTML')
            return
        
        auction_id = int(parts[2])
        winner_id, winner_bid = complete_auction(auction_id)
        
        if winner_id:
            # Уведомляем победителя
            try:
                winner_info = get_user_info(winner_id)
                winner_name = winner_info['custom_name'] if winner_info['custom_name'] else (
                    f"@{winner_info['username']}" if winner_info['username'] else winner_info['first_name']
                )
                
                bot.send_message(winner_id,
                               f"🎉 ПОБЕДА НА АУКЦИОНЕ!\n\n"
                               f"🏆 Победа аукцион!\n"
                               f"💵 Твоя общая бет: {format_balance(winner_bid)}\n\n"
                               f"📞 Свяжитесь с админом для получения выигрыша!", parse_mode='HTML')
            except:
                pass
            
            bot.send_message(message.chat.id,
                           f"🏆 Аукцион окончен!\n\n"
                           f"🏆 Победитель: ID {winner_id}\n"
                           f"💵 Выигрышная бет: {format_balance(winner_bid)}\n\n"
                           f"💸 Все средства проигравшим возвращены!\n"
                           f"💬 Победитель уведомлен!", parse_mode='HTML')
        else:
            bot.send_message(message.chat.id,
                           "🏆 Аукцион окончен!\n\n"
                           "📭 Победителей нет - ставок не было\n"
                           "💵 Все средства возвращены боецам", parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка окончения аукциона: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при окончении аукциона!", parse_mode='HTML')

# Функция для автоматической проверки оконченных аукционов
def check_expired_auctions():
    """Проверить и завершить просроченные аукционы"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT id FROM auctions 
            WHERE status = 'active' AND ends_at <= ?
        ''', (time.time(),))
        
        expired_auctions = cursor.fetchall()
        
        for (auction_id,) in expired_auctions:
            print(f"🔄 Завершаем просроченный аукцион {auction_id}")
            winner_id, winner_bid = complete_auction(auction_id)
            
            if winner_id:
                print(f"🏆 Аукцион {auction_id} окончен. Победитель: {winner_id}")
            else:
                print(f"🏆 Аукцион {auction_id} окончен. Победителей нет")
        
        if expired_auctions:
            print(f"🏆 Завершено {len(expired_auctions)} аукционов")
# Админская команда создания чеков с кнопкой (пароль отдельным сообщением)
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
        
        # Проверяем, является ли второй параметр юзернеймом или числом активаций
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
        
        # Генерируем уникальный код чека
        check_code = f"admin{random.randint(100000, 999999)}"
        
        # Сохраняем чек в БД
        with get_db_cursor() as cursor:
            cursor.execute('''
                INSERT INTO checks (code, amount, max_activations, password, created_by, target_username)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (check_code, amount, max_activations, password, message.from_user.id, target_username))
        
        # Создаем ссылку для активации
        bot_username = (bot.get_me()).username
        check_link = f"https://t.me/{bot_username}?start={check_code}"
        
        # Создаем кнопку
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🎫 Активировать чек", url=check_link))
        
        # Формируем сообщение с премиум-эмодзи через HTML
        message_text = f"🎫 <b>Ваучер создан!</b>\n\n"
        message_text += f"💰 Сумма: {format_balance(amount)}\n"
        
        if target_username:
            message_text += f"👤 Для: {target_username}\n"
        else:
            message_text += f"📊 Активаций: {max_activations}\n"
        
        message_text += f"🔑 Пароль: {'есть' if password else 'нет'}"
        
        # Отправляем сообщение с HTML-разметкой
        bot.send_message(message.chat.id, message_text, reply_markup=markup, parse_mode='HTML')
        
        # Отдельным сообщением отправляем пароль если он есть
        if password:
            bot.send_message(message.chat.id, 
                           f"🔑 Пароль от чека: <code>{password}</code>\n\n"
                           f"⚠️ Никому не сообщайте этот пароль!", parse_mode='HTML')
        
    except Exception as e:
        print(f"Ошибка при создании чека: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при создании чека!")

        
# Добавьте эту функцию для расчета возраста пользователя в боте
def get_user_age(user_id):
    """Получить возраст пользователя в боте (в днях)"""
    with get_db_cursor() as cursor:
        cursor.execute('SELECT last_activity FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        if result and result[0]:
            # Если есть запись о последней активности, считаем от нее
            join_date = result[0]
            if isinstance(join_date, str):
                # Если это строка, преобразуем в datetime
                join_date = datetime.fromisoformat(join_date.replace('Z', '+00:00'))
            elif isinstance(join_date, (int, float)):
                # Если это timestamp
                join_date = datetime.fromtimestamp(join_date)
        else:
            # Если нет данных, используем текущее время
            join_date = datetime.now()
        
        age_days = (datetime.now() - join_date).days
        return max(1, age_days)  # Минимум 1 день

# Обработчик команды "профиль"
@bot.message_handler(func=lambda message: message.text.lower() == 'профиль')
def handle_profile(message):
    
    try:
        user_id = message.from_user.id
        user_info = get_user_info(user_id)
        
        display_name = user_info['custom_name'] if user_info['custom_name'] else (f"@{user_info['username']}" if user_info['username'] else user_info['first_name'])
        
        # Уровень и титул
        level, experience = get_user_level(user_id)
        emoji, title_name = get_title(level)
        lvl_cur, lvl_exp, lvl_need = get_level_progress(experience)
        progress_pct = int(lvl_exp / lvl_need * 10) if lvl_need > 0 else 10
        progress_bar = "█" * progress_pct + "░" * (10 - progress_pct)
        
        # Рассчитываем возраст пользователя
        age_days = get_user_age(user_id)
        
        # Рассчитываем бонус за клики
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
        
        # Статистика игр
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
        
        # Информация о бизнесе
        business_info = get_user_business(user_id)
        if business_info:
            message_text += f"\n\n🏭 Предприятие: {business_info['name']}\n"
            message_text += f"📦 Ресурсы: {business_info['raw_materials']}/{business_info['storage_capacity']}\n"
        
        # Информация о клане
        user_clan = get_user_clan(user_id)
        if user_clan:
            message_text += f"\n⚔️ Гильдия: {user_clan['name']} [{user_clan['tag']}]\n"
            message_text += f"🎖 Статус: {user_clan['role']}\n"
        
        bot.send_message(message.chat.id, message_text)
    
    except Exception as e:
        print(f"Ошибка в handle_profile: {e}")
        bot.send_message(message.chat.id, "⚠️ Что-то пошло не так. Попробуй ещё раз.")

# Админ команда для сброса статистики игр
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
        
        # Определяем ID пользователя
        if target.startswith('@'):
            # Поиск по username
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
            # По ID
            try:
                target_user_id = int(target)
            except ValueError:
                bot.reply_to(message, "❌ Неверный формат ID пользователя!", parse_mode='HTML')
                return
        
        # Проверяем существование пользователя
        target_info = get_user_info(target_user_id)
        if not target_info:
            bot.reply_to(message, "❌ Пользователь отсутствует в базе данных!", parse_mode='HTML')
            return
        
        # Сбрасываем статистику
        with get_db_cursor() as cursor:
            cursor.execute('''
                UPDATE users SET 
                games_won = 0,
                games_lost = 0, 
                total_won_amount = 0,
                total_lost_amount = 0
                WHERE user_id = ?
            ''', (target_user_id,))
        
        # Получаем информацию о пользователе для отображения
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

# Админ команда для сброса статистики всех пользователей
@bot.message_handler(func=lambda message: message.text.lower() == 'сбросить всю статистику' and is_admin(message.from_user.id))
def handle_reset_all_stats(message):
    """Сбросить статистику игр всех пользователей"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        # Создаем кнопку подтверждения
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
# Обработчик подтверждения сброса всей статистики
@bot.callback_query_handler(func=lambda call: call.data == "confirm_reset_all_stats")
def confirm_reset_all_stats(call):
    try:
        user_id = call.from_user.id
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "❌ Недостаточно прав!")
            return
        
        # Сбрасываем статистику всех пользователей
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
# Обработчик кнопки "Бизнес"
@bot.message_handler(func=lambda message: message.text == "Бизнеrс")
def handle_business(message):
    user_id = message.from_user.id
    markup = create_business_menu()
    business_info = get_user_business(user_id)
    
    if business_info:
        if business_info['progress'] == 1:
            status = "🏆 Готов"
            time_info = ""
        else:
            current_time = time.time()
            time_passed = current_time - business_info['start_time']
            time_left = business_info['delivery_time'] - time_passed
            
            if time_left > 0:
                hours = int(time_left // 3600)
                minutes = int((time_left % 3600) // 60)
                status = f"⏳ {hours}ч {minutes}м"
                time_info = ""
            else:
                status = "🏆 Готов"
                time_info = ""
        
        message_text = f"🏢 <b>{business_info['name']}</b>\n\n"
        message_text += f"📦 {business_info['raw_materials']}/{business_info['storage_capacity']}\n"
        message_text += f"💵 x{business_info['profit_multiplier']}\n"
        message_text += f"🚚 {status}{time_info}"
        
        if business_info['image_url']:
            try:
                bot.send_photo(message.chat.id, business_info['image_url'], 
                             caption=message_text, reply_markup=markup, parse_mode='HTML')
                return
            except:
                pass
        
        bot.send_message(message.chat.id, message_text, reply_markup=markup, parse_mode='HTML')
    else:
        bot.send_message(message.chat.id, 
                       "🏢 <b>БИЗНЕС</b>\n\n"
                       "У вас нет бизнеса\n"
                       "Купить в магазине",
                       reply_markup=markup, parse_mode='HTML')

# Обработчик кнопки "🏪 Бизнесы"
@bot.message_handler(func=lambda message: message.text == "🏪 Бизнесы")
def handle_business_shop(message):
    user_id = message.from_user.id
    businesses = get_available_businesses()
    
    if not businesses:
        bot.send_message(message.chat.id, "🏢 🛒 Шоп пуст")
        return
    
    message_text = "🏢 <b>МАГАЗИН</b>\n\n"
    
    for biz in businesses:
        message_text += f"🏷️ {biz['name']}\n"
        message_text += f"💵 {format_balance(biz['price'])}\n"
        message_text += f"📦 {biz['storage_capacity']}\n"
        message_text += f"📈 x{biz['profit_multiplier']}\n"
        message_text += f"👥 {biz['current_players']}/{biz['max_players']}\n"
        message_text += f"🛒 /приобрестибизнес_{biz['id']}\n\n"
    
    bot.send_message(message.chat.id, message_text, parse_mode='HTML')

# Обработчик покупки бизнеса
@bot.message_handler(func=lambda message: message.text.startswith('/приобрестибизнес_'))
def handle_buy_business_command(message):
    user_id = message.from_user.id
    
    try:
        business_id = int(message.text.split('_')[1])
        success, result_message = buy_business(user_id, business_id)
        bot.send_message(message.chat.id, result_message)
        
        if success:
            handle_business(message)
    
    except:
        bot.send_message(message.chat.id, "❌ Ошибка", parse_mode='HTML')

# Обработчик "📦 Купить сырьё"
@bot.message_handler(func=lambda message: message.text == "📦 Купить сырьё")
def handle_buy_raw_materials(message):
    user_id = message.from_user.id
    business_info = get_user_business(user_id)
    
    if not business_info:
        bot.send_message(message.chat.id, "❌ Нет бизнеса", parse_mode='HTML')
        return
    
    if business_info['progress'] == 1:
        bot.send_message(message.chat.id, "❌ Сначала соберите доход", parse_mode='HTML')
        return
    
    max_purchase = business_info['price'] // 2
    free_space = business_info['storage_capacity'] - business_info['raw_materials']
    actual_max = min(max_purchase, free_space * 1000000)
    
    msg = bot.send_message(message.chat.id, 
                         f"🏭 <b>ЗАКУПКА</b>\n\n"
                         f"💵 до {format_balance(actual_max)}\n"
                         f"📦 {free_space} свободно\n"
                         f"💡 1 единица = 1M\n\n"
                         f"Введите сумму:", parse_mode='HTML')
    
    bot.register_next_step_handler(msg, process_raw_materials_purchase, user_id, actual_max)

def process_raw_materials_purchase(message, user_id, max_amount):
    try:
        amount = parse_bet_amount(message.text, max_amount)
        
        if not amount or amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма", parse_mode='HTML')
            return
        
        if amount > max_amount:
            bot.send_message(message.chat.id, f"❌ Максимум {format_balance(max_amount)}", parse_mode='HTML')
            return
        
        success, result_message = buy_raw_materials(user_id, amount)
        bot.send_message(message.chat.id, result_message)
        
        if success:
            handle_business(message)
    
    except:
        bot.send_message(message.chat.id, "❌ Ошибка", parse_mode='HTML')

# Обработчик "💵 Собрать доход"
@bot.message_handler(func=lambda message: message.text == "💵 Собрать доход")
def handle_collect_income(message):
    user_id = message.from_user.id
    success, result_message = collect_business_income(user_id)
    bot.send_message(message.chat.id, result_message)
    
    if success:
        handle_business(message)

# Обработчик "Продать бизнес"
@bot.message_handler(func=lambda message: message.text == "Продать бизнес")
def handle_sell_business(message):
    user_id = message.from_user.id
    business_info = get_user_business(user_id)
    
    if not business_info:
        bot.send_message(message.chat.id, "❌ Нет бизнеса", parse_mode='HTML')
        return
    
    sell_price = business_info['price'] // 2
    
    sell_biz_keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Продать", "callback_data": f"confirm_sell_{user_id}", "style": "success"},
            {"text": "❌ Нет", "callback_data": "cancel_sell", "style": "danger"}
        ]]
    }
    
    bot.send_message(message.chat.id,
                   f"🏢 <b>ПРОДАЖА</b>\n\n"
                   f"<blockquote>🆔 {business_info['name']}\n"
                   f"💵 {format_balance(sell_price)}</blockquote>\n\n"
                   f"Продать бизнес?",
                   reply_markup=sell_biz_keyboard, parse_mode='HTML')

# Обработчик "🏭 Мой бизнес"
@bot.message_handler(func=lambda message: message.text == "🏭 Мой бизнес")
def handle_my_business(message):
    handle_business(message)

# Обработчик передачи бизнеса
@bot.message_handler(func=lambda message: message.text.lower().startswith('передать бизнес'))
def handle_transfer_business_command(message):
    user_id = message.from_user.id
    
    if not message.reply_to_message:
        bot.send_message(message.chat.id, "❌ Ответьте на сообщение", parse_mode='HTML')
        return
    
    target_user_id = message.reply_to_message.from_user.id
    
    if target_user_id == user_id:
        bot.send_message(message.chat.id, "❌ Нельзя себе", parse_mode='HTML')
        return
    
    success, result_message = transfer_business(user_id, target_user_id)
    bot.send_message(message.chat.id, result_message)

# Админ команда создания бизнеса
@bot.message_handler(func=lambda message: message.text.lower().startswith('добавить бизнес') and is_admin(message.from_user.id))
def handle_add_business(message):
    if not is_admin(message.from_user.id):
        return
    
    msg = bot.send_message(message.chat.id,
                         "🏢 <b>СОЗДАНИЕ</b>\n\n"
                         "Формат:\n"
                         "Название|Описание|Цена|Игроки|Склад|Множитель|Часы|URL",
                         parse_mode='HTML')
    
    bot.register_next_step_handler(msg, process_business_creation)

def process_business_creation(message):
    try:
        if not is_admin(message.from_user.id):
            return
        
        parts = message.text.split('|')
        if len(parts) != 8:
            bot.send_message(message.chat.id, "❌ 8 параметров", parse_mode='HTML')
            return
        
        name = parts[0].strip()
        description = parts[1].strip()
        price = parse_bet_amount(parts[2].strip(), float('inf'))
        max_players = int(parts[3].strip())
        storage_capacity = int(parts[4].strip())
        profit_multiplier = float(parts[5].strip())
        delivery_time = int(parts[6].strip()) * 3600
        image_url = parts[7].strip()
        
        if not all([name, price, max_players > 0, storage_capacity > 0, profit_multiplier > 0, delivery_time > 0]):
            bot.send_message(message.chat.id, "❌ Все > 0", parse_mode='HTML')
            return
        
        business_id = create_business(name, description, price, max_players, storage_capacity, 
                                    profit_multiplier, delivery_time, image_url, message.from_user.id)
        
        bot.send_message(message.chat.id,
                       f"🏆 Создан\n"
                       f"🆔 {name}\n"
                       f"💵 {format_balance(price)}\n"
                       f"👥 {max_players}\n"
                       f"📦 {storage_capacity}\n"
                       f"📈 x{profit_multiplier}", parse_mode='HTML')
    
    except:
        bot.send_message(message.chat.id, "❌ Ошибка", parse_mode='HTML')

# Обработчики callback
@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_sell_'))
def confirm_sell_callback(call):
    try:
        user_id = int(call.data.split('_')[2])
        
        if call.from_user.id != user_id:
            bot.answer_callback_query(call.id, "❌ Не ваш")
            return
        
        success, result_message = sell_business(user_id)
        bot.edit_message_text(result_message, call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id)
    
    except:
        bot.answer_callback_query(call.id, "❌ Ошибка")

@bot.callback_query_handler(func=lambda call: call.data == 'cancel_sell')
def cancel_sell_callback(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id, "🏆")

# Обработчик "Назад"
@bot.message_handler(func=lambda message: message.text in ["🔙 Назад", "Назад"])
def handle_back_from_business(message):
    markup = create_main_menu()
    bot.send_message(message.chat.id, "Главное меню:", reply_markup=markup)

# Функции бизнеса (оставляем без изменений)
def get_user_business(user_id):
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT b.id, b.name, b.description, b.price, b.storage_capacity, 
                   b.profit_multiplier, b.delivery_time, b.image_url,
                   u.business_progress, u.business_start_time, u.business_raw_materials
            FROM users u
            LEFT JOIN businesses b ON u.business_id = b.id
            WHERE u.user_id = ?
        ''', (user_id,))
        
        result = cursor.fetchone()
        
        if result and result[0]:
            return {
                'id': result[0],
                'name': result[1],
                'description': result[2],
                'price': result[3],
                'storage_capacity': result[4],
                'profit_multiplier': result[5],
                'delivery_time': result[6],
                'image_url': result[7],
                'progress': result[8],
                'start_time': result[9],
                'raw_materials': result[10]
            }
        return None

def get_available_businesses():
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT id, name, description, price, storage_capacity, 
                   profit_multiplier, delivery_time, image_url,
                   (SELECT COUNT(*) FROM users WHERE business_id = businesses.id) as current_players,
                   max_players
            FROM businesses 
            WHERE available = 1 
            AND (SELECT COUNT(*) FROM users WHERE business_id = businesses.id) < max_players
            ORDER BY price ASC
        ''')
        
        businesses = cursor.fetchall()
        result = []
        for biz in businesses:
            result.append({
                'id': biz[0],
                'name': biz[1],
                'description': biz[2],
                'price': biz[3],
                'storage_capacity': biz[4],
                'profit_multiplier': biz[5],
                'delivery_time': biz[6],
                'image_url': biz[7],
                'current_players': biz[8],
                'max_players': biz[9]
            })
        return result

def buy_business(user_id, business_id):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT business_id FROM users WHERE user_id = ?', (user_id,))
        current_business = cursor.fetchone()
        
        if current_business and current_business[0] > 0:
            return False, "Уже есть бизнес"
        
        cursor.execute('SELECT price, max_players FROM businesses WHERE id = ?', (business_id,))
        business_info = cursor.fetchone()
        
        if not business_info:
            return False, "Не найден"
        
        price, max_players = business_info
        cursor.execute('SELECT COUNT(*) FROM users WHERE business_id = ?', (business_id,))
        current_players = cursor.fetchone()[0]
        
        if current_players >= max_players:
            return False, "Лимит игроков"
        
        cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
        balance = cursor.fetchone()[0]
        
        if balance < price:
            return False, f"Недостаточно {format_balance(price)}"
        
        cursor.execute('UPDATE users SET business_id = ?, balance = balance - ? WHERE user_id = ?', 
                      (business_id, price, user_id))
        
        return True, f"🏆 Куплен"

def sell_business(user_id):
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT u.business_id, b.price 
            FROM users u 
            LEFT JOIN businesses b ON u.business_id = b.id 
            WHERE u.user_id = ?
        ''', (user_id,))
        
        result = cursor.fetchone()
        
        if not result or not result[0]:
            return False, "Нет бизнеса"
        
        business_id, original_price = result
        sell_price = original_price // 2
        
        cursor.execute('UPDATE users SET business_id = 0, business_progress = 0, business_start_time = 0, business_raw_materials = 0, balance = balance + ? WHERE user_id = ?', 
                      (sell_price, user_id))
        
        return True, f"🏆 Продано за {format_balance(sell_price)}"

def transfer_business(from_user_id, to_user_id):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT business_id FROM users WHERE user_id = ?', (from_user_id,))
        from_business = cursor.fetchone()
        
        if not from_business or not from_business[0]:
            return False, "Нет бизнеса"
        
        cursor.execute('SELECT business_id FROM users WHERE user_id = ?', (to_user_id,))
        to_business = cursor.fetchone()
        
        if to_business and to_business[0] > 0:
            return False, "У получателя уже есть"
        
        business_id = from_business[0]
        
        cursor.execute('UPDATE users SET business_id = ?, business_progress = 0, business_start_time = 0, business_raw_materials = 0 WHERE user_id = ?', 
                      (business_id, to_user_id))
        cursor.execute('UPDATE users SET business_id = 0, business_progress = 0, business_start_time = 0, business_raw_materials = 0 WHERE user_id = ?', 
                      (from_user_id,))
        
        return True, "🏆 Передан"

def buy_raw_materials(user_id, amount):
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT u.business_id, u.business_raw_materials, b.storage_capacity, b.price
            FROM users u 
            LEFT JOIN businesses b ON u.business_id = b.id 
            WHERE u.user_id = ?
        ''', (user_id,))
        
        result = cursor.fetchone()
        
        if not result or not result[0]:
            return False, "Нет бизнеса"
        
        business_id, current_materials, storage_capacity, business_price = result
        max_purchase = business_price // 2
        
        if amount > max_purchase:
            return False, f"Максимум {format_balance(max_purchase)}"
        
        cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
        balance = cursor.fetchone()[0]
        
        if balance < amount:
            return False, "Недостаточно"
        
        materials_to_add = amount // 1000000
        
        if current_materials + materials_to_add > storage_capacity:
            return False, f"Склад полон {storage_capacity}"
        
        cursor.execute('UPDATE users SET business_raw_materials = business_raw_materials + ?, balance = balance - ?, business_start_time = ?, business_progress = ? WHERE user_id = ?', 
                      (materials_to_add, amount, int(time.time()), 0, user_id))
        
        return True, f"🏆 Закуплено {materials_to_add} за {format_balance(amount)}"

def collect_business_income(user_id):
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT u.business_id, u.business_raw_materials, u.business_start_time, 
                   u.business_progress, b.profit_multiplier, b.delivery_time
            FROM users u 
            LEFT JOIN businesses b ON u.business_id = b.id 
            WHERE u.user_id = ?
        ''', (user_id,))
        
        result = cursor.fetchone()
        
        if not result or not result[0]:
            return False, "Нет бизнеса"
        
        business_id, raw_materials, start_time, progress, profit_multiplier, delivery_time = result
        
        if raw_materials == 0:
            return False, "Нет сырья"
        
        if progress == 1:
            income = int(raw_materials * 1000000 * profit_multiplier)
            cursor.execute('UPDATE users SET balance = balance + ?, business_progress = 0, business_start_time = 0, business_raw_materials = 0 WHERE user_id = ?', 
                          (income, user_id))
            return True, f"🏆 Получено {format_balance(income)}"
        
        current_time = time.time()
        time_passed = current_time - start_time
        
        if time_passed >= delivery_time:
            cursor.execute('UPDATE users SET business_progress = 1 WHERE user_id = ?', (user_id,))
            return False, "🏆 Готов к сбору"
        else:
            time_left = delivery_time - time_passed
            hours = int(time_left // 3600)
            minutes = int((time_left % 3600) // 60)
            return False, f"⏳ {hours}ч {minutes}м"

def create_business(name, description, price, max_players, storage_capacity, profit_multiplier, delivery_time, image_url, created_by):
    with get_db_cursor() as cursor:
        cursor.execute('''
            INSERT INTO businesses (name, description, price, max_players, storage_capacity, profit_multiplier, delivery_time, image_url, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (name, description, price, max_players, storage_capacity, profit_multiplier, delivery_time, image_url, created_by))
        return cursor.lastrowid
# Обработчик кнопки "Работа"
@bot.message_handler(func=lambda message: message.text in ["💼 Работа", "Работа"])
def handle_work(message):
    try:
        # Пытаемся отправить с фото
        with open('work.jpg', 'rb') as photo:
            bot.send_photo(
                chat_id=message.chat.id,
                photo=photo,
                caption="💼 Выберите способ заработка:",
                reply_markup=create_work_menu(),
                parse_mode='HTML'
            )
    except FileNotFoundError:
        # Если фото отсутствуето, отправляем только текст
        bot.send_message(message.chat.id, "💼 Выберите способ заработка:", reply_markup=create_work_menu())
        print("Файл work.jpg не найден!")
    except Exception as e:
        # При любой другой ошибке - только текст
        bot.send_message(message.chat.id, "💼 Выберите способ заработка:", reply_markup=create_work_menu())
        print(f"Ошибка при отправке фото работы: {e}")

# Обработчик кнопки "Кликер"
@bot.message_handler(func=lambda message: message.text in ["👆 Кликер", "Кликер"])
def handle_clicker(message):

    bot.send_message(message.chat.id, "⚔️ Найди правильную кнопку:", reply_markup=create_clicker_keyboard(), parse_mode='HTML')

# Обработчик кнопки "Скам"
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

# Обработчик кнопки "Баланс" и команд баланса
@bot.message_handler(func=lambda message: message.text in ["Баланс", "баланс", "Б", "б", "/б"])
def handle_balance(message):
    try:
        user_id = message.from_user.id
        balance = get_balance(user_id)
        bot.send_message(message.chat.id, f"💵 Твой баланс: {format_balance(balance)}", parse_mode='HTML')
    except Exception as e:
        print(f"Ошибка в handle_balance: {e}")
        bot.send_message(message.chat.id, "⚠️ Что-то пошло не так. Попробуй ещё раз.")
# СЛОТ-МАШИНА КОНФИГУРАЦИЯ (СБАЛАНСИРОВАННАЯ)
SLOT_CONFIG = {
    "min_bet": 100,  # 1M минимальная бет
    "max_bet": 50000000000,  # 50B максимальная бет
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
    
    # Проверяем обычные комбинации (3 одинаковых символа)
    if reels_emojis[0] == reels_emojis[1] == reels_emojis[2]:
        for symbol in SLOT_CONFIG["symbols"]:
            if symbol["emoji"] == reels_emojis[0]:
                return {
                    "name": f"ТРИ {symbol['name'].upper()}",
                    "multiplier": symbol["multiplier"] * 2,
                    "symbols": reels_emojis
                }
    
    # Проверяем комбинации 2 одинаковых символа
    if reels_emojis[0] == reels_emojis[1] or reels_emojis[1] == reels_emojis[2] or reels_emojis[0] == reels_emojis[2]:
        # Находим символ который повторяется
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
    # Эмодзи для отображения
    slot_display = "🎰─────🎰─────🎰\n"
    slot_display += "│  {}  │  {}  │  {}  │\n".format(reels[0]["emoji"], reels[1]["emoji"], reels[2]["emoji"])
    slot_display += "🎰─────🎰─────🎰\n\n"
    
    # Информация о ставке и выигрыше
    if bet_amount is not None:
        slot_display += f"💵 Ставка: {format_balance(bet_amount)}\n"
    
    if win_amount is not None:
        if win_amount > 0:
            slot_display += f"🎉 Выигрыш: {format_balance(win_amount)}\n"
        else:
            slot_display += "😔 Выигрыша нет\n"
    
    return slot_display

# Обработчик команды слоты
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
        
        # Списываем ставку
        update_balance(user_id, -bet_amount)
        
        # Крутим слоты
        reels, combination = spin_slots()
        
        # Рассчитываем выигрыш
        win_amount = 0
        if combination:
            win_amount = int(bet_amount * combination["multiplier"])
            update_balance(user_id, win_amount)
        
        # Создаем сообщение с анимацией
        msg = bot.send_message(message.chat.id, "🎰 Крутим слоты...\n\n🔄 🔄 🔄")
        time.sleep(1.5)
        
        # Обновляем с результатами
        result_text = create_slots_display(reels, bet_amount, win_amount)
        
        if combination:
            result_text += f"\n🎊 {combination['name']}!\n"
            result_text += f"📈 Множитель: x{combination['multiplier']}\n"
        
        # Добавляем информацию о балансе
        new_balance = get_balance(user_id)
        result_text += f"\n💳 Новый баланс: {format_balance(new_balance)}"
        
        # Создаем кнопку для повторной игры
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
        bot.send_message(message.chat.id, "❌ Ошибка в игре!", parse_mode='HTML')

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

# Обработчик повторной игры
@bot.callback_query_handler(func=lambda call: call.data.startswith('slots_repeat_'))
def handle_slots_repeat(call):
    user_id = call.from_user.id
    balance = get_balance(user_id)
    
    try:
        bet_amount = int(call.data.split('_')[2])
        
        if bet_amount > balance:
            bot.answer_callback_query(call.id, "❌ Не хватает монет!")
            return
        
        # Списываем ставку
        update_balance(user_id, -bet_amount)
        
        # Крутим слоты
        reels, combination = spin_slots()
        
        # Рассчитываем выигрыш
        win_amount = 0
        if combination:
            win_amount = int(bet_amount * combination["multiplier"])
            update_balance(user_id, win_amount)
        
        # Создаем результат
        result_text = create_slots_display(reels, bet_amount, win_amount)
        
        if combination:
            result_text += f"\n🎊 {combination['name']}!\n"
            result_text += f"📈 Множитель: x{combination['multiplier']}\n"
        
        # Добавляем информацию о балансе
        new_balance = get_balance(user_id)
        result_text += f"\n💳 Новый баланс: {format_balance(new_balance)}"
        
        # Обновляем сообщение
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
        bot.answer_callback_query(call.id, "❌ Ошибка!")


# Обработчик кнопки "Майнинг"
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

# Обработчик callback для майнинга
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

# Глобальная переменная для хранения временного буста кликера
clicker_boost = {
    'active': False,
    'multiplier': 1.0,
    'end_time': 0,
    'message': ''
}

# Команда для админов для установки буста кликера
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
        
        # Время вмах (по умолчанию 30м)
        duration_minutes = 30
        if len(parts) > 3 and parts[3].isdigit():
            duration_minutes = int(parts[3])
        
        # Устанавливаем буст
        clicker_boost['active'] = True
        clicker_boost['multiplier'] = multiplier
        clicker_boost['end_time'] = time.time() + (duration_minutes * 60)  # Конвертируем всы
        
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

# Функция для проверки и получения текущего буста
def get_clicker_boost():
    """Получить текущий активный буст кликера"""
    global clicker_boost
    
    # Проверяем не истек ли буст
    if clicker_boost['active'] and time.time() > clicker_boost['end_time']:
        clicker_boost['active'] = False
        clicker_boost['multiplier'] = 1.0
        print("⏰ Время буста кликера истекло")
    
    return clicker_boost

# Функция для получения заработка с учетом буста
def get_boosted_click_power(base_power):
    """Получить мощность клика с учетом активного буста"""
    boost = get_clicker_boost()
    
    if boost['active']:
        return int(base_power * boost['multiplier'])
    
    return base_power

# Функция для получения информации о бусте для отображения
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
            

# Добавь в начало файла, где другие глобальные переменные
click_limits = {}  # {user_id: {'clicks': 0, 'reset_time': timestamp, 'rest_until': 0}}

# Функция для проверки лимита кликов
def check_click_limit(user_id):
    """Проверяет, не превысил ли пользователь лимит кликов"""
    current_time = time.time()
    
    # Проверяем, не на отдыхе ли пользователь
    if user_id in click_limits and 'rest_until' in click_limits[user_id]:
        if current_time < click_limits[user_id]['rest_until']:
            return False  # Еще отдыхает
    
    if user_id not in click_limits:
        click_limits[user_id] = {'clicks': 1, 'reset_time': current_time + 600}  # 600 сек = 10 минут
        return True
    
    limit_data = click_limits[user_id]
    
    # Если время сброса прошло, обнуляем счетчик
    if current_time > limit_data['reset_time']:
        click_limits[user_id] = {'clicks': 1, 'reset_time': current_time + 600}
        return True
    
    # Если лимит не превышен
    if limit_data['clicks'] < 300:
        click_limits[user_id]['clicks'] += 1
        return True
    
    # Лимит превышен - отправляем отдыхать на 3 минуты
    click_limits[user_id]['rest_until'] = current_time + 180  # 180 сек = 3 минуты
    return False

# Обновленный обработчик кликера
@bot.callback_query_handler(func=lambda call: call.data.startswith('clicker_'))
def clicker_callback_handler(call):
    user_id = call.from_user.id
    symbol = call.data.split('_')[1]
    
    # Проверяем лимит
    if not check_click_limit(user_id):
        # Если лимит превышен, показываем сообщение про отдых
        try:
            # Считаем сколько осталось отдыхать
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
                # Получаем базовую мощность клика
                cursor.execute('SELECT click_power, click_streak, total_clicks, experience FROM users WHERE user_id = ?', (user_id,))
                result = cursor.fetchone()
                
                if not result:
                    return
                    
                base_power, click_streak, total_clicks, old_exp = result
                
                # Применяем буст к заработку
                actual_power = get_boosted_click_power(base_power)
                
                new_streak = click_streak + 1
                new_total_clicks = total_clicks + 1

                # Начисляем опыт: 9 exp за клик
                EXP_PER_CLICK = 9
                new_exp = old_exp + EXP_PER_CLICK
                cursor.execute('UPDATE users SET experience = ? WHERE user_id = ?', (new_exp, user_id))
                
                # Проверяем повышение уровня
                old_level = get_level_from_exp(old_exp)
                new_level = get_level_from_exp(new_exp)
                
                bonus = 0
                level_up_bonus = 0
                
                # Бонус за 100 кликов
                if new_total_clicks % 100 == 0:
                    bonus = 10000
                    cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (bonus, user_id))

                # Обновляем основные данные
                cursor.execute('UPDATE users SET click_streak = ?, total_clicks = ? WHERE user_id = ?', 
                              (new_streak, new_total_clicks, user_id))
                cursor.execute('UPDATE users SET balance = balance + ?, last_click = ? WHERE user_id = ?',
                              (actual_power, time.time(), user_id))

                # Бонус за уровень
                if new_level > old_level:
                    level_up_bonus = new_level * 200
                    if level_up_bonus > 0:
                        cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (level_up_bonus, user_id))

            # После коммита отправляем уведомления о новом уровне
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

            # Обновляем сообщение кликера
            new_balance = get_balance(user_id)
            boost_info = get_boost_info_text()
            level_now = get_level_from_exp(new_exp)
            lv_emoji, lv_title = get_title(level_now)
            next_bonus = 100 - (new_total_clicks % 100)

            # Прогресс-бар опыта
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
        "• Стоимость кейсов (÷1000)\n"
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
        
        # Начинаем конвертацию
        bot.edit_message_text("🔄 Конвертируем балансы и цены...", call.message.chat.id, call.message.message_id)
        
        conversion_count = 0
        total_converted = 0
        
        with get_db_cursor() as cursor:
            # 1. Конвертируем балансы пользователей
            cursor.execute('SELECT SUM(balance) FROM users')
            old_total_balance = cursor.fetchone()[0] or 0
            
            cursor.execute('UPDATE users SET balance = balance / 1000 WHERE balance > 0')
            users_converted = cursor.rowcount
            
            cursor.execute('SELECT SUM(balance) FROM users')
            new_total_balance = cursor.fetchone()[0] or 0
            
            # 2. Конвертируем банковские вклады
            cursor.execute('SELECT SUM(bank_deposit) FROM users')
            old_total_deposit = cursor.fetchone()[0] or 0
            
            cursor.execute('UPDATE users SET bank_deposit = bank_deposit / 1000 WHERE bank_deposit > 0')
            deposits_converted = cursor.rowcount
            
            # 3. Конвертируем цены в магазине одежды
            cursor.execute('SELECT SUM(price) FROM clothes_shop')
            old_clothes_total = cursor.fetchone()[0] or 0
            
            cursor.execute('UPDATE clothes_shop SET price = price / 1000 WHERE price > 0')
            clothes_converted = cursor.rowcount
            
            # 4. Конвертируем бизнесы
            cursor.execute('SELECT SUM(price) FROM businesses')
            old_business_total = cursor.fetchone()[0] or 0
            
            cursor.execute('UPDATE businesses SET price = price / 1000 WHERE price > 0')
            businesses_converted = cursor.rowcount
            
            # 5. Конвертируем займы
            cursor.execute('SELECT SUM(loan_amount) FROM loans')
            old_loans_total = cursor.fetchone()[0] or 0
            
            cursor.execute('UPDATE loans SET loan_amount = loan_amount / 1000 WHERE loan_amount > 0')
            loans_converted = cursor.rowcount
            
            # 6. Конвертируем клановые балансы
            cursor.execute('SELECT SUM(balance) FROM clans')
            old_clans_total = cursor.fetchone()[0] or 0
            
            cursor.execute('UPDATE clans SET balance = balance / 1000 WHERE balance > 0')
            clans_converted = cursor.rowcount
            
            # 7. Конвертируем лотерею
            cursor.execute('UPDATE lottery SET jackpot = jackpot / 1000, last_win_amount = last_win_amount / 1000 WHERE jackpot > 0')
            
            # 8. Конвертируем транзакцияы (для истории)
            cursor.execute('UPDATE transfers SET amount = amount / 1000, fee = fee / 1000 WHERE amount > 0')
            
            # 9. Конвертируем аукционы
            cursor.execute('UPDATE auctions SET winner_bid = winner_bid / 1000 WHERE winner_bid > 0')
            cursor.execute('UPDATE auction_bids SET bid_amount = bid_amount / 1000 WHERE bid_amount > 0')
            
            # 10. Конвертируем компоненты кейсов
            cursor.execute('UPDATE user_bag SET component_price = component_price / 1000 WHERE component_price > 0')

        # Формируем отчет
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
# Глобальный словарь для хранения активных игр
tower_games = {}

def calculate_tower_multipliers():
    """Рассчитать множители для каждого уровня башни"""
    return {
        1: 1.5,   # x1.5 за первый ранг
        2: 2.0,   # x2.0 за второй ранг  
        3: 3.0,   # x3.0 за третий ранг
        4: 5.0,   # x5.0 за четвертый ранг
        5: 7.0   # x7.0 за победу
    }

def create_tower_keyboard(game_id, level, left_state, right_state, multipliers, show_mines=False):
    """Создать клавиатуру для игры Башня с 2 кнопками"""
    markup = InlineKeyboardMarkup(row_width=2)
    
    # Левая башня
    if show_mines and left_state == 'mine':
        left_button = InlineKeyboardButton("💣 Левая", callback_data=f"t{game_id}_{level}_left_m")
    elif left_state == 'safe':
        left_button = InlineKeyboardButton("🟢 Левая", callback_data=f"t{game_id}_{level}_left_s")
    elif left_state == 'exploded':
        left_button = InlineKeyboardButton("💥 Левая", callback_data=f"t{game_id}_{level}_left_e")
    elif left_state == 'selected':
        left_button = InlineKeyboardButton("🏆 Левая", callback_data=f"t{game_id}_{level}_left_c")
    else:
        left_button = InlineKeyboardButton("🏰 Левая", callback_data=f"t{game_id}_{level}_left_u")
    
    # Правая башня  
    if show_mines and right_state == 'mine':
        right_button = InlineKeyboardButton("💣 Правая", callback_data=f"t{game_id}_{level}_right_m")
    elif right_state == 'safe':
        right_button = InlineKeyboardButton("🟢 Правая", callback_data=f"t{game_id}_{level}_right_s")
    elif right_state == 'exploded':
        right_button = InlineKeyboardButton("💥 Правая", callback_data=f"t{game_id}_{level}_right_e")
    elif right_state == 'selected':
        right_button = InlineKeyboardButton("🏆 Правая", callback_data=f"t{game_id}_{level}_right_c")
    else:
        right_button = InlineKeyboardButton("🏰 Правая", callback_data=f"t{game_id}_{level}_right_u")
    
    markup.add(left_button, right_button)
    
    # Кнопка выхода с текущим выигрышем (только со 2 уровня и выше)
    current_multiplier = multipliers[level]
    win_amount = int(tower_games[game_id]['bet_amount'] * current_multiplier)
    
    # Разрешаем выход только со 2 уровня и выше или при окончении игры
    if level > 1 or show_mines:
        markup.add(InlineKeyboardButton(f"💵 Забрать {plain_balance(win_amount)}", callback_data=f"t{game_id}_x"))
    
    return markup

def start_tower_game(user_id, bet_amount):
    """Начать новую игру в Башню"""
    game_id = str(int(time.time()))  # Простой game_id из timestamp
    
    # Случайно выбираем, в какой башне будет мина (0 = левая, 1 = правая)
    mine_position = random.randint(0, 1)
    
    # Создаем скрытую карту мин
    if mine_position == 0:
        hidden_map = {'left': 'mine', 'right': 'safe'}
    else:
        hidden_map = {'left': 'safe', 'right': 'mine'}
    
    # Получаем множители
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
    
    # Создаем видимое состояние (обе башни неизвестны)
    visible_state = {'left': 'unknown', 'right': 'unknown'}
    
    # Случайно выбираем, в какой башне будет мина
    mine_position = random.randint(0, 1)
    
    # Создаем скрытую карту мин
    if mine_position == 0:
        hidden_map = {'left': 'mine', 'right': 'safe'}
    else:
        hidden_map = {'left': 'safe', 'right': 'mine'}
    
    # Сохраняем ранг
    game['levels'][next_level] = visible_state
    game['hidden_maps'][next_level] = hidden_map
    game['current_level'] = next_level
    
    return next_level, visible_state, hidden_map

def refund_expired_tower_games():
    """Возврат средств за просроченные игры в башне"""
    current_time = time.time()
    expired_games = []
    
    for game_id, game_data in tower_games.items():
        # Возвращаем только активные игры, которые превысили время
        if game_data['status'] == 'active' and current_time - game_data['start_time'] > 240:  # 4мы
            expired_games.append(game_id)
    
    for game_id in expired_games:
        game_data = tower_games[game_id]
        bet_amount = game_data['bet_amount']
        
        # Возвращаем средства только для активных игр
        update_balance(game_data['user_id'], bet_amount)
        
        # Отправляем уведомление пользователю
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
            pass  # Пользователь заблокировал бота
        
        # Обновляем сообщение игры если есть chat_id и message_id
        if game_data.get('chat_id') and game_data.get('message_id'):
            try:
                multipliers = game_data['multipliers']
                current_level = game_data['current_level']
                
                # Показываем все башни текущего уровня
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
        
        # Удаляем игру
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
                time.sleep(60)  # Проверяем каждуюму
            except Exception as e:
                print(f"❌ Ошибка в tower_refund_checker: {e}")
                time.sleep(60)
    
    thread = threading.Thread(target=checker, daemon=True)
    thread.start()


@bot.callback_query_handler(func=lambda call: call.data.startswith('t'))
def handle_tower_callback(call):
    """Обработчик callback'ов игры Башня"""
    try:
        user_id = call.from_user.id
        data = call.data
        
        # Формат: t{game_id}_{level}_{tower}_{type}
        if not data.startswith('t'):
            return
            
        parts = data[1:].split('_')  # Пропускаем первый символ 't'
        
        if len(parts) < 2:
            bot.answer_callback_query(call.id, "❌ Ошибка в данных!")
            return
        
        game_id = parts[0]
        
        # Проверяем существование игры
        if game_id not in tower_games:
            bot.answer_callback_query(call.id, "❌ Игра не найдено!")
            return
        
        game = tower_games[game_id]
        
        # Проверяем, что игрок является владельцем игры
        if game['user_id'] != user_id:
            bot.answer_callback_query(call.id, "❌ Это не твоя игра!")
            return
        
        # Проверяем статус игры
        if game['status'] != 'active':
            bot.answer_callback_query(call.id, "❌ Игра уже окончена!")
            return
        
        # Обработка выхода из игры
        if len(parts) == 2 and parts[1] == 'x':
            handle_tower_exit(game_id, call)
            return
        
        # Обработка хода игрока (формат: t{game_id}_{level}_{tower}_{type})
        if len(parts) >= 4:
            level = int(parts[1])
            tower = parts[2]  # 'left' или 'right'
            button_type = parts[3]
            
            # Проверяем, что ранг совпадает с текущим
            if level != game['current_level']:
                bot.answer_callback_query(call.id, "❌ Неверный ранг!")
                return
            
            # Получаем скрытую карту для текущего уровня
            hidden_map = game['hidden_maps'][level]
            
            # Проверяем тип кнопки по скрытой карте
            if hidden_map[tower] == 'mine':
                # Игрок наступил на мину
                handle_tower_mine(game_id, level, tower, call)
            elif hidden_map[tower] == 'safe':
                # Игрок выбрал безопасную башню
                handle_tower_safe(game_id, level, tower, call)
            else:
                bot.answer_callback_query(call.id, "❌ Ошибка в данных кнопки!")
        
    except Exception as e:
        print(f"Ошибка в обработчике Башни: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка в игре!")

def handle_tower_safe(game_id, level, tower, call):
    """Обработка выбора безопасной башни"""
    game = tower_games[game_id]
    
    # Обновляем видимое состояние башен
    level_state = game['levels'][level].copy()
    level_state[tower] = 'selected'
    game['levels'][level] = level_state
    
    # Проверяем, можно ли перейти на следующий ранг
    if level < 5:
        # Генерируем следующий ранг
        next_level, next_state, next_hidden = generate_next_level(game_id)
        
        if next_level:
            # Обновляем сообщение
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
            # Игрок прошел все уровни
            handle_tower_win(game_id, call)
    else:
        # Игрок прошел все уровни
        handle_tower_win(game_id, call)

def handle_tower_mine(game_id, level, tower, call):
    """Обработка наступления на мину - ИСПРАВЛЕННАЯ ВЕРСИЯ"""
    game = tower_games[game_id]
    
    # Обновляем видимое состояние башен (показываем обе башни)
    level_state = game['levels'][level].copy()
    hidden_map = game['hidden_maps'][level]
    
    # Показываем все башни
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
    
    # Обновляем статистику игрока - бет НЕ возвращается!
    update_game_stats(game['user_id'], False, 0, game['bet_amount'])

def handle_tower_win(game_id, call):
    """Обработка победы в игре"""
    game = tower_games[game_id]
    
    game['status'] = 'won'
    multipliers = game['multipliers']
    win_amount = int(game['bet_amount'] * multipliers[5])
    
    # Начисляем выигрыш
    update_balance(game['user_id'], win_amount)
    
    # Показываем все башни последнего уровня
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
    
    # Обновляем статистику игрока
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
    
    # Начисляем выигрыш
    update_balance(game['user_id'], win_amount)
    
    # Показываем все башни текущего уровня
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
    
    # Обновляем статистику игрока
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

# Запускаем проверку просроченных игр при старте бота
start_tower_refund_checker()
# Команда для проверки статуса буста
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

# Канал для подписки
REQUIRED_CHANNEL = "@FECTIZ"

# Функция проверки подписки
def check_subscription(user_id):
    """Проверяет подписку пользователя на канал"""
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

# Обработчик ежедневного бонуса (каждые 15м)
@bot.message_handler(func=lambda message: message.text in ["🎁 Бонус", "Бонус"])
def handle_daily_bonus(message):
    try:
        user_id = message.from_user.id
        
        # Проверяем подписку
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
        
        # Проверяем время
        with get_db_cursor() as cursor:
            cursor.execute('SELECT last_daily_bonus FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            last_bonus = result[0] if result else 0
        
        current_time = time.time()
        
        if last_bonus > 0:
            time_passed = current_time - last_bonus
            cooldown = 600 if is_premium(user_id) else 1800  # 10 мин премиум / 15 мин обычный
            if time_passed < cooldown:
                time_left = cooldown - time_passed
                minutes = int(time_left // 60)
                seconds = int(time_left % 60)
                bot.send_message(message.chat.id, f"⏳ {minutes}:{seconds:02d}")
                return
        
        # Рассчитываем бонус
        level, _ = get_user_level(user_id)
        level_mult = get_level_daily_bonus(level)
        base_bonus = 5000
        bonus_amount = int(base_bonus * level_mult)
        
        # Создаем JSON клавиатуру с зеленой кнопкой
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "🎁 ЗАБРАТЬ",
                        "callback_data": "claim_bonus",
                        "style": "success"  # 🟢 Зеленая!
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
            f"<tg-emoji emoji-id='5929466926408404919'>⏱</tg-emoji> Каждые {'10' if is_premium(user_id) else '15'} мин"
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

# Обработчик получения бонуса
@bot.callback_query_handler(func=lambda call: call.data == "claim_bonus")
def handle_claim_bonus(call):
    try:
        user_id = call.from_user.id
        
        # Проверяем подписку еще раз
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
        
        # Проверяем время
        with get_db_cursor() as cursor:
            cursor.execute('SELECT last_daily_bonus FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            last_bonus = result[0] if result else 0
        
        current_time = time.time()
        
        if last_bonus > 0:
            time_passed = current_time - last_bonus
            cooldown = 600 if is_premium(user_id) else 900  # 10 мин премиум / 15 мин обычный
            if time_passed < cooldown:
                time_left = int(cooldown - time_passed)
                minutes = time_left // 60
                seconds = time_left % 60
                bot.answer_callback_query(call.id, f"⏳ Подожди ещё {minutes}:{seconds:02d}", show_alert=True)
                return
        
        # Рассчитываем бонус
        level, _ = get_user_level(user_id)
        level_mult = get_level_daily_bonus(level)
        base_bonus = 5000
        bonus_amount = int(base_bonus * level_mult)
        
        # Выдаем деньги и очки
        update_balance(user_id, bonus_amount)
        add_experience(user_id, 500)
        
        # Обновляем время
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

# Обработчик проверки подписки (специальный для бонуса)
@bot.callback_query_handler(func=lambda call: call.data == "check_sub_bonus")
def handle_check_subscription_bonus(call):
    try:
        user_id = call.from_user.id
        
        if check_subscription(user_id):
            bot.answer_callback_query(call.id, "🏆 Подписка подтверждена")
            # Обновляем сообщение
            bot.edit_message_text(
                "✅ Подписка подтверждена! Бонусы открыты.",
                call.message.chat.id,
                call.message.message_id
            , parse_mode='HTML')
            # Через 1су показываем бонус
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


# Админ команда для массовой рассылки бонусов
@bot.message_handler(func=lambda message: message.text.lower().startswith('разбонус') and is_admin(message.from_user.id))
def handle_bonus_broadcast(message):
    """Рассылка бонусов всем пользователям"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        # Подтверждение с цветными кнопками
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
        
        # Просто отмечаем что рассылка активна
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
    
    # Начинаем рассылку
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
            # Получаем всех пользователей
            cursor.execute('SELECT user_id FROM users')
            users = cursor.fetchall()
            total_users = len(users)
            
            for i, (user_id,) in enumerate(users, 1):
                try:
                    # Получаем данные пользователя для расчета бонуса
                    cursor.execute('SELECT experience FROM users WHERE user_id = ?', (user_id,))
                    result = cursor.fetchone()
                    exp = result[0] if result else 0
                    level = int((exp / 1000) ** 0.5) + 1
                    
                    base_bonus = 5000
                    bonus_levels = level // 3
                    bonus_amount = base_bonus + (1234 * bonus_levels)
                    
                    # Проверяем подписан ли игрок
                    is_subscribed = check_subscription(user_id)
                    
                    if is_subscribed:
                        # Если уже подписан - отправляем бонус сразу
                        markup = InlineKeyboardMarkup()
                        markup.add(InlineKeyboardButton("🎁 Забрать", callback_data="bonus_broadcast_claim"))
                        
                        bonus_text = f"🎁 Бонус\n\n"
                        bonus_text += f"💵 {format_balance(bonus_amount)}\n"
                        bonus_text += f"⚔️ +500 ⭐ опыта\n\n"
                        bonus_text += f"🕐 от администрации"
                        
                        bot.send_message(user_id, bonus_text, reply_markup=markup)
                        sent += 1
                    else:
                        # Если не подписан - показываем с проверкой
                        markup = InlineKeyboardMarkup()
                        markup.add(InlineKeyboardButton("🎁 Получить бонус", callback_data="bonus_broadcast_claim"))
                        markup.add(InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/FECTIZ"))
                        
                        bonus_text = f"🎁 Бонус\n\n"
                        bonus_text += f"💵 {format_balance(bonus_amount)}\n"
                        bonus_text += f"⚔️ +500 ⭐ опыта\n\n"
                        bonus_text += f"🕐 от администрации\n"
                        bonus_text += f"⚠️ Требуется подписка на канал"
                        
                        bot.send_message(user_id, bonus_text, reply_markup=markup)
                        sent += 1
                    
                    # Обновляем прогресс каждые 20 пользователей
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
                    
                    # Пауза между отправками
                    time.sleep(0.1)
                    
                except Exception as e:
                    print(f"Ошибка отправки бонуса {user_id}: {e}")
                    failed += 1
                    time.sleep(0.5)  # Большая пауза при ошибке
        
        # Финальный результат
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

# Обработчик получения бонуса из рассылки
@bot.callback_query_handler(func=lambda call: call.data == "bonus_broadcast_claim")
def handle_broadcast_bonus_claim(call):
    """Получение бонуса из массовой рассылки"""
    try:
        user_id = call.from_user.id
        
        # Проверяем подписку
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
        
        # Получаем данные пользователя для расчета бонуса
        level, _ = get_user_level(user_id)
        level_mult = get_level_daily_bonus(level)
        base_bonus = 5000
        bonus_amount = int(base_bonus * level_mult)
        
        # Выдаем деньги и очки
        update_balance(user_id, bonus_amount)
        add_experience(user_id, 500)
        
        # Обновляем сообщение (ТОЧНО ТАК ЖЕ КАК ОБЫЧНЫЙ БОНУС)
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
# Команда /menu для показа главного меню
@bot.message_handler(commands=['', 'меню', '🔄'])
def handle_menu_command(message):
    """Показывает главное меню"""
    try:
        user_id = message.from_user.id
        username = message.from_user.username
        first_name = message.from_user.first_name
        
        # Проверяем/создаем пользователя
        get_or_create_user(user_id, username, first_name)
        
        # Показываем главное меню
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
# Команда для быстрого сброса опыта пользователю
# Команда для быстрого сброса опыта пользователю
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
        
        # Определяем ID пользователя
        if target.startswith('@'):
            # Поиск по юзернейму (убираем @)
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
            # По ID
            try:
                target_user_id = int(target)
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный ID! Используй цифры или @юзернейм")
                return
        
        # Проверяем существование пользователя
        user_info = get_user_info(target_user_id)
        if not user_info:
            bot.send_message(message.chat.id, f"❌ Пользователь с ID {target_user_id} не найден в базе!")
            return
        
        # Сбрасываем опыт
        with get_db_cursor() as cursor:
            cursor.execute('UPDATE users SET experience = 0 WHERE user_id = ?', (target_user_id,))
        
        bot.send_message(message.chat.id, f"✅ Опыт сброшен")
        
    except Exception as e:
        print(f"Ошибка при сбросе опыта: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
# Также обработчик для текстовой команды "меню"
@bot.message_handler(func=lambda message: message.text.lower() == 'меню')
def handle_menu_text(message):
    handle_menu_command(message)

# Функция для получения информации об уровне пользователя
# ==================== СИСТЕМА УРОВНЕЙ ====================

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
        needed = 1000 + (level - 1) * 500  # 1: 1000, 2: 1500, 3: 2000 ...
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

# ==================== КОНЕЦ СИСТЕМЫ УРОВНЕЙ ====================

# Функция для добавления опыта
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
        
        # Определяем время суток
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
            # Абсурдные и современные
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
            
            # Про деньги с юмором
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
            
            # Короткие и дерзкие
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
            
            # С твистом
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
            
            # Шуточные угрозы
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

        # Формируем компактное сообщение
        prem_badge = f" {PREMIUM_EMOJI}" if is_premium(user_id) else ""
        message_text = f"{emoji} {time_greeting} <b>{display_name}</b>{prem_badge}\n"
        message_text += f"На счету — <b>{format_balance(user_info['balance'])}</b>\n"
        message_text += f"<blockquote>{lv_emoji} Титул: <b>{lv_title}</b></blockquote>"
        
        # Отправляем с фото персонажа
        outfit_path = create_character_outfit(user_id)
        
        try:
            with open(outfit_path, 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption=message_text, parse_mode='HTML')
        except:
            with open("images/base_human.jpg", 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption=message_text, parse_mode='HTML')
    
    except Exception as e:
        bot.send_message(message.chat.id, "❌ Ошибка", parse_mode='HTML')


# СИСТЕМА ТРЕЙДОВ (ОБМЕНА ОДЕЖДОЙ)
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

# Команда для предложения обмена
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

    # Проверяем есть ли активный обмен
    for trade_id, trade in active_trades.items():
        if (trade.user1_id == user1_id and trade.user2_id == user2_id) or \
           (trade.user1_id == user2_id and trade.user2_id == user1_id):
            bot.send_message(message.chat.id, "❌ У вас уже есть активный обмен!", parse_mode='HTML')
            return

    # Создаем новый обмен
    trade = Trade(user1_id, user2_id)
    active_trades[trade.trade_id] = trade

    print(f"🏆 Создан обмен: {trade.trade_id}")

    # Отправляем интерфейс обмена
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
        
        # Получаем одежду пользователя
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
        
        # Кнопки для добавления/удаления вещей
        if user_clothes:
            for i, item in enumerate(user_clothes[:8], 1):
                item_in_trade = item['item_id'] in (trade.user1_items if user_id == trade.user1_id else trade.user2_items)
                if item_in_trade:
                    trade_inline.append([{"text": f"❌ Убрать {item['name'][:12]}", "callback_data": f"TRADE_REM_{trade.trade_code}_{item['item_id']}_{user_id}", "style": "danger"}])
                else:
                    trade_inline.append([{"text": f"➕ Добавить {item['name'][:12]}", "callback_data": f"TRADE_ADD_{trade.trade_code}_{item['item_id']}_{user_id}", "style": "secondary"}])
        
        # Кнопки подтверждения
        trade_inline.append([
            {"text": "✅ Подтвердить обмен", "callback_data": f"TRADE_CFM_{trade.trade_code}_{user_id}", "style": "success"},
            {"text": "❌ Отмена", "callback_data": f"TRADE_CNL_{trade.trade_code}_{user_id}", "style": "danger"}
        ])
        trade_inline.append([{"text": "🔄 Обновить", "callback_data": f"TRADE_REF_{trade.trade_code}_{user_id}"}])
        
        trade_keyboard = json.dumps({"inline_keyboard": trade_inline})
        
        # Отправляем или редактируем сообщение
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

# Обработчик добавления вещи в обмен
@bot.callback_query_handler(func=lambda call: call.data.startswith('TRADE_ADD_'))
def handle_trade_add(call):
    user_id = call.from_user.id
    parts = call.data.split('_')
    
    print(f"🔍 TRADE_ADD callback: {call.data}")  # Дебаг
    
    if len(parts) < 4:
        bot.answer_callback_query(call.id, "❌ Ошибка данных!")
        return
        
    trade_code = parts[2]
    item_id = int(parts[3])
    target_user_id = int(parts[4])
    
    trade_id = f"TRADE_{trade_code}"
    
    # Проверяем что игрок тот же
    if user_id != target_user_id:
        bot.answer_callback_query(call.id, "❌ Это не твой интерфейс!")
        return
    
    if trade_id not in active_trades:
        bot.answer_callback_query(call.id, "❌ Обмен не найден!")
        return
    
    trade = active_trades[trade_id]
    
    # Проверяем что игрок боец обмена
    if user_id not in [trade.user1_id, trade.user2_id]:
        bot.answer_callback_query(call.id, "❌ Вы не боец обмена!")
        return
    
    # Определяем чья это вещь
    user_clothes = get_user_clothes(user_id)
    user_has_item = any(item['item_id'] == item_id for item in user_clothes)
    
    if not user_has_item:
        bot.answer_callback_query(call.id, "❌ У вас нет этой вещи!")
        return
    
    # Добавляем в соответствующий список
    if user_id == trade.user1_id:
        if item_id not in trade.user1_items:
            trade.user1_items.append(item_id)
    else:
        if item_id not in trade.user2_items:
            trade.user2_items.append(item_id)
    
    bot.answer_callback_query(call.id, "🏆 Вещь добавлена в обмен!")
    
    # Обновляем интерфейсы
    send_trade_interface(trade.user1_id, trade)
    send_trade_interface(trade.user2_id, trade)

# Обработчик удаления вещи из обмена
@bot.callback_query_handler(func=lambda call: call.data.startswith('TRADE_REM_'))
def handle_trade_remove(call):
    user_id = call.from_user.id
    parts = call.data.split('_')
    
    print(f"🔍 TRADE_REM callback: {call.data}")  # Дебаг
    
    if len(parts) < 4:
        bot.answer_callback_query(call.id, "❌ Ошибка данных!")
        return
        
    trade_code = parts[2]
    item_id = int(parts[3])
    target_user_id = int(parts[4])
    
    trade_id = f"TRADE_{trade_code}"
    
    # Проверяем что игрок тот же
    if user_id != target_user_id:
        bot.answer_callback_query(call.id, "❌ Это не твой интерфейс!")
        return
    
    if trade_id not in active_trades:
        bot.answer_callback_query(call.id, "❌ Обмен не найден!")
        return
    
    trade = active_trades[trade_id]
    
    # Удаляем из соответствующего списка
    if user_id == trade.user1_id:
        if item_id in trade.user1_items:
            trade.user1_items.remove(item_id)
    else:
        if item_id in trade.user2_items:
            trade.user2_items.remove(item_id)
    
    bot.answer_callback_query(call.id, "🏆 Вещь убрана из обмена!")
    
    # Обновляем интерфейсы
    send_trade_interface(trade.user1_id, trade)
    send_trade_interface(trade.user2_id, trade)

# Обработчик подтверждения обмена
@bot.callback_query_handler(func=lambda call: call.data.startswith('TRADE_CFM_'))
def handle_trade_confirm(call):
    user_id = call.from_user.id
    parts = call.data.split('_')
    
    print(f"🔍 TRADE_CFM callback: {call.data}")  # Дебаг
    
    if len(parts) < 4:
        bot.answer_callback_query(call.id, "❌ Ошибка данных!")
        return
        
    trade_code = parts[2]
    target_user_id = int(parts[3])
    
    trade_id = f"TRADE_{trade_code}"
    
    # Проверяем что игрок тот же
    if user_id != target_user_id:
        bot.answer_callback_query(call.id, "❌ Это не твой интерфейс!")
        return
    
    if trade_id not in active_trades:
        bot.answer_callback_query(call.id, "❌ Обмен не найден!")
        return
    
    trade = active_trades[trade_id]
    
    # Проверяем что есть вещи для обмена
    if not trade.user1_items and not trade.user2_items:
        bot.answer_callback_query(call.id, "❌ Нет вещей для обмена!")
        return
    
    # Если оба подтвердили - совершаем обмен
    if user_id == trade.user1_id:
        trade.user1_confirmed = True
    else:
        trade.user2_confirmed = True
    
    # Если оба подтвердили
    if hasattr(trade, 'user1_confirmed') and hasattr(trade, 'user2_confirmed'):
        execute_trade(trade)
        bot.answer_callback_query(call.id, "🏆 Обмен окончен!")
    else:
        bot.answer_callback_query(call.id, "🏆 Вы подтвердили обмен! Ждите соперника.")
        
        # Уведомляем другого игрока
        other_user_id = trade.user2_id if user_id == trade.user1_id else trade.user1_id
        try:
            bot.send_message(other_user_id, "⚠️ Соперник подтвердил обмен! Подтвердите вы.")
        except:
            pass

# Обработчик обновления интерфейса
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
    
    # Проверяем что игрок тот же
    if user_id != target_user_id:
        bot.answer_callback_query(call.id, "❌ Это не твой интерфейс!")
        return
    
    if trade_id not in active_trades:
        bot.answer_callback_query(call.id, "❌ Обмен не найден!")
        return
    
    trade = active_trades[trade_id]
    send_trade_interface(user_id, trade)
    bot.answer_callback_query(call.id, "🏆 Обновлено!")

# Обработчик отмены обмена
@bot.callback_query_handler(func=lambda call: call.data.startswith('TRADE_CNL_'))
def handle_trade_cancel(call):
    user_id = call.from_user.id
    parts = call.data.split('_')
    
    print(f"🔍 TRADE_CNL callback: {call.data}")  # Дебаг
    
    if len(parts) < 4:
        bot.answer_callback_query(call.id, "❌ Ошибка данных!")
        return
        
    trade_code = parts[2]
    target_user_id = int(parts[3])
    
    trade_id = f"TRADE_{trade_code}"
    
    # Проверяем что игрок тот же
    if user_id != target_user_id:
        bot.answer_callback_query(call.id, "❌ Это не твой интерфейс!")
        return
    
    if trade_id not in active_trades:
        bot.answer_callback_query(call.id, "❌ Обмен не найден!")
        return
    
    trade = active_trades[trade_id]
    
    # Уведомляем другого игрока
    other_user_id = trade.user2_id if user_id == trade.user1_id else trade.user1_id
    try:
        other_user_info = get_user_info(other_user_id)
        other_user_name = other_user_info['custom_name'] if other_user_info['custom_name'] else (
            f"@{other_user_info['username']}" if other_user_info['username'] else other_user_info['first_name']
        )
        bot.send_message(other_user_id, f"❌ {other_user_name} отменил обмен", parse_mode='HTML')
    except:
        pass
    
    # Удаляем обмен
    del active_trades[trade_id]
    
    bot.answer_callback_query(call.id, "🏆 Обмен сброшен!")
    
    try:
        bot.send_message(user_id, "❌ Обмен сброшен", parse_mode='HTML')
    except:
        pass

def execute_trade(trade):
    try:
        with get_db_cursor() as cursor:
            # Обмениваем вещи
            for item_id in trade.user1_items:
                # Передаем от user1 к user2
                cursor.execute('UPDATE user_clothes SET user_id = ? WHERE user_id = ? AND item_id = ?', 
                              (trade.user2_id, trade.user1_id, item_id))
            
            for item_id in trade.user2_items:
                # Передаем от user2 к user1
                cursor.execute('UPDATE user_clothes SET user_id = ? WHERE user_id = ? AND item_id = ?', 
                              (trade.user1_id, trade.user2_id, item_id))
            
            # Получаем информацию для уведомлений
            user1_info = get_user_info(trade.user1_id)
            user2_info = get_user_info(trade.user2_id)
            
            user1_name = user1_info['custom_name'] if user1_info['custom_name'] else (
                f"@{user1_info['username']}" if user1_info['username'] else user1_info['first_name']
            )
            user2_name = user2_info['custom_name'] if user2_info['custom_name'] else (
                f"@{user2_info['username']}" if user2_info['username'] else user2_info['first_name']
            )
            
            # Уведомляем обоих
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
            
            # Отправляем обоим игрокам
            try:
                bot.send_message(trade.user1_id, trade_result)
            except:
                pass
            
            try:
                bot.send_message(trade.user2_id, trade_result)
            except:
                pass
            
            # Удаляем обмен
            if trade_id in active_trades:
                del active_trades[trade_id]
                
    except Exception as e:
        print(f"❌ Ошибка выполнения обмена: {e}")
        
        # Уведомляем об ошибке
        try:
            bot.send_message(trade.user1_id, "❌ Ошибка при обмене!", parse_mode='HTML')
            bot.send_message(trade.user2_id, "❌ Ошибка при обмене!", parse_mode='HTML')
        except:
            pass

# Вспомогательная функция для получения информации о вещи
def get_item_info(item_id):
    with get_db_cursor() as cursor:
        cursor.execute('SELECT name FROM clothes_shop WHERE id = ?', (item_id,))
        result = cursor.fetchone()
        if result:
            return {'name': result[0]}
        return None

# Очистка старых обменов
def cleanup_expired_trades():
    current_time = time.time()
    expired = []
    
    for trade_id, trade in active_trades.items():
        if current_time - trade.created_at > 1800:  # 30м
            expired.append(trade_id)
    
    for trade_id in expired:
        trade = active_trades[trade_id]
        
        # Уведомляем боецов
        try:
            bot.send_message(trade.user1_id, "❌ Обмен сброшен (время вышло)", parse_mode='HTML')
        except:
            pass
        
        try:
            bot.send_message(trade.user2_id, "❌ Обмен сброшен (время вышло)", parse_mode='HTML')
        except:
            pass
        
        del active_trades[trade_id]

# Запускаем очистку обменов
def start_trade_cleanup():
    while True:
        try:
            cleanup_expired_trades()
        except:
            pass
        time.sleep(60)

trade_cleanup_thread = threading.Thread(target=start_trade_cleanup, daemon=True)
trade_cleanup_thread.start()

# Команда для добавления одежды через текст
@bot.message_handler(func=lambda message: message.text.lower().startswith('добавить одежду ') and is_admin(message.from_user.id))
def handle_add_clothing(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        # Формат: добавить одежду Название | Цена | Тип | Файл.png
        parts = message.text.split('|')
        if len(parts) < 4:
            bot.send_message(message.chat.id,
                           "❌ Формат: добавить одежду Название | Цена | Тип | Файл.png\n\n"
                           "📋 Типы: Голова, Тело, Ноги, Слева, Справа\n"
                           "💡 Пример:\n"
                           "добавить одежду Кепка | 1000000 | Голова | cap.png\n"
                           "добавить одежду Часы | 5000000 | Слева | watch.png", parse_mode='HTML')
            return
        
        # Парсим параметры
        name_part = parts[0][16:].strip()  # "добавить одежду " = 16 символов
        name = name_part
        price_text = parts[1].strip()
        item_type = parts[2].strip()
        image_file = parts[3].strip()
        
        # Проверяем тип
        valid_types = ['Голова', 'Тело', 'Ноги', 'Слева', 'Справа']
        if item_type not in valid_types:
            bot.send_message(message.chat.id, f"❌ Неверный тип! Допустимо: {', '.join(valid_types)}", parse_mode='HTML')
            return
        
        # Парсим цену
        price = parse_bet_amount(price_text, float('inf'))
        if price is None or price <= 0:
            bot.send_message(message.chat.id, "❌ Неверная стоимость!", parse_mode='HTML')
            return
        
        # Проверяем существование файла
        image_path = f"images/{image_file}"
        if not os.path.exists(image_path):
            bot.send_message(message.chat.id, 
                           f"❌ Файл отсутствует: {image_file}\n\n"
                           f"📁 Убедитесь что файл лежит в папке images/", parse_mode='HTML')
            return
        
        # Проверяем, есть ли уже такая вещь
        with get_db_cursor() as cursor:
            cursor.execute('SELECT id FROM clothes_shop WHERE name = ?', (name,))
            if cursor.fetchone():
                bot.send_message(message.chat.id, f"❌ Вещь '{name}' уже существует!", parse_mode='HTML')
                return
            
            # Добавляем вещь в базу
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
# ===================== ЛОТЕРЕЙНЫЙ АВТОМАТ =====================
LOTTERY_MACHINE_CONFIG = {
    "min_bet": 1000,      # 1кк
    "max_bet": 100000, # 100ккк
    "symbols": {
        "❌": 0.0,   # Пусто
        "💵": 0.5,   # x0.5
        "🎁": 2.0,   # x2  
        "⭐": 3.0,   # x5
        "👑": 5.0,  # x10
        "🎰": 10.0, # x100 ДЖЕКПОТ
        "⚠️": -0.5,  # -50%
        "❌": -1.0,  # -100%
        "🎁": 0.0    # Кейс (бонус)
    },
    "chances": [50, 20, 7, 3, 2, 1, 10, 5, 2]  # Вероятности символов
}

# Активные игры
lottery_games = {}

def create_lottery_ticket(bet):
    """Создает лотерейный билет из 3 ячеек"""
    symbols = random.choices(
        list(LOTTERY_MACHINE_CONFIG["symbols"].keys()),
        weights=LOTTERY_MACHINE_CONFIG["chances"],
        k=3
    )
    
    return {
        "bet": bet,
        "symbols": symbols,
        "revealed": [False, False, False],
        "opened": 0,
        "finished": False,
        "created_at": time.time()
    }

def calculate_win(ticket):
    """Рассчитывает выигрыш для 3 ячеек"""
    symbols = ticket["symbols"]
    
    # Проверяем комбинации из 3 одинаковых символов
    if len(set(symbols)) == 1:  # Все 3 одинаковые
        symbol = symbols[0]
        
        if symbol == "🎰":  # ДЖЕКПОТ
            return ticket["bet"] * 100  # x100 за 3 джекпота
        elif symbol == "❌":  # Полный проигрыш
            return -ticket["bet"]  # -100%
        elif symbol == "⚠️":  # Штраф
            return -int(ticket["bet"] * 0.8)  # -80%
        elif symbol == "❌":  # Все пустые
            return 0
        else:
            multiplier = LOTTERY_MACHINE_CONFIG["symbols"][symbol]
            return int(ticket["bet"] * multiplier * 3)  # x3 за комбинацию
    
    # Если есть кейс
    if "🎁" in symbols:
        return int(ticket["bet"] * 2)  # x2 за кейс
    
    # Считаем средний множитель
    total_multiplier = 0
    for symbol in symbols:
        total_multiplier += LOTTERY_MACHINE_CONFIG["symbols"][symbol]
    
    avg_multiplier = total_multiplier / 3
    
    if avg_multiplier <= 0:
        return -int(ticket["bet"] * abs(avg_multiplier))
    
    return int(ticket["bet"] * avg_multiplier)


# Обработчик царапания
@bot.callback_query_handler(func=lambda call: call.data.startswith('lot_'))
def handle_scratch(call):
    data = call.data.split('_')
    
    if len(data) < 3:
        return
    
    target_user_id = int(data[1])
    
    if call.from_user.id != target_user_id:
        bot.answer_callback_query(call.id, "❌ Не твой билет!")
        return
    
    if target_user_id not in lottery_games:
        bot.answer_callback_query(call.id, "❌ Билет не найден!")
        return
    
    ticket = lottery_games[target_user_id]
    
    if data[2] == "all":
        # Открыть все 3 ячейки
        for i in range(3):
            ticket["revealed"][i] = True
        ticket["opened"] = 3
        finish_game(target_user_id, ticket)
        bot.answer_callback_query(call.id, "🏆 Все открыто!")
        return
    
    cell = int(data[2])
    
    if cell < 0 or cell > 2:
        return
    
    if ticket["revealed"][cell]:
        bot.answer_callback_query(call.id, "❌ Уже открыто!")
        return
    
    # Открываем ячейку
    ticket["revealed"][cell] = True
    ticket["opened"] += 1
    
    # Обновляем клавиатуру
    markup = InlineKeyboardMarkup(row_width=3)
    buttons = []
    
    for i in range(3):
        if ticket["revealed"][i]:
            buttons.append(InlineKeyboardButton(ticket["symbols"][i], callback_data=f"lot_{i}_done"))
        else:
            buttons.append(InlineKeyboardButton("⬜", callback_data=f"lot_{target_user_id}_{i}"))
    
    markup.row(*buttons)
    
    # Если еще не все открыты, показываем кнопку "Открыть все"
    if ticket["opened"] < 3:
        markup.row(InlineKeyboardButton("⚔️ ОТКРЫТЬ ВСЕ", callback_data=f"lot_{target_user_id}_all"))
    
    # Обновляем сообщение
    user_info = get_user_info(target_user_id)
    name = user_info['custom_name'] or user_info['username'] or user_info['first_name']
    
    # Показываем текущие символы
    current_view = ""
    for i in range(3):
        if ticket["revealed"][i]:
            current_view += f"[{ticket['symbols'][i]}]"
        else:
            current_view += "[⬜]"
    
    bot.edit_message_text(
        f"🎰 Лотерейный билет\n\n"
        f"🎴 Игрок: {name}\n"
        f"🎫 Билет #{len(lottery_games)}\n"
        f"💵 Ставка: {format_balance(ticket['bet'])}\n"
        f"📊 Открыто: {ticket['opened']}/3\n\n"
        f"{current_view}",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    , parse_mode='HTML')
    
    # Показываем что открыли
    symbol = ticket["symbols"][cell]
    bot.answer_callback_query(call.id, f"🏆 {symbol}")
    
    # Если открыли все 3 ячейки
    if ticket["opened"] >= 3:
        finish_game(target_user_id, ticket)

def finish_game(user_id, ticket):
    """Завершает игру и показывает результат"""
    # Рассчитываем выигрыш
    win_amount = calculate_win(ticket)
    
    # Начисляем/списываем
    update_balance(user_id, win_amount)
    
    # Рассчитываем множитель
    if win_amount > 0:
        multiplier = win_amount / ticket["bet"]
    elif win_amount < 0:
        multiplier = win_amount / ticket["bet"]
    else:
        multiplier = 1.0
    
    # Получаем инфо пользователя
    user_info = get_user_info(user_id)
    name = user_info['custom_name'] or user_info['username'] or user_info['first_name']
    
    # Показываем все символы
    symbols_view = f"[{ticket['symbols'][0]}][{ticket['symbols'][1]}][{ticket['symbols'][2]}]"
    
    # Финальное сообщение
    result_text = f"🎰 Лотерейный билет\n\n"
    result_text += f"🎴 Игрок: {name}\n"
    result_text += f"🎫 Билет #{len(lottery_games)}\n"
    result_text += f"💵 Ставка: {format_balance(ticket['bet'])}\n"
    result_text += f"📊 Открыто: 3/3\n\n"
    result_text += f"{symbols_view}\n\n"
    result_text += f"⚔️ РЕЗУЛЬТАТ:\n"
    result_text += f"📈 Множитель: x{abs(multiplier):.1f}\n"
    
    if win_amount > 0:
        result_text += f"🎉 ВЫИГРЫШ: +{format_balance(win_amount)}"
    elif win_amount < 0:
        result_text += f"💸 ПРОИГРЫШ: {format_balance(abs(win_amount))}"
    else:
        result_text += f"😐 НИЧЬЯ: 0🌸"
    
    # Обновляем сообщение
    try:
        bot.edit_message_text(
            result_text,
            ticket["chat_id"],
            ticket["message_id"]
        )
    except:
        # Если ошибка отредактировать, отправляем новое
        bot.send_message(ticket["chat_id"], result_text)
    
    # Удаляем игру из активных
    if user_id in lottery_games:
        del lottery_games[user_id]

# Очистка старых игр (на случай зависаний)
def cleanup_old_lottery_games():
    """Очищает игры старше 10м"""
    current_time = time.time()
    to_remove = []
    
    for user_id, ticket in lottery_games.items():
        if current_time - ticket.get("created_at", 0) > 600:  # 10м
            # Возвращаем ставку
            update_balance(user_id, ticket["bet"])
            to_remove.append(user_id)
    
    for user_id in to_remove:
        del lottery_games[user_id]

# Запуск фоновой очистки
import threading
def start_lottery_cleanup():
    def cleanup_loop():
        while True:
            time.sleep(300)  # Каждые 5м
            cleanup_old_lottery_games()
    
    thread = threading.Thread(target=cleanup_loop, daemon=True)
    thread.start()

# Запускаем очистку при импорте
start_lottery_cleanup()
print("🏆 Лотерейный автомат (3 ячейки) готов!")
# Команда для массового добавления одежды
@bot.message_handler(func=lambda message: message.text.lower().startswith('добавить несколько ') and is_admin(message.from_user.id))
def handle_add_multiple_clothing(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        # Формат: добавить несколько
        # Название1 | Цена1 | Тип1 | Файл1.png
        # Название2 | Цена2 | Тип2 | Файл2.png
        lines = message.text.split('\n')[1:]  # Пропускаем первую строку с командой
        
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
                    
                    # Проверяем тип
                    valid_types = ['Голова', 'Тело', 'Ноги', 'Слева', 'Справа']
                    if item_type not in valid_types:
                        errors.append(f"Строка {i}: Неверный тип '{item_type}'")
                        continue
                    
                    # Парсим цену
                    price = parse_bet_amount(price_text, float('inf'))
                    if price is None or price <= 0:
                        errors.append(f"Строка {i}: Неверная стоимость '{price_text}'")
                        continue
                    
                    # Проверяем файл
                    image_path = f"images/{image_file}"
                    if not os.path.exists(image_path):
                        errors.append(f"Строка {i}: Файл отсутствует '{image_file}'")
                        continue
                    
                    # Проверяем существование
                    cursor.execute('SELECT id FROM clothes_shop WHERE name = ?', (name,))
                    if cursor.fetchone():
                        errors.append(f"Строка {i}: Вещь уже существует '{name}'")
                        continue
                    
                    # Добавляем
                    cursor.execute('INSERT INTO clothes_shop (name, price, type, image_name) VALUES (?, ?, ?, ?)', 
                                  (name, price, item_type, image_file))
                    added_count += 1
                    
                except Exception as e:
                    errors.append(f"Строка {i}: Ошибка {e}")
        
        # Формируем результат
        result_text = f"🏆 Добавлено {added_count} вещей\n"
        if errors:
            result_text += f"\n❌ Ошибки ({len(errors)}):\n" + "\n".join(errors[:10])  # Показываем первые 10 ошибок
        
        bot.send_message(message.chat.id, result_text)
        
    except Exception as e:
        print(f"❌ Ошибка массового добавления: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

# Команда для просмотра файлов в папке images
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
        
        # Группируем по 20 файлов
        for i in range(0, len(files), 20):
            batch = files[i:i+20]
            batch_text = files_text + "\n".join([f"• {f}" for f in batch])
            
            if len(batch_text) > 4000:
                # Если слишком длинное, разбиваем
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
            # Создаем таблицы если их нет
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
            
# Добавляем одежду
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
# Функция для изменения типов одежды в БД
@bot.message_handler(func=lambda message: message.text.lower() == 'миграция типов' and is_admin(message.from_user.id))
def handle_migration_types(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        with get_db_cursor() as cursor:
            # Старые типы -> Новые типы
            type_mapping = {
                'hat': 'Голова',
                'body': 'Тело', 
                'legs': 'Ноги',
                'shoes': 'Ноги',  # Обувь тоже в Ноги
                'accessories': 'Слева'  # Аксессуары в Слева
            }
            
            # Обновляем типы в таблице clothes_shop
            for old_type, new_type in type_mapping.items():
                cursor.execute('UPDATE clothes_shop SET type = ? WHERE type = ?', (new_type, old_type))
                updated = cursor.rowcount
                if updated > 0:
                    print(f"🏆 Обновлено {updated} записей: {old_type} -> {new_type}")
            
            # Проверяем результат
            cursor.execute('SELECT DISTINCT type FROM clothes_shop')
            current_types = [row[0] for row in cursor.fetchall()]
            
            bot.send_message(message.chat.id, 
                           f"🏆 Миграция окончена!\n\n"
                           f"📊 Новые типы в базе:\n" + "\n".join([f"• {t}" for t in current_types]), parse_mode='HTML')
            
    except Exception as e:
        print(f"❌ Ошибка миграции: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

# Функция для просмотра текущих типов
@bot.message_handler(func=lambda message: message.text.lower() == 'типы одежды' and is_admin(message.from_user.id))
def handle_show_types(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        with get_db_cursor() as cursor:
            # Смотрим какие типы сейчас в базе
            cursor.execute('SELECT DISTINCT type, COUNT(*) as count FROM clothes_shop GROUP BY type')
            types = cursor.fetchall()
            
            types_text = "📊 ТИПЫ ОДЕЖДЫ В БАЗЕ:\n\n"
            for type_name, count in types:
                types_text += f"• {type_name}: {count} вещей\n"
            
            bot.send_message(message.chat.id, types_text)
            
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка!", parse_mode='HTML')
# Админ команда для снятия вещи с продажи
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
            # Пытаемся найти вещь по ID или названию
            if item_identifier.isdigit():
                # Поиск по ID
                cursor.execute('SELECT id, name, supply FROM clothes_shop WHERE id = ?', (int(item_identifier),))
            else:
                # Поиск по названию
                cursor.execute('SELECT id, name, supply FROM clothes_shop WHERE name LIKE ?', (f'%{item_identifier}%',))
            
            items = cursor.fetchall()
            
            if not items:
                bot.send_message(message.chat.id, f"❌ Вещь '{item_identifier}' не найдено!", parse_mode='HTML')
                return
            
            if len(items) > 1:
                # Показываем список найденных предметов
                items_text = "📋 Найдено несколько вещей:\n\n"
                for item in items:
                    status = "🟢 В продаже" if item[2] == -1 or item[2] > 0 else "🔴 Снята"
                    items_text += f"• {item[1]} (ID: {item[0]}) - {status}\n"
                items_text += f"\nУточните ID: снять вещь [ID]"
                bot.send_message(message.chat.id, items_text)
                return
            
            # Найден один предмет
            item_id, item_name, current_supply = items[0]
            
            if current_supply == 0:
                bot.send_message(message.chat.id, f"❌ Вещь '{item_name}' уже снята с продажи!", parse_mode='HTML')
                return
            
            # Снимаем вещь с продажи (устанавливаем supply = 0)
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

# Админ команда для возврата вещи в продажу
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
            # Пытаемся найти вещь по ID или названию
            if item_identifier.isdigit():
                # Поиск по ID
                cursor.execute('SELECT id, name, supply FROM clothes_shop WHERE id = ?', (int(item_identifier),))
            else:
                # Поиск по названию
                cursor.execute('SELECT id, name, supply FROM clothes_shop WHERE name LIKE ?', (f'%{item_identifier}%',))
            
            items = cursor.fetchall()
            
            if not items:
                bot.send_message(message.chat.id, f"❌ Вещь '{item_identifier}' не найдено!", parse_mode='HTML')
                return
            
            if len(items) > 1:
                # Показываем список найденных предметов
                items_text = "📋 Найдено несколько вещей:\n\n"
                for item in items:
                    status = "🟢 В продаже" if item[2] == -1 or item[2] > 0 else "🔴 Снята"
                    items_text += f"• {item[1]} (ID: {item[0]}) - {status}\n"
                items_text += f"\nУточните ID: вернуть вещь [ID]"
                bot.send_message(message.chat.id, items_text)
                return
            
            # Найден один предмет
            item_id, item_name, current_supply = items[0]
            
            if current_supply != 0:
                bot.send_message(message.chat.id, f"❌ Вещь '{item_name}' уже в продаже!", parse_mode='HTML')
                return
            
            # Определяем количество для саплая
            if supply_amount:
                supply = int(supply_amount)
                supply_text = f"с лимитом {supply} штук"
            else:
                supply = -1  # Без лимита
                supply_text = "без лимита"
            
            # Возвращаем вещь в продажу
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

# Админ команда для просмотра статуса вещей
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
                
                # Определяем статус
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
        
        # ПРАВИЛЬНЫЙ порядок слоев для отрисовки (от нижнего к верхнему)
        layer_order = ['body', 'legs', 'shoes', 'hat', 'accessories']
        
        for layer in layer_order:
            if layer in equipped:
                clothes_data = equipped[layer]
                
                # Для аксессуаров может быть несколько значений
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
                    # Обычная вещь
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
    
    # Вызываем оригинальную функцию
    equipped = get_equipped_clothes(user_id)
    
    message_text = "🔍 Debug get_equipped_clothes:\n\n"
    message_text += f"📋 Результат: {equipped}\n\n"
    
    # Проверим напрямую через базу
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
    
    # Удаляем старый образ если есть
    old_outfit = f"images/outfit_{user_id}.jpg"
    if os.path.exists(old_outfit):
        os.remove(old_outfit)
        print(f"🗑️ Удален старый образ: {old_outfit}")
    
    # Создаем новый
    outfit_path = create_character_outfit(user_id)
    
    # Показываем результат
    try:
        with open(outfit_path, 'rb') as photo:
            bot.send_photo(message.chat.id, photo, caption="🔄 Образ обновлен!")
    except:
        bot.send_message(message.chat.id, "❌ Ошибка обновления образа", parse_mode='HTML')
# Глобальная переменная для хранения состояния
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
        # Получаем файл фото
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # Сохраняем информацию о фото
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
        
        # Открываем базовое изображение
        base_image = Image.open(base_path).convert("RGBA")
        print(f"🏆 Базовое фото загружено: {base_image.size}")
        
        # Получаем надетую одежду
        equipped = get_equipped_clothes(user_id)
        print(f"🎽 Надета одежда: {equipped}")
        
        if not equipped:
            print("ℹ️ Ничего не надето, возвращаем базовое фото")
            return base_path
        
        # Накладываем одежду
        for item_type, image_name in equipped.items():
            clothes_path = f"images/{image_name}"
            print(f"🔄 Обрабатываем {item_type}: {clothes_path}")
            
            if os.path.exists(clothes_path):
                try:
                    clothes_image = Image.open(clothes_path).convert("RGBA")
                    print(f"🏆 Фото загружено: {clothes_image.size}")
                    print(f"🏆 Режим фото: {clothes_image.mode}")
                    
                    # Проверяем прозрачность
                    if clothes_image.mode != 'RGBA':
                        print(f"❌ Фото не в режиме RGBA: {clothes_image.mode}")
                        continue
                    
                    # Изменяем размер если нужно
                    if clothes_image.size != base_image.size:
                        print(f"📏 Изменяем размер {item_type} с {clothes_image.size} на {base_image.size}")
                        clothes_image = clothes_image.resize(base_image.size, Image.Resampling.LANCZOS)
                    
                    # Накладываем
                    print(f"🔄 Накладываем {item_type}...")
                    base_image = Image.alpha_composite(base_image, clothes_image)
                    print(f"🏆 Успешно наложен {item_type}")
                    
                except Exception as e:
                    print(f"❌ Ошибка наложения {item_type}: {e}")
                    import traceback
                    print(f"❌ Детали ошибки: {traceback.format_exc()}")
            else:
                print(f"❌ Файл отсутствует: {clothes_path}")
        
        # Сохраняем результат
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
from PIL import Image, ImageDraw  # Добавь ImageDraw
import os

@bot.message_handler(func=lambda message: message.text.lower() == 'тест наложения')
def test_overlay(message):
    """Тест наложения изображений"""
    user_id = message.from_user.id
    
    try:
        # Проверяем базовое изображение
        base_path = "images/base_human.jpg"
        base_image = Image.open(base_path)
        
        # Проверяем файл кроссовков
        shoes_path = "images/кроссовки_nike_air_monarch_iv_1763137116.png"
        shoes_image = Image.open(shoes_path)
        
        # Информация о файлах
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
        
        # Пробуем просто показать кроссовки
        try:
            with open(shoes_path, 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption="👟 Вот как выглядят кроссовки")
        except:
            bot.send_message(message.chat.id, "❌ Не могу показать кроссовки", parse_mode='HTML')
            
        # Пробуем создать тестовое наложение
        test_overlay_image(user_id)
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка теста: {e}", parse_mode='HTML')

def test_overlay_image(user_id):
    """Тестовое наложение с простым красным кругом"""
    try:
        from PIL import Image, ImageDraw  # Локальный импорт
        
        base_path = "images/base_human.jpg"
        base_image = Image.open(base_path).convert("RGBA")
        
        # Создаем простой тестовый слой (красный круг)
        test_layer = Image.new('RGBA', base_image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(test_layer)
        
        # Рисуем красный круг на ногах
        draw.ellipse((180, 400, 220, 440), fill=(255, 0, 0, 128))  # Полупрозрачный красный
        
        # Накладываем
        result_image = Image.alpha_composite(base_image, test_layer)
        
        # Сохраняем
        test_path = f"images/test_{user_id}.png"
        result_image.save(test_path, "PNG")
        
        # Показываем результат
        with open(test_path, 'rb') as photo:
            bot.send_photo(user_id, photo, caption="🔴 ТЕСТ: Должен быть красный круг на ногах")
            
    except Exception as e:
        bot.send_message(user_id, f"❌ Ошибка теста: {e}", parse_mode='HTML')
@bot.message_handler(func=lambda message: message.text.lower() == 'исправить кроссовки')
def fix_shoes_filename(message):
    """Исправить имя файла кроссовков в базе"""
    try:
        with get_db_cursor() as cursor:
            # Меняем имя файла на правильное
            cursor.execute(
                'UPDATE clothes_shop SET image_name = ? WHERE image_name = ?', 
                ('sneakers.png', 'кроссовки_nike_air_monarch_iv_1763137116.png')
            )
            
            # Проверяем что изменилось
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
    
    # Завершаем создание
    finish_clothes_creation(message)

def finish_clothes_creation(message):
    """Завершить создание одежды и добавить в магазин"""
    try:
        user_id = message.from_user.id
        data = clothes_creation_state[user_id]
        
        # Генерируем уникальное имя файла
        file_extension = "png"  # Все вещи сохраняем как PNG для прозрачности
        filename = f"{data['name'].lower().replace(' ', '_')}_{int(time.time())}.{file_extension}"
        file_path = f"images/{filename}"
        
        # Сохраняем фото
        with open(file_path, 'wb') as f:
            f.write(data['photo'])
        
        # Добавляем в базу данных
        with get_db_cursor() as cursor:
            cursor.execute(
                'INSERT INTO clothes_shop (name, price, type, image_name) VALUES (?, ?, ?, ?)',
                (data['name'], data['price'], data['type'], filename)
            )
            item_id = cursor.lastrowid
        
        # Показываем результат
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
        
        # Показываем превью
        try:
            with open(file_path, 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption=result_text, reply_markup=markup, parse_mode='HTML')
        except:
            bot.send_message(message.chat.id, result_text, reply_markup=markup)
        
        # Очищаем состояние
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
 # Добавь в начало с другими импортами
from telebot.types import InputFile

# Глобальная переменная для хранения состояния
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
        # Получаем файл фото
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # Сохраняем информацию о фото
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
    
    # Завершаем создание
    finish_clothes_creation(message)

def finish_clothes_creation(message):
    """Завершить создание одежды и добавить в магазин"""
    try:
        user_id = message.from_user.id
        data = clothes_creation_state[user_id]
        
        # Генерируем уникальное имя файла
        file_extension = "png"  # Все вещи сохраняем как PNG для прозрачности
        filename = f"{data['name'].lower().replace(' ', '_')}_{int(time.time())}.{file_extension}"
        file_path = f"images/{filename}"
        
        # Сохраняем фото
        with open(file_path, 'wb') as f:
            f.write(data['photo'])
        
        # Добавляем в базу данных
        with get_db_cursor() as cursor:
            cursor.execute(
                'INSERT INTO clothes_shop (name, price, type, image_name) VALUES (?, ?, ?, ?)',
                (data['name'], data['price'], data['type'], filename)
            )
            item_id = cursor.lastrowid
        
        # Показываем результат
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
        
        # Показываем превью
        try:
            with open(file_path, 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption=result_text, reply_markup=markup, parse_mode='HTML')
        except:
            bot.send_message(message.chat.id, result_text, reply_markup=markup)
        
        # Очищаем состояние
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
               
# Обработчик команды "кости" с ответом на сообщение
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

# Обработчик принятия вызова
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
        
        # Списываем ставки
        update_balance(challenger_id, -bet_amount)
        update_balance(target_id, -bet_amount)
        
        # Бросаем по 1 кубику каждому
        bot.edit_message_text("🎲 Бросаем кости...", call.message.chat.id, call.message.message_id)
        
        dice1 = bot.send_dice(call.message.chat.id, emoji='🎲')
        time.sleep(2)
        dice2 = bot.send_dice(call.message.chat.id, emoji='🎲')
        
        time.sleep(2)
        
        challenger_score = dice1.dice.value
        target_score = dice2.dice.value
        
        # Определяем победителя
        if challenger_score > target_score:
            winner_id = challenger_id
            win_amount = bet_amount * 2
            result_text = f"🎉 Победил {get_user_info(challenger_id)['custom_name'] or get_user_info(challenger_id)['first_name']}"
        elif target_score > challenger_score:
            winner_id = target_id
            win_amount = bet_amount * 2
            result_text = f"🎉 Победил {get_user_info(target_id)['custom_name'] or get_user_info(target_id)['first_name']}"
        else:
            # Ничья - возвращаем деньги
            update_balance(challenger_id, bet_amount)
            update_balance(target_id, bet_amount)
            result_text = "🤝 Ничья"
            win_amount = 0
        
        if win_amount > 0:
            update_balance(winner_id, win_amount)
        
        # Удаляем кубики через 2сы
        time.sleep(2)
        bot.delete_message(call.message.chat.id, dice1.message_id)
        bot.delete_message(call.message.chat.id, dice2.message_id)
        
        # Обновляем сообщение с результатом
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


# Обработчик отмены понижения
@bot.callback_query_handler(func=lambda call: call.data == "cancel_demote")
def cancel_demote_member(call):
    bot.edit_message_text(
        "❌ Понижение сброшено",
        call.message.chat.id,
        call.message.message_id
    , parse_mode='HTML')
    bot.answer_callback_query(call.id, "🏆 Отменено")
    
# Функции для форматирования текста игр
def format_game_win_text(username, win_amount, balance):
    """Форматирование текста выигрыша для игр"""
    return f"<blockquote>🎉 <b>{username}</b> выиграл {format_balance(win_amount)}!\n💵 Средства: {format_balance(balance)}</blockquote>"

def format_game_lose_text(username, lose_amount, balance):
    """Форматирование текста проигрыша для игр"""
    return f"<blockquote>😢 <b>{username}</b> проиграл {format_balance(lose_amount)}!\n💵 Средства: {format_balance(balance)}</blockquote>"

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



# Функция для обновления баланса с автоматической статистикой
def update_balance(user_id, amount):
    with get_db_cursor() as cursor:
        # Получаем старый баланс для определения выигрыша/проигрыша
        cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
        old_balance = cursor.fetchone()[0]
        
        # Обновляем баланс
        cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
        
        # Автоматически обновляем статистику игр
        if amount > 0:
            # Это выигрыш
            cursor.execute('UPDATE users SET games_won = games_won + 1, total_won_amount = total_won_amount + ? WHERE user_id = ?', 
                          (amount, user_id))
        elif amount < 0:
            # Это проигрыш (бет)
            cursor.execute('UPDATE users SET games_lost = games_lost + 1, total_lost_amount = total_lost_amount + ? WHERE user_id = ?', 
                          (abs(amount), user_id))

# Админ команды для выдачи валюты (без логов)
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
        
        # Выдаем деньги
        update_balance(target_user_id, amount)
        
        bot.send_message(message.chat.id, 
                       f"🏆 Успешно выдано {format_balance(amount)} пользователю {target_first_name} (@{target_username})", parse_mode='HTML')
        
        # Уведомляем получателя
        try:
            bot.send_message(target_user_id, 
                           f"🎉 Администратор выдал вам {format_balance(amount)}!\n💳 Твой баланс: {format_balance(get_balance(target_user_id))}", parse_mode='HTML')
        except:
            pass
            
    except Exception as e:
        print(f"Ошибка при выдаче денег: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при выдаче денег!", parse_mode='HTML')

# Админ команда для снятия валюты
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
        
        # Проверяем баланс пользователя
        target_balance = get_balance(target_user_id)
        if target_balance < amount:
            bot.send_message(message.chat.id, f"❌ У пользователя недостаточно средств! Средства: {format_balance(target_balance)}", parse_mode='HTML')
            return
        
        # Снимаем деньги
        update_balance(target_user_id, -amount)
        
        bot.send_message(message.chat.id, 
                       f"🏆 Успешно снято {format_balance(amount)} у пользователя {target_first_name} (@{target_username})", parse_mode='HTML')
        
        # Уведомляем пользователя
        try:
            bot.send_message(target_user_id, 
                           f"⚠️ Администратор снял с вас {format_balance(amount)}!\n💳 Твой баланс: {format_balance(get_balance(target_user_id))}", parse_mode='HTML')
        except:
            pass
            
    except Exception as e:
        print(f"Ошибка при снятии денег: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при снятии денег!", parse_mode='HTML')

# Админ команда для установки баланса
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
        
        # Получаем текущий баланс
        current_balance = get_balance(target_user_id)
        
        # Устанавливаем новый баланс
        with get_db_cursor() as cursor:
            cursor.execute('UPDATE users SET balance = ? WHERE user_id = ?', (amount, target_user_id))
        
        bot.send_message(message.chat.id, 
                       f"🏆 Баланс пользователя {target_first_name} (@{target_username}) установлен:\n"
                       f"📊 Было: {format_balance(current_balance)}\n"
                       f"📈 Стало: {format_balance(amount)}", parse_mode='HTML')
        
        # Уведомляем пользователя
        try:
            bot.send_message(target_user_id, 
                           f"⚡ Администратор установил твой баланс: {format_balance(amount)}!", parse_mode='HTML')
        except:
            pass
            
    except Exception as e:
        print(f"Ошибка при установке баланса: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при установке баланса!", parse_mode='HTML')

# Админ команда для проверки баланса пользователя
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
        message_text += f"💵 Средства: {format_balance(balance)}\n"
        message_text += f"🏛 Депозит: {format_balance(bank_deposit)}\n"
        message_text += f"🎁 Общий капитал: {format_balance(balance + bank_deposit)}\n"
        message_text += f"⚡ Майнеров: {video_cards}\n"
        message_text += f"🖱 Кликов: {total_clicks}\n"
        
        # Информация о бизнесе
        business_info = get_user_business(target_user_id)
        if business_info:
            message_text += f"🏭 Предприятие: {business_info['name']}\n"
            message_text += f"📦 Ресурсы: {business_info['raw_materials']}/{business_info['storage_capacity']}\n"
        
        # Информация о клане
        user_clan = get_user_clan(target_user_id)
        if user_clan:
            message_text += f"⚔️ Гильдия: {user_clan['name']} [{user_clan['tag']}]\n"
            message_text += f"🎖 Статус: {user_clan['role']}\n"
        
        bot.send_message(message.chat.id, message_text)
        
    except Exception as e:
        print(f"Ошибка при проверке баланса: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при проверке баланса!", parse_mode='HTML')



      
# Система налогов
TAX_CONFIG = {
    "high_wealth_tax": 0.10,  # 10% для баланса > 1кккк
    "high_wealth_threshold": 100000,  # 1кккк
    "medium_wealth_tax": 0.05,  # 5% для баланса > 100кке
    "medium_wealth_threshold": 10000,  # 100кке
    "general_tax": 0.03,  # 3% для всех у кого есть деньги
    "min_tax_amount": 1  # Минимальная сумма для налога (1М)
}

def collect_taxes():
    """Собрать налоги со всех пользователей"""
    try:
        with get_db_cursor() as cursor:
            # Получаем всех пользователей с балансом > 0
            cursor.execute('''
                SELECT user_id, username, first_name, custom_name, balance, bank_deposit 
                FROM users 
                WHERE balance > 0 OR bank_deposit > 0
            ''')
            users = cursor.fetchall()
            
            total_collected = 0
            tax_report = []
            affected_users = 0
            
            for user in users:
                user_id, username, first_name, custom_name, balance, bank_deposit = user
                total_wealth = balance + bank_deposit
                
                if total_wealth <= 0:
                    continue
                
                tax_rate = 0
                tax_reason = ""
                
                # Определяем ставку налога
                if total_wealth >= TAX_CONFIG["high_wealth_threshold"]:
                    tax_rate = TAX_CONFIG["high_wealth_tax"]
                    tax_reason = "10% (богачи >1кккк)"
                elif total_wealth >= TAX_CONFIG["medium_wealth_threshold"]:
                    tax_rate = TAX_CONFIG["medium_wealth_tax"]
                    tax_reason = "5% (состоятельные >100кке)"
                elif total_wealth > TAX_CONFIG["min_tax_amount"]:
                    tax_rate = TAX_CONFIG["general_tax"]
                    tax_reason = "3% (общий налог)"
                else:
                    continue  # Пропускаем если сумма меньше минимальной
                
                # Рассчитываем налог (только с баланса, не с вклада)
                tax_amount = int(balance * tax_rate)
                
                if tax_amount < TAX_CONFIG["min_tax_amount"]:
                    continue  # Пропускаем если налог меньше минимального
                
                # Списываем налог
                if balance >= tax_amount:
                    new_balance = balance - tax_amount
                    cursor.execute('UPDATE users SET balance = ? WHERE user_id = ?', (new_balance, user_id))
                    
                    total_collected += tax_amount
                    affected_users += 1
                    
                    # Добавляем в отчет
                    display_name = custom_name if custom_name else (f"@{username}" if username else first_name)
                    tax_report.append({
                        'user': display_name,
                        'wealth': total_wealth,
                        'tax': tax_amount,
                        'rate': tax_reason
                    })
                    
                    # Уведомляем пользователя
                    try:
                        bot.send_message(
                            user_id,
                            f"🏛️ <b>НАЛОГОВЫЙ СБОР</b>\n\n"
                            f"💵 С вашего баланса списан налог:\n"
                            f"💸 Сумма: {format_balance(tax_amount)}\n"
                            f"📊 Ставка: {tax_reason}\n"
                            f"💳 Было: {format_balance(balance)}\n"
                            f"💳 Стало: {format_balance(new_balance)}\n\n"
                            f"🏛 Общее состояние: {format_balance(total_wealth)}",
                            parse_mode='HTML'
                        )
                    except:
                        pass  # Пользователь заблокировал бота
            
            return {
                'success': True,
                'total_collected': total_collected,
                'affected_users': affected_users,
                'tax_report': tax_report
            }
            
    except Exception as e:
        print(f"❌ Ошибка при сборе налогов: {e}")
        return {
            'success': False,
            'error': str(e)
        }

def get_wealth_stats():
    """Получить статистику по богатству пользователей"""
    try:
        with get_db_cursor() as cursor:
            # Общая статистика
            cursor.execute('''
                SELECT 
                    COUNT(*) as total_users,
                    SUM(balance + bank_deposit) as total_wealth,
                    AVG(balance + bank_deposit) as avg_wealth,
                    SUM(balance) as total_balance,
                    SUM(bank_deposit) as total_deposits
                FROM users
            ''')
            stats = cursor.fetchone()
            
            # Статистика по категориям богатства
            cursor.execute('''
                SELECT 
                    COUNT(*) as count,
                    SUM(balance + bank_deposit) as total_wealth
                FROM users 
                WHERE balance + bank_deposit >= ?
            ''', (TAX_CONFIG["high_wealth_threshold"],))
            high_wealth = cursor.fetchone()
            
            cursor.execute('''
                SELECT 
                    COUNT(*) as count,
                    SUM(balance + bank_deposit) as total_wealth
                FROM users 
                WHERE balance + bank_deposit >= ? AND balance + bank_deposit < ?
            ''', (TAX_CONFIG["medium_wealth_threshold"], TAX_CONFIG["high_wealth_threshold"]))
            medium_wealth = cursor.fetchone()
            
            cursor.execute('''
                SELECT 
                    COUNT(*) as count,
                    SUM(balance + bank_deposit) as total_wealth
                FROM users 
                WHERE balance + bank_deposit > ? AND balance + bank_deposit < ?
            ''', (TAX_CONFIG["min_tax_amount"], TAX_CONFIG["medium_wealth_threshold"]))
            low_wealth = cursor.fetchone()
            
            # Топ-10 самых богатых
            cursor.execute('''
                SELECT user_id, username, first_name, custom_name, balance, bank_deposit 
                FROM users 
                WHERE balance + bank_deposit > 0
                ORDER BY balance + bank_deposit DESC 
                LIMIT 10
            ''')
            top_rich = cursor.fetchall()
            
            return {
                'total_users': stats[0],
                'total_wealth': stats[1] or 0,
                'avg_wealth': stats[2] or 0,
                'total_balance': stats[3] or 0,
                'total_deposits': stats[4] or 0,
                'high_wealth': {
                    'count': high_wealth[0],
                    'total': high_wealth[1] or 0
                },
                'medium_wealth': {
                    'count': medium_wealth[0],
                    'total': medium_wealth[1] or 0
                },
                'low_wealth': {
                    'count': low_wealth[0],
                    'total': low_wealth[1] or 0
                },
                'top_rich': top_rich
            }
            
    except Exception as e:
        print(f"❌ Ошибка при получении статистики: {e}")
        return None

@bot.message_handler(func=lambda message: message.text.lower().startswith('собрать налог') and is_admin(message.from_user.id))
def handle_collect_tax(message):
    """Собрать налоги (админ команда)"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        # Показываем подтверждение
        tax_keyboard = {
            "inline_keyboard": [[
                {"text": "✅ ДА, СОБРАТЬ НАЛОГИ", "callback_data": "confirm_tax_collection", "style": "success"},
                {"text": "❌ Отмена", "callback_data": "cancel_tax_collection", "style": "secondary"}
            ]]
        }
        
        stats = get_wealth_stats()
        if not stats:
            bot.send_message(message.chat.id, "❌ Ошибка получения статистики!", parse_mode='HTML')
            return
        
        message_text = "🏛️ <b>СБОР НАЛОГОВ</b>\n\n"
        message_text += f"<blockquote>📊 Статистика экономики:\n"
        message_text += f"👥 Всего пользователей: {stats['total_users']}\n"
        message_text += f"💵 Общее богатство: {format_balance(stats['total_wealth'])}\n"
        message_text += f"💳 На балансах: {format_balance(stats['total_balance'])}\n"
        message_text += f"🏛 На вкладах: {format_balance(stats['total_deposits'])}</blockquote>\n\n"
        
        message_text += f"📈 <b>Категории налогообложения:</b>\n"
        message_text += f"• Богачи (&gt;1кккк): {stats['high_wealth']['count']} чел. — 10%\n"
        message_text += f"• Состоятельные (&gt;100кке): {stats['medium_wealth']['count']} чел. — 5%\n"
        message_text += f"• Все остальные: {stats['low_wealth']['count']} чел. — 3%\n\n"
        
        message_text += f"⚠️ <b>Налог взимается только с баланса (не с вкладов)!</b>"
        
        bot.send_message(message.chat.id, message_text, reply_markup=tax_keyboard, parse_mode='HTML')
        
    except Exception as e:
        print(f"❌ Ошибка в handle_collect_tax: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data == "confirm_tax_collection")
def handle_confirm_tax_collection(call):
    """Подтверждение сбора налогов"""
    try:
        user_id = call.from_user.id
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "❌ Недостаточно прав!")
            return
        
        # Начинаем сбор налогов
        bot.edit_message_text(
            text="🔄 Собираем налоги...",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
        
        result = collect_taxes()
        
        if result['success']:
            message_text = "🏛️ <b>НАЛОГИ СОБРАНЫ</b>\n\n"
            message_text += f"💵 Собрано всего: {format_balance(result['total_collected'])}\n"
            message_text += f"👥 Затронуто пользователей: {result['affected_users']}\n\n"
            
            if result['tax_report']:
                message_text += "📋 <b>Крупнейшие налогоплательщики:</b>\n"
                for i, taxpayer in enumerate(result['tax_report'][:10], 1):
                    message_text += f"{i}. {taxpayer['user']} - {format_balance(taxpayer['tax'])} ({taxpayer['rate']})\n"
            
            bot.edit_message_text(
                text=message_text,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML'
            )
            bot.answer_callback_query(call.id, "🏆 Налоги собраны!")
        else:
            bot.edit_message_text(
                text=f"❌ Ошибка при сборе налогов: {result['error']}",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML'
            )
            bot.answer_callback_query(call.id, "❌ Ошибка!")
        
    except Exception as e:
        print(f"❌ Ошибка в handle_confirm_tax_collection: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка!")

@bot.callback_query_handler(func=lambda call: call.data == "cancel_tax_collection")
def handle_cancel_tax_collection(call):
    """Отмена сбора налогов"""
    bot.edit_message_text(text="❌ Сбор налогов сброшен", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML')
    bot.answer_callback_query(call.id, "🏆 Отменено")

@bot.message_handler(func=lambda message: message.text.lower() == 'стата налогов' and is_admin(message.from_user.id))
def handle_tax_stats(message):
    """Показать статистику для налогообложения"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        stats = get_wealth_stats()
        if not stats:
            bot.send_message(message.chat.id, "❌ Ошибка получения статистики!", parse_mode='HTML')
            return
        
        message_text = "🏛️ <b>СТАТИСТИКА НАЛОГООБЛОЖЕНИЯ</b>\n\n"
        
        message_text += f"📊 <b>Общая экономика:</b>\n"
        message_text += f"👥 Пользователей: {stats['total_users']}\n"
        message_text += f"💵 Всего богатства: {format_balance(stats['total_wealth'])}\n"
        message_text += f"📈 Среднее на человека: {format_balance(int(stats['avg_wealth']))}\n\n"
        
        message_text += f"💸 <b>Распределение по категориям:</b>\n"
        message_text += f"• 🏆 Богачи (>1кккк): {stats['high_wealth']['count']} чел. ({format_balance(stats['high_wealth']['total'])})\n"
        message_text += f"• 💵 Состоятельные (>100кке): {stats['medium_wealth']['count']} чел. ({format_balance(stats['medium_wealth']['total'])})\n"
        message_text += f"• 👨‍💼 Обычные: {stats['low_wealth']['count']} чел. ({format_balance(stats['low_wealth']['total'])})\n\n"
        
        message_text += f"📈 <b>Потенциальный сбор налогов:</b>\n"
        # Расчет потенциального сбора
        high_tax = int(stats['high_wealth']['total'] * TAX_CONFIG["high_wealth_tax"])
        medium_tax = int(stats['medium_wealth']['total'] * TAX_CONFIG["medium_wealth_tax"]) 
        low_tax = int(stats['low_wealth']['total'] * TAX_CONFIG["general_tax"])
        total_potential = high_tax + medium_tax + low_tax
        
        message_text += f"• Богачи (10%): ~{format_balance(high_tax)}\n"
        message_text += f"• Состоятельные (5%): ~{format_balance(medium_tax)}\n"
        message_text += f"• Обычные (3%): ~{format_balance(low_tax)}\n"
        message_text += f"💵 Всего: ~{format_balance(total_potential)}\n\n"
        
        message_text += f"🏆 <b>Топ-5 самых богатых:</b>\n"
        for i, (user_id, username, first_name, custom_name, balance, bank_deposit) in enumerate(stats['top_rich'][:5], 1):
            display_name = custom_name if custom_name else (f"@{username}" if username else first_name)
            total_wealth = balance + bank_deposit
            message_text += f"{i}. {display_name} - {format_balance(total_wealth)}\n"
        
        bot.send_message(message.chat.id, message_text, parse_mode='HTML')
        
    except Exception as e:
        print(f"❌ Ошибка в handle_tax_stats: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text.lower() == 'налог' and is_admin(message.from_user.id))
def handle_tax_help(message):
    """Показать справку по налогам"""
    if not is_admin(message.from_user.id):
        return
    
    help_text = """🏛️ <b>СИСТЕМА НАЛОГООБЛОЖЕНИЯ</b>

📋 <b>Ставки налогов:</b>
• 10% - для богачей (>1кккк общего богатства)
• 5% - для состоятельных (>100кке общего богатства)  
• 3% - для всех остальных (если есть деньги)

⚡ <b>Особенности:</b>
• Налог взимается только с баланса (не с вкладов)
• Минимальный налог: 1М
• Пользователи уведомляются о списании

⚔️ <b>Команды админа:</b>
<code>собрать налог</code> - запустить сбор налогов
<code>стата налогов</code> - статистика налогообложения
<code>налог</code> - эта справка

💡 <b>Примечание:</b>
Налоги помогают бороться с инфляцией и поддерживать экономический баланс!"""

    bot.send_message(message.chat.id, help_text, parse_mode='HTML')

print("🏆 Налоговая бот загружена!")
# Обработчик транзакцияа денег с подтверждением (по ответу или юзернейму)
# Обработчик перевода денег с подтверждением (по ответу или юзернейму)
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
            # кинуть 1000 — ответом на сообщение
            if len(parts) < 2:
                bot.send_message(message.chat.id, "❌ Укажи сумму: <code>кинуть 1000</code> ответом на сообщение", parse_mode='HTML')
                return
            target_user_id = message.reply_to_message.from_user.id
            amount_text = parts[1]
        else:
            # кинуть @username 1000  или  кинуть 123456789 1000
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

        # Получатель — ищем в БД, если нет — ищем из reply
        target_info = get_user_info(target_user_id)
        if not target_info:
            # Попробуем достать имя из reply напрямую
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

        # Сумма
        user_balance = get_balance(user_id)
        amount = parse_bet_amount(amount_text, user_balance)
        if not amount or amount <= 0:
            bot.send_message(message.chat.id, "❌ Неверная сумма")
            return
        if amount > user_balance:
            bot.send_message(message.chat.id, f"❌ Недостаточно монет. Баланс: {format_balance(user_balance)}")
            return

        # Комиссия
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

# Обработчик подтверждения перевода
@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_transfer_'))
def handle_confirm_transfer(call):
    try:
        data_parts = call.data.split('_')
        sender_id = int(data_parts[2])
        target_user_id = int(data_parts[3])
        amount = int(data_parts[4])
        
        # Проверяем, что нажал именно отправитель
        if call.from_user.id != sender_id:
            bot.answer_callback_query(call.id, "❌ Только отправитель может подтвердить перевод!", show_alert=True)
            return
        
        user_balance = get_balance(sender_id)
        if user_balance < amount:
            bot.answer_callback_query(call.id, "❌ Не хватает монет")
            return
        
        # Рассчитываем комиссию и сумму получения
        fee = int(amount * TRANSFER_FEE)
        receive_amount = amount - fee
        
        # Выполняем перевод
        success, result_message = transfer_money(sender_id, target_user_id, amount)
        
        if success:
            add_experience(sender_id, amount // 100000)
            
            # Обновляем сообщение
            result_text = f"✅ <b>Перевод выполнен</b>\n\n"
            result_text += f"<blockquote>Переведено: {format_balance(receive_amount)}\n"
            result_text += f"Комиссия: {format_balance(fee)}</blockquote>"
            
            bot.edit_message_text(
                result_text, 
                call.message.chat.id, 
                call.message.message_id,
                parse_mode='HTML'
            )
            
            # Уведомляем получателя
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

# Обработчик отмены перевода
@bot.callback_query_handler(func=lambda call: call.data.startswith('cancel_transfer_'))
def handle_cancel_transfer(call):
    try:
        data_parts = call.data.split('_')
        sender_id = int(data_parts[2])
        
        # Проверяем, что нажал именно отправитель
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
# Обработчик для изменения цены одежды (только для админов)
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
        
        # Объединяем название одежды (может состоять из нескольких слов)
        item_name = ' '.join(parts[2:-1])
        new_price_text = parts[-1]
        
        # Проверяем новую цену
        try:
            new_price = int(new_price_text)
            if new_price < 0:
                bot.send_message(message.chat.id, "❌ Цена не может быть отрицательной!", parse_mode='HTML')
                return
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверная стоимость! Используйте только цифры", parse_mode='HTML')
            return
        
        # Ищем одежду в магазине
        with get_db_cursor() as cursor:
            cursor.execute('SELECT id, name, price FROM clothes_shop WHERE name LIKE ?', (f'%{item_name}%',))
            items = cursor.fetchall()
            
            if not items:
                bot.send_message(message.chat.id, f"❌ Одежда '{item_name}' отсутствуета в магазине!", parse_mode='HTML')
                return
            
            if len(items) > 1:
                # Показываем список найденных предметов
                items_text = "📋 Найдено несколько предметов:\n\n"
                for item in items:
                    items_text += f"• {item[1]} (ID: {item[0]}) - {format_balance(item[2])}\n"
                items_text += f"\nУточните название или используйте ID: изменить цену [ID] [новая стоимость]"
                bot.send_message(message.chat.id, items_text)
                return
            
            # Найден один предмет
            item_id, item_name, old_price = items[0]
            
            # Обновляем цену
            cursor.execute('UPDATE clothes_shop SET price = ? WHERE id = ?', (new_price, item_id))
            
            bot.send_message(message.chat.id, 
                           f"🏆 Цена изменена для {item_name}!\n"
                           f"💵 Было: {format_balance(old_price)}\n"
                           f"💵 Стало: {format_balance(new_price)}", parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка при изменении цены: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при изменении цены!", parse_mode='HTML')

# Обработчик для изменения цены по ID (альтернативный вариант)
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
        
        # Проверяем ID
        try:
            item_id = int(item_id_text)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверный ID! Используйте только цифры", parse_mode='HTML')
            return
        
        # Проверяем новую цену
        try:
            new_price = int(new_price_text)
            if new_price < 0:
                bot.send_message(message.chat.id, "❌ Цена не может быть отрицательной!", parse_mode='HTML')
                return
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверная стоимость! Используйте только цифры", parse_mode='HTML')
            return
        
        # Ищем одежду по ID
        with get_db_cursor() as cursor:
            cursor.execute('SELECT name, price FROM clothes_shop WHERE id = ?', (item_id,))
            item = cursor.fetchone()
            
            if not item:
                bot.send_message(message.chat.id, f"❌ Одежда с ID {item_id} не найдено!", parse_mode='HTML')
                return
            
            item_name, old_price = item
            
            # Обновляем цену
            cursor.execute('UPDATE clothes_shop SET price = ? WHERE id = ?', (new_price, item_id))
            
            bot.send_message(message.chat.id, 
                           f"🏆 Цена изменена для {item_name} (ID: {item_id})!\n"
                           f"💵 Было: {format_balance(old_price)}\n"
                           f"💵 Стало: {format_balance(new_price)}", parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка при изменении цены по ID: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при изменении цены!", parse_mode='HTML')


# Команда для загрузки фото в папку images
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

# Обработчик загрузки фото (отдельный от документов)
@bot.message_handler(content_types=['photo'], func=lambda message: is_admin(message.from_user.id))
def handle_photo_upload_images(message):
    """Сохраняет фото в папку images"""
    try:
        # Получаем файл фото
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # Определяем расширение по MIME типу (если доступно) или по умолчанию .jpg
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
        
        # Определяем имя файла
        if message.caption and message.caption.strip():
            # Используем подпись как имя файла
            filename = message.caption.strip()
            # Добавляем расширение если его нет
            if not filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                filename += default_ext
        else:
            # Генерируем имя на основе времени
            filename = f"photo_{int(time.time())}{default_ext}"
        
        # Путь для сохранения
        save_path = f"images/{filename}"
        
        # Создаем папку если её нет
        os.makedirs("images", exist_ok=True)
        
        # Сохраняем файл
        with open(save_path, 'wb') as new_file:
            new_file.write(downloaded_file)
        
        # Проверяем размер файла
        file_size = os.path.getsize(save_path)
        size_kb = file_size / 1024
        
        # Отправляем подтверждение
        success_text = (
            f"✅ <b>Фото сохранено!</b>\n\n"
            f"📁 Путь: <code>{save_path}</code>\n"
            f"📄 Имя: {filename}\n"
            f"📊 Размер: {size_kb:.1f} KB\n"
            f"📌 Расширение: {default_ext}\n\n"
            f"💡 Теперь можно использовать в магазине: <code>{filename}</code>"
        )
        
        # Показываем превью
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
        # Обработчик кнопки "Игры" или команды "игры"
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

@bot.message_handler(func=lambda message: message.text.lower().startswith('краш'))
def crash_command(message):
    user_id = message.from_user.id
    
    # Проверяем формат команды
    parts = message.text.split()
    if len(parts) != 3:
        bot.send_message(
            message.chat.id,
            "📌 Формат: `краш X.XX сумма`\nПример: `краш 2.00 100к`",
            parse_mode='Markdown'
        )
        return
    
    try:
        # Парсим множитель
        multiplier = float(parts[1])
        if multiplier < 1.10 or multiplier > 10:
            bot.send_message(message.chat.id, "⚠️ Множитель: 1.10 - 10.00x")
            return
        
        # Парсим ставку
        bet_amount = parse_bet_amount(parts[2], get_balance(user_id))
        
        if bet_amount is None:
            bot.send_message(message.chat.id, "💀 Неверный формат суммы")
            return
        
        # Минимальная бет
        if bet_amount < 50:
            bot.send_message(message.chat.id, f"💵 Мин. бет: 50$")
            return
        
        # Проверяем баланс
        balance = get_balance(user_id)
        if bet_amount > balance:
            bot.send_message(message.chat.id, f"💀 Не хватает монет")
            return
        
        # Списываем ставку
        update_balance(user_id, -bet_amount)
        
        # Регистрируем активную ставку
        active_crash_bets[user_id] = {
            'bet': bet_amount,
            'multiplier': multiplier,
            'chat_id': message.chat.id,
            'started_at': time.time()
        }
        
        # Формируем текст с HTML для премиум-эмодзи
        crash_text = (
            f"🎰 <b>Игра 'Краш' началась!</b>\n\n"
            f"<blockquote>"
            f"⚔️ Множитель: <b>{multiplier:.2f}x</b>\n"
            f"💵 Ставка: {format_balance(bet_amount)}"
            f"</blockquote>\n\n"#
            f"📩 Результат придет в личные сообщения"
        )
        
        # Отправляем подтверждение
        bot.send_message(
            message.chat.id,
            crash_text,
            parse_mode='HTML'
        )
        
        # Запускаем таймер игры
        thread = threading.Thread(
            target=process_crash_game_ls_only,
            args=(user_id, bet_amount, multiplier),
            daemon=True
        )
        thread.start()
        
    except ValueError:
        bot.send_message(message.chat.id, "💀 Неверный формат команды")

# ── Принудительный краш (только для админов) ──
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
    # Убираем из активных
    active_crash_bets.pop(user_id, None)
    
    # Генерируем уникальный ID игры
    game_id = f"{user_id}_{int(time.time() * 1000)}"
    
    # Сохраняем игру в ожидании решения админа
    pending_crash_decisions[game_id] = {
        'user_id': user_id,
        'user_name': '',
        'bet': bet_amount,
        'target_mult': target_multiplier,
        'admin_msg_id': None,
        'timestamp': time.time(),
        'decided': False
    }
    
    # Получаем имя игрока
    try:
        user_info = bot.get_chat(user_id)
        user_name = user_info.first_name or str(user_id)
        if user_info.username:
            user_name = f"{user_name} (@{user_info.username})"
    except:
        user_name = str(user_id)
    
    pending_crash_decisions[game_id]['user_name'] = user_name
    
    # Отправляем в чат админов для решения
    decision_text = (
        f"⚡ *БЫСТРОЕ РЕШЕНИЕ*\n\n"
        f"👤 Игрок: {user_name} (ID: {user_id})\n"
        f"🎰 Игра: Краш\n"
        f"⚔️ Множитель ставки: {target_multiplier:.2f}x\n"
        f"💵 Ставка: {int(bet_amount)} 🌸\n\n"
        f"⏰ У вас есть 5 сек на решение!\n"
        f"Если не ответите - рандом результат"
    )
    
    # Кнопки для админа
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Выигрыш", callback_data=f"crash_win_{game_id}"),
        InlineKeyboardButton("❌ Проигрыш", callback_data=f"crash_lose_{game_id}")
    )
    
    # Отправляем в чат админов
    try:
        msg = bot.send_message(CRASH_ADMIN_CHAT, decision_text, reply_markup=markup)
        pending_crash_decisions[game_id]['admin_msg_id'] = msg.message_id
    except Exception as e:
        print(f"Ошибка отправки в чат админов: {e}")
        # Чат недоступен — всё равно ждём 5 сек через таймер
    
    # Таймер на 5 сек - если админ не решил, рандом результат
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
        send_crash_result(game_id, None)  # None = рандом


def send_crash_result(game_id, admin_decision):
    """Отправляет результат игроку в ЛС"""
    if game_id not in pending_crash_decisions:
        return
    
    game = pending_crash_decisions[game_id]
    
    # Защита от двойного вызова
    if game.get('result_sent'):
        return
    game['result_sent'] = True
    user_id = game['user_id']
    bet = game['bet']
    target_mult = game['target_mult']
    admin_msg_id = game['admin_msg_id']
    
    # Определяем результат
    if admin_decision is None:
        # Рандом результат
        win_chance = 0.42 / (target_mult ** 0.55)
        won = random.random() < win_chance
    else:
        # Решение админа
        won = admin_decision  # True = выигрыш, False = проигрыш
    
    # Генерируем множитель краша исходя из результата
    if won:
        # Краш улетел ВЫШЕ target_mult — игрок выиграл (успел забрать)
        result_multiplier = round(random.uniform(target_mult + 0.01, target_mult + random.uniform(0.5, 10.0)), 2)
    else:
        # Краш упал НИЖЕ target_mult — игрок проиграл
        result_multiplier = round(random.uniform(1.01, max(1.02, target_mult - 0.01)), 2)
    
    # Считаем результат
    win_amount = 0
    if won:
        win_amount = int(bet * target_mult)
        update_balance(user_id, win_amount)
        profit = win_amount - bet
        add_experience(user_id, int(50 + bet // 10000))  # опыт за выигрыш
    else:
        profit = -bet
        add_experience(user_id, int(20 + bet // 50000))  # опыт даже за проигрыш
    
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

    # Отправляем результат в ЛС игроку
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
    
    # Обновляем сообщение в чате админов
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
    
    # Удаляем из ожидающих
    pending_crash_decisions.pop(game_id, None)


# ═════ ОБРАБОТЧИКИ КНОПОК КРАША ═════

@bot.callback_query_handler(func=lambda call: call.data.startswith('crash_win_'))
def handle_crash_win(call):
    """Админ решил что игрок ВЫИГРЫВАЕТ"""
    game_id = call.data.replace('crash_win_', '')
    
    if game_id not in pending_crash_decisions:
        bot.answer_callback_query(call.id, "❌ Игра не найдена", show_alert=True)
        return
    
    pending_crash_decisions[game_id]['decided'] = True
    send_crash_result(game_id, True)  # True = выигрыш
    
    bot.answer_callback_query(call.id, "✅ Выигрыш записан!", show_alert=False)
    # edit_message_reply_markup убран — send_crash_result уже редактирует сообщение


@bot.callback_query_handler(func=lambda call: call.data.startswith('crash_lose_'))
def handle_crash_lose(call):
    """Админ решил что игрок ПРОИГРЫВАЕТ"""
    game_id = call.data.replace('crash_lose_', '')
    
    if game_id not in pending_crash_decisions:
        bot.answer_callback_query(call.id, "❌ Игра не найдена", show_alert=True)
        return
    
    pending_crash_decisions[game_id]['decided'] = True
    send_crash_result(game_id, False)  # False = проигрыш
    
    bot.answer_callback_query(call.id, "❌ Проигрыш записан!", show_alert=False)
    # edit_message_reply_markup убран — send_crash_result уже редактирует сообщение

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
# КЛАНЫ: Команда для принудительной раздачи наград (админ)
@bot.message_handler(func=lambda message: message.text.lower() == 'наградить кланы' and is_admin(message.from_user.id))
def handle_force_clan_rewards(message):
    """Принудительная раздача наград кланам (админ)"""
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
# ==================== КНБ (КАМЕНЬ НОЖНИЦЫ БУМАГА) ====================

# Хранилище активных игр КНБ: {challenger_id: {bet, opponent_id, choice, msg_id, chat_id}}
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

def knb_winner(choice1, choice2):
    """Определяет победителя: возвращает 1 если победил первый, 2 если второй, 0 - ничья"""
    if choice1 == choice2:
        return 0
    wins = {
        '🪨 Камень': '✂️ Ножницы',
        '✂️ Ножницы': '📄 Бумага',
        '📄 Бумага': '🪨 Камень'
    }
    if wins[choice1] == choice2:
        return 1
    return 2

@bot.message_handler(func=lambda message: message.text and message.text.lower().startswith('кнб') and message.reply_to_message is not None)
def handle_knb_challenge(message):
    """Обработка команды кнб <ставка> в ответ на сообщение"""
    challenger_id = message.from_user.id
    
    # Проверяем reply
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
    
    # Получаем имена
    challenger_name = message.from_user.first_name or f"User{challenger_id}"
    opponent_name = opponent.first_name or f"User{opponent_id}"
    
    # Сохраняем вызов
    knb_games[f"{challenger_id}_{opponent_id}"] = {
        'bet': bet_amount,
        'challenger_id': challenger_id,
        'opponent_id': opponent_id,
        'challenger_name': challenger_name,
        'opponent_name': opponent_name,
        'chat_id': message.chat.id,
        'challenger_choice': None
    }
    
    # Кнопки принять/отклонить для оппонента
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
    action = parts[1]  # accept или decline
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
    
    # Принял - проверяем баланс оппонента
    opponent_balance = get_balance(opponent_id)
    bet = game['bet']
    
    if bet > opponent_balance:
        bot.answer_callback_query(call.id, f"❌ Недостаточно средств! Нужно: {format_balance(bet)}", show_alert=True)
        return
    
    # Принял вызов - отправляем кнопки выбора обоим в ЛС
    choices_markup = InlineKeyboardMarkup()
    choices_markup.row(
        InlineKeyboardButton("🪨 Камень", callback_data=f"knb_choice_{challenger_id}_{opponent_id}_камень"),
        InlineKeyboardButton("✂️ Ножницы", callback_data=f"knb_choice_{challenger_id}_{opponent_id}_ножницы"),
        InlineKeyboardButton("📄 Бумага", callback_data=f"knb_choice_{challenger_id}_{opponent_id}_бумага")
    )
    
    game['status'] = 'choosing'
    game['challenger_chose'] = False
    game['opponent_chose'] = False
    
    # Обновляем сообщение в чате
    bot.edit_message_text(
        f"✅ *{game['opponent_name']} принял вызов!*\n\n"
        f"⚔️ {game['challenger_name']} vs {game['opponent_name']}\n"
        f"💵 Ставка: *{format_balance(bet)}*\n\n"
        f"📩 Проверьте личные сообщения и выберите свой ход!",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='Markdown'
    )
    
    game['group_msg_id'] = call.message.message_id
    game['group_chat_id'] = call.message.chat.id
    
    # Отправляем выбор в ЛС обоим
    try:
        msg1 = bot.send_message(
            challenger_id,
            f"⚔️ *КНБ против {game['opponent_name']}*\n💵 Ставка: *{format_balance(bet)}*\n\nВыберите ваш ход:",
            parse_mode='Markdown',
            reply_markup=choices_markup
        )
        game['challenger_msg_id'] = msg1.message_id
    except Exception:
        bot.send_message(call.message.chat.id, f"⚠️ Не могу написать {game['challenger_name']} в ЛС. Пусть напишет боту /start")
    
    try:
        msg2 = bot.send_message(
            opponent_id,
            f"⚔️ *КНБ против {game['challenger_name']}*\n💵 Ставка: *{format_balance(bet)}*\n\nВыберите ваш ход:",
            parse_mode='Markdown',
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
    # knb_choice_{challenger_id}_{opponent_id}_{choice}
    challenger_id = int(parts[2])
    opponent_id = int(parts[3])
    choice_text = parts[4]
    
    choice_map = {
        'камень': '🪨 Камень',
        'ножницы': '✂️ Ножницы',
        'бумага': '📄 Бумага'
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
    
    # Сохраняем выбор
    if is_challenger:
        if game.get('challenger_choice'):
            bot.answer_callback_query(call.id, "Вы уже сделали выбор!")
            return
        game['challenger_choice'] = choice
        game['challenger_chose'] = True
        bot.answer_callback_query(call.id, f"Вы выбрали: {choice}")
        # Убираем кнопки у challenger
        try:
            bot.edit_message_text(
                f"✅ Вы выбрали *{choice}*\n⏳ Ожидаем выбор соперника...",
                user_id,
                game.get('challenger_msg_id'),
                parse_mode='Markdown'
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
        # Убираем кнопки у opponent
        try:
            bot.edit_message_text(
                f"✅ Вы выбрали *{choice}*\n⏳ Ожидаем выбор соперника...",
                user_id,
                game.get('opponent_msg_id'),
                parse_mode='Markdown'
            )
        except Exception:
            pass
    
    # Проверяем оба ли выбрали
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
        # Ничья — баланс не меняется
        result_text = (
            f"🤝 *НИЧЬЯ!*\n\n"
            f"👤 {challenger_name}: {c_choice}\n"
            f"👤 {opponent_name}: {o_choice}\n\n"
            f"💵 Ставки остаются у игроков"
        )
    elif winner == 1:
        # Победил challenger: снимаем ставку с opponent, начисляем challenger
        update_balance(opponent_id, -bet)
        update_balance(challenger_id, bet)
        new_balance = get_balance(challenger_id)
        result_text = (
            f"🏆 *ПОБЕДИЛ {challenger_name.upper()}!*\n\n"
            f"👤 {challenger_name}: {c_choice}\n"
            f"👤 {opponent_name}: {o_choice}\n\n"
            f"💵 {challenger_name} получил *+{format_balance(bet)}*\n"
            f"💵 Новый баланс: *{format_balance(new_balance)}*"
        )
    else:
        # Победил opponent: снимаем ставку с challenger, начисляем opponent
        update_balance(challenger_id, -bet)
        update_balance(opponent_id, bet)
        new_balance = get_balance(opponent_id)
        result_text = (
            f"🏆 *ПОБЕДИЛ {opponent_name.upper()}!*\n\n"
            f"👤 {challenger_name}: {c_choice}\n"
            f"👤 {opponent_name}: {o_choice}\n\n"
            f"💵 {opponent_name} получил *+{format_balance(bet)}*\n"
            f"💵 Новый баланс: *{format_balance(new_balance)}*"
        )
    
    # Отправляем результат в ЛС обоим
    try:
        bot.send_message(challenger_id, result_text, parse_mode='Markdown')
    except Exception:
        pass
    try:
        bot.send_message(opponent_id, result_text, parse_mode='Markdown')
    except Exception:
        pass
    
    # Обновляем сообщение в группе
    try:
        bot.edit_message_text(
            result_text,
            game.get('group_chat_id'),
            game.get('group_msg_id'),
            parse_mode='Markdown'
        )
    except Exception:
        try:
            bot.send_message(game.get('group_chat_id'), result_text, parse_mode='Markdown')
        except Exception:
            pass

# ==================== КОНЕЦ КНБ ====================

# ==================== ПРОМОКОДЫ ====================

def create_promo(code, reward, max_uses, expires_at, created_by):
    with get_db_cursor() as cursor:
        cursor.execute('''
            INSERT OR REPLACE INTO promo_codes (code, reward, max_uses, current_uses, expires_at, created_by, created_at, is_active)
            VALUES (?, ?, ?, 0, ?, ?, ?, 1)
        ''', (code.upper(), reward, max_uses, expires_at, created_by, int(time.time())))

def use_promo(user_id, code):
    code = code.upper().strip()
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

# Активация промокода пользователем: /promo КОД или просто «промо КОД»
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

# Админ: создать промокод
# Формат: создать промо КОД СУММА USES [ЧАСЫ]
# Пример: создать промо SALE2024 50000 100 24
@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith('создать промо') and is_admin(m.from_user.id))
def handle_create_promo(message):
    parts = message.text.split()
    # создать промо КОД СУММА USES [ЧАСЫ]
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

# Админ: список промокодов
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

# Админ: удалить промокод
@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith('удалить промо ') and is_admin(m.from_user.id))
def handle_delete_promo(message):
    code = message.text.split(maxsplit=2)[2].upper().strip()
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE promo_codes SET is_active = 0 WHERE code = ?', (code,))
    bot.send_message(message.chat.id, f"🗑 Промокод <code>{code}</code> отключён", parse_mode='HTML')

# Админ: удалить все чеки
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

# ==================== КОНЕЦ ПРОМОКОДОВ ====================

# ==================== КОМАНДА МОЙ УРОВЕНЬ ====================

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

    # Следующий титул
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

# ==================== СБРОС УРОВНЕЙ (АДМИН) ====================

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

# Админ: обнулить видеокарты у конкретного пользователя
# Формат: обнулитькарты @username или обнулитькарты 123456789
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


# Формат: обнулитьопыт 50000
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

# ==================== КОНЕЦ УРОВНЕЙ ====================

# ==================== ИВЕНТ: СБОР ====================

EVENT_CHAT_ID = int(os.getenv("EVENT_CHAT_ID", "0"))
EVENT_REQUIRED = 10      # сколько нужно участников
EVENT_WINNERS = 3        # сколько победителей
EVENT_MIN_PRIZE = 1000
EVENT_MAX_PRIZE = 100000

# Состояние текущего ивента
event_state = {
    "active": False,
    "participants": {},   # user_id -> first_name
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

    def force_finish():
        if state["active"] and len(state["participants"]) >= EVENT_WINNERS:
            event_finish()
        elif state["active"]:
            state["active"] = False
            state["participants"] = {}
            try:
                _tg_api(
                    "editMessageText",
                    chat_id=EVENT_CHAT_ID,
                    message_id=state["message_id"],
                    text="😴 <b>Сбор отменён</b> — не хватило участников.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
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
            # Каждые 20–27 часов — немного в разное время каждый день
            delay = random.randint(20 * 3600, 27 * 3600)
            time.sleep(delay)
            event_launch()
        except Exception as e:
            print(f"[event_scheduler] ошибка: {e}")
            time.sleep(3600)

def start_event_scheduler():
    t = threading.Thread(target=event_scheduler, daemon=True)
    t.daemon = True
    t.start()
    print("🎲 Планировщик ивентов запущен")

# Админ: запустить ивент вручную
@bot.message_handler(func=lambda m: m.text and m.text.lower().strip() == "запустить ивент" and is_admin(m.from_user.id))
def handle_manual_event(message):
    if event_state["active"]:
        bot.send_message(message.chat.id, "⚠️ Ивент уже активен.", parse_mode="HTML")
        return
    event_launch()
    bot.send_message(message.chat.id, "✅ Ивент запущен!", parse_mode="HTML")

# ==================== КОНЕЦ ИВЕНТА ====================

# Инициализация базы данных
init_db()
init_dice_tables()  # Инициализация таблиц для костей

# Запускаем бота
if __name__ == "__main__":
    print("Бот запущен...")
    try:
        # Безопасная очистка старых вызовов
        cleanup_expired_challenges()
        start_event_scheduler()
        
        bot.infinity_polling()
    except Exception as e:
        print(f"Критическая ошибка: {e}")
        print("Перезапустите бота.")