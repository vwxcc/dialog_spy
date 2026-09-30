import asyncio
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
from fastapi import FastAPI, Request


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.getenv(
    "MISTRAL_MODEL",
    "mistral-small-latest",
)
MISTRAL_BASE_URL = os.getenv(
    "MISTRAL_BASE_URL",
    "https://api.mistral.ai/v1",
).rstrip("/")

MAX_EVENTS = int(
    os.getenv("MAX_EVENTS", "2000")
)

PORT = int(
    os.getenv("PORT", "10000")
)

TELEGRAM_API = (
    f"https://api.telegram.org/bot{BOT_TOKEN}"
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)

log = logging.getLogger(
    "telegram-archive"
)


# ============================================================
# IN-MEMORY EVENT STORAGE
# ============================================================

events_memory = deque(
    maxlen=MAX_EVENTS
)

# Последняя известная версия сообщения.
# Ключ:
# (source_type, business_connection_id, chat_id, message_id)
#
# Значение:
# dict с данными сообщения.
known_messages: dict[
    tuple,
    dict
] = {}

# Для защиты от одновременной обработки
event_lock = asyncio.Lock()


# ============================================================
# BASIC HELPERS
# ============================================================

def now() -> int:
    return int(time.time())


def cut(
    value: Any,
    limit: int = 10000,
) -> str:

    if value is None:
        return ""

    text = str(value)

    if len(text) <= limit:
        return text

    return (
        text[:limit]
        + "\n...[обрезано]"
    )


def get_chat(
    message: dict,
) -> dict:

    return message.get(
        "chat",
        {}
    )


def chat_title(
    chat: dict,
) -> str:

    if chat.get("title"):
        return chat["title"]

    first = chat.get(
        "first_name",
        ""
    )

    last = chat.get(
        "last_name",
        ""
    )

    username = chat.get(
        "username"
    )

    name = (
        f"{first} {last}"
    ).strip()

    if username:
        return (
            f"{name or 'Пользователь'} "
            f"(@{username})"
        )

    return (
        name
        or "Без названия"
    )


def sender_name(
    user: dict | None,
) -> str:

    if not user:
        return "Неизвестно"

    first = user.get(
        "first_name",
        ""
    )

    last = user.get(
        "last_name",
        ""
    )

    username = user.get(
        "username"
    )

    name = (
        f"{first} {last}"
    ).strip()

    if username:
        return (
            f"{name or 'Пользователь'} "
            f"(@{username})"
        )

    return (
        name
        or "Неизвестно"
    )


def message_text(
    message: dict,
) -> str:

    return cut(
        message.get("text")
        or message.get("caption")
        or "",
        20000,
    )


def media_type(
    message: dict,
) -> str:

    if message.get("photo"):
        return "photo"

    if message.get("video"):
        return "video"

    if message.get("video_note"):
        return "video_note"

    if message.get("voice"):
        return "voice"

    if message.get("audio"):
        return "audio"

    if message.get("document"):
        return "document"

    if message.get("animation"):
        return "animation"

    if message.get("sticker"):
        return "sticker"

    if message.get("contact"):
        return "contact"

    if message.get("location"):
        return "location"

    if message.get("venue"):
        return "venue"

    if message.get("poll"):
        return "poll"

    if message.get("dice"):
        return "dice"

    return "text"


def message_key(
    message: dict,
    source_type: str,
) -> tuple:

    return (
        source_type,
        message.get(
            "business_connection_id"
        ),
        get_chat(message).get("id"),
        message.get("message_id"),
    )


def timestamp(
    value: int | None,
) -> str:

    if not value:
        return "неизвестно"

    return time.strftime(
        "%d.%m.%Y %H:%M:%S UTC",
        time.gmtime(value),
    )


# ============================================================
# TELEGRAM API
# ============================================================

async def telegram(
    method: str,
    payload: dict | None = None,
) -> dict:

    url = (
        f"{TELEGRAM_API}/{method}"
    )

    timeout = aiohttp.ClientTimeout(
        total=60
    )

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:

        async with session.post(
            url,
            json=payload or {},
        ) as response:

            data = await response.json()

            if not data.get("ok"):

                raise RuntimeError(
                    f"Telegram {method}: "
                    f"{data.get('description', data)}"
                )

            return data["result"]


# ============================================================
# SEND ADMIN TEXT
# ============================================================

async def admin_text(
    text: str,
) -> None:

    text = text or ""

    # Telegram maximum for text messages.
    chunks = [
        text[i:i + 4000]
        for i in range(
            0,
            len(text),
            4000
        )
    ]

    if not chunks:
        chunks = [""]

    for chunk in chunks:

        try:

            await telegram(
                "sendMessage",
                {
                    "chat_id":
                        ADMIN_CHAT_ID,
                    "text":
                        chunk,
                },
            )

        except Exception:

            log.exception(
                "Cannot send admin text"
            )


# ============================================================
# COPY ORIGINAL MESSAGE
# ============================================================

async def copy_original(
    message: dict,
) -> int | None:

    chat = get_chat(
        message
    )

    chat_id = chat.get(
        "id"
    )

    message_id = message.get(
        "message_id"
    )

    if chat_id is None:
        return None

    if message_id is None:
        return None

    payload = {
        "chat_id":
            ADMIN_CHAT_ID,

        "from_chat_id":
            chat_id,

        "message_id":
            message_id,
    }

    business_connection_id = (
        message.get(
            "business_connection_id"
        )
    )

    if business_connection_id:
        payload[
            "business_connection_id"
        ] = business_connection_id

    try:

        result = await telegram(
            "copyMessage",
            payload,
        )

        return result.get(
            "message_id"
        )

    except Exception as error:

        log.warning(
            "copyMessage failed: %s",
            error,
        )

        return None


# ============================================================
# COPY MESSAGE + METADATA
# ============================================================

async def archive_message(
    message: dict,
    source_type: str,
    event_type: str,
) -> None:

    key = message_key(
        message,
        source_type,
    )

    text = message_text(
        message
    )

    mtype = media_type(
        message
    )

    chat = get_chat(
        message
    )

    sender = message.get(
        "from"
    )

    # New messages are only cached in RAM. They are not sent
    # to the admin. Edits are copied; this keeps the archive
    # focused on changes/deletions.
    copied_message_id = None

    if event_type != "new":
        copied_message_id = (
            await copy_original(
                message
            )
        )

    # --------------------------------------------------------
    # Keep metadata in RAM.
    # --------------------------------------------------------

    snapshot = {
        "source_type":
            source_type,

        "business_connection_id":
            message.get(
                "business_connection_id"
            ),

        "chat_id":
            chat.get("id"),

        "chat_title":
            chat_title(chat),

        "message_id":
            message.get("message_id"),

        "sender":
            sender_name(sender),

        "sender_id":
            (
                sender.get("id")
                if sender
                else None
            ),

        "date":
            message.get("date"),

        "text":
            text,

        "media_type":
            mtype,

        "copied_message_id":
            copied_message_id,

        "saved_at":
            now(),
    }

    async with event_lock:

        previous = (
            known_messages.get(key)
        )

        known_messages[key] = snapshot

        events_memory.append(
            {
                "type":
                    event_type,

                "time":
                    now(),

                "chat":
                    chat_title(chat),

                "chat_id":
                    chat.get("id"),

                "message_id":
                    message.get(
                        "message_id"
                    ),

                "sender":
                    sender_name(sender),

                "text":
                    text,

                "media":
                    mtype,

                "source":
                    source_type,

                "copied_message_id":
                    copied_message_id,

                "previous_text":
                    (
                        previous.get("text")
                        if previous
                        else None
                    ),
            }
        )

    # --------------------------------------------------------
    # Human-readable notification.
    #
    # The actual message/media itself was already copied
    # into ADMIN_CHAT_ID.
    # --------------------------------------------------------

    if event_type == "new":
        # Intentionally silent: the message is cached above
        # so a later edit/delete/reply can refer to it.
        return

    elif event_type == "edit":

        prefix = "✏️ ИЗМЕНЕНИЕ"

    else:

        prefix = "📌 СОБЫТИЕ"

    notification = (
        f"{prefix}\n\n"
        f"Чат: {chat_title(chat)}\n"
        f"Автор: {sender_name(sender)}\n"
        f"Message ID: "
        f"{message.get('message_id')}\n"
        f"Время: "
        f"{timestamp(message.get('date'))}\n"
        f"Тип: {mtype}"
    )

    if event_type == "edit":

        old_text = (
            previous.get("text")
            if previous
            else None
        )

        notification += (
            "\n\n"
            "━━━━━━━━━━━━\n"
            "БЫЛО:\n"
            f"{old_text or '[без текста]'}"
            "\n\n"
            "СТАЛО:\n"
            f"{text or '[без текста]'}"
        )

    if copied_message_id:

        notification += (
            "\n\n"
            "💾 Копия отправлена выше."
        )

    else:

        notification += (
            "\n\n"
            "⚠️ Telegram не позволил "
            "скопировать сообщение."
        )

    await admin_text(
        notification
    )


# ============================================================
# DELETE EVENT
# ============================================================

async def process_deleted_business(
    update: dict,
) -> None:

    connection_id = update.get(
        "business_connection_id"
    )

    chat = update.get(
        "chat",
        {}
    )

    chat_id = chat.get(
        "id"
    )

    message_ids = update.get(
        "message_ids",
        []
    )

    for message_id in message_ids:

        key = (
            "business",
            connection_id,
            chat_id,
            message_id,
        )

        async with event_lock:

            old = known_messages.get(
                key
            )

            events_memory.append(
                {
                    "type":
                        "delete",

                    "time":
                        now(),

                    "chat":
                        chat_title(chat),

                    "chat_id":
                        chat_id,

                    "message_id":
                        message_id,

                    "sender":
                        (
                            old.get("sender")
                            if old
                            else None
                        ),

                    "text":
                        (
                            old.get("text")
                            if old
                            else None
                        ),

                    "media":
                        (
                            old.get(
                                "media_type"
                            )
                            if old
                            else None
                        ),

                    "source":
                        "business",

                    "copied_message_id":
                        (
                            old.get(
                                "copied_message_id"
                            )
                            if old
                            else None
                        ),
                }
            )

        notification = (
            "🗑 СООБЩЕНИЕ УДАЛЕНО\n\n"
            f"Чат: {chat_title(chat)}\n"
            f"Message ID: {message_id}\n"
        )

        if old:

            notification += (
                f"Автор: "
                f"{old.get('sender', 'неизвестно')}\n"
                f"Тип: "
                f"{old.get('media_type', 'text')}\n"
            )

            if old.get("text"):

                notification += (
                    "\n━━━━━━━━━━━━\n"
                    "ТЕКСТ:\n"
                    f"{old['text']}\n"
                )

            copied_id = old.get(
                "copied_message_id"
            )

            if copied_id:

                notification += (
                    "\n💾 Исходная копия "
                    "уже находится выше "
                    "в этом чате."
                )

            else:

                notification += (
                    "\n⚠️ Содержимое "
                    "не удалось скопировать "
                    "до удаления."
                )

        else:

            notification += (
                "\n⚠️ Это сообщение не было "
                "сохранено в памяти процесса."
            )

        await admin_text(
            notification
        )


# ============================================================
# BUSINESS CONNECTION
# ============================================================

async def process_business_connection(
    connection: dict,
) -> None:

    user = connection.get(
        "user",
        {}
    )

    enabled = connection.get(
        "is_enabled"
    )

    rights = connection.get(
        "rights",
        {}
    )

    text = (
        "🔗 BUSINESS CONNECTION\n\n"
        f"Статус: "
        f"{'включён' if enabled else 'выключен'}\n"
        f"Пользователь: "
        f"{sender_name(user)}\n"
        f"User ID: "
        f"{user.get('id')}\n"
        f"Connection ID: "
        f"{connection.get('id')}\n\n"
        f"Права:\n"
        f"{json.dumps(rights, ensure_ascii=False)}"
    )

    await admin_text(
        text
    )


# ============================================================
# NORMAL BOT MESSAGE
# ============================================================

async def process_reply_target(
    message: dict,
    source_type: str,
) -> None:
    """
    If an incoming message is a reply, Telegram may include the
    replied-to Message object in reply_to_message. For ephemeral
    messages the Bot API also exposes ephemeral_message_id.

    We do not try to reconstruct an already destroyed message by
    guessing IDs. We copy the actual reply target while Telegram
    still exposes it in the update.
    """
    target = message.get("reply_to_message")

    if not isinstance(target, dict):
        return

    target = dict(target)

    if message.get("business_connection_id") and not target.get(
        "business_connection_id"
    ):
        target["business_connection_id"] = message.get(
            "business_connection_id"
        )

    ephemeral_id = target.get("ephemeral_message_id")
    if ephemeral_id is None:
        ephemeral_id = message.get("reply_to_message", {}).get(
            "ephemeral_message_id"
        )

    target_type = media_type(target)
    if target_type == "text" and not message_text(target):
        return

    copied_id = await copy_original(target)

    event = {
        "type": "ephemeral_reply" if ephemeral_id is not None else "reply_target",
        "time": now(),
        "chat": chat_title(get_chat(message)),
        "chat_id": get_chat(message).get("id"),
        "message_id": target.get("message_id"),
        "ephemeral_message_id": ephemeral_id,
        "sender": sender_name(target.get("from")),
        "text": message_text(target),
        "media": target_type,
        "source": source_type,
        "copied_message_id": copied_id,
    }

    async with event_lock:
        events_memory.append(event)

        key = message_key(target, source_type)
        known_messages[key] = {
            "source_type": source_type,
            "business_connection_id": target.get("business_connection_id"),
            "chat_id": get_chat(target).get("id"),
            "chat_title": chat_title(get_chat(target)),
            "message_id": target.get("message_id"),
            "sender": sender_name(target.get("from")),
            "sender_id": (
                target.get("from", {}).get("id")
                if target.get("from")
                else None
            ),
            "date": target.get("date"),
            "text": message_text(target),
            "media_type": target_type,
            "ephemeral_message_id": ephemeral_id,
            "copied_message_id": copied_id,
            "saved_at": now(),
        }

    prefix = (
        "⏱ ЭФЕМЕРНОЕ СООБЩЕНИЕ ПО REPLY"
        if ephemeral_id is not None
        else "↩️ REPLY НА СООБЩЕНИЕ"
    )

    notification = (
        f"{prefix}\n\n"
        f"Чат: {chat_title(get_chat(message))}\n"
        f"Message ID: {target.get('message_id')}\n"
        f"Ephemeral ID: {ephemeral_id or 'нет'}\n"
        f"Тип: {target_type}"
    )

    if copied_id:
        notification += "\n\n💾 Содержимое скопировано выше."
    else:
        notification += (
            "\n\n⚠️ Telegram передал reply, но copyMessage "
            "не смог получить исходное содержимое."
        )

    await admin_text(notification)


async def process_normal_message(
    message: dict,
    source_type: str,
    event_type: str = "new",
) -> None:

    # Не архивируем команды владельца.
    chat = get_chat(
        message
    )

    if (
        chat.get("id")
        == ADMIN_CHAT_ID
        and (
            message.get("text")
            or ""
        ).startswith("/")
    ):

        return

    await archive_message(
        message,
        source_type,
        event_type,
    )


# ============================================================
# MISTRAL SUMMARY
# ============================================================

async def make_summary(
    hours: int,
) -> str:

    if not MISTRAL_API_KEY:

        return (
            "❌ MISTRAL_API_KEY не задан."
        )

    cutoff = now() - (
        hours * 3600
    )

    async with event_lock:

        selected = [
            event
            for event in events_memory
            if event["time"] >= cutoff
        ]

    if not selected:

        return (
            "За выбранный период "
            "событий нет."
        )

    lines = []

    for event in selected:

        lines.append(
            json.dumps(
                event,
                ensure_ascii=False
            )
        )

    journal = "\n".join(
        lines
    )

    prompt = f"""
Сделай краткую точную сводку
изменений и удалений Telegram.

Период: последние {hours} часов.

Разделы:

1. Удалено
2. Изменено
3. Медиа
4. Главное

Не придумывай факты.
Если информации недостаточно,
так и напиши.

Журнал событий:

{journal}
""".strip()

    payload = {
        "model":
            MISTRAL_MODEL,

        "temperature":
            0.1,

        "max_tokens":
            2000,

        "messages": [
            {
                "role":
                    "system",

                "content":
                    (
                        "Ты делаешь краткие "
                        "фактические сводки "
                        "журналов Telegram."
                    ),
            },
            {
                "role":
                    "user",

                "content":
                    prompt,
            },
        ],
    }

    headers = {
        "Authorization":
            f"Bearer {MISTRAL_API_KEY}",

        "Content-Type":
            "application/json",
    }

    url = (
        f"{MISTRAL_BASE_URL}"
        "/chat/completions"
    )

    timeout = aiohttp.ClientTimeout(
        total=120
    )

    try:

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            async with session.post(
                url,
                headers=headers,
                json=payload,
            ) as response:

                data = await response.json()

                if response.status >= 400:

                    return (
                        "❌ Mistral API error:\n"
                        + cut(
                            json.dumps(
                                data,
                                ensure_ascii=False
                            ),
                            3000
                        )
                    )

                return (
                    data["choices"][0]
                    ["message"]["content"]
                )

    except Exception as error:

        log.exception(
            "Mistral error"
        )

        return (
            "❌ Ошибка Mistral:\n"
            f"{error}"
        )


# ============================================================
# ADMIN COMMANDS
# ============================================================

async def handle_command(
    message: dict,
) -> bool:

    chat = get_chat(
        message
    )

    if chat.get("id") != ADMIN_CHAT_ID:
        return False

    text = (
        message.get("text")
        or ""
    ).strip()

    if not text.startswith("/"):
        return False

    command = text.split()[0].lower()

    if "@" in command:

        command = command.split(
            "@",
            1
        )[0]

    # --------------------------------------------------------
    # /start
    # --------------------------------------------------------

    if command == "/start":

        await admin_text(
            "🟢 Архиватор работает.\n\n"
            "Команды:\n"
            "/summary — последние 24 часа\n"
            "/summary 6 — последние 6 часов\n"
            "/summary 24 — сутки\n"
            "/summary 168 — неделя\n"
            "/status — состояние"
        )

        return True

    # --------------------------------------------------------
    # /status
    # --------------------------------------------------------

    if command == "/status":

        async with event_lock:

            event_count = len(
                events_memory
            )

            message_count = len(
                known_messages
            )

        await admin_text(
            "📊 STATUS\n\n"
            f"Событий в RAM: "
            f"{event_count}\n"
            f"Сообщений в RAM: "
            f"{message_count}\n"
            f"Mistral: "
            f"{'ON' if MISTRAL_API_KEY else 'OFF'}\n"
            f"PORT: {PORT}"
        )

        return True

    # --------------------------------------------------------
    # /summary
    # --------------------------------------------------------

    if command == "/summary":

        parts = text.split()

        hours = 24

        if len(parts) >= 2:

            try:

                hours = int(
                    parts[1]
                )

            except ValueError:

                await admin_text(
                    "Использование:\n"
                    "/summary\n"
                    "/summary 6\n"
                    "/summary 24\n"
                    "/summary 168"
                )

                return True

        hours = max(
            1,
            min(hours, 24 * 30)
        )

        await admin_text(
            "⏳ Формирую сводку..."
        )

        result = await make_summary(
            hours
        )

        await admin_text(
            "🤖 СВОДКА\n\n"
            + result
        )

        return True

    return False


# ============================================================
# UPDATE ROUTER
# ============================================================

async def process_update(
    update: dict,
) -> None:

    # --------------------------------------------------------
    # Our own admin commands
    # --------------------------------------------------------

    message = update.get(
        "message"
    )

    if message:

        handled = await handle_command(
            message
        )

        if handled:
            return

    # --------------------------------------------------------
    # Business connection
    # --------------------------------------------------------

    connection = update.get(
        "business_connection"
    )

    if connection:

        await process_business_connection(
            connection
        )

        return

    # --------------------------------------------------------
    # Business message
    # --------------------------------------------------------

    business_message = update.get(
        "business_message"
    )

    if business_message:

        await process_reply_target(
            business_message,
            "business",
        )

        await process_normal_message(
            business_message,
            "business",
            "new",
        )

        return

    # --------------------------------------------------------
    # Business edited message
    # --------------------------------------------------------

    edited_business_message = update.get(
        "edited_business_message"
    )

    if edited_business_message:

        await process_reply_target(
            edited_business_message,
            "business",
        )

        await process_normal_message(
            edited_business_message,
            "business",
            "edit",
        )

        return

    # --------------------------------------------------------
    # Business deleted messages
    # --------------------------------------------------------

    deleted_business_messages = (
        update.get(
            "deleted_business_messages"
        )
    )

    if deleted_business_messages:

        await process_deleted_business(
            deleted_business_messages
        )

        return

    # --------------------------------------------------------
    # Normal channel post
    # --------------------------------------------------------

    channel_post = update.get(
        "channel_post"
    )

    if channel_post:

        await process_reply_target(
            channel_post,
            "channel",
        )

        await process_normal_message(
            channel_post,
            "channel",
            "new",
        )

        return

    # --------------------------------------------------------
    # Edited channel post
    # --------------------------------------------------------

    edited_channel_post = update.get(
        "edited_channel_post"
    )

    if edited_channel_post:

        await process_reply_target(
            edited_channel_post,
            "channel",
        )

        await process_normal_message(
            edited_channel_post,
            "channel",
            "edit",
        )

        return

    # --------------------------------------------------------
    # Normal group/private message
    # --------------------------------------------------------

    normal_message = update.get(
        "message"
    )

    if normal_message:

        await process_reply_target(
            normal_message,
            "normal",
        )

        await process_normal_message(
            normal_message,
            "normal",
            "new",
        )

        return

    # --------------------------------------------------------
    # Edited normal message
    # --------------------------------------------------------

    edited_message = update.get(
        "edited_message"
    )

    if edited_message:

        await process_reply_target(
            edited_message,
            "normal",
        )

        await process_normal_message(
            edited_message,
            "normal",
            "edit",
        )

        return


# ============================================================
# FASTAPI APP
# ============================================================

# The app must exist before route decorators are evaluated.
app = FastAPI(
    title="Telegram Archive",
)


# ============================================================
# WEBHOOK
# ============================================================

@app.post("/webhook")
async def webhook(
    request: Request,
):

    try:

        update = await request.json()

    except Exception:

        return {
            "ok": False,
            "error": "invalid_json",
        }

    try:

        await process_update(
            update
        )

    except Exception:

        log.exception(
            "Update processing failed"
        )

        # Возвращаем 200, чтобы Telegram
        # не зацикливал повторную доставку
        # из-за ошибки нашего обработчика.

    return {
        "ok": True
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/")
async def root():

    return {
        "ok": True,
        "service":
            "telegram-archive",
    }


@app.get("/health")
async def health():

    return {
        "ok": True,
        "service":
            "telegram-archive",
        "events_in_memory":
            len(events_memory),
    }


# ============================================================
# WEBHOOK SETUP
# ============================================================

async def configure_webhook():

    render_url = os.getenv(
        "RENDER_EXTERNAL_URL"
    )

    if not render_url:

        log.warning(
            "RENDER_EXTERNAL_URL "
            "is not set. "
            "Set webhook manually."
        )

        return

    webhook_url = (
        render_url.rstrip("/")
        + "/webhook"
    )

    result = await telegram(
        "setWebhook",
        {
            "url":
                webhook_url,

            "allowed_updates": [
                "message",
                "edited_message",

                "channel_post",
                "edited_channel_post",

                "business_connection",
                "business_message",
                "edited_business_message",
                "deleted_business_messages",
            ],

            "drop_pending_updates":
                False,
        },
    )

    log.info(
        "Webhook: %s",
        webhook_url,
    )

    log.info(
        "setWebhook result: %s",
        result,
    )


# ============================================================
# STARTUP
# ============================================================

@asynccontextmanager
async def lifespan(
    app: FastAPI,
):

    log.info(
        "Starting Telegram Archive"
    )

    try:

        me = await telegram(
            "getMe"
        )

        log.info(
            "Bot: @%s, id=%s",
            me.get("username"),
            me.get("id"),
        )

        await configure_webhook()

    except Exception:

        log.exception(
            "Telegram startup error"
        )

    yield

    log.info(
        "Stopping Telegram Archive"
    )


# Attach the lifespan after the route declarations.
# This keeps the FastAPI app defined before @app.post/@app.get decorators.
app.router.lifespan_context = lifespan
