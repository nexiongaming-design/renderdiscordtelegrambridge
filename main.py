import os
import discord
from discord.ext import commands
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler
from dotenv import load_dotenv
import asyncio
import io
import aiofiles
from aiohttp import web

# Web health check server imports
async def handle_health(request):
    # Get the folder where main.py is located
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, 'index.html')
    
    async with aiofiles.open(file_path, mode='r') as f:
        content = await f.read()
    return web.Response(text=content, content_type='text/html')

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

# --- PERIODIC CACHE CLEANER ---
async def cache_cleaner():
    """Voorkomt geheugenlekken door caches periodiek te halveren indien vol."""
    while True:
        await asyncio.sleep(86400)  # Draait elke 24 uur
        print("--- CACHE CLEANUP --- Periodieke opschoning gestart...")
        # Zorgt ervoor dat we alleen opschonen als we de helft van de max size bereiken
        while len(DISCORD_TO_TELEGRAM_MAP) > (MAX_MAP_SIZE // 2):
            DISCORD_TO_TELEGRAM_MAP.pop(next(iter(DISCORD_TO_TELEGRAM_MAP)))
        while len(TELEGRAM_TO_DISCORD_MAP) > (MAX_MAP_SIZE // 2):
            TELEGRAM_TO_DISCORD_MAP.pop(next(iter(TELEGRAM_TO_DISCORD_MAP)))
        print("--- CACHE CLEANUP --- Geheugen is succesvol geoptimaliseerd.")


# Initialize Discord bot
intents = discord.Intents.default()
intents.message_content = True
discord_bot = commands.Bot(command_prefix="!", intents=intents)


# --- DISCORD BOT LOGIC (Discord Translated Channels -> Telegram Topics) ---

@discord_bot.event
async def on_ready():
    print(f'Logged in to Discord successfully as: {discord_bot.user.name} - VERSIE 2.0')
    print(f'Total language channels monitored across all categories: {len(ALL_MONITORED_DISCORD_CHANNELS)}')
    for cat_name, mapping in CATEGORIES.items():
        print(f" -> Matrix Active [{cat_name.upper()}]: Listening to {len(mapping['listen_channels'])} channels | Routing to Topic ID: {mapping['telegram_topic_id']}")


@discord_bot.event
async def on_message(message):
    global TELEGRAM_GROUP_ID
    
    # 1. Ignore messages sent by any bot (prevents infinite loops)
    if message.author.bot:
        return
    
    # 2. Only process if the channel is one we monitor
    if message.channel.id not in ALL_MONITORED_DISCORD_CHANNELS:
        return
        
    # 3. Find the category configuration
    matched_category = None
    for cat_name, config in CATEGORIES.items():
        if message.channel.id in config["listen_channels"]:
            matched_category = config
            break

    if not matched_category:
        return

    print(f"--- DISCORD DEBUG --- Match Found! Relaying from channel {message.channel.id} to Telegram Topic {matched_category['telegram_topic_id']}")

    sender_name = message.author.display_name
    target_topic = matched_category["telegram_topic_id"]
    
    dynamic_content = message.content or ""
    
    # Handle Embeds (if any)
    if message.embeds:
        for embed in message.embeds:
            embed_parts = []
            if embed.title:
                embed_parts.append(f"=== {embed.title.upper()} ===")
            if embed.description:
                embed_parts.append(embed.description)
            for field in embed.fields:
                embed_parts.append(f"• {field.name}:\n{field.value}")
            
            if embed_parts:
                joined_embed_str = "\n\n".join(embed_parts)
                if dynamic_content:
                    dynamic_content += "\n\n" + joined_embed_str
                else:
                    dynamic_content = joined_embed_str

    formatted_text = f"{sender_name}:\n\n{dynamic_content}"

    try:
        if message.attachments:
            attachment = message.attachments[0]
            if attachment.filename.lower().endswith(('png', 'jpg', 'jpeg', 'webp')):
                image_bytes = await attachment.read()
                
                tg_msg = await tg_bot_sender.send_photo(
                    chat_id=TELEGRAM_GROUP_ID,
                    photo=image_bytes,
                    caption=formatted_text if dynamic_content else f"{sender_name}:",
                    message_thread_id=target_topic if target_topic != 0 else None
                )
                save_message_pair(message.id, tg_msg.message_id, is_photo=True)
                return  

        if dynamic_content:
            tg_msg = await tg_bot_sender.send_message(
                chat_id=TELEGRAM_GROUP_ID,
                text=formatted_text,
                message_thread_id=target_topic if target_topic != 0 else None
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
    # FIX: Ignore raw payload changes initiated by translator bots or system bots
    author_data = payload.data.get('author', {})
    if author_data.get('bot'):
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
