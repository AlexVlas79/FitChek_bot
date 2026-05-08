#!/usr/bin/env python3
"""Telegram-бот для персонального тренера по хоккею"""

import logging
import re
from datetime import datetime, date, timedelta
from io import BytesIO
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

BOOK_NAME, BOOK_DATE, BOOK_TIME, BOOK_TYPE, BOOK_CONFIRM = range(5)
CONTACT_MSG = 5


# ════════════ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ════════════

def main_kb(is_admin=False):
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


def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Все записи",          callback_data="adm_list")],
        [InlineKeyboardButton("🚫 Заблокировать слот",  callback_data="adm_block"),
         InlineKeyboardButton("➕ Доп. слот",           callback_data="adm_extra")],
        [InlineKeyboardButton("💰 Изменить цены",       callback_data="adm_prices")],
        [InlineKeyboardButton("👥 Управление админами", callback_data="adm_admins")],
        [InlineKeyboardButton("🏠 Главное меню",        callback_data="main_menu")],
    ])


def get_available_dates():
    result, seen = [], set()
    today = date.today()
    extra_dates = set(db.get_extra_slot_dates())
    for i in range(1, 15):
        d = today + timedelta(days=i)
        ds = d.isoformat()
        seen.add(ds)
        if config.SCHEDULE.get(d.weekday()) or db.get_extra_slots(ds):
            result.append((ds, f"{config.DAY_NAMES[d.weekday()]}, {d.strftime('%d.%m')}"))
    for ds in sorted(extra_dates - seen):
        d = datetime.strptime(ds, "%Y-%m-%d").date()
        if d > today:
            result.append((ds, f"{config.DAY_NAMES[d.weekday()]}, {d.strftime('%d.%m')}"))
    result.sort(key=lambda x: x[0])
    return result


def get_free_slots(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    all_slots = sorted(set(config.SCHEDULE.get(d.weekday(), []) + db.get_extra_slots(date_str)))
    unavail = db.get_unavailable_times(date_str)
    return [s for s in all_slots if s not in unavail]


def fmt_date(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return f"{config.DAY_NAMES[d.weekday()]}, {d.strftime('%d.%m.%Y')}"


def get_addr(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return config.get_address(d.weekday())


async def notify_admins(context, text):
    for chat_id, _ in db.get_admins():
        try:
            await context.bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"notify {chat_id}: {e}")


def make_ics(child, date_str, time_str, venue, training_name):
    dt_start = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    dt_end = dt_start + timedelta(minutes=60)
    fmt = "%Y%m%dT%H%M%S"
    uid = f"{date_str}-{time_str}-{child}@bot".replace(" ", "")
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//HockeyBot//RU\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTART:{dt_start.strftime(fmt)}\r\nDTEND:{dt_end.strftime(fmt)}\r\n"
        f"SUMMARY:Тренировка {child}\r\nLOCATION:{venue}\r\n"
        f"DESCRIPTION:{training_name}. Тренер: {config.TRAINER_NAME}\r\n"
        "BEGIN:VALARM\r\nTRIGGER:-PT3H\r\nACTION:DISPLAY\r\n"
        "DESCRIPTION:Напоминание\r\nEND:VALARM\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode("utf-8")


# ════════════ /start и /myid ════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_admin = db.is_admin(user.id)

    # Deep link из расписания: /start sch_YYYY_MM_DD_HH-MM
    if context.args and context.args[0].startswith("sch_"):
        try:
            p = context.args[0].split("_")
            date_str = f"{p[1]}-{p[2]}-{p[3]}"
            time_str = p[4].replace("-", ":")
            context.user_data.clear()
            context.user_data["date"] = date_str
            context.user_data["time"] = time_str
            await update.message.reply_text(
                f"📝 *Запись*\n📅 *{fmt_date(date_str)}* в *{time_str}*\n"
                f"📍 *{get_addr(date_str)}*\n\nВведите имя ребёнка:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data="main_menu")
                ]])
            )
            return BOOK_NAME
        except Exception:
            pass

    text = (
        f"👋 Привет, *{user.first_name or 'друг'}*!\n\n"
        f"🏒 Бот тренера по хоккею — *{config.TRAINER_NAME}*\n\n"
        "Выбери действие 👇"
    )
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_kb(is_admin))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_kb(is_admin))


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(
        f"🆔 Ваш Telegram ID: `{u.id}`\n👤 @{u.username or 'не задан'}\n\n"
        "Отправьте этот ID тренеру для добавления как администратора.",
        parse_mode="Markdown"
    )


async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("adm_action", None)
    await update.callback_query.answer()
    await cmd_start(update, context)


# ════════════ РАСПИСАНИЕ ════════════

async def cb_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    week = int(query.data[9:]) if query.data.startswith("sch_week_") else 0
    dates = get_available_dates()
    if not dates:
        await query.edit_message_text(
            "😔 Свободных слотов нет.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Меню", callback_data="main_menu")
            ]])
        )
        return
    week_dates = dates[week * 7:(week + 1) * 7]
    lines = [f"📅 *Расписание — неделя {week + 1}*\n"]
    for date_str, label in week_dates:
        free = get_free_slots(date_str)
        venue = get_addr(date_str)
        lines.append(f"*{label}* — {venue}")
        if free:
            links = [
                f"[{t}](https://t.me/{config.BOT_USERNAME}?start=sch_{date_str.replace('-', '_')}_{t.replace(':', '-')})"
                for t in free
            ]
            lines.append("  ".join(links))
        else:
            lines.append("❌ Все слоты заняты")
        lines.append("")
    nav = []
    if week > 0:
        nav.append(InlineKeyboardButton("◀️ Назад", callback_data=f"sch_week_{week - 1}"))
    if (week + 1) * 7 < len(dates):
        nav.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"sch_week_{week + 1}"))
    rows = []
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Меню", callback_data="main_menu")])
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
        disable_web_page_preview=True
    )


# ════════════ СТОИМОСТЬ ════════════

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
        text += f"\n⛸ Вход: *{fee}* _(оплачивается дополнительно)_"
    text += "\n\n💳 *Оплата:* наличные / перевод"
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📝 Записаться", callback_data="book"),
            InlineKeyboardButton("🏠 Меню",       callback_data="main_menu"),
        ]])
    )


# ════════════ ЗАПИСЬ ════════════

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
    if context.user_data.get("date") and context.user_data.get("time"):
        return await _show_types(update.message, context)
    dates = get_available_dates()
    if not dates:
        await update.message.reply_text("😔 Нет свободных дат.",
                                        reply_markup=main_kb(db.is_admin(update.effective_user.id)))
        return ConversationHandler.END
    rows = [[InlineKeyboardButton(f"{l} ({get_addr(ds)})", callback_data=f"d_{ds}")]
            for ds, l in dates if get_free_slots(ds)]
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="main_menu")])
    await update.message.reply_text(
        f"✅ *{context.user_data['child']}*\n\n📅 Выберите дату:",
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
        await query.answer("Все слоты заняты", show_alert=True)
        return BOOK_DATE
    rows = [[InlineKeyboardButton(t, callback_data=f"t_{t}")] for t in free]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="book"),
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
    return await _show_types(query, context)


async def _show_types(msg_or_query, context):
    date_str = context.user_data["date"]
    venue = get_addr(date_str)
    pricing, _ = db.get_prices()
    items = pricing.get(venue, [])
    rows = [[InlineKeyboardButton(f"{n}  —  {p}", callback_data=f"tp_{i}")]
            for i, (n, p) in enumerate(items)]
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="main_menu")])
    text = f"📍 *{venue}*\n\n💰 Выберите тип занятия:"
    if hasattr(msg_or_query, "edit_message_text"):
        await msg_or_query.edit_message_text(text, parse_mode="Markdown",
                                             reply_markup=InlineKeyboardMarkup(rows))
    else:
        await msg_or_query.reply_text(text, parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(rows))
    return BOOK_TYPE


async def book_got_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data[3:])
    date_str = context.user_data["date"]
    venue = get_addr(date_str)
    pricing, fees = db.get_prices()
    name, price = pricing[venue][idx]
    entry = fees.get(venue, "")
    context.user_data["t_name"] = name
    context.user_data["t_price"] = price
    child = context.user_data["child"]
    time_str = context.user_data["time"]
    full_addr = config.ICE_RINK_FULL_ADDRESSES.get(venue, venue)
    map_url = config.ICE_RINK_MAP_LINKS.get(venue, "")

    nav = [[InlineKeyboardButton("✅ Подтвердить", callback_data="confirm"),
            InlineKeyboardButton("❌ Отмена",      callback_data="main_menu")]]
    if map_url:
        nav.append([InlineKeyboardButton("🗺 На карте", url=map_url)])

    await query.edit_message_text(
        "📋 *Подтверждение записи:*\n\n"
        f"👦 *{child}*\n"
        f"📅 *{fmt_date(date_str)}* в *{time_str}*\n"
        f"📍 *{venue}*\n🏠 {full_addr}\n"
        f"🏒 *{name}*\n💰 *{price}*\n"
        f"⛸ Вход: *{entry}* _(отдельно)_\n\n"
        f"📞 {config.TRAINER_USERNAME} · {config.TRAINER_PHONE}\n\nВсё верно?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(nav)
    )
    return BOOK_CONFIRM


async def book_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    child = context.user_data["child"]
    date_str = context.user_data["date"]
    time_str = context.user_data["time"]
    t_name = context.user_data["t_name"]
    t_price = context.user_data["t_price"]
    venue = get_addr(date_str)
    _, fees = db.get_prices()
    entry = fees.get(venue, "")

    booking_id = db.add_booking(user.id, user.username or user.first_name,
                                child, date_str, time_str)
    await notify_admins(context,
        f"🆕 *Запись #{booking_id}*\n👦 {child} | @{user.username or user.first_name}\n"
        f"📅 {fmt_date(date_str)} {time_str} | {venue}\n🏒 {t_name} — {t_price}"
    )
    context.user_data["ics"] = dict(child=child, date=date_str,
                                    time=time_str, venue=venue, name=t_name)
    await query.edit_message_text(
        f"✅ *Запись подтверждена!*\n\n"
        f"👦 *{child}*\n📅 {fmt_date(date_str)} в *{time_str}*\n"
        f"📍 *{venue}*\n🏒 *{t_name}*\n💰 *{t_price}*\n"
        f"⛸ Вход: *{entry}* _(на месте)_\n\n"
        f"📞 {config.TRAINER_USERNAME} · {config.TRAINER_PHONE}\n"
        f"🔔 Напоминание за {config.REMINDER_HOURS_BEFORE} ч.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
        ]])
    )
    try:
        await context.bot.send_message(user.id,
            "📅 Добавить тренировку в календарь?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📅 Добавить в календарь",
                                     callback_data=f"ical_{booking_id}")
            ]])
        )
    except Exception:
        pass
    return ConversationHandler.END


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await cmd_start(update, context)
    return ConversationHandler.END


# ════════════ МОИ ЗАПИСИ ════════════

async def cb_my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bookings = db.get_user_bookings(update.effective_user.id)
    if not bookings:
        await query.edit_message_text(
            "📋 Нет предстоящих записей.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📝 Записаться", callback_data="book"),
                InlineKeyboardButton("🏠 Меню",       callback_data="main_menu"),
            ]])
        )
        return
    lines = ["📋 *Ваши записи:*\n"]
    rows = []
    for bid, child, bdate, btime, status in bookings:
        icon = "✅" if status == "active" else "❌"
        lines.append(f"{icon} #{bid} | *{child}* | {fmt_date(bdate)} {btime}")
        if status == "active":
            rows.append([InlineKeyboardButton(
                f"❌ Отменить #{bid} ({child})",
                callback_data=f"ub_{bid}"
            )])
    rows.append([InlineKeyboardButton("🏠 Меню", callback_data="main_menu")])
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def cb_cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    bid = int(query.data[3:])
    ok = db.cancel_booking(bid, update.effective_user.id)
    await query.answer("✅ Отменена" if ok else "❌ Не удалось", show_alert=True)
    if ok:
        await notify_admins(context,
            f"❌ Отменена запись *#{bid}* — @{update.effective_user.username}")
    await cb_my_bookings(update, context)


# ════════════ НАПИСАТЬ ТРЕНЕРУ ════════════

async def contact_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        f"💬 *Написать тренеру {config.TRAINER_NAME}*\n\nНапишите ваш вопрос:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="main_menu")
        ]])
    )
    return CONTACT_MSG


async def contact_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await notify_admins(context,
        f"📩 *От @{user.username or user.first_name}:*\n\n{update.message.text}")
    await update.message.reply_text(
        "✅ Сообщение отправлено!",
        reply_markup=main_kb(db.is_admin(user.id))
    )
    return ConversationHandler.END


# ════════════ ПАНЕЛЬ ТРЕНЕРА ════════════

async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not db.is_admin(update.effective_user.id):
        await query.answer("⛔ Нет доступа", show_alert=True); return
    await query.answer()
    await query.edit_message_text("🛠 *Панель тренера*",
        parse_mode="Markdown", reply_markup=admin_kb())


async def adm_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not db.is_admin(update.effective_user.id):
        await query.answer("⛔", show_alert=True); return
    await query.answer()
    bookings = db.get_all_upcoming_bookings()
    if not bookings:
        await query.edit_message_text("📋 Нет предстоящих записей.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="admin")
            ]]))
        return
    lines = [f"📋 *Записей: {len(bookings)}*\n"]
    rows = []
    for bid, uname, child, bdate, btime in bookings:
        lines.append(f"• #{bid} | *{child}* | {fmt_date(bdate)} {btime} | {get_addr(bdate)}")
        rows.append([InlineKeyboardButton(
            f"❌ Отменить #{bid} ({child})", callback_data=f"ac_{bid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin")])
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows))


async def adm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not db.is_admin(update.effective_user.id):
        await query.answer("⛔", show_alert=True); return
    bid = int(query.data[3:])
    row = db.get_booking_by_id(bid)
    db.admin_cancel_booking(bid)
    await query.answer(f"✅ #{bid} отменена", show_alert=True)
    if row:
        _, user_id, _, child, bdate, btime = row
        try:
            await context.bot.send_message(user_id,
                f"❗ Ваша запись отменена тренером.\n👦 {child} | {fmt_date(bdate)} {btime}")
        except Exception:
            pass
    await adm_list(update, context)


# Блокировка слота
async def adm_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not db.is_admin(update.effective_user.id):
        await query.answer("⛔", show_alert=True); return
    await query.answer()
    # Показываем все дни со слотами в расписании
    today = date.today()
    rows = []
    for i in range(1, 15):
        d = today + timedelta(days=i)
        ds = d.isoformat()
        if config.SCHEDULE.get(d.weekday()) or db.get_extra_slots(ds):
            label = f"{config.DAY_NAMES[d.weekday()]}, {d.strftime('%d.%m')}"
            rows.append([InlineKeyboardButton(label, callback_data=f"bld_{ds}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin")])
    await query.edit_message_text("🚫 *Заблокировать слот*\n\nВыберите дату:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


async def adm_block_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    date_str = query.data[4:]   # "bld_" = 4 chars
    context.user_data["blk_date"] = date_str
    # Показываем ВСЕ слоты по расписанию (не только свободные)
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    all_slots = sorted(set(
        config.SCHEDULE.get(d.weekday(), []) + db.get_extra_slots(date_str)
    ))
    if not all_slots:
        await query.answer("Нет слотов на эту дату", show_alert=True); return
    rows = [[InlineKeyboardButton(t, callback_data=f"blt_{t}")] for t in all_slots]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="adm_block")])
    await query.edit_message_text(
        f"🚫 *{fmt_date(date_str)}*\n\nВыберите время для блокировки:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


async def adm_block_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    time_str = query.data[4:]   # "blt_" = 4 chars
    date_str = context.user_data.get("blk_date", "")
    db.block_slot(date_str, time_str)
    await query.edit_message_text(
        f"🚫 Слот *{fmt_date(date_str)} {time_str}* заблокирован.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Панель", callback_data="admin")
        ]]))


# Доп. слот
async def adm_extra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not db.is_admin(update.effective_user.id):
        await query.answer("⛔", show_alert=True); return
    await query.answer()
    today = date.today()
    rows = []
    for i in range(1, 15):
        d = today + timedelta(days=i)
        rows.append([InlineKeyboardButton(
            f"{config.DAY_NAMES[d.weekday()]}, {d.strftime('%d.%m')}",
            callback_data=f"exd_{d.isoformat()}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin")])
    await query.edit_message_text("➕ *Доп. слот*\n\nВыберите дату:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


async def adm_extra_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    date_str = query.data[4:]   # "exd_" = 4 chars
    context.user_data["adm_action"] = "extra_time"
    context.user_data["extra_date"] = date_str
    await query.edit_message_text(
        f"➕ Доп. слот на *{fmt_date(date_str)}*\n\nВведите время (ЧЧ:ММ):",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="admin")
        ]]))


# Изменить цены
async def adm_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not db.is_admin(update.effective_user.id):
        await query.answer("⛔", show_alert=True); return
    await query.answer()
    pricing, fees = db.get_prices()
    context.user_data["pc"] = {
        "v": list(pricing.keys()),
        "i": {v: items for v, items in pricing.items()}
    }
    rows = []
    for vi, (venue, items) in enumerate(pricing.items()):
        rows.append([InlineKeyboardButton(f"── {venue} ──", callback_data="noop")])
        for ii, (name, price) in enumerate(items):
            short = name.replace("🏒 ", "").replace("👥 ", "").replace("📦 ", "")
            rows.append([InlineKeyboardButton(
                f"{short[:24]}  →  {price}",
                callback_data=f"pe_{vi}_{ii}"
            )])
        fee = fees.get(venue, "")
        rows.append([InlineKeyboardButton(
            f"⛸ Вход {venue}: {fee}",
            callback_data=f"fe_{vi}"
        )])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin")])
    await query.edit_message_text(
        "💰 *Управление ценами*\nНажмите для изменения:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


async def adm_price_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data[3:].split("_")   # "pe_" = 3 chars
    vi, ii = int(parts[0]), int(parts[1])
    pricing, _ = db.get_prices()        # всегда свежие данные
    venue = list(pricing.keys())[vi]
    name, _ = pricing[venue][ii]
    context.user_data["adm_action"] = "price_val"
    context.user_data["pv"] = venue
    context.user_data["pn"] = name
    await query.edit_message_text(
        f"💰 *{venue}*\n{name}\n\nВведите новую цену:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="adm_prices")
        ]]))


async def adm_fee_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    vi = int(query.data[3:])   # "fe_" = 3 chars
    pricing, _ = db.get_prices()        # всегда свежие данные
    venue = list(pricing.keys())[vi]
    context.user_data["adm_action"] = "fee_val"
    context.user_data["fv"] = venue
    await query.edit_message_text(
        f"⛸ Вход *{venue}*\n\nВведите новую стоимость:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="adm_prices")
        ]]))


# Управление админами
async def adm_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not db.is_admin(update.effective_user.id):
        await query.answer("⛔", show_alert=True); return
    await query.answer()
    admins = db.get_admins()
    lines = ["👥 *Администраторы:*\n"]
    rows = []
    for chat_id, username in admins:
        m = "⭐" if chat_id == config.TRAINER_CHAT_ID else "👤"
        lines.append(f"{m} @{username} (`{chat_id}`)")
        if chat_id != config.TRAINER_CHAT_ID:
            rows.append([InlineKeyboardButton(
                f"❌ @{username}", callback_data=f"rm_{chat_id}")])
    rows.append([InlineKeyboardButton("➕ Добавить", callback_data="aa")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin")])
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows))


async def adm_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["adm_action"] = "add_admin"
    await query.edit_message_text(
        "👥 *Добавить администратора*\n\nПопросите написать /myid\nВведите полученный ID:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="adm_admins")
        ]]))


async def adm_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not db.is_admin(update.effective_user.id):
        await query.answer("⛔", show_alert=True); return
    chat_id = int(query.data[3:])   # "rm_" = 3 chars
    db.remove_admin(chat_id)
    await query.answer("✅ Удалён", show_alert=True)
    await adm_admins(update, context)


# Обработчик текстового ввода тренера
async def adm_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get("adm_action", "")
    if not action:
        return
    text = update.message.text.strip()

    if action == "extra_time":
        date_str = context.user_data.get("extra_date", "")
        if not re.match(r"^\d{2}:\d{2}$", text):
            await update.message.reply_text("❌ Введите время в формате ЧЧ:ММ (например, 14:30):")
            return
        db.add_extra_slot(date_str, text)
        context.user_data.pop("adm_action", None)
        await update.message.reply_text(
            f"✅ Доп. слот *{fmt_date(date_str)} {text}* добавлен!",
            parse_mode="Markdown", reply_markup=admin_kb())

    elif action == "price_val":
        db.update_price(context.user_data["pv"], context.user_data["pn"], text)
        context.user_data.pop("adm_action", None)
        await update.message.reply_text(
            f"✅ Цена обновлена: *{text}*",
            parse_mode="Markdown", reply_markup=admin_kb())

    elif action == "fee_val":
        db.update_entry_fee(context.user_data["fv"], text)
        context.user_data.pop("adm_action", None)
        await update.message.reply_text(
            f"✅ Вход обновлён: *{text}*",
            parse_mode="Markdown", reply_markup=admin_kb())

    elif action == "add_admin":
        if not text.isdigit():
            await update.message.reply_text("❌ ID должен быть числом:")
            return
        new_id = int(text)
        try:
            chat = await context.bot.get_chat(new_id)
            username = chat.username or chat.first_name or str(new_id)
        except Exception:
            username = str(new_id)
        db.add_admin(new_id, username)
        context.user_data.pop("adm_action", None)
        try:
            await context.bot.send_message(new_id,
                f"✅ Вас добавили как администратора бота *{config.TRAINER_NAME}*!",
                parse_mode="Markdown")
        except Exception:
            pass
        await update.message.reply_text(
            f"✅ @{username} добавлен!",
            parse_mode="Markdown", reply_markup=admin_kb())


# ════════════ КАЛЕНДАРЬ ════════════

async def cb_add_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    info = context.user_data.get("ics", {})
    if not info:
        await query.edit_message_text("❌ Данные не найдены.")
        return
    try:
        ics = make_ics(info["child"], info["date"], info["time"], info["venue"], info["name"])
        f = BytesIO(ics)
        f.name = f"training.ics"
        await query.edit_message_text("✅ Отправляю...")
        await context.bot.send_document(
            update.effective_user.id, document=f,
            caption="📅 *Как добавить:*\n📱 iPhone — нажать → Добавить в Календарь\n"
                    "🤖 Android — нажать → Google Calendar",
            parse_mode="Markdown")
    except Exception as e:
        logger.error(e)
        await query.edit_message_text("❌ Не удалось создать файл.")


# ════════════ ПОДТВЕРЖДЕНИЕ И НАПОМИНАНИЯ ════════════

async def cb_confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bid = int(query.data.split("_")[2])
    row = db.get_booking_by_id(bid)
    if row:
        _, _, _, child, bdate, btime = row
        await query.edit_message_text(
            f"✅ Ждём вас!\n👦 {child} | {fmt_date(bdate)} в *{btime}*",
            parse_mode="Markdown")
    else:
        await query.edit_message_text("✅ Спасибо!")


async def cb_confirm_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bid = int(query.data.split("_")[2])
    row = db.get_booking_by_id(bid)
    db.cancel_booking_by_id(bid)
    if row:
        _, _, _, child, bdate, btime = row
        await notify_admins(context,
            f"❌ Отмена через подтверждение\n👦 {child} | {fmt_date(bdate)} {btime}")
        await query.edit_message_text(
            f"❌ Запись *{fmt_date(bdate)} {btime}* отменена.",
            parse_mode="Markdown")


async def job_confirmations(context: ContextTypes.DEFAULT_TYPE):
    for row in db.get_bookings_for_confirmation():
        bid, user_id, child, bdate, btime = row
        venue = get_addr(bdate)
        try:
            await context.bot.send_message(user_id,
                f"❓ *Подтверждение*\n\nЗавтра в *{btime}* — *{child}*\n"
                f"📅 {fmt_date(bdate)}\n📍 {venue}\n\n"
                f"⚠️ _Отмена менее чем за 12 часов — тренировка оплачивается._\n\nВы придёте?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Да",            callback_data=f"cfm_yes_{bid}"),
                    InlineKeyboardButton("❌ Отменить", callback_data=f"cfm_no_{bid}"),
                ]]))
            db.mark_confirmation_sent(bid)
        except Exception as e:
            logger.error(e)


async def job_reminders(context: ContextTypes.DEFAULT_TYPE):
    _, fees = db.get_prices()
    for row in db.get_bookings_for_reminder():
        bid, user_id, child, bdate, btime = row
        venue = get_addr(bdate)
        entry = fees.get(venue, "")
        try:
            await context.bot.send_message(user_id,
                f"🔔 *Напоминание!*\n\nСегодня в *{btime}* — *{child}*\n"
                f"📅 {fmt_date(bdate)}\n📍 {venue}\n⛸ Вход: *{entry}*\n\nУдачи! 🏒",
                parse_mode="Markdown")
            db.mark_reminder_sent(bid)
        except Exception as e:
            logger.error(e)


# ════════════ ЗАПУСК ════════════

def main():
    db.init_db()
    app = Application.builder().token(config.TOKEN).build()

    booking_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(book_entry, pattern="^book$"),
            CommandHandler("start", cmd_start),
        ],
        states={
            BOOK_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, book_got_name)],
            BOOK_DATE:    [CallbackQueryHandler(book_got_date, pattern=r"^d_")],
            BOOK_TIME:    [CallbackQueryHandler(book_got_time, pattern=r"^t_")],
            BOOK_TYPE:    [CallbackQueryHandler(book_got_type, pattern=r"^tp_\d+$")],
            BOOK_CONFIRM: [CallbackQueryHandler(book_confirm,  pattern="^confirm$")],
        },
        fallbacks=[
            CallbackQueryHandler(conv_cancel, pattern="^main_menu$"),
            CommandHandler("start", cmd_start),
        ],
    )

    contact_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(contact_entry, pattern="^contact$")],
        states={
            CONTACT_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, contact_send)],
        },
        fallbacks=[
            CallbackQueryHandler(conv_cancel, pattern="^main_menu$"),
            CommandHandler("start", cmd_start),
        ],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myid",  cmd_myid))
    app.add_handler(booking_conv)
    app.add_handler(contact_conv)

    # Текстовый ввод тренера — после диалогов
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        adm_text_input
    ))

    # Основные кнопки
    app.add_handler(CallbackQueryHandler(cb_main_menu,      pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(cb_schedule,       pattern="^schedule$"))
    app.add_handler(CallbackQueryHandler(cb_schedule,       pattern=r"^sch_week_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_pricing,        pattern="^pricing$"))
    app.add_handler(CallbackQueryHandler(cb_my_bookings,    pattern="^my_bookings$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_booking, pattern=r"^ub_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_add_calendar,   pattern=r"^ical_"))
    app.add_handler(CallbackQueryHandler(cb_confirm_yes,    pattern=r"^cfm_yes_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_confirm_no,     pattern=r"^cfm_no_\d+$"))

    # Панель тренера
    app.add_handler(CallbackQueryHandler(cb_admin,          pattern="^admin$"))
    app.add_handler(CallbackQueryHandler(adm_list,          pattern="^adm_list$"))
    app.add_handler(CallbackQueryHandler(adm_cancel,        pattern=r"^ac_\d+$"))
    app.add_handler(CallbackQueryHandler(adm_block,         pattern="^adm_block$"))
    app.add_handler(CallbackQueryHandler(adm_block_date,    pattern=r"^bld_"))
    app.add_handler(CallbackQueryHandler(adm_block_time,    pattern=r"^blt_"))
    app.add_handler(CallbackQueryHandler(adm_extra,         pattern="^adm_extra$"))
    app.add_handler(CallbackQueryHandler(adm_extra_date,    pattern=r"^exd_"))
    app.add_handler(CallbackQueryHandler(adm_prices,        pattern="^adm_prices$"))
    app.add_handler(CallbackQueryHandler(adm_price_edit,    pattern=r"^pe_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(adm_fee_edit,      pattern=r"^fe_\d+$"))
    app.add_handler(CallbackQueryHandler(adm_admins,        pattern="^adm_admins$"))
    app.add_handler(CallbackQueryHandler(adm_add_admin,     pattern="^aa$"))
    app.add_handler(CallbackQueryHandler(adm_remove,        pattern=r"^rm_\d+$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: None, pattern="^noop$"))

    app.job_queue.run_repeating(job_reminders,     interval=1800, first=30)
    app.job_queue.run_repeating(job_confirmations, interval=1800, first=60)

    logger.info("✅ Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
