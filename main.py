import os
import discord
from discord.ext import commands
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler
from dotenv import load_dotenv
import asyncio
import io
import aiofiles
import time

# Web health check server imports
from aiohttp import web

async def handle_health(request):
    # Get the folder where main.py is located
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, 'index.html')
    
    async with aiofiles.open(file_path, mode='r') as f:
        content = await f.read()
    return web.Response(text=content, content_type='text/html')

# Remote Control Endpoints (Optimized with delayed execution to prevent systemd deadlocks)
async def handle_restart(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        async def delayed_restart():
            await asyncio.sleep(1)
            os.system('sudo systemctl restart bot-bridge')
        asyncio.create_task(delayed_restart())
        return web.Response(text="Bot is restarting...")
    return web.Response(text="Unauthorized", status=403)

async def handle_stop(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        async def delayed_stop():
            await asyncio.sleep(1)
            os.system('sudo systemctl stop bot-bridge')
        asyncio.create_task(delayed_stop())
        return web.Response(text="Bot stopped.")
    return web.Response(text="Unauthorized", status=403)

async def handle_update(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        # Assumes you have already set up 'git' in /home/dano/matrix-bridge/
        process = await asyncio.create_subprocess_shell(
            'cd /home/dano/matrix-bridge/ && git pull',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()
        
        async def delayed_restart():
            await asyncio.sleep(1)
            os.system('sudo systemctl restart bot-bridge')
        asyncio.create_task(delayed_restart())
        return web.Response(text="Pulling latest code and restarting...")
    return web.Response(text="Unauthorized", status=403)

async def handle_logs(request):
    # Reads the last 50 lines of the systemd journal non-blockingly
    process = await asyncio.create_subprocess_shell(
        'journalctl -u bot-bridge.service -n 50 --no-pager',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    return web.Response(text=stdout.decode('utf-8'))

# Load secrets
load_dotenv()

# Safe cleaning function to strip spaces or accidental quotes from tokens
def clean_token(env_var_name):
    val = os.getenv(env_var_name)
    if not val:
        return None
    return val.strip().replace('"', '').replace("'", "")

# Updated defensive integer parser to clean out trailing inline comments
def safe_int(env_var_name, default=0):
    val = os.getenv(env_var_name, "").strip()
    if not val: 
        return default
    # Split by '#' to remove any trailing comments on the line
    val = val.split('#')[0].strip()
    try:
        return int(val)
    except ValueError:
        print(f"⚠️ WARNING: Environment variable '{env_var_name}' is not a valid integer. Defaulting to {default}.")
        return default

# Updated list parser to handle trailing inline comments safely
def parse_inline_ids(env_var_name):
    val = os.getenv(env_var_name) 
    if not val: 
        return []
    # Split by '#' to isolate the raw IDs from any comments
    val = val.split('#')[0].strip()
    return [int(cid.strip()) for cid in val.split(",") if cid.strip().isdigit()]

# --- ROUTING MATRIX CONFIGURATION ---
CATEGORIES = {
    "herds": {
        "listen_channels": parse_inline_ids("HERDS_LISTEN_CHANNELS"),
        "source_channel_id": safe_int("HERDS_SOURCE_CHANNEL_ID", default=0),
        "telegram_topic_id": safe_int("HERDS_TELEGRAM_TOPIC_ID", default=0)
    },
    "notifications": {
        "listen_channels": parse_inline_ids("NOTIF_LISTEN_CHANNELS"),
        "source_channel_id": safe_int("NOTIF_SOURCE_CHANNEL_ID", default=0),
        "telegram_topic_id": safe_int("NOTIF_TELEGRAM_TOPIC_ID", default=0)
    },
    "schedule": {
        "listen_channels": parse_inline_ids("SCHEDULE_LISTEN_CHANNELS"),
        "source_channel_id": safe_int("SCHEDULE_SOURCE_CHANNEL_ID", default=0),
        "telegram_topic_id": safe_int("SCHEDULE_TELEGRAM_TOPIC_ID", default=0)
    },    
    "chat": {
         "listen_channels": parse_inline_ids("CHAT_LISTEN_CHANNELS"),
         "source_channel_id": safe_int("CHAT_SOURCE_CHANNEL_ID", default=0),
         "telegram_topic_id": safe_int("CHAT_TELEGRAM_TOPIC_ID", default=0)
     },
    "polls": {
         "listen_channels": parse_inline_ids("POLLS_LISTEN_CHANNELS"),
         "source_channel_id": safe_int("POLLS_SOURCE_CHANNEL_ID", default=0),
         "telegram_topic_id": safe_int("POLLS_TELEGRAM_TOPIC_ID", default=0)
     },
     "absence": {
         "listen_channels": parse_inline_ids("ABSENCE_LISTEN_CHANNELS"),
         "source_channel_id": safe_int("ABSENCE_SOURCE_CHANNEL_ID", default=0),
         "telegram_topic_id": safe_int("ABSENCE_TELEGRAM_TOPIC_ID", default=0)
     }
}

# Compile a flat list of all monitored Discord language channels for rapid lookups
ALL_MONITORED_DISCORD_CHANNELS = []
for cat_data in CATEGORIES.values():
    ALL_MONITORED_DISCORD_CHANNELS.extend(cat_data["listen_channels"])
ALL_MONITORED_DISCORD_CHANNELS = list(set(ALL_MONITORED_DISCORD_CHANNELS))

# Global Telegram Sender Handler
tg_bot_sender = None 
TELEGRAM_GROUP_ID = 0

# --- MESSAGE DELETION & EDIT SYSTEM CACHE ---
MAX_MAP_SIZE = 10000 

DISCORD_TO_TELEGRAM_MAP = {}
TELEGRAM_TO_DISCORD_MAP = {}

# Deduplication cache tracking high-speed cross-channel mirror activity
# Format: { sender_display_name: (last_seen_channel_id, timestamp) }
RECENT_POSTS = {}

def save_message_pair(discord_id, telegram_id, author_name, is_photo=False):
    if len(DISCORD_TO_TELEGRAM_MAP) >= MAX_MAP_SIZE:
        oldest_key = next(iter(DISCORD_TO_TELEGRAM_MAP))
        DISCORD_TO_TELEGRAM_MAP.pop(oldest_key)
    DISCORD_TO_TELEGRAM_MAP[discord_id] = (telegram_id, is_photo, author_name)

def save_telegram_to_discord(telegram_id, discord_id):
    if len(TELEGRAM_TO_DISCORD_MAP) >= MAX_MAP_SIZE:
        oldest_key = next(iter(TELEGRAM_TO_DISCORD_MAP))
        TELEGRAM_TO_DISCORD_MAP.pop(oldest_key)
    TELEGRAM_TO_DISCORD_MAP[telegram_id] = discord_id

# Initialize Discord bot
intents = discord.Intents.default() 
intents.message_content = True 
discord_bot = commands.Bot(command_prefix="!", intents=intents) 


# --- DISCORD BOT LOGIC (Discord Translated Channels -> Telegram Topics) ---

@discord_bot.event 
async def on_ready(): 
    print(f'Logged in to Discord successfully as: {discord_bot.user.name} - VERSIE 2.2') 
    print(f'Total language channels monitored across all categories: {len(ALL_MONITORED_DISCORD_CHANNELS)}')
    for cat_name, mapping in CATEGORIES.items():
        print(f" -> Matrix Active [{cat_name.upper()}]: Listening to {len(mapping['listen_channels'])} channels | Routing to Topic ID: {mapping['telegram_topic_id']}")


@discord_bot.event 
async def on_message(message): 
    global TELEGRAM_GROUP_ID
    
    # --- LOOP & SELF-PREVENTION ---
    # Block messages originating from this exact bot profile to prevent infinite feedback loops
    if message.author.id == discord_bot.user.id:
        return
        
    # --- CROSS-CHANNEL TRANSLATION GUARD ---
    # Captures automatic translation bots (like iTranslator webhooks/bots) that instantly 
    # broadcast duplicates across separate language channels within a tiny time window.
    current_time = time.time()
    sender_name = message.author.display_name

    if sender_name in RECENT_POSTS:
        last_channel_id, last_time = RECENT_POSTS[sender_name]
        if message.channel.id != last_channel_id and (current_time - last_time) < 3.0:
            return  # Drop multi-channel translation spam silently

    RECENT_POSTS[sender_name] = (message.channel.id, current_time)
    
    # Only process if the channel is one we monitor
    if message.channel.id not in ALL_MONITORED_DISCORD_CHANNELS:
        return
        
    matched_category = next((c for c in CATEGORIES.values() if message.channel.id in c["listen_channels"]), None)
    if not matched_category:
        return

    dynamic_content = message.content or ""
    
    # Comprehensive Embed Parsing (Crucial for Sesh, Survey Bot, and Poll Layouts)
    if message.embeds:
        for embed in message.embeds:
            if embed.title:
                dynamic_content += f"\n**{embed.title}**"
            if embed.description: 
                dynamic_content += f"\n{embed.description}"
            if embed.fields:
                for field in embed.fields:
                    dynamic_content += f"\n\n**{field.name}**:\n{field.value}"

    formatted_text = f"{sender_name}:\n\n{dynamic_content.strip()}"

    try: 
        if message.attachments: 
            attachment = message.attachments[0] 
            if attachment.filename.lower().endswith(('png', 'jpg', 'jpeg', 'webp')): 
                image_bytes = await attachment.read() 
                tg_msg = await tg_bot_sender.send_photo(
                    chat_id=TELEGRAM_GROUP_ID, 
                    photo=image_bytes, 
                    caption=formatted_text, 
                    message_thread_id=matched_category["telegram_topic_id"] or None,
                    parse_mode=None
                ) 
                save_message_pair(message.id, tg_msg.message_id, sender_name, is_photo=True)
                return 

        if dynamic_content: 
            tg_msg = await tg_bot_sender.send_message(
                chat_id=TELEGRAM_GROUP_ID, 
                text=formatted_text, 
                message_thread_id=matched_category["telegram_topic_id"] or None,
                parse_mode=None
            ) 
            save_message_pair(message.id, tg_msg.message_id, sender_name, is_photo=False)
            
    except Exception as e: 
        print(f"CRITICAL ERROR forwarding to Telegram: {e}")


@discord_bot.event
async def on_raw_message_delete(payload):
    if payload.message_id in DISCORD_TO_TELEGRAM_MAP:
        mapped_data = DISCORD_TO_TELEGRAM_MAP.pop(payload.message_id)
        telegram_msg_id = mapped_data[0]
        
        try:
            print(f"--- SYNC DELETION --- Discord message {payload.message_id} vanished. Striking from Telegram...")
            await tg_bot_sender.delete_message(
                chat_id=TELEGRAM_GROUP_ID,
                message_id=telegram_msg_id
            )
            print("--- SYNC DELETION SUCCESS --- Target message removed from Telegram topic thread.")
        except Exception as e:
            print(f"Error executing synchronized deletion loop on Telegram: {e}")


@discord_bot.event
async def on_raw_message_edit(payload):
    author_data = payload.data.get('author') or {}
    
    # Prevent application loop issues caused by this bot editing things
    if int(author_data.get('id', 0)) == discord_bot.user.id:
        return

    if payload.message_id in DISCORD_TO_TELEGRAM_MAP:
        telegram_msg_id, is_photo, cached_author_name = DISCORD_TO_TELEGRAM_MAP[payload.message_id]
        
        new_content = payload.data.get('content')
        
        if new_content is None:
            if payload.cached_message:
                new_content = payload.cached_message.content
            else:
                return

        display_name = author_data.get('global_name') or author_data.get('username')
        
        if not display_name and payload.cached_message:
            display_name = payload.cached_message.author.display_name
        if not display_name:
            display_name = cached_author_name

        formatted_text = f"{display_name}:\n\n{new_content}"

        try:
            if is_photo:
                await tg_bot_sender.edit_message_caption(
                    chat_id=TELEGRAM_GROUP_ID,
                    message_id=telegram_msg_id,
                    caption=formatted_text if new_content else f"{display_name}:"
                )
            else:
                await tg_bot_sender.edit_message_text(
                    chat_id=TELEGRAM_GROUP_ID,
                    message_id=telegram_msg_id,
                    text=formatted_text
                )
            print(f"--- SYNC EDIT SUCCESS --- Updated Telegram target message ID: {telegram_msg_id}")
        except Exception as e:
            print(f"Error executing synchronized message edit on Telegram: {e}")


# --- TELEGRAM BOT LOGIC (Telegram Topics -> Discord Source Channels) ---

async def telegram_receive_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    message = update.effective_message 
    
    # --- EXTRA CHECKS ---
    if not message or message.from_user is None:
        return
    if not message.text and not message.caption and not message.photo:
        return
    if message.from_user.is_bot:
        return

    incoming_topic_id = message.message_thread_id or 0
    matched_category = next((c for c in CATEGORIES.values() if c["telegram_topic_id"] == incoming_topic_id), None)
    
    if not matched_category:
        return

    target_source_channel_id = matched_category["source_channel_id"]
    target_channel = discord_bot.get_channel(target_source_channel_id) 
    if not target_channel: 
        return 

    sender_name = update.effective_user.first_name or update.effective_user.username 
    text_content = message.text or message.caption or "" 
    photo_content = message.photo 

    byte_array = None 
    if photo_content: 
        photo = photo_content[-1] 
        tg_file = await context.bot.get_file(photo.file_id) 
        byte_array = await tg_file.download_to_memory()

    original_discord_text = f"**{sender_name}**" 
    if text_content: 
        original_discord_text += f"\n\n{text_content}" 

    # --- HANDLE EDITS & NEW MESSAGES ---
    is_edit = bool(update.edited_message)
    if is_edit:
        if message.message_id in TELEGRAM_TO_DISCORD_MAP:
            discord_msg_id = TELEGRAM_TO_DISCORD_MAP[message.message_id]
            partial_msg = target_channel.get_partial_message(discord_msg_id)
            await partial_msg.edit(content=original_discord_text)
        return

    # --- SEND NEW ---
    discord_msg = None
    if byte_array: 
        file_stream = io.BytesIO(byte_array) 
        discord_file = discord.File(file_stream, filename="telegram_image.png") 
        discord_msg = await target_channel.send(content=original_discord_text, file=discord_file) 
    elif text_content: 
        discord_msg = await target_channel.send(content=original_discord_text) 
            
    if discord_msg:
        save_telegram_to_discord(message.message_id, discord_msg.id)

async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Sync initiated: Please ensure channels are manually aligned.")

# --- INTEGRATED RUNNER ---

async def main(): 
    global tg_bot_sender 

    # Web Server Startup
    port = int(os.getenv("PORT", 8080))
    app = web.Application()
    app.router.add_get('/', handle_health)
    
    # Registering Control Panel routes
    app.router.add_get('/restart', handle_restart)
    app.router.add_get('/stop', handle_stop)
    app.router.add_get('/update', handle_update)
    app.router.add_get('/logs', handle_logs)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port, reuse_address=True) 
    await site.start()
    print(f"Web health check server successfully bound to port {port}")
    
    # 1. Clean and extract tokens from the environment safely first
    discord_token = clean_token("DISCORD_TOKEN")
    telegram_token = clean_token("TELEGRAM_TOKEN")
    global TELEGRAM_GROUP_ID
    TELEGRAM_GROUP_ID = safe_int("TELEGRAM_GROUP_ID", default=0)
    
    # 2. Pre-flight logging check
    print("=== ENVIRONMENT DIAGNOSTICS ===")
    print(f"DISCORD_TOKEN loaded? {'YES' if discord_token else 'NO'} (Length: {len(discord_token) if discord_token else 0})")
    print(f"TELEGRAM_TOKEN loaded? {'YES' if telegram_token else 'NO'} (Length: {len(telegram_token) if telegram_token else 0})")
    print(f"TELEGRAM_GROUP_ID: {TELEGRAM_GROUP_ID}")
    print("===============================")
     
    if not discord_token or not telegram_token or TELEGRAM_GROUP_ID == 0:
        print("CRITICAL: Missing core token configurations or group ID...")
        return

    tg_app = ( 
        ApplicationBuilder() 
        .token(telegram_token) 
        .connect_timeout(30.0) 
        .read_timeout(30.0) 
        .write_timeout(30.0) 
        .get_updates_read_timeout(30.0) 
        .build()
    ) 

    tg_bot_sender = tg_app.bot  

    # Handlers configureren (Schoon en foutvrij)
    tg_app.add_handler(CommandHandler("sync", sync_command))
    
    tg_msg_filter = filters.Chat(TELEGRAM_GROUP_ID) & (filters.TEXT | filters.PHOTO | filters.UpdateType.EDITED_MESSAGE)
    tg_app.add_handler(MessageHandler(tg_msg_filter, telegram_receive_handler)) 

    print("Starting Telegram Connection Module...") 
    await tg_app.initialize() 
    await tg_app.updater.start_polling(drop_pending_updates=True) 
    await tg_app.start() 

    print("Starting Discord Gateway Core Connection...") 
    try: 
        await discord_bot.start(discord_token) 
    finally: 
        print("Shutting down bot connections gracefully...") 
        try:
            await site.stop()
        except Exception:
            pass
            
        if tg_app.updater and tg_app.updater.running:
            try:
                await tg_app.updater.stop() 
            except Exception:
                pass
        try:
            await tg_app.stop() 
            await tg_app.shutdown() 
        except Exception:
            pass
            
        try:
            await discord_bot.close()
        except Exception:
            pass

if __name__ == '__main__': 
    try: 
        asyncio.run(main()) 
    except Exception as e: 
        print(f"Critical System Failure: {e}")import os
import discord
from discord.ext import commands
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler
from dotenv import load_dotenv
import asyncio
import io
import aiofiles
import time

# Web health check server imports
from aiohttp import web

async def handle_health(request):
    # Get the folder where main.py is located
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, 'index.html')
    
    async with aiofiles.open(file_path, mode='r') as f:
        content = await f.read()
    return web.Response(text=content, content_type='text/html')

# Remote Control Endpoints
async def handle_restart(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        os.system('sudo systemctl restart bot-bridge')
        return web.Response(text="Bot is restarting...")
    return web.Response(text="Unauthorized", status=403)

async def handle_stop(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        os.system('sudo systemctl stop bot-bridge')
        return web.Response(text="Bot stopped.")
    return web.Response(text="Unauthorized", status=403)

async def handle_update(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        # Assumes you have already set up 'git' in /home/dano/matrix-bridge/
        os.system('cd /home/dano/matrix-bridge/ && git pull')
        os.system('sudo systemctl restart bot-bridge')
        return web.Response(text="Pulling latest code and restarting...")
    return web.Response(text="Unauthorized", status=403)

async def handle_logs(request):
    # Reads the last 50 lines of the systemd journal
    log_data = os.popen('journalctl -u bot-bridge.service -n 50 --no-pager').read()
    return web.Response(text=log_data)

# Load secrets
load_dotenv()

# Safe cleaning function to strip spaces or accidental quotes from tokens
def clean_token(env_var_name):
    val = os.getenv(env_var_name)
    if not val:
        return None
    return val.strip().replace('"', '').replace("'", "")

# Updated defensive integer parser to clean out trailing inline comments
def safe_int(env_var_name, default=0):
    val = os.getenv(env_var_name, "").strip()
    if not val: 
        return default
    # Split by '#' to remove any trailing comments on the line
    val = val.split('#')[0].strip()
    try:
        return int(val)
    except ValueError:
        print(f"⚠️ WARNING: Environment variable '{env_var_name}' is not a valid integer. Defaulting to {default}.")
        return default

# Updated list parser to handle trailing inline comments safely
def parse_inline_ids(env_var_name):
    val = os.getenv(env_var_name) 
    if not val: 
        return []
    # Split by '#' to isolate the raw IDs from any comments
    val = val.split('#')[0].strip()
    return [int(cid.strip()) for cid in val.split(",") if cid.strip().isdigit()]

# --- ROUTING MATRIX CONFIGURATION ---
CATEGORIES = {
    "herds": {
        "listen_channels": parse_inline_ids("HERDS_LISTEN_CHANNELS"),
        "source_channel_id": safe_int("HERDS_SOURCE_CHANNEL_ID", default=0),
        "telegram_topic_id": safe_int("HERDS_TELEGRAM_TOPIC_ID", default=0)
    },
    "notifications": {
        "listen_channels": parse_inline_ids("NOTIF_LISTEN_CHANNELS"),
        "source_channel_id": safe_int("NOTIF_SOURCE_CHANNEL_ID", default=0),
        "telegram_topic_id": safe_int("NOTIF_TELEGRAM_TOPIC_ID", default=0)
    },
    "schedule": {
        "listen_channels": parse_inline_ids("SCHEDULE_LISTEN_CHANNELS"),
        "source_channel_id": safe_int("SCHEDULE_SOURCE_CHANNEL_ID", default=0),
        "telegram_topic_id": safe_int("SCHEDULE_TELEGRAM_TOPIC_ID", default=0)
    },    
    "chat": {
         "listen_channels": parse_inline_ids("CHAT_LISTEN_CHANNELS"),
         "source_channel_id": safe_int("CHAT_SOURCE_CHANNEL_ID", default=0),
         "telegram_topic_id": safe_int("CHAT_TELEGRAM_TOPIC_ID", default=0)
     },
    "polls": {
         "listen_channels": parse_inline_ids("POLLS_LISTEN_CHANNELS"),
         "source_channel_id": safe_int("POLLS_SOURCE_CHANNEL_ID", default=0),
         "telegram_topic_id": safe_int("POLLS_TELEGRAM_TOPIC_ID", default=0)
     },
     "absence": {
         "listen_channels": parse_inline_ids("ABSENCE_LISTEN_CHANNELS"),
         "source_channel_id": safe_int("ABSENCE_SOURCE_CHANNEL_ID", default=0),
         "telegram_topic_id": safe_int("ABSENCE_TELEGRAM_TOPIC_ID", default=0)
     }
}

# Compile a flat list of all monitored Discord language channels for rapid lookups
ALL_MONITORED_DISCORD_CHANNELS = []
for cat_data in CATEGORIES.values():
    ALL_MONITORED_DISCORD_CHANNELS.extend(cat_data["listen_channels"])
ALL_MONITORED_DISCORD_CHANNELS = list(set(ALL_MONITORED_DISCORD_CHANNELS))

# Global Telegram Sender Handler
tg_bot_sender = None 
TELEGRAM_GROUP_ID = 0

# --- MESSAGE DELETION & EDIT SYSTEM CACHE ---
MAX_MAP_SIZE = 10000 

DISCORD_TO_TELEGRAM_MAP = {}
TELEGRAM_TO_DISCORD_MAP = {}

# Deduplication cache tracking high-speed cross-channel mirror activity
# Format: { sender_display_name: (last_seen_channel_id, timestamp) }
RECENT_POSTS = {}

def save_message_pair(discord_id, telegram_id, is_photo=False):
    if len(DISCORD_TO_TELEGRAM_MAP) >= MAX_MAP_SIZE:
        oldest_key = next(iter(DISCORD_TO_TELEGRAM_MAP))
        DISCORD_TO_TELEGRAM_MAP.pop(oldest_key)
    DISCORD_TO_TELEGRAM_MAP[discord_id] = (telegram_id, is_photo)

def save_telegram_to_discord(telegram_id, discord_id):
    if len(TELEGRAM_TO_DISCORD_MAP) >= MAX_MAP_SIZE:
        oldest_key = next(iter(TELEGRAM_TO_DISCORD_MAP))
        TELEGRAM_TO_DISCORD_MAP.pop(oldest_key)
    TELEGRAM_TO_DISCORD_MAP[telegram_id] = discord_id

# Initialize Discord bot
intents = discord.Intents.default() 
intents.message_content = True 
discord_bot = commands.Bot(command_prefix="!", intents=intents) 


# --- DISCORD BOT LOGIC (Discord Translated Channels -> Telegram Topics) ---

@discord_bot.event 
async def on_ready(): 
    print(f'Logged in to Discord successfully as: {discord_bot.user.name} - VERSIE 2.2') 
    print(f'Total language channels monitored across all categories: {len(ALL_MONITORED_DISCORD_CHANNELS)}')
    for cat_name, mapping in CATEGORIES.items():
        print(f" -> Matrix Active [{cat_name.upper()}]: Listening to {len(mapping['listen_channels'])} channels | Routing to Topic ID: {mapping['telegram_topic_id']}")


@discord_bot.event 
async def on_message(message): 
    global TELEGRAM_GROUP_ID
    
    # --- LOOP & SELF-PREVENTION ---
    # Block messages originating from this exact bot profile to prevent infinite feedback loops
    if message.author.id == discord_bot.user.id:
        return
        
    # --- CROSS-CHANNEL TRANSLATION GUARD ---
    # Captures automatic translation bots (like iTranslator webhooks/bots) that instantly 
    # broadcast duplicates across separate language channels within a tiny time window.
    current_time = time.time()
    sender_name = message.author.display_name

    if sender_name in RECENT_POSTS:
        last_channel_id, last_time = RECENT_POSTS[sender_name]
        if message.channel.id != last_channel_id and (current_time - last_time) < 3.0:
            return  # Drop multi-channel translation spam silently

    RECENT_POSTS[sender_name] = (message.channel.id, current_time)
    
    # Only process if the channel is one we monitor
    if message.channel.id not in ALL_MONITORED_DISCORD_CHANNELS:
        return
        
    matched_category = next((c for c in CATEGORIES.values() if message.channel.id in c["listen_channels"]), None)
    if not matched_category:
        return

    dynamic_content = message.content or ""
    
    # Comprehensive Embed Parsing (Crucial for Sesh, Survey Bot, and Poll Layouts)
    if message.embeds:
        for embed in message.embeds:
            if embed.title:
                dynamic_content += f"\n**{embed.title}**"
            if embed.description: 
                dynamic_content += f"\n{embed.description}"
            if embed.fields:
                for field in embed.fields:
                    dynamic_content += f"\n\n**{field.name}**:\n{field.value}"

    formatted_text = f"{sender_name}:\n\n{dynamic_content.strip()}"

    try: 
        if message.attachments: 
            attachment = message.attachments[0] 
            if attachment.filename.lower().endswith(('png', 'jpg', 'jpeg', 'webp')): 
                image_bytes = await attachment.read() 
                tg_msg = await tg_bot_sender.send_photo(
                    chat_id=TELEGRAM_GROUP_ID, 
                    photo=image_bytes, 
                    caption=formatted_text, 
                    message_thread_id=matched_category["telegram_topic_id"] or None,
                    parse_mode=None
                ) 
                save_message_pair(message.id, tg_msg.message_id, is_photo=True)
                return 

        if dynamic_content: 
            tg_msg = await tg_bot_sender.send_message(
                chat_id=TELEGRAM_GROUP_ID, 
                text=formatted_text, 
                message_thread_id=matched_category["telegram_topic_id"] or None,
                parse_mode=None
            ) 
            save_message_pair(message.id, tg_msg.message_id, is_photo=False)
            
    except Exception as e: 
        print(f"CRITICAL ERROR forwarding to Telegram: {e}")


@discord_bot.event
async def on_raw_message_delete(payload):
    if payload.message_id in DISCORD_TO_TELEGRAM_MAP:
        mapped_data = DISCORD_TO_TELEGRAM_MAP.pop(payload.message_id)
        telegram_msg_id = mapped_data[0]
        
        try:
            print(f"--- SYNC DELETION --- Discord message {payload.message_id} vanished. Striking from Telegram...")
            await tg_bot_sender.delete_message(
                chat_id=TELEGRAM_GROUP_ID,
                message_id=telegram_msg_id
            )
            print("--- SYNC DELETION SUCCESS --- Target message removed from Telegram topic thread.")
        except Exception as e:
            print(f"Error executing synchronized deletion loop on Telegram: {e}")


@discord_bot.event
async def on_raw_message_edit(payload):
    author_data = payload.data.get('author', {})
    
    # Prevent application loop issues caused by this bot editing things
    if int(author_data.get('id', 0)) == discord_bot.user.id:
        return

    if payload.message_id in DISCORD_TO_TELEGRAM_MAP:
        telegram_msg_id, is_photo = DISCORD_TO_TELEGRAM_MAP[payload.message_id]
        
        new_content = payload.data.get('content')
        
        if new_content is None:
            if payload.cached_message:
                new_content = payload.cached_message.content
            else:
                return

        display_name = author_data.get('global_name') or author_data.get('username')
        
        if not display_name and payload.cached_message:
            display_name = payload.cached_message.author.display_name
        if not display_name:
            display_name = "User"

        formatted_text = f"{display_name}:\n\n{new_content}"

        try:
            if is_photo:
                await tg_bot_sender.edit_message_caption(
                    chat_id=TELEGRAM_GROUP_ID,
                    message_id=telegram_msg_id,
                    caption=formatted_text if new_content else f"{display_name}:"
                )
            else:
                await tg_bot_sender.edit_message_text(
                    chat_id=TELEGRAM_GROUP_ID,
                    message_id=telegram_msg_id,
                    text=formatted_text
                )
            print(f"--- SYNC EDIT SUCCESS --- Updated Telegram target message ID: {telegram_msg_id}")
        except Exception as e:
            print(f"Error executing synchronized message edit on Telegram: {e}")


# --- TELEGRAM BOT LOGIC (Telegram Topics -> Discord Source Channels) ---

async def telegram_receive_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    message = update.effective_message 
    
    # --- EXTRA CHECKS ---
    if not message or message.from_user is None:
        return
    if not message.text and not message.caption and not message.photo:
        return
    if message.from_user.is_bot:
        return

    incoming_topic_id = message.message_thread_id or 0
    matched_category = next((c for c in CATEGORIES.values() if c["telegram_topic_id"] == incoming_topic_id), None)
    
    if not matched_category:
        return

    target_source_channel_id = matched_category["source_channel_id"]
    target_channel = discord_bot.get_channel(target_source_channel_id) 
    if not target_channel: 
        return 

    sender_name = update.effective_user.first_name or update.effective_user.username 
    text_content = message.text or message.caption or "" 
    photo_content = message.photo 

    byte_array = None 
    if photo_content: 
        photo = photo_content[-1] 
        tg_file = await context.bot.get_file(photo.file_id) 
        byte_array = await tg_file.download_as_bytearray() 

    original_discord_text = f"**{sender_name}**" 
    if text_content: 
        original_discord_text += f"\n\n{text_content}" 

    # --- HANDLE EDITS & NEW MESSAGES ---
    is_edit = bool(update.edited_message)
    if is_edit:
        if message.message_id in TELEGRAM_TO_DISCORD_MAP:
            discord_msg_id = TELEGRAM_TO_DISCORD_MAP[message.message_id]
            partial_msg = target_channel.get_partial_message(discord_msg_id)
            await partial_msg.edit(content=original_discord_text)
        return

    # --- SEND NEW ---
    discord_msg = None
    if byte_array: 
        file_stream = io.BytesIO(byte_array) 
        discord_file = discord.File(file_stream, filename="telegram_image.png") 
        discord_msg = await target_channel.send(content=original_discord_text, file=discord_file) 
    elif text_content: 
        discord_msg = await target_channel.send(content=original_discord_text) 
            
    if discord_msg:
        save_telegram_to_discord(message.message_id, discord_msg.id)

async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Sync initiated: Please ensure channels are manually aligned.")

# --- INTEGRATED RUNNER ---

async def main(): 
    global tg_bot_sender 

    # Web Server Startup
    port = int(os.getenv("PORT", 8080))
    app = web.Application()
    app.router.add_get('/', handle_health)
    
    # Registering Control Panel routes
    app.router.add_get('/restart', handle_restart)
    app.router.add_get('/stop', handle_stop)
    app.router.add_get('/update', handle_update)
    app.router.add_get('/logs', handle_logs)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port, reuse_address=True) 
    await site.start()
    print(f"Web health check server successfully bound to port {port}")
    
    # 1. Clean and extract tokens from the environment safely first
    discord_token = clean_token("DISCORD_TOKEN")
    telegram_token = clean_token("TELEGRAM_TOKEN")
    global TELEGRAM_GROUP_ID
    TELEGRAM_GROUP_ID = safe_int("TELEGRAM_GROUP_ID", default=0)
    
    # 2. Pre-flight logging check
    print("=== ENVIRONMENT DIAGNOSTICS ===")
    print(f"DISCORD_TOKEN loaded? {'YES' if discord_token else 'NO'} (Length: {len(discord_token) if discord_token else 0})")
    print(f"TELEGRAM_TOKEN loaded? {'YES' if telegram_token else 'NO'} (Length: {len(telegram_token) if telegram_token else 0})")
    print(f"TELEGRAM_GROUP_ID: {TELEGRAM_GROUP_ID}")
    print("===============================")
     
    if not discord_token or not telegram_token or TELEGRAM_GROUP_ID == 0:
        print("CRITICAL: Missing core token configurations or group ID...")
        return

    tg_app = ( 
        ApplicationBuilder() 
        .token(telegram_token) 
        .connect_timeout(30.0) 
        .read_timeout(30.0) 
        .write_timeout(30.0) 
        .get_updates_read_timeout(30.0) 
        .build()
    ) 

    tg_bot_sender = tg_app.bot  

    # Handlers configureren (Schoon en foutvrij)
    tg_app.add_handler(CommandHandler("sync", sync_command))
    
    tg_msg_filter = filters.Chat(TELEGRAM_GROUP_ID) & (filters.TEXT | filters.PHOTO | filters.UpdateType.EDITED_MESSAGE)
    tg_app.add_handler(MessageHandler(tg_msg_filter, telegram_receive_handler)) 

    print("Starting Telegram Connection Module...") 
    await tg_app.initialize() 
    await tg_app.updater.start_polling(drop_pending_updates=True) 
    await tg_app.start() 

    print("Starting Discord Gateway Core Connection...") 
    try: 
        await discord_bot.start(discord_token) 
    finally: 
        print("Shutting down bot connections gracefully...") 
        await site.stop()
        await tg_app.updater.stop() 
        await tg_app.start() # Safeguard structure alignment
        await tg_app.updater.stop()
        await tg_app.stop() 
        await tg_app.shutdown() 
        await discord_bot.close()

if __name__ == '__main__': 
    try: 
        asyncio.run(main()) 
    except Exception as e: 
        print(f"Critical System Failure: {e}")
