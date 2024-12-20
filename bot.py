import sys
import glob
import importlib
from pathlib import Path
from pyrogram import idle
import logging
import logging.config
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Get logging configurations
logging.config.fileConfig('logging.conf')
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.ERROR)
logging.getLogger("imdbpy").setLevel(logging.ERROR)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logging.getLogger("aiohttp").setLevel(logging.ERROR)
logging.getLogger("aiohttp.web").setLevel(logging.ERROR)

from pyrogram import Client, __version__
from pyrogram.raw.all import layer
from database.ia_filterdb import Media
from database.users_chats_db import db
from info import *
from utils import temp
from typing import Union, Optional, AsyncGenerator
from pyrogram import types
from Script import script
from datetime import date, datetime
import pytz
from aiohttp import web
from plugins import web_server
import psutil
import asyncio
from Jisshu.bot import JisshuBot
from Jisshu.util.keepalive import ping_server
from Jisshu.bot.clients import initialize_clients

ppath = "plugins/*.py"
files = glob.glob(ppath)
JisshuBot.start()
loop = asyncio.get_event_loop()



async def Jisshu_start():
    print('\n')
    print('Initializing The Movie Provider Bot')
    bot_info = await JisshuBot.get_me()
    JisshuBot.username = bot_info.username
    await initialize_clients()
            
    for name in files:
        with open(name) as a:
            patt = Path(a.name)
            plugin_name = patt.stem.replace(".py", "")
            plugins_dir = Path(f"plugins/{plugin_name}.py")
            import_path = "plugins.{}".format(plugin_name)
            spec = importlib.util.spec_from_file_location(import_path, plugins_dir)
            load = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(load)
            sys.modules["plugins." + plugin_name] = load
            print("The Movie Provider Imported => " + plugin_name)

    if ON_HEROKU:
        asyncio.create_task(ping_server())

    # Initialize the scheduler
    scheduler = AsyncIOScheduler()

    async def start():
        # Run migration to ensure all users have correct expiry_time and notified fields
        await db.migrate_old_users()

        # Initialize temporary data
        b_users, b_chats = await db.get_banned()
        temp.BANNED_USERS = b_users
        temp.BANNED_CHATS = b_chats

    await notify_restart_success(JisshuBot)
    await Media.ensure_indexes()
    me = await JisshuBot.get_me()
    temp.ME = me.id
    temp.U_NAME = me.username
    temp.B_NAME = me.first_name
    JisshuBot.username = '@' + me.username
    logging.info(f"{me.first_name} with Pyrogram v{__version__} (Layer {layer}) started on {me.username}.")
    logging.info(script.LOGO)
    
    # Add system stats (optional)
    stats = get_system_stats()
    logging.info(f"System Stats: {stats}")

    # Set up the scheduler job to check for expired plans every 60 minutes
    scheduler.add_job(check_expired_plans, "interval", minutes=120)
    scheduler.start()
    tz = pytz.timezone('Asia/Kolkata')
    today = date.today()
    now = datetime.now(tz)
    time = now.strftime("%H:%M:%S %p")
    await JisshuBot.send_message(chat_id=LOG_CHANNEL, text=script.RESTART_TXT.format(today, time))
    app = web.AppRunner(await web_server())
    await app.setup()
    bind_address = "0.0.0.0"
    await web.TCPSite(app, bind_address, PORT).start()
    await idle()


async def stop(*args):
    scheduler.shutdown()
    await JisshuBot.stop()
    logging.info("Bot stopped. Bye.")

# Initialize bot and scheduler
async def notify_restart_success(bot):
    """
    Notify the user after a successful restart.
    """
    if os.path.exists(".restartmsg"):
        try:
            with open(".restartmsg", "r") as f:
                chat_id, message_id = map(str.strip, f.readlines())

            await bot.send_message(
                chat_id=int(chat_id),
                text="Bot has successfully restarted! 😊"
            )
        except Exception as e:
            logging.error(f"Failed to send restart success message: {e}")
        finally:
            os.remove(".restartmsg")

# New system stats function
def get_system_stats():
    memory = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=1)
    disk = psutil.disk_usage('/')
    return f"Memory: {memory.percent}%, CPU: {cpu}%, Disk: {disk.percent}%"
    


async def check_expired_plans():
    """Periodically check for expired plans and notify users, logging each step."""
    current_time = datetime.utcnow()  # Use UTC to match MongoDB storage
    logging.info("Running expired plan check...")

    try:
        # Retrieve users with expired premium who have not been notified and are reachable
        async for user in db.users.find(
            {
                "expiry_time": {"$lt": current_time},
                "notified": {"$ne": True},
                "unreachable": {"$ne": True}  # Exclude users marked as unreachable
            }
        ):
            user_id = user["id"]
            logging.info(f"Attempting to notify user {user_id} with expiry time {user['expiry_time']}")

            try:
                # Get the user info to retrieve first name
                user_info = await JisshuBot.get_users(user_id)
                first_name = user_info.first_name if user_info else "User"
                # Send expiration notification to the user
                await JisshuBot.send_message(
                    chat_id=user_id,
                    text=f"**Hi {first_name}, \nYour premium plan has expired. Click on /plans to renew it and continue enjoying premium features.**"
                )

                # Mark user as notified to prevent duplicate notifications
                await db.users.update_one({"id": user_id}, {"$set": {"notified": True}})
                logging.info(f"Notified user {user_id} of premium expiration")

                # Convert current_time to Asia/Kolkata timezone
                tz = pytz.timezone('Asia/Kolkata')
                current_time_ist = current_time.astimezone(tz)
                # Send notification to the log channel and admin ID
                notification_text = (
                    f"#Expiry \nUser: {first_name}\nUser ID: <code>{user_id}</code> \n"
                    f"Premium has expired and the user was notified at {current_time_ist.strftime('%Y-%m-%d')}."
                )
                await JisshuBot.send_message(chat_id=LOG_CHANNEL, text=notification_text)
                await JisshuBot.send_message(chat_id=ADMINID, text=notification_text)

            except pyrogram.errors.exceptions.bad_request_400.PeerIdInvalid:
                # Log and mark the user as unreachable
                logging.warning(f"User {user_id} has an invalid peer ID and cannot be reached.")
                await db.users.update_one({"id": user_id}, {"$set": {"notified": True, "unreachable": True}})

            except pyrogram.errors.exceptions.bad_request_400.InputUserDeactivated:
                logging.warning(f"User {user_id} is deactivated; removing from database.")
                await db.users.delete_one({"id": user_id})

            except pyrogram.errors.exceptions.bad_request_400.UserIsBlocked:
                logging.warning(f"User {user_id} blocked the bot; skipping notification.")
                await db.users.update_one({"id": user_id}, {"$set": {"notified": True, "unreachable": True}})

            except Exception as e:
                logging.error(f"Failed to notify user {user_id} of expiration: {e}")

    except Exception as e:
        logging.error(f"Error during expired plan check: {e}")

    logging.info("Expired plan check completed.")


if __name__ == '__main__':
    try:
        loop.run_until_complete(Jisshu_start())
    except KeyboardInterrupt:
        logging.info('Service Stopped. Bye 👋')
