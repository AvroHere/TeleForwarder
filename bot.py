from web import keep_alive
keep_alive()
import logging
import asyncio
import os
import sys
import aiosqlite
import pytz
from datetime import date, datetime, timedelta
from dotenv import load_dotenv

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.error import TelegramError, RetryAfter

# ================= CONFIGURATION =================
# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_GROUP_ID = os.getenv("TARGET_GROUP_ID")
DB_NAME = os.getenv("DB_NAME", "bot_master_v3.db")

# Parse Admin IDs from comma-separated string
admin_ids_str = os.getenv("ADMIN_IDS", "")
try:
    ADMIN_IDS = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip()]
except ValueError:
    print("❌ Error: ADMIN_IDS in .env must be a comma-separated list of integers.")
    sys.exit(1)

# Validate Critical Config
if not BOT_TOKEN or not TARGET_GROUP_ID:
    print("❌ Error: BOT_TOKEN and TARGET_GROUP_ID must be set in .env file.")
    sys.exit(1)

TARGET_GROUP_ID = int(TARGET_GROUP_ID)

# ================= LOGGING =================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global Dictionary to store temporary captions
# Structure: { user_id: {'text': "Caption", 'time': datetime_object} }
pending_captions = {}

# ================= DATABASE MANAGER =================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        
        defaults = {
            'delay': '10',
            'paused': '0',
            'link': 'https://t.me/telegram',
            'join_enabled': '0',
            'custom_text': '',
            'custom_remaining': '0',
            'total_off': '0'
        }
        
        for key, val in defaults.items():
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))

        await db.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                user_id INTEGER,
                sent_today INTEGER DEFAULT 0,
                sent_lifetime INTEGER DEFAULT 0,
                last_updated DATE,
                PRIMARY KEY (user_id)
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message_id INTEGER,
                media_type TEXT,
                status TEXT DEFAULT 'pending',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def get_setting(key):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def update_setting(key, value):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        await db.commit()

async def add_to_queue(user_id, message_id, media_type):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO queue (user_id, message_id, media_type) VALUES (?, ?, ?)",
            (user_id, message_id, media_type)
        )
        await db.commit()
        async with db.execute("SELECT COUNT(*) FROM queue WHERE status='pending'") as cursor:
            return (await cursor.fetchone())[0]

async def update_stats(user_id):
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT last_updated FROM stats WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0] != today:
                await db.execute("UPDATE stats SET sent_today = 0, last_updated = ? WHERE user_id = ?", (today, user_id))
        
        await db.execute("""
            INSERT INTO stats (user_id, sent_today, sent_lifetime, last_updated)
            VALUES (?, 1, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
            sent_today = sent_today + 1,
            sent_lifetime = sent_lifetime + 1,
            last_updated = ?
        """, (user_id, today, today))
        await db.commit()

async def get_queue_counts():
    async with aiosqlite.connect(DB_NAME) as db:
        pending = (await (await db.execute("SELECT COUNT(*) FROM queue WHERE status='pending'")).fetchone())[0]
        total_sent = (await (await db.execute("SELECT SUM(sent_lifetime) FROM stats")).fetchone())[0] or 0
        return pending, total_sent

async def get_next_item():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM queue WHERE status='pending' ORDER BY id ASC LIMIT 1") as cursor:
            return await cursor.fetchone()

async def mark_as_sent(queue_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE queue SET status='sent' WHERE id = ?", (queue_id,))
        await db.commit()

# ================= HELPERS =================
def is_admin(user_id):
    return user_id in ADMIN_IDS

def build_progress_text(sent_batch, total_batch, total_queue, today, lifetime, delay):
    return (
        f"✅ <b>Video Sent :</b> {sent_batch}/{total_batch}\n"
        f"⏳ <b>Total in Queue :</b> {total_queue}\n"
        f"📅 <b>Total Sent Today :</b> {today}\n"
        f"📈 <b>Total Sent Lifetime :</b> {lifetime}\n"
        f"⏲ <b>Current Delay :</b> {delay}s"
    )

# ================= BATCH NOTIFICATION LOGIC =================
batch_buffer = {}

async def send_batch_notification(user_id):
    """Waits a moment, then sends the summary message."""
    try:
        await asyncio.sleep(3)
        if user_id in batch_buffer:
            data = batch_buffer[user_id]
            count = data['count']
            last_msg = data['last_msg']
            pending, _ = await get_queue_counts()
            
            text = (
                f"📥 <b>Batch Received!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📎 <b>Added:</b> {count} files\n"
                f"🔢 <b>Total in Queue:</b> {pending}"
            )
            try:
                await last_msg.reply_text(text, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"Failed to send batch reply: {e}")
            del batch_buffer[user_id]
    except asyncio.CancelledError:
        pass

# ================= COMMAND HANDLERS =================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    delay = await get_setting('delay')
    text = (
        "🤖 <b>Media Forwarder Bot Ready!</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"⏱ <b>Current Delay:</b> {delay}s\n"
        "Use /help to see all commands."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    text = (
        "<b>🛠 Command List:</b>\n\n"
        "<b>⚙️ Basic:</b>\n"
        "/delay X - Set interval (min 5s)\n"
        "/info - Show stats & config\n"
        "/infoadmin - Admin Dashboard\n"
        "/hold - Pause /resume - Resume\n"
        "/cancel - Clear queue\n\n"
        "<b>📝 Caption Management:</b>\n"
        "<i>Just send text to set caption for next video!</i>\n"
        "/link {url} - Set Join Link\n"
        "/joinshow - Show 'For More...'\n"
        "/joinoff - Hide 'For More...'\n"
        "/custom X {text} - Set custom caption manually\n"
        "/customoff - Stop custom caption\n"
        "/totaloff - <b>CLEAN MODE</b>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def delay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    try:
        new_delay = int(context.args[0])
        if new_delay < 5:
            await update.message.reply_text("⚠️ <b>Delay must be >= 5s.</b>", parse_mode=ParseMode.HTML)
            return
        await update_setting('delay', new_delay)
        await update.message.reply_text(f"✅ Delay updated to <b>{new_delay}s</b>.", parse_mode=ParseMode.HTML)
    except:
        await update.message.reply_text("❌ Usage: `/delay 10`", parse_mode=ParseMode.HTML)

async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("❌ Usage: `/link https://t.me/yourlink`", parse_mode=ParseMode.HTML)
        return
    link = context.args[0]
    await update_setting('link', link)
    await update.message.reply_text(f"✅ Link set to: {link}")

async def joinshow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update_setting('join_enabled', '1')
    await update.message.reply_text("✅ <b>Join Footer Enabled.</b>", parse_mode=ParseMode.HTML)

async def joinoff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update_setting('join_enabled', '0')
    await update.message.reply_text("❌ <b>Join Footer Disabled.</b>", parse_mode=ParseMode.HTML)

async def totaloff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update_setting('total_off', '1')
    await update.message.reply_text("🧹 <b>Total Off ENABLED:</b> Sending clean videos only.", parse_mode=ParseMode.HTML)

async def custom_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    try:
        count = int(context.args[0])
        text = " ".join(context.args[1:])
        db_count = -1 if count == 0 else count
        
        await update_setting('custom_remaining', db_count)
        await update_setting('custom_text', text)
        await update_setting('total_off', '0')
        
        count_str = "Infinite" if count == 0 else str(count)
        await update.message.reply_text(f"✅ <b>Custom Caption Set!</b>\n\nText: {text}\nVideos: {count_str}", parse_mode=ParseMode.HTML)
    except:
        await update.message.reply_text("❌ Usage: `/custom 5 Your Text Here` (Use 0 for all)", parse_mode=ParseMode.HTML)

async def customoff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update_setting('custom_remaining', '0')
    await update.message.reply_text("❌ <b>Custom Caption Disabled.</b>", parse_mode=ParseMode.HTML)

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    
    pending, total_sent_all = await get_queue_counts()
    delay = await get_setting('delay')
    paused = await get_setting('paused')
    total_off = await get_setting('total_off')
    join_en = await get_setting('join_enabled')
    cust_rem = await get_setting('custom_remaining')
    cust_text = await get_setting('custom_text')

    state = "Paused ⏸" if paused == '1' else "Active ▶️"
    captions = "OFF (Clean) 🧹" if total_off == '1' else "ON 📝"
    
    msg = (
        f"📊 <b>Detailed Info</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ <b>State:</b> {state}\n"
        f"⏱ <b>Delay:</b> {delay}s\n"
        f"📥 <b>Queue Pending:</b> {pending}\n"
        f"📤 <b>Total Sent (Global):</b> {total_sent_all}\n"
        f"📝 <b>Captions:</b> {captions}\n"
        f"🔗 <b>Join Footer:</b> {'Yes' if join_en=='1' else 'No'}\n"
        f"💬 <b>Custom Queue:</b> {cust_rem} left ({cust_text[:10]}...)"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def infoadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return

    msg_text = "<b>👮 ADMIN INFORMATION DASHBOARD</b>\n\n"
    
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        count = 1
        for admin_id in ADMIN_IDS:
            try:
                chat = await context.bot.get_chat(admin_id)
                name = chat.first_name + (f" {chat.last_name}" if chat.last_name else "")
                username = f"@{chat.username}" if chat.username else "No Username"
            except:
                name = "Unknown Admin"
                username = "Unknown"

            today_count = 0
            total_count = 0
            async with db.execute("SELECT sent_today, sent_lifetime FROM stats WHERE user_id = ?", (admin_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    today_count = row['sent_today']
                    total_count = row['sent_lifetime']

            last_seen_str = "Never"
            async with db.execute("SELECT timestamp FROM queue WHERE user_id = ? AND status='sent' ORDER BY id DESC LIMIT 1", (admin_id,)) as cursor:
                last_q = await cursor.fetchone()
                if last_q:
                    try:
                        utc_dt = datetime.strptime(last_q['timestamp'], "%Y-%m-%d %H:%M:%S")
                        utc_dt = pytz.utc.localize(utc_dt)
                        bd_zone = pytz.timezone('Asia/Dhaka')
                        bd_dt = utc_dt.astimezone(bd_zone)
                        last_seen_str = bd_dt.strftime("%d-%b-%Y %I:%M %p")
                    except:
                        last_seen_str = str(last_q['timestamp'])

            msg_text += (
                f"<b>👮 Admin {count:02d}</b>\n"
                f"👤 <b>Display Name :</b> {name}\n"
                f"💠 <b>Username :</b> {username}\n"
                f"🆔 <b>Uid :</b> <code>{admin_id}</code>\n"
                f"🕰 <b>Last Send :</b> {last_seen_str}\n"
                f"📅 <b>Today Send :</b> {today_count}\n"
                f"📊 <b>Total Send :</b> {total_count}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
            )
            count += 1

    await update.message.reply_text(msg_text, parse_mode=ParseMode.HTML)

async def hold_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update_setting('paused', '1')
    await update.message.reply_text("⏸ <b>Forwarding PAUSED.</b>", parse_mode=ParseMode.HTML)

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update_setting('paused', '0')
    await update.message.reply_text("▶️ <b>Forwarding RESUMED.</b>", parse_mode=ParseMode.HTML)

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE queue SET status='cancelled' WHERE status='pending'")
        await db.commit()
    await update.message.reply_text("🗑 <b>Queue Cleared!</b>", parse_mode=ParseMode.HTML)

# ================= SMART MEDIA HANDLER =================
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    
    msg = update.message
    
    # 1. TEXT ONLY DETECTION (For Custom Caption)
    # If message has text but NO media, treat it as a Caption Setup
    if msg.text and not (msg.video or msg.photo or msg.animation or msg.document):
        # Save text in memory buffer
        pending_captions[user_id] = {
            'text': msg.text,
            'time': datetime.now()
        }
        # Confirm to admin but DO NOT queue or forward
        await msg.reply_text("📝 <b>Caption Saved!</b> Send media within 5s to apply.", parse_mode=ParseMode.HTML)
        return

    # 2. MEDIA DETECTION
    media_type = "File"
    if msg.video: media_type = "Video"
    elif msg.photo: media_type = "Photo"
    elif msg.animation: media_type = "GIF"
    elif msg.document: media_type = "Document"
    
    # Check if a custom caption is waiting (sent < 5 seconds ago)
    if user_id in pending_captions:
        saved = pending_captions[user_id]
        time_diff = (datetime.now() - saved['time']).total_seconds()
        
        # If text was sent recently (e.g., within 5 seconds)
        if time_diff < 5:
            # Apply settings AUTOMATICALLY (Like /custom 1 Text)
            await update_setting('custom_text', saved['text'])
            await update_setting('custom_remaining', '1') # Apply to 1 video
            await update_setting('total_off', '0') # Ensure captions are ON
            
            # Clear the buffer
            del pending_captions[user_id]
        else:
            # Too old, clear it
            del pending_captions[user_id]

    # 3. Add to Database
    await add_to_queue(user_id, msg.message_id, media_type)
    
    # 4. Batch Notification
    if user_id in batch_buffer:
        batch_buffer[user_id]['count'] += 1
        batch_buffer[user_id]['last_msg'] = msg
        if batch_buffer[user_id]['task']:
            batch_buffer[user_id]['task'].cancel()
    else:
        batch_buffer[user_id] = {
            'count': 1,
            'last_msg': msg,
            'task': None
        }
    
    batch_buffer[user_id]['task'] = asyncio.create_task(send_batch_notification(user_id))


# ================= BACKGROUND WORKER =================
async def queue_processor(app):
    logger.info("Queue Processor Started...")
    batch_counter = 0 
    
    while True:
        try:
            is_paused = await get_setting('paused')
            if is_paused == '1':
                await asyncio.sleep(2)
                continue

            item = await get_next_item()
            
            if not item:
                if batch_counter > 0: batch_counter = 0
                await asyncio.sleep(2)
                continue

            pending_count, _ = await get_queue_counts()
            if batch_counter == 0: pass 
            batch_counter += 1
            
            user_id = item['user_id']
            msg_id = item['message_id']
            queue_id = item['id']
            delay = int(await get_setting('delay'))
            
            # Caption Building
            total_off = await get_setting('total_off')
            final_caption = ""
            
            if total_off == '1':
                final_caption = "" 
            else:
                cust_rem = int(await get_setting('custom_remaining'))
                if cust_rem != 0:
                    custom_text = await get_setting('custom_text')
                    final_caption += custom_text + "\n\n"
                    # Decrease count if it's not infinite (-1)
                    if cust_rem > 0:
                        cust_rem -= 1
                        await update_setting('custom_remaining', cust_rem)
                
                join_en = await get_setting('join_enabled')
                if join_en == '1':
                    link = await get_setting('link')
                    final_caption += f"For More Video <a href='{link}'>Join Here</a>"

            try:
                await app.bot.copy_message(
                    chat_id=TARGET_GROUP_ID,
                    from_chat_id=user_id,
                    message_id=msg_id,
                    caption=final_caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None 
                )
                
                await update_stats(user_id)
                await mark_as_sent(queue_id)
                
                async with aiosqlite.connect(DB_NAME) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.execute("SELECT * FROM stats WHERE user_id = ?", (user_id,)) as cursor:
                        stat = await cursor.fetchone()
                
                pending_now, _ = await get_queue_counts()
                current_batch_total = batch_counter + pending_now

                report = build_progress_text(
                    sent_batch=batch_counter,
                    total_batch=current_batch_total, 
                    total_queue=pending_now,
                    today=stat['sent_today'],
                    lifetime=stat['sent_lifetime'],
                    delay=delay
                )
                
                try:
                    await app.bot.send_message(chat_id=user_id, text=report, parse_mode=ParseMode.HTML)
                except: pass

                try:
                    await app.bot.delete_message(chat_id=user_id, message_id=msg_id)
                except: pass

            except RetryAfter as e:
                logger.warning(f"Flood limit exceeded. Sleeping {e.retry_after}s.")
                await asyncio.sleep(e.retry_after)
                continue
            except TelegramError as e:
                logger.error(f"Telegram Error: {e}")
                await mark_as_sent(queue_id)

            await asyncio.sleep(delay)

        except Exception as e:
            logger.error(f"Worker Error: {e}")
            await asyncio.sleep(5)

# ================= MAIN =================
if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_db())

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("delay", delay_command))
    app.add_handler(CommandHandler("hold", hold_command))
    app.add_handler(CommandHandler("resume", resume_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CommandHandler("infoadmin", infoadmin_command))
    
    app.add_handler(CommandHandler("link", link_command))
    app.add_handler(CommandHandler("joinshow", joinshow_command))
    app.add_handler(CommandHandler("joinoff", joinoff_command))
    app.add_handler(CommandHandler("totaloff", totaloff_command))
    app.add_handler(CommandHandler("custom", custom_command))
    app.add_handler(CommandHandler("customoff", customoff_command))

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_media))

    loop.create_task(queue_processor(app))

    print("Bot is running...")
    app.run_polling()
