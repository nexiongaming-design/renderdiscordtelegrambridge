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
from aiohttp import web

# Load secrets
load_dotenv()

# --- GLOBAL CONTROL STATE & METRICS ---
bridge_active = True
start_time = time.time()
stats_discord_to_tg = 0
stats_tg_to_discord = 0
stats_photos_bridged = 0

# --- MESSAGE DELETION & EDIT SYSTEM CACHE ---
MAX_MAP_SIZE = 10000 
DISCORD_TO_TELEGRAM_MAP = {}
TELEGRAM_TO_DISCORD_MAP = {}
RECENT_POSTS = set()

# NEW: Track translations
LAST_SOURCE_MSG_ID = None 
TRANSLATION_MAP = {} # Maps original Discord ID to a list of (Channel_ID, Webhook_Message_ID)

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
     },
     "rules": {
         "listen_channels": parse_inline_ids("RULES_LISTEN_CHANNELS"),
         "source_channel_id": safe_int("RULES_SOURCE_CHANNEL_ID", default=0),
         "telegram_topic_id": safe_int("RULES_TELEGRAM_TOPIC_ID", default=0)
     }
}

# Compile a flat list of all monitored Discord channels for rapid lookups
ALL_MONITORED_DISCORD_CHANNELS = []
for cat_data in CATEGORIES.values():
    ALL_MONITORED_DISCORD_CHANNELS.extend(cat_data["listen_channels"])
ALL_MONITORED_DISCORD_CHANNELS = list(set(ALL_MONITORED_DISCORD_CHANNELS))

# Global Telegram Sender Handler References
tg_bot_sender = None 
TELEGRAM_GROUP_ID = 0

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

# --- WEB CONTROL HELPER FUNCTIONS ---
async def run_delayed_system_command(command, delay=1.0):
    await asyncio.sleep(delay)
    process = await asyncio.create_subprocess_shell(command)
    await process.wait()

async def handle_health(request):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, 'index.html')
    try:
        async with aiofiles.open(file_path, mode='r') as f:
            content = await f.read()
        return web.Response(text=content, content_type='text/html')
    except Exception:
        return web.Response(text="MyBridgeBot Health Check: ONLINE", content_type='text/plain')

async def handle_restart(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        asyncio.create_task(run_delayed_system_command('sudo systemctl restart bot-bridge'))
        return web.Response(text="Bot is restarting...")
    return web.Response(text="Unauthorized", status=403)

async def handle_stop(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        asyncio.create_task(run_delayed_system_command('sudo systemctl stop bot-bridge'))
        return web.Response(text="Bot stopped.")
    return web.Response(text="Unauthorized", status=403)

async def handle_update(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        process = await asyncio.create_subprocess_shell('cd /home/dano/matrix-bridge/ && git pull')
        await process.communicate()
        asyncio.create_task(run_delayed_system_command('sudo systemctl restart bot-bridge'))
        return web.Response(text="Pulling latest code and restarting...")
    return web.Response(text="Unauthorized", status=403)

async def handle_logs(request):
    process = await asyncio.create_subprocess_shell(
        'journalctl -u bot-bridge.service -n 50 --no-pager',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if not stdout and stderr:
        return web.Response(text=f"⚠️ LINUX ERROR FETCHING LOGS:\n\n{stderr.decode('utf-8')}", status=500)
    return web.Response(text=stdout.decode('utf-8') if stdout else "No logs found.")

async def handle_pause(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        global bridge_active
        bridge_active = False
        return web.Response(text="Bridge has been paused.")
    return web.Response(text="Unauthorized", status=403)

async def handle_resume(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        global bridge_active
        bridge_active = True
        return web.Response(text="Bridge has been resumed.")
    return web.Response(text="Unauthorized", status=403)

async def handle_clear_cache(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        DISCORD_TO_TELEGRAM_MAP.clear()
        TELEGRAM_TO_DISCORD_MAP.clear()
        RECENT_POSTS.clear()
        return web.Response(text="Caches cleared.")
    return web.Response(text="Unauthorized", status=403)

async def handle_stats(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        uptime = int(time.time() - start_time)
        return web.Response(text=f"Uptime: {uptime}s\nDiscord->TG: {stats_discord_to_tg}\nTG->Discord: {stats_tg_to_discord}\nPhotos Bridged: {stats_photos_bridged}")
    return web.Response(text="Unauthorized", status=403)

async def handle_bandwidth(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        process = await asyncio.create_subprocess_shell(
            'vnstat',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        output = stdout.decode('utf-8') or "No data available."
        return web.Response(text=f"=== BANDWIDTH USAGE ===\n\n{output}")
    return web.Response(text="Unauthorized", status=403)

async def handle_system(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        process = await asyncio.create_subprocess_shell(
            "free -h && df -h /",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        return web.Response(text=stdout.decode('utf-8'))
    return web.Response(text="Unauthorized", status=403)

async def handle_ping(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        return web.Response(text="Pong!")
    return web.Response(text="Unauthorized", status=403)


# --- DISCORD BOT EVENT HANDLERS ---

@discord_bot.event 
async def on_ready(): 
    print(f'Logged in to Discord successfully as: {discord_bot.user.name} - VERSIE 2.5') 
    print(f'Total language channels monitored across all categories: {len(ALL_MONITORED_DISCORD_CHANNELS)}')
    for cat_name, mapping in CATEGORIES.items():
        print(f" -> Matrix Active [{cat_name.upper()}]: Listening to {len(mapping['listen_channels'])} channels | Routing to Topic ID: {mapping['telegram_topic_id']}")

@discord_bot.event 
async def on_message(message): 
    global TELEGRAM_GROUP_ID, stats_discord_to_tg, stats_photos_bridged, LAST_SOURCE_MSG_ID
    
    if not bridge_active:
        return
        
    # --- NEW: SHADOW TRACKING FOR ITRANSLATOR WEBHOOKS ---
    if message.webhook_id:
        if LAST_SOURCE_MSG_ID:
            if LAST_SOURCE_MSG_ID not in TRANSLATION_MAP:
                TRANSLATION_MAP[LAST_SOURCE_MSG_ID] = []
            
            TRANSLATION_MAP[LAST_SOURCE_MSG_ID].append((message.channel.id, message.id))
            
            if len(TRANSLATION_MAP) > MAX_MAP_SIZE:
                oldest_key = next(iter(TRANSLATION_MAP))
                TRANSLATION_MAP.pop(oldest_key)
                
        return 

    if message.author.bot:
        return
        
    # --- TRACK THE SOURCE MESSAGE ---
    LAST_SOURCE_MSG_ID = message.id
    
    if message.channel.id not in ALL_MONITORED_DISCORD_CHANNELS:
        return
        
    matched_category = next((c for c in CATEGORIES.values() if message.channel.id in c["listen_channels"]), None)
    if not matched_category:
        return

    sender_name = message.author.display_name 
    dynamic_content = message.content or ""
    
    if message.embeds:
        for embed in message.embeds:
            if embed.description: 
                dynamic_content += f"\n\n{embed.description}"

    formatted_text = f"{sender_name}:\n\n{dynamic_content}"

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
                stats_discord_to_tg += 1
                stats_photos_bridged += 1
                return 

        if dynamic_content: 
            tg_msg = await tg_bot_sender.send_message(
                chat_id=TELEGRAM_GROUP_ID, 
                text=formatted_text, 
                message_thread_id=matched_category["telegram_topic_id"] or None,
                parse_mode=None
            ) 
            save_message_pair(message.id, tg_msg.message_id, is_photo=False)
            stats_discord_to_tg += 1
            
    except Exception as e: 
        print(f"CRITICAL ERROR forwarding to Telegram: {e}")

@discord_bot.event
async def on_raw_message_delete(payload):
    # 1. Delete on Telegram 
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

    # 2. NEW: Delete the iTranslator Webhooks
    if payload.message_id in TRANSLATION_MAP:
        print("--- CLEANING UP TRANSLATIONS ---")
        webhook_list = TRANSLATION_MAP.pop(payload.message_id)
        
        for channel_id, webhook_msg_id in webhook_list:
            target_channel = discord_bot.get_channel(channel_id)
            if target_channel:
                try:
                    msg_to_delete = target_channel.get_partial_message(webhook_msg_id)
                    await msg_to_delete.delete()
                    print(f"Deleted translated webhook {webhook_msg_id} in channel {channel_id}")
                except Exception as e:
                    print(f"Could not delete webhook: {e}")

@discord_bot.event
async def on_raw_message_edit(payload):
    author_data = payload.data.get('author', {})
    if author_data.get('bot') and not payload.data.get('webhook_id'):
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


# --- TELEGRAM BOT EVENT HANDLERS ---

async def telegram_receive_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    global stats_tg_to_discord
    
    if not bridge_active:
        return

    message = update.effective_message 
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

    is_edit = bool(update.edited_message)
    if is_edit:
        if message.message_id in TELEGRAM_TO_DISCORD_MAP:
            discord_msg_id = TELEGRAM_TO_DISCORD_MAP[message.message_id]
            partial_msg = target_channel.get_partial_message(discord_msg_id)
            await partial_msg.edit(content=original_discord_text)
        return

    discord_msg = None
    if byte_array: 
        file_stream = io.BytesIO(byte_array) 
        discord_file = discord.File(file_stream, filename="telegram_image.png") 
        discord_msg = await target_channel.send(content=original_discord_text, file=discord_file) 
    elif text_content: 
        discord_msg = await target_channel.send(content=original_discord_text) 
            
    if discord_msg:
        save_telegram_to_discord(message.message_id, discord_msg.id)
        stats_tg_to_discord += 1

async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Sync initiated: Please ensure channels are manually aligned.")


# --- INTEGRATED SERVICE CORE RUNNER ---

async def main(): 
    global tg_bot_sender, TELEGRAM_GROUP_ID

    # Web Server Startup Config
    port = int(os.getenv("PORT", 8080))
    app = web.Application()
    app.router.add_get('/', handle_health)
    app.router.add_get('/restart', handle_restart)
    app.router.add_get('/stop', handle_stop)
    app.router.add_get('/update', handle_update)
    app.router.add_get('/logs', handle_logs)
    app.router.add_get('/pause', handle_pause)
    app.router.add_get('/resume', handle_resume)
    app.router.add_get('/clear-cache', handle_clear_cache)
    app.router.add_get('/stats', handle_stats)
    app.router.add_get('/bandwidth', handle_bandwidth)
    app.router.add_get('/system', handle_system)
    app.router.add_get('/ping', handle_ping)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port, reuse_address=True) 
    await site.start()
    print(f"Web control panel server successfully bound to port {port}")
    
    # Clean and parse application environment data
    discord_token = clean_token("DISCORD_TOKEN")
    telegram_token = clean_token("TELEGRAM_TOKEN")
    TELEGRAM_GROUP_ID = safe_int("TELEGRAM_GROUP_ID", default=0)
    
    print("=== ENVIRONMENT DIAGNOSTICS ===")
    print(f"DISCORD_TOKEN loaded? {'YES' if discord_token else 'NO'} (Length: {len(discord_token) if discord_token else 0})")
    print(f"TELEGRAM_TOKEN loaded? {'YES' if telegram_token else 'NO'} (Length: {len(telegram_token) if telegram_token else 0})")
    print(f"TELEGRAM_GROUP_ID: {TELEGRAM_GROUP_ID}")
    print("===============================")
     
    if not discord_token or not telegram_token or TELEGRAM_GROUP_ID == 0:
        print("CRITICAL: Missing core token configurations or group ID...")
        await site.stop()
        return

    # Build cross-platform context applications
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

    tg_app.add_handler(CommandHandler("sync", sync_command))
    
    tg_msg_filter = filters.Chat(TELEGRAM_GROUP_ID) & (filters.TEXT | filters.PHOTO)
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
        await tg_app.stop() 
        await tg_app.shutdown() 
        await discord_bot.close()

if __name__ == '__main__': 
    try: 
        asyncio.run(main())
    except Exception as e: 
        print(f"Critical System Failure: {e}")
