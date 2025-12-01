import os
import json
import base64
import datetime
import time
import asyncio
import threading

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
# 1. ENV VARS
# ============================================================

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME")
SERVICE_JSON_B64 = os.getenv("GSPREAD_SERVICE_ACCOUNT_JSON_B64")

if not TG_BOT_TOKEN:
    raise RuntimeError("Missing TG_BOT_TOKEN")
if not SPREADSHEET_NAME:
    raise RuntimeError("Missing SPREADSHEET_NAME")
if not SERVICE_JSON_B64:
    raise RuntimeError("Missing GSPREAD_SERVICE_ACCOUNT_JSON_B64")

# ============================================================
# 2. GOOGLE SHEETS
# ============================================================

sa_info = json.loads(base64.b64decode(SERVICE_JSON_B64).decode("utf-8"))
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
credentials = Credentials.from_service_account_info(sa_info, scopes=scopes)
gc = gspread.authorize(credentials)
sh = gc.open(SPREADSHEET_NAME)


def get_or_create_worksheet(name, headers):
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

logs_sheet = get_or_create_worksheet(
    "logs",
    ["timestamp", "user_id", "action", "details"],
)

# ============================================================
# 3. HELPERS
# ============================================================

def today() -> str:
    return datetime.date.today().isoformat()


def log_event(user_id, action, details=""):
    """Запись события в лист logs."""
    logs_sheet.append_row(
        [
            datetime.datetime.now().isoformat(),
            str(user_id),
            action,
            details,
        ]
    )


def find_user(tg_id):
    for r in users_sheet.get_all_records():
        if str(r["user_id"]) == str(tg_id):
            return r
    return None


def register_user(tg_id, name):
    if find_user(tg_id):
        return False
    users_sheet.append_row([str(tg_id), name, today()])
    log_event(tg_id, "register", name)
    return True


def get_today_game():
    for idx, r in enumerate(games_sheet.get_all_records(), start=2):
        if r["date"] == today():
            return idx, r
    return None, None


def create_new_game(mode):
    gid = f"{today()}_{int(time.time())}"
    games_sheet.append_row([gid, today(), mode, "", 0, "FALSE"])
    log_event("SYSTEM", "create_game", f"{gid} mode={mode}")
    return gid


def record_mode_vote(tg_id, mode):
    """
    Возврат:
      ("waiting", None)  - ждём второго игрока
      ("started", gid)   - оба выбрали одинаковый режим, игра создана
      ("already", gid)   - игра уже есть на сегодня
    """
    date = today()
    rows = mode_votes_sheet.get_all_records()

    updated = False
    for i, r in enumerate(rows, start=2):
        if r["date"] == date and str(r["user_id"]) == str(tg_id):
            mode_votes_sheet.update_cell(i, 3, mode)
            updated = True
            break
    if not updated:
        mode_votes_sheet.append_row([date, str(tg_id), mode])

    log_event(tg_id, "mode_vote", f"{mode}")

    gi, game = get_today_game()
    if game:
        return "already", game["game_id"]

    # проверяем голоса
    votes = mode_votes_sheet.get_all_records()
    same = {}
    for v in votes:
        if v["date"] != date:
            continue
        same.setdefault(v["mode"], set()).add(str(v["user_id"]))

    for m, users in same.items():
        if len(users) >= 2:
            gid = create_new_game(m)
            return "started", gid

    return "waiting", None


def determine_winner(a, b):
    beats = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
    if a == b:
        return "tie"
    return "player1" if beats[a] == b else "player2"


def save_auto_choice(game_id, tg_id, move):
    """Сохраняем ход игрока в авто-режиме (winner_for_move='auto_choice')."""
    rows = moves_sheet.get_all_records()
    to_del = []
    for idx, r in enumerate(rows, start=2):
        if (
            r["game_id"] == game_id
            and r["winner_for_move"] == "auto_choice"
            and str(r["player1_id"]) == str(tg_id)
        ):
            to_del.append(idx)
    for d in reversed(to_del):
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
            datetime.datetime.now().isoformat(),
        ]
    )
    log_event(tg_id, "auto_choice", f"{game_id} -> {move}")


def daily_auto():
    """Обработка авто-игры (вызывается /daily_check)."""
    gi, game = get_today_game()
    if not game:
        return {"status": "no_game"}

    if game["mode"] != "auto":
        return {"status": "not_auto"}

    if str(game["finished"]).upper() == "TRUE":
        return {"status": "already"}

    gid = game["game_id"]
    rows = moves_sheet.get_all_records()
    choices = {}
    for r in rows:
        if r["game_id"] == gid and r["winner_for_move"] == "auto_choice":
            choices[str(r["player1_id"])] = r

    if len(choices) < 2:
        return {"status": "not_enough"}

    ids = list(choices.keys())[:2]
    c1 = choices[ids[0]]
    c2 = choices[ids[1]]
    m1 = c1["player1_move"]
    m2 = c2["player1_move"]

    w = determine_winner(m1, m2)

    all_moves = moves_sheet.get_all_records()
    move_no = (
        sum(
            1
            for r in all_moves
            if r["game_id"] == gid and r["winner_for_move"] in ("player1", "player2", "tie")
        )
        + 1
    )

    moves_sheet.append_row(
        [
            gid,
            move_no,
            c1["player1_id"],
            m1,
            c2["player1_id"],
            m2,
            w,
            datetime.datetime.now().isoformat(),
        ]
    )

    if w == "tie":
        games_sheet.update_cell(gi, 5, move_no)  # moves_count
        games_sheet.update_cell(gi, 4, "draw_pending")
        log_event("SYSTEM", "auto_tie", f"{gid} move {move_no}")
        return {"status": "tie", "move_no": move_no}

    winner_id = c1["player1_id"] if w == "player1" else c2["player1_id"]
    users = users_sheet.get_all_records()
    winner_name = next(
        (u["name"] for u in users if str(u["user_id"]) == str(winner_id)), "Unknown"
    )

    row_values = [gid, today(), "auto", winner_name, move_no, "TRUE"]
    games_sheet.update(f"A{gi}:F{gi}", [row_values])
    log_event("SYSTEM", "auto_finish", f"{gid} winner={winner_name} moves={move_no}")

    return {"status": "finished", "winner": winner_name, "move_no": move_no}


# ============================================================
# 4. MANUAL MODE STATE (в памяти)
# ============================================================

manual_state = {
    "game_id": None,
    "move_no": 0,
    "p1_move": None,
    "p2_move": None,
}

# player1 — тот, у кого name == "Руся"
# player2 — тот, у кого name == "Никита"


def get_player_ids():
    """Возвращает (rusya_id, nikita_id) или (None, None)."""
    rusya_id = None
    nikita_id = None
    for r in users_sheet.get_all_records():
        if r["name"] == "Руся":
            rusya_id = r["user_id"]
        if r["name"] == "Никита":
            nikita_id = r["user_id"]
    return rusya_id, nikita_id


def start_manual_input():
    """Подготовка состояния для ручного ввода."""
    gi, g = get_today_game()
    if not g or g["mode"] != "manual":
        return False, "На сегодня нет игры в режиме manual."

    if str(g["finished"]).upper() == "TRUE":
        return False, "Игра на сегодня уже завершена."

    manual_state["game_id"] = g["game_id"]
    manual_state["move_no"] = (
        sum(
            1
            for r in moves_sheet.get_all_records()
            if r["game_id"] == g["game_id"]
            and r["winner_for_move"] in ("player1", "player2", "tie")
        )
        + 1
    )
    manual_state["p1_move"] = None
    manual_state["p2_move"] = None
    return True, ""


def manual_process_if_both_moves():
    """Когда есть оба хода, считаем результат, пишем в таблицу, обновляем игру."""
    gid = manual_state["game_id"]
    move_no = manual_state["move_no"]
    p1_move = manual_state["p1_move"]
    p2_move = manual_state["p2_move"]

    rusya_id, nikita_id = get_player_ids()
    if not rusya_id or not nikita_id:
        return False, "Не найдены оба игрока (Руся и Никита)."

    w = determine_winner(p1_move, p2_move)

    moves_sheet.append_row(
        [
            gid,
            move_no,
            rusya_id,
            p1_move,
            nikita_id,
            p2_move,
            w,
            datetime.datetime.now().isoformat(),
        ]
    )

    gi, g = get_today_game()
    if not g:
        return False, "Не найдена сегодняшняя игра."

    if w == "tie":
        games_sheet.update_cell(gi, 5, move_no)
        games_sheet.update_cell(gi, 4, "draw_pending")
        log_event("SYSTEM", "manual_tie", f"{gid} move {move_no}")
        # подготавливаем следующий ход
        manual_state["move_no"] += 1
        manual_state["p1_move"] = None
        manual_state["p2_move"] = None
        return True, "tie"

    winner_id = rusya_id if w == "player1" else nikita_id
    users = users_sheet.get_all_records()
    winner_name = next(
        (u["name"] for u in users if str(u["user_id"]) == str(winner_id)), "Unknown"
    )

    row_values = [gid, today(), "manual", winner_name, move_no, "TRUE"]
    games_sheet.update(f"A{gi}:F{gi}", [row_values])
    log_event("SYSTEM", "manual_finish", f"{gid} winner={winner_name} moves={move_no}")
    return True, winner_name


# ============================================================
# 5. TELEGRAM BOT
# ============================================================

application = Application.builder().token(TG_BOT_TOKEN).build()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = find_user(user.id)
    hello = f"Привет, {u['name']}!" if u else "Привет! Ты ещё не зарегистрирован."

    kb = [
        [
            InlineKeyboardButton("Я — Руся", callback_data="reg_rusya"),
            InlineKeyboardButton("Я — Никита", callback_data="reg_nikita"),
        ],
        [InlineKeyboardButton("Выбрать режим (manual/auto)", callback_data="choose_mode")],
        [InlineKeyboardButton("Сделать ход (auto)", callback_data="auto_move")],
        [InlineKeyboardButton("Ввести результат (manual)", callback_data="manual_start")],
        [InlineKeyboardButton("Статистика", callback_data="stats")],
    ]
    await update.message.reply_text(hello, reply_markup=InlineKeyboardMarkup(kb))


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Простая админ-панель: покажем 10 последних логов."""
    logs = logs_sheet.get_all_records()
    last = logs[-10:]
    lines = []
    for r in last:
        lines.append(f"{r['timestamp']} | {r['user_id']} | {r['action']} | {r['details']}")
    text = "Последние события:\n" + "\n".join(lines) if lines else "Лог пуст."
    await update.message.reply_text(text)


async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    # ===== Регистрация =====
    if data == "reg_rusya":
        ok = register_user(uid, "Руся")
        await q.edit_message_text("Ты зарегистрирован как Руся." if ok else "Ты уже зарегистрирован.")
        return
    if data == "reg_nikita":
        ok = register_user(uid, "Никита")
        await q.edit_message_text("Ты зарегистрирован как Никита." if ok else "Ты уже зарегистрирован.")
        return

    # ===== Выбор режима =====
    if data == "choose_mode":
        kb = [
            [InlineKeyboardButton("Ручной (manual)", callback_data="mode_manual")],
            [InlineKeyboardButton("Авто (auto)", callback_data="mode_auto")],
        ]
        await q.edit_message_text(
            "Выберите режим на сегодня.\n"
            "Режим активируется, когда оба игрока выберут один и тот же вариант.",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data in ("mode_manual", "mode_auto"):
        mode = "manual" if data == "mode_manual" else "auto"
        status, gid = record_mode_vote(uid, mode)
        if status == "waiting":
            await q.edit_message_text(f"Твой выбор: {mode}. Ждём второго игрока.")
        elif status == "started":
            await q.edit_message_text(f"Игра создана на сегодня. Режим: {mode}.")
        else:
            await q.edit_message_text("Игра на сегодня уже существует.")
        return

    # ===== Авто-ход =====
    if data == "auto_move":
        gi, g = get_today_game()
        if not g or g["mode"] != "auto":
            await q.edit_message_text(
                "На сегодня ещё не создана авто-игра.\n"
                "Сначала оба игрока должны выбрать режим 'Авто (auto)'."
            )
            return
        kb = [
            [
                InlineKeyboardButton("Камень", callback_data="auto_rock"),
                InlineKeyboardButton("Ножницы", callback_data="auto_scissors"),
                InlineKeyboardButton("Бумага", callback_data="auto_paper"),
            ]
        ]
        await q.edit_message_text("Выберите свой ход:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("auto_"):
        move_map = {
            "auto_rock": "rock",
            "auto_scissors": "scissors",
            "auto_paper": "paper",
        }
        move = move_map[data]
        gi, g = get_today_game()
        if not g or g["mode"] != "auto":
            await q.edit_message_text("Авто-игра на сегодня не создана.")
            return
        save_auto_choice(g["game_id"], uid, move)
        await q.edit_message_text("Ход сохранён. Результат будет подведён при ежедневной проверке.")
        return

    # ===== Ручной режим: старт ввода =====
    if data == "manual_start":
        ok, msg = start_manual_input()
        if not ok:
            await q.edit_message_text(msg)
            return
        # начинаем с выбора хода Руси
        kb = [
            [
                InlineKeyboardButton("Камень", callback_data="man_p1_rock"),
                InlineKeyboardButton("Ножницы", callback_data="man_p1_scissors"),
                InlineKeyboardButton("Бумага", callback_data="man_p1_paper"),
            ]
        ]
        await q.edit_message_text(
            f"Ручной режим.\nХод №{manual_state['move_no']}.\n"
            f"Сначала выберите ход Руси:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    # ===== Ручной режим: ход Руси =====
    if data.startswith("man_p1_"):
        move = data.split("_")[2]  # rock / paper / scissors
        manual_state["p1_move"] = move
        kb = [
            [
                InlineKeyboardButton("Камень", callback_data="man_p2_rock"),
                InlineKeyboardButton("Ножницы", callback_data="man_p2_scissors"),
                InlineKeyboardButton("Бумага", callback_data="man_p2_paper"),
            ]
        ]
        await q.edit_message_text(
            f"Ход №{manual_state['move_no']}.\n"
            f"Ход Руси: {move}.\n"
            f"Теперь выберите ход Никиты:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    # ===== Ручной режим: ход Никиты =====
    if data.startswith("man_p2_"):
        move = data.split("_")[2]
        manual_state["p2_move"] = move

        ok, result = manual_process_if_both_moves()
        if not ok:
            await q.edit_message_text(result)
            return

        if result == "tie":
            # ничья, начинаем следующий ход
            kb = [
                [
                    InlineKeyboardButton("Камень", callback_data="man_p1_rock"),
                    InlineKeyboardButton("Ножницы", callback_data="man_p1_scissors"),
                    InlineKeyboardButton("Бумага", callback_data="man_p1_paper"),
                ]
            ]
            await q.edit_message_text(
                f"Ничья на ходу №{manual_state['move_no'] - 1}.\n"
                f"Начинаем следующий ход №{manual_state['move_no']}.\n"
                f"Выберите ход Руси:",
                reply_markup=InlineKeyboardMarkup(kb),
            )
        else:
            await q.edit_message_text(
                f"Партия завершена.\nПобедитель: {result}.\n"
                f"Всего ходов: {manual_state['move_no']}."
            )
        return

    # ===== Статистика =====
    if data == "stats":
        games = games_sheet.get_all_records()
        total = len(games)
        finished = sum(1 for g in games if str(g["finished"]).upper() == "TRUE")

        # статистика по игрокам
        users = users_sheet.get_all_records()
        moves = moves_sheet.get_all_records()

        stats_lines = [f"Всего игр: {total}", f"Завершено: {finished}", ""]

        for u in users:
            uid_str = str(u["user_id"])
            name = u["name"]
            moves_count = 0
            rock = paper = scissors = 0
            wins = 0

            for m in moves:
                if m["winner_for_move"] not in ("player1", "player2"):
                    continue

                # он как player1
                if str(m["player1_id"]) == uid_str:
                    moves_count += 1
                    if m["player1_move"] == "rock":
                        rock += 1
                    elif m["player1_move"] == "paper":
                        paper += 1
                    elif m["player1_move"] == "scissors":
                        scissors += 1
                    if m["winner_for_move"] == "player1":
                        wins += 1

                # он как player2
                if str(m["player2_id"]) == uid_str:
                    moves_count += 1
                    if m["player2_move"] == "rock":
                        rock += 1
                    elif m["player2_move"] == "paper":
                        paper += 1
                    elif m["player2_move"] == "scissors":
                        scissors += 1
                    if m["winner_for_move"] == "player2":
                        wins += 1

            stats_lines.append(
                f"{name}: ходов={moves_count}, побед={wins}, "
                f"камень={rock}, бумага={paper}, ножницы={scissors}"
            )

        await q.edit_message_text("\n".join(stats_lines))
        return


# handlers
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("admin", cmd_admin))
application.add_handler(CallbackQueryHandler(cb_handler))

# ============================================================
# 6. GLOBAL EVENT LOOP & THREAD
# ============================================================

loop = asyncio.new_event_loop()


def run_bot():
    asyncio.set_event_loop(loop)
    loop.run_until_complete(application.initialize())
    loop.run_until_complete(application.start())
    loop.run_forever()


threading.Thread(target=run_bot, daemon=True).start()

# ============================================================
# 7. FLASK APP (WEBHOOK + DAILY_CHECK)
# ============================================================

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
    res = daily_auto()
    return jsonify(res)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
