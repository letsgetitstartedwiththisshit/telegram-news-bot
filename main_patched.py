import os
import feedparser
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
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
# pending_posts maps callback IDs to a dict with keys:
# 'text': the message text sent to the validation channel (translated and formatted),
# 'image_url': an optional image URL to post with the message,
# 'validator_message_id': the Telegram message ID of the post in the validation channel,
# 'canonical_link': the canonicalised URL used for deduplication,
# 'source_link': the original article link.
pending_posts = {}

# In the simplified version (no edit propagation), we no longer track posted posts
# separately or map validator message IDs back to callback IDs.  These features
# were used to support live edit propagation between the validation and main
# channels.  Since we're reverting to a pre-edit workflow, they are no longer
# needed.

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
        # Ukrainian sources: keep the original title and summary but ensure the
        # resulting message does not exceed Telegram's 4096 character limit.
        # Telegram messages (non-photo) allow up to 4096 characters.  Long
        # summaries can cause BadRequest: Text is too long.  To avoid this,
        # truncate the summary to a reasonable length before composing the
        # message.  We target a summary length of at most 1000 characters,
        # leaving ample space for the title and link.  If the summary is
        # longer than 1000 characters, it will be truncated and an ellipsis
        # appended.
        cap_lines = []
        if title:
            cap_lines.append(f"*{title}*")
        # Truncate summary for Ukrainian sources to prevent overly long
        # messages.  Use 1000 characters as a safe upper bound.
        if summary:
            max_summary_len = 1000
            truncated_summary = summary[:max_summary_len]
            if len(summary) > max_summary_len:
                truncated_summary = truncated_summary.rstrip() + "…"
            cap_lines.append(truncated_summary)
        # Include a blank line before the source link for readability.
        cap_lines.append(f"\n[Читати джерело]({link})")
        message = "\n".join(cap_lines)
        # If for some reason the assembled message still exceeds 4096
        # characters (e.g., exceptionally long title), truncate the end.
        max_text_len = 4096
        if len(message) > max_text_len:
            message = message[: max_text_len - 1] + "…"
        return message

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
            # Save pending post metadata.  Store the original link so we can
            # always include it in the caption even if the summary is
            # truncated when posting to the main channel.
            # Store the post metadata.  We keep only the fields needed to
            # construct the final post: the original message text, the image
            # URL (if any), the canonical link for deduplication, and the
            # original source link.  We no longer track edited_text,
            # main_message_id or status, since edit propagation is disabled.
            pending_posts[callback_id] = {
                "text": message,
                "image_url": image_url,
                "validator_message_id": sent_msg.message_id,
                "canonical_link": canonical_link,
                "source_link": link,
            }

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data:
        return
    if ":" not in query.data:
        return
    action, callback_id = query.data.split(":", 1)
    # Fetch the pending post data.  Since edit propagation is disabled, we
    # only look up the post in `pending_posts`.  If it's missing, the post
    # has already been processed.
    data = pending_posts.get(callback_id)
    if not data:
        await query.edit_message_text("Цей запис вже опрацьовано.")
        return
    original_text = data["text"]
    image_url = data.get("image_url")
    # Use the stored source link; fall back to the canonical link if needed.
    source_link = data.get("source_link") or data.get("canonical_link")
    # Always append the source link on a new line when posting to the main channel.
    link_line = f"\n\n🔗 Джерело: {source_link}"
    if action == "approve":
        if image_url:
            # For photos, limit captions to 1024 characters including the link.
            max_len = 1024 - len(link_line)
            if len(original_text) > max_len:
                base_caption = original_text[: max_len - 3] + "..."
            else:
                base_caption = original_text
            caption = base_caption + link_line
            await context.bot.send_photo(
                chat_id=MAIN_CHANNEL_ID,
                photo=image_url,
                caption=caption,
                parse_mode="Markdown",
            )
            status = "✅ Опубліковано"
        else:
            # For text messages, limit to 4096 characters including the link.
            max_len = 4096 - len(link_line)
            if len(original_text) > max_len:
                base_text = original_text[: max_len - 3] + "..."
            else:
                base_text = original_text
            text_to_send = base_text + link_line
            await context.bot.send_message(
                chat_id=MAIN_CHANNEL_ID,
                text=text_to_send,
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
            status = "✅ Опубліковано"
    else:
        # No message sent to main channel on rejection
        status = "❌ Відхилено"
    # Edit the validation channel message to show the status and remove the buttons.
    await query.edit_message_text(
        original_text + f"\n\n{status}",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
    # Remove the post from pending posts since it's now processed.
    pending_posts.pop(callback_id, None)


def main():
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    # Build the application
    application = Application.builder().token(TOKEN).build()
    # Register the callback handler for approve/decline buttons
    application.add_handler(CallbackQueryHandler(handle_callback))
    # In the simplified workflow, we do not register handlers for edited
    # messages or channel posts.  Edits made in the validation channel will
    # not propagate to the main channel.  If an administrator needs to
    # correct a post, they should decline the original and resubmit a new
    # item.
    # Schedule periodic feed fetching every 10 minutes. The first run is after
    # 5 seconds to allow the bot to initialize properly.
    application.job_queue.run_repeating(fetch_feeds, interval=600, first=5)

    # Start polling. We disable signal handling because Render may manage
    # process signals itself. run_polling will initialize and start the
    # application, then cleanly shut down on exit.
    #
    # Note: We previously attempted to delete any existing webhook via
    # asyncio.run(application.bot.delete_webhook(...)), but that approach
    # closes the default event loop. In this simplified version we
    # assume there is only a single instance of the bot running, so a
    # conflicting webhook is unlikely. If webhook conflicts arise in
    # future, consider deleting the webhook via an asynchronous task
    # before calling run_polling() (see PTB docs for examples).
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
