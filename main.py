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


# ── Message processor ──────────────────────────────────────────────────────────
async def process_message(
    client: TelegramClient,
    msg,
    source_title: str,
    chat_id=None,
) -> None:
    """
    Bypasses forward restrictions by downloading media to memory and
    re-uploading as a new post with a plain-text "Forwarded from" header.

    Format:
        Forwarded from **{Source Name}**

        {original text or caption}
    """
    source_label = chat_id or "?"
    original_text = msg.text or msg.message or ""

    # Build the full message text with bold header (no hyperlinks)
    if original_text:
        full_text = f"Forwarded from **{source_title}**\n\n{original_text}"
    else:
        full_text = f"Forwarded from **{source_title}**"

    log.info("SEND | chat=%-20s  msg_id=%s", source_label, msg.id)

    try:
        if msg.media:
            # Download media into memory (no temp files on disk)
            log.info("Downloading media for msg_id=%s…", msg.id)
            buf = await client.download_media(msg, file=bytes)

            if buf:
                media_buf = BytesIO(buf)
                # Preserve original filename if available
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
                    message=full_text,
                    file=media_buf,
                    parse_mode="md",
                )
            else:
                # Media download returned empty — send text only
                if full_text:
                    await client.send_message(
                        DESTINATION_ID,
                        message=full_text,
                        parse_mode="md",
                    )
        else:
            # Text-only message
            if full_text:
                await client.send_message(
                    DESTINATION_ID,
                    message=full_text,
                    parse_mode="md",
                )

    except FloodWaitError as e:
        log.warning("FloodWait: sleeping %ds as Telegram requested…", e.seconds)
        await asyncio.sleep(e.seconds)
        # Retry once after flood wait
        await process_message(client, msg, source_title, chat_id)

    except Exception as exc:
        log.error("Failed to process msg_id=%s from chat=%s: %s", msg.id, source_label, exc)


# ── Catch-up: replay missed messages on startup ────────────────────────────────
async def catchup(client: TelegramClient, source, source_title: str) -> None:
    log.info("Catch-up | starting for: %s (limit=%d)", source_title, CATCHUP_LIMIT)

    messages = []
    async for msg in client.iter_messages(source, limit=CATCHUP_LIMIT):
        messages.append(msg)
    messages.reverse()  # oldest → newest

    sent = 0
    for msg in messages:
        if not msg.text and not msg.media:
            continue
        await process_message(client, msg, source_title,
                              chat_id=getattr(source, "id", source))
        sent += 1
        await asyncio.sleep(CATCHUP_DELAY)

    log.info("Catch-up | done for %s — %d message(s) processed", source_title, sent)


# ── Entry point ────────────────────────────────────────────────────────────────
async def main() -> None:
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()

    me = await client.get_me()
    log.info("Logged in as: %s (id=%s)", me.username or me.first_name, me.id)

    # Resolve sources and build title map
    log.info("Resolving %d source(s)…", len(RAW_SOURCES))
    resolved      = []   # list of entities / IDs
    source_titles = {}   # entity_id → display name

    for raw in RAW_SOURCES:
        entity = await resolve_source(client, raw)
        if entity is None:
            continue
        resolved.append(entity)

        # Determine a clean display title for the "Forwarded from" header
        if isinstance(entity, int):
            try:
                full_entity = await client.get_entity(entity)
                title = getattr(full_entity, "title",
                        getattr(full_entity, "username", str(entity)))
            except Exception:
                title = str(entity)
            source_titles[entity] = title
        else:
            title = getattr(entity, "title",
                    getattr(entity, "username", str(getattr(entity, "id", entity))))
            source_titles[getattr(entity, "id", id(entity))] = title

    if not resolved:
        log.error("No valid sources could be resolved. Exiting.")
        return

    log.info("Monitoring %d source(s) → destination %s", len(resolved), DESTINATION_ID)

    # Catch-up on missed messages
    if CATCHUP_LIMIT > 0:
        log.info("Starting catch-up (CATCHUP_LIMIT=%d)…", CATCHUP_LIMIT)
        for source in resolved:
            sid   = getattr(source, "id", source)
            title = source_titles.get(sid, str(sid))
            await catchup(client, source, title)
        log.info("Catch-up complete. Switching to live mode…")

    # Live listener
    @client.on(events.NewMessage(chats=resolved))
    async def _handler(event):
        sid   = event.chat_id
        title = source_titles.get(sid, str(sid))
        await process_message(client, event.message, title, chat_id=sid)

    log.info("Bot is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
