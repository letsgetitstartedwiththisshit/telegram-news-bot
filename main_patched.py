import os
import feedparser
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes, CallbackQueryHandler
import asyncio
import uuid
import requests
from bs4 import BeautifulSoup

# Additional imports for filtering and translation
import re
from urllib.parse import urlparse
from deep_translator import LibreTranslator, GoogleTranslator

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
VALIDATION_CHANNEL_ID = int(os.getenv("VALIDATION_CHANNEL_ID", "0"))
MAIN_CHANNEL_ID = int(os.getenv("MAIN_CHANNEL_ID", "0"))
SOURCES = [s.strip() for s in os.getenv("SOURCES", "").split(",") if s.strip()]

posted_links = set()
# pending_posts will map callback IDs to a dict with keys: 'text' and 'image_url'
pending_posts = {}

# -----------------------------------------------------------------------------
# International source filtering and translation helpers
#
# The bot monitors both Ukrainian and international RSS feeds. For international
# sources we only want to forward items that are related to Ukraine. To detect
# relevant items we look for Ukraine-related keywords in the title and summary.
# If an item is relevant, we translate the title and a short portion of the
# summary into Ukrainian using a free translation service (LibreTranslate or
# GoogleTranslator via deep_translator). The original Ukrainian feeds are not
# translated.

# Snippets of hostnames that identify international sources. Any feed whose
# hostname contains one of these snippets will be treated as international.
INTL_HOST_SNIPPETS = (
    "bbc.", "reuters.", "theguardian.", "apnews.", "cnn.", "aljazeera.",
    "nytimes.", "dw.com", "npr.org", "euronews."
)

# Regular expression to match Ukraine-related keywords in multiple languages.
# Matches both Ukrainian and English spellings of key terms and cities.
UA_REGEX = re.compile(
    r"(Україна|україн|Київ|Києв|Харків|Львів|Одеса|Донбас|Донецьк|Крим|"
    r"Херсон|Маріуполь|Запоріжж|Дніпро|Зеленськ|"
    r"Украина|Киев|Харьков|Львов|Одесса|"
    r"Ukraine|Ukrainian|Kyiv|Kiev|Kharkiv|Lviv|Odesa|Donbas|Donetsk|"
    r"Crimea|Kherson|Mariupol|Zaporizh|Dnipro|Zelensky)",
    re.IGNORECASE
)

def is_international(url: str) -> bool:
    """Return True if the given link belongs to an international source.

    We determine this by checking the link's hostname for known snippets.
    """
    try:
        host = urlparse(url).netloc.lower()
        return any(snippet in host for snippet in INTL_HOST_SNIPPETS)
    except Exception:
        return False

def is_ukraine_related(title: str, summary: str) -> bool:
    """Return True if the title or summary contains Ukraine-related keywords."""
    haystack = f"{title or ''} {summary or ''}"
    return bool(UA_REGEX.search(haystack))

def translate_to_uk(text: str) -> str:
    """Translate arbitrary text into Ukrainian using a free service.

    This tries LibreTranslator first and falls back to GoogleTranslator.
    If both fail, the original text is returned unchanged.
    """
    if not text:
        return text
    for translator in (
        lambda t: LibreTranslator(source="auto", target="uk").translate(t),
        lambda t: GoogleTranslator(source="auto", target="uk").translate(t),
    ):
        try:
            return translator(text)
        except Exception:
            continue
    return text

def build_validation_message(link: str, title: str, summary: str) -> str:
    """Construct the message sent to the validation channel.

    For international sources, the title and a brief summary (up to 300
    characters) are translated into Ukrainian. For Ukrainian sources,
    the original title and summary are used. A link to the source is always
    appended. The return value is formatted in Markdown.
    """
    if is_international(link):
        # Only forward if it's Ukraine-related
        if not is_ukraine_related(title, summary):
            return ""
        brief = (summary or "")[:300]
        translated_title = translate_to_uk(title or "")
        translated_brief = translate_to_uk(brief) if brief else ""
        lines = [f"🇺🇦 *{translated_title}*"]
        if translated_brief:
            lines.append(translated_brief)
        lines.append(f"🔗 Джерело: {link}")
        return "\n".join(lines)
    else:
        # Ukrainian sources: keep original text
        cap_lines = []
        if title:
            cap_lines.append(f"*{title}*")
        if summary:
            cap_lines.append(summary)
        cap_lines.append(f"\n[Читати джерело]({link})")
        return "\n".join(cap_lines)

def extract_image(entry):
    """
    Try to extract an image URL from an RSS entry.
    Priority:
      1. media:content or media:thumbnail
      2. enclosure link with image MIME type
      3. og:image from the linked article's HTML
    Returns None if no image is found.
    """
    # media:content or media:thumbnail
    media = entry.get("media_content") or []
    if media:
        url = media[0].get("url")
        if url:
            return url
    thumbs = entry.get("media_thumbnail") or []
    if thumbs:
        url = thumbs[0].get("url")
        if url:
            return url
    # enclosure links
    for link_info in entry.get("links", []):
        if link_info.get("rel") == "enclosure" and link_info.get("type", "").startswith("image/"):
            href = link_info.get("href")
            if href:
                return href
    # fallback: scrape og:image from article page
    try:
        html = requests.get(entry.link, timeout=6).text
        soup = BeautifulSoup(html, "html.parser")
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return og["content"]
    except Exception:
        pass
    return None

async def fetch_feeds(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    for url in SOURCES:
        feed = feedparser.parse(url)
        for entry in feed.entries[:5]:
            link = entry.link
            # Skip if already posted
            if link in posted_links:
                continue
            posted_links.add(link)
            title = entry.title
            summary = entry.get("summary", "")
            # Build a validation message with translation and filtering
            message = build_validation_message(link, title, summary)
            # Skip if the international item isn't Ukraine-related (empty message)
            if not message:
                continue
            image_url = extract_image(entry)
            # Generate a short unique callback ID instead of using the full link
            callback_id = uuid.uuid4().hex
            pending_posts[callback_id] = {"text": message, "image_url": image_url}
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
    data = pending_posts.get(callback_id)
    if not data:
        await query.edit_message_text("Цей запис вже опрацьовано.")
        return
    text = data["text"]
    image_url = data.get("image_url")
    if action == "approve":
        # Send with photo if available, otherwise as text
        if image_url:
            # Telegram captions have a max length of 1024 characters
            caption = text if len(text) <= 1024 else text[:1020] + "..."
            await context.bot.send_photo(
                chat_id=MAIN_CHANNEL_ID,
                photo=image_url,
                caption=caption,
                parse_mode="Markdown",
            )
        else:
            await context.bot.send_message(
                chat_id=MAIN_CHANNEL_ID,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
        status = "✅ Опубліковано"
    else:
        status = "❌ Відхилено"
    # Edit original message in the validation channel to reflect status and remove buttons
    await query.edit_message_text(
        text + f"\n\n{status}",
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
