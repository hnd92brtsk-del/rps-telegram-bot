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
# 1. Environment variables
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
# 2. Google Sheets setup
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
        ws = sh.add_worksheet(title=name, rows=200, cols=len(headers))
        ws.append_row(headers)
        return ws

    first = ws.row_values(1)
    if not first:
        ws.append_row(headers)
    return ws


users_sheet = get_or_create_worksheet("users",
    ["user_id", "name", "reg_date"])

mode_votes_sheet = get_or_create_worksheet("mode_votes",
    ["date", "user_id", "mode"])

games_sheet = get_or_create_worksheet("games",
    ["game_id", "date", "mode", "winner", "moves_count", "finished"])

moves_sheet = get_or_create_worksheet("moves",
    ["game_id", "move_no", "player1_id", "player1_move",
     "player2_id", "player2_move", "winner_for_move", "timestamp"])


def today():
    return datetime.date.today().isoformat()


def find_user(tg_id):
    for r in users_sheet.get_all_records():
        if str(r["user_id"]) == str(tg_id):
            return r
    return None


def register_user(tg_id, name):
    if find_user(tg_id):
        return False
    users_sheet.append_row([str(tg_id), name, today()])
    return True


def get_today_game():
    for idx, r in enumerate(games_sheet.get_all_records(), start=2):
        if r["date"] == today():
            return idx, r
    return None, None


def create_new_game(mode):
    gid = f"{today()}_{int(time.time())}"
    games_sheet.append_row([gid, today(), mode, "", 0, "FALSE"])
    return gid


def record_mode_vote(tg_id, mode):
    date = today()
    records = mode_votes_sheet.get_all_records()

    updated = False
    for i, r in enumerate(records, start=2):
        if r["date"] == date and str(r["user_id"]) == str(tg_id):
            mode_votes_sheet.update_cell(i, 3, mode)
            updated = True
            break

    if not updated:
        mode_votes_sheet.append_row([date, str(tg_id), mode])

    # already game?
    i, g = get_today_game()
    if g:
        return "already", g["game_id"]

    # check 2 players same mode
    votes = mode_votes_sheet.get_all_records()
    same = {}
    for v in votes:
        if v["date"] != date:
            continue
        same.setdefault(v["mode"], set()).add(str(v["user_id"]))

    for m, us in same.items():
        if len(us) >= 2:
            gid = create_new_game(m)
            return "started", gid

    return "waiting", None


def determine_winner(a, b):
    beats = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
    if a == b:
        return "tie"
    return "player1" if beats[a] == b else "player2"


def save_auto_choice(game_id, tg_id, move):
    rows = moves_sheet.get_all_records()

    # delete old auto choice
    to_delete = []
    for idx, r in enumerate(rows, start=2):
        if (
            r["game_id"] == game_id
            and r["winner_for_move"] == "auto_choice"
            and str(r["player1_id"]) == str(tg_id)
        ):
            to_delete.append(idx)

    for d in reversed(to_delete):
        moves_sheet.delete_rows(d)

    moves_sheet.append_row([
        game_id, 0, str(tg_id), move, "", "", "auto_choice",
        datetime.datetime.now().isoformat()
    ])


def daily_auto():
    idx, g = get_today_game()
    if not g:
        return {"status": "no_game"}

    if g["mode"] != "auto":
        return {"status": "not_auto"}
    if str(g["finished"]).upper() == "TRUE":
        return {"status": "already"}

    gid = g["game_id"]
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

    w = determine_winner(c1["player1_move"], c2["player1_move"])

    # get move_no
    all_moves = moves_sheet.get_all_records()
    move_no = sum(1 for r in all_moves if r["game_id"] == gid and r["winner_for_move"] in ("player1","player2","tie")) + 1

    moves_sheet.append_row([
        gid, move_no,
        c1["player1_id"], c1["player1_move"],
        c2["player1_id"], c2["player1_move"],
        w, datetime.datetime.now().isoformat()
    ])

    if w == "tie":
        games_sheet.update_cell(idx, 5, move_no)
        games_sheet.update_cell(idx, 4, "draw_pending")
        return {"status": "tie"}

    winner_id = c1["player1_id"] if w == "player1" else c2["player1_id"]
    users = users_sheet.get_all_records()
    winner_name = next((u["name"] for u in users if str(u["user_id"]) == str(winner_id)), "Winner")

    games_sheet.update_row(idx, [
        gid, today(), "auto", winner_name, move_no, "TRUE"
    ])

    return {"status": "finished", "winner": winner_name}


# ============================================================
# Telegram bot setup (NO event loop closing!)
# ============================================================

application = Application.builder().token(TG_BOT_TOKEN).build()

# --- Handlers ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = find_user(user.id)

    hello = f"Привет, {u['name']}!" if u else "Привет! Ты не зарегистрирован."

    kb = [
        [
            InlineKeyboardButton("Я — Руся", callback_data="reg_rusya"),
            InlineKeyboardButton("Я — Никита", callback_data="reg_nikita")
        ],
        [InlineKeyboardButton("Выбрать режим", callback_data="choose_mode")],
        [InlineKeyboardButton("Сделать ход (auto)", callback_data="auto_move")],
        [InlineKeyboardButton("Статистика", callback_data="stats")],
    ]

    await update.message.reply_text(hello, reply_markup=InlineKeyboardMarkup(kb))


async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    uid = q.from_user.id
    data = q.data

    # Registration
    if data == "reg_rusya":
        ok = register_user(uid, "Руся")
        await q.edit_message_text("Регистрация выполнена." if ok else "Уже зарегистрирован.")
        return

    if data == "reg_nikita":
        ok = register_user(uid, "Никита")
        await q.edit_message_text("Регистрация выполнена." if ok else "Уже зарегистрирован.")
        return

    # Choose mode
    if data == "choose_mode":
        kb = [
            [InlineKeyboardButton("manual", callback_data="mode_manual")],
            [InlineKeyboardButton("auto", callback_data="mode_auto")],
        ]
        await q.edit_message_text("Выбери режим:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data in ("mode_manual", "mode_auto"):
        mode = "manual" if data == "mode_manual" else "auto"
        status, gid = record_mode_vote(uid, mode)
        if status == "waiting":
            await q.edit_message_text(f"Твой выбор: {mode}. Ждём второго игрока.")
        elif status == "started":
            await q.edit_message_text(f"Игра создана ({mode}).")
        else:
            await q.edit_message_text("Игра уже есть.")
        return

    # Auto-move choose
    if data == "auto_move":
        row, g = get_today_game()
        if not g or g["mode"] != "auto":
            await q.edit_message_text("Авто-игра ещё не создана.")
            return

        kb = [
            [
                InlineKeyboardButton("Камень", callback_data="auto_rock"),
                InlineKeyboardButton("Ножницы", callback_data="auto_scissors"),
                InlineKeyboardButton("Бумага", callback_data="auto_paper"),
            ]
        ]
        await q.edit_message_text("Выбери ход:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("auto_"):
        move = data.split("_")[1]
        row, g = get_today_game()
        if not g or g["mode"] != "auto":
            await q.edit_message_text("Авто-игра не создана.")
            return

        save_auto_choice(g["game_id"], uid, move)
        await q.edit_message_text(f"Ход сохранён ({move}).")
        return

    if data == "stats":
        games = games_sheet.get_all_records()
        total = len(games)
        finished = sum(1 for g in games if str(g["finished"]).upper() == "TRUE")
        await q.edit_message_text(f"Всего игр: {total}\nЗавершено: {finished}")
        return


application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CallbackQueryHandler(cb))

# ============================================================
# Create ONE GLOBAL EVENT LOOP forever
# ============================================================

loop = asyncio.new_event_loop()


def run_bot():
    asyncio.set_event_loop(loop)
    loop.run_until_complete(application.initialize())
    loop.run_until_complete(application.start())
    loop.run_forever()


threading.Thread(target=run_bot, daemon=True).start()

# ============================================================
# Flask server
# ============================================================

app = Flask(__name__)


@app.route("/")
def index():
    return "Bot running"


@app.route("/webhook", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run_coroutine_threadsafe(application.process_update(update), loop)
    return "ok"


@app.route("/daily_check")
def daily_check():
    return jsonify(daily_auto())


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
