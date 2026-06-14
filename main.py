import os
import re
import asyncio
import logging
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
                # Race condition: joined between check and import; search dialogs
                pass

        except (InviteHashInvalidError, InviteHashExpiredError) as exc:
            log.error("Invalid or expired invite link '%s': %s", raw, exc)
            return None
        except Exception as exc:
            log.error("Could not resolve private invite '%s': %s", raw, exc)
            return None

        # Fallback: scan open dialogs for a match (handles race condition above)
        async for dialog in client.iter_dialogs():
            # Invite hashes don't map to usernames, so we can't match by name;
            # just return None and let the user provide a numeric ID instead.
            pass
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


# ── Core message handler ───────────────────────────────────────────────────────
async def handle_message(client: TelegramClient, event) -> None:
    msg = event.message

    try:
        if has_link(msg):
            # Rule 1 — FORWARD
            # Keeps the "Forwarded from [Source]" header and all links intact.
            log.info("FORWARD  | chat=%-20s  msg_id=%s  (link detected)", event.chat_id, msg.id)
            await client.forward_messages(DESTINATION_ID, msg)

        else:
            # Rule 2 — COPY
            # Sends content cleanly with no forwarding attribution.
            log.info("COPY     | chat=%-20s  msg_id=%s", event.chat_id, msg.id)
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

    except Exception as exc:
        log.error("Failed to process msg_id=%s from chat=%s: %s", msg.id, event.chat_id, exc)


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

    # Register handler against resolved entities
    @client.on(events.NewMessage(chats=resolved))
    async def _handler(event):
        await handle_message(client, event)

    log.info("Bot is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
