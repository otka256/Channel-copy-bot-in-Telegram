# ربات انتقال‌دهنده هوشمند پست‌های تلگرام
# این کد با استفاده از کتابخانه Pyrogram نوشته شده است

import os
import io
import json
import sqlite3
import asyncio
import logging
import tempfile
import subprocess
from datetime import datetime
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from pyrogram.errors import FloodWait, BadRequest
from pyrogram.enums import ChatMemberStatus, ParseMode, ChatType
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

load_dotenv()

# تنظیم لاگر
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# مشخصات API تلگرام
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

# آیدی ادمین (فقط این شخص می‌تواند ربات را مدیریت کند)
ADMIN_ID = int(os.getenv("ADMIN_ID"))

# ایجاد کلاینت‌های مورد نیاز
bot = Client("transfer_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
user = Client("user_account", api_id=API_ID, api_hash=API_HASH)
user_states = {}  # ذخیره وضعیت مرحله به مرحله ادمین‌ها

# ایجاد دیتابیس SQLite
def create_database():
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()

    # جداول اصلی
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS channel_connections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_channel TEXT NOT NULL,
        destination_channel TEXT NOT NULL,
        watermark_text TEXT,
        -- جدید
        is_active INTEGER DEFAULT 1,
        last_scanned_message_id INTEGER DEFAULT 0,
        is_restricted INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS word_replacements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        connection_id INTEGER,
        original_word TEXT NOT NULL,
        replacement_word TEXT NOT NULL,
        FOREIGN KEY (connection_id) REFERENCES channel_connections (id) ON DELETE CASCADE
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS transferred_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        connection_id INTEGER,
        source_message_id INTEGER NOT NULL,
        destination_message_id INTEGER NOT NULL,
        transferred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (connection_id) REFERENCES channel_connections (id) ON DELETE CASCADE,
        UNIQUE(connection_id, source_message_id)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS activity_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        connection_id INTEGER,
        action_type TEXT NOT NULL,
        details TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (connection_id) REFERENCES channel_connections (id) ON DELETE CASCADE
    )
    ''')

    # مهاجرتِ امن برای نسخه‌های قدیمی
    def _safe_alter(sql):
        try:
            cursor.execute(sql)
        except Exception:
            pass

    _safe_alter("ALTER TABLE channel_connections ADD COLUMN is_active INTEGER DEFAULT 1")
    _safe_alter("ALTER TABLE channel_connections ADD COLUMN last_scanned_message_id INTEGER DEFAULT 0")
    _safe_alter("ALTER TABLE channel_connections ADD COLUMN is_restricted INTEGER DEFAULT 0")
    _safe_alter("CREATE UNIQUE INDEX IF NOT EXISTS idx_transferred_unique ON transferred_posts(connection_id, source_message_id)")

    conn.commit()
    conn.close()

# توابع دیتابیس
def add_channel_connection(source, destination):
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO channel_connections (source_channel, destination_channel) VALUES (?, ?)", 
                  (source, destination))
    connection_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return connection_id

def get_all_connections():
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, source_channel, destination_channel FROM channel_connections")
    connections = cursor.fetchall()
    conn.close()
    return connections

def delete_connection(connection_id):
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM channel_connections WHERE id = ?", (connection_id,))
    conn.commit()
    conn.close()

def add_word_replacement(connection_id, original_word, replacement_word):
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO word_replacements (connection_id, original_word, replacement_word) VALUES (?, ?, ?)",
                  (connection_id, original_word, replacement_word))
    conn.commit()
    conn.close()

def get_word_replacements(connection_id):
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT original_word, replacement_word FROM word_replacements WHERE connection_id = ?", 
                  (connection_id,))
    replacements = cursor.fetchall()
    conn.close()
    return replacements

def clear_word_replacements(connection_id):
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM word_replacements WHERE connection_id = ?", (connection_id,))
    conn.commit()
    conn.close()

def save_transferred_post(connection_id, source_message_id, destination_message_id):
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transferred_posts (connection_id, source_message_id, destination_message_id) VALUES (?, ?, ?)",
                  (connection_id, source_message_id, destination_message_id))
    conn.commit()
    conn.close()

def get_destination_message_id(connection_id, source_message_id):
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT destination_message_id FROM transferred_posts WHERE connection_id = ? AND source_message_id = ?",
                  (connection_id, source_message_id))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def add_activity_log(connection_id, action_type, details):
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO activity_logs (connection_id, action_type, details) VALUES (?, ?, ?)",
                  (connection_id, action_type, details))
    conn.commit()
    conn.close()

def get_recent_activity_logs(limit=10):
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        SELECT a.id, c.source_channel, c.destination_channel, a.action_type, a.details, a.created_at
        FROM activity_logs a
        JOIN channel_connections c ON a.connection_id = c.id
        ORDER BY a.created_at DESC
        LIMIT ?
    """, (limit,))
    logs = cursor.fetchall()
    conn.close()
    return logs

# تابع افزودن واترمارک به تصویر
def add_watermark(image_bytes, watermark_text):
    image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    txt_layer = Image.new('RGBA', image.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(txt_layer)

    width, height = image.size

    # بهتر کردن سایز داینامیک فونت
    dynamic_font_size = 70

    try:
        font = ImageFont.truetype("arial.ttf", dynamic_font_size)
    except IOError:
        font = ImageFont.load_default()

    margin = 20

    bbox = draw.textbbox((0, 2), watermark_text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    x = margin
    y = height - text_height - margin

    draw.text((x, y), watermark_text, font=font, fill=(255, 255, 255, 128))

    combined = Image.alpha_composite(image, txt_layer)

    output = io.BytesIO()
    combined.convert("RGB").save(output, format="JPEG")
    output.seek(0)
    return output

# تابع جایگزینی کلمات در متن بر اساس قوانین تعریف شده
def replace_words(text, connection_id):
    if text is None:
        return None
        
    replacements = get_word_replacements(connection_id)
    for original, replacement in replacements:
        text = text.replace(original, replacement)
    
    return text

def set_connection_watermark(connection_id, watermark_text):
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE channel_connections SET watermark_text = ? WHERE id = ?", (watermark_text, connection_id))
    conn.commit()
    conn.close()

def get_connection_watermark(connection_id):
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT watermark_text FROM channel_connections WHERE id = ?", (connection_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def frame_to_bytes(frame):
    output = io.BytesIO()
    frame.save(output, format="PNG")
    output.seek(0)
    return output.getvalue()

def add_text_watermark_to_video(video_bytes: bytes, watermark_text: str, is_gif: bool = False) -> bytes:
    try:
        # اعتبارسنجی داده ورودی
        if not video_bytes or len(video_bytes) < 100:  # حداقل اندازه برای یک فایل گیف/ویدیو
            logger.error("⛔️ داده ورودی نامعتبر یا خالی است")
            return video_bytes

        logger.debug(f"اندازه داده ورودی: {len(video_bytes)} بایت")

        suffix = '.gif' if is_gif else '.mp4'

        # ایجاد فایل موقت برای ورودی
        input_path = tempfile.mktemp(suffix=suffix)
        with open(input_path, 'wb') as input_file:
            input_file.write(video_bytes)
            input_file.flush()

        # ایجاد فایل موقت برای خروجی
        output_path = tempfile.mktemp(suffix=suffix)

        # مسیر فونت
        font_path = "Impact.ttf"  # اطمینان حاصل کنید که این فونت وجود دارد

        # فیلتر drawtext برای واترمارک
        drawtext_filter = (
            f"drawtext=fontfile={font_path}:text='{watermark_text}':"
            f"fontcolor=white@0.4:fontsize=70:box=1:boxcolor=black@0.3:"
            f"x=mod((w/6)*mod(t\,6)\,w):y=mod((h/6)*mod(t\,6)\,h)"
        )

        # دستور FFmpeg
        cmd = [
            'ffmpeg',
            '-y',
            '-i', input_path,
            '-vf', drawtext_filter,
            '-c:v', 'gif' if is_gif else 'libx264',
            '-c:a', 'copy' if not is_gif else 'none',
            output_path
        ]

        # اجرای FFmpeg با گرفتن خروجی خطا
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.debug(f"FFmpeg output: {result.stdout}")

        # خواندن فایل خروجی
        with open(output_path, 'rb') as f:
            return f.read()

    except subprocess.CalledProcessError as e:
        logger.error(f"⛔️ خطای FFmpeg: {e.stderr}")
        return video_bytes
    except FileNotFoundError as e:
        logger.error(f"⛔️ FFmpeg یا فونت یافت نشد: {e}")
        return video_bytes
    except Exception as e:
        logger.error(f"⛔️ خطا در واترمارک‌گذاری: {e}")
        return video_bytes
    finally:
        # حذف فایل‌های موقت
        for path in [input_path, output_path]:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

def get_reply_message_id(source_reply_id: int, message_map: dict) -> int | None:
    """
    اگر پیام ریپلای‌شده در مقصد ارسال شده باشد، آیدی پیام مقصد را برمی‌گرداند.
    در غیر این صورت None برمی‌گرداند.
    """
    return message_map.get(source_reply_id)

def get_reply_dest_id_if_exists(conn_id, source_reply_id):
    """
    بررسی می‌کند که آیا پیام ریپلای‌شده از کانال منبع قبلاً به مقصد منتقل شده یا نه.
    """
    return get_destination_message_id(conn_id, source_reply_id)

def set_connection_active(connection_id: int, active: bool):
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE channel_connections SET is_active = ? WHERE id = ?", (1 if active else 0, connection_id))
    conn.commit()
    conn.close()

def get_connection_by_id(connection_id: int):
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("""SELECT id, source_channel, destination_channel, is_active, last_scanned_message_id
                      FROM channel_connections WHERE id = ?""", (connection_id,))
    row = cursor.fetchone()
    conn.close()
    return row  # (id, source, dest, is_active, last_scanned)

def get_active_connections():
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("""SELECT id, source_channel, destination_channel
                      FROM channel_connections WHERE is_active = 1""")
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_last_scanned_message_id(connection_id: int) -> int:
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT last_scanned_message_id FROM channel_connections WHERE id = ?", (connection_id,))
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row and row[0] else 0

def update_last_scanned_message_id(connection_id: int, msg_id: int):
    conn = sqlite3.connect('transfer_bot.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE channel_connections SET last_scanned_message_id = ? WHERE id = ?", (int(msg_id), connection_id))
    conn.commit()
    conn.close()

# مدیریت پنل ادمین
@bot.on_message(filters.command("start") & filters.private & filters.user(ADMIN_ID))
async def start_command(client, message: Message):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ افزودن اتصال جدید", callback_data="add_connection")],
        [InlineKeyboardButton("⚙️ مدیریت اتصال‌ها", callback_data="manage_connections")],
        [InlineKeyboardButton("✅ تست اتصال", callback_data="test_connection")],
        [InlineKeyboardButton("✏️ مدیریت کلمات جایگزین", callback_data="manage_replacements")],
        [InlineKeyboardButton("🖋 مدیریت واترمارک‌ها", callback_data="manage_watermarks")],
        [InlineKeyboardButton("📋 لیست کانال‌های متصل", callback_data="list_connections")],
        [InlineKeyboardButton("📊 وضعیت فعلی ربات", callback_data="bot_status")],
        [InlineKeyboardButton("📝 مشاهده لاگ فعالیت‌ها", callback_data="view_logs")]
    ])
    await message.reply(
        "به ربات انتقال‌دهنده خوش آمدید. یکی را انتخاب کنید:",
        reply_markup=keyboard
    )
    user_states.pop(message.from_user.id, None)

# دریافت کالبک‌های ناشی از دکمه‌های اینلاین
@bot.on_callback_query()
async def handle_callback(client, callback_query: CallbackQuery):
    data = callback_query.data
    
    if data == "add_connection":
        user_states[callback_query.from_user.id] = {"step": "waiting_source"}
        await callback_query.message.edit_text(
            "🔹 لطفا آیدی عددی یا یوزرنیم کانال منبع که اکانت کاربری عضو آن است را وارد کنید.\n\nمثال: `@destination_channel` یا `-100xxxxxxxxxx`",
            parse_mode=ParseMode.HTML
        )

    elif data == "list_connections":
        connections = get_all_connections()
        if connections:
            text = "🔄 لیست اتصال‌های فعلی:\n\n"
            buttons = []
            
            for conn_id, source, dest in connections:
                text += f"{conn_id}. از {source} به {dest}\n"
                buttons.append([
                    InlineKeyboardButton(f"حذف {source} → {dest}", callback_data=f"delete_{conn_id}")
                ])
            
            buttons.append([InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")])
            keyboard = InlineKeyboardMarkup(buttons)
            
            await callback_query.message.edit_text(text, reply_markup=keyboard)
        else:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")
            ]])
            await callback_query.message.edit_text("هیچ اتصالی تعریف نشده است.", reply_markup=keyboard)
    
    elif data == "manage_replacements":
        connections = get_all_connections()
        if connections:
            text = "برای مدیریت کلمات جایگزین، اتصال مورد نظر را انتخاب کنید:\n\n"
            buttons = []
            
            for conn_id, source, dest in connections:
                buttons.append([
                    InlineKeyboardButton(f"{source} → {dest}", callback_data=f"replace_{conn_id}")
                ])
            
            buttons.append([InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")])
            keyboard = InlineKeyboardMarkup(buttons)
            
            await callback_query.message.edit_text(text, reply_markup=keyboard)
        else:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")
            ]])
            await callback_query.message.edit_text("هیچ اتصالی برای مدیریت کلمات وجود ندارد.", reply_markup=keyboard)
    
    elif data.startswith("replace_"):
        conn_id = int(data.split("_")[1])

        # دریافت اطلاعات اتصال
        conn = sqlite3.connect('transfer_bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT source_channel, destination_channel FROM channel_connections WHERE id = ?", (conn_id,))
        connection = cursor.fetchone()
        cursor.execute("SELECT id, original_word, replacement_word FROM word_replacements WHERE connection_id = ?", (conn_id,))
        replacements = cursor.fetchall()
        conn.close()

        if connection:
            source, dest = connection
            text = f"🔹 کلمات جایگزین اتصال {source} → {dest}:\n\n"
            buttons = []

            if replacements:
                for rep_id, original, replacement in replacements:
                    text += f"- {original} ➔ {replacement}\n"
                    buttons.append([InlineKeyboardButton(f"❌ حذف {original}", callback_data=f"delword_{rep_id}_{conn_id}")])
            else:
                text += "⚠️ هیچ کلمه جایگزینی تعریف نشده است.\n"

            buttons.append([InlineKeyboardButton("➕ افزودن کلمه جدید", callback_data=f"addword_{conn_id}")])
            buttons.append([InlineKeyboardButton("🗑 حذف همه کلمات", callback_data=f"clear_replacements_{conn_id}")])
            buttons.append([InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")])

            keyboard = InlineKeyboardMarkup(buttons)

            await callback_query.message.edit_text(text, reply_markup=keyboard)

        else:
            await callback_query.message.edit_text(
                "❌ اتصال مورد نظر یافت نشد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")]])
            )


    elif data.startswith("clear_replacements_"):
        conn_id = int(data.split("_")[2])
        clear_word_replacements(conn_id)
        
        # دریافت اطلاعات اتصال برای نمایش در پیام
        conn = sqlite3.connect('transfer_bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT source_channel, destination_channel FROM channel_connections WHERE id = ?", (conn_id,))
        connection = cursor.fetchone()
        conn.close()
        
        if connection:
            source, dest = connection
            add_activity_log(conn_id, "clear_replacements", f"تمام کلمات جایگزین برای اتصال {source} → {dest} پاک شدند")
            
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")
            ]])
            
            await callback_query.message.edit_text(
                f"تمام کلمات جایگزین برای اتصال {source} → {dest} با موفقیت پاک شدند.",
                reply_markup=keyboard
            )
    
    elif data.startswith("delword_"):
        parts = data.split("_")
        word_id = int(parts[1])
        conn_id = int(parts[2])

        conn = sqlite3.connect('transfer_bot.db')
        cursor = conn.cursor()
        cursor.execute("DELETE FROM word_replacements WHERE id = ?", (word_id,))
        conn.commit()
        conn.close()

        await callback_query.answer("✅ کلمه حذف شد.", show_alert=False)
        await handle_callback(client, CallbackQuery(id=callback_query.id, from_user=callback_query.from_user, chat_instance=callback_query.chat_instance, message=callback_query.message, data=f"replace_{conn_id}"))


    elif data.startswith("delete_"):
        conn_id = int(data.split("_")[1])
        
        # دریافت اطلاعات اتصال برای نمایش در لاگ
        conn = sqlite3.connect('transfer_bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT source_channel, destination_channel FROM channel_connections WHERE id = ?", (conn_id,))
        connection = cursor.fetchone()
        
        if connection:
            source, dest = connection
            # ثبت لاگ قبل از حذف
            add_activity_log(conn_id, "delete_connection", f"اتصال {source} → {dest} حذف شد")
            
        # حذف اتصال
        delete_connection(conn_id)
        conn.close()
        
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")
        ]])
        
        await callback_query.message.edit_text(
            f"اتصال با شناسه {conn_id} با موفقیت حذف شد.",
            reply_markup=keyboard
        )
    
    elif data == "bot_status":
        # دریافت وضعیت فعلی ربات
        connections = get_all_connections()
        recent_logs = get_recent_activity_logs(5)
        
        text = "📊 وضعیت فعلی ربات:\n\n"
        text += f"🔄 تعداد اتصال‌های فعال: {len(connections)}\n"
        text += f"⏱ زمان فعلی سرور: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        
        if recent_logs:
            text += "🔍 آخرین فعالیت‌ها:\n"
            for _, source, dest, action, details, time in recent_logs:
                action_type = {
                    "transfer": "انتقال پست",
                    "edit": "ویرایش پست",
                    "add_connection": "افزودن اتصال",
                    "delete_connection": "حذف اتصال",
                    "add_replacement": "افزودن کلمه جایگزین",
                    "clear_replacements": "پاک کردن کلمات جایگزین"
                }.get(action, action)
                
                text += f"• {action_type} - {time}: {details}\n"
        
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")
        ]])
        
        await callback_query.message.edit_text(text, reply_markup=keyboard)
    
    elif data == "view_logs":
        logs = get_recent_activity_logs(20)
        
        if logs:
            text = "📋 آخرین فعالیت‌های ربات:\n\n"
            for _, source, dest, action, details, time in logs:
                action_type = {
                    "transfer": "انتقال پست",
                    "edit": "ویرایش پست",
                    "add_connection": "افزودن اتصال",
                    "delete_connection": "حذف اتصال",
                    "add_replacement": "افزودن کلمه جایگزین",
                    "clear_replacements": "پاک کردن کلمات جایگزین"
                }.get(action, action)
                
                text += f"• {time} - {action_type}:\n"
                text += f"  {source} → {dest}: {details}\n\n"
        else:
            text = "هیچ لاگی یافت نشد."
        
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")
        ]])
        
        # اگر متن خیلی طولانی باشد، آن را کوتاه می‌کنیم
        if len(text) > 4000:
            text = text[:3900] + "...\n\n(نمایش بخشی از لاگ‌ها به دلیل محدودیت حجم پیام)"
        
        await callback_query.message.edit_text(text, reply_markup=keyboard)
    
    elif data == "back_to_main":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ افزودن اتصال جدید", callback_data="add_connection")],
            [InlineKeyboardButton("⚙️ مدیریت اتصال‌ها", callback_data="manage_connections")],
            [InlineKeyboardButton("✅ تست اتصال", callback_data="test_connection")],
            [InlineKeyboardButton("✏️ مدیریت کلمات جایگزین", callback_data="manage_replacements")],
            [InlineKeyboardButton("🖋 مدیریت واترمارک‌ها", callback_data="manage_watermarks")],
            [InlineKeyboardButton("📋 لیست کانال‌های متصل", callback_data="list_connections")],
            [InlineKeyboardButton("📊 وضعیت فعلی ربات", callback_data="bot_status")],
            [InlineKeyboardButton("📝 مشاهده لاگ فعالیت‌ها", callback_data="view_logs")]
        ])
        await callback_query.message.edit_text(
            "به ربات انتقال‌دهنده خوش آمدید. یکی را انتخاب کنید:",
            reply_markup=keyboard
        )
    
    elif data.startswith("addword_"):
        conn_id = int(data.split("_")[1])

        # دریافت اطلاعات اتصال
        conn = sqlite3.connect('transfer_bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT source_channel, destination_channel FROM channel_connections WHERE id = ?", (conn_id,))
        connection = cursor.fetchone()
        conn.close()

        if connection:
            source, dest = connection
            user_states[callback_query.from_user.id] = {
                "step": "waiting_original_word",
                "conn_id": conn_id,
                "source": source,
                "dest": dest
            }
            await callback_query.message.edit_text(
                f"🔹 لطفاً کلمه‌ای که در اتصال {source} → {dest} باید جایگزین شود را وارد کنید:",
                parse_mode=ParseMode.HTML
            )
        else:
            await callback_query.message.edit_text(
                "❌ اتصال مورد نظر یافت نشد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")]])
            )
    elif data == "manage_watermarks":
        connections = get_all_connections()
        if connections:
            text = "🔹 برای مدیریت واترمارک، اتصال موردنظر را انتخاب کنید:\n\n"
            buttons = []
            for conn_id, source, dest in connections:
                buttons.append([
                    InlineKeyboardButton(f"{source} → {dest}", callback_data=f"watermark_{conn_id}")
                ])
            buttons.append([InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")])

            keyboard = InlineKeyboardMarkup(buttons)
            await callback_query.message.edit_text(text, reply_markup=keyboard)
        else:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")]])
            await callback_query.message.edit_text("هیچ اتصالی برای مدیریت واترمارک وجود ندارد.", reply_markup=keyboard)

    elif data.startswith("watermark_"):
        conn_id = int(data.split("_")[1])

        conn = sqlite3.connect('transfer_bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT source_channel, destination_channel, watermark_text FROM channel_connections WHERE id = ?", (conn_id,))
        connection = cursor.fetchone()
        conn.close()

        if connection:
            source, dest, current_watermark = connection
            text = f"🔹 اتصال {source} → {dest}\n\n"
            if current_watermark:
                text += f"واترمارک فعلی: `{current_watermark}`"
            else:
                text += "⚠️ واترمارکی تعریف نشده."

            buttons = [
                [InlineKeyboardButton("✏️ تغییر واترمارک", callback_data=f"setwatermark_{conn_id}")],
                [InlineKeyboardButton("🗑 حذف واترمارک", callback_data=f"delwatermark_{conn_id}")],
                [InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")]
            ]

            await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.MARKDOWN)

    elif data.startswith("setwatermark_"):
        conn_id = int(data.split("_")[1])
        user_states[callback_query.from_user.id] = {
            "step": "waiting_watermark_text",
            "conn_id": conn_id
        }
        await callback_query.message.edit_text(
            "🔹 لطفاً متن جدید واترمارک را ارسال کنید:",
            parse_mode=ParseMode.HTML
        )

    elif data.startswith("delwatermark_"):
        conn_id = int(data.split("_")[1])
        set_connection_watermark(conn_id, None)
        await callback_query.message.edit_text(
            "✅ واترمارک اتصال حذف شد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")]])
        )

    elif data == "test_connection":
        connections = get_all_connections()
        if connections:
            buttons = []
            for conn_id, source, dest in connections:
                buttons.append([InlineKeyboardButton(f"{source} → {dest}", callback_data=f"test_{conn_id}")])
            buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_main")])
            await callback_query.message.edit_text(
                "🔍 لطفاً یکی از اتصال‌ها را برای تست انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        else:
            await callback_query.message.edit_text(
                "⚠️ هیچ اتصالی برای تست یافت نشد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_main")]])
            )

    elif data.startswith("test_"):
        conn_id = int(data.split("_")[1])

        conn = sqlite3.connect('transfer_bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT source_channel, destination_channel FROM channel_connections WHERE id = ?", (conn_id,))
        connection = cursor.fetchone()
        conn.close()

        if not connection:
            await callback_query.message.reply("❌ اتصال مورد نظر یافت نشد.")
            return

        source_channel, dest_channel = connection

        try:
            source_chat = await user.get_chat(source_channel)
            dest_chat = await user.get_chat(dest_channel)

            last_msg = None
            async for msg in user.get_chat_history(source_chat.id, limit=1):
                last_msg = msg
                break

            if not last_msg:
                await callback_query.message.reply("⚠️ هیچ پستی در کانال منبع یافت نشد.")
                return

            sent = None
            if last_msg.text:
                sent = await user.send_message(dest_chat.id, last_msg.text)
            elif last_msg.photo:
                photo_file = await user.download_media(last_msg.photo, in_memory=True)
                sent = await user.send_photo(dest_chat.id, photo_file, caption=last_msg.caption)
            else:
                await callback_query.message.reply("⚠️ پیام آخر منبع فقط از نوع متن یا عکس باید باشد.")
                return

            test_msg = await user.send_message(dest_chat.id, "🧪 این یک پیام تست است و تا ۵ ثانیه دیگر حذف خواهد شد.")
            await asyncio.sleep(5)
            await user.delete_messages(dest_chat.id, [sent.id, test_msg.id])

            # مهم: این خط را حذف/نگه ندار
            # update_last_scanned_message_id(conn_id, last_msg.id)

            await callback_query.message.reply("✅ تست اتصال با موفقیت انجام شد. پیام‌ها حذف شدند.")
        except Exception as e:
            await callback_query.message.reply(f"❌ خطا در تست اتصال:\n<code>{str(e)}</code>", parse_mode="HTML")

    elif data in ["restricted_yes", "restricted_no"]:
        state = user_states.get(callback_query.from_user.id)
        if not state or state.get("step") != "waiting_restriction":
            await callback_query.answer("⚠️ ابتدا اطلاعات اتصال را وارد کنید.", show_alert=True)
            return

        source = state["source"]
        destination = state["destination"]
        is_restricted = 1 if data == "restricted_yes" else 0

        # افزودن اتصال اولیه
        conn_id = add_channel_connection(source, destination)

        # به‌روزرسانی فیلد is_restricted
        conn = sqlite3.connect("transfer_bot.db")
        cursor = conn.cursor()

        # اطمینان از وجود ستون
        try:
            cursor.execute("ALTER TABLE channel_connections ADD COLUMN is_restricted INTEGER DEFAULT 0")
        except:
            pass  # ستون از قبل وجود داشته

        cursor.execute("UPDATE channel_connections SET is_restricted = ? WHERE id = ?", (is_restricted, conn_id))
        conn.commit()
        conn.close()

        add_activity_log(conn_id, "add_connection", f"اتصال {source} → {destination} ثبت شد (محدود: {'بله' if is_restricted else 'خیر'}).")

        await callback_query.message.edit_text(
            f"✅ اتصال با موفقیت ثبت شد!\n\n"
            f"از `{source}` به `{destination}`\n"
            f"🔒 وضعیت محدود: {'✅ بله' if is_restricted else '❌ نه'}",
            parse_mode=ParseMode.MARKDOWN
        )

        user_states.pop(callback_query.from_user.id, None)
    
    elif data == "manage_connections":
        conns = get_all_connections()
        if not conns:
            await callback_query.message.edit_text(
                "هیچ اتصالی تعریف نشده.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت", callback_data="back_to_main")]])
            )
            return

        # واکشی وضعیت فعال بودن
        conn = sqlite3.connect('transfer_bot.db')
        cur = conn.cursor()
        cur.execute("SELECT id, is_active FROM channel_connections")
        status_map = {row[0]: row[1] for row in cur.fetchall()}
        conn.close()

        text = "⚙️ مدیریت اتصال‌ها:\n"
        buttons = []
        for conn_id, source, dest in conns:
            is_active = bool(status_map.get(conn_id, 1))
            state = "🟢 فعال" if is_active else "🔴 غیرفعال"
            text += f"\n{conn_id}. {source} → {dest}  | {state}"
            buttons.append([
                InlineKeyboardButton("⏮ بک‌فیل از ابتدا", callback_data=f"backfill_{conn_id}"),
                InlineKeyboardButton("تغییر وضعیت", callback_data=f"toggle_{conn_id}")
            ])
            buttons.append([
                InlineKeyboardButton("🗑 حذف", callback_data=f"delete_{conn_id}")
            ])
        buttons.append([InlineKeyboardButton("بازگشت", callback_data="back_to_main")])
        await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("toggle_"):
        conn_id = int(data.split("_")[1])
        row = get_connection_by_id(conn_id)
        if not row:
            await callback_query.answer("اتصال یافت نشد.", show_alert=True)
            return
        _, source, dest, is_active, _ = row
        new_state = not bool(is_active)
        set_connection_active(conn_id, new_state)
        add_activity_log(conn_id, "toggle_connection", f"{'فعال' if new_state else 'غیرفعال'} شد: {source} → {dest}")
        await callback_query.answer("وضعیت به‌روزرسانی شد.")
        await handle_callback(client, CallbackQuery(
            id=callback_query.id, from_user=callback_query.from_user,
            chat_instance=callback_query.chat_instance,
            message=callback_query.message, data="manage_connections"
        ))

    elif data.startswith("backfill_"):
        conn_id = int(data.split("_")[1])
        row = get_connection_by_id(conn_id)
        if not row:
            await callback_query.answer("اتصال یافت نشد.", show_alert=True)
            return
        await callback_query.message.edit_text("⏳ شروع بک‌فیل از ابتدا. از تکرار جلوگیری می‌شود...")
        try:
            transferred, scanned_upto = await backfill_connection(conn_id, from_start=True)
            await callback_query.message.edit_text(
                f"✅ بک‌فیل تمام شد.\n"
                f"پیام منتقل‌شده: {transferred}\n"
                f"آخرین پیام اسکن‌شده: {scanned_upto}\n",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت", callback_data="manage_connections")]])
            )
        except Exception as e:
            await callback_query.message.edit_text(
                f"❌ خطا در بک‌فیل:\n<code>{e}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت", callback_data="manage_connections")]])
            )

# افزودن اتصال جدید
@bot.on_message(filters.command("add") & filters.private & filters.user(ADMIN_ID))
async def add_connection_command(client, message):
    try:
        _, source_channel, destination_channel = message.text.split()
        
        # بررسی اعتبار کانال‌ها
        try:
            source_info = await user.get_chat(source_channel)
            dest_info = await user.get_chat(destination_channel)
            
            # افزودن اتصال به دیتابیس
            connection_id = add_channel_connection(source_channel, destination_channel)
            
            # ثبت در لاگ فعالیت‌ها
            add_activity_log(connection_id, "add_connection", f"اتصال از {source_channel} به {destination_channel} اضافه شد")
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("افزودن کلمات جایگزین", callback_data=f"replace_{connection_id}")],
                [InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")]
            ])
            
            await message.reply(
                f"✅ اتصال جدید با موفقیت اضافه شد:\n\n"
                f"از: {source_info.title} ({source_channel})\n"
                f"به: {dest_info.title} ({destination_channel})\n\n"
                f"شناسه اتصال: {connection_id}\n"
                f"اکنون می‌توانید کلمات جایگزین را تنظیم کنید.",
                reply_markup=keyboard
            )
            
        except BadRequest as e:
            await message.reply(f"❌ خطا در بررسی کانال‌ها: {str(e)}\n\nلطفاً مطمئن شوید که کانال‌ها وجود دارند و حساب کاربری به آنها دسترسی دارد.")
            
    except ValueError:
        await message.reply("❌ فرمت نادرست. لطفاً به این شکل وارد کنید:\n/add آیدی_کانال_مبدا آیدی_کانال_مقصد")

# افزودن کلمه جایگزین
@bot.on_message(filters.command("replace") & filters.private & filters.user(ADMIN_ID))
async def add_replacement_command(client, message):
    try:
        parts = message.text.split(maxsplit=3)
        if len(parts) != 4:
            raise ValueError("تعداد پارامترها نادرست است")
            
        _, conn_id_str, original_word, replacement_word = parts
        conn_id = int(conn_id_str)
        
        # بررسی وجود اتصال
        conn = sqlite3.connect('transfer_bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT source_channel, destination_channel FROM channel_connections WHERE id = ?", (conn_id,))
        connection = cursor.fetchone()
        conn.close()
        
        if not connection:
            await message.reply(f"❌ اتصالی با شناسه {conn_id} یافت نشد.")
            return
        
        # افزودن کلمه جایگزین
        add_word_replacement(conn_id, original_word, replacement_word)
        
        # ثبت در لاگ فعالیت‌ها
        source, dest = connection
        add_activity_log(conn_id, "add_replacement", f"کلمه '{original_word}' به '{replacement_word}' در اتصال {source} → {dest} اضافه شد")
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("مدیریت کلمات بیشتر", callback_data=f"replace_{conn_id}")],
            [InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="back_to_main")]
        ])
        
        await message.reply(
            f"✅ کلمه جایگزین با موفقیت اضافه شد:\n\n"
            f"اتصال: {source} → {dest}\n"
            f"کلمه اصلی: {original_word}\n"
            f"کلمه جایگزین: {replacement_word}",
            reply_markup=keyboard
        )
        
    except ValueError as e:
        await message.reply(
            f"❌ فرمت نادرست: {str(e)}\n\n"
            f"لطفاً به این شکل وارد کنید:\n"
            f"/replace شناسه_اتصال کلمه_اصلی کلمه_جایگزین"
        )

# گوش دادن به پیام‌های کانال‌های مبدا و انتقال آنها
@user.on_message(filters.channel)
async def handle_channel_messages(client, message: Message):
    connections = get_active_connections()  # فقط فعال‌ها
    source_chat_id = message.chat.id

    for conn_id, source_channel, destination_channel in connections:
        try:
            source_chat = await client.get_chat(source_channel)
            if source_chat.id != source_chat_id:
                continue

            dest_chat = await client.get_chat(destination_channel)

            # ریپلای اگر پیام مرجع در مقصد موجود باشد
            reply_to_message_id = None
            if message.reply_to_message:
                reply_to_message_id = get_destination_message_id(conn_id, message.reply_to_message.id)

            # جایگزینی کلمات
            caption = replace_words(message.caption, conn_id) if message.caption else None
            text = replace_words(message.text, conn_id) if message.text else None

            sent_message = None

            if message.photo:
                photo_file = await client.download_media(message.photo, in_memory=True)
                image = Image.open(io.BytesIO(photo_file.getvalue())).convert("RGBA")
                width, height = image.size

                watermark_text = get_connection_watermark(conn_id) or (f"@{dest_chat.username}" if getattr(dest_chat, "username", None) else "")

                txt_layer = Image.new("RGBA", image.size, (255, 255, 255, 0))
                draw = ImageDraw.Draw(txt_layer)

                try:
                    font = ImageFont.truetype("Impact.ttf", 70)
                except IOError:
                    font = ImageFont.load_default()

                margin_x = 70 * 4
                margin_y = 70 * 4

                for x in range(0, width, margin_x):
                    for y in range(0, height, margin_y):
                        draw.text((x, y), watermark_text, font=font, fill=(255, 255, 255, 128))

                txt_layer = txt_layer.filter(ImageFilter.GaussianBlur(radius=1.5))
                combined = Image.alpha_composite(image, txt_layer)

                output = io.BytesIO()
                combined.convert("RGB").save(output, format="JPEG")
                output.seek(0)

                sent_message = await client.send_photo(
                    chat_id=dest_chat.id,
                    photo=output,
                    caption=caption,
                    reply_to_message_id=reply_to_message_id
                )

            elif message.animation:
                animation_file = await client.download_media(message.animation, in_memory=True)
                watermark_text = get_connection_watermark(conn_id) or (f"@{dest_chat.username}" if getattr(dest_chat, "username", None) else "")
                try:
                    watermarked_gif_bytes = add_text_watermark_to_video(animation_file.getvalue(), watermark_text, is_gif=True)
                    sent_message = await client.send_animation(
                        chat_id=dest_chat.id,
                        animation=watermarked_gif_bytes,
                        caption=text or "",
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=reply_to_message_id
                    )
                except Exception:
                    sent_message = await client.send_animation(
                        chat_id=dest_chat.id,
                        animation=animation_file,
                        caption=text or "",
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=reply_to_message_id
                    )

            elif message.video:
                video_file = await client.download_media(message.video, in_memory=True)
                watermark_text = get_connection_watermark(conn_id) or (f"@{dest_chat.username}" if getattr(dest_chat, "username", None) else "")
                watermarked = add_text_watermark_to_video(video_file.getvalue(), watermark_text)

                sent_message = await client.send_video(
                    chat_id=dest_chat.id,
                    video=io.BytesIO(watermarked),
                    caption=caption,
                    reply_to_message_id=reply_to_message_id
                )

            elif message.sticker:
                sent_message = await client.send_sticker(
                    chat_id=dest_chat.id,
                    sticker=message.sticker.file_id,
                    reply_to_message_id=reply_to_message_id
                )

            elif text:
                sent_message = await client.send_message(
                    chat_id=dest_chat.id,
                    text=text,
                    reply_to_message_id=reply_to_message_id
                )

            elif message.voice:
                voice_file = await client.download_media(message.voice, in_memory=True)
                sent_message = await client.send_voice(
                    chat_id=dest_chat.id,
                    voice=voice_file,
                    caption=caption,
                    reply_to_message_id=reply_to_message_id
                )

            if sent_message:
                save_transferred_post(conn_id, message.id, sent_message.id)
                update_last_scanned_message_id(conn_id, message.id)

                message_type = (
                    "تصویر" if message.photo else
                    "ویدیو" if message.video else
                    "گیف" if message.animation else
                    "صوت" if message.voice else
                    "استیکر" if message.sticker else
                    "متن"
                )
                log_details = f"پست {message_type} از {source_channel} به {destination_channel} منتقل شد"
                add_activity_log(conn_id, "transfer", log_details)

        except Exception as e:
            logger.error(f"⛔️ خطا در انتقال پیام از {source_channel} به {destination_channel}: {str(e)}")
           

# محدود کردن دسترسی به ربات فقط برای ادمین
@bot.on_message(filters.private & ~filters.user(ADMIN_ID))
async def unauthorized_access(client, message):
    await message.reply("⛔️ شما اجازه دسترسی به این ربات را ندارید.")

@bot.on_message(filters.private & filters.user(ADMIN_ID))
async def handle_admin_messages(client, message: Message):
    user_id = message.from_user.id
    text = message.text.strip()
    state = user_states.get(user_id)

    if text == "/start":
        user_states.pop(user_id, None)
        await start_command(client, message)
        return

    if not state:
        await message.reply("⚠️ لطفاً از منوی اصلی شروع کنید یا روی /start بزنید.")
        return

    # مدیریت افزودن اتصال (Add Connection)
    # مدیریت افزودن اتصال (Add Connection)
    if state["step"] == "waiting_source":
        source_input = text.strip()

        if not (source_input.startswith("@") or source_input.startswith("-100") or source_input.isdigit()):
            await message.reply("❌ لطفاً یوزرنیم با '@' یا آیدی عددی معتبر وارد کنید.")
            return

        # فقط ذخیره ورودی کاربر بدون بررسی
        user_states[user_id]["source"] = source_input
        user_states[user_id]["step"] = "waiting_destination"

        await message.reply(
            "🔹 لطفاً آیدی عددی یا یوزرنیم کانال مقصد که ربات در آن ادمین است را وارد کنید.\n\nمثال:\n`@destination_channel`\nیا\n`-100xxxxxxxxxx`",
            parse_mode=ParseMode.HTML
        )
        return

    if state["step"] == "waiting_destination":
        dest_input = text.strip()

        if not (dest_input.startswith("@") or dest_input.startswith("-100") or dest_input.isdigit()):
            await message.reply("❌ لطفاً یوزرنیم با '@' یا آیدی عددی معتبر وارد کنید.")
            return

        user_states[user_id]["destination"] = dest_input
        user_states[user_id]["step"] = "waiting_restriction"

        await message.reply(
            "❓ آیا کانال منبع این اتصال، قابلیت فوروارد و ذخیره پیام را بسته است؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ بله، محدود است", callback_data="restricted_yes")],
                [InlineKeyboardButton("❌ نه، آزاد است", callback_data="restricted_no")]
            ])
        )
        return

    # مدیریت افزودن کلمه جایگزین (Replacement)
    if state["step"] == "waiting_original_word":
        original_word = text

        user_states[user_id]["original_word"] = original_word
        user_states[user_id]["step"] = "waiting_replacement_word"

        await message.reply(
            f"🔹 حالا لطفاً بنویسید که کلمه {original_word} در اتصال {state['source']} → {state['dest']} با چه چیزی جایگزین شود:",
            parse_mode=ParseMode.HTML
        )
        return

    if state["step"] == "waiting_replacement_word":
        replacement_word = text
        conn_id = state["conn_id"]
        original_word = state["original_word"]

        add_word_replacement(conn_id, original_word, replacement_word)
        add_activity_log(conn_id, "add_replacement", f"کلمه '{original_word}' → '{replacement_word}' برای اتصال {state['source']} → {state['dest']} افزوده شد.")

        await message.reply(
            f"✅ موفقیت‌آمیز!\n\n"
            f"در اتصال {state['source']} → {state['dest']} کلمه {original_word} با {replacement_word} جایگزین شد.",
            parse_mode=ParseMode.HTML
        )

        user_states.pop(user_id, None)
        await start_command(client, message)
        return
    
    if state["step"] == "waiting_watermark_text":
        watermark_text = text
        conn_id = state["conn_id"]
        set_connection_watermark(conn_id, watermark_text)

        await message.reply(
            f"✅ واترمارک با موفقیت تغییر یافت به: `{watermark_text}`",
            parse_mode=ParseMode.MARKDOWN
        )

        user_states.pop(user_id, None)
        await start_command(client, message)
        return


# ورود به حساب کاربری تلگرام قبل از شروع ربات
async def main():
    # ایجاد دیتابیس اگر وجود نداشته باشد
    create_database()
    
    # شروع کلاینت ربات
    await bot.start()
    logger.info("ربات آغاز به کار کرد")
    
    # ورود به حساب کاربری تلگرام
    await user.start()
    logger.info("کلاینت کاربری متصل شد")
    
    loop.create_task(check_restricted_channels_loop())

    # منتظر ماندن برای سیگنال خروج
    await idle()
    
    # خروج از ربات و حساب کاربری
    await bot.stop()
    await user.stop()

async def check_restricted_channels_loop():
    while True:
        await asyncio.sleep(60)

        conn = sqlite3.connect('transfer_bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id, source_channel, destination_channel FROM channel_connections WHERE is_restricted = 1")
        restricted_connections = cursor.fetchall()
        conn.close()

        for conn_id, source, destination in restricted_connections:
            try:
                async for msg in user.get_chat_history(source, limit=15):
                    if get_destination_message_id(conn_id, msg.id):
                        break  # این پیام قبلاً منتقل شده

                    dest_chat = await user.get_chat(destination)
                    caption = replace_words(msg.caption, conn_id) if msg.caption else None
                    text = replace_words(msg.text, conn_id) if msg.text else None
                    watermark_text = get_connection_watermark(conn_id) or f"@{dest_chat.username}"
                    reply_to_message_id = None

                    if msg.reply_to_message:
                        try:
                            reply_to_message_id = get_destination_message_id(conn_id, msg.reply_to_message.id)
                        except:
                            reply_to_message_id = None  # اگر ریپلای ناقص بود، نادیده بگیر

                    sent = None

                    if msg.photo:
                        photo_file = await user.download_media(msg.photo, in_memory=True)
                        image = Image.open(io.BytesIO(photo_file.getvalue())).convert("RGBA")
                        width, height = image.size

                        txt_layer = Image.new("RGBA", image.size, (255, 255, 255, 0))
                        draw = ImageDraw.Draw(txt_layer)

                        try:
                            font = ImageFont.truetype("Impact.ttf", 70)
                        except IOError:
                            font = ImageFont.load_default()

                        margin_x = 70 * 4
                        margin_y = 70 * 4

                        for x in range(0, width, margin_x):
                            for y in range(0, height, margin_y):
                                draw.text((x, y), watermark_text, font=font, fill=(255, 255, 255, 128))

                        txt_layer = txt_layer.filter(ImageFilter.GaussianBlur(radius=1.5))
                        combined = Image.alpha_composite(image, txt_layer)

                        output = io.BytesIO()
                        combined.convert("RGB").save(output, format="JPEG")
                        output.seek(0)

                        sent = await user.send_photo(chat_id=dest_chat.id, photo=output, caption=caption, reply_to_message_id=reply_to_message_id)

                    elif msg.video:
                        video_file = await user.download_media(msg.video, in_memory=True)
                        watermarked = add_text_watermark_to_video(video_file.getvalue(), watermark_text)
                        sent = await user.send_video(chat_id=dest_chat.id, video=io.BytesIO(watermarked), caption=caption, reply_to_message_id=reply_to_message_id)

                    elif msg.animation:
                        animation_file = await user.download_media(msg.animation, in_memory=True)
                        gif = Image.open(io.BytesIO(animation_file.getvalue()))

                        frames = []
                        try:
                            font_size = max(30, gif.width // 15)
                            font = ImageFont.truetype("Impact.ttf", font_size)
                        except IOError:
                            font = ImageFont.load_default()

                        margin_x = font_size * 2
                        margin_y = font_size * 2

                        for frame in ImageSequence.Iterator(gif):
                            frame = frame.convert("RGBA")
                            txt_layer = Image.new('RGBA', frame.size, (255, 255, 255, 0))
                            draw = ImageDraw.Draw(txt_layer)

                            for x in range(0, frame.width, margin_x):
                                for y in range(0, frame.height, margin_y):
                                    draw.text((x, y), watermark_text, font=font, fill=(255, 255, 255, 128))

                            txt_layer = txt_layer.filter(ImageFilter.GaussianBlur(radius=1))
                            combined = Image.alpha_composite(frame, txt_layer)
                            frames.append(combined)

                        output = io.BytesIO()
                        frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], loop=0, duration=gif.info.get('duration', 100))
                        output.seek(0)

                        sent = await user.send_animation(chat_id=dest_chat.id, animation=output, caption=caption, reply_to_message_id=reply_to_message_id)

                    elif msg.sticker:
                        sent = await user.send_sticker(chat_id=dest_chat.id, sticker=msg.sticker.file_id, reply_to_message_id=reply_to_message_id)

                    elif text:
                        sent = await user.send_message(chat_id=dest_chat.id, text=text, reply_to_message_id=reply_to_message_id)

                    elif msg.voice:
                        voice_file = await user.download_media(msg.voice, in_memory=True)
                        sent = await user.send_voice(chat_id=dest_chat.id, voice=voice_file, caption=caption, reply_to_message_id=reply_to_message_id)

                    if sent:
                        save_transferred_post(conn_id, msg.id, sent.id)
                        add_activity_log(conn_id, "transfer", f"پست محدود از {source} به {destination} منتقل شد.")
                        break  # فقط یک پیام منتقل شود در هر بررسی

            except Exception as e:
                logger.error(f"⛔️ خطا در بررسی اتصال محدود {source} → {destination}: {e}")

async def backfill_connection(connection_id: int, batch_size: int = 200, from_start: bool = False) -> tuple[int, int]:
    """
    همهٔ پیام‌های یک کانال را از ابتدا تا امروز منتقل می‌کند.
    - اگر from_start=True باشد، last_scanned نادیده گرفته می‌شود تا واقعا از پیام 1 شروع کند.
    - از duplicated با جدول transferred_posts جلوگیری می‌شود.
    خروجی: (تعداد منتقل‌شده، آخرین msg_id اسکن‌شده)
    """
    row = get_connection_by_id(connection_id)
    if not row:
        raise ValueError("اتصال یافت نشد.")
    _, source, dest, _, last_scanned = row

    src_chat = await user.get_chat(source)
    dst_chat = await user.get_chat(dest)

    async def _send_like_realtime(msg: Message, reply_to_message_id: int | None):
        caption = replace_words(msg.caption, connection_id) if msg.caption else None
        text = replace_words(msg.text, connection_id) if msg.text else None
        watermark_text = get_connection_watermark(connection_id) or (f"@{dst_chat.username}" if getattr(dst_chat, "username", None) else "")

        sent = None
        if msg.photo:
            photo_file = await user.download_media(msg.photo, in_memory=True)
            image = Image.open(io.BytesIO(photo_file.getvalue())).convert("RGBA")
            txt_layer = Image.new("RGBA", image.size, (255, 255, 255, 0))
            draw = ImageDraw.Draw(txt_layer)
            try:
                font = ImageFont.truetype("Impact.ttf", 70)
            except IOError:
                font = ImageFont.load_default()
            width, height = image.size
            margin_x = 70 * 4
            margin_y = 70 * 4
            for x in range(0, width, margin_x):
                for y in range(0, height, margin_y):
                    draw.text((x, y), watermark_text, font=font, fill=(255, 255, 255, 128))
            txt_layer = txt_layer.filter(ImageFilter.GaussianBlur(radius=1.5))
            output = io.BytesIO()
            Image.alpha_composite(image, txt_layer).convert("RGB").save(output, format="JPEG")
            output.seek(0)
            sent = await user.send_photo(dst_chat.id, output, caption=caption, reply_to_message_id=reply_to_message_id)

        elif msg.animation:
            animation_file = await user.download_media(msg.animation, in_memory=True)
            try:
                watermarked_gif_bytes = add_text_watermark_to_video(animation_file.getvalue(), watermark_text, is_gif=True)
                sent = await user.send_animation(dst_chat.id, watermarked_gif_bytes, caption=text or "", parse_mode=ParseMode.HTML, reply_to_message_id=reply_to_message_id)
            except Exception:
                sent = await user.send_animation(dst_chat.id, animation_file, caption=text or "", parse_mode=ParseMode.HTML, reply_to_message_id=reply_to_message_id)

        elif msg.video:
            video_file = await user.download_media(msg.video, in_memory=True)
            watermarked = add_text_watermark_to_video(video_file.getvalue(), watermark_text)
            sent = await user.send_video(dst_chat.id, io.BytesIO(watermarked), caption=caption, reply_to_message_id=reply_to_message_id)

        elif msg.sticker:
            sent = await user.send_sticker(dst_chat.id, msg.sticker.file_id, reply_to_message_id=reply_to_message_id)

        elif msg.voice:
            voice_file = await user.download_media(msg.voice, in_memory=True)
            sent = await user.send_voice(dst_chat.id, voice_file, caption=caption, reply_to_message_id=reply_to_message_id)

        elif text:
            sent = await user.send_message(dst_chat.id, text, reply_to_message_id=reply_to_message_id)

        return sent

    transferred_count = 0
    offset_id = 0
    done_oldest = False
    last_seen_id = 0 if from_start else (last_scanned or 0)

    while True:
        try:
            batch = [m async for m in user.get_chat_history(src_chat.id, offset_id=offset_id, limit=batch_size)]
        except FloodWait as fw:
            await asyncio.sleep(int(fw.value) + 1)
            continue

        if not batch:
            break

        offset_id = batch[-1].id  # حرکت به قدیمی‌ترها
        batch_sorted = list(sorted(batch, key=lambda m: m.id))  # قدیمی → جدید

        for msg in batch_sorted:
            # اگر از ابتدا می‌رویم، این شرط عملاً بی‌اثر است چون last_seen_id=0
            if last_seen_id and msg.id <= last_seen_id:
                continue

            if get_destination_message_id(connection_id, msg.id):
                update_last_scanned_message_id(connection_id, msg.id)
                continue

            reply_to_message_id = None
            if msg.reply_to_message:
                try:
                    reply_to_message_id = get_destination_message_id(connection_id, msg.reply_to_message.id)
                except Exception:
                    reply_to_message_id = None

            try:
                sent = await _send_like_realtime(msg, reply_to_message_id)
                if sent:
                    save_transferred_post(connection_id, msg.id, sent.id)
                    update_last_scanned_message_id(connection_id, msg.id)
                    add_activity_log(connection_id, "transfer", f"بک‌فیل: {source} → {dest} | msg_id={msg.id}")
                    transferred_count += 1
            except FloodWait as fw:
                await asyncio.sleep(int(fw.value) + 1)
                try:
                    sent = await _send_like_realtime(msg, reply_to_message_id)
                    if sent:
                        save_transferred_post(connection_id, msg.id, sent.id)
                        update_last_scanned_message_id(connection_id, msg.id)
                        add_activity_log(connection_id, "transfer", f"بک‌فیل: {source} → {dest} | msg_id={msg.id}")
                        transferred_count += 1
                except Exception as e:
                    add_activity_log(connection_id, "error", f"ارسال ناموفق در بک‌فیل msg_id={msg.id}: {e}")
            except Exception as e:
                add_activity_log(connection_id, "error", f"ارسال ناموفق در بک‌فیل msg_id={msg.id}: {e}")

        if len(batch) < batch_size:
            done_oldest = True
        if done_oldest:
            break

    return transferred_count, get_last_scanned_message_id(connection_id)

# اجرای اصلی برنامه
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
