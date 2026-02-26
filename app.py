import os
import json
import math
import random
import sqlite3
import threading
import subprocess
import time as time_module
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='public')

PORT = int(os.environ.get('PORT', 3000))
DB_PATH = os.path.join(os.path.dirname(__file__), 'game.db')

# ── DB helpers ──

def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    return conn

def db_get(sql, params=()):
    conn = get_conn()
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def db_all(sql, params=()):
    conn = get_conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def db_run(sql, params=()):
    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()

# ── Number formatting ──

def fmt(n):
    """1000 -> '1 000'"""
    return f"{int(n):,}".replace(',', ' ')

# ── Level helpers ──

def get_level_from_exp(exp):
    level, total = 1, 0
    while level < 50:
        needed = 1000 + (level - 1) * 500
        if total + needed > exp:
            break
        total += needed
        level += 1
    return level

def get_exp_for_level(level):
    total = 0
    for l in range(1, level):
        total += 1000 + (l - 1) * 500
    return total

TITLE_MAP = {
    1:'🥉 Bronze I',2:'🥉 Bronze II',3:'🥉 Bronze III',4:'🥉 Bronze IV',
    5:'🥉 Bronze V',6:'🥉 Bronze VI',7:'🥉 Bronze VII',8:'🥉 Bronze VIII',
    9:'🥈 Silver I',10:'🥈 Silver II',11:'🥈 Silver III',12:'🥈 Silver IV',
    13:'🥈 Silver V',14:'🥈 Silver VI',15:'🥈 Silver VII',16:'🥈 Silver VIII',
    17:'🥇 Gold I',18:'🥇 Gold II',19:'🥇 Gold III',20:'🥇 Gold IV',
    21:'🥇 Gold V',22:'🥇 Gold VI',23:'🥇 Gold VII',24:'🥇 Gold VIII',
    25:'💎 Diamond I',26:'💎 Diamond II',27:'💎 Diamond III',28:'💎 Diamond IV',
    29:'💎 Diamond V',30:'💎 Diamond VI',31:'💎 Diamond VII',32:'💎 Diamond VIII',
    33:'🔮 The Mythic I',34:'🔮 The Mythic II',35:'🔮 The Mythic III',36:'🔮 The Mythic IV',
    37:'🔮 The Mythic V',38:'🔮 The Mythic VI',39:'🔮 The Mythic VII',40:'🔮 The Mythic VIII',
    41:'👑 Legend I',42:'👑 Legend II',43:'👑 Legend III',44:'👑 Legend IV',
    45:'👑 Legend V',46:'👑 Legend VI',47:'👑 Legend VII',48:'👑 Legend VIII',
    49:'👑 Legend IX',50:'👑 Legend X',
}
TITLE_COLORS = {
    '🥉':'#cd7f32','🥈':'#c0c0c0','🥇':'#ffd700',
    '💎':'#b9f2ff','🔮':'#bf00ff','👑':'#ff4500'
}

def get_title(level):
    t = TITLE_MAP.get(level, '👑 Legend X')
    parts = t.split(' ', 1)
    emoji = parts[0]
    name = parts[1] if len(parts) > 1 else ''
    color = TITLE_COLORS.get(emoji, '#fff')
    return {'full': t, 'emoji': emoji, 'name': name, 'color': color}

def migrate_db():
    """Add created_at column to users if missing"""
    conn = get_conn()
    try:
        conn.execute("ALTER TABLE users ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        conn.commit()
    except Exception:
        pass  # column already exists
    finally:
        conn.close()

migrate_db()

def get_user_internal_id(user_id):
    """Return sequential registration number (1 = first registered)"""
    row = db_get(
        "SELECT COUNT(*) as cnt FROM users WHERE rowid <= (SELECT rowid FROM users WHERE user_id=?)",
        (user_id,)
    )
    return row['cnt'] if row else 0

def get_days_playing(row):
    """Days since registration"""
    import time as _t
    created = row.get('created_at')
    if not created:
        return None
    try:
        import datetime
        if isinstance(created, (int, float)):
            ts = float(created)
        else:
            # Parse ISO string
            dt = datetime.datetime.fromisoformat(str(created).replace('Z',''))
            ts = dt.timestamp()
        days = int((_t.time() - ts) / 86400)
        return max(0, days)
    except Exception:
        return None

def build_user_info(row, rank=None):
    exp = row.get('experience') or 0
    level = get_level_from_exp(exp)
    exp_start = get_exp_for_level(level)
    exp_needed = (1000 + (level - 1) * 500) if level < 50 else 0
    title = get_title(level)
    days = get_days_playing(row)
    result = {
        'user_id': row.get('user_id'),
        'name': row.get('custom_name') or row.get('first_name') or row.get('username') or 'Игрок',
        'balance': row.get('balance') or 0,
        'balance_fmt': fmt(row.get('balance') or 0),
        'experience': exp,
        'level': level,
        'title_emoji': title['emoji'],
        'title_name': title['name'],
        'title_color': title['color'],
        'exp_current': exp - exp_start,
        'exp_needed': exp_needed,
        'games_won': row.get('games_won') or 0,
        'games_lost': row.get('games_lost') or 0,
        'total_won': row.get('total_won_amount') or 0,
        'total_lost': row.get('total_lost_amount') or 0,
        'days_playing': days,
    }
    if rank is not None:
        result['rank'] = rank
    return result

# ── Taxi config (совместимо с bot.py) ──

DRIVER_CLASSES = {
    'economy':  {'name': '🚕 Стандарт', 'emoji': '🚕', 'price_multiplier': 1.3,  'experience_bonus': 0.0},
    'comfort':  {'name': '🚙 Комфорт+', 'emoji': '🚙', 'price_multiplier': 1.55, 'experience_bonus': 0.1},
    'business': {'name': '🏎️ Бизнес',  'emoji': '🏎️','price_multiplier': 1.8,  'experience_bonus': 0.2},
    'vip':      {'name': '👑 Премиум',  'emoji': '👑', 'price_multiplier': 2.3,  'experience_bonus': 0.3},
}

# Маршруты с теми же ключами что в bot.py
ROUTES = [
    {'id':1,'name':'📍 Центр -> Аэропорт',             'distance':'25 км','time':'5 мин','min_time':5,'base_price':1500,'variation':0.2},
    {'id':2,'name':'🏠 Жилой район -> Офисный центр',  'distance':'15 км','time':'4 мин','min_time':4,'base_price':1000,'variation':0.15},
    {'id':3,'name':'🎓 Университет -> Торговый центр', 'distance':'12 км','time':'3 мин','min_time':3,'base_price':800, 'variation':0.1},
    {'id':4,'name':'🏥 Больница -> Ж/Д вокзал',        'distance':'18 км','time':'4 мин','min_time':4,'base_price':1200,'variation':0.18},
    {'id':5,'name':'🏢 Бизнес-центр -> Ресторан',      'distance':'10 км','time':'3 мин','min_time':3,'base_price':600, 'variation':0.12},
    {'id':6,'name':'🛍️ Торговый центр -> Кинотеатр',  'distance':'8 км', 'time':'3 мин','min_time':3,'base_price':500, 'variation':0.1},
    {'id':7,'name':'🌃 Ночной рейс',                   'distance':'30 км','time':'6 мин','min_time':6,'base_price':2000,'variation':0.25},
    {'id':8,'name':'🚄 Вокзал -> Гостиница',           'distance':'7 км', 'time':'3 мин','min_time':3,'base_price':400, 'variation':0.08},
]

def get_driver_class(trips):
    if trips >= 300: return 'vip'
    if trips >= 150: return 'business'
    if trips >= 100: return 'comfort'
    return 'economy'

# ── Slots ──

SLOT_SYMS = [
    {'sym':'🍋','weight':30,'mult':1.5},
    {'sym':'🍊','weight':25,'mult':2.0},
    {'sym':'🍇','weight':20,'mult':2.5},
    {'sym':'🍒','weight':12,'mult':4.0},
    {'sym':'⭐','weight':7, 'mult':8.0},
    {'sym':'🔔','weight':4, 'mult':15.0},
    {'sym':'💎','weight':1.5,'mult':35.0},
    {'sym':'7️⃣','weight':0.5,'mult':100.0},
]

def spin_reel():
    total = sum(s['weight'] for s in SLOT_SYMS)
    r = random.random() * total
    for s in SLOT_SYMS:
        r -= s['weight']
        if r <= 0:
            return s
    return SLOT_SYMS[0]

# ── Blackjack helpers ──

def make_deck():
    suits = ['♠','♥','♦','♣']
    ranks = ['2','3','4','5','6','7','8','9','10','J','Q','K','A']
    deck = [{'rank': r, 'suit': s} for s in suits for r in ranks]
    random.shuffle(deck)
    return deck

def card_value(card):
    if card['rank'] in ['J','Q','K']: return 10
    if card['rank'] == 'A': return 11
    return int(card['rank'])

def hand_total(hand):
    total = sum(card_value(c) for c in hand)
    aces = sum(1 for c in hand if c['rank'] == 'A')
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total

def card_str(card):
    return card['rank'] + card['suit']

# Хранилище сессий блэкджека
blackjack_games = {}

# ── CORS ──

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options_handler(path):
    return '', 204

# ══════════════════════════════════════════
# API: USER
# ══════════════════════════════════════════

@app.route('/api/user/<int:uid>', methods=['GET'])
def get_user(uid):
    row = db_get(
        'SELECT user_id,username,first_name,custom_name,balance,experience,'
        'games_won,games_lost,total_won_amount,total_lost_amount,created_at FROM users WHERE user_id=?',
        (uid,)
    )
    if not row:
        return jsonify({'error': 'User not found'}), 404
    info = build_user_info(row)
    info['internal_id'] = get_user_internal_id(uid)
    return jsonify(info)

@app.route('/api/leaderboard', methods=['GET'])
def leaderboard():
    rows = db_all(
        'SELECT user_id,username,first_name,custom_name,balance,experience '
        'FROM users ORDER BY balance DESC LIMIT 50'
    )
    return jsonify([build_user_info(r, rank=i+1) for i, r in enumerate(rows)])

@app.route('/api/leaderboard/exp', methods=['GET'])
def leaderboard_exp():
    rows = db_all(
        'SELECT user_id,username,first_name,custom_name,balance,experience '
        'FROM users ORDER BY experience DESC LIMIT 50'
    )
    return jsonify([build_user_info(r, rank=i+1) for i, r in enumerate(rows)])

# ══════════════════════════════════════════
# API: TAXI
# ══════════════════════════════════════════

@app.route('/api/taxi/routes', methods=['GET'])
def taxi_routes():
    uid = int(request.args.get('user_id', 0))
    mult = 1.3
    if uid:
        drv = db_get('SELECT trips_completed FROM driver_stats WHERE user_id=?', (uid,))
        trips = drv['trips_completed'] if drv else 0
        mult = DRIVER_CLASSES[get_driver_class(trips)]['price_multiplier']
    return jsonify([{
        'id': r['id'], 'name': r['name'],
        'distance': r['distance'], 'time': r['time'],
        'min_time': r['min_time'],
        'price': round(r['base_price'] * mult),
        'price_fmt': fmt(round(r['base_price'] * mult)),
    } for r in ROUTES])

@app.route('/api/taxi/status', methods=['GET'])
def taxi_status():
    uid = int(request.args.get('user_id', 0))
    row = db_get('SELECT trip_data, start_time FROM active_trips WHERE user_id=? AND finish_time IS NULL', (uid,))
    if not row:
        return jsonify({'active': False})
    trip = json.loads(row['trip_data'])
    elapsed = int(time_module.time()) - row['start_time']
    # Поддержка обоих форматов ключей
    if 'min_time' in trip:
        required = trip['min_time'] * 60
    else:
        try:
            required = int(str(trip.get('time', '5 мин')).split()[0]) * 60
        except:
            required = 300
    return jsonify({
        'active': True, 'trip': trip,
        'elapsed': elapsed, 'required': required,
        'done': elapsed >= required,
        'price_fmt': fmt(trip.get('price', 0)),
    })

@app.route('/api/taxi/stats', methods=['GET'])
def taxi_stats():
    uid = int(request.args.get('user_id', 0))
    row = db_get('SELECT trips_completed, total_earned FROM driver_stats WHERE user_id=?', (uid,))
    trips = row['trips_completed'] if row else 0
    earned = row['total_earned'] if row else 0
    dc = get_driver_class(trips)
    next_map = {'economy': 120, 'comfort': 170, 'business': 333, 'vip': None}
    return jsonify({
        'trips_completed': trips,
        'total_earned': earned,
        'total_earned_fmt': fmt(earned),
        'driver_class': dc,
        'driver_class_info': DRIVER_CLASSES[dc],
        'next_threshold': next_map[dc],
    })

@app.route('/api/taxi/start', methods=['POST'])
def taxi_start():
    data = request.get_json(force=True) or {}
    uid = int(data.get('user_id', 0))
    route_id = int(data.get('route_id', 0))
    route = next((r for r in ROUTES if r['id'] == route_id), None)
    if not route:
        return jsonify({'error': 'Route not found'}), 404

    drv = db_get('SELECT trips_completed FROM driver_stats WHERE user_id=?', (uid,))
    trips = drv['trips_completed'] if drv else 0
    if not drv:
        db_run('INSERT OR IGNORE INTO driver_stats (user_id) VALUES (?)', (uid,))

    dc = get_driver_class(trips)
    mult = DRIVER_CLASSES[dc]['price_multiplier']
    random_factor = 1 + random.uniform(-route['variation'], route['variation'])
    price = round(route['base_price'] * random_factor * mult)
    now = int(time_module.time())

    # Структура полностью совпадает с bot.py
    trip_data = {
        'id': route['id'],
        'name': route['name'],
        'distance': route['distance'],
        'time': route['time'],
        'min_time': route['min_time'],
        'base_price': round(route['base_price'] * random_factor),
        'price': price,
        'experience': 250,
        'class': dc,
        'class_multiplier': mult,
        'created_at': now,
    }
    db_run('DELETE FROM active_trips WHERE user_id=?', (uid,))
    db_run('INSERT INTO active_trips (user_id, trip_data, start_time) VALUES (?,?,?)',
           (uid, json.dumps(trip_data), now))
    return jsonify({
        'trip': trip_data,
        'duration_seconds': route['min_time'] * 60,
        'price_fmt': fmt(price),
    })

@app.route('/api/taxi/complete', methods=['POST'])
def taxi_complete():
    data = request.get_json(force=True) or {}
    uid = int(data.get('user_id', 0))
    row = db_get('SELECT trip_data, start_time FROM active_trips WHERE user_id=? AND finish_time IS NULL', (uid,))
    if not row:
        return jsonify({'error': 'Нет активного рейса'}), 400
    trip = json.loads(row['trip_data'])
    elapsed = int(time_module.time()) - row['start_time']

    if 'min_time' in trip:
        required = trip['min_time'] * 60
    else:
        try:
            required = int(str(trip.get('time', '5 мин')).split()[0]) * 60
        except:
            required = 300

    if elapsed < required - 2:
        return jsonify({'error': 'Рейс ещё не завершён'}), 400

    price = trip['price']
    db_run('UPDATE users SET balance=balance+?, experience=experience+250 WHERE user_id=?', (price, uid))
    db_run('UPDATE driver_stats SET trips_completed=trips_completed+1, total_earned=total_earned+? WHERE user_id=?', (price, uid))
    db_run('UPDATE active_trips SET finish_time=? WHERE user_id=? AND finish_time IS NULL', (int(time_module.time()), uid))

    updated = db_get('SELECT balance FROM users WHERE user_id=?', (uid,))
    stats = db_get('SELECT trips_completed, total_earned FROM driver_stats WHERE user_id=?', (uid,))
    dc = get_driver_class(stats['trips_completed'])
    return jsonify({
        'success': True,
        'earned': price, 'earned_fmt': fmt(price),
        'exp_gain': 250,
        'new_balance': updated['balance'], 'new_balance_fmt': fmt(updated['balance']),
        'trips_completed': stats['trips_completed'],
        'driver_class': dc,
        'driver_class_info': DRIVER_CLASSES[dc],
    })

# ══════════════════════════════════════════
# API: SLOTS
# ══════════════════════════════════════════

@app.route('/api/slots/spin', methods=['POST'])
def slots_spin():
    data = request.get_json(force=True) or {}
    uid = int(data.get('user_id', 0))
    bet_amt = int(data.get('bet', 100))
    row = db_get('SELECT balance FROM users WHERE user_id=?', (uid,))
    if not row:
        return jsonify({'error': 'User not found'}), 404
    if row['balance'] < bet_amt:
        return jsonify({'error': 'Недостаточно средств'}), 400

    reels = [spin_reel(), spin_reel(), spin_reel()]
    syms = [r['sym'] for r in reels]
    win, win_type = 0, None

    if syms[0] == syms[1] == syms[2]:
        win = round(bet_amt * reels[0]['mult'])
        win_type = 'jackpot'
    elif syms[0]==syms[1] or syms[1]==syms[2] or syms[0]==syms[2]:
        m = reels[0]['mult'] if syms[0]==syms[1] else (reels[1]['mult'] if syms[1]==syms[2] else reels[0]['mult'])
        win = round(bet_amt * m * 0.3)
        win_type = 'pair'

    net = win - bet_amt
    exp_gain = max(10, math.floor(bet_amt / 100))
    db_run('UPDATE users SET balance=balance+?, experience=experience+? WHERE user_id=?', (net, exp_gain, uid))
    if win > 0:
        db_run('UPDATE users SET games_won=games_won+1, total_won_amount=total_won_amount+? WHERE user_id=?', (win, uid))
    else:
        db_run('UPDATE users SET games_lost=games_lost+1, total_lost_amount=total_lost_amount+? WHERE user_id=?', (bet_amt, uid))

    updated = db_get('SELECT balance FROM users WHERE user_id=?', (uid,))
    return jsonify({
        'reels': syms, 'win': win, 'win_fmt': fmt(win),
        'bet': bet_amt, 'net': net, 'win_type': win_type,
        'new_balance': updated['balance'], 'new_balance_fmt': fmt(updated['balance']),
    })

# ══════════════════════════════════════════
# API: ROULETTE
# ══════════════════════════════════════════

RED_NUMBERS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}

@app.route('/api/roulette/spin', methods=['POST'])
def roulette_spin():
    data = request.get_json(force=True) or {}
    uid = int(data.get('user_id', 0))
    bet_amt = int(data.get('bet', 100))
    bet_type = data.get('bet_type', 'red')   # red | black | green | even | odd | number
    bet_number = int(data.get('bet_number', 0))

    row = db_get('SELECT balance FROM users WHERE user_id=?', (uid,))
    if not row:
        return jsonify({'error': 'User not found'}), 404
    if row['balance'] < bet_amt:
        return jsonify({'error': 'Недостаточно средств'}), 400

    number = random.randint(0, 36)
    if number == 0:       color = 'green'
    elif number in RED_NUMBERS: color = 'red'
    else:                 color = 'black'

    win, win_type = 0, None
    if bet_type == 'number' and number == bet_number:
        win = bet_amt * 36;  win_type = 'number'
    elif bet_type == 'green' and color == 'green':
        win = bet_amt * 14;  win_type = 'green'
    elif bet_type == 'red' and color == 'red':
        win = bet_amt * 2;   win_type = 'red'
    elif bet_type == 'black' and color == 'black':
        win = bet_amt * 2;   win_type = 'black'
    elif bet_type == 'even' and number != 0 and number % 2 == 0:
        win = bet_amt * 2;   win_type = 'even'
    elif bet_type == 'odd' and number % 2 == 1:
        win = bet_amt * 2;   win_type = 'odd'

    net = win - bet_amt
    exp_gain = max(10, math.floor(bet_amt / 100))
    db_run('UPDATE users SET balance=balance+?, experience=experience+? WHERE user_id=?', (net, exp_gain, uid))
    if win > 0:
        db_run('UPDATE users SET games_won=games_won+1, total_won_amount=total_won_amount+? WHERE user_id=?', (win, uid))
    else:
        db_run('UPDATE users SET games_lost=games_lost+1, total_lost_amount=total_lost_amount+? WHERE user_id=?', (bet_amt, uid))

    updated = db_get('SELECT balance FROM users WHERE user_id=?', (uid,))
    return jsonify({
        'number': number, 'color': color,
        'win': win, 'win_fmt': fmt(win),
        'bet': bet_amt, 'net': net, 'win_type': win_type,
        'new_balance': updated['balance'], 'new_balance_fmt': fmt(updated['balance']),
    })

# ══════════════════════════════════════════
# API: BLACKJACK
# ══════════════════════════════════════════

@app.route('/api/blackjack/deal', methods=['POST'])
def blackjack_deal():
    data = request.get_json(force=True) or {}
    uid = int(data.get('user_id', 0))
    bet_amt = int(data.get('bet', 100))

    row = db_get('SELECT balance FROM users WHERE user_id=?', (uid,))
    if not row:
        return jsonify({'error': 'User not found'}), 404
    if row['balance'] < bet_amt:
        return jsonify({'error': 'Недостаточно средств'}), 400

    db_run('UPDATE users SET balance=balance-? WHERE user_id=?', (bet_amt, uid))

    deck = make_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    blackjack_games[uid] = {'deck': deck, 'player': player, 'dealer': dealer, 'bet': bet_amt, 'status': 'playing'}

    player_total = hand_total(player)
    updated = db_get('SELECT balance FROM users WHERE user_id=?', (uid,))
    return jsonify({
        'player': [card_str(c) for c in player],
        'dealer_visible': card_str(dealer[0]),
        'player_total': player_total,
        'blackjack': player_total == 21,
        'status': 'blackjack' if player_total == 21 else 'playing',
        'balance': updated['balance'], 'balance_fmt': fmt(updated['balance']),
    })

@app.route('/api/blackjack/hit', methods=['POST'])
def blackjack_hit():
    data = request.get_json(force=True) or {}
    uid = int(data.get('user_id', 0))
    game = blackjack_games.get(uid)
    if not game or game['status'] != 'playing':
        return jsonify({'error': 'Нет активной игры'}), 400

    card = game['deck'].pop()
    game['player'].append(card)
    total = hand_total(game['player'])

    if total > 21:
        game['status'] = 'bust'
        db_run('UPDATE users SET games_lost=games_lost+1, total_lost_amount=total_lost_amount+? WHERE user_id=?',
               (game['bet'], uid))
        updated = db_get('SELECT balance FROM users WHERE user_id=?', (uid,))
        return jsonify({
            'player': [card_str(c) for c in game['player']],
            'player_total': total, 'status': 'bust', 'win': 0,
            'new_balance': updated['balance'], 'new_balance_fmt': fmt(updated['balance']),
        })

    updated = db_get('SELECT balance FROM users WHERE user_id=?', (uid,))
    return jsonify({
        'player': [card_str(c) for c in game['player']],
        'player_total': total, 'status': 'playing',
        'balance': updated['balance'], 'balance_fmt': fmt(updated['balance']),
    })

@app.route('/api/blackjack/stand', methods=['POST'])
def blackjack_stand():
    data = request.get_json(force=True) or {}
    uid = int(data.get('user_id', 0))
    game = blackjack_games.get(uid)
    if not game or game['status'] != 'playing':
        return jsonify({'error': 'Нет активной игры'}), 400

    dealer, deck = game['dealer'], game['deck']
    while hand_total(dealer) < 17:
        dealer.append(deck.pop())

    p = hand_total(game['player'])
    d = hand_total(dealer)
    bet = game['bet']

    if d > 21 or p > d:
        win = bet * 2; result = 'win'
        db_run('UPDATE users SET balance=balance+?, games_won=games_won+1, total_won_amount=total_won_amount+? WHERE user_id=?',
               (win, win, uid))
    elif p == d:
        win = bet; result = 'push'
        db_run('UPDATE users SET balance=balance+? WHERE user_id=?', (win, uid))
    else:
        win = 0; result = 'lose'
        db_run('UPDATE users SET games_lost=games_lost+1, total_lost_amount=total_lost_amount+? WHERE user_id=?', (bet, uid))

    db_run('UPDATE users SET experience=experience+? WHERE user_id=?', (max(10, math.floor(bet/100)), uid))
    game['status'] = result
    updated = db_get('SELECT balance FROM users WHERE user_id=?', (uid,))
    return jsonify({
        'player': [card_str(c) for c in game['player']],
        'dealer': [card_str(c) for c in dealer],
        'player_total': p, 'dealer_total': d,
        'result': result, 'win': win, 'win_fmt': fmt(win),
        'new_balance': updated['balance'], 'new_balance_fmt': fmt(updated['balance']),
    })

# ══════════════════════════════════════════
# API: COIN FLIP
# ══════════════════════════════════════════

@app.route('/api/coin/flip', methods=['POST'])
def coin_flip():
    data = request.get_json(force=True) or {}
    uid = int(data.get('user_id', 0))
    bet_amt = int(data.get('bet', 100))
    choice = data.get('choice', 'heads')  # heads / tails

    row = db_get('SELECT balance FROM users WHERE user_id=?', (uid,))
    if not row:
        return jsonify({'error': 'User not found'}), 404
    if row['balance'] < bet_amt:
        return jsonify({'error': 'Недостаточно средств'}), 400

    result = random.choice(['heads', 'tails'])
    won = result == choice
    win = bet_amt * 2 if won else 0
    net = win - bet_amt
    exp_gain = max(10, math.floor(bet_amt / 100))

    db_run('UPDATE users SET balance=balance+?, experience=experience+? WHERE user_id=?', (net, exp_gain, uid))
    if won:
        db_run('UPDATE users SET games_won=games_won+1, total_won_amount=total_won_amount+? WHERE user_id=?', (win, uid))
    else:
        db_run('UPDATE users SET games_lost=games_lost+1, total_lost_amount=total_lost_amount+? WHERE user_id=?', (bet_amt, uid))

    updated = db_get('SELECT balance FROM users WHERE user_id=?', (uid,))
    return jsonify({
        'result': result,
        'result_emoji': '👑' if result == 'heads' else '🦅',
        'choice': choice, 'won': won,
        'win': win, 'win_fmt': fmt(win),
        'bet': bet_amt, 'net': net,
        'new_balance': updated['balance'], 'new_balance_fmt': fmt(updated['balance']),
    })

# ══════════════════════════════════════════
# Static files
# ══════════════════════════════════════════

@app.route('/', defaults={'path': 'index.html'})
@app.route('/<path:path>')
def static_files(path):
    public = os.path.join(os.path.dirname(__file__), 'public')
    file_path = os.path.join(public, path)
    if os.path.isfile(file_path):
        return send_from_directory(public, path)
    return send_from_directory(public, 'index.html')

# ══════════════════════════════════════════
# Start bot in background
# ══════════════════════════════════════════

def start_bot():
    bot_path = os.path.join(os.path.dirname(__file__), 'bot.py')
    if os.path.exists(bot_path):
        proc = subprocess.Popen(['python', bot_path], stdout=None, stderr=None)
        print(f'✅ bot.py запущен (pid={proc.pid})')

if __name__ == '__main__':
    threading.Thread(target=start_bot, daemon=True).start()
    print(f'✅ Server running on port {PORT}')
    app.run(host='0.0.0.0', port=PORT, debug=False)
