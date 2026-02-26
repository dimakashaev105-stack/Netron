const http = require('http');
const path = require('path');
const fs = require('fs');
const url = require('url');
const sqlite3 = require('sqlite3').verbose();
const { execFile } = require('child_process');

const PORT = process.env.PORT || 3000;
const DB_PATH = path.join(__dirname, 'game.db');
const PUBLIC = path.join(__dirname, 'public');

// ── MIME types ──
const MIME = {
  '.html': 'text/html',
  '.js':   'text/javascript',
  '.css':  'text/css',
  '.json': 'application/json',
  '.png':  'image/png',
  '.jpg':  'image/jpeg',
  '.svg':  'image/svg+xml',
  '.ico':  'image/x-icon',
};

// ── DB helper ──
function dbAll(sql, params = []) {
  return new Promise((res, rej) => {
    const db = new sqlite3.Database(DB_PATH);
    db.all(sql, params, (err, rows) => { db.close(); err ? rej(err) : res(rows); });
  });
}
function dbGet(sql, params = []) {
  return new Promise((res, rej) => {
    const db = new sqlite3.Database(DB_PATH);
    db.get(sql, params, (err, row) => { db.close(); err ? rej(err) : res(row); });
  });
}
function dbRun(sql, params = []) {
  return new Promise((res, rej) => {
    const db = new sqlite3.Database(DB_PATH);
    db.run(sql, params, function(err) { db.close(); err ? rej(err) : res(this); });
  });
}

// ── Level helpers (same logic as bot.py) ──
function getLevelFromExp(exp) {
  let level = 1, total = 0;
  while (level < 50) {
    const needed = 1000 + (level - 1) * 500;
    if (total + needed > exp) break;
    total += needed;
    level++;
  }
  return level;
}
function getExpForLevel(level) {
  let total = 0;
  for (let l = 1; l < level; l++) total += 1000 + (l - 1) * 500;
  return total;
}

const TITLE_MAP = {
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
};
const TITLE_COLORS = {
  '🥉':'#cd7f32','🥈':'#c0c0c0','🥇':'#ffd700',
  '💎':'#b9f2ff','🔮':'#bf00ff','👑':'#ff4500'
};

function getTitle(level) {
  const t = TITLE_MAP[level] || '👑 Legend X';
  const emoji = t.split(' ')[0];
  return { full: t, emoji, name: t.slice(emoji.length + 1), color: TITLE_COLORS[emoji] || '#fff' };
}

function buildUserInfo(row) {
  const exp = row.experience || 0;
  const level = getLevelFromExp(exp);
  const expStart = getExpForLevel(level);
  const expNeeded = level < 50 ? 1000 + (level - 1) * 500 : 0;
  const title = getTitle(level);
  return {
    user_id: row.user_id,
    name: row.custom_name || row.first_name || row.username || 'Игрок',
    balance: row.balance || 0,
    experience: exp,
    level,
    title_emoji: title.emoji,
    title_name: title.name,
    title_color: title.color,
    exp_current: exp - expStart,
    exp_needed: expNeeded,
    games_won: row.games_won || 0,
    games_lost: row.games_lost || 0,
    total_won: row.total_won_amount || 0,
    total_lost: row.total_lost_amount || 0,
  };
}

// ── Taxi config ──
const DC = {
  economy: { name:'Стандарт', emoji:'🚕', mult:1.3 },
  comfort:  { name:'Комфорт+', emoji:'🚙', mult:1.55 },
  business: { name:'Бизнес',   emoji:'🏎️', mult:1.8 },
  vip:      { name:'Премиум',  emoji:'👑', mult:2.3 },
};
const ROUTES = [
  {id:1,name:'Центр → Аэропорт',       distance:'25 км',time_min:5,base_price:1500},
  {id:2,name:'Жилой р-н → Офис',       distance:'15 км',time_min:4,base_price:1000},
  {id:3,name:'Университет → ТЦ',        distance:'12 км',time_min:3,base_price:800},
  {id:4,name:'Больница → Вокзал',       distance:'18 км',time_min:4,base_price:1200},
  {id:5,name:'Бизнес-центр → Ресторан', distance:'10 км',time_min:3,base_price:600},
  {id:6,name:'ТЦ → Кинотеатр',          distance:'8 км', time_min:3,base_price:500},
  {id:7,name:'🌃 Ночной рейс',           distance:'30 км',time_min:6,base_price:2000},
  {id:8,name:'Вокзал → Гостиница',      distance:'7 км', time_min:3,base_price:400},
];

function getDriverClass(trips) {
  if (trips >= 300) return 'vip';
  if (trips >= 150) return 'business';
  if (trips >= 100) return 'comfort';
  return 'economy';
}

// ── Slots ──
const SLOT_SYMS = [
  {sym:'🍋',weight:30,mult:1.5},
  {sym:'🍊',weight:25,mult:2.0},
  {sym:'🍇',weight:20,mult:2.5},
  {sym:'🍒',weight:12,mult:4.0},
  {sym:'⭐',weight:7, mult:8.0},
  {sym:'🔔',weight:4, mult:15.0},
  {sym:'💎',weight:1.5,mult:35.0},
  {sym:'7️⃣',weight:0.5,mult:100.0},
];
function spinReel() {
  const total = SLOT_SYMS.reduce((s,x) => s + x.weight, 0);
  let r = Math.random() * total;
  for (const s of SLOT_SYMS) { r -= s.weight; if (r <= 0) return s; }
  return SLOT_SYMS[0];
}

// ── JSON response helper ──
function json(res, code, data) {
  const body = JSON.stringify(data);
  res.writeHead(code, { 'Content-Type':'application/json', 'Access-Control-Allow-Origin':'*' });
  res.end(body);
}

// ── Parse body ──
function body(req) {
  return new Promise(res => {
    let d = '';
    req.on('data', c => d += c);
    req.on('end', () => { try { res(JSON.parse(d)); } catch { res({}); } });
  });
}

// ── Router ──
const server = http.createServer(async (req, res) => {
  const parsed = url.parse(req.url, true);
  const pathname = parsed.pathname;
  const q = parsed.query;

  // CORS preflight
  if (req.method === 'OPTIONS') {
    res.writeHead(204, { 'Access-Control-Allow-Origin':'*', 'Access-Control-Allow-Methods':'GET,POST', 'Access-Control-Allow-Headers':'Content-Type' });
    return res.end();
  }

  // ── API routes ──
  try {

    // GET /api/user/:id
    if (req.method === 'GET' && /^\/api\/user\/(\d+)$/.test(pathname)) {
      const uid = parseInt(pathname.split('/')[3]);
      const row = await dbGet(`SELECT user_id,username,first_name,custom_name,balance,experience,
        games_won,games_lost,total_won_amount,total_lost_amount FROM users WHERE user_id=?`, [uid]);
      if (!row) return json(res, 404, { error:'User not found' });
      return json(res, 200, buildUserInfo(row));
    }

    // GET /api/leaderboard
    if (req.method === 'GET' && pathname === '/api/leaderboard') {
      const rows = await dbAll(`SELECT user_id,username,first_name,custom_name,balance,experience
        FROM users ORDER BY balance DESC LIMIT 50`);
      return json(res, 200, rows.map((r, i) => ({ rank:i+1, ...buildUserInfo(r) })));
    }

    // GET /api/leaderboard/exp
    if (req.method === 'GET' && pathname === '/api/leaderboard/exp') {
      const rows = await dbAll(`SELECT user_id,username,first_name,custom_name,balance,experience
        FROM users ORDER BY experience DESC LIMIT 50`);
      return json(res, 200, rows.map((r, i) => ({ rank:i+1, ...buildUserInfo(r) })));
    }

    // GET /api/taxi/routes
    if (req.method === 'GET' && pathname === '/api/taxi/routes') {
      const uid = parseInt(q.user_id) || 0;
      let mult = 1.3;
      if (uid) {
        const drv = await dbGet(`SELECT trips_completed FROM driver_stats WHERE user_id=?`, [uid]);
        const trips = drv ? drv.trips_completed : 0;
        mult = DC[getDriverClass(trips)].mult;
      }
      return json(res, 200, ROUTES.map(r => ({ ...r, price: Math.round(r.base_price * mult) })));
    }

    // GET /api/taxi/status
    if (req.method === 'GET' && pathname === '/api/taxi/status') {
      const uid = parseInt(q.user_id);
      const row = await dbGet(`SELECT trip_data,start_time FROM active_trips WHERE user_id=? AND finish_time IS NULL`, [uid]);
      if (!row) return json(res, 200, { active:false });
      const trip = JSON.parse(row.trip_data);
      const elapsed = Math.floor(Date.now()/1000) - row.start_time;
      const required = trip.time_min * 60;
      return json(res, 200, { active:true, trip, elapsed, required, done: elapsed >= required });
    }

    // GET /api/taxi/stats
    if (req.method === 'GET' && pathname === '/api/taxi/stats') {
      const uid = parseInt(q.user_id);
      const row = await dbGet(`SELECT trips_completed,total_earned FROM driver_stats WHERE user_id=?`, [uid]);
      const trips = row ? row.trips_completed : 0;
      const dc = getDriverClass(trips);
      const nextMap = { economy:120, comfort:170, business:333, vip:null };
      return json(res, 200, {
        trips_completed: trips,
        total_earned: row ? row.total_earned : 0,
        driver_class: dc,
        driver_class_info: DC[dc],
        next_threshold: nextMap[dc],
      });
    }

    // POST /api/taxi/start
    if (req.method === 'POST' && pathname === '/api/taxi/start') {
      const { user_id, route_id } = await body(req);
      const uid = parseInt(user_id);
      const route = ROUTES.find(r => r.id === parseInt(route_id));
      if (!route) return json(res, 404, { error:'Route not found' });

      const drv = await dbGet(`SELECT trips_completed FROM driver_stats WHERE user_id=?`, [uid]);
      const trips = drv ? drv.trips_completed : 0;
      if (!drv) await dbRun(`INSERT OR IGNORE INTO driver_stats (user_id) VALUES (?)`, [uid]);
      const mult = DC[getDriverClass(trips)].mult;
      const price = Math.round(route.base_price * mult * (0.85 + Math.random() * 0.3));
      const tripData = {
        route_id: route.id, route_name: route.name, price,
        distance: route.distance, time_min: route.time_min,
        driver_class: getDriverClass(trips),
        start_time: Math.floor(Date.now()/1000),
      };
      await dbRun(`DELETE FROM active_trips WHERE user_id=?`, [uid]);
      await dbRun(`INSERT INTO active_trips (user_id,trip_data,start_time) VALUES (?,?,?)`,
        [uid, JSON.stringify(tripData), Math.floor(Date.now()/1000)]);
      return json(res, 200, { trip: tripData, duration_seconds: route.time_min * 60 });
    }

    // POST /api/taxi/complete
    if (req.method === 'POST' && pathname === '/api/taxi/complete') {
      const { user_id } = await body(req);
      const uid = parseInt(user_id);
      const row = await dbGet(`SELECT trip_data,start_time FROM active_trips WHERE user_id=? AND finish_time IS NULL`, [uid]);
      if (!row) return json(res, 400, { error:'Нет активного рейса' });
      const trip = JSON.parse(row.trip_data);
      const elapsed = Math.floor(Date.now()/1000) - row.start_time;
      if (elapsed < trip.time_min * 60 - 2) return json(res, 400, { error:`Рейс ещё не завершён` });

      await dbRun(`UPDATE users SET balance=balance+?,experience=experience+250 WHERE user_id=?`, [trip.price, uid]);
      await dbRun(`UPDATE driver_stats SET trips_completed=trips_completed+1,total_earned=total_earned+? WHERE user_id=?`, [trip.price, uid]);
      await dbRun(`UPDATE active_trips SET finish_time=? WHERE user_id=? AND finish_time IS NULL`, [Math.floor(Date.now()/1000), uid]);

      const updated = await dbGet(`SELECT balance FROM users WHERE user_id=?`, [uid]);
      const stats = await dbGet(`SELECT trips_completed FROM driver_stats WHERE user_id=?`, [uid]);
      const dc = getDriverClass(stats.trips_completed);
      return json(res, 200, { success:true, earned:trip.price, exp_gain:250, new_balance:updated.balance, trips_completed:stats.trips_completed, driver_class:dc, driver_class_info:DC[dc] });
    }

    // POST /api/slots/spin
    if (req.method === 'POST' && pathname === '/api/slots/spin') {
      const { user_id, bet } = await body(req);
      const uid = parseInt(user_id);
      const betAmt = parseInt(bet) || 100;
      const row = await dbGet(`SELECT balance FROM users WHERE user_id=?`, [uid]);
      if (!row) return json(res, 404, { error:'User not found' });
      if (row.balance < betAmt) return json(res, 400, { error:'Недостаточно средств' });

      const reels = [spinReel(), spinReel(), spinReel()];
      const syms = reels.map(r => r.sym);
      let win = 0, winType = null;

      if (syms[0] === syms[1] && syms[1] === syms[2]) {
        win = Math.round(betAmt * reels[0].mult);
        winType = 'jackpot';
      } else if (syms[0]===syms[1] || syms[1]===syms[2] || syms[0]===syms[2]) {
        const m = syms[0]===syms[1] ? reels[0].mult : syms[1]===syms[2] ? reels[1].mult : reels[0].mult;
        win = Math.round(betAmt * m * 0.3);
        winType = 'pair';
      }

      const net = win - betAmt;
      await dbRun(`UPDATE users SET balance=balance+?,experience=experience+? WHERE user_id=?`,
        [net, Math.max(10, Math.floor(betAmt/100)), uid]);
      if (win > 0) await dbRun(`UPDATE users SET games_won=games_won+1,total_won_amount=total_won_amount+? WHERE user_id=?`, [win, uid]);
      else await dbRun(`UPDATE users SET games_lost=games_lost+1,total_lost_amount=total_lost_amount+? WHERE user_id=?`, [betAmt, uid]);

      const updated = await dbGet(`SELECT balance FROM users WHERE user_id=?`, [uid]);
      return json(res, 200, { reels:syms, win, bet:betAmt, net, win_type:winType, new_balance:updated.balance });
    }

    // ── Static files ──
    let filePath = pathname === '/' ? '/index.html' : pathname;
    filePath = path.join(PUBLIC, filePath);

    if (fs.existsSync(filePath) && fs.statSync(filePath).isFile()) {
      const ext = path.extname(filePath);
      res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
      return fs.createReadStream(filePath).pipe(res);
    }

    // SPA fallback
    res.writeHead(200, { 'Content-Type':'text/html' });
    fs.createReadStream(path.join(PUBLIC, 'index.html')).pipe(res);

  } catch (e) {
    console.error(e);
    json(res, 500, { error: e.message });
  }
});

server.listen(PORT, () => console.log(`✅ Server running on port ${PORT}`));

// ── Start bot in background ──
const botPath = path.join(__dirname, 'bot.py');
if (fs.existsSync(botPath)) {
  const bot = require('child_process').spawn('python3', [botPath], {
    stdio: 'inherit',
    detached: false,
  });
  bot.on('error', e => console.error('[BOT]', e.message));
  bot.on('exit', code => console.log(`[BOT] exited with code ${code}`));
  console.log('✅ bot.py запущен');
}
