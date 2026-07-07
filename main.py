"""
main.py — точка входа.

Запускает:
 1. Telegram-бота (aiogram, long polling) — хендлеры для клиенток и админки
 2. aiohttp веб-сервер — отдаёт WebApp (статику) и REST API для него
 3. Фоновую задачу напоминаний (за день до визита)
 4. Self-ping задачу, чтобы реже засыпать на Render free tier

Всё в одном процессе/порту, чтобы Render видел открытый порт и не "ронял" сервис.
"""
import asyncio
import json
import logging
from datetime import date, datetime, timedelta

from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton,
    MenuButtonWebApp, BotCommand
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
from webapp_auth import validate_init_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("booking_bot")

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()


# ============================================================
#  БОТ: команды и хендлеры
# ============================================================

def is_admin(tg_id: int) -> bool:
    return tg_id in config.ADMIN_IDS


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await db.upsert_client(
        message.from_user.id, message.from_user.full_name, message.from_user.username
    )
    if not config.WEBAPP_URL:
        await message.answer(
            "Запись пока недоступна — администратор не настроил WebApp."
        )
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="Поставить запись", web_app=WebAppInfo(url=config.WEBAPP_URL))
    kb.adjust(1)

    text = (
        f"Команды:\n"
        "/my — мои записи (перенос/отмена)\n"
        "/start — открыть запись заново"
    )
    await message.answer(text, reply_markup=kb.as_markup())


@dp.message(Command("my"))
async def cmd_my_bookings(message: Message):
    bookings = await db.get_client_bookings(message.from_user.id)
    if not bookings:
        await message.answer("Пока нет активных записей \nНапиши /start, чтобы добавить запись.")
        return

    for b in bookings:
        text = (
            f"📅 {format_date_ru(b['slot_date'])} в {b['slot_time']}\n"
            f"{('Услуга: ' + b['service']) if b.get('service') else ''}"
        ).strip()
        kb = InlineKeyboardBuilder()
        kb.button(text="🔄 Перенести", callback_data=f"resched:{b['id']}")
        kb.button(text="❌ Отменить", callback_data=f"cancel:{b['id']}")
        kb.adjust(2)
        await message.answer(text, reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(callback: CallbackQuery):
    booking_id = int(callback.data.split(":")[1])
    booking = await db.get_booking(booking_id)
    if not booking or booking["client_tg_id"] != callback.from_user.id:
        await callback.answer("Запись не найдена", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="Да, отменить", callback_data=f"cancel_yes:{booking_id}")
    kb.button(text="Не надо", callback_data="noop")
    kb.adjust(2)
    await callback.message.edit_text(
        f"Точно отменить запись на {format_date_ru(booking['slot_date'])} в {booking['slot_time']}?",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("cancel_yes:"))
async def cb_cancel_confirm(callback: CallbackQuery):
    booking_id = int(callback.data.split(":")[1])
    booking = await db.get_booking(booking_id)
    if not booking or booking["client_tg_id"] != callback.from_user.id:
        await callback.answer("Запись не найдена", show_alert=True)
        return

    await db.cancel_booking(booking_id)
    await callback.message.edit_text("Запись отменена.")
    await callback.answer()

    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"❌ Отмена записи\n{booking['client_name']} "
                f"(@{booking['client_username'] or '—'})\n"
                f"{format_date_ru(booking['slot_date'])} в {booking['slot_time']}",
            )
        except Exception:
            pass


@dp.callback_query(F.data.startswith("resched:"))
async def cb_reschedule_start(callback: CallbackQuery):
    booking_id = int(callback.data.split(":")[1])
    booking = await db.get_booking(booking_id)
    if not booking or booking["client_tg_id"] != callback.from_user.id:
        await callback.answer("Запись не найдена", show_alert=True)
        return

    free_slots = await db.get_free_slots(from_date=date.today().isoformat())
    if not free_slots:
        await callback.answer("Свободных слотов пока нет", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for s in free_slots[:30]:
        label = f"{format_date_ru(s['slot_date'])} {s['slot_time']}"
        kb.button(text=label, callback_data=f"resched_to:{booking_id}:{s['id']}")
    kb.adjust(2)
    await callback.message.edit_text("На какое время перенести запись?", reply_markup=kb.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("resched_to:"))
async def cb_reschedule_apply(callback: CallbackQuery):
    _, booking_id, new_slot_id = callback.data.split(":")
    booking_id, new_slot_id = int(booking_id), int(new_slot_id)

    booking = await db.get_booking(booking_id)
    if not booking or booking["client_tg_id"] != callback.from_user.id:
        await callback.answer("Запись не найдена", show_alert=True)
        return

    ok = await db.reschedule_booking(booking_id, new_slot_id)
    if not ok:
        await callback.answer("Этот слот только что заняли, выбери другой", show_alert=True)
        return

    new_slot = await db.get_slot(new_slot_id)
    await callback.message.edit_text(
        f"Перенесено на {format_date_ru(new_slot['slot_date'])} в {new_slot['slot_time']} 🌸"
    )
    await callback.answer("Готово!")

    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🔄 Перенос записи\n{booking['client_name']} (@{booking['client_username'] or '—'})\n"
                f"Было: {format_date_ru(booking['slot_date'])} {booking['slot_time']}\n"
                f"Стало: {format_date_ru(new_slot['slot_date'])} {new_slot['slot_time']}",
            )
        except Exception:
            pass


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer()


# ---------- Админка ----------

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    text = (
        "🛠 Админ-команды:\n\n"
        "/addslot ДД.ММ.ГГГГ ЧЧ:ММ — добавить один слот\n"
        "/addday ДД.ММ.ГГГГ ЧЧ:ММ ЧЧ:ММ шаг_мин — добавить слоты на день "
        "(пример: /addday 05.07.2026 10:00 18:00 60)\n"
        "/today — записи на сегодня\n"
        "/week — записи на ближайшую неделю\n"
    )
    await message.answer(text)


@dp.message(Command("addslot"))
async def cmd_addslot(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Формат: /addslot ДД.ММ.ГГГГ ЧЧ:ММ")
        return
    try:
        d = datetime.strptime(parts[1], "%d.%m.%Y").date().isoformat()
        t = datetime.strptime(parts[2], "%H:%M").time().strftime("%H:%M")
    except ValueError:
        await message.answer("Не понял дату/время. Пример: /addslot 05.07.2026 14:30")
        return
    added = await db.add_slot(d, t)
    await message.answer("✅ Слот добавлен" if added else "⚠️ Такой слот уже есть")


@dp.message(Command("addday"))
async def cmd_addday(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 5:
        await message.answer("Формат: /addday ДД.ММ.ГГГГ ЧЧ:ММ ЧЧ:ММ шаг_мин\nПример: /addday 05.07.2026 10:00 18:00 60")
        return
    try:
        d = datetime.strptime(parts[1], "%d.%m.%Y").date().isoformat()
        start_t = datetime.strptime(parts[2], "%H:%M")
        end_t = datetime.strptime(parts[3], "%H:%M")
        step = int(parts[4])
    except ValueError:
        await message.answer("Не понял параметры. Пример: /addday 05.07.2026 10:00 18:00 60")
        return

    slots = []
    cur = start_t
    while cur < end_t:
        slots.append((d, cur.strftime("%H:%M")))
        cur += timedelta(minutes=step)

    added = await db.add_slots_bulk(slots)
    await message.answer(f"✅ Добавлено {added} слотов на {parts[1]} (всего сгенерировано {len(slots)})")


@dp.message(Command("today"))
async def cmd_today(message: Message):
    if not is_admin(message.from_user.id):
        return
    today = date.today().isoformat()
    bookings = [b for b in await db.get_all_active_bookings(from_date=today) if b["slot_date"] == today]
    if not bookings:
        await message.answer("На сегодня записей нет 🌸")
        return
    lines = ["📅 Записи на сегодня:\n"]
    for b in bookings:
        lines.append(
            f"{b['slot_time']} — {b['client_name']} (@{b['client_username'] or '—'})"
            + (f", {b['service']}" if b.get("service") else "")
            + (f", тел: {b['phone']}" if b.get("phone") else "")
        )
    await message.answer("\n".join(lines))


@dp.message(Command("week"))
async def cmd_week(message: Message):
    if not is_admin(message.from_user.id):
        return
    today = date.today().isoformat()
    bookings = await db.get_all_active_bookings(from_date=today)
    week_end = (date.today() + timedelta(days=7)).isoformat()
    bookings = [b for b in bookings if b["slot_date"] <= week_end]
    if not bookings:
        await message.answer("На ближайшую неделю записей нет 🌸")
        return
    lines = ["📅 Записи на неделю:\n"]
    cur_date = None
    for b in bookings:
        if b["slot_date"] != cur_date:
            cur_date = b["slot_date"]
            lines.append(f"\n— {format_date_ru(cur_date)} —")
        lines.append(
            f"{b['slot_time']} — {b['client_name']} (@{b['client_username'] or '—'})"
            + (f", {b['service']}" if b.get("service") else "")
        )
    await message.answer("\n".join(lines))


def format_date_ru(iso_date: str) -> str:
    d = datetime.strptime(iso_date, "%Y-%m-%d")
    months = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
    weekdays = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    return f"{d.day} {months[d.month-1]} ({weekdays[d.weekday()]})"


# ============================================================
#  WEB-СЕРВЕР: API для WebApp + раздача статики
# ============================================================

def require_valid_user(request: web.Request, body: dict) -> dict | None:
    """Достаёт и проверяет initData, переданный из WebApp. Возвращает user dict или None."""
    init_data = body.get("initData", "")
    parsed = validate_init_data(init_data, config.BOT_TOKEN)
    if not parsed:
        return None
    user_raw = parsed.get("user")
    if not user_raw:
        return None
    return json.loads(user_raw)


async def api_get_slots(request: web.Request):
    today = date.today().isoformat()
    slots = await db.get_free_slots(from_date=today)
    return web.json_response({"slots": slots})


async def api_get_my_bookings(request: web.Request):
    body = await request.json()
    user = require_valid_user(request, body)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)

    user_is_admin = is_admin(user["id"])

    if user_is_admin:
        today = date.today().isoformat()
        bookings = await db.get_all_active_bookings(from_date=today)
    else:
        bookings = await db.get_client_bookings(user["id"])

    # Передаем список записей + флаг, админ это или нет
    return web.json_response({
        "bookings": bookings,
        "is_admin": user_is_admin
    })


async def api_create_booking(request: web.Request):
    body = await request.json()
    user = require_valid_user(request, body)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)

    slot_id = body.get("slot_id")
    service = (body.get("service") or "").strip()[:200]
    phone = (body.get("phone") or "").strip()[:30]

    if not slot_id:
        return web.json_response({"error": "slot_id required"}, status=400)

    name = (user.get("first_name", "") + " " + user.get("last_name", "")).strip() or "Клиентка"
    username = user.get("username")

    await db.upsert_client(user["id"], name, username, phone or None)
    booking_id = await db.create_booking(slot_id, user["id"], name, username, service or None, phone or None)

    if booking_id is None:
        return web.json_response({"error": "slot_taken"}, status=409)

    slot = await db.get_slot(slot_id)
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🆕 Новая запись!"
                f"{format_date_ru(slot['slot_date'])} в {slot['slot_time']}"
                + (f"\nУслуга: {service}" if service else "")
                + (f"\nИмя: {phone}" if phone else ""),
            )
        except Exception:
            pass

    return web.json_response({"ok": True, "booking_id": booking_id})


async def api_cancel_booking(request: web.Request):
    body = await request.json()
    user = require_valid_user(request, body)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)

    booking_id = body.get("booking_id")
    booking = await db.get_booking(booking_id) if booking_id else None
    if not booking or booking["client_tg_id"] != user["id"]:
        return web.json_response({"error": "not_found"}, status=404)

    await db.cancel_booking(booking_id)
    return web.json_response({"ok": True})


async def api_reschedule_booking(request: web.Request):
    body = await request.json()
    user = require_valid_user(request, body)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)

    booking_id = body.get("booking_id")
    new_slot_id = body.get("new_slot_id")
    booking = await db.get_booking(booking_id) if booking_id else None
    if not booking or booking["client_tg_id"] != user["id"]:
        return web.json_response({"error": "not_found"}, status=404)

    ok = await db.reschedule_booking(booking_id, new_slot_id)
    if not ok:
        return web.json_response({"error": "slot_taken"}, status=409)
    return web.json_response({"ok": True})


async def healthcheck(request: web.Request):
    return web.json_response({"status": "ok", "time": datetime.now().isoformat()})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", healthcheck)
    app.router.add_get("/api/slots", api_get_slots)
    app.router.add_post("/api/my_bookings", api_get_my_bookings)
    app.router.add_post("/api/book", api_create_booking)
    app.router.add_post("/api/cancel", api_cancel_booking)
    app.router.add_post("/api/reschedule", api_reschedule_booking)
    app.router.add_static("/webapp/", path="webapp", name="webapp", show_index=True)
    return app


# ============================================================
#  Фоновые задачи
# ============================================================

async def reminder_loop():
    """Раз в час проверяет, кому завтра нужно напомнить о записи."""
    while True:
        try:
            tomorrow = (date.today() + timedelta(days=1)).isoformat()
            bookings = await db.get_bookings_needing_reminder(tomorrow)
            for b in bookings:
                try:
                    await bot.send_message(
                        b["client_tg_id"],
                        f"🌸 Напоминание: завтра, {format_date_ru(b['slot_date'])}, "
                        f"в {b['slot_time']} у тебя запись"
                        + (f" ({b['service']})" if b.get("service") else "")
                        + ".\nЕсли планы изменились — напиши /my, чтобы перенести или отменить.",
                    )
                    await db.mark_reminded(b["id"])
                except Exception as e:
                    log.warning(f"Не удалось отправить напоминание {b['id']}: {e}")
        except Exception as e:
            log.exception(f"Ошибка в reminder_loop: {e}")
        await asyncio.sleep(3600)


async def self_ping_loop():
    """Пингует сам себя каждые 10 минут, чтобы реже засыпать на Render free tier."""
    if not config.BASE_URL:
        return
    await asyncio.sleep(30)
    async with ClientSession() as session:
        while True:
            try:
                async with session.get(f"{config.BASE_URL}/health", timeout=10) as resp:
                    log.info(f"self-ping: {resp.status}")
            except Exception as e:
                log.warning(f"self-ping failed: {e}")
            await asyncio.sleep(600)


async def set_bot_menu():
    await bot.set_my_commands([
        BotCommand(command="start", description="Записаться"),
        BotCommand(command="my", description="Мои записи"),
    ])
    if config.WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Запись", web_app=WebAppInfo(url=config.WEBAPP_URL)))
        except Exception as e:
            log.warning(f"Не удалось установить menu button: {e}")


# ============================================================
#  Запуск
# ============================================================

async def main():
    await db.init_db()
    await set_bot_menu()

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info(f"Веб-сервер запущен на порту {config.PORT}")

    asyncio.create_task(reminder_loop())
    asyncio.create_task(self_ping_loop())

    log.info("Бот запущен, начинаю polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
