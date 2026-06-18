# -*- coding: utf-8 -*-
"""
Пост-бот для рассылки по каналам.

Логика:
  /start -> выбираешь предмет -> шлёшь сообщения (любой формат) ->
  жмёшь «Готово» -> бот рассылает:
    • в ТВОЙ канал (MY_CHANNEL)        — ВСЕ сообщения (все предметы)
    • в каналы друга (FRIEND_CHANNELS) — по предметам

  В конце каждого сообщения добавляется водяной знак
  (свой для твоего канала, свой для каналов друга).

Только админы (config.ADMINS) могут пользоваться ботом.
Рассылка по разным каналам идёт параллельно -> быстро.
"""

import asyncio
import logging
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
)

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("postbot")

bot = Bot(
    token=config.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


# ===================== Сессии пользователей =====================
# Каждый админ собирает свою рассылку независимо.
def new_session():
    return {
        "current": None,   # выбранный сейчас предмет (ключ)
        # items: список элементов по порядку. Каждый элемент:
        #   {"subject": ключ, "messages": [Message, ...]}
        # одиночное сообщение -> messages длиной 1, альбом -> несколько.
        "items": [],
    }


sessions = {}  # user_id -> session

# Буфер для сборки альбомов (сообщения альбома приходят по одному).
# (user_id, media_group_id) -> {"messages": [...], "subject": ключ}
album_buffers = {}

# История рассылок (чтобы можно было удалить отправленное во всех каналах).
# user_id -> deque последних рассылок. Каждая: {"id", "sent": [(chat_id, msg_id), ...]}
broadcasts = defaultdict(lambda: deque(maxlen=20))
_broadcast_counter = 0


def _next_broadcast_id() -> int:
    global _broadcast_counter
    _broadcast_counter += 1
    return _broadcast_counter


def get_session(user_id: int):
    if user_id not in sessions:
        sessions[user_id] = new_session()
    return sessions[user_id]


# ===================== Клавиатуры =====================
def subjects_keyboard(current=None):
    rows = []
    keys = list(config.SUBJECTS.keys())
    # по 2 кнопки в ряд
    for i in range(0, len(keys), 2):
        row = []
        for key in keys[i:i + 2]:
            title = config.SUBJECTS[key]
            mark = "✅ " if key == current else ""
            row.append(InlineKeyboardButton(
                text=f"{mark}{title}",
                callback_data=f"subj:{key}",
            ))
        rows.append(row)
    rows.append([
        InlineKeyboardButton(text="🚀 Готово (разослать)", callback_data="done"),
    ])
    rows.append([
        InlineKeyboardButton(text="🗑 Сбросить", callback_data="reset"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def session_summary(session) -> str:
    items = session["items"]
    if not items:
        return "Пока сообщений нет."
    total_msgs = sum(len(it["messages"]) for it in items)
    per_subject = defaultdict(int)
    for it in items:
        per_subject[it["subject"]] += len(it["messages"])
    lines = [f"📦 Собрано сообщений: <b>{total_msgs}</b>"]
    for key, n in per_subject.items():
        lines.append(f"• {config.SUBJECTS.get(key, key)}: {n}")
    return "\n".join(lines)


# ===================== Доступ только админам =====================
def is_admin(user_id: int) -> bool:
    return user_id in config.ADMINS


# ===================== Хендлеры =====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id):
        return  # молча игнорируем чужих
    sessions[message.from_user.id] = new_session()
    await message.answer(
        "👋 Привет! Выбери предмет, затем пришли сообщения "
        "(текст, фото, видео, документы — любой формат).\n\n"
        "Можешь переключать предметы и слать ещё.\n"
        "Когда закончишь — жми <b>«Готово»</b>.",
        reply_markup=subjects_keyboard(),
    )


@dp.callback_query(F.data.startswith("subj:"))
async def on_subject(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    key = call.data.split(":", 1)[1]
    if key not in config.SUBJECTS:
        await call.answer("Неизвестный предмет", show_alert=True)
        return
    session = get_session(call.from_user.id)
    session["current"] = key
    try:
        await call.message.edit_reply_markup(reply_markup=subjects_keyboard(key))
    except Exception:
        pass
    await call.answer(f"Предмет: {config.SUBJECTS[key]}")
    await call.message.answer(
        f"✍️ Выбран предмет: <b>{config.SUBJECTS[key]}</b>.\n"
        f"Шли сообщения для него."
    )


@dp.callback_query(F.data == "reset")
async def on_reset(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    sessions[call.from_user.id] = new_session()
    await call.answer("Сброшено")
    await call.message.answer("🗑 Всё очищено. Выбери предмет заново.",
                              reply_markup=subjects_keyboard())


@dp.callback_query(F.data == "done")
async def on_done(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    session = get_session(call.from_user.id)
    if not session["items"]:
        await call.answer("Нет сообщений для рассылки", show_alert=True)
        return
    await call.answer("Рассылаю…")
    status = await call.message.answer("⏳ Рассылаю по каналам…")
    result, sent_all = await broadcast(session)
    sessions[call.from_user.id] = new_session()

    # сохраняем рассылку, чтобы можно было удалить
    bid = _next_broadcast_id()
    broadcasts[call.from_user.id].append({"id": bid, "sent": sent_all})

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="❌ Удалить эту рассылку во всех каналах",
            callback_data=f"del:{bid}",
        )
    ]])
    await status.edit_text("✅ Готово!\n\n" + result, reply_markup=kb)


@dp.callback_query(F.data.startswith("del:"))
async def on_delete(call: CallbackQuery):
    """Первый шаг: показать подтверждение."""
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    bid = int(call.data.split(":", 1)[1])
    record = next((b for b in broadcasts[call.from_user.id] if b["id"] == bid), None)
    if not record:
        await call.answer("Эта рассылка уже недоступна", show_alert=True)
        return
    if not record["sent"]:
        await call.answer("Нечего удалять", show_alert=True)
        return
    count = len(record["sent"])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"delyes:{bid}"),
        InlineKeyboardButton(text="↩️ Отмена", callback_data=f"delno:{bid}"),
    ]])
    await call.answer()
    await call.message.edit_text(
        f"⚠️ Точно удалить эту рассылку?\n"
        f"Будет удалено <b>{count}</b> сообщений во всех каналах. "
        f"Это действие необратимо.",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("delno:"))
async def on_delete_cancel(call: CallbackQuery):
    """Отмена удаления — возвращаем кнопку удаления."""
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    bid = int(call.data.split(":", 1)[1])
    record = next((b for b in broadcasts[call.from_user.id] if b["id"] == bid), None)
    await call.answer("Отменено")
    if record and record["sent"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="❌ Удалить эту рассылку во всех каналах",
                callback_data=f"del:{bid}",
            )
        ]])
        await call.message.edit_text("✅ Рассылка отправлена.", reply_markup=kb)
    else:
        await call.message.edit_text("✅ Рассылка отправлена.")


@dp.callback_query(F.data.startswith("delyes:"))
async def on_delete_confirm(call: CallbackQuery):
    """Подтверждено: удаляем во всех каналах."""
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    bid = int(call.data.split(":", 1)[1])
    record = next((b for b in broadcasts[call.from_user.id] if b["id"] == bid), None)
    if not record or not record["sent"]:
        await call.answer("Уже недоступно", show_alert=True)
        await call.message.edit_text("ℹ️ Эта рассылка уже удалена или недоступна.")
        return
    await call.answer("Удаляю…")
    await call.message.edit_text("⏳ Удаляю сообщения во всех каналах…")
    result = await delete_broadcast(record)
    # помечаем как удалённую
    record["sent"] = []
    try:
        broadcasts[call.from_user.id].remove(record)
    except ValueError:
        pass
    await call.message.edit_text("🗑 Рассылка удалена!\n\n" + result)


@dp.message(Command("undo"))
async def cmd_undo(message: Message):
    """Удалить последнюю рассылку во всех каналах."""
    if not is_admin(message.from_user.id):
        return
    hist = broadcasts[message.from_user.id]
    record = None
    while hist:
        cand = hist[-1]
        if cand["sent"]:
            record = cand
            break
        hist.pop()
    if not record:
        await message.answer("Нет рассылок для удаления.")
        return
    count = len(record["sent"])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"delyes:{record['id']}"),
        InlineKeyboardButton(text="↩️ Отмена", callback_data=f"delno:{record['id']}"),
    ]])
    await message.answer(
        f"⚠️ Удалить последнюю рассылку?\n"
        f"Будет удалено <b>{count}</b> сообщений во всех каналах. "
        f"Это действие необратимо.",
        reply_markup=kb,
    )


async def _flush_album(user_id: int, mgid: str):
    """Через паузу собрать все части альбома в один элемент."""
    await asyncio.sleep(1.0)  # ждём пока придут все части альбома
    buf = album_buffers.pop((user_id, mgid), None)
    if not buf or not buf["messages"]:
        return
    session = get_session(user_id)
    session["items"].append({
        "subject": buf["subject"],
        "messages": buf["messages"],
    })


# Любое сообщение от админа (кроме команд) — собираем в текущий предмет.
@dp.message(F.from_user.id.in_(set(config.ADMINS)))
async def collect(message: Message):
    if message.text and message.text.startswith("/"):
        return  # команды не собираем
    session = get_session(message.from_user.id)
    if not session["current"]:
        await message.answer("Сначала выбери предмет 👇",
                             reply_markup=subjects_keyboard())
        return

    if message.media_group_id:
        # часть альбома -> копим в буфер, отправим вместе
        key = (message.from_user.id, message.media_group_id)
        if key not in album_buffers:
            album_buffers[key] = {"messages": [], "subject": session["current"]}
            asyncio.create_task(_flush_album(message.from_user.id, message.media_group_id))
        album_buffers[key]["messages"].append(message)
    else:
        # одиночное сообщение
        session["items"].append({
            "subject": session["current"],
            "messages": [message],
        })

    # лёгкая реакция, чтобы было видно что принято (без спама ответами)
    try:
        await message.react([ReactionTypeEmoji(emoji="👍")])
    except Exception:
        pass


# ===================== Рассылка =====================
def _is_captionable(msg: Message) -> bool:
    """Есть ли у сообщения медиа, к которому можно прицепить подпись."""
    return bool(
        msg.photo or msg.video or msg.document or msg.audio
        or msg.animation or msg.voice
    )


async def _send_one(chat_id: int, msg: Message, watermark: str) -> int:
    """Отправить одно сообщение в канал с водяным знаком.
    Возвращает message_id отправленного сообщения."""
    if msg.text:
        # обычный текст -> переотправляем с сохранением форматирования + вотермарк
        sent = await bot.send_message(chat_id, (msg.html_text or "") + watermark)
        return sent.message_id
    elif _is_captionable(msg):
        # медиа с подписью (или без) -> копируем, подменяя подпись
        base = msg.html_text or ""  # вернёт подпись с HTML, если она есть
        caption = (base + watermark)[:1024]  # лимит подписи Telegram
        sent = await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=msg.chat.id,
            message_id=msg.message_id,
            caption=caption,
        )
        return sent.message_id
    else:
        # стикеры, голосовые-кружки, опросы, геолокация и т.п. —
        # подпись прицепить нельзя, просто копируем как есть
        sent = await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=msg.chat.id,
            message_id=msg.message_id,
        )
        return sent.message_id


def _build_input_media(msg: Message, caption=None):
    """Собрать InputMedia из сообщения для отправки в составе альбома."""
    pm = ParseMode.HTML if caption else None
    if msg.photo:
        return InputMediaPhoto(media=msg.photo[-1].file_id, caption=caption, parse_mode=pm)
    if msg.video:
        return InputMediaVideo(media=msg.video.file_id, caption=caption, parse_mode=pm)
    if msg.document:
        return InputMediaDocument(media=msg.document.file_id, caption=caption, parse_mode=pm)
    if msg.audio:
        return InputMediaAudio(media=msg.audio.file_id, caption=caption, parse_mode=pm)
    return None  # тип не поддерживается в альбоме


async def _send_album(chat_id: int, msgs, watermark: str):
    """Отправить несколько файлов ОДНОЙ группой с водяным знаком на тексте.
    Возвращает список message_id."""
    # к какому файлу прицеплена подпись (текст). Обычно к последнему.
    cap_idx = next((i for i, m in enumerate(msgs) if m.caption), None)
    if cap_idx is None:
        cap_idx = len(msgs) - 1  # текста не было — повесим вотермарк на последний

    media = []
    for i, m in enumerate(msgs):
        if i == cap_idx:
            base = m.html_text or ""
            caption = (base + watermark)[:1024]
        else:
            caption = None
        im = _build_input_media(m, caption)
        if im is None:
            # есть неподдерживаемый тип — откатываемся на поштучную отправку
            raise ValueError("unsupported media in album")
        media.append(im)

    sent = await bot.send_media_group(chat_id, media)
    return [m.message_id for m in sent]


async def _send_item(chat_id: int, item, watermark: str):
    """Отправить один элемент (одиночное сообщение или альбом).
    Возвращает список message_id."""
    msgs = item["messages"]
    if len(msgs) == 1:
        mid = await _send_one(chat_id, msgs[0], watermark)
        return [mid]
    # альбом
    try:
        return await _send_album(chat_id, msgs, watermark)
    except Exception as e:
        # не получилось группой — шлём по одному (вотермарк на сообщение с подписью)
        log.warning("Альбом в %s не отправлен группой (%s), шлю по одному", chat_id, e)
        ids = []
        cap_idx = next((i for i, m in enumerate(msgs) if m.caption), len(msgs) - 1)
        for i, m in enumerate(msgs):
            wm = watermark if i == cap_idx else ""
            ids.append(await _send_one(chat_id, m, wm))
        return ids


async def _send_sequence(chat_id: int, items, watermark: str):
    """Отправить список элементов в один канал ПО ПОРЯДКУ.
    Возвращает (chat_id, успешно, ошибок, [message_id, ...])."""
    ok, fail = 0, 0
    sent_ids = []
    for item in items:
        try:
            ids = await _send_item(chat_id, item, watermark)
            sent_ids.extend(ids)
            ok += len(ids)
        except Exception as e:
            fail += len(item["messages"])
            log.error("Ошибка отправки в %s: %s", chat_id, e)
        # маленькая пауза, чтобы не упереться в лимиты Telegram
        await asyncio.sleep(0.05)
    return chat_id, ok, fail, sent_ids


async def broadcast(session) -> str:
    """Параллельная рассылка по всем каналам. Внутри канала — по порядку."""
    items = session["items"]

    # Раскладываем элементы по каналам, сохраняя порядок.
    # Твой канал получает ВСЕ элементы; каналы друга — по предмету.
    channel_items = defaultdict(list)
    channel_wm = {}
    for it in items:
        channel_items[config.MY_CHANNEL].append(it)
        channel_wm[config.MY_CHANNEL] = config.MY_WATERMARK
        ch = config.FRIEND_CHANNELS.get(it["subject"])
        if ch:
            channel_items[ch].append(it)
            channel_wm[ch] = config.FRIEND_WATERMARK

    # Каналы рассылаются параллельно, внутри канала — по порядку.
    tasks = [
        _send_sequence(chat_id, ch_items, channel_wm[chat_id])
        for chat_id, ch_items in channel_items.items()
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Собираем отчёт и список отправленных сообщений (для возможного удаления)
    lines = []
    sent_all = []  # [(chat_id, message_id), ...]
    for r in results:
        if isinstance(r, Exception):
            lines.append(f"❌ Ошибка канала: {r}")
            continue
        chat_id, ok, fail, sent_ids = r
        for mid in sent_ids:
            sent_all.append((chat_id, mid))
        name = "Мой канал" if chat_id == config.MY_CHANNEL else str(chat_id)
        status = f"✅ {ok}" + (f", ❌ {fail}" if fail else "")
        lines.append(f"• {name}: {status}")
    return "\n".join(lines), sent_all


async def delete_broadcast(record) -> str:
    """Удалить все сообщения рассылки во всех каналах. Быстро (пачками по 100)."""
    # группируем message_id по каналам
    by_chat = defaultdict(list)
    for chat_id, mid in record["sent"]:
        by_chat[chat_id].append(mid)

    async def _del_chat(chat_id, ids):
        ok, fail = 0, 0
        # deleteMessages умеет до 100 id за раз
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            try:
                await bot.delete_messages(chat_id, chunk)
                ok += len(chunk)
            except Exception as e:
                # если пачкой не вышло — пробуем по одному
                log.warning("Пакетное удаление в %s не удалось: %s", chat_id, e)
                for mid in chunk:
                    try:
                        await bot.delete_message(chat_id, mid)
                        ok += 1
                    except Exception as e2:
                        fail += 1
                        log.error("Не удалить %s в %s: %s", mid, chat_id, e2)
        return chat_id, ok, fail

    results = await asyncio.gather(
        *[_del_chat(c, ids) for c, ids in by_chat.items()],
        return_exceptions=True,
    )
    lines = []
    for r in results:
        if isinstance(r, Exception):
            lines.append(f"❌ {r}")
            continue
        chat_id, ok, fail = r
        name = "Мой канал" if chat_id == config.MY_CHANNEL else str(chat_id)
        status = f"🗑 {ok}" + (f", ❌ {fail}" if fail else "")
        lines.append(f"• {name}: {status}")
    return "\n".join(lines)


# ===================== Запуск =====================
async def main():
    log.info("Бот запускается…")
    # удаляем вебхук на случай конфликтов и стартуем поллинг
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен.")
