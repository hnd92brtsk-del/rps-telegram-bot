import os
import asyncio
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
import gspread
import base64
import json
from google.oauth2 import service_account

# ==========================
# 1. ЗАГРУЗКА ПЕРЕМЕННЫХ
# ==========================
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME")
SERVICE_JSON_B64 = os.getenv("GSPREAD_SERVICE_ACCOUNT_JSON_B64")

# ==========================
# 2. НАСТРОЙКА GOOGLE SHEETS
# ==========================
service_info = json.loads(base64.b64decode(SERVICE_JSON_B64))
credentials = service_account.Credentials.from_service_account_info(
    service_info,
    scopes=["https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"]
)
gc = gspread.authorize(credentials)
sh = gc.open(SPREADSHEET_NAME)
ws = sh.sheet1

# ==========================
# 3. FLASK ПРИЛОЖЕНИЕ
# ==========================
app = Flask(__name__)

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{os.getenv('RENDER_EXTERNAL_URL')}{WEBHOOK_PATH}"

# ==========================
# 4. TELEGRAM BOT
# ==========================
application = Application.builder().token(TG_BOT_TOKEN).build()

# ========== START ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Регистрация", callback_data="register")],
        [InlineKeyboardButton("Выбрать режим", callback_data="choose_mode")],
        [InlineKeyboardButton("Сделать ход", callback_data="make_move")],
        [InlineKeyboardButton("Статистика", callback_data="stats")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# ========== КНОПКИ ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "register":
        await query.edit_message_text("Вы выбрали регистрацию.")
    elif query.data == "choose_mode":
        await query.edit_message_text("Выбор режима игры.")
    elif query.data == "make_move":
        await query.edit_message_text("Введите ваш ход.")
    elif query.data == "stats":
        await query.edit_message_text("Загрузка статистики...")

# ==========================
# 5. ДОБАВЛЕНИЕ ХЕНДЛЕРОВ
# ==========================
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button_handler))

# ==========================
# 6. ЗАПУСК PTB В БЭКГРАУНДЕ
# ==========================
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

loop.run_until_complete(application.initialize())
loop.run_until_complete(application.start())

# ==========================
# 7. WEBHOOK ДЛЯ TELEGRAM
# ==========================
@app.post(WEBHOOK_PATH)
def telegram_webhook():
    """Получение обновлений от Telegram"""
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.process_update(update))
    return "OK"

@app.get("/")
def root():
    return "Bot is running!"

# ==========================
# 8. ЗАПУСК FLASK (для Render)
# ==========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"Run Flask app on port {port}")
    app.run(host="0.0.0.0", port=port)
