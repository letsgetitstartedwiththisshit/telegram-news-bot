import os
import feedparser
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes, CallbackQueryHandler
import asyncio

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
VALIDATION_CHANNEL_ID = int(os.getenv("VALIDATION_CHANNEL_ID", "0"))
MAIN_CHANNEL_ID = int(os.getenv("MAIN_CHANNEL_ID", "0"))
SOURCES = [s.strip() for s in os.getenv("SOURCES", "").split(",") if s.strip()]

posted_links = set()
pending_posts = {}

async def fetch_feeds(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    for url in SOURCES:
        feed = feedparser.parse(url)
        for entry in feed.entries[:5]:
            link = entry.link
            if link in posted_links:
                continue
            posted_links.add(link)
            title = entry.title
            summary = entry.get("summary", "")
            message = f"*{title}*\n{summary}\n\n[Читати джерело]({link})"
            callback_id = link
            pending_posts[callback_id] = message
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Публікувати", callback_data=f"approve:{callback_id}"),
                    InlineKeyboardButton("❌ Відхилити", callback_data=f"reject:{callback_id}"),
                ]
            ])
            await bot.send_message(
                chat_id=VALIDATION_CHANNEL_ID,
                text=message,
                parse_mode="Markdown",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data:
        return
    if ":" not in query.data:
        return
    action, callback_id = query.data.split(":", 1)
    message_text = pending_posts.get(callback_id)
    if not message_text:
        await query.edit_message_text("Цей запис вже опрацьовано.")
        return
    if action == "approve":
        await context.bot.send_message(
            chat_id=MAIN_CHANNEL_ID,
            text=message_text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        status = "✅ Опубліковано"
    else:
        status = "❌ Відхилено"
    # Edit original message to reflect status and remove buttons
    await query.edit_message_text(
        message_text + f"\n\n{status}",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
    pending_posts.pop(callback_id, None)

def main():
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CallbackQueryHandler(handle_callback))
    # Schedule periodic feed fetching every 10 minutes
    application.job_queue.run_repeating(fetch_feeds, interval=600, first=5)
    application.run_polling()

if __name__ == "__main__":
    main()
