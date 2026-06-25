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

# --- GLOBAL CONTROL STATE & METRICS ---
bridge_active = True
start_time = time.time()
stats_discord_to_tg = 0
stats_tg_to_discord = 0
stats_photos_bridged = 0

# Helper function for non-blocking remote control execution to avoid Systemd deadlocks
async def run_delayed_system_command(command, delay=1.0):
    await asyncio.sleep(delay)
    os.system(command)

async def handle_health(request):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, 'index.html')
    
    try:
        async with aiofiles.open(file_path, mode='r') as f:
            content = await f.read()
        return web.Response(text=content, content_type='text/html')
    except Exception:
        return web.Response(text="MyBridgeBot Health Check: ONLINE", content_type='text/plain')

# Remote Control Endpoints
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
        os.system('cd /home/dano/matrix-bridge/ && git pull')
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
        error_msg = stderr.decode('utf-8')
        return web.Response(text=f"⚠️ LINUX ERROR FETCHING LOGS:\n\n{error_msg}", status=500)
        
    output = stdout.decode('utf-8')
    return web.Response(text=output if output.strip() else "No logs found for bot-bridge.service.")

async def handle_pause(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        global bridge_active
        bridge_active = False
        print("⏸️ BRIDGE MANAGEMENT: Forwarding globally paused via Control Panel.")
        return web.Response(text="Bridge has been paused.")
    return web.Response(text="Unauthorized", status=403)

async def handle_resume(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        global bridge_active
        bridge_active = True
        print("▶️ BRIDGE MANAGEMENT: Forwarding globally resumed via Control Panel.")
        return web.Response(text="Bridge has been resumed.")
    return web.Response(text="Unauthorized", status=403)

async def handle_clear_cache(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        DISCORD_TO_TELEGRAM_MAP.clear()
        TELEGRAM_TO_DISCORD_MAP.clear()
        RECENT_POSTS.clear()
        print("🧹 BRIDGE MANAGEMENT: Memory caches manually flushed via Control Panel.")
        return web.Response(text="All diagnostic maps and caches cleared successfully.")
    return web.Response(text="Unauthorized", status=403)

async def handle_stats(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        uptime_seconds = int(time.time() - start_time)
        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"
        status_text = "RUNNING / ACTIVE" if bridge_active else "PAUSED / SUSPENDED"
        
        metrics = (
            f"=== MYBRIDGEBOT LIFETIME METRICS ===\n"
            f"Bridge Operational Status : {status_text}\n"
            f"Gateway Engine Uptime    : {uptime_str}\n\n"
            f"Discord -> Telegram Relays: {stats_discord_to_tg} messages\n"
            f"Telegram -> Discord Relays: {stats_tg_to_discord} messages\n"
            f"Total Binary Media Syncs  : {stats_photos_bridged} attachments\n\n"
            f"Active Memory Lookup Map Sizes:\n"
            f" - Forward Sync Cache     : {len(DISCORD_TO_TELEGRAM_MAP)} / {MAX_MAP_SIZE}\n"
            f" - Reverse Sync Cache     : {len(TELEGRAM_TO_DISCORD_MAP)} / {MAX_MAP_SIZE}\n"
            f" - Anti-Spam Anti-Echo Map: {len(RECENT_POSTS)}"
        )
        return web.Response(text=metrics)
    return web.Response(text="Unauthorized", status=403)

async def handle_system(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        try:
            async with aiofiles.open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
                content = await f.read()
            cpu_temp = f"{float(content.strip()) / 1000:.1f}°C"
        except Exception:
            cpu_temp = "Unavailable (Non-Linux Environment)"

        process = await asyncio.create_subprocess_shell(
            "free -h && echo '' && echo '--- DISK RESOURCE SPACE ---' && df -h /",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        sys_info = stdout.decode('utf-8')
        
        output = f"=== RASPBERRY PI HOST SYSTEM HEALTH ===\n\nSoC CPU Temperature: {cpu_temp}\n\n{sys_info}"
        return web.Response(text=output)
    return web.Response(text="Unauthorized", status=403)

async def handle_ping(request):
    secret = request.query.get('secret')
    if secret == 'da55123456':
        discord_ready = discord_bot.is_ready()
        discord_latency = f"{round(discord_bot.latency * 1000)}ms" if discord_ready else "OFFLINE"
        tg_ready = tg_bot_sender is not None
        
        output = (
            f"=== ENDPOINT CONNECTIVITY STATUS ===\n\n"
            f"Discord Gateway Client: {'ONLINE' if discord_ready else 'OFFLINE'}\n"
            f"Discord WebSocket Latency: {discord_latency}\n\n"
            f"Telegram Bot Connection: {'ONLINE' if tg_ready else 'OFFLINE'}\n"
            f"Telegram API Gateway Target: https://api.telegram.org\n"
        )
        return web.Response(text=output)
    return web.Response(text="Unauthorized", status=403)

# Load secrets
load_dotenv()

def clean_token(env_var_name):
    val = os.getenv(env_var_name)
    if not val:
        return None
    return val.strip().replace('"', '').replace("'", "")

def safe_int(env_var_name, default=0):
    val = os.getenv(env_var_name, "").strip()
    if not val: 
        return default
    val = val.split('#')[0].strip()
    try:
        return int(val)
    except ValueError:
        return default

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
     }
}

ALL_MONITORED_DISCORD_CHANNELS = []
for cat_data in CATEGORIES.values():
    ALL_MONITORED_DISCORD_CHANNELS.extend(cat_data["listen_channels"])
ALL_MONITORED_DISCORD_CHANNELS = list(set(ALL_MONITORED_DISCORD_CHANNELS))

tg_bot_sender = None 
TELEGRAM_GROUP_ID = 0
MAX_MAP_SIZE = 10000 

DISCORD_TO_TELEGRAM_MAP = {}
TELEGRAM_TO_DISCORD_MAP = {}
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

intents = discord.Intents.default() 
intents.message_content = True 
discord_bot = commands.Bot(command_prefix="!", intents=intents) 

@discord_bot.event 
async def on_ready(): 
    print(f'Logged in to Discord as: {discord_bot.user.name} - VERSIE 2.3') 

@discord_bot.event 
async def on_message(message): 
    global TELEGRAM_GROUP_ID, bridge_active, stats_discord_to_tg, stats_photos_bridged
    
    if not bridge_active or message.author.id == discord_bot.user.id:
        return

    current_time = time.time()
    sender_name = message.author.display_name
    msg_hash = hash(message.content or "")
    tracking_key = f"{sender_name}_{msg_hash}" if message.webhook_id else sender_name

    if tracking_key in RECENT_POSTS:
        last_channel_id, last_time = RECENT_POSTS[tracking_key]
        if message.channel.id == last_channel_id and (current_time - last_time) < 3.0:
            return

    RECENT_POSTS[tracking_key] = (message.channel.id, current_time)
    
    if message.channel.id not in ALL_MONITORED_DISCORD_CHANNELS:
        return
        
    matched_category = next((c for c in CATEGORIES.values() if message.channel.id in c["listen_channels"]), None)
    if not matched_category:
        return

    dynamic_content = message.content or ""
    
    if message.embeds:
        for embed in message.embeds:
            if embed.title: dynamic_content += f"\n**{embed.title}**"
            if embed.description: dynamic_content += f"\n{embed.description}"
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
                    message_thread_id=matched_category["telegram_topic_id"] or None
                ) 
                save_message_pair(message.id, tg_msg.message_id, is_photo=True)
                stats_discord_to_tg += 1
                stats_photos_bridged += 1
                return 

        if dynamic_content.strip(): 
            tg_msg = await tg_bot_sender.send_message(
                chat_id=TELEGRAM_GROUP_ID, 
                text=formatted_text, 
                message_thread_id=matched_category["telegram_topic_id"] or None
            ) 
            save_message_pair(message.id, tg_msg.message_id, is_photo=False)
            stats_discord_to_tg += 1
            
    except Exception as e: 
        print(f"CRITICAL ERROR forwarding to Telegram: {e}")

@discord_bot.event
async def on_raw_message_delete(payload):
    if payload.message_id in DISCORD_TO_TELEGRAM_MAP:
        mapped_data = DISCORD_TO_TELEGRAM_MAP.pop(payload.message_id)
        telegram_msg_id = mapped_data[0]
        try:
            await tg_bot_sender.delete_message(chat_id=TELEGRAM_GROUP_ID, message_id=telegram_msg_id)
        except Exception as e:
            print(f"Error executing synchronized deletion: {e}")

@discord_bot.event
async def on_raw_message_edit(payload):
    author_data = payload.data.get('author') or {}
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
        display_name = display_name or "User"

        formatted_text = f"{display_name}:\n\n{new_content}"

        try:
            if is_photo:
                await tg_bot_sender.edit_message_caption(
                    chat_id=TELEGRAM_GROUP_ID,
                    message_id=telegram_msg_id,
                    caption=formatted_text
                )
            else:
                await tg_bot_sender.edit_message_text(
                    chat_id=TELEGRAM_GROUP_ID,
                    message_id=telegram_msg_id,
                    text=formatted_text
                )
        except Exception as e:
            print(f"Error executing synchronized edit on Telegram: {e}")

# --- TELEGRAM BOT LOGIC ---

async def telegram_receive_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    global bridge_active, stats_tg_to_discord, stats_photos_bridged
    if not bridge_active:
        return

    message = update.edited_message if update.edited_message else update.message
    
    if not message or message.from_user is None or message.from_user.is_bot:
        return

    incoming_topic_id = message.message_thread_id or 0
    matched_category = next((c for c in CATEGORIES.values() if c["telegram_topic_id"] == incoming_topic_id), None)
    if not matched_category:
        return

    target_channel = discord_bot.get_channel(matched_category["source_channel_id"]) 
    if not target_channel: 
        return 

    sender_name = message.from_user.first_name or message.from_user.username or "Telegram User"
    text_content = message.text or message.caption or "" 
    
    original_discord_text = f"**{sender_name}**" 
    if text_content: 
        original_discord_text += f"\n\n{text_content}" 

    if update.edited_message:
        if message.message_id in TELEGRAM_TO_DISCORD_MAP:
            discord_msg_id = TELEGRAM_TO_DISCORD_MAP[message.message_id]
            try:
                # Upgraded to robust fetch to prevent context loss on old caches
                partial_msg = target_channel.get_partial_message(discord_msg_id)
                await partial_msg.edit(content=original_discord_text)
            except Exception as e:
                print(f"Failed to edit Discord mirror: {e}")
        return

    byte_array = None 
    if message.photo: 
        photo = message.photo[-1] 
        tg_file = await context.bot.get_file(photo.file_id) 
        byte_array = await tg_file.download_as_bytearray() 

    discord_msg = None
    try:
        if byte_array: 
            file_stream = io.BytesIO(byte_array) 
            discord_file = discord.File(file_stream, filename="telegram_image.png") 
            discord_msg = await target_channel.send(content=original_discord_text, file=discord_file) 
            stats_tg_to_discord += 1
            stats_photos_bridged += 1
        elif text_content.strip(): 
            discord_msg = await target_channel.send(content=original_discord_text) 
            stats_tg_to_discord += 1
            
        if discord_msg:
            save_telegram_to_discord(message.message_id, discord_msg.id)
    except Exception as e:
        print(f"Error forwarding from Telegram to Discord: {e}")

async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Sync initiated: Please ensure channels are manually aligned.")

# --- INTEGRATED RUNNER ---

async def main(): 
    global tg_bot_sender, TELEGRAM_GROUP_ID

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
    app.router.add_get('/system', handle_system)
    app.router.add_get('/ping', handle_ping)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port, reuse_address=True) 
    await site.start()
    
    discord_token = clean_token("DISCORD_TOKEN")
    telegram_token = clean_token("TELEGRAM_TOKEN")
    TELEGRAM_GROUP_ID = safe_int("TELEGRAM_GROUP_ID", default=0)
     
    if not discord_token or not telegram_token or TELEGRAM_GROUP_ID == 0:
        return

    tg_app = ( 
        ApplicationBuilder() 
        .token(telegram_token) 
        .connect_timeout(30.0) 
        .read_timeout(30.0) 
        .write_timeout(30.0) 
        .build()
    ) 

    tg_bot_sender = tg_app.bot  
    tg_app.add_handler(CommandHandler("sync", sync_command))
    
    # FIX: Combined strict structural text/photo filter scopes.
    # python-telegram-bot routes edits natively into MessageHandlers unless explicitly bypassed.
    tg_msg_filter = filters.Chat(TELEGRAM_GROUP_ID) & (filters.TEXT | filters.PHOTO)
    tg_app.add_handler(MessageHandler(tg_msg_filter, telegram_receive_handler)) 

    await tg_app.initialize() 
    await tg_app.updater.start_polling(drop_pending_updates=True) 
    await tg_app.start() 

    try: 
        await discord_bot.start(discord_token) 
    finally: 
        try: await site.stop()
        except Exception: pass
        try: await tg_app.updater.stop() 
        except Exception: pass
        try: await tg_app.stop() 
        except Exception: pass
        try: await tg_app.shutdown() 
        except Exception: pass
        try: await discord_bot.close()
        except Exception: pass

if __name__ == '__main__': 
    try: 
        asyncio.run(main()) 
    except Exception as e: 
        print(f"Critical System Failure: {e}")
