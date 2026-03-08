"""
Flask API — Casino Mini App
- Rate limiting
- Leaderboard caching
- Profile endpoint
- No taxi
"""

from flask import Flask, request, jsonify
import sqlite3, json, hmac, hashlib, time, random, threading, os
from contextlib import contextmanager
from urllib.parse import parse_qsl
from functools import wraps
from collections import defaultdict

app = Flask(__name__)

from dotenv import load_dotenv
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_NAME   = "game.db"

# ══════════════════════════════════════════════
#  DB
# ══════════════════════════════════════════════

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT, first_name TEXT, custom_name TEXT,
                balance INTEGER DEFAULT 0, last_click TIMESTAMP DEFAULT 0,
                click_power INTEGER DEFAULT 10, referral_code TEXT UNIQUE,
                referred_by INTEGER, video_cards INTEGER DEFAULT 0,
                deposit INTEGER DEFAULT 0, last_mining_collect TIMESTAMP DEFAULT 0,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                click_streak INTEGER DEFAULT 0, total_clicks INTEGER DEFAULT 0,
                bank_deposit INTEGER DEFAULT 0, daily_streak INTEGER DEFAULT 0,
                last_daily_bonus TIMESTAMP DEFAULT 0, last_interest_calc TIMESTAMP DEFAULT 0,
                business_id INTEGER DEFAULT 0, business_progress INTEGER DEFAULT 0,
                experience INTEGER DEFAULT 0, business_start_time TIMESTAMP DEFAULT 0,
                business_raw_materials INTEGER DEFAULT 0, clan_id INTEGER DEFAULT 0,
                games_won INTEGER DEFAULT 0, games_lost INTEGER DEFAULT 0,
                total_won_amount INTEGER DEFAULT 0, total_lost_amount INTEGER DEFAULT 0
            )
        ''')
        conn.execute('''
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
        # Индекс для быстрого поиска истории по юзеру
        conn.execute('CREATE INDEX IF NOT EXISTS idx_game_history_user ON game_history(user_id)')
        print("✅ БД инициализирована (users + game_history)")

init_db()

def migrate_db():
    """Безопасная миграция — добавляет недостающие таблицы не трогая старые данные"""
    with get_db() as conn:
        conn.execute('''
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
        conn.execute('CREATE INDEX IF NOT EXISTS idx_game_history_user ON game_history(user_id)')
        print("✅ Миграция: game_history готова (старые данные не тронуты)")

migrate_db()

# ══════════════════════════════════════════════
#  RATE LIMITING
# ══════════════════════════════════════════════

_rate_data = defaultdict(list)  # user_id/ip -> [timestamps]
_rate_lock = threading.Lock()

def rate_limit(max_calls=10, window=10):
    """Декоратор: max_calls запросов за window секунд на пользователя"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            # Ключ — user_id из тела или IP
            try:
                body = request.get_json(silent=True) or {}
                key = str(body.get('user_id') or request.args.get('user_id') or request.remote_addr)
            except:
                key = request.remote_addr

            now = time.time()
            with _rate_lock:
                calls = _rate_data[key]
                # Убираем старые записи
                calls[:] = [t for t in calls if now - t < window]
                if len(calls) >= max_calls:
                    return jsonify({'error': 'Слишком много запросов, подожди немного'}), 429
                calls.append(now)
            return f(*args, **kwargs)
        return wrapper
    return decorator

# ══════════════════════════════════════════════
#  CACHE (для лидерборда)
# ══════════════════════════════════════════════

_cache = {}
_cache_lock = threading.Lock()

def cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() < entry['expires']:
            return entry['data']
    return None

def cache_set(key, data, ttl=30):
    with _cache_lock:
        _cache[key] = {'data': data, 'expires': time.time() + ttl}

def cache_clear(key):
    with _cache_lock:
        _cache.pop(key, None)

# ══════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════

def verify_tg(init_data):
    if not init_data: return None
    try:
        params = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_str = params.pop('hash', '')
        check_str = '\n'.join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, hash_str):
            return json.loads(params.get('user', '{}'))
    except: pass
    return None

# ══════════════════════════════════════════════
#  LEVELS
# ══════════════════════════════════════════════

LEVEL_TITLES = {
    1:('🥉','Bronze I'),    2:('🥉','Bronze II'),   3:('🥉','Bronze III'),
    4:('🥉','Bronze IV'),   5:('🥉','Bronze V'),    6:('🥉','Bronze VI'),
    7:('🥉','Bronze VII'),  8:('🥉','Bronze VIII'),
    9:('🥈','Silver I'),   10:('🥈','Silver II'),  11:('🥈','Silver III'),
   12:('🥈','Silver IV'),  13:('🥈','Silver V'),   14:('🥈','Silver VI'),
   15:('🥈','Silver VII'), 16:('🥈','Silver VIII'),
   17:('🥇','Gold I'),     18:('🥇','Gold II'),    19:('🥇','Gold III'),
   20:('🥇','Gold IV'),    21:('🥇','Gold V'),     22:('🥇','Gold VI'),
   23:('🥇','Gold VII'),   24:('🥇','Gold VIII'),
   25:('💎','Diamond I'),  26:('💎','Diamond II'), 27:('💎','Diamond III'),
   28:('💎','Diamond IV'), 29:('💎','Diamond V'),  30:('💎','Diamond VI'),
   31:('💎','Diamond VII'),32:('💎','Diamond VIII'),
   33:('🔮','The Mythic I'),  34:('🔮','The Mythic II'),  35:('🔮','The Mythic III'),
   36:('🔮','The Mythic IV'), 37:('🔮','The Mythic V'),   38:('🔮','The Mythic VI'),
   39:('🔮','The Mythic VII'),40:('🔮','The Mythic VIII'),
   41:('👑','Legend I'),   42:('👑','Legend II'),  43:('👑','Legend III'),
   44:('👑','Legend IV'),  45:('👑','Legend V'),   46:('👑','Legend VI'),
   47:('👑','Legend VII'), 48:('👑','Legend VIII'),49:('👑','Legend IX'),
   50:('👑','Legend X'),
}
TITLE_COLORS = {
    '🥉':'#cd7f32','🥈':'#c0c0c0','🥇':'#ffd700',
    '💎':'#00bfff','🔮':'#9f00ff','👑':'#ff4500',
}

def get_level_from_exp(exp):
    lv, total = 1, 0
    while lv < 50:
        needed = 1000 + (lv-1)*500
        if total + needed > exp: break
        total += needed; lv += 1
    return lv

def get_title(lv):
    e, n = LEVEL_TITLES.get(lv, LEVEL_TITLES[50])
    return f"{e} {n}"

def build_user_title(lv):
    emoji, name = LEVEL_TITLES.get(lv, LEVEL_TITLES[50])
    return emoji, name, TITLE_COLORS.get(emoji, '#5b7fa6')

# ══════════════════════════════════════════════
#  CORS
# ══════════════════════════════════════════════

@app.after_request
def cors(resp):
    resp.headers['Access-Control-Allow-Origin']  = '*'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Telegram-Init-Data'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return resp

@app.route('/api/<path:p>', methods=['OPTIONS'])
def options(p): return '', 204

@app.route('/ping')
def ping(): return 'ok', 200

# ══════════════════════════════════════════════
#  LEVEL UP NOTIFY + GAME RESULT
# ══════════════════════════════════════════════

def send_level_up_notify(user_id, new_level):
    try:
        mod = _load_bot()
        if not mod: return
        emoji, tname = LEVEL_TITLES.get(new_level, LEVEL_TITLES[50])
        bonus = new_level * 200
        click_b = int((1 + new_level * 0.02) * 100 - 100)
        daily_b = int((1 + new_level * 0.05) * 100 - 100)
        mod.bot.send_message(
            user_id,
            f"🎉 <b>НОВЫЙ УРОВЕНЬ!</b>\n\n"
            f"{emoji} <b>{new_level} — {tname}</b>\n\n"
            f"💵 Бонус: +{bonus:,}\n"
            f"⚡ Бонус к кликам: +{click_b}%\n"
            f"🎁 Бонус к ежедневке: +{daily_b}%",
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"Level up notify error: {e}")

def apply_game_result(conn, user_id, delta, xp, game=None, bet=0, result='', win_amount=0):
    xp = max(1, xp)
    old_row = conn.execute('SELECT experience FROM users WHERE user_id=?', (user_id,)).fetchone()
    old_lv  = get_level_from_exp(old_row['experience']) if old_row else 1

    # Обновляем баланс и опыт — главная операция
    conn.execute(
        'UPDATE users SET balance=MAX(0,balance+?), experience=MIN(experience+?,999999), '
        'last_activity=CURRENT_TIMESTAMP WHERE user_id=?',
        (delta, xp, user_id)
    )

    # Сохраняем в историю (некритично — не ломаем транзакцию при ошибке)
    if game:
        try:
            conn.execute(
                'INSERT INTO game_history(user_id,game,bet,result,win_amount,created_at) VALUES(?,?,?,?,?,?)',
                (user_id, game, bet, result, win_amount, int(time.time()))
            )
        except Exception as e:
            print(f"⚠️ game_history insert error (ignored): {e}")

    row = conn.execute('SELECT balance,experience FROM users WHERE user_id=?', (user_id,)).fetchone()
    new_lv = get_level_from_exp(row['experience'])
    emoji, tname, tcolor = build_user_title(new_lv)

    if new_lv > old_lv:
        threading.Thread(target=send_level_up_notify, args=(user_id, new_lv), daemon=True).start()

    return {
        'balance': row['balance'], 'experience': row['experience'], 'level': new_lv,
        'title': f"{emoji} {tname}", 'title_emoji': emoji, 'title_name': tname, 'title_color': tcolor
    }

# ══════════════════════════════════════════════
#  USER + PROFILE
# ══════════════════════════════════════════════

@app.route('/api/user/<int:user_id>')
def get_user(user_id):
    with get_db() as conn:
        row = conn.execute(
            '''SELECT user_id,username,first_name,custom_name,balance,experience,
                      games_won,games_lost,total_won_amount,total_lost_amount,
                      last_activity,total_clicks,daily_streak,last_daily_bonus
               FROM users WHERE user_id=?''', (user_id,)
        ).fetchone()
        if not row: return jsonify({'error': 'not found'}), 404
        d = dict(row)
        lv = get_level_from_exp(d.get('experience', 0))
        emoji, tname, tcolor = build_user_title(lv)
        d['level']       = lv
        d['title']       = f"{emoji} {tname}"
        d['title_emoji'] = emoji
        d['title_name']  = tname
        d['title_color'] = tcolor
        d['name']        = d.get('custom_name') or d.get('first_name') or d.get('username') or 'Аноним'
        lv_xp_start      = sum(1000 + i*500 for i in range(lv-1))
        d['exp_current'] = max(0, d['experience'] - lv_xp_start)
        d['exp_needed']  = 1000 + (lv-1)*500
        d['total_won']   = d.get('total_won_amount', 0)
        d['total_lost']  = d.get('total_lost_amount', 0)
        return jsonify(d)

@app.route('/api/profile/<int:user_id>')
def get_profile(user_id):
    """Расширенный профиль: статистика + история игр + ранг"""
    with get_db() as conn:
        row = conn.execute(
            '''SELECT user_id,username,first_name,custom_name,balance,experience,
                      games_won,games_lost,total_won_amount,total_lost_amount,
                      last_activity,total_clicks,daily_streak,clan_id
               FROM users WHERE user_id=?''', (user_id,)
        ).fetchone()
        if not row: return jsonify({'error': 'not found'}), 404
        d = dict(row)

        # Уровень и титул
        lv = get_level_from_exp(d.get('experience', 0))
        emoji, tname, tcolor = build_user_title(lv)
        d['level']       = lv
        d['title']       = f"{emoji} {tname}"
        d['title_emoji'] = emoji
        d['title_name']  = tname
        d['title_color'] = tcolor
        d['name']        = d.get('custom_name') or d.get('first_name') or d.get('username') or 'Аноним'

        # XP прогресс
        lv_xp_start      = sum(1000 + i*500 for i in range(lv-1))
        d['exp_current'] = max(0, d['experience'] - lv_xp_start)
        d['exp_needed']  = 1000 + (lv-1)*500 if lv < 50 else 0
        d['exp_pct']     = round(d['exp_current'] / d['exp_needed'] * 100, 1) if d['exp_needed'] > 0 else 100

        # Статистика
        total_games = d['games_won'] + d['games_lost']
        d['total_games']  = total_games
        d['win_rate']     = round(d['games_won'] / total_games * 100, 1) if total_games > 0 else 0
        d['total_won']    = d.get('total_won_amount', 0)
        d['total_lost']   = d.get('total_lost_amount', 0)
        d['net_profit']   = d['total_won'] - d['total_lost']

        # Ранг по балансу
        rank_row = conn.execute(
            'SELECT COUNT(*)+1 as rank FROM users WHERE balance > (SELECT balance FROM users WHERE user_id=?)',
            (user_id,)
        ).fetchone()
        d['rank'] = rank_row['rank'] if rank_row else 0

        # Последние 10 игр
        history = conn.execute(
            'SELECT game,bet,result,win_amount,created_at FROM game_history '
            'WHERE user_id=? ORDER BY created_at DESC LIMIT 10',
            (user_id,)
        ).fetchall()
        d['history'] = [dict(h) for h in history]

        # Следующий уровень
        if lv < 50:
            next_emoji, next_name = LEVEL_TITLES.get(lv+1, LEVEL_TITLES[50])
            d['next_title'] = f"{next_emoji} {next_name}"
        else:
            d['next_title'] = None

        return jsonify(d)

# ══════════════════════════════════════════════
#  CASINO GAMES
# ══════════════════════════════════════════════

# ── SLOTS ──
@app.route('/api/slots/spin', methods=['POST'])
@rate_limit(max_calls=15, window=10)
def slots_spin():
    data    = request.json
    user_id = data.get('user_id')
    bet     = int(data.get('bet', 0))
    if not user_id or bet <= 0: return jsonify({'error': 'bad data'}), 400
    if bet < 100: return jsonify({'error': 'Минимальная ставка 100'}), 400

    symbols = ['🍒','🍋','🍊','⭐','💎','🎰']
    weights = [30,25,20,15,8,2]
    reels   = random.choices(symbols, weights=weights, k=3)
    won     = reels[0] == reels[1] == reels[2]
    mult_map = {'🍒':2,'🍋':3,'🍊':4,'⭐':5,'💎':10,'🎰':20}
    win_amount = bet * mult_map.get(reels[0], 2) if won else 0
    win_type   = ('jackpot' if reels[0] in ['💎','🎰'] else 'pair') if won else None
    delta = win_amount - bet if won else -bet
    xp    = min(5, max(1, bet // 500000))

    with get_db() as conn:
        row = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
        if not row: return jsonify({'error': 'user not found'}), 404
        if row['balance'] < bet: return jsonify({'error': 'Недостаточно средств', 'balance': row['balance']}), 400
        conn.execute('UPDATE users SET games_won=games_won+?,games_lost=games_lost+? WHERE user_id=?',
                     (1 if won else 0, 0 if won else 1, user_id))
        res = apply_game_result(conn, user_id, delta, xp,
                                game='slots', bet=bet,
                                result='win' if won else 'lose',
                                win_amount=win_amount)
    return jsonify({'ok':True,'reels':reels,'won':won,
                    'win':win_amount,'win_amount':win_amount,'win_type':win_type,
                    'bet':bet,'new_balance':res['balance'],**res})

# ── ROULETTE ──
@app.route('/api/roulette/spin', methods=['POST'])
@rate_limit(max_calls=15, window=10)
def roulette_spin():
    data     = request.json
    user_id  = data.get('user_id')
    bet      = int(data.get('bet', 0))
    bet_type = data.get('bet_type', 'red')
    number   = data.get('number') or data.get('bet_number')
    if not user_id or bet <= 0: return jsonify({'error': 'bad data'}), 400
    if bet < 100: return jsonify({'error': 'Минимальная ставка 100'}), 400

    result_num = random.randint(0, 36)
    reds = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    color = 'green' if result_num == 0 else ('red' if result_num in reds else 'black')
    won, win_mult = False, 0
    if   bet_type == 'red'    and color == 'red':   won,win_mult = True,2
    elif bet_type == 'black'  and color == 'black': won,win_mult = True,2
    elif bet_type == 'green'  and color == 'green': won,win_mult = True,14
    elif bet_type == 'number' and number is not None and int(number) == result_num: won,win_mult = True,36
    win_amount = bet * win_mult if won else 0
    delta = win_amount - bet if won else -bet
    xp    = min(5, max(1, bet // 500000))

    with get_db() as conn:
        row = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
        if not row: return jsonify({'error': 'user not found'}), 404
        if row['balance'] < bet: return jsonify({'error': 'Недостаточно средств', 'balance': row['balance']}), 400
        conn.execute('UPDATE users SET games_won=games_won+?,games_lost=games_lost+? WHERE user_id=?',
                     (1 if won else 0, 0 if won else 1, user_id))
        res = apply_game_result(conn, user_id, delta, xp,
                                game='roulette', bet=bet,
                                result='win' if won else 'lose',
                                win_amount=win_amount)
    return jsonify({'ok':True,
                    'number':result_num,'result_number':result_num,
                    'color':color,'result_color':color,
                    'won':won,'win':win_amount,'win_amount':win_amount,
                    'bet':bet,'new_balance':res['balance'],**res})

# ── BLACKJACK ──
bj_sessions = {}

def card_value(card):
    rank = card[:-1]
    if rank in ('J','Q','K'): return 10
    if rank == 'A': return 11
    try: return int(rank)
    except: return 0

def hand_value(hand):
    val  = sum(card_value(c) for c in hand)
    aces = sum(1 for c in hand if c[:-1] == 'A')
    while val > 21 and aces: val -= 10; aces -= 1
    return val

def make_deck():
    ranks = ['2','3','4','5','6','7','8','9','10','J','Q','K','A']
    suits = ['♠','♥','♦','♣']
    deck  = [r+s for r in ranks for s in suits]
    random.shuffle(deck); return deck

@app.route('/api/blackjack/deal', methods=['POST'])
@rate_limit(max_calls=10, window=10)
def bj_deal():
    data    = request.json
    user_id = data.get('user_id')
    bet     = int(data.get('bet', 0))
    if not user_id or bet <= 0: return jsonify({'error': 'bad data'}), 400
    if bet < 100: return jsonify({'error': 'Минимальная ставка 100'}), 400

    with get_db() as conn:
        row = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
        if not row: return jsonify({'error': 'user not found'}), 404
        if row['balance'] < bet: return jsonify({'error': 'Недостаточно средств'}), 400

    deck   = make_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    bj_sessions[user_id] = {'deck': deck, 'player': player, 'dealer': dealer, 'bet': bet}
    pv = hand_value(player)
    dv = hand_value(dealer)

    if pv == 21:
        win_amount = int(bet * 2.5)
        with get_db() as conn:
            conn.execute('UPDATE users SET games_won=games_won+1 WHERE user_id=?', (user_id,))
            res = apply_game_result(conn, user_id, int(bet*1.5), min(5, max(1, bet//500000)),
                                    game='blackjack', bet=bet, result='blackjack', win_amount=win_amount)
        del bj_sessions[user_id]
        return jsonify({'ok':True,'player':player,'dealer':dealer,
                        'player_value':pv,'player_total':pv,
                        'dealer_value':dv,'dealer_total':dv,
                        'dealer_visible':dealer[0],
                        'status':'blackjack','blackjack':True,
                        'win':win_amount,'win_amount':win_amount,
                        'new_balance':res['balance'],**res})

    return jsonify({'ok':True,'player':player,
                    'dealer':[dealer[0],'🂠'],'dealer_visible':dealer[0],
                    'player_value':pv,'player_total':pv,
                    'dealer_value':card_value(dealer[0]),'status':'playing','blackjack':False})

@app.route('/api/blackjack/hit', methods=['POST'])
@rate_limit(max_calls=20, window=10)
def bj_hit():
    data    = request.json
    user_id = data.get('user_id')
    sess    = bj_sessions.get(user_id)
    if not sess: return jsonify({'error': 'no active game'}), 400

    sess['player'].append(sess['deck'].pop())
    pv = hand_value(sess['player'])

    if pv > 21:
        with get_db() as conn:
            conn.execute('UPDATE users SET games_lost=games_lost+1 WHERE user_id=?', (user_id,))
            res = apply_game_result(conn, user_id, -sess['bet'], min(5, max(1, sess['bet']//500000)),
                                    game='blackjack', bet=sess['bet'], result='bust', win_amount=0)
        del bj_sessions[user_id]
        return jsonify({'ok':True,'player':sess['player'],'dealer':sess['dealer'],
                        'player_value':pv,'player_total':pv,
                        'dealer_value':hand_value(sess['dealer']),
                        'status':'bust','result':'lose',
                        'win':0,'win_amount':0,'new_balance':res['balance'],**res})

    return jsonify({'ok':True,'player':sess['player'],
                    'dealer':[sess['dealer'][0],'🂠'],'dealer_visible':sess['dealer'][0],
                    'player_value':pv,'player_total':pv,'status':'playing'})

@app.route('/api/blackjack/stand', methods=['POST'])
@rate_limit(max_calls=10, window=10)
def bj_stand():
    data    = request.json
    user_id = data.get('user_id')
    sess    = bj_sessions.get(user_id)
    if not sess: return jsonify({'error': 'no active game'}), 400

    while hand_value(sess['dealer']) < 17:
        sess['dealer'].append(sess['deck'].pop())
    pv  = hand_value(sess['player'])
    dv  = hand_value(sess['dealer'])
    bet = sess['bet']

    if dv > 21 or pv > dv:  status,delta,win_amount = 'win',  bet,   bet*2
    elif pv == dv:           status,delta,win_amount = 'push', 0,     bet
    else:                    status,delta,win_amount = 'lose', -bet,  0

    with get_db() as conn:
        if status == 'win':
            conn.execute('UPDATE users SET games_won=games_won+1 WHERE user_id=?', (user_id,))
        elif status == 'lose':
            conn.execute('UPDATE users SET games_lost=games_lost+1 WHERE user_id=?', (user_id,))
        res = apply_game_result(conn, user_id, delta, min(5, max(1, bet//500000)),
                                game='blackjack', bet=bet, result=status, win_amount=win_amount)
    del bj_sessions[user_id]
    return jsonify({'ok':True,'player':sess['player'],'dealer':sess['dealer'],
                    'player_value':pv,'player_total':pv,
                    'dealer_value':dv,'dealer_total':dv,
                    'status':status,'result':status,
                    'win':win_amount,'win_amount':win_amount,
                    'bet':bet,'new_balance':res['balance'],**res})

# ── COIN FLIP ──
@app.route('/api/coin/flip', methods=['POST'])
@rate_limit(max_calls=15, window=10)
def coin_flip():
    data    = request.json
    user_id = data.get('user_id')
    bet     = int(data.get('bet', 0))
    choice  = data.get('choice', 'heads')
    if not user_id or bet <= 0: return jsonify({'error': 'bad data'}), 400
    if bet < 100: return jsonify({'error': 'Минимальная ставка 100'}), 400

    result       = random.choice(['heads', 'tails'])
    won          = result == choice
    result_emoji = '👑' if result == 'heads' else '🦅'
    delta        = bet if won else -bet
    xp           = min(5, max(1, bet // 500000))
    win_amount   = bet * 2 if won else 0

    with get_db() as conn:
        row = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
        if not row: return jsonify({'error': 'user not found'}), 404
        if row['balance'] < bet: return jsonify({'error': 'Недостаточно средств', 'balance': row['balance']}), 400
        conn.execute('UPDATE users SET games_won=games_won+?,games_lost=games_lost+? WHERE user_id=?',
                     (1 if won else 0, 0 if won else 1, user_id))
        res = apply_game_result(conn, user_id, delta, xp,
                                game='coin', bet=bet,
                                result='win' if won else 'lose',
                                win_amount=win_amount)
    return jsonify({'ok':True,'result':result,'result_emoji':result_emoji,'won':won,
                    'win':win_amount,'win_amount':win_amount,
                    'bet':bet,'new_balance':res['balance'],**res})

# ══════════════════════════════════════════════
#  ВЫСОКАЯ КАРТА — vs Бот
# ══════════════════════════════════════════════

CARD_RANKS  = ['2','3','4','5','6','7','8','9','10','J','Q','K','A']
CARD_SUITS  = ['♠','♥','♦','♣']
CARD_VALUES = {r: i for i, r in enumerate(CARD_RANKS)}

@app.route('/api/highcard/play', methods=['POST'])
@rate_limit(max_calls=10, window=10)
def hc_play():
    data    = request.json
    user_id = data.get('user_id')
    bet     = int(data.get('bet', 0))
    if not user_id or bet < 100:
        return jsonify({'error': 'bad data'}), 400

    with get_db() as conn:
        row = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
        if not row:
            return jsonify({'error': 'user not found'}), 404
        if row['balance'] < bet:
            return jsonify({'error': 'Недостаточно средств', 'balance': row['balance']}), 400

        pr, ps = random.choice(CARD_RANKS), random.choice(CARD_SUITS)
        br, bs = random.choice(CARD_RANKS), random.choice(CARD_SUITS)
        pv, bv = CARD_VALUES[pr], CARD_VALUES[br]

        # Переигрываем ничью
        attempts = 0
        while pv == bv and attempts < 10:
            br, bs = random.choice(CARD_RANKS), random.choice(CARD_SUITS)
            bv = CARD_VALUES[br]
            attempts += 1

        won  = pv > bv
        draw = pv == bv
        delta      = bet if won else (0 if draw else -bet)
        win_amount = bet * 2 if won else (bet if draw else 0)
        xp = min(5, max(1, bet // 500000))

        if won:
            conn.execute('UPDATE users SET games_won=games_won+1 WHERE user_id=?', (user_id,))
        elif not draw:
            conn.execute('UPDATE users SET games_lost=games_lost+1 WHERE user_id=?', (user_id,))

        res = apply_game_result(conn, user_id, delta, xp,
                                game='highcard', bet=bet,
                                result='win' if won else ('draw' if draw else 'lose'),
                                win_amount=win_amount)

    return jsonify({
        'ok': True,
        'player_card': pr + ps, 'bot_card': br + bs,
        'player_rank': pr, 'player_suit': ps,
        'bot_rank': br,    'bot_suit': bs,
        'won': won, 'draw': draw,
        'win_amount': win_amount,
        'bet': bet,
        'new_balance': res['balance'],
        **res
    })

# ══════════════════════════════════════════════
#  LEADERBOARD (с кешем 30 сек)
# ══════════════════════════════════════════════

def build_lb(rows):
    result = []
    for i, r in enumerate(rows):
        d = dict(r)
        lv = get_level_from_exp(d.get('exp', 0))
        emoji, tname, tcolor = build_user_title(lv)
        d['level'] = lv; d['title'] = f"{emoji} {tname}"
        d['title_emoji'] = emoji; d['title_name'] = tname; d['title_color'] = tcolor
        d['rank'] = i + 1
        result.append(d)
    return result

@app.route('/api/leaderboard')
def leaderboard():
    tab = request.args.get('type', 'balance')
    col = {'balance':'balance','exp':'experience','wins':'games_won'}.get(tab, 'balance')
    cache_key = f'lb_{col}'
    cached = cache_get(cache_key)
    if cached: return jsonify(cached)
    with get_db() as conn:
        rows = conn.execute(f'''
            SELECT user_id, COALESCE(custom_name,first_name,username,'Аноним') as name,
                   balance, experience as exp, games_won as wins
            FROM users ORDER BY {col} DESC LIMIT 50''').fetchall()
        result = build_lb(rows)
    cache_set(cache_key, result, ttl=30)
    return jsonify(result)

@app.route('/api/leaderboard/exp')
def leaderboard_exp():
    cached = cache_get('lb_exp')
    if cached: return jsonify(cached)
    with get_db() as conn:
        rows = conn.execute('''
            SELECT user_id, COALESCE(custom_name,first_name,username,'Аноним') as name,
                   balance, experience as exp, games_won as wins
            FROM users ORDER BY experience DESC LIMIT 50''').fetchall()
        result = build_lb(rows)
    cache_set('lb_exp', result, ttl=30)
    return jsonify(result)

# ── Быстрый баланс (для синхронизации frontend) ──
@app.route('/api/balance/<int:user_id>')
def get_balance(user_id):
    with get_db() as conn:
        row = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
        if not row: return jsonify({'error': 'not found'}), 404
        return jsonify({'balance': row['balance'], 'user_id': user_id})

# ── Счётчик онлайн игроков (активных за последние 5 минут) ──
@app.route('/api/online')
def online_count():
    cached = cache_get('online_count')
    if cached: return jsonify(cached)
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM users WHERE last_activity >= datetime('now', '-5 minutes')"
        ).fetchone()
        total = conn.execute('SELECT COUNT(*) as cnt FROM users').fetchone()['cnt']
        result = {'online': row['cnt'] if row else 0, 'total': total}
    cache_set('online_count', result, ttl=30)
    return jsonify(result)

@app.route('/api/my_rank/<int:user_id>')
def my_rank(user_id):
    with get_db() as conn:
        r = conn.execute(
            'SELECT COUNT(*)+1 as rank FROM users WHERE balance>(SELECT balance FROM users WHERE user_id=?)',
            (user_id,)
        ).fetchone()
        return jsonify({'rank': r['rank'] if r else 0})

# ══════════════════════════════════════════════
#  SERVE HTML
# ══════════════════════════════════════════════

@app.route('/')
@app.route('/mini')
def mini():
    try:
        return open('index.html','r',encoding='utf-8').read(), 200, {'Content-Type':'text/html'}
    except FileNotFoundError:
        return 'Place index.html next to app.py', 404

# ══════════════════════════════════════════════
#  БОТ ЧЕРЕЗ ВЕБХУК
# ══════════════════════════════════════════════

import importlib.util as _ilu
_bot_module = None

def _load_bot():
    global _bot_module
    if _bot_module is not None: return _bot_module
    bot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot.py')
    if not os.path.exists(bot_path):
        print("⚠️  bot.py не найден"); return None
    spec = _ilu.spec_from_file_location("bot_module", bot_path)
    mod  = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _bot_module = mod
    return mod

def setup_webhook():
    mod = _load_bot()
    if not mod: return
    url = os.environ.get('RENDER_EXTERNAL_URL', '').rstrip('/')
    if not url:
        print("⚠️  RENDER_EXTERNAL_URL не задан"); return
    webhook_url = f"{url}/webhook/{BOT_TOKEN}"
    try:
        mod.bot.remove_webhook()
        time.sleep(1)
        mod.bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        print(f"✅ Вебхук: {webhook_url}")
        try: mod.cleanup_expired_challenges()
        except: pass
    except Exception as e:
        print(f"❌ Вебхук ошибка: {e}")

@app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
def webhook():
    mod = _load_bot()
    if not mod: return 'bot not loaded', 500
    import telebot
    update = telebot.types.Update.de_json(request.get_data(as_text=True))
    mod.bot.process_new_updates([update])
    return 'ok', 200

def auto_ping():
    import urllib.request
    time.sleep(60)
    url = os.environ.get('RENDER_EXTERNAL_URL', 'http://localhost:3001')
    while True:
        try:
            urllib.request.urlopen(f"{url}/ping", timeout=10)
            print("🏓 Ping OK")
        except Exception as e:
            print(f"🏓 Ping error: {e}")
        time.sleep(240)

if __name__ == '__main__':
    threading.Thread(target=auto_ping, daemon=True, name="PingThread").start()
    setup_webhook()
    print("✅ Flask запущен (webhook mode)")
    app.run(host='0.0.0.0', port=3001, debug=False)


# ══════════════════════════════════════════════
#  BONUS
# ══════════════════════════════════════════════

@app.route('/api/bonus/claim', methods=['POST'])
def claim_bonus():
    data = request.get_json() or {}
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'error': 'user_id required'}), 400
    with get_db() as conn:
        row = conn.execute('SELECT last_daily_bonus, experience FROM users WHERE user_id=?', (user_id,)).fetchone()
        if not row:
            return jsonify({'error': 'not found'}), 404
        last_bonus = row['last_daily_bonus'] or 0
        current_time = time.time()
        # Проверяем премиум как в боте: 600с премиум / 900с обычный
        with get_db() as conn2:
            prem_row = conn2.execute('SELECT premium FROM users WHERE user_id=?', (user_id,)).fetchone()
            is_prem = bool(prem_row and prem_row['premium']) if prem_row else False
        cooldown = 600 if is_prem else 900
        if last_bonus > 0 and (current_time - last_bonus) < cooldown:
            time_left = int(cooldown - (current_time - last_bonus))
            return jsonify({'error': 'Too soon', 'time_left': time_left}), 429
        lv = get_level_from_exp(row['experience'] or 0)
        bonus_amount = int(5000 * (1 + (lv - 1) * 0.05))
        bonus_xp = 500
        conn.execute('UPDATE users SET balance=balance+?, experience=experience+?, last_daily_bonus=? WHERE user_id=?',
                     (bonus_amount, bonus_xp, current_time, user_id))
        new_row = conn.execute('SELECT balance, experience FROM users WHERE user_id=?', (user_id,)).fetchone()
        new_level = get_level_from_exp(new_row['experience'])
        emoji, tname, tcolor = build_user_title(new_level)
        lv_xp_start = sum(1000 + i*500 for i in range(new_level-1))
        exp_current = max(0, new_row['experience'] - lv_xp_start)
        exp_needed  = 1000 + (new_level-1)*500 if new_level < 50 else 0
        return jsonify({
            'success': True,
            'bonus_amount': bonus_amount,
            'bonus_xp': bonus_xp,
            'new_balance': new_row['balance'],
            'new_level': new_level,
            'new_experience': new_row['experience'],
            'exp_current': exp_current,
            'exp_needed': exp_needed,
            'title_emoji': emoji,
            'title_name': tname,
            'title_color': tcolor,
            'last_daily_bonus': current_time
        })


# ══════════════════════════════════════════════
#  CRASH GAME
# ══════════════════════════════════════════════

import threading as _threading
import random as _random

_crash_state = {
    'phase': 'betting',
    'game_id': 0,
    'start_time': 0.0,
    'crash_at': 2.0,
    'bets': {},
    'cashed_out': {},
    'history': [],
    'betting_ends': 0.0,
    'lock': _threading.Lock(),
}

def _crash_gen_point():
    r = _random.random()
    if r < 0.05:
        return 1.0
    return round(min(0.95 / (1 - r), 100.0), 2)

def _crash_current_mult():
    if _crash_state['phase'] != 'flying':
        return 1.0
    elapsed = time.time() - _crash_state['start_time']
    return round(min(pow(2.718281828, 0.06 * elapsed), _crash_state['crash_at']), 2)

def _crash_loop():
    import time as _t
    while True:
        with _crash_state['lock']:
            _crash_state['phase'] = 'betting'
            _crash_state['game_id'] += 1
            _crash_state['bets'] = {}
            _crash_state['cashed_out'] = {}
            _crash_state['crash_at'] = _crash_gen_point()
            _crash_state['betting_ends'] = _t.time() + 8
        _t.sleep(8)

        with _crash_state['lock']:
            _crash_state['phase'] = 'flying'
            _crash_state['start_time'] = _t.time()

        crash_at = _crash_state['crash_at']
        while True:
            mult = _crash_current_mult()
            if mult >= crash_at:
                break
            _t.sleep(0.1)

        with _crash_state['lock']:
            _crash_state['phase'] = 'crashed'
            final_mult = _crash_state['crash_at']
            _crash_state['history'].insert(0, final_mult)
            _crash_state['history'] = _crash_state['history'][:20]

        _t.sleep(3)

_threading.Thread(target=_crash_loop, daemon=True, name='CrashLoop').start()


@app.route('/api/crash/state')
def crash_state():
    user_id = request.args.get('user_id')
    with _crash_state['lock']:
        phase = _crash_state['phase']
        mult = _crash_current_mult() if phase == 'flying' else 1.0
        elapsed = (time.time() - _crash_state['start_time']) if phase == 'flying' else 0
        has_bet = str(user_id) in _crash_state['bets'] if user_id else False
        cashed = str(user_id) in _crash_state['cashed_out'] if user_id else False
        # Собираем список игроков с именами
        bets_snap = dict(_crash_state['bets'])
        co_snap   = dict(_crash_state['cashed_out'])
        players = []
        if bets_snap:
            uid_list = list(bets_snap.keys())
            placeholders = ','.join('?' * len(uid_list))
            with get_db() as pconn:
                rows = pconn.execute(
                    f'SELECT user_id, first_name, custom_name FROM users WHERE user_id IN ({placeholders})',
                    uid_list
                ).fetchall()
            names = {str(r['user_id']): r['custom_name'] or r['first_name'] or f'Игрок {str(r["user_id"])[-4:]}' for r in rows}
            for uid, bet in bets_snap.items():
                co = co_snap.get(uid)
                players.append({
                    'user_id': uid,
                    'name': names.get(str(uid), f'Игрок {str(uid)[-4:]}'),
                    'bet': bet,
                    'cashed_out': co is not None,
                    'cashed_at': round(co, 2) if co else None,
                })
            # Сначала вышедшие, потом в игре
            players.sort(key=lambda p: (not p['cashed_out'], -(p.get('cashed_at') or 0)))

        return jsonify({
            'phase': phase,
            'game_id': _crash_state['game_id'],
            'multiplier': mult,
            'elapsed': round(elapsed, 2),
            'crash_at': _crash_state['crash_at'] if phase == 'crashed' else None,
            'last_crash': _crash_state['history'][0] if _crash_state['history'] else None,
            'history': _crash_state['history'][:10],
            'has_bet': has_bet,
            'cashed_out': cashed,
            'time_to_start': max(0, round(_crash_state['betting_ends'] - time.time(), 1)) if phase == 'betting' else 0,
            'players': players,
        })


@app.route('/api/crash/bet', methods=['POST'])
def crash_bet():
    data = request.get_json() or {}
    user_id = str(data.get('user_id', ''))
    bet = int(data.get('bet', 0))
    if not user_id or bet < 100:
        return jsonify({'error': 'invalid params'}), 400
    with _crash_state['lock']:
        if _crash_state['phase'] != 'betting':
            return jsonify({'error': 'Ставки закрыты'}), 400
        if user_id in _crash_state['bets']:
            return jsonify({'error': 'Ставка уже сделана'}), 400
        with get_db() as conn:
            row = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
            if not row or row['balance'] < bet:
                return jsonify({'error': 'Недостаточно средств'}), 400
            conn.execute('UPDATE users SET balance=balance-? WHERE user_id=?', (bet, user_id))
            new_bal = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()['balance']
        _crash_state['bets'][user_id] = bet
    return jsonify({'success': True, 'new_balance': new_bal, 'bet': bet})


@app.route('/api/crash/cashout', methods=['POST'])
def crash_cashout():
    data = request.get_json() or {}
    user_id = str(data.get('user_id', ''))
    if not user_id:
        return jsonify({'error': 'user_id required'}), 400
    with _crash_state['lock']:
        if _crash_state['phase'] != 'flying':
            return jsonify({'error': 'Игра не в полёте'}), 400
        if user_id not in _crash_state['bets']:
            return jsonify({'error': 'Нет ставки'}), 400
        if user_id in _crash_state['cashed_out']:
            return jsonify({'error': 'Уже выведено'}), 400
        mult = _crash_current_mult()
        bet = _crash_state['bets'][user_id]
        win_amount = int(bet * mult)
        xp_gain = max(10, int(win_amount * 0.01))
        _crash_state['cashed_out'][user_id] = mult
        with get_db() as conn:
            conn.execute(
                'UPDATE users SET balance=balance+?, experience=experience+?, games_won=games_won+1, total_won_amount=total_won_amount+? WHERE user_id=?',
                (win_amount, xp_gain, win_amount, user_id)
            )
            conn.execute(
                'INSERT INTO game_history (user_id,game,bet,result,win_amount) VALUES (?,?,?,?,?)',
                (user_id, 'crash', bet, 'win', win_amount)
            )
            new_bal = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()['balance']
    return jsonify({'success': True, 'multiplier': mult, 'win_amount': win_amount, 'new_balance': new_bal})
