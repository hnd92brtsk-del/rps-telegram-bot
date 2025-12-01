import os
import json
import base64
import random
import time
import asyncio
import threading
from datetime import datetime, date

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

# ============================
# 1. Переменные окружения
# ============================

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME")
SERVICE_JSON_B64 = os.getenv("GSPREAD_SERVICE_ACCOUNT_JSON_B64")

if not TG_BOT_TOKEN or not SPREADSHEET_NAME or not SERVICE_JSON_B64:
    # при запуске на Render всё это должно быть задано
    pass

# ============================
# 2. Подключение к Google Sheets
# ============================

def init_gspread():
    if not SERVICE_JSON_B64 or not SPREADSHEET_NAME:
        return None, None
    sa_info = json.loads(base64.b64decode(SERVICE_JSON_B64).decode("utf-8"))
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(sa_info, scopes=scopes)
    gc_client = gspread.authorize(credentials)
    spreadsheet = gc_client.open(SPREADSHEET_NAME)
    return gc_client, spreadsheet

gc_client, sh = init_gspread()

def get_or_create_worksheet(name, headers):
    """Берём лист по имени, если нет — создаём с указанными заголовками."""
    if sh is None:
        return None
    try:
        ws = sh.worksheet(name)
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=500, cols=len(headers))
        ws.append_row(headers)
        return ws
    first = ws.row_values(1)
    if not first:
        ws.append_row(headers)
    return ws

# Листы в таблице
users_sheet = get_or_create_worksheet(
    "users", ["user_id", "name", "reg_date", "chat_id"]
)
mode_votes_sheet = get_or_create_worksheet(
    "mode_votes", ["date", "user_id", "mode"]
)
games_sheet = get_or_create_worksheet(
    "games", ["game_id", "date", "mode", "winner", "moves_count", "finished"]
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
logs_sheet = get_or_create_worksheet(
    "logs", ["timestamp", "user_id", "action", "details"]
)

# ============================
# 3. Вспомогательные функции
# ============================

def today_iso():
    return date.today().isoformat()

def today_human():
    # формат дд.мм.гг — как ты просил
    return datetime.now().strftime("%d.%m.%y")

def log_event(user_id, action, details=""):
    """Записываем событие в лист logs."""
    if logs_sheet is None:
        return
    logs_sheet.append_row(
        [
            datetime.now().isoformat(timespec="seconds"),
            str(user_id),
            action,
            details,
        ]
    )

def get_users_records():
    return users_sheet.get_all_records() if users_sheet is not None else []

def find_user(tg_id):
    """Поиск игрока по Telegram ID."""
    for r in get_users_records():
        if str(r.get("user_id")) == str(tg_id):
            return r
    return None

def get_other_user(tg_id):
    """Возвращает данные второго игрока (если оба зарегистрированы)."""
    users = get_users_records()
    if len(users) < 2:
        return None
    for r in users:
        if str(r.get("user_id")) != str(tg_id):
            return r
    return None

def register_user(tg_id, chat_id, name):
    """
    Регистрируем/обновляем игрока в листе users.
    Возврат: (создан_ли_с_нуля, 'already'/'new')
    """
    if users_sheet is None:
        return False, "sheet_error"
    existing = None
    row_idx = None
    for idx, r in enumerate(users_sheet.get_all_records(), start=2):
        if str(r.get("user_id")) == str(tg_id):
            existing = r
            row_idx = idx
            break
    if existing:
        users_sheet.update(
            f"A{row_idx}:D{row_idx}",
            [[str(tg_id), name, existing.get("reg_date") or today_iso(), str(chat_id)]],
        )
        log_event(tg_id, "re_register", name)
        return False, "already"
    users_sheet.append_row([str(tg_id), name, today_iso(), str(chat_id)])
    log_event(tg_id, "register", name)
    return True, "new"

def get_today_game():
    """Игра на сегодня (если есть)."""
    if games_sheet is None:
        return None, None
    for idx, r in enumerate(games_sheet.get_all_records(), start=2):
        if r.get("date") == today_iso():
            return idx, r
    return None, None

def create_new_game(mode):
    """Создаём игру на сегодня с заданным режимом."""
    if games_sheet is None:
        return None
    gid = f"{today_iso()}_{int(time.time())}"
    games_sheet.append_row([gid, today_iso(), mode, "", 0, "FALSE"])
    log_event("SYSTEM", "create_game", f"{gid} mode={mode}")
    return gid

def record_mode_vote(tg_id, mode):
    """
    Сохраняем выбор режима игрока.
    Возврат:
      ('waiting', None)   - выбран, ждём второго
      ('started', game_id)- оба выбрали, игра создана
      ('already', game_id)- игра уже есть
    """
    if mode_votes_sheet is None:
        return "error", None
    date_str = today_iso()
    records = mode_votes_sheet.get_all_records()
    updated = False
    for idx, r in enumerate(records, start=2):
        if r.get("date") == date_str and str(r.get("user_id")) == str(tg_id):
            mode_votes_sheet.update_cell(idx, 3, mode)
            updated = True
            break
    if not updated:
        mode_votes_sheet.append_row([date_str, str(tg_id), mode])

    log_event(tg_id, "mode_vote", mode)

    # Уже есть игра на сегодня?
    gi, g = get_today_game()
    if g:
        return "already", g.get("game_id")

    # Проверяем голоса за сегодня
    records = mode_votes_sheet.get_all_records()
    votes = {}
    for r in records:
        if r.get("date") != date_str:
            continue
        m = r.get("mode")
        votes.setdefault(m, set()).add(str(r.get("user_id")))
    for m, users in votes.items():
        if len(users) >= 2:
            gid = create_new_game(m)
            return "started", gid
    return "waiting", None

def determine_winner(a, b):
    """rock/paper/scissors → кто победил."""
    beats = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
    if a == b:
        return "tie"
    return "player1" if beats[a] == b else "player2"

def save_auto_choice(game_id, tg_id, move):
    """Сохраняем выбор в авто-режиме (winner_for_move = 'auto_choice')."""
    if moves_sheet is None:
        return
    rows = moves_sheet.get_all_records()
    to_delete = []
    for idx, r in enumerate(rows, start=2):
        if (
            r.get("game_id") == game_id
            and r.get("winner_for_move") == "auto_choice"
            and str(r.get("player1_id")) == str(tg_id)
        ):
            to_delete.append(idx)
    for d in reversed(to_delete):
        moves_sheet.delete_rows(d)
    moves_sheet.append_row(
        [
            game_id,
            0,
            str(tg_id),
            move,
            "",
            "",
            "auto_choice",
            datetime.now().isoformat(timespec="seconds"),
        ]
    )
    log_event(tg_id, "auto_choice", f"{game_id}:{move}")

def get_player_ids():
    """Возвращает (id Руси, id Никиты)"""
    rusya_id = None
    nikita_id = None
    for r in get_users_records():
        if r.get("name") == "Руся":
            rusya_id = r.get("user_id")
        elif r.get("name") == "Никита":
            nikita_id = r.get("user_id")
    return rusya_id, nikita_id

def get_player_chat_ids():
    """Возвращает (chat_id Руси, chat_id Никиты)."""
    rusya_chat = None
    nikita_chat = None
    for r in get_users_records():
        if r.get("name") == "Руся":
            rusya_chat = r.get("chat_id")
        elif r.get("name") == "Никита":
            nikita_chat = r.get("chat_id")
    return rusya_chat, nikita_chat

async def broadcast_final_result(winner_name, loser_name, moves_count, mode, app: Application):
    """Отправляем обоим финальное сообщение с рандомным приколом."""
    jokes = [
        f"{winner_name} сегодня доминирует и унижает!",
        f"Сила {winner_name} сегодня вне конкуренции!",
        f"{loser_name}, может в следующий раз?",
        f"{winner_name} раздавил соперника как жвачку!",
        f"{loser_name}, не расстраивайся — бывает и хуже 😉",
    ]
    text = (
        f"Сегодня {today_human()} на переднем сидении едет {winner_name}! 🚗💨\n"
        f"Так что сорян, {loser_name}, ты выдавливаешь двери на заднем сидении.\n\n"
        f"Режим игры: {mode}, ходов: {moves_count}.\n"
        f"⚡ {random.choice(jokes)}"
    )
    rusya_chat, nikita_chat = get_player_chat_ids()
    for chat_id in [rusya_chat, nikita_chat]:
        if chat_id:
            try:
                await app.bot.send_message(int(chat_id), text)
            except Exception:
                pass

def process_daily_auto_game(app: Application, loop: asyncio.AbstractEventLoop):
    """
    Обработка авто-игры (вызов /daily_check внешним кроном).
    """
    gi, g = get_today_game()
    if not g:
        return {"status": "no_game_today"}
    if g.get("mode") != "auto":
        return {"status": "mode_not_auto"}
    if str(g.get("finished")).upper() == "TRUE":
        return {"status": "already_finished"}

    gid = g.get("game_id")
    rows = moves_sheet.get_all_records()
    choices = {}
    for r in rows:
        if r.get("game_id") == gid and r.get("winner_for_move") == "auto_choice":
            choices[str(r.get("player1_id"))] = r

    if len(choices) < 2:
        return {"status": "not_enough_players"}

    ids = list(choices.keys())[:2]
    c1 = choices[ids[0]]
    c2 = choices[ids[1]]
    m1 = c1.get("player1_move")
    m2 = c2.get("player1_move")

    w = determine_winner(m1, m2)

    all_moves = moves_sheet.get_all_records()
    move_no = (
        sum(
            1
            for r in all_moves
            if r.get("game_id") == gid
            and r.get("winner_for_move") in ("player1", "player2", "tie")
        )
        + 1
    )

    moves_sheet.append_row(
        [
            gid,
            move_no,
            c1.get("player1_id"),
            m1,
            c2.get("player2_id") or c2.get("player1_id"),
            m2,
            w,
            datetime.now().isoformat(timespec="seconds"),
        ]
    )

    if w == "tie":
        games_sheet.update_cell(gi, 5, move_no)
        games_sheet.update_cell(gi, 4, "draw_pending")
        log_event("SYSTEM", "auto_tie", f"{gid} move {move_no}")
        return {"status": "tie", "move_no": move_no}

    rusya_id, nikita_id = get_player_ids()
    # определяем, кто победил по id
    if str(c1.get("player1_id")) == str(rusya_id):
        winner_name = "Руся" if w == "player1" else "Никита"
    else:
        winner_name = "Никита" if w == "player1" else "Руся"
    loser_name = "Никита" if winner_name == "Руся" else "Руся"

    row_values = [gid, today_iso(), "auto", winner_name, move_no, "TRUE"]
    games_sheet.update(f"A{gi}:F{gi}", [row_values])
    log_event("SYSTEM", "auto_finish", f"{gid} winner={winner_name} moves={move_no}")

    # отправляем финал обоим
    asyncio.run_coroutine_threadsafe(
        broadcast_final_result(winner_name, loser_name, move_no, "auto", app), loop
    )
    return {"status": "finished", "winner": winner_name, "move_no": move_no}

# ------- состояние для ручного режима -------

manual_state = {
    "game_id": None,
    "move_no": 0,
    "p1_move": None,
    "p2_move": None,
}

def start_manual_input():
    """Подготовка к вводу ручной партии."""
    gi, g = get_today_game()
    if not g or g.get("mode") != "manual":
        return False, "На сегодня нет игры в режиме manual."
    if str(g.get("finished")).upper() == "TRUE":
        return False, "Игра на сегодня уже завершена."
    gid = g.get("game_id")
    all_moves = moves_sheet.get_all_records()
    move_no = (
        sum(
            1
            for r in all_moves
            if r.get("game_id") == gid
            and r.get("winner_for_move") in ("player1", "player2", "tie")
        )
        + 1
    )
    manual_state["game_id"] = gid
    manual_state["move_no"] = move_no
    manual_state["p1_move"] = None
    manual_state["p2_move"] = None
    return True, ""

async def manual_process_if_both_moves(app: Application):
    """Когда оба хода введены вручную — считаем и обновляем всё."""
    gid = manual_state["game_id"]
    move_no = manual_state["move_no"]
    m1 = manual_state["p1_move"]
    m2 = manual_state["p2_move"]
    rusya_id, nikita_id = get_player_ids()
    if not rusya_id or not nikita_id:
        return False, "Не найдены оба игрока."

    w = determine_winner(m1, m2)

    moves_sheet.append_row(
        [
            gid,
            move_no,
            rusya_id,
            m1,
            nikita_id,
            m2,
            w,
            datetime.now().isoformat(timespec="seconds"),
        ]
    )

    gi, g = get_today_game()
    if not g:
        return False, "Игра не найдена."

    if w == "tie":
        games_sheet.update_cell(gi, 5, move_no)
        games_sheet.update_cell(gi, 4, "draw_pending")
        log_event("SYSTEM", "manual_tie", f"{gid} move {move_no}")
        manual_state["move_no"] += 1
        manual_state["p1_move"] = None
        manual_state["p2_move"] = None
        return True, "tie"

    winner_name = "Руся" if w == "player1" else "Никита"
    loser_name = "Никита" if winner_name == "Руся" else "Руся"

    row_values = [gid, today_iso(), "manual", winner_name, move_no, "TRUE"]
    games_sheet.update(f"A{gi}:F{gi}", [row_values])
    log_event("SYSTEM", "manual_finish", f"{gid} winner={winner_name} moves={move_no}")

    await broadcast_final_result(winner_name, loser_name, move_no, "manual", app)
    return True, winner_name

# ============================
# 4. Настройка Telegram-бота
# ============================

application = Application.builder().token(TG_BOT_TOKEN or "TEST").build()

def main_menu_keyboard(user_registered, game):
    """Главное меню, зависящее от состояния."""
    buttons = []
    if not user_registered:
        buttons.append([InlineKeyboardButton("Зарегистрироваться", callback_data="register")])
    else:
        buttons.append([InlineKeyboardButton("Выбрать режим", callback_data="choose_mode")])
        if game:
            mode = game.get("mode")
            if mode == "auto":
                buttons.append([InlineKeyboardButton("Сделать ход (авто)", callback_data="auto_move")])
            elif mode == "manual":
                buttons.append([InlineKeyboardButton("Ввести результат (manual)", callback_data="manual_start")])
    buttons.append([InlineKeyboardButton("Статистика", callback_data="stats")])
    return InlineKeyboardMarkup(buttons)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start: приветствие и меню."""
    user = update.effective_user
    tg_id = user.id
    chat_id = update.effective_chat.id
    u = find_user(tg_id)
    gi, g = get_today_game()

    text = (
        "Здарова, пацаны!\n\n"
        f"Сегодня {datetime.now().strftime('%d.%m.%Y %H:%M')} двое взрослых мужчин "
        "будут соперничать в жестокой битве за переднее сиденье в корпоративной тачке.\n\n"
        "Если вы не готовы или очкуете по определённым причинам — мы вас поймём, "
        "всегда можно отдать переднее сиденье без боя 😎\n"
    )
    if u:
        text += f"\nТы уже зарегистрирован как {u.get('name')}."
    else:
        text += "\nСначала зарегистрируйся."

    await update.message.reply_text(
        text,
        reply_markup=main_menu_keyboard(user_registered=bool(u), game=g),
    )
    log_event(tg_id, "cmd_start", f"chat_id={chat_id}")

application.add_handler(CommandHandler("start", cmd_start))

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Простая админ-команда: последние 10 событий в логе."""
    logs = logs_sheet.get_all_records()[-10:]
    lines = []
    for r in logs:
        lines.append(f"{r['timestamp']} | {r['user_id']} | {r['action']} | {r['details']}")
    text = "Последние события:\n" + "\n".join(lines) if lines else "Лог пуст."
    await update.message.reply_text(text)

application.add_handler(CommandHandler("admin", cmd_admin))

def mode_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Ручной режим", callback_data="mode_manual")],
            [InlineKeyboardButton("Автоматический режим", callback_data="mode_auto")],
        ]
    )

def manual_move_keyboard(player: str):
    prefix = "man_p1" if player == "p1" else "man_p2"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Камень", callback_data=f"{prefix}_rock"),
                InlineKeyboardButton("Ножницы", callback_data=f"{prefix}_scissors"),
                InlineKeyboardButton("Бумага", callback_data=f"{prefix}_paper"),
            ]
        ]
    )

def auto_move_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Камень", callback_data="auto_rock"),
                InlineKeyboardButton("Ножницы", callback_data="auto_scissors"),
                InlineKeyboardButton("Бумага", callback_data="auto_paper"),
            ]
        ]
    )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка всех нажатий кнопок."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    tg_id = user.id
    chat_id = query.message.chat_id
    u = find_user(tg_id)

    # --- Регистрация шаг 1: "Зарегистрироваться" ---
    if data == "register":
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Я — Руся", callback_data="reg_rusya"),
                    InlineKeyboardButton("Я — Никита", callback_data="reg_nikita"),
                ]
            ]
        )
        await query.edit_message_text("Кто ты сегодня?", reply_markup=kb)
        return

    # --- Регистрация выбор роли ---
    if data in ("reg_rusya", "reg_nikita"):
        name = "Руся" if data == "reg_rusya" else "Никита"
        created, status = register_user(tg_id, chat_id, name)
        if status == "already":
            msg = f"Ты уже был зарегистрирован как {name}."
        else:
            msg = f"Ты зарегистрирован как {name}."
        await query.edit_message_text(msg)

        # Пишем второму игроку
        other = get_other_user(tg_id)
        if other and other.get("chat_id"):
            try:
                await context.bot.send_message(
                    int(other["chat_id"]), f"{name} зарегистрировался на сегодняшнюю битву."
                )
            except Exception:
                pass

        await context.bot.send_message(
            chat_id,
            "Теперь выберите режим на сегодня.",
            reply_markup=mode_keyboard(),
        )
        return

    # --- Выбор режима (кнопка "Выбрать режим") ---
    if data == "choose_mode":
        await query.edit_message_text(
            "Выберите режим на сегодня:",
            reply_markup=mode_keyboard(),
        )
        return

    # --- Выбор режима manual / auto ---
    if data in ("mode_manual", "mode_auto"):
        mode = "manual" if data == "mode_manual" else "auto"
        status, gid = record_mode_vote(tg_id, mode)
        name = u.get("name") if u else "Игрок"

        if status == "waiting":
            text = f"Твой выбор: {mode}. Ждём второго игрока."
        elif status == "started":
            text = f"Режим {mode} согласован. Игра на сегодня создана."
        elif status == "already":
            text = "Игра на сегодня уже существует."
        else:
            text = "Ошибка при выборе режима."

        await query.edit_message_text(text)

        # уведомляем второго игрока
        other = get_other_user(tg_id)
        if other and other.get("chat_id"):
            try:
                await context.bot.send_message(
                    int(other["chat_id"]),
                    f"{name} выбрал режим: {mode}. Проверь свой выбор с помощью /start.",
                )
            except Exception:
                pass
        return

    # --- Авто-режим: кнопка "Сделать ход" ---
    if data == "auto_move":
        gi, g = get_today_game()
        if not g or g.get("mode") != "auto":
            await query.edit_message_text(
                "Авто-игра на сегодня ещё не создана. Сначала оба выберите режим."
            )
            return
        await query.edit_message_text(
            "Выбери свой ход (он останется скрытым до подсчёта результата):",
            reply_markup=auto_move_keyboard(),
        )
        return

    # --- Авто-режим: конкретный ход ---
    if data.startswith("auto_"):
        move = data.split("_")[1]
        gi, g = get_today_game()
        if not g or g.get("mode") != "auto":
            await query.edit_message_text("Авто-игра на сегодня не создана.")
            return
        save_auto_choice(g.get("game_id"), tg_id, move)
        await query.edit_message_text("Твой ход сохранён. Ждём второго игрока.")

        other = get_other_user(tg_id)
        if other and other.get("chat_id"):
            try:
                await context.bot.send_message(
                    int(other["chat_id"]),
                    f"{(u or {}).get('name','Игрок')} сделал свой ход в авто-режиме.",
                )
            except Exception:
                pass
        return

    # --- Ручной режим: старт ввода ---
    if data == "manual_start":
        ok, msg = start_manual_input()
        if not ok:
            await query.edit_message_text(msg)
            return
        await query.edit_message_text(
            f"Ручной режим. Ход №{manual_state['move_no']}.\n"
            "Сначала выберите ход Руси:",
            reply_markup=manual_move_keyboard("p1"),
        )
        return

    # --- Ручной режим: ход Руси ---
    if data.startswith("man_p1_"):
        move = data.split("_")[2]
        manual_state["p1_move"] = move
        await query.edit_message_text(
            f"Ход №{manual_state['move_no']}.\n"
            f"Ход Руси: {move}.\n"
            "Теперь выберите ход Никиты:",
            reply_markup=manual_move_keyboard("p2"),
        )
        return

    # --- Ручной режим: ход Никиты ---
    if data.startswith("man_p2_"):
        move = data.split("_")[2]
        manual_state["p2_move"] = move
        ok, result = await manual_process_if_both_moves(application)
        if not ok:
            await query.edit_message_text(result)
            return
        if result == "tie":
            await query.edit_message_text(
                f"Ничья на ходу. Начинаем следующий ход №{manual_state['move_no']}.\n"
                "Сначала выберите ход Руси:",
                reply_markup=manual_move_keyboard("p1"),
            )
        else:
            await query.edit_message_text(
                f"Партия завершена. Победитель: {result}."
            )
        return

    # --- Статистика ---
    if data == "stats":
        games = games_sheet.get_all_records()
        total = len(games)
        finished = sum(
            1 for g in games if str(g.get("finished")).upper() == "TRUE"
        )
        moves = moves_sheet.get_all_records()
        users = get_users_records()
        lines = [
            f"Всего игр: {total}",
            f"Завершено: {finished}",
            "",
        ]
        for urec in users:
            uid = str(urec.get("user_id"))
            name = urec.get("name")
            moves_count = 0
            wins = 0
            rock = paper = scissors = 0
            for m in moves:
                if m.get("winner_for_move") not in ("player1", "player2"):
                    continue
                if str(m.get("player1_id")) == uid:
                    moves_count += 1
                    if m.get("player1_move") == "rock":
                        rock += 1
                    elif m.get("player1_move") == "paper":
                        paper += 1
                    elif m.get("player1_move") == "scissors":
                        scissors += 1
                    if m.get("winner_for_move") == "player1":
                        wins += 1
                if str(m.get("player2_id")) == uid:
                    moves_count += 1
                    if m.get("player2_move") == "rock":
                        rock += 1
                    elif m.get("player2_move") == "paper":
                        paper += 1
                    elif m.get("player2_move") == "scissors":
                        scissors += 1
                    if m.get("winner_for_move") == "player2":
                        wins += 1
            lines.append(
                f"{name}: ходов={moves_count}, побед={wins}, "
                f"камень={rock}, бумага={paper}, ножницы={scissors}"
            )
        await query.edit_message_text("\n".join(lines))
        return

# регистрируем обработчик кнопок
application.add_handler(CallbackQueryHandler(on_callback))

# ============================
# 5. Global event loop + Flask
# ============================

loop = asyncio.new_event_loop()

def run_bot():
    asyncio.set_event_loop(loop)
    loop.run_until_complete(application.initialize())
    loop.run_until_complete(application.start())
    loop.run_forever()

bot_thread = threading.Thread(target=run_bot, daemon=True)
bot_thread.start()

app = Flask(__name__)

@app.route("/")
def index():
    return "RPS bot running"

@app.route("/webhook", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run_coroutine_threadsafe(application.process_update(update), loop)
    return "ok"

@app.route("/daily_check", methods=["GET", "POST"])
def daily_check():
    result = process_daily_auto_game(application, loop)
    return jsonify(result)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
