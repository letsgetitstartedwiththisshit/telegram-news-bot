import os
import feedparser
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes, CallbackQueryHandler, EditedMessageHandler
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

# Track canonical links we've already seen to avoid duplicates.
posted_links = set()
# pending_posts will map callback IDs to a dict with keys: 
# 'text', 'edited_text', 'image_url', 'validator_message_id', 'main_message_id',
# 'canonical_link', and 'status'. These posts are awaiting a decision.
pending_posts = {}
# posted_posts will store the same structure for items that have been processed
# (either approved or declined). This allows us to propagate edits after posting.
posted_posts = {}
# Map validator message IDs back to callback IDs so we can look up items by the
# edited message's ID.
validator_to_callback = {}

# Query parameters to drop when canonicalizing URLs
DROP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid"
}

def canonicalize_url(u: str) -> str:
    """Return a canonical form of the URL for deduplication.

    Removes tracking parameters, strips fragments, normalizes host and path,
    and ensures a consistent scheme.
    """
    try:
        from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
        p = urlparse(u)
        # filter query parameters
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in DROP_PARAMS]
        # normalize path (remove trailing slash, ensure at least '/')
        path = p.path.rstrip("/") or "/"
        return urlunparse((
            p.scheme.lower(),
            p.netloc.lower(),
            path,
            "",  # params unused
            urlencode(q, doseq=True),
            ""   # fragment removed
        ))
    except Exception:
        return u

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
            # Skip if already processed based on canonical URL
            canonical_link = canonicalize_url(link)
            if canonical_link in posted_links:
                continue
            posted_links.add(canonical_link)
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
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Публікувати", callback_data=f"approve:{callback_id}"),
                    InlineKeyboardButton("❌ Відхилити", callback_data=f"reject:{callback_id}"),
                ]
            ])
            # Send the message to the validation channel and capture the message ID
            sent_msg = await bot.send_message(
                chat_id=VALIDATION_CHANNEL_ID,
                text=message,
                parse_mode="Markdown",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            # Save pending post metadata
            pending_posts[callback_id] = {
                "text": message,
                "edited_text": None,
                "image_url": image_url,
                "validator_message_id": sent_msg.message_id,
                "main_message_id": None,
                "canonical_link": canonical_link,
                "status": "pending",
            }
            validator_to_callback[sent_msg.message_id] = callback_id

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data:
        return
    if ":" not in query.data:
        return
    action, callback_id = query.data.split(":", 1)
    # Try to fetch from pending posts first, else posted posts (shouldn't happen)
    data = pending_posts.get(callback_id) or posted_posts.get(callback_id)
    if not data:
        await query.edit_message_text("Цей запис вже опрацьовано.")
        return
    # Determine the final text/caption to use (editor edits take precedence)
    final_text = data.get("edited_text") or data["text"]
    image_url = data.get("image_url")
    if action == "approve":
        # Send with photo if available, otherwise as text
        if image_url:
            # Telegram captions have a max length of 1024 characters
            caption = final_text if len(final_text) <= 1024 else final_text[:1020] + "..."
            sent_main = await context.bot.send_photo(
                chat_id=MAIN_CHANNEL_ID,
                photo=image_url,
                caption=caption,
                parse_mode="Markdown",
            )
        else:
            sent_main = await context.bot.send_message(
                chat_id=MAIN_CHANNEL_ID,
                text=final_text,
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
        status = "✅ Опубліковано"
        # Store main message ID for edit propagation
        data["main_message_id"] = sent_main.message_id
        data["status"] = "approved"
    else:
        status = "❌ Відхилено"
        data["status"] = "declined"
    # Edit original message in the validation channel to reflect status and remove buttons.
    # Use the final text (so edits appear) plus status.
    await query.edit_message_text(
        final_text + f"\n\n{status}",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
    # Move from pending to posted posts for tracking edits
    if callback_id in pending_posts:
        pending_posts.pop(callback_id, None)
        posted_posts[callback_id] = data

def main():
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CallbackQueryHandler(handle_callback))
    # Handle edits in the validation channel
    application.add_handler(EditedMessageHandler(handle_validation_edit))
    # Schedule periodic feed fetching every 10 minutes
    application.job_queue.run_repeating(fetch_feeds, interval=600, first=5)
    application.run_polling()

if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# Validation edit handler
# When a message in the validation channel is edited by an admin, update our
# stored text and propagate the change to the main channel if already posted.
async def handle_validation_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    edited_msg = update.edited_message
    if not edited_msg:
        return
    try:
        chat_id = edited_msg.chat_id
    except Exception:
        return
    # Only process edits in the validation channel
    if int(chat_id) != VALIDATION_CHANNEL_ID:
        return
    callback_id = validator_to_callback.get(edited_msg.message_id)
    if not callback_id:
        return
    new_text = (edited_msg.caption or edited_msg.text or "").strip()
    if not new_text:
        return
    # Update the appropriate record
    if callback_id in pending_posts:
        pending_posts[callback_id]["edited_text"] = new_text
    if callback_id in posted_posts:
        data = posted_posts[callback_id]
        data["edited_text"] = new_text
        # If the item has been approved, propagate the edit to the main channel
        if data.get("status") == "approved" and data.get("main_message_id"):
            try:
                if data.get("image_url"):
                    # Edit caption of photo in main channel
                    caption = new_text if len(new_text) <= 1024 else new_text[:1020] + "..."
                    await context.bot.edit_message_caption(
                        chat_id=MAIN_CHANNEL_ID,
                        message_id=data["main_message_id"],
                        caption=caption,
                        parse_mode="Markdown",
                    )
                else:
                    # Edit text message in main channel
                    await context.bot.edit_message_text(
                        chat_id=MAIN_CHANNEL_ID,
                        message_id=data["main_message_id"],
                        text=new_text,
                        parse_mode="Markdown",
                    )
            except Exception:
                # Ignore edit failures (e.g. message too long or bad markdown)
                pass
