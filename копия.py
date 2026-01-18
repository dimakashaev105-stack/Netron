import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import sqlite3
import time
import random
import os
import re
from datetime import datetime, timedelta
import threading

# Замените на свой токен
BOT_TOKEN = "7885520897:AAGTde5ZNNXkZrCXcCMT35GvOeFnHsSGZjE"

# ID администраторов (замените на свои)
ADMIN_IDS = [8139807344, 5255608302]  # Пример ID администраторов

bot = telebot.TeleBot(BOT_TOKEN)

# Словарь для защиты от спама
user_last_action = {}
user_captcha_status = {}

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect('game.db')
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        balance INTEGER DEFAULT 0,
        last_click TIMESTAMP DEFAULT 0,
        click_power INTEGER DEFAULT 1,
        referral_code TEXT UNIQUE,
        referred_by INTEGER,
        video_cards INTEGER DEFAULT 0,
        deposit INTEGER DEFAULT 0,
        last_mining_collect TIMESTAMP DEFAULT 0,
        last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        click_streak INTEGER DEFAULT 0,
        bank_deposit INTEGER DEFAULT 0,
        captcha_passed INTEGER DEFAULT 0,
        registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_banned INTEGER DEFAULT 0,
        ban_reason TEXT,
        banned_at TIMESTAMP,
        last_interest_calc TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Таблица чеков
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS checks (
        code TEXT PRIMARY KEY,
        amount INTEGER,
        max_activations INTEGER,
        current_activations INTEGER DEFAULT 0,
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Таблица активаций чеков
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS check_activations (
        user_id INTEGER,
        check_code TEXT,
        activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, check_code)
    )
    ''')
    
    # Проверяем существование колонок (исправленная версия для Android)
    cursor.execute("PRAGMA table_info(users)")
    columns = [column[1] for column in cursor.fetchall()]
    
    # Список колонок и их типов
    column_definitions = {
        'video_cards': 'INTEGER DEFAULT 0',
        'deposit': 'INTEGER DEFAULT 0', 
        'last_mining_collect': 'INTEGER DEFAULT 0',
        'click_streak': 'INTEGER DEFAULT 0',
        'bank_deposit': 'INTEGER DEFAULT 0',
        'captcha_passed': 'INTEGER DEFAULT 0',
        'registered_at': 'TIMESTAMP',
        'is_banned': 'INTEGER DEFAULT 0',
        'ban_reason': 'TEXT',
        'banned_at': 'TIMESTAMP',
        'last_interest_calc': 'TIMESTAMP'
    }
    
    for column, definition in column_definitions.items():
        if column not in columns:
            try:
                # Для Android SQLite нужно добавлять колонки без DEFAULT в ALTER TABLE
                if 'DEFAULT' in definition:
                    # Сначала добавляем колонку без DEFAULT
                    cursor.execute(f'ALTER TABLE users ADD COLUMN {column} {definition.split("DEFAULT")[0].strip()}')
                    # Затем обновляем значения
                    if 'INTEGER' in definition:
                        cursor.execute(f'UPDATE users SET {column} = 0 WHERE {column} IS NULL')
                    elif 'TIMESTAMP' in definition:
                        cursor.execute(f'UPDATE users SET {column} = CURRENT_TIMESTAMP WHERE {column} IS NULL')
                else:
                    cursor.execute(f'ALTER TABLE users ADD COLUMN {column} {definition}')
            except Exception as e:
                print(f"Ошибка при добавлении колонки {column}: {e}")
                # Пропускаем ошибку и продолжаем
    
    # Обновляем мощность клика для всех пользователей
    try:
        cursor.execute('UPDATE users SET click_power = 1 WHERE click_power < 1')
    except:
        pass  # Если колонки еще нет, пропускаем
    
    conn.commit()
    conn.close()
    print("База данных проверена и обновлена")

# Функция для проверки прав администратора
def is_admin(user_id):
    return user_id in ADMIN_IDS

# Функция для проверки бана
def is_banned(user_id):
    conn = sqlite3.connect('game.db')
    cursor = conn.cursor()
    cursor.execute('SELECT is_banned, ban_reason FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result and result[0] == 1:
        return True, result[1] if result[1] else "Причина не указана"
    return False, None

# Функция для проверки спама
def is_spam(user_id):
    current_time = time.time()
    if user_id in user_last_action:
        time_passed = current_time - user_last_action[user_id]
        if time_passed < 1:  # 1 секунда между действиями
            return True
    user_last_action[user_id] = current_time
    return False

# Функция для проверки капчи
def is_captcha_passed(user_id):
    conn = sqlite3.connect('game.db')
    cursor = conn.cursor()
    cursor.execute('SELECT captcha_passed FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] == 1 if result else False

# Функция для генерации математической капчи
def generate_captcha():
    # Генерируем два случайных числа
    num1 = random.randint(1, 10)
    num2 = random.randint(1, 10)
    
    # Выбираем случайную операцию
    operation = random.choice(['+', '-', '*'])
    
    # Вычисляем правильный ответ
    if operation == '+':
        answer = num1 + num2
    elif operation == '-':
        answer = num1 - num2
    else:  # '*'
        answer = num1 * num2
    
    # Формируем вопрос
    captcha_question = f"{num1} {operation} {num2} = ?"
    
    return captcha_question, str(answer)

# Функция для парсинга суммы ставки
def parse_bet_amount(bet_text, user_balance):
    if bet_text.lower() in ['все', 'all']:
        return user_balance
    
    bet_text = bet_text.lower().replace(' ', '')
    
    pattern = r'^(\d*\.?\d+)([кk]|[кk]{2,}|[mb]?)$'
    match = re.match(pattern, bet_text)
    
    if match:
        number_part = match.group(1)
        multiplier_part = match.group(2)
        
        try:
            number = float(number_part)
            
            if multiplier_part.startswith('к') or multiplier_part.startswith('k'):
                k_count = multiplier_part.count('к') + multiplier_part.count('k')
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

# Функция для форматирования суммы с пробелами
def format_balance(balance):
    return f"{balance:,}".replace(",", " ")

# Функция для получения или создания пользователя
def get_or_create_user(user_id, username, first_name):
    conn = sqlite3.connect('game.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    
    if not user:
        referral_code = f"ref{user_id}"
        
        cursor.execute(
            'INSERT INTO users (user_id, username, first_name, balance, referral_code, video_cards, deposit, last_mining_collect, click_streak, bank_deposit, captcha_passed, is_banned, last_interest_calc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (user_id, username, first_name, 0, referral_code, 0, 0, 0, 0, 0, 0, 0, datetime.now().timestamp())
        )
        conn.commit()
    
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    conn.close()
    return user

# Функция для обновления баланса с проверкой бана
def update_balance(user_id, amount):
    banned, reason = is_banned(user_id)
    if banned:
        return False
    
    conn = sqlite3.connect('game.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET balance = balance + ?, last_activity = CURRENT_TIMESTAMP WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()
    return True

# Функция для получения баланса с начислением процентов
def get_balance(user_id):
    # Сначала начисляем проценты
    calculate_interest(user_id)
    
    conn = sqlite3.connect('game.db')
    cursor = conn.cursor()
    cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

# Функция для начисления процентов по вкладу
def calculate_interest(user_id):
    conn = sqlite3.connect('game.db')
    cursor = conn.cursor()
    
    # Получаем данные о вкладе и последнем начислении
    cursor.execute('SELECT bank_deposit, last_interest_calc FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    
    if result and result[0] > 0:  # Если есть вклад
        bank_deposit, last_calc = result
        
        # Преобразуем last_calc в timestamp
        if isinstance(last_calc, str):
            try:
                last_calc_time = datetime.strptime(last_calc, '%Y-%m-%d %H:%M:%S').timestamp()
            except:
                last_calc_time = time.time() - 3600  # Если ошибка, считаем что прошёл час
        elif isinstance(last_calc, float) or isinstance(last_calc, int):
            last_calc_time = last_calc
        else:
            last_calc_time = time.time() - 3600
        
        current_time = time.time()
        hours_passed = (current_time - last_calc_time) / 3600
        
        if hours_passed >= 1:  # Прошёл минимум 1 час
            # Рассчитываем проценты (0.5% в час)
            interest_hours = int(hours_passed)  # Целые часы
            interest = int(bank_deposit * 0.005 * interest_hours)  # 0.5% за каждый час
            
            if interest > 0:
                # Начисляем проценты
                cursor.execute('UPDATE users SET balance = balance + ?, last_interest_calc = ? WHERE user_id = ?',
                             (interest, current_time, user_id))
                conn.commit()
    
    conn.close()

# Функция для получения банковского вклада
def get_bank_deposit(user_id):
    conn = sqlite3.connect('game.db')
    cursor = conn.cursor()
    cursor.execute('SELECT bank_deposit FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

# Функция для обновления банковского вклада
def update_bank_deposit(user_id, amount):
    banned, reason = is_banned(user_id)
    if banned:
        return False
    
    conn = sqlite3.connect('game.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET bank_deposit = bank_deposit + ?, last_interest_calc = ? WHERE user_id = ?',
                  (amount, datetime.now().timestamp(), user_id))
    conn.commit()
    conn.close()
    return True

# Функция для получения серии кликов
def get_click_streak(user_id):
    conn = sqlite3.connect('game.db')
    cursor = conn.cursor()
    cursor.execute('SELECT click_streak FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

# Функция для обновления серии кликов
def update_click_streak(user_id, amount):
    banned, reason = is_banned(user_id)
    if banned:
        return False
    
    conn = sqlite3.connect('game.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET click_streak = click_streak + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()
    return True

# Функция для получения топа мажоров
def get_top_majors():
    conn = sqlite3.connect('game.db')
    cursor = conn.cursor()
    
    cursor.execute('''
    SELECT 
        CASE 
            WHEN username IS NOT NULL AND username != '' THEN '@' || username 
            ELSE first_name 
        END as display_name,
        balance 
    FROM users 
    WHERE balance > 0 AND is_banned = 0
    ORDER BY balance DESC 
    LIMIT 10
    ''')
    
    top_users = cursor.fetchall()
    conn.close()
    
    return top_users

# Функция для создания сообщения с топом мажоров
def create_top_message():
    top_users = get_top_majors()
    
    if not top_users:
        return "❄️ ТОП МАЖОРОВ ❄️\n\nТоп пока пуст! Станьте первым мажором!\n\nТоп обновляется в реальном времени!"
    
    message = "❄️ ТОП МАЖОРОВ ❄️\n\n"
    
    medals = ["👑", "🥈", "🥉", "👤", "👤", "👤", "👤", "👤", "👤", "👤"]
    
    for i, (display_name, balance) in enumerate(top_users):
        medal = medals[i] if i < len(medals) else "👤"
        message += f"{medal} {i+1}. {display_name}: ❄️{format_balance(balance)}\n"
    
    message += "\nТоп обновляется в реальном времени!"
    return message

# Функция для расчета дохода майнинга
def calculate_mining_income(video_cards):
    base_income = 25000000
    return base_income * (2 ** (video_cards - 1)) if video_cards > 0 else 0

# Функция для расчета цены видеокарты
def calculate_video_card_price(video_cards):
    base_price = 500000000
    return base_price * (2 ** video_cards)

# Функция для создания клавиатуры майнинга
def create_mining_keyboard():
    markup = InlineKeyboardMarkup()
    markup.row_width = 2
    markup.add(
        InlineKeyboardButton("❄️ Собрать", callback_data="mining_collect"),
        InlineKeyboardButton("🖥 Купить видеокарту", callback_data="mining_buy")
    )
    return markup

# Функция для создания клавиатуры кликера
def create_clicker_keyboard():
    symbols = ["❌", "❌", "❌", "❌", "✅"]
    random.shuffle(symbols)
    
    markup = InlineKeyboardMarkup()
    row = []
    for i, symbol in enumerate(symbols):
        row.append(InlineKeyboardButton(symbol, callback_data=f"clicker_{symbol}"))
        if len(row) == 3:
            markup.row(*row)
            row = []
    if row:
        markup.row(*row)
    return markup

# Клавиатура для работы
def create_work_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    clicker_button = KeyboardButton("🖱️ Кликер")
    scam_button = KeyboardButton("👥 Скам")
    back_button = KeyboardButton("◀️ Назад")
    markup.add(clicker_button, scam_button, back_button)
    return markup

# Главное меню
def create_main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    balance_button = KeyboardButton("Баланс")
    mining_button = KeyboardButton("Майнинг")
    bank_button = KeyboardButton("Банк")
    casino_button = KeyboardButton("Казино")
    work_button = KeyboardButton("Работа")
    majors_button = KeyboardButton("Мажоры")
    top_scam_button = KeyboardButton("Топ скам")
    clan_panel_button = KeyboardButton("Панель клана")
    markup.add(balance_button, mining_button, bank_button, casino_button,
               work_button, majors_button, top_scam_button, clan_panel_button)
    return markup





# Обработчик команды /start с реферальной системой
@bot.message_handler(commands=['start'])
def start(message):
    try:
        if is_spam(message.from_user.id):
            return
            
        user_id = message.from_user.id
        username = message.from_user.username
        first_name = message.from_user.first_name
        
        get_or_create_user(user_id, username, first_name)
        
        # Проверяем бан
        banned, reason = is_banned(user_id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}\nОбратитесь к администратору.")
            return
        
        # Проверяем, прошел ли пользователь капчу
        if not is_captcha_passed(user_id):
            # Генерируем математическую капчу
            captcha_question, correct_answer = generate_captcha()
            user_captcha_status[user_id] = correct_answer
            
            bot.send_message(message.chat.id, 
                           f"🔒 Для продолжения решите пример:\n\n"
                           f"{captcha_question}\n\n"
                           f"Отправьте ответ числом в чат.",
                           parse_mode='Markdown')
            return
        
        referred_by = None
        if len(message.text.split()) > 1:
            ref_code = message.text.split()[1]
            conn = sqlite3.connect('game.db')
            cursor = conn.cursor()
            
            # Проверяем чек (эта часть для активации чеков)
            cursor.execute('SELECT amount, max_activations, current_activations FROM checks WHERE code = ?', (ref_code,))
            check_data = cursor.fetchone()
            
            if check_data:
                amount, max_activations, current_activations = check_data
                
                # Проверяем, активировал ли пользователь уже этот чек
                cursor.execute('SELECT * FROM check_activations WHERE user_id = ? AND check_code = ?', (user_id, ref_code))
                already_activated = cursor.fetchone()
                
                if already_activated:
                    bot.send_message(message.chat.id, "❌ Вы уже активировали этот чек!")
                elif current_activations < max_activations:
                    # Добавляем запись об активации
                    cursor.execute('INSERT INTO check_activations (user_id, check_code) VALUES (?, ?)', (user_id, ref_code))
                    
                    # Обновляем счетчик активаций
                    cursor.execute('UPDATE checks SET current_activations = current_activations + 1 WHERE code = ?', (ref_code,))
                    
                    # Начисляем деньги пользователю
                    cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
                    
                    conn.commit()
                    bot.send_message(message.chat.id, f"🎉 Вы активировали чек на ❄️{format_balance(amount)}!")
                else:
                    bot.send_message(message.chat.id, "❌ Чек уже использован максимальное количество раз!")
            else:
                # Проверяем реферальную ссылку
                cursor.execute('SELECT user_id FROM users WHERE referral_code = ?', (ref_code,))
                ref_user = cursor.fetchone()
                
                if ref_user and ref_user[0] != user_id:
                    referred_by = ref_user[0]
                    cursor.execute('SELECT referred_by FROM users WHERE user_id = ?', (user_id,))
                    current_ref = cursor.fetchone()
                    
                    if not current_ref or not current_ref[0]:
                        cursor.execute('UPDATE users SET balance = balance + 25 WHERE user_id = ?', (referred_by,))
                        cursor.execute('UPDATE users SET referred_by = ? WHERE user_id = ?', (referred_by, user_id))
                        cursor.execute('UPDATE users SET balance = balance + 25 WHERE user_id = ?', (user_id,))
                        conn.commit()
                        bot.send_message(message.chat.id, "🎉 Вы получили ❄️25 за регистрацию по скам-ссылке!")
            
            conn.close()
        
        markup = create_main_menu()
        bot.send_message(message.chat.id, "Добро пожаловать! Выберите действие:", reply_markup=markup)
    
    except Exception as e:
        print(f"Ошибка в start: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при регистрации. Попробуйте снова.")



# Обработчик команды "я"
@bot.message_handler(func=lambda message: message.text.lower() == "я")
def handle_me(message):
    try:
        if is_spam(message.from_user.id):
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        user_id = message.from_user.id
        
        # Начисляем проценты перед отображением
        calculate_interest(user_id)
        
        conn = sqlite3.connect('game.db')
        cursor = conn.cursor()
        
        cursor.execute('SELECT first_name, balance, video_cards, bank_deposit FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        
        if result:
            first_name, balance, video_cards, bank_deposit = result
            
            message_text = f"👤 Имя: {first_name}\n"
            message_text += f"❄️ Баланс: ❄️{format_balance(balance)}\n"
            message_text += f"🖥 Видеокарт: {video_cards}\n"
            message_text += f"🏦 В банке: ❄️{format_balance(bank_deposit)} (+0.5%/час)"
            
            # Пробуем отправить картинку с подписью
            try:
                with open('g.jpg', 'rb') as photo:
                    bot.send_photo(message.chat.id, photo, caption=message_text)
            except Exception as e:
                print(f"Ошибка при отправке фото: {e}")
                # Если картинки нет, отправляем только текст
                bot.send_message(message.chat.id, message_text)
        else:
            bot.send_message(message.chat.id, "❌ Пользователь не найден")
    
    except Exception as e:
        print(f"Ошибка в handle_me: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка базы данных. Попробуйте снова.")
# Обработчик кнопки "Майнинг"
@bot.message_handler(func=lambda message: message.text == "Майнинг")
def handle_mining(message):
    try:
        if is_spam(message.from_user.id):
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        # Просто показываем сообщение о разработке
        message_text = "🖥 Майнинг ферма\n\n"
        message_text += "⏳ Функция майнинга находится в разработке!\n"
        message_text += "Скоро здесь будет интересный функционал!\n\n"
        message_text += "💡 Следите за обновлениями!"
        
        bot.send_message(message.chat.id, message_text)
    
    except Exception as e:
        print(f"Ошибка в handle_mining: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка. Попробуйте снова.")

# Обработчик callback для майнинга (если остались старые callback'и)
@bot.callback_query_handler(func=lambda call: call.data.startswith('mining_'))
def mining_callback_handler(call):
    if is_spam(call.from_user.id):
        bot.answer_callback_query(call.id, "⏳ Слишком быстро! Подождите немного.")
        return
        
    user_id = call.from_user.id
    
    # Проверяем бан
    banned, reason = is_banned(user_id)
    if banned:
        bot.answer_callback_query(call.id, "🚫 Вы забанены!")
        return
    
    # Просто показываем сообщение о разработке
    bot.answer_callback_query(call.id, "⏳ Майнинг в разработке!")
    
    message_text = "🖥 Майнинг ферма\n\n"
    message_text += "⏳ Функция майнинга находится в разработке!\n"
    message_text += "Скоро здесь будет интересный функционал!\n\n"
    message_text += "💡 Следите за обновлениями!"
    
    # Если сообщение можно редактировать, редактируем его
    try:
        bot.edit_message_text(
            message_text,
            call.message.chat.id,
            call.message.message_id
        )
    except:
        # Если не получается редактировать, отправляем новое сообщение
        bot.send_message(call.message.chat.id, message_text)

# Обработчик кнопки "Работа"
@bot.message_handler(func=lambda message: message.text == "Работа")
def handle_work(message):
    if is_spam(message.from_user.id):
        return
    
    # Проверяем бан
    banned, reason = is_banned(message.from_user.id)
    if banned:
        bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
        return
        
    bot.send_message(message.chat.id, "💼 Выберите способ заработка:", reply_markup=create_work_menu())

# Обработчик кнопки "◀️ Назад"
@bot.message_handler(func=lambda message: message.text == "◀️ Назад")
def handle_back(message):
    if is_spam(message.from_user.id):
        return
    
    # Проверяем бан
    banned, reason = is_banned(message.from_user.id)
    if banned:
        bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
        return
        
    markup = create_main_menu()
    bot.send_message(message.chat.id, "Главное меню:", reply_markup=markup)

# Обработчик кнопки "🖱️ Кликер"
@bot.message_handler(func=lambda message: message.text == "🖱️ Кликер")
def handle_clicker(message):
    if is_spam(message.from_user.id):
        return
    
    # Проверяем бан
    banned, reason = is_banned(message.from_user.id)
    if banned:
        bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
        return
        
    bot.send_message(message.chat.id, "🎯 Найди правильную кнопку:", reply_markup=create_clicker_keyboard())

# Обработчик callback для кликера
@bot.callback_query_handler(func=lambda call: call.data.startswith('clicker_'))
def clicker_callback_handler(call):
    if is_spam(call.from_user.id):
        bot.answer_callback_query(call.id, "⏳ Слишком быстро! Подождите немного.")
        return
        
    user_id = call.from_user.id
    
    # Проверяем бан
    banned, reason = is_banned(user_id)
    if banned:
        bot.answer_callback_query(call.id, "🚫 Вы забанены!")
        return
    
    symbol = call.data.split('_')[1]
    
    try:
        if symbol == "✅":
            conn = sqlite3.connect('game.db')
            cursor = conn.cursor()
            
            cursor.execute('SELECT click_power, click_streak FROM users WHERE user_id = ?', (user_id,))
            click_power, click_streak = cursor.fetchone()
            
            new_streak = click_streak + 1
            cursor.execute('UPDATE users SET click_streak = ? WHERE user_id = ?', (new_streak, user_id))
            
            cursor.execute(
                'UPDATE users SET balance = balance + ?, last_click = ? WHERE user_id = ?',
                (click_power, time.time(), user_id)
            )
            conn.commit()
            
            cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
            new_balance = cursor.fetchone()[0]
            
            conn.close()
            
            bot.answer_callback_query(call.id, "✅ Верно! +❄️" + format_balance(click_power))
            bot.edit_message_text(
                f"👻 Серия: {new_streak}\n❄️ Баланс: ❄️{format_balance(new_balance)}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=create_clicker_keyboard()
            )
        else:
            conn = sqlite3.connect('game.db')
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET click_streak = 0 WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()
            
            bot.answer_callback_query(call.id, "❌ Неверно! Серия сброшена.")
            bot.edit_message_text(
                "❌ Неверный выбор! Серия сброшена.\n🎯 Найди правильную кнопку:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=create_clicker_keyboard()
            )
    
    except Exception as e:
        print(f"Ошибка в clicker_callback_handler: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка базы данных")

# Обработчик кнопки "👥 Скам"
@bot.message_handler(func=lambda message: message.text == "👥 Скам")
def handle_scam(message):
    try:
        if is_spam(message.from_user.id):
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        user_id = message.from_user.id
        conn = sqlite3.connect('game.db')
        cursor = conn.cursor()
        
        cursor.execute('SELECT referral_code FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if result:
            ref_code = result[0]
            
            cursor.execute('SELECT COUNT(*) FROM users WHERE referred_by = ?', (user_id,))
            ref_count = cursor.fetchone()[0]
            
            earned = ref_count * 25
            
            ref_link = f"https://t.me/{(bot.get_me()).username}?start={ref_code}"
            
            message_text = f"👨🏻‍💻 Твоя скам-ссылка:\n{ref_link}\n(нажми на неё, чтобы скопировать)\n\n"
            message_text += f"📊 Статистика:\n"
            message_text += f"Заскамлено людей: {ref_count}\n"
            message_text += f"Заработано: ❄️{format_balance(earned)}\n\n"
            message_text += "💡 Кидай ссылку друзьям и скамь их на бабки!"
            
            bot.send_message(message.chat.id, message_text)
        else:
            bot.send_message(message.chat.id, "❌ Реферальный код не найден")
        
        conn.close()
    except Exception as e:
        print(f"Ошибка в handle_scam: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка базы данных. Попробуйте снова.")

# Обработчик кнопки "Топ скам"
@bot.message_handler(func=lambda message: message.text == "Топ скам")
def handle_top_scam(message):
    try:
        if is_spam(message.from_user.id):
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        conn = sqlite3.connect('game.db')
        cursor = conn.cursor()
        
        cursor.execute('''
        SELECT u.first_name, u.username, COUNT(r.user_id) as ref_count
        FROM users u
        JOIN users r ON u.user_id = r.referred_by
        WHERE u.is_banned = 0
        GROUP BY u.user_id
        ORDER BY ref_count DESC
        LIMIT 10
        ''')
        top_scammers = cursor.fetchall()
        
        message_text = "❄️ ТОП СКАМА ❄️\n\n"
        
        if top_scammers:
            medals = ["👑", "🥈", '🥉', "🔰", "🔰", "🔰", "🔰", "🔰", "🔰", "🔰"]
            for i, (first_name, username, ref_count) in enumerate(top_scammers):
                medal = medals[i] if i < len(medals) else "🔰"
                display_name = f"@{username}" if username else first_name
                message_text += f"{medal} {i+1}. {display_name}: {ref_count} скам\n"
        else:
            message_text += "Топ скама пока пуст!\n"
        
        message_text += "\n💡 Скамь друзей и попади в топ!"
        
        bot.send_message(message.chat.id, message_text)
        
        conn.close()
    except Exception as e:
        print(f"Ошибка в handle_top_scam: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка базы данных. Попробуйте снова.")

@bot.message_handler(func=lambda message: message.text in ["Баланс", "б", "/balance"])
def handle_balance(message):
    try:
        if is_spam(message.from_user.id):
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        user_id = message.from_user.id
        balance = get_balance(user_id)
        bot.send_message(message.chat.id, f" Ваш баланс: ❄️{format_balance(balance)}")
    except Exception as e:
        print(f"Ошибка в handle_balance: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка базы данных. Попробуйте снова.")

@bot.message_handler(func=lambda message: message.text in ["Мажоры", "топ", "Топ"])
def handle_majors(message):
    try:
        if is_spam(message.from_user.id):
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        top_message = create_top_message()
        bot.send_message(message.chat.id, top_message)
    except Exception as e:
        print(f"Ошибка в handle_majors: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка базы данных. Попробуйте снова.")

# Обработчик кнопки "Банк"
@bot.message_handler(func=lambda message: message.text == "Банк")
def handle_bank(message):
    try:
        if is_spam(message.from_user.id):
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        user_id = message.from_user.id
        bank_deposit = get_bank_deposit(user_id)
        
        bank_text = f"""🏦 Банковские услуги:

❄️ На вкладе: ❄️{format_balance(bank_deposit)}
📈 Проценты: 0.5% каждый час
❄️ Начисляются автоматически при любом действии

📝 Команды:
• вклад [сумма] - положить под 0.5% в час
• снять [сумма] - забрать с вклада"""
        
        bot.send_message(message.chat.id, bank_text)
    except Exception as e:
        print(f"Ошибка в handle_bank: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка базы данных. Попробуйте снова.")

# Обработчик команды "вклад"
@bot.message_handler(func=lambda message: message.text.lower().startswith('вклад '))
def handle_deposit(message):
    try:
        if is_spam(message.from_user.id):
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        user_id = message.from_user.id
        balance = get_balance(user_id)
        bank_deposit = get_bank_deposit(user_id)
        
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: вклад 1000к")
            return
        
        deposit_amount = parse_bet_amount(' '.join(parts[1:]), balance)
        
        if deposit_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма")
            return
        
        if deposit_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма должна быть больше 0")
            return
        
        if deposit_amount > balance:
            bot.send_message(message.chat.id, "❌ Недостаточно средств на балансе")
            return
        
        update_balance(user_id, -deposit_amount)
        update_bank_deposit(user_id, deposit_amount)
        
        new_balance = get_balance(user_id)
        new_deposit = get_bank_deposit(user_id)
        
        bot.send_message(message.chat.id,
                       f"✅ Вы положили ❄️{format_balance(deposit_amount)} на вклад под 0.5% в час\n"
                       f"❄️ На вкладе: ❄️{format_balance(new_deposit)}\n"
                       f"❄️ Баланс: ❄️{format_balance(new_balance)}")
    
    except Exception as e:
        print(f"Ошибка в handle_deposit: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка в операции. Попробуйте снова.")

# Обработчик команды "снять"
@bot.message_handler(func=lambda message: message.text.lower().startswith('снять '))
def handle_withdraw(message):
    try:
        if is_spam(message.from_user.id):
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        user_id = message.from_user.id
        bank_deposit = get_bank_deposit(user_id)
        
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: снять 1000к")
            return
        
        withdraw_amount = parse_bet_amount(' '.join(parts[1:]), bank_deposit)
        
        if withdraw_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма")
            return
        
        if withdraw_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма должна быть больше 0")
            return
        
        if withdraw_amount > bank_deposit:
            bot.send_message(message.chat.id, "❌ Недостаточно средств на вкладе")
            return
        
        update_balance(user_id, withdraw_amount)
        update_bank_deposit(user_id, -withdraw_amount)
        
        new_balance = get_balance(user_id)
        new_deposit = get_bank_deposit(user_id)
        
        bot.send_message(message.chat.id,
                       f"✅ Вы сняли ❄️{format_balance(withdraw_amount)} с вклада\n"
                       f"❄️ Осталось на вкладе: ❄️{format_balance(new_deposit)}\n"
                       f"❄️ Баланс: ❄️{format_balance(new_balance)}")
    
    except Exception as e:
        print(f"Ошибка в handle_withdraw: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка в операции. Попробуйте снова.")

# Обработчик кнопки "Казино"
@bot.message_handler(func=lambda message: message.text == "Казино")
def handle_casino(message):
    if is_spam(message.from_user.id):
        bot.send_message(message.chat.id, "⏳ Слишком быстро! Подождите 1 секунду между командами.")
        return
        
    casino_text = """🎰 Казино:

Добро пожаловать в казино!
Ознакомьтесь с командами. Желаем приятно провести время!

📝 Команды:
• рул/рулетка [тип ставки] [сумма]
Примеры ставок:
рул кра/красный 1000к
рул чер/черный все
рул бол/большие 1кк
рул мал/малые 1ккк

• куб/кости [число] [сумма]
куб 1 1000к
куб 6 все
куб чет/нечет все

• слот/слоты [сумма]
слот 1000к

• бск/баскетбол [сумма]
бск 1000к

• фтб/футбол [сумма]
фтб 1000к

• дартс [сумма]
дартс 1000к

• боул/боулинг [сумма]
боул 1000к"""
    
    bot.send_message(message.chat.id, casino_text)

# Обработчик рулетки
@bot.message_handler(func=lambda message: message.text.lower().startswith(('рул ', 'рулетка ')))
def handle_roulette(message):
    try:
        if is_spam(message.from_user.id):
            bot.send_message(message.chat.id, "⏳ Слишком быстро! Подождите 1 секунду между командами.")
            return
            
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        user_id = message.from_user.id
        balance = get_balance(user_id)
        
        parts = message.text.lower().split()
        if len(parts) < 3:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: рул красный 1000к")
            return
        
        bet_type = parts[1]
        bet_amount = parse_bet_amount(' '.join(parts[2:]), balance)
        
        if bet_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки")
            return
        
        if bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма ставки должна быть больше 0")
            return
        
        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Недостаточно средств для ставки")
            return
        
        # Сразу списываем ставку с баланса
        update_balance(user_id, -bet_amount)
        
        # Генерируем случайное число рулетки (0-36)
        winning_number = random.randint(0, 36)
        
        win = False
        multiplier = 1
        bet_type_name = ""
        
        # Определяем цвет выпавшего числа
        red_numbers = [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]
        black_numbers = [2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35]
        
        # Сначала пробуем распознать как число (0-36)
        try:
            number_bet = int(bet_type)
            if 0 <= number_bet <= 36:
                win = winning_number == number_bet
                multiplier = 36  # ×36 за угаданное число
                bet_type_name = f"число {number_bet}"
            else:
                bot.send_message(message.chat.id, "❌ Число должно быть от 0 до 36")
                update_balance(user_id, bet_amount)  # Возвращаем ставку
                return
        except ValueError:
            # Если не число, проверяем другие типы ставок
            if bet_type in ['красный', 'крас', 'кра', 'кр', 'к']:
                win = winning_number in red_numbers
                multiplier = 2
                bet_type_name = "красный"
            elif bet_type in ['черный', 'чер', 'черн', 'ч', 'чр']:
                win = winning_number in black_numbers
                multiplier = 2
                bet_type_name = "черный"
            elif bet_type in ['зеленый', 'зел', 'з', '0', 'зеро', 'ноль']:
                win = winning_number == 0
                multiplier = 36
                bet_type_name = "зеленый (0)"
            elif bet_type in ['большие', 'бол', 'б', 'бльш']:
                win = winning_number >= 19 and winning_number <= 36
                multiplier = 2
                bet_type_name = "большие (19-36)"
            elif bet_type in ['малые', 'мал', 'м', 'мл']:
                win = winning_number >= 1 and winning_number <= 18
                multiplier = 2
                bet_type_name = "малые (1-18)"
            elif bet_type in ['чет', 'четные', 'четн', 'ч']:
                # Четные: числа от 1 до 36, которые делятся на 2
                win = winning_number % 2 == 0 and winning_number != 0  # 0 не считается четным!
                multiplier = 2
                bet_type_name = "четные"
            elif bet_type in ['нечет', 'нечетные', 'неч', 'н', 'нечетн']:
                # Нечетные: числа от 1 до 36, которые не делятся на 2
                win = winning_number % 2 == 1 and winning_number != 0  # 0 не считается нечетным!
                multiplier = 2
                bet_type_name = "нечетные"
            else:
                bot.send_message(message.chat.id, "❌ Неверный тип ставки. Доступно: красный, черный, зеленый, большие, малые, чет, нечет, или число 0-36")
                update_balance(user_id, bet_amount)  # Возвращаем ставку
                return
        
        if win:
            win_amount = bet_amount * multiplier
            update_balance(user_id, win_amount)
            new_balance = get_balance(user_id)
            
            # Определяем цвет выпавшего числа для сообщения
            color = "🔴" if winning_number in red_numbers else "⚫" if winning_number in black_numbers else "🟢"
            
            bot.send_message(message.chat.id, 
                           f"🎉 Ставка на {bet_type_name} выиграла!\n"
                           f"🎯 Выпало: {winning_number} {color}\n"
                           f"💰 Вы выиграли ❄️{format_balance(win_amount)}!\n"
                           f"❄️ Баланс: ❄️{format_balance(new_balance)}")
        else:
            new_balance = get_balance(user_id)
            
            # Определяем цвет выпавшего числа для сообщения
            color = "🔴" if winning_number in red_numbers else "⚫" if winning_number in black_numbers else "🟢"
            
            bot.send_message(message.chat.id, 
                           f"❌ Ставка на {bet_type_name} проиграла!\n"
                           f"🎯 Выпало: {winning_number} {color}\n"
                           f"💸 Вы проиграли ❄️{format_balance(bet_amount)}.\n"
                           f"❄️ Баланс: ❄️{format_balance(new_balance)}")
    
    except Exception as e:
        print(f"Ошибка в handle_roulette: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка в игре. Попробуйте снова.")

# Обработчик костей
@bot.message_handler(func=lambda message: message.text.lower().startswith(('куб ', 'кости ')))
def handle_dice(message):
    try:
        if is_spam(message.from_user.id):
            bot.send_message(message.chat.id, "⏳ Слишком быстро! Подождите 1 секунду между командами.")
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        user_id = message.from_user.id
        balance = get_balance(user_id)
        
        parts = message.text.lower().split()
        if len(parts) < 3:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: куб 1 1000к или куб бол все")
            return
        
        bet_type = parts[1]
        bet_amount = parse_bet_amount(' '.join(parts[2:]), balance)
        
        if bet_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки")
            return
        
        if bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма ставки должна быть больше 0")
            return
        
        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Недостаточно средств для ставки")
            return
        
        # Сразу списываем ставку с баланса
        update_balance(user_id, -bet_amount)
        
        dice_message = bot.send_dice(message.chat.id, emoji='🎲')
        time.sleep(4)
        
        result = dice_message.dice.value
        
        win = False
        multiplier = 1
        
        # Проверяем тип ставки
        if bet_type in ['бол', 'большие', 'больше', 'б']:
            # Большие: 4, 5, 6
            win = result in [4, 5, 6]
            multiplier = 2
            bet_type_name = "большие (4-5-6)"
        
        elif bet_type in ['мал', 'малые', 'меньше', 'м']:
            # Малые: 1, 2, 3
            win = result in [1, 2, 3]
            multiplier = 2
            bet_type_name = "малые (1-2-3)"
        
        elif bet_type in ['чет', 'четные', 'четн', 'ч']:
            # Четные: 2, 4, 6
            win = result in [2, 4, 6]
            multiplier = 2
            bet_type_name = "четные"
        
        elif bet_type in ['нечет', 'нечетные', 'неч', 'н']:
            # Нечетные: 1, 3, 5
            win = result in [1, 3, 5]
            multiplier = 2
            bet_type_name = "нечетные"
        
        else:
            try:
                target = int(bet_type)
                if 1 <= target <= 6:
                    # Конкретное число
                    win = result == target
                    multiplier = 6
                    bet_type_name = f"число {target}"
                else:
                    bot.send_message(message.chat.id, "❌ Неверный тип ставки. Доступно: 1-6, бол, мал, чет, нечет")
                    # Возвращаем ставку при ошибке
                    update_balance(user_id, bet_amount)
                    return
            except:
                bot.send_message(message.chat.id, "❌ Неверный тип ставки. Доступно: 1-6, бол, мал, чет, нечет")
                # Возвращаем ставку при ошибке
                update_balance(user_id, bet_amount)
                return
        
        if win:
            win_amount = bet_amount * multiplier
            update_balance(user_id, win_amount)
            new_balance = get_balance(user_id)
            bot.send_message(message.chat.id, f"🎉 Ставка на {bet_type_name} выиграла! Выпало: {result}\nВы выиграли ❄️{format_balance(win_amount)}! Баланс: ❄️{format_balance(new_balance)}")
        else:
            new_balance = get_balance(user_id)
            bot.send_message(message.chat.id, f"❌ Ставка на {bet_type_name} проиграла! Выпало: {result}\nВы проиграли ❄️{format_balance(bet_amount)}. Баланс: ❄️{format_balance(new_balance)}")
    
    except Exception as e:
        print(f"Ошибка в handle_dice: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка в игре. Попробуйте снова.")

# Обработчик слотов
@bot.message_handler(func=lambda message: message.text.lower().startswith(('слот ', 'слоты ')))
def handle_slots(message):
    try:
        if is_spam(message.from_user.id):
            bot.send_message(message.chat.id, "⏳ Слишком быстро! Подождите 1 секунду между командами.")
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        user_id = message.from_user.id
        balance = get_balance(user_id)
        
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: слот 1000к")
            return
        
        bet_amount = parse_bet_amount(' '.join(parts[1:]), balance)
        
        if bet_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки")
            return
        
        if bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма ставки должна быть больше 0")
            return
        
        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Недостаточно средств для ставки")
            return
        
        # Сразу списываем ставку с баланса
        update_balance(user_id, -bet_amount)
        
        dice_message = bot.send_dice(message.chat.id, emoji='🎰')
        time.sleep(4)
        
        result = dice_message.dice.value
        
        win = False
        multiplier = 1
        
        if result == 1:  # Джекпот
            win = True
            multiplier = 64
        elif result == 22:  # Три семерки
            win = True
            multiplier = 10
        elif result == 43:  # Три вишни
            win = True
            multiplier = 5
        elif result == 64:  # Три одинаковых символа
            win = True
            multiplier = 3
        
        if win:
            win_amount = bet_amount * multiplier
            update_balance(user_id, win_amount)
            new_balance = get_balance(user_id)
            bot.send_message(message.chat.id, f"🎉 Вы выиграли ❄️{format_balance(win_amount)}! Баланс: ❄️{format_balance(new_balance)}")
        else:
            new_balance = get_balance(user_id)
            bot.send_message(message.chat.id, f"❌ Вы проиграли ❄️{format_balance(bet_amount)}. Баланс: ❄️{format_balance(new_balance)}")
    
    except Exception as e:
        print(f"Ошибка в handle_slots: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка в игре. Попробуйте снова.")

# Обработчик баскетбола
@bot.message_handler(func=lambda message: message.text.lower().startswith(('бск ', 'баскетбол ')))
def handle_basketball(message):
    try:
        if is_spam(message.from_user.id):
            bot.send_message(message.chat.id, "⏳ Слишком быстро! Подождите 1 секунду между командами.")
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        user_id = message.from_user.id
        balance = get_balance(user_id)
        
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: бск 1000к")
            return
        
        bet_amount = parse_bet_amount(' '.join(parts[1:]), balance)
        
        if bet_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки")
            return
        
        if bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма ставки должна быть больше 0")
            return
        
        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Недостаточно средств для ставки")
            return
        
        # Сразу списываем ставку с баланса
        update_balance(user_id, -bet_amount)
        
        dice_message = bot.send_dice(message.chat.id, emoji='🏀')
        time.sleep(4)
        
        result = dice_message.dice.value
        
        win = False
        multiplier = 2.5  # Изменено с 2 на 2.5
        
        if result == 4 or result == 5:  # Попадание
            win = True
        
        if win:
            win_amount = int(bet_amount * multiplier)
            update_balance(user_id, win_amount)
            new_balance = get_balance(user_id)
            bot.send_message(message.chat.id, f"🎉 Вы выиграли ❄️{format_balance(win_amount)}! Баланс: ❄️{format_balance(new_balance)}")
        else:
            new_balance = get_balance(user_id)
            bot.send_message(message.chat.id, f"❌ Вы проиграли ❄️{format_balance(bet_amount)}. Баланс: ❄️{format_balance(new_balance)}")
    
    except Exception as e:
        print(f"Ошибка в handle_basketball: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка в игре. Попробуйте снова.")

# Обработчик футбола
@bot.message_handler(func=lambda message: message.text.lower().startswith(('фтб ', 'футбол ')))
def handle_football(message):
    try:
        if is_spam(message.from_user.id):
            bot.send_message(message.chat.id, "⏳ Слишком быстро! Подождите 1 секунду между командами.")
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        user_id = message.from_user.id
        balance = get_balance(user_id)
        
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: фтб 1000к")
            return
        
        bet_amount = parse_bet_amount(' '.join(parts[1:]), balance)
        
        if bet_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки")
            return
        
        if bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма ставки должна быть больше 0")
            return
        
        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Недостаточно средств для ставки")
            return
        
        # Сразу списываем ставку с баланса
        update_balance(user_id, -bet_amount)
        
        dice_message = bot.send_dice(message.chat.id, emoji='⚽')
        time.sleep(4)
        
        result = dice_message.dice.value
        
        win = False
        multiplier = 1.5  # Коэффициент выигрыша
        
        # В футболе значения кубика:
        # 1 - мяч за пределами поля
        # 2 - мяч попал в штангу
        # 3 - гол (левая сторона)
        # 4 - гол (правая сторона) 
        # 5 - гол (центр)
        # 6 - мяч заблокирован вратарем
        
        # Все голы (3, 4, 5) считаются победой
        if result == 3 or result == 4 or result == 5:  # Гол
            win = True
        
        if win:
            win_amount = int(bet_amount * multiplier)
            update_balance(user_id, win_amount)
            new_balance = get_balance(user_id)
            bot.send_message(message.chat.id, f"🎉 ГОООЛ! Вы выиграли ❄️{format_balance(win_amount)}! Баланс: ❄️{format_balance(new_balance)}")
        else:
            new_balance = get_balance(user_id)
            bot.send_message(message.chat.id, f"❌ Мяч не забит! Вы проиграли ❄️{format_balance(bet_amount)}. Баланс: ❄️{format_balance(new_balance)}")
    
    except Exception as e:
        print(f"Ошибка в handle_football: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка в игре. Попробуйте снова.")

# Обработчик дартса
@bot.message_handler(func=lambda message: message.text.lower().startswith('дартс '))
def handle_darts(message):
    try:
        if is_spam(message.from_user.id):
            bot.send_message(message.chat.id, "⏳ Слишком быстро! Подождите 1 секунду между командами.")
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        user_id = message.from_user.id
        balance = get_balance(user_id)
        
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: дартс 1000к")
            return
        
        bet_amount = parse_bet_amount(' '.join(parts[1:]), balance)
        
        if bet_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки")
            return
        
        if bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма ставки должна быть больше 0")
            return
        
        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Недостаточно средств для ставки")
            return
        
        # Сразу списываем ставку с баланса
        update_balance(user_id, -bet_amount)
        
        dice_message = bot.send_dice(message.chat.id, emoji='🎯')
        time.sleep(4)
        
        result = dice_message.dice.value
        
        win = False
        multiplier = 1
        
        if result == 6:  # Попадание в яблочко
            win = True
            multiplier = 5
        elif result == 5:  # Попадание в центр
            win = True
            multiplier = 2
        elif result == 4:  # Попадание во внешнее кольцо
            win = True
            multiplier = 1.5
        
        if win:
            win_amount = bet_amount * multiplier
            update_balance(user_id, win_amount)
            new_balance = get_balance(user_id)
            bot.send_message(message.chat.id, f"🎉 Вы выиграли ❄️{format_balance(win_amount)}! Баланс: ❄️{format_balance(new_balance)}")
        else:
            new_balance = get_balance(user_id)
            bot.send_message(message.chat.id, f"❌ Вы проиграли ❄️{format_balance(bet_amount)}. Баланс: ❄️{format_balance(new_balance)}")
    
    except Exception as e:
        print(f"Ошибка в handle_darts: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка в игре. Попробуйте снова.")

# Обработчик боулинга
@bot.message_handler(func=lambda message: message.text.lower().startswith(('боул ', 'боулинг ')))
def handle_bowling(message):
    try:
        if is_spam(message.from_user.id):
            bot.send_message(message.chat.id, "⏳ Слишком быстро! Подождите 1 секунду между командами.")
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        user_id = message.from_user.id
        balance = get_balance(user_id)
        
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: боул 1000к")
            return
        
        bet_amount = parse_bet_amount(' '.join(parts[1:]), balance)
        
        if bet_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма ставки")
            return
        
        if bet_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма ставки должна быть больше 0")
            return
        
        if bet_amount > balance:
            bot.send_message(message.chat.id, "❌ Недостаточно средств для ставки")
            return
        
        # Сразу списываем ставку с баланса
        update_balance(user_id, -bet_amount)
        
        dice_message = bot.send_dice(message.chat.id, emoji='🎳')
        time.sleep(3)
        
        result = dice_message.dice.value
        
        # Значения кубика боулинга в Telegram и сколько кеглей осталось:
        # 6 - страйк (все 10 кеглей сбиты) = 0 осталось
        # 5 - сбито 9 кеглей = 1 осталась
        # 4 - сбито 7-8 кеглей = 2-3 осталось
        # 3 - сбито 5-6 кеглей = 4-5 осталось
        # 2 - сбито 3-4 кегли = 6-7 осталось
        # 1 - сбито 1-2 кегли = 8-9 осталось
        
        if result == 6:  # Все кегли сбиты (0 осталось)
            win_amount = bet_amount * 2  # ×2 за страйк
            update_balance(user_id, win_amount)
            new_balance = get_balance(user_id)
            bot.send_message(message.chat.id, f"🎳 СТРАЙК! Все кегли сбиты! Вы выиграли ❄️{format_balance(win_amount)}! Баланс: ❄️{format_balance(new_balance)}")
        
        elif result == 5:  # 9 кеглей сбито (1 осталась)
            # Возврат ставки
            update_balance(user_id, bet_amount)
            new_balance = get_balance(user_id)
            bot.send_message(message.chat.id, f"⚖️ Осталась 1 кегля! Возврат ставки ❄️{format_balance(bet_amount)}. Баланс: ❄️{format_balance(new_balance)}")
        
        elif result == 1:  # 1-2 кегли сбито (8-9 осталось)
            # Это тоже "почти все" по вашей логике? 
            # Если да, то возвращаем ставку, если нет - оставляем проигрыш
            # Здесь оставляю проигрыш, так как сбито мало кеглей
            new_balance = get_balance(user_id)
            bot.send_message(message.chat.id, f"❌ Сбито всего 1-2 кегли! Осталось 8-9. Вы проиграли ❄️{format_balance(bet_amount)}. Баланс: ❄️{format_balance(new_balance)}")
        
        else:  # result 2, 3, 4 - проигрыш (осталось 2+ кеглей)
            new_balance = get_balance(user_id)
            # Определяем сколько примерно кеглей осталось
            if result == 2:
                remaining = "6-7 кеглей"
            elif result == 3:
                remaining = "4-5 кеглей"
            elif result == 4:
                remaining = "2-3 кегли"
            else:
                remaining = "кеглей"
            
            bot.send_message(message.chat.id, f"❌ Осталось {remaining}! Вы проиграли ❄️{format_balance(bet_amount)}. Баланс: ❄️{format_balance(new_balance)}")
    
    except Exception as e:
        print(f"Ошибка в handle_bowling: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка в игре. Попробуйте снова.")

# Обработчик команды "чек" (для пользователей)
@bot.message_handler(func=lambda message: message.text.lower().startswith('чек ') and not is_admin(message.from_user.id))
def handle_check(message):
    try:
        if is_spam(message.from_user.id):
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        user_id = message.from_user.id
        balance = get_balance(user_id)
        
        parts = message.text.lower().split()
        if len(parts) < 3:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: чек 10ккк 2")
            return
        
        # Парсим сумму
        amount = parse_bet_amount(parts[1], balance)
        
        if amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма")
            return
        
        if amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма должна быть больше 0")
            return
        
        # Парсим количество активаций
        try:
            activations = int(parts[2])
            if activations <= 0 or activations > 100:
                bot.send_message(message.chat.id, "❌ Количество активаций должно быть от 1 до 100")
                return
        except:
            bot.send_message(message.chat.id, "❌ Неверное количество активаций")
            return
        
        # Рассчитываем общую сумму для списания
        total_amount = amount * activations
        
        if total_amount > balance:
            bot.send_message(message.chat.id, f"❌ Недостаточно средств для создания чека! Нужно: ❄️{format_balance(total_amount)}")
            return
        
        # Списываем общую сумму с баланса
        update_balance(user_id, -total_amount)
        
        # Генерируем случайный код чека
        code = ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=8))
        
        conn = sqlite3.connect('game.db')
        cursor = conn.cursor()
        
        # Создаем чек
        cursor.execute(
            'INSERT INTO checks (code, amount, max_activations, created_by) VALUES (?, ?, ?, ?)',
            (code, amount, activations, user_id)
        )
        
        conn.commit()
        conn.close()
        
        check_link = f"https://t.me/{(bot.get_me()).username}?start={code}"
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("Активировать❄️", url=check_link))
        
        bot.send_message(message.chat.id,
                f"💳 Чек создан!\n"
                f"❄️ Сумма за активацию: ❄️{format_balance(amount)}\n"
                f"🔢 Активаций: {activations}\n",  # <-- Добавьте запятую здесь
                reply_markup=markup)
    
    except Exception as e:
        print(f"Ошибка в создании чека: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при создании чека. Попробуйте снова.")

# Обработчик команды "чек" для админов
@bot.message_handler(func=lambda message: message.text.lower().startswith('чеф ') and is_admin(message.from_user.id))
def handle_admin_check(message):
    try:
        if is_spam(message.from_user.id):
            return
            
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды")
            return
        
        parts = message.text.split()
        if len(parts) < 3:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: чек 1000к 10")
            return
        
        # Парсим сумму
        amount = parse_bet_amount(parts[1], float('inf'))
        
        if amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма")
            return
        
        if amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма должна быть больше 0")
            return
        
        # Парсим количество активаций
        try:
            max_activations = int(parts[2])
            if max_activations <= 0:
                bot.send_message(message.chat.id, "❌ Количество активаций должно быть больше 0")
                return
        except:
            bot.send_message(message.chat.id, "❌ Неверное количество активаций")
            return
        
        check_code = f"check{random.randint(100000, 999999)}"
        
        conn = sqlite3.connect('game.db')
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO checks (code, amount, max_activations, created_by) VALUES (?, ?, ?, ?)',
            (check_code, amount, max_activations, message.from_user.id)
        )
        conn.commit()
        conn.close()
        
        check_link = f"https://t.me/{(bot.get_me()).username}?start={check_code}"
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("Активировать❄️", url=check_link))
        
        bot.send_message(message.chat.id, 
                f"✅ Админский чек создан!\n"
                f"❄️ Сумма за активацию: ❄️{format_balance(amount)}\n"
                f"🔢 Активаций: {max_activations}\n",  # <-- Добавьте запятую
                reply_markup=markup)
                       
    
    except Exception as e:
        print(f"Ошибка в handle_admin_check: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при создании чека")

# Обработчик команды "выдать" для админов
@bot.message_handler(func=lambda message: message.text.lower().startswith('выдать ') and is_admin(message.from_user.id))
def handle_give_money(message):
    try:
        if is_spam(message.from_user.id):
            return
            
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды")
            return
        
        parts = message.text.split()
        if len(parts) < 3:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: выдать @username 1000к")
            return
        
        target = parts[1]
        amount = parse_bet_amount(' '.join(parts[2:]), float('inf'))
        
        if amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма")
            return
        
        if amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма должна быть больше 0")
            return
        
        conn = sqlite3.connect('game.db')
        cursor = conn.cursor()
        
        if target.startswith('@'):
            cursor.execute('UPDATE users SET balance = balance + ? WHERE username = ?', (amount, target[1:]))
        else:
            try:
                target_id = int(target)
                cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, target_id))
            except:
                bot.send_message(message.chat.id, "❌ Неверный ID пользователя")
                conn.close()
                return
        
        conn.commit()
        conn.close()
        
        bot.send_message(message.chat.id, f"✅ Выдано ❄️{format_balance(amount)} пользователю {target}")
    
    except Exception as e:
        print(f"Ошибка в handle_give_money: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при выдаче денег")

# Обработчик команды "забрать" для админов
@bot.message_handler(func=lambda message: message.text.lower().startswith('забрать ') and is_admin(message.from_user.id))
def handle_take_money(message):
    try:
        if is_spam(message.from_user.id):
            return
            
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды")
            return
        
        if not message.reply_to_message:
            bot.send_message(message.chat.id, "❌ Ответьте на сообщение пользователя, у которого хотите забрать деньги")
            return
        
        target_user_id = message.reply_to_message.from_user.id
        target_username = message.reply_to_message.from_user.username
        target_first_name = message.reply_to_message.from_user.first_name
        
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: забрать 1000к")
            return
        
        amount = parse_bet_amount(' '.join(parts[1:]), float('inf'))
        
        if amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма")
            return
        
        if amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма должна быть больше 0")
            return
        
        # Создаем пользователя если его нет
        get_or_create_user(target_user_id, target_username, target_first_name)
        
        conn = sqlite3.connect('game.db')
        cursor = conn.cursor()
        
        # Проверяем баланс пользователя
        cursor.execute('SELECT balance FROM users WHERE user_id = ?', (target_user_id,))
        user_balance = cursor.fetchone()
        
        if user_balance:
            balance = user_balance[0]
            if balance < amount:
                bot.send_message(message.chat.id, f"❌ У пользователя недостаточно средств! Баланс: ❄️{format_balance(balance)}")
                conn.close()
                return
            
            # Забираем деньги
            cursor.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, target_user_id))
            conn.commit()
            
            # Получаем новое имя пользователя
            target_name = f"@{target_username}" if target_username else target_first_name
            
            bot.send_message(message.chat.id, 
                           f"✅ Забрано ❄️{format_balance(amount)} у пользователя {target_name}\n"
                           f"❄️ Новый баланс пользователя: ❄️{format_balance(balance - amount)}")
            
            # Уведомляем пользователя
            try:
                bot.send_message(target_user_id, 
                               f"⚠️ У вас забрали ❄️{format_balance(amount)} администратором\n"
                               f"❄️ Новый баланс: ❄️{format_balance(balance - amount)}")
            except:
                pass
        else:
            bot.send_message(message.chat.id, "❌ Пользователь не найден")
        
        conn.close()
    
    except Exception as e:
        print(f"Ошибка в handle_take_money: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при заборе денег")

# Обработчик команды "бан" для админов
@bot.message_handler(func=lambda message: message.text.lower().startswith('бан ') and is_admin(message.from_user.id))
def handle_ban(message):
    try:
        if is_spam(message.from_user.id):
            return
            
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды")
            return
        
        if not message.reply_to_message:
            bot.send_message(message.chat.id, "❌ Ответьте на сообщение пользователя, которого хотите забанить")
            return
        
        target_user_id = message.reply_to_message.from_user.id
        target_username = message.reply_to_message.from_user.username
        target_first_name = message.reply_to_message.from_user.first_name
        
        parts = message.text.split()
        ban_reason = "Нарушение правил"
        if len(parts) > 1:
            ban_reason = ' '.join(parts[1:])
        
        # Создаем пользователя если его нет
        get_or_create_user(target_user_id, target_username, target_first_name)
        
        conn = sqlite3.connect('game.db')
        cursor = conn.cursor()
        
        # Баним пользователя
        cursor.execute('UPDATE users SET is_banned = 1, ban_reason = ?, banned_at = CURRENT_TIMESTAMP WHERE user_id = ?',
                      (ban_reason, target_user_id))
        conn.commit()
        conn.close()
        
        target_name = f"@{target_username}" if target_username else target_first_name
        
        bot.send_message(message.chat.id, 
                       f"✅ Пользователь {target_name} забанен!\n"
                       f"📝 Причина: {ban_reason}")
        
        # Уведомляем пользователя
        try:
            bot.send_message(target_user_id, 
                           f"🚫 Вы забанены в боте!\n"
                           f"📝 Причина: {ban_reason}\n"
                           f"⏰ Время бана: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                           f"👮 Администратор: @{message.from_user.username if message.from_user.username else 'Неизвестно'}")
        except:
            pass
    
    except Exception as e:
        print(f"Ошибка в handle_ban: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при бане пользователя")

# Обработчик команды "разбан" для админов
@bot.message_handler(func=lambda message: message.text.lower().startswith('разбан ') and is_admin(message.from_user.id))
def handle_unban(message):
    try:
        if is_spam(message.from_user.id):
            return
            
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды")
            return
        
        if not message.reply_to_message:
            bot.send_message(message.chat.id, "❌ Ответьте на сообщение пользователя, которого хотите разбанить")
            return
        
        target_user_id = message.reply_to_message.from_user.id
        
        conn = sqlite3.connect('game.db')
        cursor = conn.cursor()
        
        # Проверяем, существует ли пользователь
        cursor.execute('SELECT username, first_name, is_banned FROM users WHERE user_id = ?', (target_user_id,))
        user_data = cursor.fetchone()
        
        if not user_data:
            bot.send_message(message.chat.id, "❌ Пользователь не найден в базе данных")
            conn.close()
            return
        
        username, first_name, is_banned = user_data
        
        if is_banned == 0:
            bot.send_message(message.chat.id, "⚠️ Пользователь не забанен")
            conn.close()
            return
        
        # Разбаниваем пользователя
        cursor.execute('UPDATE users SET is_banned = 0, ban_reason = NULL, banned_at = NULL WHERE user_id = ?',
                      (target_user_id,))
        conn.commit()
        conn.close()
        
        target_name = f"@{username}" if username else first_name
        
        bot.send_message(message.chat.id, 
                       f"✅ Пользователь {target_name} разбанен!")
        
        # Уведомляем пользователя
        try:
            bot.send_message(target_user_id, 
                           f"🎉 Вы разбанены в боте!\n"
                           f"👮 Администратор: @{message.from_user.username if message.from_user.username else 'Неизвестно'}\n"
                           f"⏰ Время разбана: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        except:
            pass
    
    except Exception as e:
        print(f"Ошибка в handle_unban: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при разбане пользователя")

# Обработчик команды "передать"/"кинуть"
@bot.message_handler(func=lambda message: message.text.lower().startswith(('передать ', 'кинуть ')))
def handle_transfer(message):
    try:
        if is_spam(message.from_user.id):
            return
        
        # Проверяем бан
        banned, reason = is_banned(message.from_user.id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            return
            
        user_id = message.from_user.id
        balance = get_balance(user_id)
        
        if not message.reply_to_message:
            bot.send_message(message.chat.id, "❌ Ответьте на сообщение пользователя, которому хотите передать деньги")
            return
        
        target_user_id = message.reply_to_message.from_user.id
        target_username = message.reply_to_message.from_user.username
        target_first_name = message.reply_to_message.from_user.first_name
        
        if target_user_id == user_id:
            bot.send_message(message.chat.id, "❌ Нельзя передавать деньги самому себе")
            return
        
        # Проверяем бан получателя
        target_banned, target_reason = is_banned(target_user_id)
        if target_banned:
            bot.send_message(message.chat.id, "❌ Невозможно передать деньги забаненному пользователю")
            return
        
        parts = message.text.lower().split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: передать 1000к")
            return
        
        transfer_amount = parse_bet_amount(' '.join(parts[1:]), balance)
        
        if transfer_amount is None:
            bot.send_message(message.chat.id, "❌ Неверная сумма")
            return
        
        if transfer_amount <= 0:
            bot.send_message(message.chat.id, "❌ Сумма должна быть больше 0")
            return
        
        if transfer_amount > balance:
            bot.send_message(message.chat.id, "❌ Недостаточно средств для передачи")
            return
        
        # Создаем получателя если его нет
        get_or_create_user(target_user_id, target_username, target_first_name)
        
        # Переводим деньги
        update_balance(user_id, -transfer_amount)
        update_balance(target_user_id, transfer_amount)
        
        new_balance = get_balance(user_id)
        target_balance = get_balance(target_user_id)
        
        sender_username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        target_name = f"@{target_username}" if target_username else target_first_name
        
        bot.send_message(message.chat.id,
                       f"✅ Вы передали ❄️{format_balance(transfer_amount)} пользователю {target_name}\n"
                       f"❄️ Ваш баланс: ❄️{format_balance(new_balance)}")
        
        bot.send_message(target_user_id,
                       f"🎉 Вам передали ❄️{format_balance(transfer_amount)} от {sender_username}\n"
                       f"❄️ Ваш баланс: ❄️{format_balance(target_balance)}")
    
    except Exception as e:
        print(f"Ошибка в передаче денег: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при передаче денег. Попробуйте снова.")
# Обработчик команды "рассылка" для админов
@bot.message_handler(func=lambda message: message.text.lower().startswith('рассылка ') and is_admin(message.from_user.id))
def handle_broadcast(message):
    try:
        if is_spam(message.from_user.id):
            return
            
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды")
            return
        
        # Получаем текст рассылки (все после "рассылка ")
        broadcast_text = message.text[len('рассылка '):].strip()
        
        if not broadcast_text:
            bot.send_message(message.chat.id, "❌ Введите текст для рассылки. Пример: рассылка Привет всем!")
            return
        
        bot.send_message(message.chat.id, f"⏳ Начинаю рассылку...\nТекст: {broadcast_text[:100]}...")
        
        conn = sqlite3.connect('game.db')
        cursor = conn.cursor()
        
        # Получаем всех пользователей которые не забанены
        cursor.execute('SELECT user_id FROM users WHERE is_banned = 0')
        users = cursor.fetchall()
        conn.close()
        
        total_users = len(users)
        successful = 0
        failed = 0
        
        bot.send_message(message.chat.id, f"📊 Всего пользователей для рассылки: {total_users}")
        
        # Рассылаем сообщения
        for user_data in users:
            user_id = user_data[0]
            try:
                bot.send_message(user_id, f"📢 Рассылка от администрации:\n\n{broadcast_text}")
                successful += 1
                
                # Небольшая задержка чтобы не превысить лимиты Telegram
                time.sleep(0.05)
                
            except Exception as e:
                failed += 1
                print(f"Ошибка при отправке пользователю {user_id}: {e}")
        
        # Отправляем отчет
        report_message = f"✅ Рассылка завершена!\n\n"
        report_message += f"📊 Статистика:\n"
        report_message += f"• Всего пользователей: {total_users}\n"
        report_message += f"• Успешно отправлено: {successful}\n"
        report_message += f"• Не удалось отправить: {failed}\n"
        
        bot.send_message(message.chat.id, report_message)
    
    except Exception as e:
        print(f"Ошибка в рассылке: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка при рассылке: {e}")

# Обработчик команды "статистика" для админов
@bot.message_handler(func=lambda message: message.text.lower() == 'статистика' and is_admin(message.from_user.id))
def handle_statistics(message):
    try:
        if is_spam(message.from_user.id):
            return
            
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды")
            return
        
        conn = sqlite3.connect('game.db')
        cursor = conn.cursor()
        
        # Получаем общую статистику
        cursor.execute('SELECT COUNT(*) FROM users')
        total_users = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM users WHERE is_banned = 1')
        banned_users = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM users WHERE captcha_passed = 1')
        active_users = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM users WHERE DATE(registered_at) = DATE("now")')
        new_today = cursor.fetchone()[0]
        
        cursor.execute('SELECT SUM(balance) FROM users')
        total_balance = cursor.fetchone()[0] or 0
        
        cursor.execute('SELECT SUM(bank_deposit) FROM users')
        total_deposits = cursor.fetchone()[0] or 0
        
        conn.close()
        
        stats_message = f"📊 Статистика бота:\n\n"
        stats_message += f"👥 Всего пользователей: {total_users}\n"
        stats_message += f"✅ Активных (прошли капчу): {active_users}\n"
        stats_message += f"🚫 Забанено: {banned_users}\n"
        stats_message += f"📈 Новых сегодня: {new_today}\n"
        stats_message += f"💰 Общий баланс: ❄️{format_balance(total_balance)}\n"
        stats_message += f"🏦 Общая сумма в банке: ❄️{format_balance(total_deposits)}\n"
        
        bot.send_message(message.chat.id, stats_message)
    
    except Exception as e:
        print(f"Ошибка в статистике: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка при получении статистики: {e}")

# Обработчик команды "поиск" для админов
@bot.message_handler(func=lambda message: message.text.lower().startswith('поиск ') and is_admin(message.from_user.id))
def handle_search_user(message):
    try:
        if is_spam(message.from_user.id):
            return
            
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой команды")
            return
        
        search_query = message.text[len('поиск '):].strip()
        
        if not search_query:
            bot.send_message(message.chat.id, "❌ Введите поисковый запрос. Пример: поиск @username или поиск 123456789")
            return
        
        conn = sqlite3.connect('game.db')
        cursor = conn.cursor()
        
        # Ищем пользователя по ID, username или имени
        cursor.execute('''
            SELECT user_id, username, first_name, balance, is_banned, 
                   registered_at, last_activity 
            FROM users 
            WHERE user_id = ? OR username LIKE ? OR first_name LIKE ?
            LIMIT 10
        ''', (search_query, f'%{search_query}%', f'%{search_query}%'))
        
        users = cursor.fetchall()
        conn.close()
        
        if not users:
            bot.send_message(message.chat.id, f"❌ Пользователи по запросу '{search_query}' не найдены")
            return
        
        result_message = f"🔍 Результаты поиска '{search_query}':\n\n"
        
        for i, user in enumerate(users, 1):
            user_id, username, first_name, balance, is_banned, registered_at, last_activity = user
            
            display_name = f"@{username}" if username else first_name
            status = "🚫 Забанен" if is_banned == 1 else "✅ Активен"
            
            # Форматируем даты
            try:
                reg_date = registered_at[:10] if registered_at else "Неизвестно"
                last_active = last_activity[:16] if last_activity else "Неизвестно"
            except:
                reg_date = "Неизвестно"
                last_active = "Неизвестно"
            
            result_message += f"{i}. {display_name} (ID: {user_id})\n"
            result_message += f"   Статус: {status}\n"
            result_message += f"   Баланс: ❄️{format_balance(balance)}\n"
            result_message += f"   Регистрация: {reg_date}\n"
            result_message += f"   Последняя активность: {last_active}\n\n"
        
        bot.send_message(message.chat.id, result_message)
    
    except Exception as e:
        print(f"Ошибка в поиске: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка при поиске: {e}")
# Обработчик кнопки "Панель клана"
@bot.message_handler(func=lambda message: message.text == "Панель клана")
def handle_clan_panel(message):
    if is_spam(message.from_user.id):
        return
    
    # Проверяем бан
    banned, reason = is_banned(message.from_user.id)
    if banned:
        bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
        return
        
    bot.send_message(message.chat.id, "Панель клана в разработке...")
# Обработчик для проверки математической капчи
@bot.message_handler(func=lambda message: message.from_user.id in user_captcha_status)
def check_captcha_answer(message):
    try:
        user_id = message.from_user.id
        
        # Проверяем бан
        banned, reason = is_banned(user_id)
        if banned:
            bot.send_message(message.chat.id, f"🚫 Вы забанены в боте!\nПричина: {reason}")
            if user_id in user_captcha_status:
                del user_captcha_status[user_id]
            return
        
        # Получаем правильный ответ
        correct_answer = user_captcha_status.get(user_id)
        
        # Проверяем ответ пользователя
        user_answer = message.text.strip()
        
        if user_answer == correct_answer:
            # Помечаем капчу как пройденную
            conn = sqlite3.connect('game.db')
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET captcha_passed = 1 WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()
            
            # Удаляем капчу из памяти
            if user_id in user_captcha_status:
                del user_captcha_status[user_id]
            
            bot.send_message(message.chat.id, "✅ Капча пройдена!")
            
            markup = create_main_menu()
            bot.send_message(message.chat.id, "✅ Регистрация успешно завершена! Добро пожаловать!", reply_markup=markup)
        else:
            # Генерируем новую капчу
            captcha_question, new_correct_answer = generate_captcha()
            user_captcha_status[user_id] = new_correct_answer
            
            bot.send_message(message.chat.id, 
                           f"❌ Неверный ответ! Попробуйте еще раз:\n\n"
                           f"{captcha_question}\n\n"
                           f"Отправьте ответ числом в чат.",
                           parse_mode='Markdown')
    
    except Exception as e:
        print(f"Ошибка в check_captcha_answer: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка проверки капчи")
# Функция для очистки старых капч
def clean_old_captchas():
    while True:
        time.sleep(300)  # Каждые 5 минут
        current_time = time.time()
        to_remove = []
        for user_id, captcha_data in list(user_captcha_status.items()):
            # Вместо проверки времени, просто очищаем старые капчи
            # Мы храним только правильный ответ, так что просто очищаем периодически
            to_remove.append(user_id)
        
        for user_id in to_remove:
            if user_id in user_captcha_status:
                del user_captcha_status[user_id]
                print(f"Очищена старая капча для пользователя {user_id}")

# Запускаем очистку капч в отдельном потоке
captcha_cleaner_thread = threading.Thread(target=clean_old_captchas, daemon=True)
captcha_cleaner_thread.start()

# Инициализация базы данных при запуске
init_db()

# Запускаем бота
if __name__ == "__main__":
    print("Бот запущен...")
    try:
        bot.infinity_polling()
    except Exception as e:
        print(f"Критическая ошибка: {e}")
        print("Перезапустите бота.")