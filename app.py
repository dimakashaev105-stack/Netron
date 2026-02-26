"""
Flask API для Telegram Mini App Casino + Taxi
"""

from flask import Flask, request, jsonify
import sqlite3, json, hmac, hashlib, time, random, threading, os
from contextlib import contextmanager
from urllib.parse import parse_qsl

app = Flask(__name__)

BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # ← вставь свой токен
DB_NAME   = "game.db"

# ──────────────────── DB ─────────────────────
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
            CREATE TABLE IF NOT EXISTS driver_stats (
                user_id INTEGER PRIMARY KEY, trips_completed INTEGER DEFAULT 0,
                total_earned INTEGER DEFAULT 0, driver_class TEXT DEFAULT "economy",
                last_trip_time INTEGER DEFAULT 0
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS active_trips (
                user_id INTEGER PRIMARY KEY, trip_data TEXT NOT NULL,
                start_time INTEGER NOT NULL, finish_time INTEGER
            )
        ''')

init_db()

# ──────────────────── AUTH ───────────────────
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

# ──────────────────── LEVELS ─────────────────
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

# ──────────────────── TAXI CONFIG ────────────
DRIVER_CLASSES = {
    'economy': {'mult':1.3,  'min_rides':0,   'emoji':'🚕'},
    'comfort': {'mult':1.55, 'min_rides':100,  'emoji':'🚙'},
    'business':{'mult':1.8,  'min_rides':150,  'emoji':'🏎️'},
    'vip':     {'mult':2.3,  'min_rides':300,  'emoji':'👑'},
}
TAXI_ROUTES = [
    {"id":1,"name":"📍 Центр -> Аэропорт","distance":"25 км","time":"5 мин","base_price":1500,"variation":0.2,"min_time":5},
    {"id":2,"name":"🏠 Жилой район -> Офисный центр","distance":"15 км","time":"4 мин","base_price":1000,"variation":0.15,"min_time":4},
    {"id":3,"name":"🎓 Университет -> Торговый центр","distance":"12 км","time":"3 мин","base_price":800,"variation":0.1,"min_time":3},
    {"id":4,"name":"🏥 Больница -> Ж/Д вокзал","distance":"18 км","time":"4 мин","base_price":1200,"variation":0.18,"min_time":4},
    {"id":5,"name":"🏢 Бизнес-центр -> Ресторан","distance":"10 км","time":"3 мин","base_price":600,"variation":0.12,"min_time":3},
    {"id":6,"name":"🛍️ Торговый центр -> Кинотеатр","distance":"8 км","time":"3 мин","base_price":500,"variation":0.1,"min_time":3},
    {"id":7,"name":"🌃 Ночной рейс","distance":"30 км","time":"6 мин","base_price":2000,"variation":0.25,"min_time":6},
    {"id":8,"name":"🚄 Вокзал -> Гостиница","distance":"7 км","time":"3 мин","base_price":400,"variation":0.08,"min_time":3},
]

def get_driver_class(trips):
    if trips >= 300: return 'vip'
    if trips >= 150: return 'business'
    if trips >= 100: return 'comfort'
    return 'economy'

def get_or_create_driver(conn, user_id):
    row = conn.execute(
        'SELECT trips_completed,total_earned,driver_class FROM driver_stats WHERE user_id=?',
        (user_id,)
    ).fetchone()
    if row: return dict(row)
    conn.execute('INSERT INTO driver_stats(user_id) VALUES(?)', (user_id,))
    return {'trips_completed':0,'total_earned':0,'driver_class':'economy'}

# ──────────────────── CORS ───────────────────
@app.after_request
def cors(resp):
    resp.headers['Access-Control-Allow-Origin']  = '*'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Telegram-Init-Data'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return resp

@app.route('/api/<path:p>', methods=['OPTIONS'])
def options(p): return '', 204

# ──────────────────── PING ───────────────────
@app.route('/ping')
def ping():
    return 'ok', 200

# ══════════════════════════════════════════════
#  USER
# ══════════════════════════════════════════════

@app.route('/api/user/<int:user_id>')
def get_user(user_id):
    with get_db() as conn:
        row = conn.execute(
            '''SELECT user_id,username,first_name,custom_name,balance,experience,
                      games_won,games_lost,total_won_amount,total_lost_amount
               FROM users WHERE user_id=?''', (user_id,)
        ).fetchone()
        if not row: return jsonify({'error':'not found'}), 404
        d = dict(row)
        lv = get_level_from_exp(d.get('experience',0))
        emoji, tname, tcolor = build_user_title(lv)
        d['level']       = lv
        d['title']       = f"{emoji} {tname}"
        d['title_emoji'] = emoji
        d['title_name']  = tname
        d['title_color'] = tcolor
        d['name']        = d.get('custom_name') or d.get('first_name') or d.get('username') or 'Аноним'
        lv_xp_start      = sum(1000+i*500 for i in range(lv-1))
        d['exp_current'] = max(0, d['experience'] - lv_xp_start)
        d['exp_needed']  = 1000 + (lv-1)*500
        d['total_won']   = d.get('total_won_amount',0)
        d['total_lost']  = d.get('total_lost_amount',0)
        return jsonify(d)

# ══════════════════════════════════════════════
#  CASINO
# ══════════════════════════════════════════════

def apply_game_result(conn, user_id, delta, xp):
    conn.execute('''UPDATE users SET
        balance=MAX(0,balance+?), experience=MIN(experience+?,999999),
        last_activity=CURRENT_TIMESTAMP WHERE user_id=?''', (delta, xp, user_id))
    row = conn.execute('SELECT balance,experience FROM users WHERE user_id=?',(user_id,)).fetchone()
    lv = get_level_from_exp(row['experience'])
    emoji, tname, tcolor = build_user_title(lv)
    return {'balance':row['balance'],'experience':row['experience'],'level':lv,
            'title':f"{emoji} {tname}",'title_emoji':emoji,'title_name':tname,'title_color':tcolor}

@app.route('/api/game', methods=['POST'])
def record_game():
    data=request.json; user_id=data.get('user_id'); bet=int(data.get('bet',0))
    won=bool(data.get('won',False)); win_amount=int(data.get('win_amount',0))
    if not user_id or bet<=0: return jsonify({'error':'bad data'}),400
    xp=bet//10+(10 if won else 5)
    with get_db() as conn:
        if not conn.execute('SELECT 1 FROM users WHERE user_id=?',(user_id,)).fetchone():
            return jsonify({'error':'not found'}),404
        delta=win_amount-bet if won else -bet
        conn.execute('''UPDATE users SET balance=MAX(0,balance+?),
            games_won=games_won+?,games_lost=games_lost+?,
            total_won_amount=total_won_amount+?,total_lost_amount=total_lost_amount+?,
            experience=MIN(experience+?,999999),last_activity=CURRENT_TIMESTAMP WHERE user_id=?''',
            (delta,1 if won else 0,0 if won else 1,
             win_amount if won else 0,bet if not won else 0,xp,user_id))
        res=apply_game_result(conn,user_id,0,0)
        return jsonify({'ok':True,**res})

@app.route('/api/slots/spin', methods=['POST'])
def slots_spin():
    data=request.json; user_id=data.get('user_id'); bet=int(data.get('bet',0))
    if not user_id or bet<=0: return jsonify({'error':'bad data'}),400
    symbols=['🍒','🍋','🍊','⭐','💎','🎰']; weights=[30,25,20,15,8,2]
    reels=random.choices(symbols,weights=weights,k=3)
    won=reels[0]==reels[1]==reels[2]
    mult_map={'🍒':2,'🍋':3,'🍊':4,'⭐':5,'💎':10,'🎰':20}
    win_amount=bet*mult_map.get(reels[0],2) if won else 0
    delta=win_amount-bet if won else -bet; xp=bet//10+(10 if won else 5)
    with get_db() as conn:
        if not conn.execute('SELECT 1 FROM users WHERE user_id=?',(user_id,)).fetchone():
            return jsonify({'error':'user not found'}),404
        conn.execute('UPDATE users SET games_won=games_won+?,games_lost=games_lost+? WHERE user_id=?',
                     (1 if won else 0,0 if won else 1,user_id))
        res=apply_game_result(conn,user_id,delta,xp)
    return jsonify({'ok':True,'reels':reels,'won':won,'win_amount':win_amount,**res})

@app.route('/api/roulette/spin', methods=['POST'])
def roulette_spin():
    data=request.json; user_id=data.get('user_id'); bet=int(data.get('bet',0))
    bet_type=data.get('bet_type','red'); number=data.get('number')
    if not user_id or bet<=0: return jsonify({'error':'bad data'}),400
    result_num=random.randint(0,36)
    reds={1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    color='green' if result_num==0 else('red' if result_num in reds else 'black')
    won,win_mult=False,0
    if   bet_type=='red'    and color=='red':   won,win_mult=True,2
    elif bet_type=='black'  and color=='black': won,win_mult=True,2
    elif bet_type=='green'  and color=='green': won,win_mult=True,14
    elif bet_type=='number' and number is not None and int(number)==result_num: won,win_mult=True,36
    win_amount=bet*win_mult if won else 0; delta=win_amount-bet if won else -bet; xp=bet//10+(10 if won else 5)
    with get_db() as conn:
        if not conn.execute('SELECT 1 FROM users WHERE user_id=?',(user_id,)).fetchone():
            return jsonify({'error':'user not found'}),404
        conn.execute('UPDATE users SET games_won=games_won+?,games_lost=games_lost+? WHERE user_id=?',
                     (1 if won else 0,0 if won else 1,user_id))
        res=apply_game_result(conn,user_id,delta,xp)
    return jsonify({'ok':True,'result_number':result_num,'result_color':color,'won':won,'win_amount':win_amount,**res})

bj_sessions={}

def card_value(card):
    rank=card[:-1]
    if rank in('J','Q','K'): return 10
    if rank=='A': return 11
    try: return int(rank)
    except: return 0

def hand_value(hand):
    val=sum(card_value(c) for c in hand); aces=sum(1 for c in hand if c[:-1]=='A')
    while val>21 and aces: val-=10; aces-=1
    return val

def make_deck():
    ranks=['2','3','4','5','6','7','8','9','10','J','Q','K','A']
    suits=['♠','♥','♦','♣']; deck=[r+s for r in ranks for s in suits]
    random.shuffle(deck); return deck

@app.route('/api/blackjack/deal', methods=['POST'])
def bj_deal():
    data=request.json; user_id=data.get('user_id'); bet=int(data.get('bet',0))
    if not user_id or bet<=0: return jsonify({'error':'bad data'}),400
    with get_db() as conn:
        row=conn.execute('SELECT balance FROM users WHERE user_id=?',(user_id,)).fetchone()
        if not row: return jsonify({'error':'user not found'}),404
        if row['balance']<bet: return jsonify({'error':'недостаточно средств'}),400
    deck=make_deck(); player=[deck.pop(),deck.pop()]; dealer=[deck.pop(),deck.pop()]
    bj_sessions[user_id]={'deck':deck,'player':player,'dealer':dealer,'bet':bet}
    pv=hand_value(player); dv=hand_value(dealer)
    if pv==21:
        win_amount=int(bet*2.5)
        with get_db() as conn:
            conn.execute('UPDATE users SET games_won=games_won+1 WHERE user_id=?',(user_id,))
            res=apply_game_result(conn,user_id,int(bet*1.5),15)
        del bj_sessions[user_id]
        return jsonify({'ok':True,'player':player,'dealer':dealer,'player_value':pv,
                        'dealer_value':dv,'status':'blackjack','win_amount':win_amount,**res})
    return jsonify({'ok':True,'player':player,'dealer':[dealer[0],'🂠'],
                    'player_value':pv,'dealer_value':card_value(dealer[0]),'status':'playing'})

@app.route('/api/blackjack/hit', methods=['POST'])
def bj_hit():
    data=request.json; user_id=data.get('user_id'); sess=bj_sessions.get(user_id)
    if not sess: return jsonify({'error':'no active game'}),400
    sess['player'].append(sess['deck'].pop()); pv=hand_value(sess['player'])
    if pv>21:
        with get_db() as conn:
            conn.execute('UPDATE users SET games_lost=games_lost+1 WHERE user_id=?',(user_id,))
            res=apply_game_result(conn,user_id,-sess['bet'],5)
        del bj_sessions[user_id]
        return jsonify({'ok':True,'player':sess['player'],'dealer':sess['dealer'],
                        'player_value':pv,'dealer_value':hand_value(sess['dealer']),'status':'bust','win_amount':0,**res})
    return jsonify({'ok':True,'player':sess['player'],'dealer':[sess['dealer'][0],'🂠'],
                    'player_value':pv,'status':'playing'})

@app.route('/api/blackjack/stand', methods=['POST'])
def bj_stand():
    data=request.json; user_id=data.get('user_id'); sess=bj_sessions.get(user_id)
    if not sess: return jsonify({'error':'no active game'}),400
    while hand_value(sess['dealer'])<17: sess['dealer'].append(sess['deck'].pop())
    pv=hand_value(sess['player']); dv=hand_value(sess['dealer']); bet=sess['bet']
    if dv>21 or pv>dv:  status,delta,xp,win_amount='win',  bet,  15,bet*2
    elif pv==dv:         status,delta,xp,win_amount='push', 0,   10,bet
    else:                status,delta,xp,win_amount='lose', -bet, 5, 0
    with get_db() as conn:
        if status=='win':  conn.execute('UPDATE users SET games_won=games_won+1 WHERE user_id=?',(user_id,))
        elif status=='lose': conn.execute('UPDATE users SET games_lost=games_lost+1 WHERE user_id=?',(user_id,))
        res=apply_game_result(conn,user_id,delta,xp)
    del bj_sessions[user_id]
    return jsonify({'ok':True,'player':sess['player'],'dealer':sess['dealer'],
                    'player_value':pv,'dealer_value':dv,'status':status,'win_amount':win_amount,**res})

@app.route('/api/coin/flip', methods=['POST'])
def coin_flip():
    data=request.json; user_id=data.get('user_id'); bet=int(data.get('bet',0)); choice=data.get('choice','heads')
    if not user_id or bet<=0: return jsonify({'error':'bad data'}),400
    result=random.choice(['heads','tails']); won=result==choice
    delta=bet if won else -bet; xp=bet//10+(10 if won else 5)
    with get_db() as conn:
        if not conn.execute('SELECT 1 FROM users WHERE user_id=?',(user_id,)).fetchone():
            return jsonify({'error':'user not found'}),404
        conn.execute('UPDATE users SET games_won=games_won+?,games_lost=games_lost+? WHERE user_id=?',
                     (1 if won else 0,0 if won else 1,user_id))
        res=apply_game_result(conn,user_id,delta,xp)
    return jsonify({'ok':True,'result':result,'won':won,'win_amount':bet*2 if won else 0,**res})

# ══════════════════════════════════════════════
#  LEADERBOARD
# ══════════════════════════════════════════════

def build_lb(rows):
    result=[]
    for i,r in enumerate(rows):
        d=dict(r); lv=get_level_from_exp(d.get('exp',0))
        emoji,tname,tcolor=build_user_title(lv)
        d['level']=lv; d['title']=f"{emoji} {tname}"
        d['title_emoji']=emoji; d['title_name']=tname; d['title_color']=tcolor
        d['rank']=i+1; result.append(d)
    return result

@app.route('/api/leaderboard')
def leaderboard():
    tab=request.args.get('type','balance')
    col={'balance':'balance','exp':'experience','wins':'games_won'}.get(tab,'balance')
    with get_db() as conn:
        rows=conn.execute(f'''SELECT user_id,COALESCE(custom_name,first_name,username,'Аноним') as name,
            balance,experience as exp,games_won as wins FROM users ORDER BY {col} DESC LIMIT 50''').fetchall()
        return jsonify(build_lb(rows))

@app.route('/api/leaderboard/exp')
def leaderboard_exp():
    with get_db() as conn:
        rows=conn.execute('''SELECT user_id,COALESCE(custom_name,first_name,username,'Аноним') as name,
            balance,experience as exp,games_won as wins FROM users ORDER BY experience DESC LIMIT 50''').fetchall()
        return jsonify(build_lb(rows))

@app.route('/api/my_rank/<int:user_id>')
def my_rank(user_id):
    with get_db() as conn:
        r=conn.execute('SELECT COUNT(*) as rank FROM users WHERE balance>(SELECT balance FROM users WHERE user_id=?)',
                       (user_id,)).fetchone()
        return jsonify({'rank':(r['rank'] or 0)+1})

# ══════════════════════════════════════════════
#  TAXI
# ══════════════════════════════════════════════

@app.route('/api/taxi/routes')
def taxi_routes():
    user_id=request.args.get('user_id',type=int); result=[]
    for r in TAXI_ROUTES:
        mult=1.3
        if user_id:
            try:
                with get_db() as conn:
                    stats=get_or_create_driver(conn,user_id)
                    mult=DRIVER_CLASSES.get(stats.get('driver_class','economy'),{}).get('mult',1.3)
            except: pass
        price=int(r['base_price']*mult*(1+random.uniform(-r['variation'],r['variation'])))
        result.append({'id':r['id'],'name':r['name'],'distance':r['distance'],
                       'time':r['time'],'price':price,'min_time':r['min_time']})
    return jsonify(result)

@app.route('/api/taxi/stats', defaults={'user_id':None})
@app.route('/api/taxi/stats/<int:user_id>')
def taxi_stats(user_id):
    if user_id is None: user_id=request.args.get('user_id',type=int)
    if not user_id: return jsonify({'error':'user_id required'}),400
    with get_db() as conn:
        stats=get_or_create_driver(conn,user_id); trips=stats['trips_completed']
        correct_class=get_driver_class(trips)
        if stats['driver_class']!=correct_class:
            conn.execute('UPDATE driver_stats SET driver_class=? WHERE user_id=?',(correct_class,user_id))
        stats['driver_class']=correct_class; stats['next_class']=get_driver_class(trips+1)
        stats['class_emoji']=DRIVER_CLASSES[correct_class]['emoji']
        stats['class_mult']=DRIVER_CLASSES[correct_class]['mult']
        return jsonify(stats)

@app.route('/api/taxi/status')
def taxi_status():
    user_id=request.args.get('user_id',type=int)
    if not user_id: return jsonify({'error':'user_id required'}),400
    with get_db() as conn:
        row=conn.execute('SELECT trip_data,start_time FROM active_trips WHERE user_id=? AND finish_time IS NULL',
                         (user_id,)).fetchone()
        if row:
            order=json.loads(row['trip_data']); elapsed=int(time.time())-row['start_time']
            required=order.get('timeMinutes',order.get('min_time',3))*60
            return jsonify({'active':True,'trip':order,'elapsed':elapsed,'required':required})
        return jsonify({'active':False})

@app.route('/api/taxi/start', methods=['POST'])
def taxi_start():
    data=request.json; user_id=data.get('user_id'); route_id=data.get('route_id'); order=data.get('order')
    if not user_id: return jsonify({'error':'bad data'}),400
    if route_id and not order:
        route=next((r for r in TAXI_ROUTES if r['id']==route_id),None)
        if not route: return jsonify({'error':'route not found'}),404
        with get_db() as conn:
            stats=get_or_create_driver(conn,user_id)
            mult=DRIVER_CLASSES.get(stats.get('driver_class','economy'),{}).get('mult',1.3)
        price=int(route['base_price']*mult*(1+random.uniform(-route['variation'],route['variation'])))
        duration_sec=route['min_time']*60
        order={'id':route['id'],'name':route['name'],'distance':route['distance'],
               'time':route['time'],'price':price,'min_time':route['min_time'],
               'timeMinutes':route['min_time'],'experience':250}
    elif order:
        duration_sec=order.get('timeMinutes',order.get('min_time',3))*60
    else:
        return jsonify({'error':'bad data'}),400
    with get_db() as conn:
        conn.execute('DELETE FROM active_trips WHERE user_id=?',(user_id,))
        conn.execute('INSERT INTO active_trips(user_id,trip_data,start_time) VALUES(?,?,?)',
                     (user_id,json.dumps(order),int(time.time())))
    return jsonify({'ok':True,'trip':order,'duration_seconds':duration_sec})

@app.route('/api/taxi/complete', methods=['POST'])
def taxi_complete():
    data=request.json; user_id=data.get('user_id'); order=data.get('order')
    if not user_id: return jsonify({'error':'bad data'}),400
    if not order:
        with get_db() as conn:
            row=conn.execute('SELECT trip_data FROM active_trips WHERE user_id=? AND finish_time IS NULL',
                             (user_id,)).fetchone()
            if row: order=json.loads(row['trip_data'])
            else: return jsonify({'error':'no active trip'}),404
    price=int(order.get('price',0)); experience=int(order.get('experience',250))
    with get_db() as conn:
        if not conn.execute('SELECT 1 FROM users WHERE user_id=?',(user_id,)).fetchone():
            return jsonify({'error':'user not found'}),404
        conn.execute('''UPDATE users SET balance=balance+?,
            experience=MIN(experience+?,999999),last_activity=CURRENT_TIMESTAMP
            WHERE user_id=?''',(price,experience,user_id))
        stats=get_or_create_driver(conn,user_id)
        new_trips=stats['trips_completed']+1; new_earned=stats['total_earned']+price
        new_class=get_driver_class(new_trips)
        conn.execute('''UPDATE driver_stats SET trips_completed=trips_completed+1,
            total_earned=total_earned+?,driver_class=?,last_trip_time=? WHERE user_id=?''',
            (price,new_class,int(time.time()),user_id))
        conn.execute('UPDATE active_trips SET finish_time=? WHERE user_id=? AND finish_time IS NULL',
                     (int(time.time()),user_id))
        upd=conn.execute('SELECT balance,experience FROM users WHERE user_id=?',(user_id,)).fetchone()
        new_lv=get_level_from_exp(upd['experience'])
        return jsonify({'ok':True,'balance':upd['balance'],'new_balance':upd['balance'],
                        'earned':price,'exp_gain':experience,'experience':upd['experience'],
                        'level':new_lv,'title':get_title(new_lv),
                        'trips_completed':new_trips,'total_earned':new_earned,
                        'driver_class':new_class,'class_emoji':DRIVER_CLASSES[new_class]['emoji'],
                        'class_up':new_class!=stats['driver_class']})

@app.route('/api/taxi/leaderboard')
def taxi_leaderboard():
    with get_db() as conn:
        rows=conn.execute('''SELECT ds.user_id,ds.trips_completed,ds.total_earned,ds.driver_class,
            COALESCE(u.custom_name,u.first_name,u.username,'Аноним') as name
            FROM driver_stats ds LEFT JOIN users u ON ds.user_id=u.user_id
            WHERE ds.trips_completed>0 ORDER BY ds.trips_completed DESC,ds.total_earned DESC LIMIT 10''').fetchall()
        result=[]
        for r in rows:
            d=dict(r); cls=d.get('driver_class','economy')
            d['class_emoji']=DRIVER_CLASSES.get(cls,{}).get('emoji','🚕'); result.append(d)
        return jsonify(result)

@app.route('/api/taxi/active/<int:user_id>')
def taxi_active(user_id):
    with get_db() as conn:
        row=conn.execute('SELECT trip_data,start_time FROM active_trips WHERE user_id=? AND finish_time IS NULL',
                         (user_id,)).fetchone()
        if row:
            order=json.loads(row['trip_data']); elapsed=int(time.time())-row['start_time']
            remaining=max(0,order['timeMinutes']*60-elapsed)
            return jsonify({'active':True,'order':order,'remaining_seconds':remaining})
        return jsonify({'active':False})

# ──────────────────── Serve HTML ─────────────
@app.route('/')
@app.route('/mini')
def mini():
    try:
        return open('index.html','r',encoding='utf-8').read(),200,{'Content-Type':'text/html'}
    except FileNotFoundError:
        return 'Place index.html next to app.py',404

# ══════════════════════════════════════════════
#  ЗАПУСК: БОТ + АВТОПИНГ
# ══════════════════════════════════════════════

_bot_started = False

def run_bot():
    try:
        import importlib.util
        bot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot.py')
        if not os.path.exists(bot_path):
            print("⚠️  bot.py не найден"); return
        spec = importlib.util.spec_from_file_location("bot", bot_path)
        bot_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bot_module)
        print("🤖 Бот запускается...")
        # Сброс вебхука и старых соединений — устраняет ошибку 409
        try:
            bot_module.bot.delete_webhook(drop_pending_updates=True)
            time.sleep(3)
        except: pass
        bot_module.cleanup_expired_challenges()
        bot_module.bot.infinity_polling(timeout=30, long_polling_timeout=30)
    except Exception as e:
        print(f"❌ Ошибка бота: {e}")

def auto_ping():
    """Пингуем себя каждые 4 минуты — Render не засыпает"""
    import urllib.request
    time.sleep(40)  # ждём пока Flask запустится
    url = os.environ.get('RENDER_EXTERNAL_URL', 'http://localhost:3001')
    while True:
        try:
            urllib.request.urlopen(f"{url}/ping", timeout=10)
            print(f"🏓 Ping OK → {url}/ping")
        except Exception as e:
            print(f"🏓 Ping error: {e}")
        time.sleep(240)  # каждые 4 минуты

if __name__ == '__main__':
    global _bot_started
    if not _bot_started:
        _bot_started = True
        threading.Thread(target=run_bot,  daemon=True, name="BotThread").start()
        threading.Thread(target=auto_ping, daemon=True, name="PingThread").start()
    print("✅ Запуск Flask...")
    app.run(host='0.0.0.0', port=3001, debug=False)
