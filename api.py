from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import sqlite3
import json
import time
import random
import hashlib
import hmac
import os
from functools import wraps

app = Flask(__name__, static_folder='public')
CORS(app)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'game.db')
BOT_TOKEN = '7885520897:AAFw9SBKB8eC0NWXkXbtlieN4M87Cc61TDE'

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def verify_telegram_data(init_data):
    """Verify Telegram WebApp init data"""
    try:
        parsed = {}
        for item in init_data.split('&'):
            key, _, val = item.partition('=')
            parsed[key] = val
        
        hash_val = parsed.pop('hash', '')
        data_check = '\n'.join(sorted(f'{k}={v}' for k, v in parsed.items()))
        
        secret_key = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
        
        return hmac.compare_digest(hash_val, calc_hash)
    except:
        return True  # В dev-режиме пропускаем

def get_user_id_from_request(req):
    """Extract user_id from Telegram init_data or query param"""
    init_data = req.headers.get('X-Telegram-Init-Data', '')
    if init_data:
        for item in init_data.split('&'):
            if item.startswith('user='):
                import urllib.parse
                user_json = urllib.parse.unquote(item[5:])
                user = json.loads(user_json)
                return user.get('id')
    # Fallback for dev
    return req.args.get('user_id') or req.json.get('user_id') if req.is_json else req.args.get('user_id')

def get_level_from_exp(experience):
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
    total = 0
    for l in range(1, level):
        total += 1000 + (l - 1) * 500
    return total

LEVEL_TITLES = {
    **{i: ('🥉', f'Bronze {["I","II","III","IV","V","VI","VII","VIII"][i-1]}') for i in range(1, 9)},
    **{i: ('🥈', f'Silver {["I","II","III","IV","V","VI","VII","VIII"][i-9]}') for i in range(9, 17)},
    **{i: ('🥇', f'Gold {["I","II","III","IV","V","VI","VII","VIII"][i-17]}') for i in range(17, 25)},
    **{i: ('💎', f'Diamond {["I","II","III","IV","V","VI","VII","VIII"][i-25]}') for i in range(25, 33)},
    **{i: ('🔮', f'The Mythic {["I","II","III","IV","V","VI","VII","VIII"][i-33]}') for i in range(33, 41)},
    **{i: ('👑', f'Legend {["I","II","III","IV","V","VI","VII","VIII","IX","X"][i-41]}') for i in range(41, 51)},
}

TITLE_COLORS = {
    '🥉': '#cd7f32', '🥈': '#c0c0c0', '🥇': '#ffd700',
    '💎': '#b9f2ff', '🔮': '#bf00ff', '👑': '#ff4500'
}

DRIVER_CLASSES = {
    "economy": {"name": "Стандарт", "emoji": "🚕", "min_rides": 0, "price_multiplier": 1.3},
    "comfort":  {"name": "Комфорт+", "emoji": "🚙", "min_rides": 120, "price_multiplier": 1.55},
    "business": {"name": "Бизнес", "emoji": "🏎️", "min_rides": 170, "price_multiplier": 1.8},
    "vip":      {"name": "Премиум", "emoji": "👑", "min_rides": 333, "price_multiplier": 2.3},
}

TAXI_ROUTES = [
    {"id": 1, "name": "Центр → Аэропорт", "distance": "25 км", "time_min": 5, "base_price": 1500},
    {"id": 2, "name": "Жилой р-н → Офис", "distance": "15 км", "time_min": 4, "base_price": 1000},
    {"id": 3, "name": "Университет → ТЦ", "distance": "12 км", "time_min": 3, "base_price": 800},
    {"id": 4, "name": "Больница → Вокзал", "distance": "18 км", "time_min": 4, "base_price": 1200},
    {"id": 5, "name": "Бизнес-центр → Ресторан", "distance": "10 км", "time_min": 3, "base_price": 600},
    {"id": 6, "name": "ТЦ → Кинотеатр", "distance": "8 км", "time_min": 3, "base_price": 500},
    {"id": 7, "name": "🌃 Ночной рейс", "distance": "30 км", "time_min": 6, "base_price": 2000},
    {"id": 8, "name": "Вокзал → Гостиница", "distance": "7 км", "time_min": 3, "base_price": 400},
]

# ==================== USER ENDPOINTS ====================

@app.route('/api/user/<int:user_id>', methods=['GET'])
def get_user(user_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('''SELECT user_id, username, first_name, custom_name, balance, experience,
                       games_won, games_lost, total_won_amount, total_lost_amount
                       FROM users WHERE user_id = ?''', (user_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'User not found'}), 404
        
        exp = row['experience'] or 0
        level = get_level_from_exp(exp)
        emoji, title = LEVEL_TITLES.get(level, ('🥉', 'Bronze I'))
        exp_start = get_exp_for_level(level)
        exp_needed = 1000 + (level - 1) * 500 if level < 50 else 0
        
        # Driver stats
        cur.execute('SELECT trips_completed, total_earned FROM driver_stats WHERE user_id = ?', (user_id,))
        drv = cur.fetchone()
        trips = drv['trips_completed'] if drv else 0
        driver_class = 'vip' if trips >= 300 else 'business' if trips >= 150 else 'comfort' if trips >= 100 else 'economy'
        
        return jsonify({
            'user_id': row['user_id'],
            'username': row['username'],
            'name': row['custom_name'] or row['first_name'] or row['username'] or 'Игрок',
            'balance': row['balance'] or 0,
            'experience': exp,
            'level': level,
            'title_emoji': emoji,
            'title_name': title,
            'title_color': TITLE_COLORS.get(emoji, '#fff'),
            'exp_current': exp - exp_start,
            'exp_needed': exp_needed,
            'games_won': row['games_won'] or 0,
            'games_lost': row['games_lost'] or 0,
            'total_won': row['total_won_amount'] or 0,
            'total_lost': row['total_lost_amount'] or 0,
            'trips_completed': trips,
            'driver_class': driver_class,
            'driver_class_info': DRIVER_CLASSES[driver_class],
        })
    finally:
        conn.close()

@app.route('/api/leaderboard', methods=['GET'])
def leaderboard():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('''SELECT user_id, username, first_name, custom_name, balance, experience
                       FROM users ORDER BY balance DESC LIMIT 50''')
        rows = cur.fetchall()
        result = []
        for i, row in enumerate(rows):
            exp = row['experience'] or 0
            level = get_level_from_exp(exp)
            emoji, title = LEVEL_TITLES.get(level, ('🥉', 'Bronze I'))
            result.append({
                'rank': i + 1,
                'user_id': row['user_id'],
                'name': row['custom_name'] or row['first_name'] or row['username'] or 'Игрок',
                'balance': row['balance'] or 0,
                'level': level,
                'title_emoji': emoji,
                'title_name': title,
                'title_color': TITLE_COLORS.get(emoji, '#fff'),
            })
        return jsonify(result)
    finally:
        conn.close()

@app.route('/api/leaderboard/exp', methods=['GET'])
def leaderboard_exp():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('''SELECT user_id, username, first_name, custom_name, balance, experience
                       FROM users ORDER BY experience DESC LIMIT 50''')
        rows = cur.fetchall()
        result = []
        for i, row in enumerate(rows):
            exp = row['experience'] or 0
            level = get_level_from_exp(exp)
            emoji, title = LEVEL_TITLES.get(level, ('🥉', 'Bronze I'))
            result.append({
                'rank': i + 1,
                'user_id': row['user_id'],
                'name': row['custom_name'] or row['first_name'] or row['username'] or 'Игрок',
                'experience': exp,
                'level': level,
                'title_emoji': emoji,
                'title_name': title,
                'title_color': TITLE_COLORS.get(emoji, '#fff'),
            })
        return jsonify(result)
    finally:
        conn.close()

# ==================== SLOTS ====================

SLOT_SYMBOLS = [
    {'sym': '🍋', 'weight': 30, 'mult': 1.5},
    {'sym': '🍊', 'weight': 25, 'mult': 2.0},
    {'sym': '🍇', 'weight': 20, 'mult': 2.5},
    {'sym': '🍒', 'weight': 12, 'mult': 4.0},
    {'sym': '⭐', 'weight': 7,  'mult': 8.0},
    {'sym': '🔔', 'weight': 4,  'mult': 15.0},
    {'sym': '💎', 'weight': 1.5,'mult': 35.0},
    {'sym': '7️⃣', 'weight': 0.5,'mult': 100.0},
]

def weighted_spin():
    total = sum(s['weight'] for s in SLOT_SYMBOLS)
    r = random.uniform(0, total)
    for s in SLOT_SYMBOLS:
        r -= s['weight']
        if r <= 0:
            return s
    return SLOT_SYMBOLS[0]

@app.route('/api/slots/spin', methods=['POST'])
def slots_spin():
    data = request.get_json()
    user_id = data.get('user_id')
    bet = int(data.get('bet', 100))
    
    if not user_id:
        return jsonify({'error': 'No user_id'}), 400
    
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'User not found'}), 404
        
        balance = row['balance'] or 0
        if balance < bet:
            return jsonify({'error': 'Недостаточно средств'}), 400
        
        # Spin
        reels = [weighted_spin() for _ in range(3)]
        syms = [r['sym'] for r in reels]
        
        win = 0
        win_type = None
        
        if syms[0] == syms[1] == syms[2]:
            # Jackpot - all 3 match
            mult = reels[0]['mult']
            win = int(bet * mult)
            win_type = 'jackpot'
        elif syms[0] == syms[1] or syms[1] == syms[2] or syms[0] == syms[2]:
            # 2 match
            # Find the matching symbol
            if syms[0] == syms[1]:
                mult = reels[0]['mult']
            elif syms[1] == syms[2]:
                mult = reels[1]['mult']
            else:
                mult = reels[0]['mult']
            win = int(bet * mult * 0.3)
            win_type = 'pair'
        
        net = win - bet
        new_balance = balance + net
        
        cur.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (net, user_id))
        
        # Track stats
        if win > 0:
            cur.execute('UPDATE users SET games_won = games_won + 1, total_won_amount = total_won_amount + ? WHERE user_id = ?', (win, user_id))
        else:
            cur.execute('UPDATE users SET games_lost = games_lost + 1, total_lost_amount = total_lost_amount + ? WHERE user_id = ?', (bet, user_id))
        
        # Add experience
        exp_gain = max(10, bet // 100)
        cur.execute('UPDATE users SET experience = experience + ? WHERE user_id = ?', (exp_gain, user_id))
        
        conn.commit()
        
        return jsonify({
            'reels': syms,
            'win': win,
            'bet': bet,
            'net': net,
            'win_type': win_type,
            'new_balance': new_balance,
            'exp_gain': exp_gain,
        })
    finally:
        conn.close()

# ==================== TAXI ====================

@app.route('/api/taxi/routes', methods=['GET'])
def taxi_routes():
    user_id = request.args.get('user_id')
    if user_id:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute('SELECT trips_completed FROM driver_stats WHERE user_id = ?', (int(user_id),))
            row = cur.fetchone()
            trips = row['trips_completed'] if row else 0
        finally:
            conn.close()
        
        driver_class = 'vip' if trips >= 300 else 'business' if trips >= 150 else 'comfort' if trips >= 100 else 'economy'
        mult = DRIVER_CLASSES[driver_class]['price_multiplier']
    else:
        mult = 1.3
    
    routes_with_price = []
    for r in TAXI_ROUTES:
        routes_with_price.append({
            **r,
            'price': int(r['base_price'] * mult),
        })
    return jsonify(routes_with_price)

@app.route('/api/taxi/start', methods=['POST'])
def taxi_start():
    data = request.get_json()
    user_id = int(data.get('user_id'))
    route_id = int(data.get('route_id'))
    
    route = next((r for r in TAXI_ROUTES if r['id'] == route_id), None)
    if not route:
        return jsonify({'error': 'Route not found'}), 404
    
    conn = get_db()
    try:
        cur = conn.cursor()
        
        # Get driver stats
        cur.execute('SELECT trips_completed, total_earned FROM driver_stats WHERE user_id = ?', (user_id,))
        drv = cur.fetchone()
        if not drv:
            cur.execute('INSERT OR IGNORE INTO driver_stats (user_id) VALUES (?)', (user_id,))
            trips = 0
        else:
            trips = drv['trips_completed']
        
        driver_class = 'vip' if trips >= 300 else 'business' if trips >= 150 else 'comfort' if trips >= 100 else 'economy'
        mult = DRIVER_CLASSES[driver_class]['price_multiplier']
        price = int(route['base_price'] * mult)
        
        # Variation ±15%
        variation = random.uniform(0.85, 1.15)
        final_price = int(price * variation)
        
        trip_data = {
            'route_id': route_id,
            'route_name': route['name'],
            'price': final_price,
            'distance': route['distance'],
            'time_min': route['time_min'],
            'driver_class': driver_class,
            'start_time': int(time.time()),
        }
        
        # Save trip
        cur.execute('DELETE FROM active_trips WHERE user_id = ?', (user_id,))
        cur.execute('''INSERT INTO active_trips (user_id, trip_data, start_time)
                       VALUES (?, ?, ?)''', (user_id, json.dumps(trip_data), int(time.time())))
        
        conn.commit()
        return jsonify({'trip': trip_data, 'duration_seconds': route['time_min'] * 60})
    finally:
        conn.close()

@app.route('/api/taxi/complete', methods=['POST'])
def taxi_complete():
    data = request.get_json()
    user_id = int(data.get('user_id'))
    
    conn = get_db()
    try:
        cur = conn.cursor()
        
        cur.execute('SELECT trip_data, start_time FROM active_trips WHERE user_id = ? AND finish_time IS NULL', (user_id,))
        row = cur.fetchone()
        
        if not row:
            return jsonify({'error': 'Нет активного рейса'}), 400
        
        trip_data = json.loads(row['trip_data'])
        elapsed = int(time.time()) - row['start_time']
        required = trip_data['time_min'] * 60
        
        if elapsed < required - 2:  # 2 sec grace
            return jsonify({'error': f'Рейс ещё не закончен. Осталось {required - elapsed} сек'}), 400
        
        price = trip_data['price']
        exp_gain = 250
        
        # Update balance and stats
        cur.execute('UPDATE users SET balance = balance + ?, experience = experience + ? WHERE user_id = ?',
                   (price, exp_gain, user_id))
        cur.execute('''UPDATE driver_stats SET 
                       trips_completed = trips_completed + 1,
                       total_earned = total_earned + ?
                       WHERE user_id = ?''', (price, user_id))
        cur.execute('UPDATE active_trips SET finish_time = ? WHERE user_id = ? AND finish_time IS NULL',
                   (int(time.time()), user_id))
        
        # Get new balance
        cur.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
        new_balance = cur.fetchone()['balance']
        
        # Get updated trips
        cur.execute('SELECT trips_completed FROM driver_stats WHERE user_id = ?', (user_id,))
        trips = cur.fetchone()['trips_completed']
        driver_class = 'vip' if trips >= 300 else 'business' if trips >= 150 else 'comfort' if trips >= 100 else 'economy'
        
        conn.commit()
        return jsonify({
            'success': True,
            'earned': price,
            'exp_gain': exp_gain,
            'new_balance': new_balance,
            'trips_completed': trips,
            'driver_class': driver_class,
            'driver_class_info': DRIVER_CLASSES[driver_class],
        })
    finally:
        conn.close()

@app.route('/api/taxi/status', methods=['GET'])
def taxi_status():
    user_id = int(request.args.get('user_id'))
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT trip_data, start_time FROM active_trips WHERE user_id = ? AND finish_time IS NULL', (user_id,))
        row = cur.fetchone()
        
        if not row:
            return jsonify({'active': False})
        
        trip_data = json.loads(row['trip_data'])
        elapsed = int(time.time()) - row['start_time']
        required = trip_data['time_min'] * 60
        
        return jsonify({
            'active': True,
            'trip': trip_data,
            'elapsed': elapsed,
            'required': required,
            'done': elapsed >= required,
        })
    finally:
        conn.close()

@app.route('/api/taxi/stats', methods=['GET'])
def taxi_stats():
    user_id = int(request.args.get('user_id'))
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT trips_completed, total_earned FROM driver_stats WHERE user_id = ?', (user_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'trips_completed': 0, 'total_earned': 0, 'driver_class': 'economy'})
        
        trips = row['trips_completed']
        driver_class = 'vip' if trips >= 300 else 'business' if trips >= 150 else 'comfort' if trips >= 100 else 'economy'
        
        # Next class progress
        next_map = {'economy': 120, 'comfort': 170, 'business': 333, 'vip': None}
        next_threshold = next_map[driver_class]
        
        return jsonify({
            'trips_completed': trips,
            'total_earned': row['total_earned'],
            'driver_class': driver_class,
            'driver_class_info': DRIVER_CLASSES[driver_class],
            'next_threshold': next_threshold,
        })
    finally:
        conn.close()

# ==================== SERVE FRONTEND ====================

@app.route('/')
@app.route('/<path:path>')
def serve(path=''):
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, 'index.html')

if __name__ == '__main__':
    import threading
    import importlib.util, sys

    def run_bot():
        try:
            spec = importlib.util.spec_from_file_location(
                'bot',
                os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot.py')
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules['bot'] = mod
            spec.loader.exec_module(mod)
        except Exception as e:
            print(f'[BOT ERROR] {e}')

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    print('✅ Бот запущен в фоне')

    app.run(host='0.0.0.0', port=3000, debug=False)
