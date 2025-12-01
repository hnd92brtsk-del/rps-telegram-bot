# main.py
"""
RPS Telegram Bot (simplified, but functional)
- Uses Flask for webhook and endpoints
- Uses gspread with a service account JSON (provided via env var as base64)
- Exposes /webhook for Telegram webhook and /daily_check that a scheduler should call at 11:00 Europe/Amsterdam
"""

import os
import json
import base64
import datetime
from flask import Flask, request, jsonify
import gspread
from google.oauth2.service_account import Credentials
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
)
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ConversationHandler

# -----------------------
# Configuration from ENV
# -----------------------
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")  # обязательно установить
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")  # на хостинге укажешь полный URL
SERVICE_ACCOUNT_B64 = os.environ.get("GSPREAD_SERVICE_ACCOUNT_JSON_B64")

if not BOT_TOKEN:
    raise RuntimeError("Пожалуйста установи переменную окружения TG_BOT_TOKEN")

if not SERVICE_ACCOUNT_B64:
    raise RuntimeError("Пожалуйста установи переменную окружения GSPREAD_SERVICE_ACCOUNT_JSON_B64")

# -----------------------
# Google Sheets init
# -----------------------
sa_json = base64.b64decode(SERVICE_ACCOUNT_B64).decode('utf-8')
sa_info = json.loads(sa_json)
scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
credentials = Credentials.from_service_account_info(sa_info, scopes=scopes)
gc = gspread.authorize(credentials)

# Название таблицы
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "rps_bot_db")

sh = gc.open(SPREADSHEET_NAME)
# листы
users_sheet = sh.worksheet("users")
games_sheet = sh.worksheet("games")
moves_sheet = sh.worksheet("moves")

# -----------------------
# Helper functions for Sheets
# -----------------------
def find_user_row(telegram_id):
    vals = users_sheet.get_all_records()
    for idx, r in enumerate(vals, start=2):
        if int(r.get("user_id", 0)) == int(telegram_id):
            return idx, r
    return None, None

def register_user(telegram_id, name):
    row_idx, existing = find_user_row(telegram_id)
    if existing:
        return False
    users_sheet.append_row([telegram_id, name, datetime.date.today().isoformat()])
    return True

def get_today_game():
    today = datetime.date.today().isoformat()
    vals = games_sheet.get_all_records()
    for idx, r in enumerate(vals, start=2):
        if r.get("date") == today:
            return idx, r
    return None, None

def create_new_game(mode):
    today = datetime.date.today().isoformat()
    # game_id = date + count
    gid = f"{today}_{int(datetime.datetime.now().timestamp())}"
    games_sheet.append_row([gid, today, mode, "", 0, "FALSE"])
    return gid

def add_move(game_id, move_no, p1_id, p1_move, p2_id, p2_move, winner):
    moves_sheet.append_row([game_id, move_no, p1_id, p1_move, p2_id, p2_move, winner, datetime.datetime.now().isoformat()])

def update_game_result(game_row_index, winner, moves_count):
    games_sheet.update(f"D{game_row_index}:F{game_row_index}", [[winner, moves_count, "TRUE"]])

# Простая логика определения победителя между 'rock','paper','scissors'
def determine_winner(m1, m2):
    beats = {"rock":"scissors", "scissors":"paper", "paper":"rock"}
    if m1 == m2:
        return "tie"
    if beats[m1] == m2:
        return "player1"
    return "player2"

# -----------------------
# Telegram bot (handlers)
# -----------------------
app = Flask(__name__)
from telegram import Bot
telegram_bot = Bot(token=BOT_TOKEN)

# Use python-telegram-bot Application for convenience in handlers (we won't run polling here)
application = ApplicationBuilder().token(BOT_TOKEN).build()

# States for conversation (manual input flow)
(MANUAL_P1_MOVE, MANUAL_P2_MOVE) = range(2)

# /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Зарегистрироваться как Руся", callback_data="reg_Rusya"),
         InlineKeyboardButton("Зарегистрироваться как Никита", callback_data="reg_Nikita")],
        [InlineKeyboardButton("Выбрать режим (manual/auto)", callback_data="choose_mode")],
        [InlineKeyboardButton("Ввести ручную партию (если уже сыграли)", callback_data="manual_entry")],
        [InlineKeyboardButton("Отправить ход (авто режим)", callback_data="auto_move")],
        [InlineKeyboardButton("Показать статистику", callback_data="show_stats")],
    ]
    await update.message.reply_text("Привет! Это бот для игры РПС (Руся vs Никита). Выбери действие:", reply_markup=InlineKeyboardMarkup(keyboard))

# Callback queries
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("reg_"):
        name = data.split("_",1)[1]
        tg_id = query.from_user.id
        registered = register_user(tg_id, name)
        if registered:
            await query.edit_message_text(f"Зарегистрирован как {name}.")
        else:
            await query.edit_message_text(f"Уже зарегистрирован ранее.")
        return

    if data == "choose_mode":
        kb = [
            [InlineKeyboardButton("Ручной (manual)", callback_data="mode_manual")],
            [InlineKeyboardButton("Автоматический (auto)", callback_data="mode_auto")]
        ]
        await query.edit_message_text("Выбери режим (оба игрока должны выбрать один и тот же):", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("mode_"):
        chosen = data.split("_",1)[1]
        # Сохраняем выбор режима для этого пользователя (в простом варианте — поместим в таблицу games со статусом 'mode_vote')
        # Для простоты: применим правило: если есть незавершённая игра со статусом mode_vote и другой игрок уже выбрал тот же режим -> создаём игру
        row, g = get_today_game()
        if not g:
            # создаём временную запись с mode_vote (прошлая версия: создаём игру с выбранным режимом, но помечаем owner)
            gid = create_new_game("mode_vote")
            # запишем первый выбор в moves (move_no=0) для хранения выбора режима: store player id and choice in p1_move
            add_move(gid, 0, query.from_user.id, chosen, "", "", "mode_choice")
            await query.edit_message_text(f"Твой выбор '{chosen}' сохранён. Ожидаем выбор второго игрока.")
            return
        else:
            # есть запись mode_vote — читаем moves чтобы понять есть ли совпадение
            moves = moves_sheet.get_all_records()
            for r in moves:
                if r.get("game_id")==g.get("game_id") and r.get("winner_for_move")=="mode_choice":
                    # r содержит первый выбор
                    if r.get("player1_move")==chosen and r.get("player1_id") != query.from_user.id:
                        # Одинаковый выбор обоих — обновляем game mode и сообщаем
                        # Вычислим индекс game row
                        g_row_idx = row
                        games_sheet.update_cell(g_row_idx, 3, chosen)  # column C = mode
                        await query.edit_message_text(f"Оба выбрали '{chosen}'. Режим закреплён. Можете играть.")
                        return
            # если дошли сюда — просто добавим ещё один mode_choice
            add_move(g.get("game_id"), 0, query.from_user.id, chosen, "", "", "mode_choice")
            await query.edit_message_text("Твой выбор сохранён. Ждём другого игрока.")
            return

    if data == "manual_entry":
        await query.edit_message_text("Начинаем ручной ввод. Отправь ход игрока 1 (rock/paper/scissors).")
        return

    if data == "auto_move":
        kb2 = [
            [InlineKeyboardButton("Камень", callback_data="auto_rock"),
             InlineKeyboardButton("Ножницы", callback_data="auto_scissors"),
             InlineKeyboardButton("Бумага", callback_data="auto_paper")]
        ]
        await query.edit_message_text("Выбери свой ход (он будет скрыт до хода второго игрока).", reply_markup=InlineKeyboardMarkup(kb2))
        return

    if data.startswith("auto_"):
        move = data.split("_",1)[1]
        tg_id = query.from_user.id
        # найти или создать сегодняшнюю игру в режиме auto
        row, g = get_today_game()
        if not g or g.get("mode") not in ("auto","mode_vote"):
            gid = create_new_game("auto")
        else:
            gid = g.get("game_id")
        # Сохраняем в moves: если в строке с move_no=1 нет второго — добавим или обновим
        # Упростим: добавим запись с player1_id=current user, player1_move=move, winner_for_move="auto_choice"
        add_move(gid, 1, tg_id, move, "", "", "auto_choice")
        await query.edit_message_text("Ход сохранён. Результат будет определён, когда оба игрока сделают выбор или когда сработает проверка в 11:00.")
        return

    if data == "show_stats":
        # простая демонстрация — посчитаем общее число партий
        rows = games_sheet.get_all_records()
        total = len(rows)
        finished = sum(1 for r in rows if str(r.get("finished")).upper() in ("TRUE","True","true"))
        await query.edit_message_text(f"Всего партий: {total}\nЗавершённых: {finished}\n(детальная статистика пока упрощена)")
        return

# Endpoint для scheduler: в 11:00 GMT+1/2 вызвать этот URL (POST)
def process_daily_checks():
    """
    - Находит сегодняшнюю игру(ы) в режиме auto.
    - Если в moves есть по паре auto_choice от двух разных игроков для данного game_id -> сравнивает, пишет результат.
    - Если ничья -> делает winner='draw_pending' и оставляет игру незавершённой (finished FALSE). Тогда игроки могут прислать следующий ход (move_no++).
    - Если победитель -> помечает игру finished TRUE, записывает winner и moves_count.
    """
    today = datetime.date.today().isoformat()
    games = games_sheet.get_all_records()
    today_games = [g for g in games if g.get("date")==today and g.get("mode")=="auto"]
    processed = []
    all_moves = moves_sheet.get_all_records()
    for g in today_games:
        gid = g.get("game_id")
        # собрать все auto_choice записи для этого gid
        choices = [m for m in all_moves if m.get("game_id")==gid and m.get("winner_for_move")=="auto_choice"]
        # группируем по player1_id (т.к. в нашей записье мы сохранили ход в player1_move)
        if len(choices) < 2:
            continue
        # возьмём 2 последних уникальных игроков
        unique = {}
        for c in reversed(choices):
            unique[c.get("player1_id")] = c
            if len(unique) >= 2:
                break
        if len(unique) < 2:
            continue
        items = list(unique.values())[:2]
        p1 = items[0]
        p2 = items[1]
        winner = determine_winner(p1.get("player1_move"), p2.get("player1_move"))
        if winner == "tie":
            # записать как ход с tie и пометить draw_pending
            move_no = int(g.get("moves_count") or 0) + 1
            add_move(gid, move_no, p1.get("player1_id"), p1.get("player1_move"), p2.get("player1_id"), p2.get("player1_move"), "tie")
            # обновим moves_count в games (к-во ходов)
            # обновление: оставляем finished FALSE, winner как "draw_pending"
            # найдем строку игры
            rows = games_sheet.get_all_records()
            for idx, r in enumerate(rows, start=2):
                if r.get("game_id")==gid:
                    games_sheet.update_cell(idx, 4, "draw_pending")  # column D = winner column
                    games_sheet.update_cell(idx, 5, move_no)         # column E = moves_count
                    break
            processed.append((gid, "tie"))
        else:
            move_no = int(g.get("moves_count") or 0) + 1
            add_move(gid, move_no, p1.get("player1_id"), p1.get("player1_move"), p2.get("player1_id"), p2.get("player1_move"), winner)
            # определим кто выиграл: если winner == 'player1' -> p1 player, иначе p2
            winner_name = "Unknown"
            wid = p1.get("player1_id") if winner == "player1" else p2.get("player1_id")
            # попытаемся найти в users
            urows = users_sheet.get_all_records()
            for u in urows:
                if int(u.get("user_id")) == int(wid):
                    winner_name = u.get("name")
                    break
            # обновим игру в games (winner, moves_count, finished TRUE)
            rows = games_sheet.get_all_records()
            for idx, r in enumerate(rows, start=2):
                if r.get("game_id")==gid:
                    games_sheet.update(f"D{idx}:F{idx}", [[winner_name, move_no, "TRUE"]])
                    break
            # уведомим обоих игроков (если есть их телеграм id)
            try:
                telegram_bot.send_message(chat_id=int(p1.get("player1_id")), text=f"Игра {gid}: победитель: {winner_name} (ходов: {move_no})")
            except Exception:
                pass
            try:
                telegram_bot.send_message(chat_id=int(p2.get("player1_id")), text=f"Игра {gid}: победитель: {winner_name} (ходов: {move_no})")
            except Exception:
                pass
            processed.append((gid, "finished"))
    return processed

# Flask routes: Telegram webhook and daily_check
@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():
    # Telegram присылает update -> передаём в application
    update = Update.de_json(request.get_json(force=True), telegram_bot)
    # обработаем синхронно - используем loop
    import asyncio
    asyncio.get_event_loop().create_task(application.process_update(update))
    return "OK"

@app.route("/daily_check", methods=["POST","GET"])
def daily_check():
    processed = process_daily_checks()
    return jsonify({"processed": processed})

# Регистрация обработчиков (handler mapping)
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(callback_router))

# Если запускаешь локально для тестов (polling)
if __name__ == "__main__":
    # Для локального теста: запускаем Flask + set webhook (если нужно)
    # Но при проде: используем хостинг и установим webhook URL: https://<host>/webhook
    print("Run Flask app")
    app.run(host="0.0.0.0", port=PORT)
