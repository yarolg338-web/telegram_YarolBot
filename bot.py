import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ BacBo Bot ONLINE 24/7 🚀\nFuncionando en Render GRATIS."
    )

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN no configurado")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    print("✅ Bot iniciado correctamente")

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
