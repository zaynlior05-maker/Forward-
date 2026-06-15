import os
import re
import asyncio
import logging
from io import BytesIO
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityUrl, MessageEntityTextUrl
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.errors import (
    InviteHashInvalidError,
    InviteHashExpiredError,
    UserAlreadyParticipantError,
    FloodWaitError,
    ChatForwardsRestrictedError,
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
RAW_SOURCES    = [s.strip() for s in os.environ["SOURCE_CHATS"].split(",") if s.strip()]
CATCHUP_LIMIT  = int(os.environ.get("CATCHUP_LIMIT", "50"))
CATCHUP_DELAY  = float(os.environ.get("CATCHUP_DELAY", "0.8"))

# ── Regexes ────────────────────────────────────────────────────────────────────
_PRIVATE_INVITE_RE = re.compile(r"(?:https?://)?t\.me/\+([A-Za-z0-9_-]+)", re.IGNORECASE)
_PUBLIC_LINK_RE    = re.compile(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)",     re.IGNORECASE)
_URL_RE            = re.compile(r"(https?://\S+|t\.me/\S+)",                re.IGNORECASE)


# ── Source resolver ────────────────────────────────────────────────────────────
async def resolve_source(client: TelegramClient, raw: str):
    if re.fullmatch(r"-?\d+", raw):
        log.info("Source resolved (numeric ID): %s", raw)
        return int(raw)

    invite_match = _PRIVATE_INVITE_RE.match(raw)
    if invite_match:
        hash_ = invite_match.group(1)
        try:
            result = await client(CheckChatInviteRequest(hash=hash_))
            if hasattr(result, "chat"):
                chat = result.chat
                log.info("Source resolved (private invite, already member): %s → %s (id=%s)",
                         raw, getattr(chat, "title", "?"), chat.id)
                return chat
            log.info("Not yet a member of %s — joining now…", raw)
            try:
                updates = await client(ImportChatInviteRequest(hash=hash_))
                chat = updates.chats[0]
                log.info("Joined and resolved (private invite): %s → %s (id=%s)",
                         raw, getattr(chat, "title", "?"), chat.id)
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

    handle = raw.lstrip("@")
    public_match = _PUBLIC_LINK_RE.match(raw)
    if public_match:
        handle = public_match.group(1)
    try:
        entity = await client.get_entity(handle)
        log.info("Source resolved (public): %s → %s (id=%s)",
                 raw, getattr(entity, "title", getattr(entity, "username", "?")), entity.id)
        return entity
    except Exception as exc:
        log.error("Could not resolve public source '%s': %s", raw, exc)
        return None


# ── Link-detection ─────────────────────────────────────────────────────────────
def has_link(message) -> bool:
    if message.entities:
        for ent in message.entities:
            if isinstance(ent, (MessageEntityUrl, MessageEntityTextUrl)):
                return True
    if message.text and _URL_RE.search(message.text):
        return True
    return False


# ── Media sender (download → re-upload, bypasses all channel restrictions) ─────
async def send_as_copy(client: TelegramClient, msg, text: str) -> None:
    """
    Downloads media into memory and re-uploads it to the destination.
    This works even for channels with 'Restrict saving content' enabled.
    """
    if msg.media:
        log.info("Downloading media for msg_id=%s…", msg.id)
        buf = await client.download_media(msg, file=bytes)

        if buf:
            media_buf = BytesIO(buf)
            # Preserve original filename for documents/videos
            filename = None
            if hasattr(msg, "document") and msg.document:
                for attr in msg.document.attributes:
                    if hasattr(attr, "file_name"):
                        filename = attr.file_name
                        break
            if filename:
                media_buf.name = filename

            await client.send_message(
                DESTINATION_ID,
                message=text,
                file=media_buf,
                parse_mode="html",
            )
            log.info("Media re-uploaded successfully (msg_id=%s)", msg.id)
        else:
            # Media unavailable — send text only
            log.warning("Media download returned empty for msg_id=%s — sending text only", msg.id)
            if text:
                await client.send_message(DESTINATION_ID, message=text, parse_mode="html")
    elif text:
        await client.send_message(DESTINATION_ID, message=text, parse_mode="html")


# ── Core message processor ─────────────────────────────────────────────────────
async def process_message(client: TelegramClient, msg, chat_id=None) -> None:
    source_label = chat_id or "?"
    text = msg.text or msg.message or ""

    try:
        if has_link(msg):
            # Rule 1 — FORWARD (keeps "Forwarded from" header + clickable links)
            log.info("FORWARD  | chat=%-20s  msg_id=%s  (link detected)", source_label, msg.id)
            try:
                await client.forward_messages(DESTINATION_ID, msg)
            except ChatForwardsRestrictedError:
                # Channel has content protection — fall back to copy+re-upload
                log.warning(
                    "FORWARD restricted → falling back to re-upload | chat=%s msg_id=%s",
                    source_label, msg.id,
                )
                await send_as_copy(client, msg, text)
        else:
            # Rule 2 — COPY (no forwarding header; media re-uploaded from memory)
            log.info("COPY     | chat=%-20s  msg_id=%s", source_label, msg.id)
            await send_as_copy(client, msg, text)

    except FloodWaitError as e:
        log.warning("FloodWait: sleeping %ds…", e.seconds)
        await asyncio.sleep(e.seconds)
        await process_message(client, msg, chat_id)  # retry after wait
    except Exception as exc:
        log.error("Failed to process msg_id=%s from chat=%s: %s", msg.id, source_label, exc)


# ── Catch-up: replay missed messages on startup ────────────────────────────────
async def catchup(client: TelegramClient, source) -> None:
    try:
        entity = await client.get_entity(source)
        title  = getattr(entity, "title", getattr(entity, "username", str(source)))
    except Exception:
        entity = source
        title  = str(source)

    log.info("Catch-up | starting for: %s (limit=%d)", title, CATCHUP_LIMIT)

    messages = []
    async for msg in client.iter_messages(entity, limit=CATCHUP_LIMIT):
        messages.append(msg)
    messages.reverse()  # oldest → newest

    sent = 0
    for msg in messages:
        if not msg.text and not msg.media:
            continue
        await process_message(client, msg, chat_id=getattr(entity, "id", source))
        sent += 1
        await asyncio.sleep(CATCHUP_DELAY)

    log.info("Catch-up | done for %s — %d message(s) processed", title, sent)


# ── Entry point ────────────────────────────────────────────────────────────────
async def main() -> None:
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()

    me = await client.get_me()
    log.info("Logged in as: %s (id=%s)", me.username or me.first_name, me.id)

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

    # Catch-up on missed messages
    if CATCHUP_LIMIT > 0:
        log.info("Starting catch-up (CATCHUP_LIMIT=%d)…", CATCHUP_LIMIT)
        for source in resolved:
            await catchup(client, source)
        log.info("Catch-up complete — switching to live mode.")

    # Live listener
    @client.on(events.NewMessage(chats=resolved))
    async def _handler(event):
        await process_message(client, event.message, chat_id=event.chat_id)

    log.info("Bot is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
