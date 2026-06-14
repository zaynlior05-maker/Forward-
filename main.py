import os
import re
import asyncio
import logging
from datetime import datetime, timezone
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageEntityUrl,
    MessageEntityTextUrl,
    Channel,
    Chat,
)
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.errors import (
    InviteHashInvalidError,
    InviteHashExpiredError,
    UserAlreadyParticipantError,
    FloodWaitError,
)
from dotenv import load_dotenv

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
load_dotenv()

API_ID         = int(os.environ["TELEGRAM_API_ID"])
API_HASH       = os.environ["TELEGRAM_API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
DESTINATION_ID = int(os.environ["DESTINATION_CHAT_ID"])

# SOURCE_CHATS accepts a comma-separated mix of:
#   - Numeric IDs:          -1001234567890
#   - Public handles:       @mychannel  or  t.me/mychannel
#   - Private invite links: t.me/+AbCdEfGhIjKl
RAW_SOURCES = [s.strip() for s in os.environ["SOURCE_CHATS"].split(",") if s.strip()]

# How many recent messages to catch up on per source at startup (default: 50)
CATCHUP_LIMIT = int(os.environ.get("CATCHUP_LIMIT", "50"))

# Delay between catch-up messages to avoid Telegram flood limits (seconds)
CATCHUP_DELAY = float(os.environ.get("CATCHUP_DELAY", "0.5"))

# ── Regexes ────────────────────────────────────────────────────────────────────
_PRIVATE_INVITE_RE = re.compile(r"(?:https?://)?t\.me/\+([A-Za-z0-9_-]+)", re.IGNORECASE)
_PUBLIC_LINK_RE    = re.compile(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)",     re.IGNORECASE)
_URL_RE            = re.compile(r"(https?://\S+|t\.me/\S+)",                re.IGNORECASE)


# ── Source resolver ────────────────────────────────────────────────────────────
async def resolve_source(client: TelegramClient, raw: str):
    """
    Resolves a single source string to a Telethon entity (or integer ID).
    Handles:
      - Plain numeric IDs        → returned as int
      - Private invite links     → joined/checked via invite hash
      - Public @handles/t.me/    → resolved via get_entity()
    Returns None and logs a warning if resolution fails.
    """

    # ── Numeric ID ──────────────────────────────────────────────────────────
    if re.fullmatch(r"-?\d+", raw):
        log.info("Source resolved (numeric ID): %s", raw)
        return int(raw)

    # ── Private invite link: t.me/+HASH ─────────────────────────────────────
    invite_match = _PRIVATE_INVITE_RE.match(raw)
    if invite_match:
        hash_ = invite_match.group(1)
        try:
            result = await client(CheckChatInviteRequest(hash=hash_))

            # ChatInviteAlready → already a member; .chat holds the entity
            if hasattr(result, "chat"):
                chat = result.chat
                log.info(
                    "Source resolved (private invite, already member): %s → %s (id=%s)",
                    raw, getattr(chat, "title", "?"), chat.id,
                )
                return chat

            # ChatInvite → not yet a member; join automatically
            log.info("Not yet a member of %s — joining now…", raw)
            try:
                updates = await client(ImportChatInviteRequest(hash=hash_))
                chat = updates.chats[0]
                log.info(
                    "Joined and resolved (private invite): %s → %s (id=%s)",
                    raw, getattr(chat, "title", "?"), chat.id,
                )
                return chat
            except UserAlreadyParticipantError:
                pass

        except (InviteHashInvalidError, InviteHashExpiredError) as exc:
            log.error("Invalid or expired invite link '%s': %s", raw, exc)
            return None
        except Exception as exc:
            log.error("Could not resolve private invite '%s': %s", raw, exc)
            return None

        log.warning("Could not resolve '%s' after join attempt; skip.", raw)
        return None

    # ── Public handle or t.me/username link ─────────────────────────────────
    handle = raw.lstrip("@")
    public_match = _PUBLIC_LINK_RE.match(raw)
    if public_match:
        handle = public_match.group(1)

    try:
        entity = await client.get_entity(handle)
        log.info(
            "Source resolved (public): %s → %s (id=%s)",
            raw, getattr(entity, "title", getattr(entity, "username", "?")), entity.id,
        )
        return entity
    except Exception as exc:
        log.error("Could not resolve public source '%s': %s", raw, exc)
        return None


# ── Link-detection helper ──────────────────────────────────────────────────────
def has_link(message) -> bool:
    """
    True if the message contains any URL or Telegram link.
    Checks Telegram message entities first (catches embedded text links),
    then falls back to a regex scan of the raw text.
    """
    if message.entities:
        for ent in message.entities:
            if isinstance(ent, (MessageEntityUrl, MessageEntityTextUrl)):
                return True

    if message.text and _URL_RE.search(message.text):
        return True

    return False


# ── Single message processor (shared by catch-up and live handler) ─────────────
async def process_message(client: TelegramClient, msg, chat_id=None) -> None:
    """
    Applies Rule 1 (forward if link) or Rule 2 (copy if no link).
    Works for both catch-up messages and live incoming events.
    """
    source_label = chat_id or "?"
    try:
        if has_link(msg):
            log.info("FORWARD  | chat=%-20s  msg_id=%s  (link detected)", source_label, msg.id)
            await client.forward_messages(DESTINATION_ID, msg)
        else:
            log.info("COPY     | chat=%-20s  msg_id=%s", source_label, msg.id)
            text = msg.text or msg.message or ""
            if msg.media:
                await client.send_message(
                    DESTINATION_ID,
                    message=text,
                    file=msg.media,
                    parse_mode="html",
                )
            elif text:
                await client.send_message(
                    DESTINATION_ID,
                    message=text,
                    parse_mode="html",
                )

    except FloodWaitError as e:
        log.warning("FloodWait: sleeping %ds as requested by Telegram…", e.seconds)
        await asyncio.sleep(e.seconds)
    except Exception as exc:
        log.error("Failed to process msg_id=%s from chat=%s: %s", msg.id, source_label, exc)


# ── Catch-up: replay missed messages on startup ────────────────────────────────
async def catchup(client: TelegramClient, source) -> None:
    """
    Fetches the last CATCHUP_LIMIT messages from `source` (oldest first)
    and processes each one through the forward/copy logic.
    """
    try:
        entity = await client.get_entity(source)
        title  = getattr(entity, "title", getattr(entity, "username", str(source)))
    except Exception:
        entity = source
        title  = str(source)

    log.info("Catch-up | starting for: %s (limit=%d)", title, CATCHUP_LIMIT)

    # iter_messages returns newest-first; reverse so we send oldest first
    messages = []
    async for msg in client.iter_messages(entity, limit=CATCHUP_LIMIT):
        messages.append(msg)

    messages.reverse()  # oldest → newest order

    sent = 0
    for msg in messages:
        # Skip empty service messages (joins, pins, etc.)
        if not msg.text and not msg.media:
            continue
        await process_message(client, msg, chat_id=getattr(entity, "id", source))
        sent += 1
        await asyncio.sleep(CATCHUP_DELAY)  # be gentle on flood limits

    log.info("Catch-up | done for %s — %d message(s) processed", title, sent)


# ── Entry point ────────────────────────────────────────────────────────────────
async def main() -> None:
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()

    me = await client.get_me()
    log.info("Logged in as: %s (id=%s)", me.username or me.first_name, me.id)

    # Resolve all sources at startup
    log.info("Resolving %d source(s)…", len(RAW_SOURCES))
    resolved = []
    for raw in RAW_SOURCES:
        entity = await resolve_source(client, raw)
        if entity is not None:
            resolved.append(entity)

    if not resolved:
        log.error("No valid sources could be resolved. Exiting.")
        return

    log.info("Monitoring %d source(s) → destination %s", len(resolved), DESTINATION_ID)

    # ── Catch-up: replay missed messages from each source ────────────────────
    if CATCHUP_LIMIT > 0:
        log.info("Starting catch-up for all sources (CATCHUP_LIMIT=%d)…", CATCHUP_LIMIT)
        for source in resolved:
            await catchup(client, source)
        log.info("Catch-up complete. Switching to live mode…")

    # ── Live listener ────────────────────────────────────────────────────────
    @client.on(events.NewMessage(chats=resolved))
    async def _handler(event):
        await process_message(client, event.message, chat_id=event.chat_id)

    log.info("Bot is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
