import os
import json
import base64
import time
import datetime
import asyncio

from flask import Flask, request, jsonify

import gspread
from gspread.exceptions import WorksheetNotFound
from google.oauth2.service_account import Credentials

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ============================================================
# 1. Переменные окружения
# ============================================================

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME")
SERVICE_JSON_B64 = os.getenv("GSPREAD_SERVICE_ACCOUNT_JSON_B64")

if not TG_BOT_TOKEN:
    raise RuntimeError("Не задан TG_BOT_TOKEN в переменных окружения")

if not SPREADSHEET_NAME:
    raise RuntimeError("Не задан SPREADSHEET_NAME в переменных окружения")

if not SERVICE_JSON_B64:
    raise RuntimeError("Не задан GSPREAD_SERVICE_ACCOUNT_JSON_B64 в переменных окружения")

# ============================================================
# 2. Инициализация Google Sheets
# ============================================================

sa_info = json.loads(base64.b64decode(SERVICE_JSON_B64).decode("utf-8"))
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
credentials = Credentials.from_service_account_info(sa_info, scopes=scopes)
gc = gspread.authorize(credentials)
sh = gc.open(SPREADSHEET_NAME)


def get_or_create_worksheet(name: str, headers: list):
    """Получаем лист по имени, если нет — создаём и ставим заголовки."""
    try:
        ws = sh.worksheet(name)
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=200, cols=len(headers))
        ws.append_row(headers)
        return ws

    # если первая строка пустая — добавим заголовки
    first_row = ws.row_values(1)
    if not first_row:
        ws.append_row(headers)
    return ws


users_sheet = get_or_create_worksheet(
    "users",
    ["user_id", "name", "reg_date"],
)

mode_votes_sheet = get_or_create_worksheet(
    "mode_votes",
    ["date", "user_id", "mode"],
)

games_sheet = get_or_create_worksheet(
    "games",
    ["game_id", "date", "mode", "winner", "moves_count", "finished"],
)

moves_sheet = get_or_create_worksheet(
    "moves",
    [
        "game_id",
        "move_no",
        "player1_id",
        "player1_move",
        "player2_id",
        "player2_move",
        "winner_for_move",
        "timestamp",
    ],
)

# ============================================================
# 3. Вспомогательные функции для работы с таблицами
# ============================================================

def today_str() -> str:
    return datetime.date.today().isoformat()


def find_user(telegram_id: int):
    """Ищем пользователя в листе users. Возвращаем dict или None."""
    records = users_sheet.get_all_records()
    for r in records:
        if str(r.get("user_id")) == str(telegram_id):
            return r
    return None


def register_user(telegram_id: int, name: str):
    """Регистрируем пользователя, если его ещё нет."""
    existing = find_user(telegram_id)
    if existing:
        return False
    users_sheet.append_row([str(telegram_id), name, today_str()])
    return True


def record_mode_vote(user_id: int, mode: str):
    """
    Записываем голос за режим (manual/auto) на сегодня.
    Возвращаем:
        ('waiting', None)       — ждём второго игрока / разные режимы
        ('started', game_id)    — оба выбрали одно и то же, игра создана
        ('already', game_id)    — игра на сегодня уже есть
    """
    date = today_str()
    records = mode_votes_sheet.get_all_records()

    # обновим/добавим голос этого пользователя
    updated = False
    for idx, r in enumerate(records, start=2):
        if r.get("date") == date and str(r.get("user_id")) == str(user_id):
            mode_votes_sheet.update_cell(idx, 3, mode)
            updated = True
            break

    if not updated:
        mode_votes_sheet.append_row([date, str(user_id), mode])

    # проверяем, есть ли уже игра на сегодня
    g_row, g = get_today_game()
    if g:
        return "already", g.get("game_id")

    # теперь читаем все голоса за сегодня
    records = mode_votes_sheet.get_all_records()
    votes_today = [r for r in records if r.get("date") == date]

    # нам нужны два разных user_id с одинаковым mode
    modes = {}
    for r in votes_today:
        uid = str(r.get("user_id"))
        m = r.get("mode")
        modes.setdefault(m, set()).add(uid)

    for m, users_set in modes.items():
        if len(users_set) >= 2:
            # оба выбрали одинаковый режим — создаём игру
            gid = create_new_game(m)
            return "started", gid

    return "waiting", None


def get_today_game():
    """Возвращаем (row_index, game_dict) для сегодняшней игры или (None, None)."""
    date = today_str()
    records = games_sheet.get_all_records()
    for idx, r in enumerate(records, start=2):
        if r.get("date") == date:
            return idx, r
    return None, None


def create_new_game(mode: str) -> str:
    """Создаём новую игру на сегодня (если её ещё нет)."""
    now = int(time.time())
    gid = f"{today_str()}_{now}"
    games_sheet.append_row([gid, today_str(), mode, "", 0, "FALSE"])
    return gid


def get_moves_for_game(game_id: str):
    records = moves_sheet.get_all_records()
    return [r for r in records if r.get("game_id") == game_id]


def save_auto_choice(game_id: str, user_id: int, move: str):
    """
    Сохраняем выбор в авто-режиме.
    Записываем как winner_for_move='auto_choice', move_no=0.
    """
    # удалим старые auto_choice этого пользователя для этой игры (если были)
    records = moves_sheet.get_all_records()
    for idx, r in enumerate(records, start=2):
        if (
            r.get("game_id") == game_id
            and r.get("winner_for_move") == "auto_choice"
            and str(r.get("player1_id")) == str(user_id)
        ):
            moves_sheet.delete_rows(idx)

    # добавим новую запись
    moves_sheet.append_row(
        [
            game_id,
            0,
            str(user_id),
            move,
            "",
            "",
            "auto_choice",
            datetime.datetime.now().isoformat(),
        ]
    )


def determine_winner(move1: str, move2: str) -> str:
    """
    Определяем победителя между 'rock', 'paper', 'scissors'.
    Возвращаем 'tie', 'player1' или 'player2'.
    """
    beats = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
    if move1 == move2:
        return "tie"
    if beats[move1] == move2:
        return "player1"
    return "player2"


def process_daily_auto_game():
    """
    Вызывается раз в день (cron /daily_check).
    Обрабатывает авто-игру:
      - если два игрока сделали выбор → считаем результат
      - записываем в games и moves
      - отправляем сообщение игрокам
    """
    row, game = get_today_game()
    if not game:
        return {"status": "no_game_today"}

    if game.get("mode") != "auto":
        return {"status": "mode_not_auto"}

    if str(game.get("finished")).upper() == "TRUE":
        return {"status": "already_finished"}

    gid = game.get("game_id")
    all_moves = moves_sheet.get_all_records()
    choices = [
        r
        for r in all_moves
        if r.get("game_id") == gid and r.get("winner_for_move") == "auto_choice"
    ]

    # берём последние выборы двух разных пользователей
    by_user = {}
    for r in choices:
        by_user[str(r.get("player1_id"))] = r

    if len(by_user) < 2:
        return {"status": "not_enough_players"}

    # берём первые два
    users_list = list(by_user.keys())[:2]
    r1 = by_user[users_list[0]]
    r2 = by_user[users_list[1]]

    move1 = r1.get("player1_move")
    move2 = r2.get("player1_move")
    winner = determine_winner(move1, move2)

    # считаем номер хода
    existing_moves = get_moves_for_game(gid)
    move_no = sum(1 for m in existing_moves if m.get("winner_for_move") in ("player1", "player2", "tie")) + 1

    # записываем ход
    moves_sheet.append_row(
        [
            gid,
            move_no,
            r1.get("player1_id"),
            move1,
            r2.get("player1_id"),
            move2,
            winner,
            datetime.datetime.now().isoformat(),
        ]
    )

    # обновляем games
    if winner == "tie":
        games_sheet.update_cell(row, 5, move_no)          # moves_count
        games_sheet.update_cell(row, 4, "draw_pending")   # winner
        # игру не завершаем
        return {"status": "tie", "move_no": move_no}

    # определяем имя победителя по users
    winner_id = r1.get("player1_id") if winner == "player1" else r2.get("player1_id")
    users = users_sheet.get_all_records()
    winner_name = "Unknown"
    for u in users:
        if str(u.get("user_id")) == str(winner_id):
            winner_name = u.get("name") or "Unknown"
            break

    # обновим игру как завершённую
    games_sheet.update_row(row, [gid, today_str(), "auto", winner_name, move_no, "TRUE"])

    # уведомим игроков через бота
    try:
        application.bot.send_message(
            chat_id=int(r1.get("player1_id")),
            text=f"Игра {today_str()} (auto). Победитель: {winner_name}. Ходов: {move_no}",
        )
    except Exception:
        pass

    try:
        application.bot.send_message(
            chat_id=int(r2.get("player1_id")),
            text=f"Игра {today_str()} (auto). Победитель: {winner_name}. Ходов: {move_no}",
        )
    except Exception:
        pass

    return {"status": "finished", "winner": winner_name, "move_no": move_no}

# ============================================================
# 4. Telegram bot: обработчики
# ============================================================

application = Application.builder().token(TG_BOT_TOKEN).build()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start — показываем главное меню."""
    user = update.effective_user
    tg_id = user.id

    user_record = find_user(tg_id)
    if user_record:
        hello = f"Привет, {user_record.get('name')}!"
    else:
        hello = "Привет! Ты ещё не зарегистрирован."

    keyboard = [
        [
            InlineKeyboardButton("Я — Руся", callback_data="reg_Rusya"),
            InlineKeyboardButton("Я — Никита", callback_data="reg_Nikita"),
        ],
        [InlineKeyboardButton("Выбрать режим (manual/auto)", callback_data="choose_mode")],
        [InlineKeyboardButton("Сделать ход (auto)", callback_data="auto_move")],
        [InlineKeyboardButton("Показать статистику", callback_data="show_stats")],
    ]
    await update.message.reply_text(hello, reply_markup=InlineKeyboardMarkup(keyboard))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка всех нажатий на кнопки."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    tg_id = user.id

    # --- регистрация ---
    if data.startswith("reg_"):
        name = "Руся" if data == "reg_Rusya" else "Никита"
        ok = register_user(tg_id, name)
        if ok:
            await query.edit_message_text(f"Ты зарегистрирован как {name}.")
        else:
            await query.edit_message_text(f"Ты уже зарегистрирован.")
        return

    # --- выбор режима ---
    if data == "choose_mode":
        kb = [
            [InlineKeyboardButton("Ручной (manual)", callback_data="mode_manual")],
            [InlineKeyboardButton("Автоматический (auto)", callback_data="mode_auto")],
        ]
        await query.edit_message_text(
            "Выбери режим на сегодня.\n"
            "Режим будет установлен, когда оба игрока выберут одинаково.",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data in ("mode_manual", "mode_auto"):
        mode = "manual" if data == "mode_manual" else "auto"
        status, gid = record_mode_vote(tg_id, mode)

        if status == "waiting":
            await query.edit_message_text(
                f"Твой выбор: {mode}. Ожидаем выбор второго игрока."
            )
        elif status == "already":
            await query.edit_message_text(
                f"Игра на сегодня уже существует (режим {mode})."
            )
        elif status == "started":
            await query.edit_message_text(
                f"Отлично! Оба игрока выбрали {mode}. Игра на сегодня создана."
            )
        return

    # --- авто-ход ---
    if data == "auto_move":
        # проверим, есть ли сегодня игра в авто-режиме
        row, g = get_today_game()
        if not g or g.get("mode") != "auto":
            await query.edit_message_text(
                "На сегодня ещё не создана авто-игра. "
                "Сначала оба игрока должны выбрать режим 'Автоматический'."
            )
            return

        kb = [
            [
                InlineKeyboardButton("Камень", callback_data="auto_rock"),
                InlineKeyboardButton("Ножницы", callback_data="auto_scissors"),
                InlineKeyboardButton("Бумага", callback_data="auto_paper"),
            ]
        ]
        await query.edit_message_text(
            "Выбери свой ход (он будет скрыт до расчёта результата).",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("auto_"):
        move_map = {
            "auto_rock": "rock",
            "auto_scissors": "scissors",
            "auto_paper": "paper",
        }
        move = move_map[data]

        row, g = get_today_game()
        if not g or g.get("mode") != "auto":
            await query.edit_message_text(
                "Авто-игра на сегодня ещё не создана. "
                "Сначала оба игрока выбирают авто-режим."
            )
            return

        gid = g.get("game_id")
        save_auto_choice(gid, tg_id, move)
        await query.edit_message_text(
            f"Твой ход ({move}) сохранён.\n"
            f"Когда оба игрока сделают ход, а также при ежедневной проверке в 11:00, "
            f"бот посчитает результат."
        )
        return

    # --- статистика ---
    if data == "show_stats":
        records = games_sheet.get_all_records()
        total = len(records)
        finished = sum(
            1 for r in records if str(r.get("finished")).upper() == "TRUE"
        )
        auto_games = sum(1 for r in records if r.get("mode") == "auto")
        manual_games = sum(1 for r in records if r.get("mode") == "manual")

        text = (
            f"Общая статистика:\n"
            f"Всего партий: {total}\n"
            f"Завершённых: {finished}\n"
            f"Авто-игр: {auto_games}\n"
            f"Ручных игр: {manual_games}\n"
            f"(Детализированную статистику можно будет добавить позже.)"
        )
        await query.edit_message_text(text)
        return


# Регистрация обработчиков
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CallbackQueryHandler(on_callback))

# ============================================================
# 5. Запуск Telegram Application (initialize + start)
# ============================================================

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
loop.run_until_complete(application.initialize())
loop.run_until_complete(application.start())
print("Telegram Application initialized and started.")

# ============================================================
# 6. Flask-приложение и webhook
# ============================================================

app = Flask(__name__)
WEBHOOK_PATH = "/webhook"


@app.route("/", methods=["GET"])
def index():
    return "RPS bot is running."


@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():
    """Приём апдейтов от Telegram (Webhook)."""
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.process_update(update))
    return "OK"


@app.route("/daily_check", methods=["GET", "POST"])
def daily_check():
    """Эндпоинт, который будет вызывать cron-job.org каждый день в 11:00."""
    result = process_daily_auto_game()
    return jsonify(result)


# ============================================================
# 7. Запуск Flask (для Render)
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"Запуск Flask на порту {port}")
    app.run(host="0.0.0.0", port=port)
