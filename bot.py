#!/usr/bin/env python3
"""
Telegram-бот для персонального тренера по хоккею
Катки: Красная Поляна (пн–пт) и Сириус (сб–вс)
"""

import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes,
)

import config
import database as db

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
TZ = ZoneInfo(config.TIMEZONE)

# ── Состояния диалогов ───────────────────────────────────────
BOOK_NAME, BOOK_DATE, BOOK_TIME, BOOK_TYPE, BOOK_CONFIRM, CONTACT_MSG = range(6)

# Состояния для панели тренера
(ADM_INPUT,) = range(10, 11)
# Что именно вводит тренер — хранится в context.user_data['adm_action']
# Возможные значения: 'block_time', 'extra_date', 'extra_time', 'add_admin', 'price_val', 'fee_val'


# ════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ════════════════════════════════════════════════════════════

def main_kb(is_admin=False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📅 Расписание",      callback_data="schedule"),
         InlineKeyboardButton("📝 Записаться",       callback_data="book")],
        [InlineKeyboardButton("💰 Стоимость",        callback_data="pricing"),
         InlineKeyboardButton("📋 Мои записи",       callback_data="my_bookings")],
        [InlineKeyboardButton("💬 Написать тренеру", callback_data="contact")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🛠 Панель тренера", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Все записи",         callback_data="adm_bookings")],
        [InlineKeyboardButton("🚫 Заблокировать слот",  callback_data="adm_block"),
         InlineKeyboardButton("➕ Доп. слот",           callback_data="adm_extra")],
        [InlineKeyboardButton("💰 Изменить цены",       callback_data="adm_prices")],
        [InlineKeyboardButton("👥 Управление админами", callback_data="adm_admins")],
        [InlineKeyboardButton("🏠 Главное меню",        callback_data="main_menu")],
    ])


def get_available_dates():
    result = []
    today = date.today()
    extra_dates = set(db.get_extra_slot_dates())

    date_set = set()
    for i in range(1, 15):
        d = today + timedelta(days=i)
        date_str = d.isoformat()
        date_set.add(date_str)
        slots = config.SCHEDULE.get(d.weekday(), [])
        extras = db.get_extra_slots(date_str)
        if slots or extras:
            label = f"{config.DAY_NAMES[d.weekday()]}, {d.strftime('%d.%m')}"
            result.append((date_str, label))

    for date_str in sorted(extra_dates - date_set):
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        if d > today:
            label = f"{config.DAY_NAMES[d.weekday()]}, {d.strftime('%d.%m')}"
            result.append((date_str, label))

    result.sort(key=lambda x: x[0])
    return result


def get_free_slots(date_str: str) -> list:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    regular = config.SCHEDULE.get(d.weekday(), [])
    extra   = db.get_extra_slots(date_str)
    all_slots = sorted(set(regular + extra))
    unavail   = db.get_unavailable_times(date_str)
    return [s for s in all_slots if s not in unavail]


def fmt_date(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return f"{config.DAY_NAMES[d.weekday()]}, {d.strftime('%d.%m.%Y')}"


def get_addr(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return config.get_address(d.weekday())


def make_ics(child: str, date_str: str, time_str: str, venue: str, training_name: str) -> bytes:
    """Генерирует .ics файл для добавления в календарь."""
    from datetime import datetime, timedelta
    dt_start = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    dt_end   = dt_start + timedelta(minutes=60)
    fmt      = "%Y%m%dT%H%M%S"
    uid      = f"{date_str}-{time_str}-{child}@hockeybot".replace(" ", "")
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Hockey Bot//RU\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTART:{dt_start.strftime(fmt)}\r\n"
        f"DTEND:{dt_end.strftime(fmt)}\r\n"
        f"SUMMARY:🏒 {training_name} — {child}\r\n"
        f"LOCATION:{venue}\r\n"
        f"DESCRIPTION:Тренер: {config.TRAINER_NAME} {config.TRAINER_PHONE}\r\n"
        "BEGIN:VALARM\r\n"
        "TRIGGER:-PT3H\r\n"
        "ACTION:DISPLAY\r\n"
        "DESCRIPTION:Напоминание о тренировке\r\n"
        "END:VALARM\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    return ics.encode("utf-8")


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Отправить сообщение всем админам."""
    for chat_id, _ in db.get_admins():
        try:
            await context.bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Не удалось уведомить админа {chat_id}: {e}")


# ════════════════════════════════════════════════════════════
#  /start  и  /myid
# ════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_admin = db.is_admin(user.id)
    name = user.first_name or "друг"

    # Обработка deep link из расписания: /start sch_2026-04-29_11-00
    if context.args and context.args[0].startswith("sch_"):
        parts    = context.args[0].split("_")  # sch / YYYY / MM / DD / HH-MM
        try:
            date_str = f"{parts[1]}-{parts[2]}-{parts[3]}"
            time_str = parts[4].replace("-", ":")
            context.user_data.clear()
            context.user_data["date"] = date_str
            context.user_data["time"] = time_str
            venue = config.get_address(
                datetime.strptime(date_str, "%Y-%m-%d").weekday()
            )
            await update.message.reply_text(
                f"📝 *Запись на тренировку*\n\n"
                f"📅 *{fmt_date(date_str)}*\n"
                f"🕐 *{time_str}*\n"
                f"📍 *{venue}*\n\n"
                "Введите имя ребёнка:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data="main_menu")
                ]])
            )
            return BOOK_NAME
        except Exception:
            pass

    text = (
        f"👋 Привет, *{name}*!\n\n"
        f"🏒 Я бот персонального тренера по хоккею — *{config.TRAINER_NAME}*\n\n"
        "Здесь ты можешь:\n"
        "• Посмотреть расписание тренировок\n"
        "• Записаться на занятие\n"
        "• Узнать стоимость\n"
        "• Написать тренеру\n\n"
        "Выбери действие 👇"
    )
    kb = main_kb(is_admin)
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"🆔 Ваш Telegram ID: `{user.id}`\n"
        f"👤 Username: @{user.username or 'не задан'}\n\n"
        "Отправьте этот ID тренеру, чтобы он добавил вас как администратора.",
        parse_mode="Markdown"
    )


async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await cmd_start(update, context)


# ════════════════════════════════════════════════════════════
#  РАСПИСАНИЕ
# ════════════════════════════════════════════════════════════

async def build_schedule_kb(dates, week: int):
    """Строит клавиатуру расписания для заданной недели."""
    week_dates = dates[week*7 : week*7+7]
    rows = []
    for date_str, label in week_dates:
        free  = get_free_slots(date_str)
        venue = get_addr(date_str)
        # Заголовок дня — некликабельный
        rows.append([InlineKeyboardButton(
            f"── {label}  {venue} ──", callback_data="noop"
        )])
        if free:
            for i in range(0, len(free), 4):
                rows.append([
                    InlineKeyboardButton(t, callback_data=f"sch_{date_str}_{t}")
                    for t in free[i:i+4]
                ])
        else:
            rows.append([InlineKeyboardButton("❌ нет мест", callback_data="noop")])

    # Навигация
    nav = []
    if week > 0:
        nav.append(InlineKeyboardButton("◀️ Прошлая неделя", callback_data=f"schw_{week-1}"))
    if (week+1)*7 < len(dates):
        nav.append(InlineKeyboardButton("Следующая неделя ▶️", callback_data=f"schw_{week+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Меню", callback_data="main_menu")])
    return rows


async def cb_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    dates = get_available_dates()
    if not dates:
        await query.edit_message_text(
            "😔 Свободных слотов нет. Свяжитесь с тренером.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Меню", callback_data="main_menu")
            ]])
        )
        return
    rows = await build_schedule_kb(dates, week=0)
    await query.edit_message_text(
        "📅 *Расписание*\n_(нажмите на время чтобы записаться)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def cb_schedule_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    week  = int(query.data.split("_")[1])
    dates = get_available_dates()
    rows  = await build_schedule_kb(dates, week=week)
    await query.edit_message_text(
        "📅 *Расписание*\n_(нажмите на время чтобы записаться)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def cb_schedule_quick_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Быстрая запись прямо из расписания — пропускаем выбор даты и времени."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()

    parts    = query.data.split("_", 2)   # sch / date / time
    date_str = parts[1]
    time_str = parts[2]

    context.user_data["date"] = date_str
    context.user_data["time"] = time_str

    await query.edit_message_text(
        f"📝 *Запись на тренировку*\n\n"
        f"📅 *{fmt_date(date_str)}*\n"
        f"🕐 *{time_str}*\n"
        f"📍 *{get_addr(date_str)}*\n\n"
        "Введите имя ребёнка:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="main_menu")
        ]])
    )
    return BOOK_NAME


# ════════════════════════════════════════════════════════════
#  СТОИМОСТЬ
# ════════════════════════════════════════════════════════════

async def cb_pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pricing, entry_fees = db.get_prices()
    text = "💰 *Стоимость тренировок:*\n"
    for venue, items in pricing.items():
        fee = entry_fees.get(venue, "")
        text += f"\n\n📍 *{venue}*\n"
        for name, price in items:
            text += f"{name}\n   ➡️ *{price}*\n"
        text += f"\n⛸ Вход на каток: *{fee}* _(оплачивается дополнительно)_"
    text += "\n\n💳 *Оплата:* наличные / перевод на карту"

    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📝 Записаться", callback_data="book"),
            InlineKeyboardButton("🏠 Меню",       callback_data="main_menu"),
        ]])
    )


# ════════════════════════════════════════════════════════════
#  ЗАПИСЬ
# ════════════════════════════════════════════════════════════

async def book_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text(
        "📝 *Запись на тренировку*\n\nВведите имя ребёнка:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="main_menu")
        ]])
    )
    return BOOK_NAME


async def book_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["child"] = update.message.text.strip()

    # Если дата и время уже выбраны (быстрая запись из расписания) — сразу к типу занятия
    if context.user_data.get("date") and context.user_data.get("time"):
        date_str = context.user_data["date"]
        venue    = get_addr(date_str)
        pricing, _ = db.get_prices()
        venue_prices = pricing.get(venue, [])
        rows = []
        for i, (name, price) in enumerate(venue_prices):
            rows.append([InlineKeyboardButton(f"{name}  —  {price}", callback_data=f"type_{i}")])
        rows.append([InlineKeyboardButton("❌ Отмена", callback_data="main_menu")])
        await update.message.reply_text(
            f"📍 *{venue}*\n\n💰 Выберите тип занятия:",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
        )
        return BOOK_TYPE

    dates = get_available_dates()
    if not dates:
        await update.message.reply_text("😔 Свободных дат нет. Обратитесь к тренеру.",
                                        reply_markup=main_kb(db.is_admin(update.effective_user.id)))
        return ConversationHandler.END

    rows = []
    for date_str, label in dates:
        if get_free_slots(date_str):
            venue = get_addr(date_str)
            rows.append([InlineKeyboardButton(f"{label} ({venue})", callback_data=f"d_{date_str}")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="main_menu")])

    await update.message.reply_text(
        f"✅ Имя: *{context.user_data['child']}*\n\n📅 Выберите дату:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )
    return BOOK_DATE


async def book_got_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    date_str = query.data[2:]
    context.user_data["date"] = date_str
    free = get_free_slots(date_str)

    if not free:
        await query.edit_message_text("😔 Все слоты заняты. Выберите другой день.")
        return BOOK_DATE

    rows = [[InlineKeyboardButton(t, callback_data=f"t_{t}")] for t in free]
    rows.append([InlineKeyboardButton("⬅️ Назад",  callback_data="book"),
                 InlineKeyboardButton("❌ Отмена", callback_data="main_menu")])

    await query.edit_message_text(
        f"📅 *{fmt_date(date_str)}*\n📍 {get_addr(date_str)}\n\n🕐 Выберите время:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )
    return BOOK_TIME


async def book_got_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["time"] = query.data[2:]

    date_str = context.user_data["date"]
    venue    = get_addr(date_str)
    pricing, _ = db.get_prices()
    venue_prices = pricing.get(venue, [])

    rows = []
    for i, (name, price) in enumerate(venue_prices):
        rows.append([InlineKeyboardButton(
            f"{name}  —  {price}",
            callback_data=f"type_{i}"
        )])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="main_menu")])

    await query.edit_message_text(
        f"📍 *{venue}*\n\n"
        "💰 Выберите тип занятия:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows)
    )
    return BOOK_TYPE


async def book_got_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    type_idx = int(query.data.split("_")[1])
    date_str = context.user_data["date"]
    venue    = get_addr(date_str)
    pricing, fees = db.get_prices()
    name, price   = pricing.get(venue, [])[type_idx]
    entry         = fees.get(venue, "")

    context.user_data["training_name"]  = name
    context.user_data["training_price"] = price

    child    = context.user_data["child"]
    time_str = context.user_data["time"]

    await query.edit_message_text(
        "📋 *Подтверждение записи:*\n\n"
        f"👦 Ребёнок:   *{child}*\n"
        f"📅 Дата:      *{fmt_date(date_str)}*\n"
        f"🕐 Время:     *{time_str}*\n"
        f"📍 Каток:     *{venue}*\n"
        f"🏠 Адрес:     {config.ICE_RINK_FULL_ADDRESSES.get(venue, venue)}\n"
        f"🏒 Занятие:   *{name}*\n"
        f"💰 Стоимость: *{price}*\n"
        f"⛸ Вход:      *{entry}* _(оплачивается отдельно)_\n\n"
        f"📞 Тренер: {config.TRAINER_USERNAME} · {config.TRAINER_PHONE}\n\n"
        "Всё верно?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm"),
             InlineKeyboardButton("❌ Отмена",      callback_data="main_menu")],
            [InlineKeyboardButton("🗺 Посмотреть на карте",
             url=config.ICE_RINK_MAP_LINKS.get(venue, "https://yandex.ru/maps/"))],
        ])
    )
    return BOOK_CONFIRM


async def book_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user     = update.effective_user
    child    = context.user_data["child"]
    date_str = context.user_data["date"]
    time_str = context.user_data["time"]
    venue    = get_addr(date_str)
    _, fees  = db.get_prices()
    entry    = fees.get(venue, "")

    booking_id = db.add_booking(user.id, user.username or user.first_name,
                                child, date_str, time_str)

    t_name  = context.user_data.get("training_name", "")
    t_price = context.user_data.get("training_price", "")

    await notify_admins(
        context,
        f"🆕 *Новая запись #{booking_id}*\n\n"
        f"👦 Ребёнок: {child}\n"
        f"👤 Родитель: @{user.username or user.first_name}\n"
        f"📅 {fmt_date(date_str)} в {time_str}\n"
        f"📍 {venue}\n"
        f"🏒 {t_name} — {t_price}"
    )

    t_name  = context.user_data.get("training_name", "")
    t_price = context.user_data.get("training_price", "")

    await query.edit_message_text(
        f"✅ *Запись подтверждена!*\n\n"
        f"👦 *{child}*\n"
        f"📅 {fmt_date(date_str)} в *{time_str}*\n"
        f"📍 *{venue}*\n"
        f"🏒 *{t_name}*\n"
        f"💰 Стоимость: *{t_price}*\n"
        f"⛸ Вход на каток: *{entry}* _(оплачивается на месте)_\n\n"
        f"📞 По всем вопросам: {config.TRAINER_USERNAME} · {config.TRAINER_PHONE}\n"
        f"🔔 Напоминание придёт за {config.REMINDER_HOURS_BEFORE} ч. до начала.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
        ]])
    )

    # Кнопка "Добавить в календарь" — отправляем .ics по нажатию
    context.user_data["pending_ics"] = {
        "child": child, "date": date_str, "time": time_str,
        "venue": venue, "name": t_name
    }
    try:
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="📅 Хотите добавить тренировку в календарь?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📅 Добавить в календарь", callback_data=f"add_cal_{booking_id}")
            ]])
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить кнопку календаря: {e}")

    return ConversationHandler.END


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await cmd_start(update, context)
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════
#  МОИ ЗАПИСИ
# ════════════════════════════════════════════════════════════

async def cb_my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    bookings = db.get_user_bookings(update.effective_user.id)
    if not bookings:
        await query.edit_message_text(
            "📋 У вас нет предстоящих записей.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📝 Записаться", callback_data="book"),
                InlineKeyboardButton("🏠 Меню",       callback_data="main_menu"),
            ]])
        )
        return

    lines = ["📋 *Ваши предстоящие записи:*\n"]
    rows  = []
    for bid, child, bdate, btime, status in bookings:
        icon = "✅" if status == "active" else "❌"
        lines.append(f"{icon} #{bid} | *{child}* | {fmt_date(bdate)} {btime} | {get_addr(bdate)}")
        if status == "active":
            rows.append([InlineKeyboardButton(
                f"❌ Отменить #{bid} ({child}, {btime})",
                callback_data=f"cancel_{bid}"
            )])

    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def cb_cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    bid   = int(query.data.split("_")[1])
    ok    = db.cancel_booking(bid, update.effective_user.id)
    await query.answer("✅ Запись отменена" if ok else "❌ Не удалось отменить", show_alert=True)
    if ok:
        await notify_admins(context,
            f"❌ Отменена запись *#{bid}*\n"
            f"Пользователь: @{update.effective_user.username}"
        )
    await cb_my_bookings(update, context)


# ════════════════════════════════════════════════════════════
#  НАПИСАТЬ ТРЕНЕРУ
# ════════════════════════════════════════════════════════════

async def contact_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        f"💬 *Написать тренеру {config.TRAINER_NAME}*\n\n"
        "Напишите ваш вопрос — ответим в ближайшее время.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="main_menu")
        ]])
    )
    return CONTACT_MSG


async def contact_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        await notify_admins(
            context,
            f"📩 *Сообщение от @{user.username or user.first_name}:*\n\n{update.message.text}"
        )
        await update.message.reply_text(
            "✅ Сообщение отправлено тренеру!\nОжидайте ответа.",
            reply_markup=main_kb(db.is_admin(user.id))
        )
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        await update.message.reply_text("❌ Не удалось отправить. Попробуйте позже.",
                                        reply_markup=main_kb())
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════
#  ПАНЕЛЬ ТРЕНЕРА — главная
# ════════════════════════════════════════════════════════════

async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not db.is_admin(update.effective_user.id):
        await query.answer("⛔ Нет доступа", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(
        "🛠 *Панель тренера*\n\nВыберите действие:",
        parse_mode="Markdown", reply_markup=admin_kb()
    )


# ── Все записи ───────────────────────────────────────────────
async def adm_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not db.is_admin(update.effective_user.id):
        await query.answer("⛔ Нет доступа", show_alert=True); return
    await query.answer()

    bookings = db.get_all_upcoming_bookings()
    if not bookings:
        text = "📋 *Нет предстоящих записей*"
        rows = [[InlineKeyboardButton("⬅️ Назад", callback_data="admin")]]
    else:
        lines = [f"📋 *Предстоящих записей: {len(bookings)}*\n"]
        rows  = []
        for bid, username, child, bdate, btime in bookings:
            lines.append(f"• #{bid} | *{child}* | {fmt_date(bdate)} {btime} | {get_addr(bdate)} | @{username}")
            rows.append([InlineKeyboardButton(
                f"❌ Отменить #{bid} ({child}, {btime})",
                callback_data=f"adm_cancel_{bid}"
            )])
        text = "\n".join(lines)
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin")])

    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(rows))


async def adm_cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not db.is_admin(update.effective_user.id):
        await query.answer("⛔ Нет доступа", show_alert=True); return

    bid = int(query.data.split("_")[2])
    row = db.get_booking_by_id(bid)
    db.admin_cancel_booking(bid)
    await query.answer(f"✅ Запись #{bid} отменена", show_alert=True)

    if row:
        _, user_id, _, child, bdate, btime = row
        try:
            await context.bot.send_message(
                user_id,
                f"❗ Ваша запись была отменена тренером.\n\n"
                f"👦 {child} | {fmt_date(bdate)} {btime}\n\n"
                "По вопросам свяжитесь с тренером.",
            )
        except Exception:
            pass

    await adm_bookings(update, context)


# ── Заблокировать слот ────────────────────────────────────────
async def adm_block_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not db.is_admin(update.effective_user.id):
        await query.answer("⛔ Нет доступа", show_alert=True); return
    await query.answer()

    dates = get_available_dates()
    rows  = [[InlineKeyboardButton(label, callback_data=f"adm_blkd_{ds}")]
             for ds, label in dates]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin")])

    await query.edit_message_text(
        "🚫 *Заблокировать слот*\n\nВыберите дату:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )


async def adm_block_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    date_str = query.data[8:]
    context.user_data["blk_date"] = date_str

    free = get_free_slots(date_str)
    if not free:
        await query.answer("Нет свободных слотов на эту дату", show_alert=True)
        return

    rows = [[InlineKeyboardButton(t, callback_data=f"adm_blkt_{t}")] for t in free]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="adm_block")])

    await query.edit_message_text(
        f"🚫 Блокировка слота\n📅 *{fmt_date(date_str)}*\n\nВыберите время:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )


async def adm_block_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    time_str = query.data[8:]
    date_str = context.user_data.get("blk_date", "")
    db.block_slot(date_str, time_str)
    await query.answer(f"✅ Слот {time_str} заблокирован", show_alert=True)
    await query.edit_message_text(
        f"🚫 Слот *{fmt_date(date_str)} {time_str}* заблокирован.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Панель", callback_data="admin")
        ]])
    )


# ── Добавить доп. слот ────────────────────────────────────────
async def adm_extra_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not db.is_admin(update.effective_user.id):
        await query.answer("⛔ Нет доступа", show_alert=True); return
    await query.answer()

    today = date.today()
    rows  = []
    for i in range(1, 15):
        d = today + timedelta(days=i)
        label = f"{config.DAY_NAMES[d.weekday()]}, {d.strftime('%d.%m')}"
        rows.append([InlineKeyboardButton(label, callback_data=f"adm_exd_{d.isoformat()}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin")])

    await query.edit_message_text(
        "➕ *Добавить дополнительный слот*\n\nВыберите дату:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )


async def adm_extra_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    date_str = query.data[8:]
    context.user_data["adm_action"] = "extra_time"
    context.user_data["extra_date"] = date_str

    await query.edit_message_text(
        f"➕ Доп. слот на *{fmt_date(date_str)}*\n\n"
        "Введите время в формате *ЧЧ:ММ* (например, `14:30`):",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Отмена", callback_data="admin")
        ]])
    )
    return ADM_INPUT


# ── Управление ценами ─────────────────────────────────────────
async def adm_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not db.is_admin(update.effective_user.id):
        await query.answer("⛔ Нет доступа", show_alert=True); return
    await query.answer()

    pricing, fees = db.get_prices()
    rows = []
    for venue, items in pricing.items():
        rows.append([InlineKeyboardButton(f"── {venue} ──", callback_data="noop")])
        for name, price in items:
            short = name.replace("🏒 ", "").replace("👥 ", "").replace("📦 ", "")
            rows.append([InlineKeyboardButton(
                f"{short[:30]}  →  {price}",
                callback_data=f"adm_pe_{venue}|{name}"
            )])
        fee = fees.get(venue, "")
        rows.append([InlineKeyboardButton(
            f"⛸ Вход {venue}: {fee}",
            callback_data=f"adm_fe_{venue}"
        )])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin")])

    await query.edit_message_text(
        "💰 *Управление ценами*\nНажмите на позицию чтобы изменить:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )


async def adm_price_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data[7:]           # venue|name
    venue, name = data.split("|", 1)
    context.user_data["adm_action"] = "price_val"
    context.user_data["price_venue"] = venue
    context.user_data["price_name"]  = name

    await query.edit_message_text(
        f"💰 *{venue}*\n{name}\n\nВведите новую цену (например, `3 000 ₽`):",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Отмена", callback_data="adm_prices")
        ]])
    )
    return ADM_INPUT


async def adm_fee_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    venue = query.data[7:]
    context.user_data["adm_action"] = "fee_val"
    context.user_data["fee_venue"]  = venue

    await query.edit_message_text(
        f"⛸ Вход на каток *{venue}*\n\nВведите новую стоимость (например, `450 ₽/чел`):",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Отмена", callback_data="adm_prices")
        ]])
    )
    return ADM_INPUT


# ── Управление админами ───────────────────────────────────────
async def adm_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not db.is_admin(update.effective_user.id):
        await query.answer("⛔ Нет доступа", show_alert=True); return
    await query.answer()

    admins = db.get_admins()
    lines  = ["👥 *Администраторы:*\n"]
    rows   = []
    for chat_id, username in admins:
        marker = "⭐" if chat_id == config.TRAINER_CHAT_ID else "👤"
        lines.append(f"{marker} @{username} (`{chat_id}`)")
        if chat_id != config.TRAINER_CHAT_ID:
            rows.append([InlineKeyboardButton(
                f"❌ Удалить @{username}",
                callback_data=f"adm_rmadm_{chat_id}"
            )])

    rows.append([InlineKeyboardButton("➕ Добавить админа", callback_data="adm_addadmin")])
    rows.append([InlineKeyboardButton("⬅️ Назад",          callback_data="admin")])

    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def adm_add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["adm_action"] = "add_admin"

    await query.edit_message_text(
        "👥 *Добавить администратора*\n\n"
        "Попросите человека написать боту команду /myid\n"
        "Он получит свой ID — введите его сюда:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Отмена", callback_data="adm_admins")
        ]])
    )
    return ADM_INPUT


async def adm_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split("_")[2])
    db.remove_admin(chat_id)
    await query.answer("✅ Админ удалён", show_alert=True)
    await adm_admins(update, context)


# ── Обработчик текстового ввода в режиме админа ───────────────
async def adm_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get("adm_action", "")
    text   = update.message.text.strip()

    if action == "extra_time":
        date_str = context.user_data.get("extra_date", "")
        import re
        if not re.match(r"^\d{2}:\d{2}$", text):
            await update.message.reply_text("❌ Неверный формат. Введите время как ЧЧ:ММ, например `14:30`:",
                                            parse_mode="Markdown")
            return ADM_INPUT
        db.add_extra_slot(date_str, text)
        await update.message.reply_text(
            f"✅ Доп. слот *{fmt_date(date_str)} {text}* добавлен!",
            parse_mode="Markdown", reply_markup=admin_kb()
        )

    elif action == "price_val":
        venue = context.user_data["price_venue"]
        name  = context.user_data["price_name"]
        db.update_price(venue, name, text)
        await update.message.reply_text(
            f"✅ Цена обновлена!\n*{name}* → *{text}*",
            parse_mode="Markdown", reply_markup=admin_kb()
        )

    elif action == "fee_val":
        venue = context.user_data["fee_venue"]
        db.update_entry_fee(venue, text)
        await update.message.reply_text(
            f"✅ Вход на каток *{venue}* → *{text}*",
            parse_mode="Markdown", reply_markup=admin_kb()
        )

    elif action == "add_admin":
        if not text.isdigit():
            await update.message.reply_text("❌ ID должен быть числом. Попробуйте ещё раз:")
            return ADM_INPUT
        new_id = int(text)
        try:
            chat = await context.bot.get_chat(new_id)
            username = chat.username or chat.first_name or str(new_id)
        except Exception:
            username = str(new_id)
        db.add_admin(new_id, username)
        try:
            await context.bot.send_message(
                new_id,
                f"✅ Вас добавили как администратора бота *{config.TRAINER_NAME}*!\n\n"
                "Теперь вам будут приходить уведомления о новых записях и отменах.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        await update.message.reply_text(
            f"✅ @{username} добавлен как администратор!",
            parse_mode="Markdown", reply_markup=admin_kb()
        )

    context.user_data.pop("adm_action", None)
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════
#  НАПОМИНАНИЯ
# ════════════════════════════════════════════════════════════

async def job_reminders(context: ContextTypes.DEFAULT_TYPE):
    _, fees = db.get_prices()
    for row in db.get_bookings_for_reminder():
        bid, user_id, child, bdate, btime = row
        venue = get_addr(bdate)
        entry = fees.get(venue, "")
        try:
            await context.bot.send_message(
                user_id,
                f"🔔 *Напоминание о тренировке!*\n\n"
                f"Сегодня в *{btime}* — занятие для *{child}*\n"
                f"📅 {fmt_date(bdate)}\n"
                f"📍 *{venue}*\n"
                f"⛸ Не забудьте оплатить вход: *{entry}*\n\n"
                f"Удачи на льду! 🏒",
                parse_mode="Markdown"
            )
            db.mark_reminder_sent(bid)
        except Exception as e:
            logger.error(f"Ошибка напоминания #{bid}: {e}")


async def job_confirmations(context: ContextTypes.DEFAULT_TYPE):
    """За 12 часов спрашивает, придёт ли ребёнок."""
    for row in db.get_bookings_for_confirmation():
        bid, user_id, child, bdate, btime = row
        venue = get_addr(bdate)
        try:
            await context.bot.send_message(
                user_id,
                f"❓ *Подтверждение тренировки*\n\n"
                f"Завтра в *{btime}* занятие для *{child}*\n"
                f"📅 {fmt_date(bdate)}\n"
                f"📍 *{venue}*\n\n"
                f"⚠️ _Отмена менее чем за 12 часов — тренировка оплачивается полностью._\n\n"
                f"Вы придёте?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Да, придём",    callback_data=f"cfm_yes_{bid}"),
                    InlineKeyboardButton("❌ Отменить запись", callback_data=f"cfm_no_{bid}"),
                ]])
            )
            db.mark_confirmation_sent(bid)
        except Exception as e:
            logger.error(f"Ошибка подтверждения #{bid}: {e}")


async def cb_confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bid = int(query.data.split("_")[2])
    row = db.get_booking_by_id(bid)
    if row:
        _, _, _, child, bdate, btime = row
        await query.edit_message_text(
            f"✅ *Отлично, ждём вас!*\n\n"
            f"👦 {child} | {fmt_date(bdate)} в *{btime}*\n"
            f"📍 {get_addr(bdate)}\n\n"
            f"🔔 Напоминание придёт за {config.REMINDER_HOURS_BEFORE} ч. до начала.",
            parse_mode="Markdown"
        )
    else:
        await query.edit_message_text("✅ Спасибо за подтверждение!")


async def cb_confirm_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bid = int(query.data.split("_")[2])
    row = db.get_booking_by_id(bid)
    db.cancel_booking_by_id(bid)
    if row:
        _, _, _, child, bdate, btime = row
        await notify_admins(
            context,
            f"❌ Отмена записи (через подтверждение)\n\n"
            f"👦 {child} | {fmt_date(bdate)} в {btime}\n"
            f"📍 {get_addr(bdate)}"
        )
        await query.edit_message_text(
            f"❌ Запись на *{fmt_date(bdate)} {btime}* отменена.\n\n"
            f"Если передумаете — запишитесь снова через бота.",
            parse_mode="Markdown"
        )
    else:
        await query.edit_message_text("❌ Запись отменена.")


# ════════════════════════════════════════════════════════════
#  ЗАПУСК
# ════════════════════════════════════════════════════════════

async def cb_add_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    ics_info = context.user_data.get("pending_ics", {})
    if not ics_info:
        await query.edit_message_text("❌ Данные не найдены. Запись уже сохранена в боте.")
        return

    try:
        ics_data = make_ics(
            ics_info["child"], ics_info["date"], ics_info["time"],
            ics_info["venue"], ics_info["name"]
        )
        from io import BytesIO
        ics_file = BytesIO(ics_data)
        fname = f"training_{ics_info['date']}_{ics_info['time'].replace(':', '-')}.ics"
        ics_file.name = fname
        await query.edit_message_text("✅ Отправляю файл календаря...")
        await context.bot.send_document(
            chat_id=update.effective_user.id,
            document=ics_file,
            caption=(
                "📅 *Как добавить в календарь:*\n\n"
                "📱 *iPhone* — нажмите на файл → Добавить в Календарь\n"
                "🤖 *Android* — нажмите на файл → открыть в Google Calendar"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Ошибка отправки .ics: {e}")
        await query.edit_message_text("❌ Не удалось создать файл. Попробуйте позже.")


def main():
    db.init_db()
    app = Application.builder().token(config.TOKEN).build()

    booking_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(book_entry,              pattern="^book$"),
            CallbackQueryHandler(cb_schedule_quick_book,  pattern=r"^sch_\d{4}-\d{2}-\d{2}_"),
            CommandHandler("start", cmd_start),
        ],
        states={
            BOOK_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, book_got_name)],
            BOOK_DATE:    [CallbackQueryHandler(book_got_date, pattern=r"^d_")],
            BOOK_TIME:    [CallbackQueryHandler(book_got_time, pattern=r"^t_")],
            BOOK_TYPE:    [CallbackQueryHandler(book_got_type, pattern=r"^type_\d+$")],
            BOOK_CONFIRM: [CallbackQueryHandler(book_confirm,  pattern="^confirm$")],
        },
        fallbacks=[CallbackQueryHandler(conv_cancel, pattern="^main_menu$"),
                   CommandHandler("start", cmd_start)],
    )

    contact_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(contact_entry, pattern="^contact$")],
        states={
            CONTACT_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, contact_send)],
        },
        fallbacks=[CallbackQueryHandler(conv_cancel, pattern="^main_menu$"),
                   CommandHandler("start", cmd_start)],
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("myid",   cmd_myid))

    # Панель тренера — все обработчики до ConversationHandler
    app.add_handler(CallbackQueryHandler(cb_admin,           pattern="^admin$"),         group=0)
    app.add_handler(CallbackQueryHandler(adm_bookings,       pattern="^adm_bookings$"),  group=0)
    app.add_handler(CallbackQueryHandler(adm_cancel_booking, pattern=r"^adm_cancel_\d+$"), group=0)
    app.add_handler(CallbackQueryHandler(adm_block_start,    pattern="^adm_block$"),     group=0)
    app.add_handler(CallbackQueryHandler(adm_block_date,     pattern=r"^adm_blkd_"),     group=0)
    app.add_handler(CallbackQueryHandler(adm_block_time,     pattern=r"^adm_blkt_"),     group=0)
    app.add_handler(CallbackQueryHandler(adm_extra_start,    pattern="^adm_extra$"),     group=0)
    app.add_handler(CallbackQueryHandler(adm_prices,         pattern="^adm_prices$"),    group=0)
    app.add_handler(CallbackQueryHandler(adm_admins,         pattern="^adm_admins$"),    group=0)
    app.add_handler(CallbackQueryHandler(adm_remove_admin,   pattern=r"^adm_rmadm_\d+$"),group=0)

    # ConversationHandler для текстового ввода в панели тренера
    admin_input_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(adm_extra_date,      pattern=r"^adm_exd_"),
            CallbackQueryHandler(adm_price_edit,      pattern=r"^adm_pe_"),
            CallbackQueryHandler(adm_fee_edit,        pattern=r"^adm_fe_"),
            CallbackQueryHandler(adm_add_admin_start, pattern="^adm_addadmin$"),
        ],
        states={
            ADM_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_text_input)],
        },
        fallbacks=[
            CallbackQueryHandler(cb_admin,    pattern="^admin$"),
            CallbackQueryHandler(cb_main_menu, pattern="^main_menu$"),
            CommandHandler("start", cmd_start),
        ],
        per_message=False,
    )

    app.add_handler(admin_input_conv)
    app.add_handler(booking_conv)
    app.add_handler(contact_conv)

    app.add_handler(CallbackQueryHandler(cb_schedule,        pattern="^schedule$"))
    app.add_handler(CallbackQueryHandler(cb_pricing,         pattern="^pricing$"))
    app.add_handler(CallbackQueryHandler(cb_my_bookings,     pattern="^my_bookings$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_booking,  pattern=r"^cancel_\d+$"))

    app.add_handler(CallbackQueryHandler(cb_schedule_week,       pattern=r"^sch_week_"))
    app.add_handler(CallbackQueryHandler(cb_schedule_quick_book, pattern=r"^sch_\d{4}-\d{2}-\d{2}_"))
    app.add_handler(CallbackQueryHandler(cb_add_calendar,    pattern=r"^add_cal_"),  group=0)
    app.add_handler(CallbackQueryHandler(cb_main_menu,       pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(cb_confirm_yes,     pattern=r"^cfm_yes_"),  group=0)
    app.add_handler(CallbackQueryHandler(cb_confirm_no,      pattern=r"^cfm_no_"),   group=0)
    app.add_handler(CallbackQueryHandler(lambda u, c: None,  pattern="^noop$"))

    app.job_queue.run_repeating(job_reminders,     interval=1800, first=30)
    app.job_queue.run_repeating(job_confirmations, interval=1800, first=60)

    logger.info("✅ Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
