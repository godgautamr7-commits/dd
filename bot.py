#!/usr/bin/env python3
"""LXD/LXC VPS Deploy Discord Bot  -  run: python3 bot.py  (as root, on the LXD host)"""
import asyncio
import datetime as dt
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path

if __name__ == "__main__":
    import startup
    startup.main_entry(sys.argv)  # checks + auto-fix, then starts itself in the background

import aiohttp
import discord
from aiohttp import web, WSMsgType
from discord import app_commands
from discord.ext import commands, tasks

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import agent as nodeapi

CREATOR = os.getenv("CREATOR_NAME", "Diablo")

# ------------------------------------------------------------------ "online on mobile" status
# Discord shows the phone icon when the gateway login says the client is a mobile app.
# (Unofficial trick: if a future discord.py changes its internals this is skipped and the bot still works.)
try:
    from discord.gateway import DiscordWebSocket as _DWS
    _orig_identify = _DWS.identify

    async def _mobile_identify(self):
        try:
            payload = {"op": self.IDENTIFY, "d": {
                "token": self.token,
                "properties": {"os": "Android", "browser": "Discord Android", "device": "Discord Android",
                               "$os": "Android", "$browser": "Discord Android", "$device": "Discord Android"},
                "compress": True, "large_threshold": 250}}
            if self.shard_id is not None and self.shard_count is not None:
                payload["d"]["shard"] = [self.shard_id, self.shard_count]
            state = self._connection
            if state._activity is not None or state._status is not None:
                payload["d"]["presence"] = {"status": state._status, "game": state._activity, "since": 0, "afk": False}
            if state._intents is not None:
                payload["d"]["intents"] = state._intents.value
            await self.call_hooks("before_identify", self.shard_id, initial=self._initial_identify)
            await self.send_as_json(payload)
        except AttributeError:
            await _orig_identify(self)
    _DWS.identify = _mobile_identify
except Exception as _e:
    print("mobile status trick not applied:", _e)

TOKEN = os.environ["DISCORD_TOKEN"]
MIN_AGE_DAYS = int(os.getenv("MIN_ACCOUNT_AGE_DAYS", "213"))  # ~7 months
VPS_DAYS = int(os.getenv("VPS_DAYS", "30"))
GRACE_DAYS = 3
# bot owners (full admin + can add/remove other admins) - set ADMIN_IDS=123,456 in .env
OWNER_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.strip().isdigit()}
LOCAL_MAX = 25  # node1 hard cap
BT = "```"

# invites: (ram GB, cpu, disk GB)
PLANS = {2: (2, 1, 30), 4: (5, 2, 40), 6: (8, 2, 50), 8: (16, 3, 70)}
IMAGES = nodeapi.IMAGES

# ------------------------------------------------------------------ database
db = sqlite3.connect("bot.db", check_same_thread=False)
try:
    db.execute("pragma journal_mode=wal")
    db.execute("pragma synchronous=normal")
except Exception:
    pass
db.executescript("""
create table if not exists joins(id integer primary key autoincrement, user_id int, inviter_id int,
    kind text, left int default 0, ts int);
create table if not exists spent(user_id int primary key, used int default 0);
create table if not exists nodes(name text primary key, token text, host text, port int,
    max_vps int, is_local int default 0, online int default 0);
create table if not exists vps(name text primary key, owner_id int, node text, invites int,
    os text, expires int, status text);
create table if not exists settings(k text primary key, v text);
create table if not exists admins(user_id int primary key);
""")
db.commit()


def q(sql, *a):
    cur = db.execute(sql, a)
    db.commit()
    return cur.fetchall()


def get(k):
    r = q("select v from settings where k=?", k)
    return r[0][0] if r else None


def put(k, v):
    q("insert into settings values(?,?) on conflict(k) do update set v=excluded.v", k, str(v))


def stats(uid):
    valid = fake = rejoin = left = 0
    for kind, l, c in q("select kind,left,count(*) from joins where inviter_id=? group by kind,left", uid):
        if kind == "valid":
            if l:
                left += c
            else:
                valid += c
        elif kind == "fake":
            fake += c
        else:
            rejoin += c
    used = (q("select used from spent where user_id=?", uid) or [[0]])[0][0]
    return dict(valid=valid, fake=fake, rejoin=rejoin, left=left, used=used, avail=max(valid - used, 0))


def spend(uid, n):
    q("insert into spent values(?,?) on conflict(user_id) do update set used=max(used+?,0)", uid, max(n, 0), n)


# ------------------------------------------------------------------ helpers
def is_owner(u):
    return u.id in OWNER_IDS


def is_admin(u):
    if u.id in OWNER_IDS or q("select 1 from admins where user_id=?", u.id):
        return True
    return isinstance(u, discord.Member) and u.guild_permissions.administrator


def channel_ok(user, channel_id):
    ch = get("invite_channel")
    return not ch or str(channel_id) == ch or is_admin(user)


class Tree(app_commands.CommandTree):
    async def interaction_check(self, i):
        if channel_ok(i.user, i.channel_id):
            return True
        await i.response.send_message(f"Use this command in <#{get('invite_channel')}> only.", ephemeral=True)
        return False


intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class Bot(commands.Bot):
    async def setup_hook(self):
        if not q("select 1 from nodes where name='node1'"):
            q("insert into nodes(name,max_vps,is_local,online) values('node1',?,1,0)", LOCAL_MAX)
        q("update nodes set max_vps=? where name='node1'", LOCAL_MAX)
        q("update nodes set online=0 where is_local=0")  # remote nodes become online when they connect
        await start_panel_server()
        asyncio.create_task(bootstrap_local())  # auto-install LXD + prepare node1 in the background
        housekeeping.start()
        poll_nodes.start()
        presence_loop.start()
        # old global registrations are removed; commands are synced per-guild (appear instantly)
        try:
            await self.http.bulk_upsert_global_commands(self.application_id, [])
        except Exception as e:
            print("global clear failed:", e)


bot = Bot(command_prefix="-", intents=intents, tree_cls=Tree, help_command=None,
          status=discord.Status.online,
          activity=discord.Activity(type=discord.ActivityType.watching, name="☁️ Legacy Cloud V1"))


@bot.check
async def _global_check(ctx):
    return channel_ok(ctx.author, ctx.channel.id)


@bot.event
async def on_command_error(ctx, err):
    if isinstance(err, commands.CheckFailure):
        await ctx.send(f"Use this command in <#{get('invite_channel')}> only.", delete_after=5)
    elif not isinstance(err, commands.CommandNotFound):
        print("cmd error:", err)


async def dm(uid, text):
    try:
        u = bot.get_user(uid) or await bot.fetch_user(uid)
        await u.send(text)
    except Exception:
        pass


async def log(text):
    ch = get("log_channel")
    if ch:
        c = bot.get_channel(int(ch))
        if c:
            try:
                await c.send(text)
            except Exception:
                pass


# ------------------------------------------------------------------ admin command audit log
ADMIN_CMDS = {
    "deploy", "vps-list", "vps-delete", "vps-suspend", "vps-unsuspend", "admin-renew", "vps-expire",
    "vps-expire-all", "vps-stats", "reset-ssh", "invites-give", "invites-remove", "health", "diag", "fix",
    "node-install", "node-domain", "node-add", "node-list", "node-remove", "node-max", "setup",
    "admin-add", "admin-remove", "admin-list",
}
SECRET_ARGS = {"token"}


def _fmt_args(i):
    parts = []
    try:
        for k, v in i.namespace:
            if k in SECRET_ARGS:
                v = "***hidden***"
            elif hasattr(v, "mention"):
                v = v.mention
            parts.append(f"{k}={str(v)[:60]}")
    except Exception:
        pass
    return " ".join(parts)


@bot.event
async def on_app_command_completion(i: discord.Interaction, command):
    if command.qualified_name in ADMIN_CMDS:
        await log(f"🛡️ <@{i.user.id}> used `/{command.qualified_name}`")


async def bootstrap_local():
    """node1 = the machine this bot runs on. Installs/initialises LXD automatically if needed."""
    try:
        note = await nodeapi.ensure_lxd()
        await nodeapi.apply_protection()
        q("update nodes set online=1 where name='node1'")
        asyncio.create_task(nodeapi.watchdog_loop())
        async def _prewarm():
            async def _np(*a):
                pass
            for k in ("ubuntu22",):
                try:
                    await nodeapi._ensure_image(k, _np)
                    print(f"image {k} cached")
                except Exception as e:
                    print(f"image {k} pre-download failed: {e}")
                    await log(f"⚠️ OS image pre-download failed: `{str(e)[:700]}`")
        async def _quota():
            await _prewarm()
            note = await nodeapi.ensure_quota_pool()
            print("storage:", note)
            await bot.wait_until_ready()
            await log(("✅ " if "enforced" in note else "⚠️ ") + f"Disk limits: {note}")
        asyncio.create_task(_quota())
        print("node1 ready" + (f" ({note})" if note else ""))
        if note:
            await bot.wait_until_ready()
            await log(f"✅ node1 set up automatically: {note}")
    except Exception as e:
        print("node1 auto-setup failed:", e)
        await bot.wait_until_ready()
        await log(f"⚠️ node1 auto-setup failed: {e}")


# ------------------------------------------------------------------ invite tracking
invite_cache = {}


async def snapshot(g):
    try:
        return {i.code: (i.uses or 0, i.inviter.id if i.inviter else 0) for i in await g.invites()}
    except discord.HTTPException:
        return None


async def sync_guild(g):
    bot.tree.copy_global_to(guild=g)
    try:
        n = await bot.tree.sync(guild=g)
        print(f"synced {len(n)} slash commands to guild {g.name} ({g.id})")
        return len(n)
    except Exception as e:
        print(f"SLASH SYNC FAILED in {g.name} ({g.id}): {e}  -> re-invite the bot with the applications.commands scope")
        return -1


@bot.event
async def on_ready():
    for g in bot.guilds:
        s = await snapshot(g)
        if s is not None:
            invite_cache[g.id] = s
        await sync_guild(g)
    print("ready as", bot.user)


@bot.event
async def on_guild_join(g):
    invite_cache[g.id] = await snapshot(g) or {}
    await sync_guild(g)


@bot.event
async def on_invite_create(inv):
    invite_cache.setdefault(inv.guild.id, {})[inv.code] = (inv.uses or 0, inv.inviter.id if inv.inviter else 0)


@bot.event
async def on_invite_delete(inv):
    invite_cache.get(inv.guild.id, {}).pop(inv.code, None)


@bot.event
async def on_member_join(m):
    if m.bot:
        return
    old = invite_cache.get(m.guild.id, {})
    new = await snapshot(m.guild)
    if new is None:
        return
    invite_cache[m.guild.id] = new
    inviter = next((inv for code, (uses, inv) in new.items() if uses > old.get(code, (0, 0))[0]), None)
    if not inviter:
        return
    seen = q("select 1 from joins where user_id=?", m.id)
    age = (dt.datetime.now(dt.timezone.utc) - m.created_at).days
    if seen:
        kind = "rejoin"
    elif age < MIN_AGE_DAYS or inviter == m.id:
        kind = "fake"
    else:
        kind = "valid"
    q("insert into joins(user_id,inviter_id,kind,ts) values(?,?,?,?)", m.id, inviter, kind, int(time.time()))


@bot.event
async def on_member_remove(m):
    q("update joins set left=1 where id=(select max(id) from joins where user_id=?)", m.id)


# ------------------------------------------------------------------ nodes
HEAL_LOCK = asyncio.Lock()
START_TS = time.time()


async def heal_local():
    """Self-repair for node1: restart LXD, re-run install/init checks, re-apply firewall. True if lxc works after."""
    async with HEAL_LOCK:
        for cmd in ("systemctl restart snap.lxd.daemon", "snap start lxd"):
            await nodeapi.sh("bash", "-c", cmd, timeout=120)
        await nodeapi.sh("lxd", "waitready", "--timeout", "90", timeout=120)
        try:
            await nodeapi.ensure_lxd()
            await nodeapi.apply_protection()
        except Exception as e:
            await log(f"❌ Auto-fix of node1 failed: {e}")
            return False
        rc, _ = await nodeapi.sh("lxc", "list", "--format", "json")
        if rc == 0:
            q("update nodes set online=1 where name='node1'")
        return rc == 0


CREATE_TIMEOUT = 840  # seconds (< Discord's 15-minute interaction limit)
LXD_DOWN_RE = re.compile(r"unix\.socket|cannot connect|not running|connection refused|daemon|snap\.lxd|"
                         r"lxd is not|failed to connect", re.I)


# ------------------------------------------------------------------ node links (node -> bot, outbound WebSocket)
# Remote nodes dial IN to this bot through the Cloudflare tunnel, so they need no IP / port of their own.
# Point the tunnel at http://localhost:PANEL_PORT  (cloudflared: `cloudflared tunnel --url http://localhost:8765`).
PANEL_PORT = int(os.getenv("PANEL_PORT", "8765"))
PANEL_BIND = os.getenv("PANEL_BIND", "127.0.0.1")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
if "PASTE" in PUBLIC_URL:
    PUBLIC_URL = ""
NODE_LINKS = {}  # node name -> {"ws": WebSocketResponse, "pending": {request id: Future}}


async def ws_handler(request):
    auth = request.headers.get("Authorization", "")
    tok = auth[7:] if auth.startswith("Bearer ") else ""
    name = None
    for n, t in q("select name,token from nodes where is_local=0 and token is not null"):
        if tok and t and secrets.compare_digest(t.encode(), tok.encode()):
            name = n
            break
    if not name:
        await asyncio.sleep(1)  # slow down token guessing
        return web.Response(status=401, text="unauthorized")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=0)
    await ws.prepare(request)
    old = NODE_LINKS.get(name)
    if old:  # same node reconnecting: drop the stale link
        try:
            await old["ws"].close()
        except Exception:
            pass
    link = {"ws": ws, "pending": {}}
    NODE_LINKS[name] = link
    q("update nodes set online=1 where name=?", name)
    await log(f"🟢 Node `{name}` connected.")
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
            except ValueError:
                continue
            fut = link["pending"].pop(d.get("id"), None)
            if fut and not fut.done():
                if d.get("error"):
                    fut.set_exception(RuntimeError(d["error"]))
                else:
                    fut.set_result(d.get("data"))
    finally:
        for fut in link["pending"].values():
            if not fut.done():
                fut.set_exception(RuntimeError(f"node {name} disconnected"))
        link["pending"].clear()
        if NODE_LINKS.get(name) is link:
            del NODE_LINKS[name]
            q("update nodes set online=0 where name=?", name)
            await log(f"🔴 Node `{name}` disconnected.")
    return ws


async def start_panel_server():
    try:
        app = web.Application()
        app.router.add_get("/ws", ws_handler)
        app.router.add_get("/", lambda r: web.Response(text="ok"))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, PANEL_BIND, PANEL_PORT).start()
        print(f"node link server on {PANEL_BIND}:{PANEL_PORT} (expose it with Cloudflare Tunnel)")
    except Exception as e:
        print("node link server failed to start:", e)


async def call_remote(node, op, kw):
    link = NODE_LINKS.get(node)
    if not link or link["ws"].closed:
        raise RuntimeError(f"node {node} is offline (not connected to the bot)")
    rid = secrets.token_hex(8)
    fut = asyncio.get_running_loop().create_future()
    link["pending"][rid] = fut
    try:
        await link["ws"].send_json({"id": rid, "op": op, "kw": kw})
        return await asyncio.wait_for(fut, CREATE_TIMEOUT if op == "create" else 200)
    except asyncio.TimeoutError:
        raise RuntimeError(f"node {node} did not answer in time")
    except ConnectionError as e:
        raise RuntimeError(f"node {node} unreachable: {e}")
    finally:
        link["pending"].pop(rid, None)


async def call(node, op, _notify=None, _progress=None, **kw):
    r = q("select is_local from nodes where name=?", node)
    if not r or r[0][0]:
        if _progress and op == "create":
            kw["progress"] = _progress
        try:
            if op == "create":
                try:
                    return await asyncio.wait_for(nodeapi.OPS[op](**kw), CREATE_TIMEOUT)
                except asyncio.TimeoutError:
                    await nodeapi.sh("lxc", "delete", kw["name"], "--force")
                    raise RuntimeError(f"timed out after {CREATE_TIMEOUT // 60} min - container removed")
            return await nodeapi.OPS[op](**kw)  # local node1
        except ValueError:
            raise
        except Exception as e:
            # Only auto-heal (and retry) when LXD itself looks broken. A normal failure (no network, timeout,
            # quota ...) is reported straight away instead of silently running the whole create a 2nd time.
            if op == "create" and not LXD_DOWN_RE.search(str(e)):
                raise
            if _notify:
                try:
                    await _notify("⚠️ A problem came up on the server. **Fixing it automatically - please wait ~2 minutes...**")
                except Exception:
                    pass
            await log(f"⚠️ `{op}` failed on node1 ({str(e)[:200]}) - auto-fixing")
            if await heal_local():
                return await nodeapi.OPS[op](**kw)
            raise RuntimeError(f"auto-fix failed: {e}")
    return await call_remote(node, op, kw)


def pick_node():
    best = None
    for name, mx in q("select name,max_vps from nodes where online=1"):
        c = q("select count(*) from vps where node=? and status!='deleted'", name)[0][0]
        if c < mx and (best is None or c / mx < best[0]):
            best = (c / mx, name)
    return best[1] if best else None


# ------------------------------------------------------------------ progress bar + credentials
def pbar(p, w=18):
    f = int(w * min(max(p, 0), 100) / 100)
    return "[" + "█" * f + "░" * (w - f) + f"] {int(p)}%"


def prog_embed(title, pct, user, cid, node, ok_text, done=False, fail=None, dm_ok=True, stage=None, elapsed=0):
    e = discord.Embed(title=title, color=0x57F287 if done else (0xED4245 if fail else 0x5865F2))
    if fail:
        e.description = f"❌ **Failed:** {str(fail)[:900]}"
    else:
        working = f"⏳ **{stage or 'Working... please wait'}** · {int(elapsed)}s"
        e.description = (f"✅ **{ok_text}**" if done else working) + f"\n`{pbar(pct)}`"
    e.add_field(name="👤 Owner Tag", value=user.mention)
    e.add_field(name="📦 Container ID", value=f"`{cid}`")
    e.add_field(name="🌐 Cluster Node", value=f"`{str(node).upper()}`")
    if done:
        e.set_footer(text="🔒 SSHX access link sent directly to your DMs." if dm_ok
                     else "⚠️ Your DMs are closed - open them and use /myvps -> Reset SSH.")
    return e


async def run_with_progress(coro, channel, user, cid, node, title, ok_text, state=None):
    """Runs coro while a public embed shows the REAL stage (state = {"pct":..,"text":..} updated by the worker).
    Without a real stage (remote node) it falls back to a slow time-based bar. On error the embed turns red."""
    msg = None
    if channel is not None:
        try:
            msg = await channel.send(embed=prog_embed(title, 0, user, cid, node, ok_text, stage="Starting"))
        except Exception:
            msg = None
    task = asyncio.ensure_future(coro)
    t0 = time.time()
    while not task.done():
        if msg:
            el = time.time() - t0
            if state and state.get("text"):
                p, stage = state["pct"], state["text"]
            else:
                p, stage = 95 * (1 - math.exp(-el / 45)), None
            try:
                await msg.edit(embed=prog_embed(title, p, user, cid, node, ok_text, stage=stage, elapsed=el))
            except Exception:
                pass
        await asyncio.wait({task}, timeout=3)
    try:
        res = task.result()
    except BaseException as e:
        if msg:
            try:
                where = f" (stopped at: {state['text']})" if state and state.get("text") else ""
                await msg.edit(embed=prog_embed(title, 0, user, cid, node, ok_text, fail=f"{e}{where}"))
            except Exception:
                pass
        raise
    return res, msg


async def finish_progress(msg, title, user, cid, node, ok_text, dm_ok=True):
    if msg:
        try:
            await msg.edit(embed=prog_embed(title, 100, user, cid, node, ok_text, done=True, dm_ok=dm_ok))
        except Exception:
            pass


def creds_text(res):
    link = res.get("sshx")
    if not link:
        return "(SSHX link was not available - use Reset SSH in /myvps)"
    return (f"**SSHX (web terminal link - root access of your VPS):**\n{link}\n"
            f"No password needed - just open the link.")


# ------------------------------------------------------------------ deploy / renew
async def do_deploy(user, plan, os_key, free=False, node=None, notify=None, channel=None):
    ram, cpu, disk = PLANS[plan]
    if not free and stats(user.id)["avail"] < plan:
        return "❌ Not enough invites."
    if node:
        r = q("select online,max_vps from nodes where name=?", node)
        if not r:
            return f"❌ Node `{node}` not found."
        if not r[0][0]:
            return f"❌ Node `{node}` is offline."
        used = q("select count(*) from vps where node=? and status!='deleted'", node)[0][0]
        if used >= r[0][1]:
            return f"❌ Node `{node}` is full ({used}/{r[0][1]})."
    else:
        node = pick_node()
        if not node:
            return "❌ All nodes are full right now. Try again later."
    name = f"vps-{secrets.token_hex(4)}"
    if not free:
        spend(user.id, plan)
    q("insert into vps values(?,?,?,?,?,?,?)", name, user.id, node, plan, os_key, 0, "creating")
    T = "▶️ Starting VPS Instance Engine"
    OK = "VPS Boot Sequence Completed Successfully!"
    state = {"pct": 0, "text": ""}

    def on_prog(pct, text):
        state["pct"], state["text"] = pct, text
    try:
        res, pmsg = await run_with_progress(
            call(node, "create", _notify=notify, _progress=on_prog,
                 name=name, os_key=os_key, cpu=cpu, ram=ram, disk=disk),
            channel, user, name, node, T, OK, state=state)
    except Exception as e:
        q("delete from vps where name=?", name)
        if not free:
            spend(user.id, -plan)
        await log(f"❌ Deploy failed for <@{user.id}> on `{node}`: `{str(e)[:200]}`")
        return f"❌ Deploy failed: {e}"
    exp = int(time.time() + VPS_DAYS * 86400)
    q("update vps set expires=?, status='running' where name=?", exp, name)
    bump_created()
    text = (f"✅ **VPS deployed!**\nName: `{name}` · Node: {node}\nOS: {IMAGES[os_key][0]}\n"
            f"Specs: {ram}GB RAM · {cpu} CPU · {disk}GB disk\nExpires: <t:{exp}:R>\n\n"
            f"{creds_text(res)}\n\nManage with `/myvps`")
    await log(f"🚀 VPS `{name}` deployed for <@{user.id}> · node `{node}` · {IMAGES[os_key][0]} · "
              f"{ram}GB/{cpu}CPU/{disk}GB · {'free (admin)' if free else f'{plan} invites used'}")
    try:
        await (await user.create_dm()).send(text)
        await finish_progress(pmsg, T, user, name, node, OK, dm_ok=True)
        return "✅ VPS created! SSH details sent to your DMs."
    except discord.HTTPException:
        await finish_progress(pmsg, T, user, name, node, OK, dm_ok=False)
        return text + "\n⚠️ Your DMs are closed, so details are shown here (only you can see this)."


async def do_renew(uid, name, admin=False, days=None):
    r = q("select owner_id,node,invites,expires,status from vps where name=?", name)
    if not r:
        return "❌ VPS not found."
    owner, node, cost, exp, status = r[0]
    if owner != uid and not admin:
        return "❌ Not your VPS."
    if status not in ("running", "suspended"):
        return "❌ This VPS cannot be renewed."
    left_txt = ""
    if not admin:
        have = stats(uid)["avail"]
        if have < cost:
            return f"❌ Renewal needs **{cost}** invites, you only have **{have}**."
        spend(uid, cost)
        left_txt = f" (🎟️ {cost} invites cut, {have - cost} left)"
    new = max(exp, int(time.time())) + (days or VPS_DAYS) * 86400
    q("update vps set expires=?, status='running' where name=?", new, name)
    await log(f"🔁 VPS `{name}` renewed by <@{uid}>{' (admin)' if admin else ''} until <t:{new}:F>")
    if status == "suspended":
        try:
            await call(node, "action", name=name, action="start")
        except Exception as e:
            return f"⚠️ Renewed but start failed: {e}"
    return f"✅ Renewed until <t:{new}:F>.{left_txt}"


# ------------------------------------------------------------------ views
class SafeView(discord.ui.View):
    """Base for all menus/buttons: an error is logged and the user gets a clear message
    (instead of Discord's vague 'did not respond in time')."""
    async def on_error(self, i, error, item):
        import traceback
        print("view error:", "".join(traceback.format_exception(type(error), error, error.__traceback__)), flush=True)
        try:
            await log(f"⚠️ Menu/button error for <@{i.user.id}>: `{str(error)[:300]}`")
        except Exception:
            pass
        try:
            msg = "⚠️ Something went wrong. Please run the command again."
            if i.response.is_done():
                await i.followup.send(msg, ephemeral=True)
            else:
                await i.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


class OSView(SafeView):
    def __init__(self, uid, plan):
        super().__init__(timeout=300)
        self.uid, self.plan = uid, plan
        self.sel = discord.ui.Select(placeholder="Choose OS", options=[
            discord.SelectOption(label=v[0], value=k) for k, v in IMAGES.items()])
        self.sel.callback = self.cb
        self.add_item(self.sel)

    async def cb(self, i):
        if i.user.id != self.uid:
            return await i.response.send_message("Not yours.", ephemeral=True)
        await i.response.defer()  # acknowledge within 3s first, then build the next step
        await i.edit_original_response(content="Choose a node to deploy on:",
                                       view=NodeView(self.uid, self.plan, self.sel.values[0]))


class NodeView(SafeView):
    """Last step of /deploy: pick a node (or Auto) and deploy."""
    def __init__(self, uid, plan, os_key):
        super().__init__(timeout=300)
        self.uid, self.plan, self.os_key = uid, plan, os_key
        self._busy = False  # blocks a double click from deploying (and charging) twice
        opts = [discord.SelectOption(label="Auto (least loaded node)", value="auto", emoji="🤖")]
        for name, mx, on in q("select name,max_vps,online from nodes order by name"):
            c = q("select count(*) from vps where node=? and status!='deleted'", name)[0][0]
            if on and c < mx:
                opts.append(discord.SelectOption(label=f"{name} — {c}/{mx} VPS", value=name))
        self.sel = discord.ui.Select(placeholder="Choose node", options=opts[:25])
        self.sel.callback = self.cb
        self.add_item(self.sel)

    async def cb(self, i):
        if i.user.id != self.uid:
            return await i.response.send_message("Not yours.", ephemeral=True)
        if self._busy:
            return await i.response.send_message("⏳ Already deploying - please wait.", ephemeral=True)
        self._busy = True
        v = self.sel.values[0]
        await i.response.edit_message(content="⏳ Deploying… live progress is shown in the channel.", view=None)
        msg = await do_deploy(i.user, self.plan, self.os_key, node=None if v == "auto" else v,
                              notify=lambda t: i.edit_original_response(content=t), channel=i.channel)
        await i.edit_original_response(content=msg)


class RenewView(SafeView):
    """/renew without a name: pick one of your VPS from a list."""
    def __init__(self, uid, rows):
        super().__init__(timeout=300)
        self.uid = uid
        self._busy = False  # blocks a double click from charging twice
        self.sel = discord.ui.Select(placeholder="Select a VPS to renew", options=[
            discord.SelectOption(label=f"{n} ({st})"[:100], value=n,
                                 description=f"costs {inv} invites · {node}"[:100])
            for n, node, inv, st in rows[:25]])
        self.sel.callback = self.cb
        self.add_item(self.sel)

    async def cb(self, i):
        if i.user.id != self.uid:
            return await i.response.send_message("Not yours.", ephemeral=True)
        if self._busy:
            return await i.response.send_message("⏳ Already renewing - please wait.", ephemeral=True)
        self._busy = True
        await i.response.edit_message(content="⏳ Renewing…", view=None)
        msg = await do_renew(self.uid, self.sel.values[0], admin=False)
        await i.edit_original_response(content=msg)


class PlanView(SafeView):
    def __init__(self, uid, avail, show_all=False):
        super().__init__(timeout=300)
        self.uid = uid
        self.sel = discord.ui.Select(placeholder="Choose a VPS plan", options=[
            discord.SelectOption(label=f"{k} invites — {r}GB RAM / {c} CPU / {d}GB disk", value=str(k))
            for k, (r, c, d) in PLANS.items() if show_all or k <= avail])
        self.sel.callback = self.cb
        self.add_item(self.sel)

    async def cb(self, i):
        if i.user.id != self.uid:
            return await i.response.send_message("Run the command yourself to deploy.", ephemeral=True)
        await i.response.defer(ephemeral=True)  # acknowledge within 3s first
        have = stats(self.uid)["avail"]
        if have < int(self.sel.values[0]):
            return await i.followup.send(
                f"❌ This plan needs **{self.sel.values[0]}** invites, you have **{have}**.", ephemeral=True)
        await i.followup.send("Choose an OS:", view=OSView(self.uid, int(self.sel.values[0])), ephemeral=True)


class ManageView(SafeView):
    def __init__(self, name, owner):
        super().__init__(timeout=300)
        self.name, self.owner = name, owner

    async def _ok(self, i):
        if i.user.id == self.owner or is_admin(i.user):
            return True
        await i.response.send_message("Not your VPS.", ephemeral=True)
        return False

    async def _act(self, i, action):
        if not await self._ok(i):
            return
        r = q("select node,status from vps where name=?", self.name)
        if not r or r[0][1] in ("banned", "creating"):
            return await i.response.send_message("Unavailable.", ephemeral=True)
        if r[0][1] == "suspended" and action != "stop":
            return await i.response.send_message("VPS expired — renew first.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        try:
            await call(r[0][0], "action", name=self.name, action=action)
            await i.followup.send(f"✅ {action} done. Details sent to your DMs.", ephemeral=True)
            await dm(self.owner, f"✅ `{self.name}` — **{action}** completed (by <@{i.user.id}>).")
            await log(f"🔧 `{self.name}` **{action}** by <@{i.user.id}> (owner <@{self.owner}>)")
        except Exception as e:
            await i.followup.send(f"❌ {e}", ephemeral=True)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success)
    async def b_start(self, i, b):
        await self._act(i, "start")

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger)
    async def b_stop(self, i, b):
        await self._act(i, "stop")

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.primary)
    async def b_restart(self, i, b):
        await self._act(i, "restart")

    @discord.ui.button(label="Reset SSH", style=discord.ButtonStyle.secondary)
    async def b_ssh(self, i, b):
        if not await self._ok(i):
            return
        r = q("select node,status from vps where name=?", self.name)
        if not r or r[0][1] != "running":
            return await i.response.send_message("VPS must be running.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        T = "🔄 SSH Access Regeneration Engine"
        OK = "SSH Regeneration Completed Successfully!"
        try:
            res, pmsg = await run_with_progress(call(r[0][0], "ssh", name=self.name, reset=True),
                                                i.channel, i.user, self.name, r[0][0], T, OK)
        except Exception as e:
            return await i.followup.send(f"❌ {e}", ephemeral=True)
        sent = True
        try:
            await (await i.client.fetch_user(self.owner)).send(f"🔄 `{self.name}` — new access:\n{creds_text(res)}")
        except discord.HTTPException:
            sent = False
        await finish_progress(pmsg, T, i.user, self.name, r[0][0], OK, dm_ok=sent)
        await log(f"🔑 SSH regenerated for `{self.name}` by <@{i.user.id}> (owner <@{self.owner}>)")
        await i.followup.send("✅ New SSH sent to your DMs." if sent else
                              f"⚠️ DMs closed. New access:\n{creds_text(res)}", ephemeral=True)

    @discord.ui.button(label="Renew", style=discord.ButtonStyle.success)
    async def b_renew(self, i, b):
        if not await self._ok(i):
            return
        await i.response.defer(ephemeral=True)
        await i.followup.send(await do_renew(self.owner, self.name, admin=False), ephemeral=True)


class MyVpsView(SafeView):
    def __init__(self, uid, names):
        super().__init__(timeout=300)
        self.uid = uid
        self.sel = discord.ui.Select(placeholder="Select a VPS to manage",
                                     options=[discord.SelectOption(label=n, value=n) for n in names[:25]])
        self.sel.callback = self.cb
        self.add_item(self.sel)

    async def cb(self, i):
        if i.user.id != self.uid:
            return await i.response.send_message("Not yours.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        await i.followup.send(f"Managing `{self.sel.values[0]}`",
                              view=ManageView(self.sel.values[0], self.uid), ephemeral=True)


def invite_embed(m):
    s = stats(m.id)
    since = int(time.time()) - 14 * 86400
    rj14 = q("select count(*) from joins where inviter_id=? and kind='rejoin' and ts>=?", m.id, since)[0][0]
    by = q("select inviter_id from joins where user_id=? order by id desc limit 1", m.id)
    e = discord.Embed(title="Server Invite Statistics", color=0x1ABC9C)
    e.description = (f"**{m.display_name}** Net Invites: **{s['valid']}**\n\n"
                     f"📥 **Joins:** {s['valid'] + s['left']}\n"
                     f"📤 **Left:** {s['left']}\n"
                     f"⚠️ **Fake:** {s['fake']}\n"
                     f"🔄 **Rejoins:** {s['rejoin']} (14d: {rj14})")
    e.add_field(name="🎟️ Invites", value=f"Used: **{s['used']}** · Available: **{s['avail']}**", inline=False)
    e.add_field(name="Invited By", value=f"<@{by[0][0]}>" if by and by[0][0] else "No invite recorded", inline=False)
    lines = [f"{'✅' if k <= s['avail'] else '🔒'} **{k} invites** → {r}GB RAM · {c} CPU · {d}GB disk"
             for k, (r, c, d) in PLANS.items()]
    e.add_field(name="VPS you can get (use /buy)", value="\n".join(lines), inline=False)
    e.set_thumbnail(url=m.display_avatar.url)
    e.set_footer(text=f"Requested by {m.display_name} · counts only if account is older than ~{MIN_AGE_DAYS // 30} months and not a rejoin")
    return e, s


def buy_embed(avail):
    e = discord.Embed(title="🛒 VPS Purchase Panel", color=0x3498DB,
                      description="Choose a plan from below. Each plan checks your invite score before deployment.")
    for n, (k, (r, c, d)) in enumerate(PLANS.items(), 1):
        e.add_field(name=f"Plan {n}: {r}GB RAM / {c} CPU / {d}GB", inline=False,
                    value=f"`{r}GB` • `{c} CPU` • `{d}GB disk` • `{k} invites required`")
    e.set_footer(text=f"🎟️ You have {avail} invites available")
    return e


# ------------------------------------------------------------------ user commands
@bot.command(name="i", aliases=["invites", "inv"])
@commands.guild_only()
async def prefix_invites(ctx, member: discord.Member = None):
    m = member or ctx.author
    e, s = invite_embed(m)
    view = PlanView(ctx.author.id, s["avail"]) if m == ctx.author and s["avail"] >= min(PLANS) else None
    await ctx.send(embed=e, view=view)


@bot.command(name="buy", aliases=["b"])
@commands.guild_only()
async def prefix_buy(ctx):
    avail = stats(ctx.author.id)["avail"]
    await ctx.send(embed=buy_embed(avail), view=PlanView(ctx.author.id, avail, show_all=True))


@bot.tree.command(name="buy", description="Open the VPS purchase panel (pay with invites)")
@app_commands.guild_only()
async def slash_buy(i: discord.Interaction):
    await i.response.defer()  # acknowledge within 3s first, then do the (database) work
    avail = stats(i.user.id)["avail"]
    await i.followup.send(embed=buy_embed(avail), view=PlanView(i.user.id, avail, show_all=True))


@bot.tree.command(name="invites", description="Show invite statistics")
@app_commands.guild_only()
async def slash_invites(i: discord.Interaction, member: discord.Member = None):
    await i.response.defer()
    m = member or i.user
    e, s = invite_embed(m)
    await i.followup.send(embed=e)


@bot.tree.command(name="invite", description="Check your invites and deploy a VPS")
@app_commands.guild_only()
async def slash_invite(i: discord.Interaction, member: discord.Member = None):
    await i.response.defer()
    m = member or i.user
    e, s = invite_embed(m)
    view = PlanView(i.user.id, s["avail"]) if m == i.user and s["avail"] >= min(PLANS) else None
    if view is None:
        await i.followup.send(embed=e)
    else:
        await i.followup.send(embed=e, view=view)


@bot.tree.command(name="myvps", description="List and manage your VPS")
async def myvps(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    rows = q("select name,node,invites,os,expires,status from vps where owner_id=? and status!='deleted'", i.user.id)
    if not rows:
        return await i.followup.send("You have no VPS. Use `/invite` or `-i`.", ephemeral=True)
    e = discord.Embed(title="🖥️ Your VPS", color=0x57F287)
    for n, node, inv, osk, exp, st in rows:
        r, c, d = PLANS[inv]
        e.add_field(name=n, inline=False,
                    value=f"{IMAGES[osk][0]} · {r}GB/{c}CPU/{d}GB · {node}\nStatus: **{st}** · expires <t:{exp}:R>")
    await i.followup.send(embed=e, view=MyVpsView(i.user.id, [r[0] for r in rows]), ephemeral=True)


async def vps_autocomplete(i: discord.Interaction, current: str):
    if is_admin(i.user):
        rows = q("select name,owner_id,status from vps where status!='deleted' order by expires limit 200")
    else:
        rows = q("select name,owner_id,status from vps where owner_id=? and status!='deleted'", i.user.id)
    out = []
    for n, o, st in rows:
        if current.lower() in n.lower() or current in str(o):
            out.append(app_commands.Choice(name=f"{n} - {st} - {o}"[:100], value=n))
    return out[:25]


@bot.tree.command(name="renew", description="Renew a VPS (costs invites equal to its plan; free for admins)")
@app_commands.autocomplete(vps_name=vps_autocomplete)
async def renew(i: discord.Interaction, vps_name: str = None):
    await i.response.defer(ephemeral=True)  # acknowledge within 3s first
    admin = is_admin(i.user)
    if vps_name:
        return await i.followup.send(await do_renew(i.user.id, vps_name.strip(), admin=admin), ephemeral=True)
    rows = q("select name,node,invites,status from vps where owner_id=? and status in ('running','suspended')", i.user.id)
    if not rows:
        return await i.followup.send("You have no VPS to renew.", ephemeral=True)
    have = stats(i.user.id)["avail"]
    await i.followup.send(f"🎟️ You have **{have}** invites. Select the VPS to renew:",
                          view=RenewView(i.user.id, rows), ephemeral=True)


async def node_autocomplete(i: discord.Interaction, current: str):
    out = []
    for name, mx, on in q("select name,max_vps,online from nodes"):
        c = q("select count(*) from vps where node=? and status!='deleted'", name)[0][0]
        if current.lower() in name.lower():
            out.append(app_commands.Choice(name=f"{name} - {'online' if on else 'OFFLINE'} - {c}/{mx} VPS"[:100],
                                           value=name))
    return out[:25]


@bot.tree.command(name="help", description="Show all commands")
async def help_cmd(i: discord.Interaction):
    e = discord.Embed(title="📖 Commands", color=0x5865F2)
    e.add_field(name="Users", inline=False, value=(
        "`/invites` (or `-i`) - invite statistics\n"
        "`/buy` (or `-buy`) - VPS purchase panel (pay with invites)\n"
        "`/invite` - check invites + deploy a VPS (or use `/buy`)\n"
        "`/myvps` - list & manage your VPS (Start/Stop/Restart/Reset SSH/Renew)\n"
        "`/renew` - pick a VPS & renew it (invites are cut automatically)\n"
        "`/vps-usage` - live CPU/RAM/disk of your VPS\n"
        "`/vps-reinstall` - wipe & reinstall your VPS OS"))
    if is_admin(i.user):
        e.add_field(name="Admin", inline=False, value=(
            "`/deploy` - deploy a free VPS for a user (you can pick the node)\n"
            "`/admin-renew` - renew / un-suspend any VPS (free, custom days)\n"
            "`/vps-suspend` - suspend (stop) a VPS now\n"
            "`/vps-unsuspend` - un-suspend a VPS\n"
            "`/vps-list` - list VPS of a user or all\n"
            "`/invites-give`, `/invites-remove` - adjust a user's invites\n"
            "`/health` - server & node health, `/fix` - auto-repair node1 now, `/diag` - why deploy is stuck\n"
            "`/reset-ssh <container id>` - new SSHX link sent to your DMs\n"
            "`/vps-expire`, `/vps-expire-all` - bulk expire\n"
            "`/vps-delete` - delete a VPS\n"
            "`/vps-stats` - stats & expiring soon\n"
            "`/node-domain` (set bot domain), `/node-install`, `/node-add`, `/node-list`, `/node-remove`, `/node-max` - manage nodes\n"
            "`/setup` - set invite/log channel\n"
            "`/admin-add`, `/admin-remove`, `/admin-list` - manage bot admins (owners only)\n"
            "`-sync` - re-sync slash commands"))
    await i.response.send_message(embed=e, ephemeral=True)


# ------------------------------------------------------------------ admin commands
PLAN_CH = [app_commands.Choice(name=f"{k} invites ({r}GB/{c}CPU/{d}GB)", value=k) for k, (r, c, d) in PLANS.items()]
OS_CH = [app_commands.Choice(name=v[0], value=k) for k, v in IMAGES.items()]


@bot.tree.command(name="deploy", description="Admin: deploy a free VPS for a user (pick plan, OS, node)")
@app_commands.guild_only()
@app_commands.choices(plan=PLAN_CH, os_name=OS_CH)
@app_commands.autocomplete(node=node_autocomplete)
async def deploy_cmd(i: discord.Interaction, user: discord.Member, plan: app_commands.Choice[int],
                       os_name: app_commands.Choice[str], node: str = None):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    await i.followup.send(await do_deploy(user, plan.value, os_name.value, free=True,
                                          node=(node or '').strip().lower() or None,
                                          notify=lambda t: i.edit_original_response(content=t),
                                          channel=i.channel), ephemeral=True)


@bot.tree.command(name="vps-delete", description="Admin: delete a VPS")
@app_commands.guild_only()
async def vps_delete(i: discord.Interaction, vps_name: str):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    r = q("select node from vps where name=?", vps_name)
    if not r:
        return await i.response.send_message("Not found.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    try:
        await call(r[0][0], "action", name=vps_name, action="delete")
    except Exception as e:
        return await i.followup.send(f"❌ {e}", ephemeral=True)
    q("delete from vps where name=?", vps_name)
    await i.followup.send("🗑️ Deleted.", ephemeral=True)


@bot.tree.command(name="setup", description="Admin: set the command channel (commands work only there) and the logs channel")
@app_commands.guild_only()
@app_commands.describe(command_channel="Channel where members can run the bot commands (admins can use them anywhere)",
                       log_channel="Channel where deploy / admin-command logs are posted")
async def setup_cmd(i: discord.Interaction, command_channel: discord.TextChannel = None,
                    log_channel: discord.TextChannel = None):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    if command_channel:
        put("invite_channel", command_channel.id)  # (stored under the old key so existing setups keep working)
    if log_channel:
        put("log_channel", log_channel.id)
    cc, lc = get("invite_channel"), get("log_channel")
    await i.response.send_message(
        "✅ Saved.\n"
        f"📌 Command channel: {f'<#{cc}>' if cc else 'not set (commands work everywhere)'}\n"
        f"📜 Logs channel: {f'<#{lc}>' if lc else 'not set'}", ephemeral=True)
    if log_channel:
        await log(f"📜 This is now the logs channel (set by <@{i.user.id}>).")


@bot.tree.command(name="vps-stats", description="Admin: how many VPS are running, stopped, expired and expiring soon")
@app_commands.guild_only()
async def vps_stats(i: discord.Interaction, days: app_commands.Range[int, 1, 30] = 3):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    now = int(time.time())
    cnt = {st: n for st, n in q("select status,count(*) from vps where status!='deleted' group by status")}
    total = sum(cnt.values())
    soon = q("select name,owner_id,expires from vps where status='running' and expires between ? and ? order by expires",
             now, now + days * 86400)
    try:
        live = await call("node1", "status")
        live_txt = f"{live['running']} running / {live['count']} total containers"
    except Exception as e:
        live_txt = f"unavailable ({e})"
    e = discord.Embed(title="📊 VPS Stats", color=0x5865F2)
    e.add_field(name="🖥️ Total", value=total)
    e.add_field(name="🟢 Active (not expired)", value=cnt.get("running", 0))
    e.add_field(name="⏰ Expired / suspended", value=cnt.get("suspended", 0))
    e.add_field(name="🚫 Banned (mining)", value=cnt.get("banned", 0))
    e.add_field(name="⚠️ Expiring in {} day(s)".format(days), value=len(soon))
    e.add_field(name="🔌 Live on node1", value=live_txt, inline=False)
    if soon:
        lines = [f"`{n}` — <@{o}> — <t:{x}:R>" for n, o, x in soon[:15]]
        if len(soon) > 15:
            lines.append(f"…and {len(soon) - 15} more")
        e.add_field(name="Expiring soon", value="\n".join(lines), inline=False)
    await i.followup.send(embed=e, ephemeral=True)


async def expire_vps(name):
    r = q("select owner_id,node,status from vps where name=?", name)
    if not r:
        return False
    owner, node, status = r[0]
    if status not in ("running", "suspended"):
        return False
    try:
        await call(node, "action", name=name, action="stop")
    except Exception:
        pass
    q("update vps set expires=?, status='suspended' where name=?", int(time.time()) - 1, name)
    await dm(owner, f"⏰ VPS `{name}` was expired by an admin and stopped. "
                    f"Renew within {GRACE_DAYS} days via `/myvps` or `/renew`.")
    return True


@bot.tree.command(name="vps-expire", description="Admin: expire VPS now (by names, by user, or all expired-soon)")
@app_commands.guild_only()
async def vps_expire(i: discord.Interaction, vps_names: str = None, user: discord.Member = None):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    names = []
    if vps_names:
        names += [n.strip() for n in re.split(r"[,\s]+", vps_names) if n.strip()]
    if user:
        names += [r[0] for r in q("select name from vps where owner_id=? and status='running'", user.id)]
    names = list(dict.fromkeys(names))
    if not names:
        return await i.response.send_message("Give `vps_names` (comma separated) or a `user`.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    done = [n for n in names if await expire_vps(n)]
    failed = [n for n in names if n not in done]
    msg = f"✅ Expired {len(done)} VPS: " + (", ".join(f"`{n}`" for n in done) or "none")
    if failed:
        msg += "\n❌ Skipped (not found / already deleted / banned): " + ", ".join(f"`{n}`" for n in failed)
    await i.followup.send(msg[:1900], ephemeral=True)


@bot.tree.command(name="admin-renew", description="Admin: renew / un-suspend any VPS for free")
@app_commands.guild_only()
@app_commands.autocomplete(vps_name=vps_autocomplete)
async def admin_renew(i: discord.Interaction, vps_name: str, days: app_commands.Range[int, 1, 365] = None):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    msg = await do_renew(i.user.id, vps_name.strip(), admin=True, days=days)
    r = q("select owner_id from vps where name=?", vps_name.strip())
    if r and msg.startswith("✅"):
        await dm(r[0][0], f"✅ Your VPS `{vps_name.strip()}` was renewed by an admin. {msg}")
    await i.followup.send(msg, ephemeral=True)


@bot.tree.command(name="vps-suspend", description="Admin: suspend (stop) a VPS right now")
@app_commands.guild_only()
@app_commands.autocomplete(vps_name=vps_autocomplete)
async def vps_suspend(i: discord.Interaction, vps_name: str):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    ok = await expire_vps(vps_name.strip())
    await i.followup.send(f"⏸️ `{vps_name}` suspended." if ok else "❌ Not found / already deleted / banned.",
                          ephemeral=True)


@bot.tree.command(name="vps-unsuspend", description="Admin: un-suspend a VPS (start it and give it more days)")
@app_commands.guild_only()
@app_commands.autocomplete(vps_name=vps_autocomplete)
async def vps_unsuspend(i: discord.Interaction, vps_name: str,
                        days: app_commands.Range[int, 1, 365] = None):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    name = vps_name.strip()
    r = q("select owner_id,status from vps where name=?", name)
    if not r or r[0][1] != "suspended":
        return await i.followup.send("❌ Not found or not suspended.", ephemeral=True)
    msg = await do_renew(i.user.id, name, admin=True, days=days)
    if msg.startswith("✅"):
        await dm(r[0][0], f"✅ Your VPS `{name}` was un-suspended by an admin. {msg}")
    await i.followup.send(msg, ephemeral=True)


@bot.tree.command(name="vps-list", description="Admin: list VPS (of a user, or all)")
@app_commands.guild_only()
async def vps_list(i: discord.Interaction, user: discord.Member = None):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    if user:
        rows = q("select name,owner_id,node,expires,status from vps where owner_id=? and status!='deleted' order by expires", user.id)
    else:
        rows = q("select name,owner_id,node,expires,status from vps where status!='deleted' order by expires")
    if not rows:
        return await i.response.send_message("No VPS found.", ephemeral=True)
    lines = [f"`{n}` - <@{o}> - {st} - {node} - <t:{x}:R>" for n, o, node, x, st in rows]
    text = "\n".join(lines[:40]) + (f"\n...and {len(lines) - 40} more" if len(lines) > 40 else "")
    await i.response.send_message(text[:1950], ephemeral=True)


@bot.command(name="sync")
@commands.guild_only()
async def prefix_sync(ctx):
    if not is_admin(ctx.author):
        return
    n = await sync_guild(ctx.guild)
    await ctx.send(f"✅ Synced {n} slash commands. Press Ctrl+R in Discord." if n >= 0 else
                   "❌ Sync failed - re-invite the bot with the `applications.commands` scope (see console).")


@bot.tree.command(name="admin-add", description="Owner: give a user bot-admin access")
@app_commands.guild_only()
async def admin_add(i: discord.Interaction, user: discord.Member):
    if not is_owner(i.user):
        return await i.response.send_message("Only owners (ADMIN_IDS in .env) can add admins.", ephemeral=True)
    if user.bot:
        return await i.response.send_message("Can't add a bot.", ephemeral=True)
    q("insert or ignore into admins values(?)", user.id)
    await i.response.send_message(f"✅ {user.mention} is now a bot admin.", ephemeral=True)


@bot.tree.command(name="admin-remove", description="Owner: remove a user's bot-admin access")
@app_commands.guild_only()
async def admin_remove(i: discord.Interaction, user: discord.Member):
    if not is_owner(i.user):
        return await i.response.send_message("Only owners (ADMIN_IDS in .env) can remove admins.", ephemeral=True)
    q("delete from admins where user_id=?", user.id)
    await i.response.send_message(f"✅ {user.mention} is no longer a bot admin.", ephemeral=True)


@bot.tree.command(name="admin-list", description="Owner: list bot admins")
@app_commands.guild_only()
async def admin_list(i: discord.Interaction):
    if not is_owner(i.user):
        return await i.response.send_message("Owners only.", ephemeral=True)
    owners = ", ".join(f"<@{x}>" for x in OWNER_IDS) or "none (set ADMIN_IDS in .env)"
    added = ", ".join(f"<@{r[0]}>" for r in q("select user_id from admins")) or "none"
    await i.response.send_message(f"👑 **Owners (.env):** {owners}\n🛡️ **Added admins:** {added}", ephemeral=True)


NODE_RE = re.compile(r"[a-z0-9-]{2,20}")


def public_url():
    """Domain set with /node-domain (saved in the database) wins over PUBLIC_URL from .env."""
    return (get("public_url") or PUBLIC_URL or "").rstrip("/")


def node_cmd(tok):
    url = public_url() or "<your bot's Cloudflare URL>"
    return f"bash install_node.sh {url} {tok}"


def node_connected(name):
    link = NODE_LINKS.get(name)
    return bool(link and not link["ws"].closed)


def node_setup_text(name, tok, max_vps, note=""):
    return (f"**Node `{name}`** (max {max_vps} VPS)\n"
            f"1) On the new server, as root, inside the `node/` folder run:\n{BT}\n{node_cmd(tok)}{BT}"
            + ("" if public_url() else "⚠️ Domain not set yet - run `/node-domain` first (or replace the URL above with your Cloudflare address).\n")
            + f"2) Then press **🔌 Connect the node** below. If it is not online yet, wait a bit and press it again "
              f"- this message stays active for 2 hours.\n"
            + (f"\n{note}" if note else ""))


class NodeConnectView(SafeView):
    def __init__(self, name, tok, max_vps):
        super().__init__(timeout=7200)  # 2 hours: press the button as often as you like
        self.name, self.tok, self.max_vps = name, tok, max_vps

    @discord.ui.button(label="Connect the node", emoji="🔌", style=discord.ButtonStyle.success)
    async def connect(self, i: discord.Interaction, button: discord.ui.Button):
        if not is_admin(i.user):
            return await i.response.send_message("Admins only.", ephemeral=True)
        if not q("select 1 from nodes where name=?", self.name):
            return await i.response.edit_message(content=f"Node `{self.name}` no longer exists.", view=None)
        await i.response.defer()
        for _ in range(8):  # give a freshly started node up to ~16s to dial in
            if node_connected(self.name):
                break
            await asyncio.sleep(2)
        if node_connected(self.name):
            try:
                st = await call(self.name, "status")
                info = f"{st['count']} containers"
            except Exception as e:
                info = f"connected, but status failed: {str(e)[:100]}"
            q("update nodes set online=1 where name=?", self.name)
            await i.edit_original_response(
                content=f"✅ Node `{self.name}` is **online** · {info} · max {self.max_vps} VPS.\n"
                        f"It is ready - you can now deploy on it.", view=None)
            self.stop()
            return
        now = dt.datetime.now().strftime("%H:%M:%S")
        await i.edit_original_response(content=node_setup_text(
            self.name, self.tok, self.max_vps,
            note=f"🔴 Not connected yet (checked {now}). It can take a few minutes - run step 1 if you have not, "
                 f"then press **Connect the node** again."), view=self)


DOMAIN_RE = re.compile(r"^(?:https?://)?(localhost|[a-z0-9.-]+\.[a-z]{2,}|\d{1,3}(?:\.\d{1,3}){3})(:\d{1,5})?/?$", re.I)


@bot.tree.command(name="node-domain", description="Admin: set the bot's domain (Cloudflare) that nodes connect to")
@app_commands.guild_only()
async def node_domain(i: discord.Interaction, domain: str = None):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    if domain:
        d = domain.strip()
        m = DOMAIN_RE.fullmatch(d)
        if not m:
            return await i.response.send_message(
                "❌ Invalid domain. Example: `bot.yourdomain.com` (or `localhost:8765` for a same-machine test).",
                ephemeral=True)
        host = m.group(1).lower()
        if d.lower().startswith(("http://", "https://")):
            url = d.rstrip("/")
        else:  # real domains are served by Cloudflare over https; localhost/IP are plain
            plain = host == "localhost" or host[0].isdigit()
            url = ("http://" if plain else "https://") + d.rstrip("/")
        put("public_url", url)
    url = public_url()
    text = (f"🌐 **Bot domain:** `{url or 'not set'}`\n"
            + ("✅ Saved - `/node-add` will now use it in the install command.\n\n" if domain else
               "Change it with `/node-domain domain:bot.yourdomain.com`\n\n")
            + f"**How to connect the domain (on the bot server):**\n"
              f"Option A - quick test, no domain needed:{BT}\ncloudflared tunnel --url http://localhost:{PANEL_PORT}{BT}"
              f"(prints a `https://xxxx.trycloudflare.com` address - give that to `/node-domain`)\n"
              f"Option B - your own domain: Cloudflare Zero Trust -> Networks -> Tunnels -> your tunnel -> "
              f"Public Hostname -> add `bot.yourdomain.com`, Service type **HTTP**, URL **localhost:{PANEL_PORT}**. "
              f"Or in the terminal:{BT}\ncloudflared tunnel route dns <tunnel> bot.yourdomain.com\n"
              f"cloudflared tunnel run --url http://localhost:{PANEL_PORT} <tunnel>{BT}"
              f"Then `/node-add name:node2` -> run the command on the node -> press 🔌 Connect the node.")
    await i.response.send_message(text, ephemeral=True)


@bot.tree.command(name="node-install", description="Admin: how to connect a new node through Cloudflare")
@app_commands.guild_only()
async def node_install(i: discord.Interaction):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    text = (f"**New node setup** - the node connects OUT to the bot, so it needs no IP and no port.\n"
            f"1) Bot server: point your Cloudflare Tunnel to `http://localhost:{PANEL_PORT}` "
            f"and set the public address with `/node-domain` (current: `{public_url() or 'not set'}`)\n"
            f"2) Discord: `/node-domain domain:bot.yourdomain.com` (saves the domain), then `/node-add name:node2` -> gives you a token, the install command and a 🔌 Connect button\n"
            f"3) New server (root): copy the `node/` folder, run the command from step 2\n"
            f"4) The node turns 🟢 online by itself (`/node-list`).\n"
            f"Same-machine test: use `localhost:{PANEL_PORT}` as the URL.\n"
            f"VPS on every node give SSHX only - root of the VPS container, never the node itself.")
    await i.response.send_message(text, ephemeral=True)


@bot.tree.command(name="node-add", description="Admin: add a remote node (gives a token; the node connects by itself)")
@app_commands.guild_only()
async def node_add(i: discord.Interaction, name: str, max_vps: app_commands.Range[int, 1, 500] = 25):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    name = name.strip().lower()
    if not NODE_RE.fullmatch(name):
        return await i.response.send_message("Name: 2-20 chars, only a-z, 0-9 and `-`.", ephemeral=True)
    r = q("select token,max_vps,is_local from nodes where name=?", name)
    if r and (r[0][2] or not r[0][0]):
        return await i.response.send_message("A node with this name already exists. Use `/node-remove` first.",
                                             ephemeral=True)
    if r:  # remote node already created: show the same token + button again
        tok, max_vps = r[0][0], r[0][1]
    else:
        tok = secrets.token_urlsafe(24)
        q("insert into nodes(name,token,max_vps,is_local,online) values(?,?,?,0,0)", name, tok, max_vps)
    await i.response.send_message(node_setup_text(name, tok, max_vps), view=NodeConnectView(name, tok, max_vps),
                                  ephemeral=True)


@bot.tree.command(name="node-list", description="Admin: list all nodes")
@app_commands.guild_only()
async def node_list(i: discord.Interaction):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    e = discord.Embed(title="🖧 Nodes", color=0x5865F2)
    for name, mx, loc, on in q("select name,max_vps,is_local,online from nodes"):
        c = q("select count(*) from vps where node=? and status!='deleted'", name)[0][0]
        where = "local" if loc else "via tunnel"
        e.add_field(name=f"{'🟢' if on else '🔴'} {name}", inline=False,
                    value=f"{where} · VPS {c}/{mx} · {'online' if on else 'offline'}")
    await i.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="node-remove", description="Admin: remove a remote node (must have no VPS)")
@app_commands.guild_only()
@app_commands.autocomplete(name=node_autocomplete)
async def node_remove(i: discord.Interaction, name: str):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    name = name.strip().lower()
    r = q("select is_local from nodes where name=?", name)
    if not r:
        return await i.response.send_message("Node not found.", ephemeral=True)
    if r[0][0] or name == "node1":
        return await i.response.send_message("The local node1 cannot be removed.", ephemeral=True)
    c = q("select count(*) from vps where node=? and status!='deleted'", name)[0][0]
    if c:
        return await i.response.send_message(f"❌ Node still has {c} VPS. Delete or move them first.", ephemeral=True)
    q("delete from nodes where name=?", name)
    link = NODE_LINKS.get(name)
    if link:
        await link["ws"].close()
    await i.response.send_message(f"🗑️ Node `{name}` removed.", ephemeral=True)


@bot.tree.command(name="node-max", description="Admin: change the max VPS count of a node")
@app_commands.guild_only()
@app_commands.autocomplete(name=node_autocomplete)
async def node_max(i: discord.Interaction, name: str, max_vps: app_commands.Range[int, 1, 500]):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    name = name.strip().lower()
    if not q("select 1 from nodes where name=?", name):
        return await i.response.send_message("Node not found.", ephemeral=True)
    q("update nodes set max_vps=? where name=?", max_vps, name)
    await i.response.send_message(f"✅ `{name}` max VPS set to {max_vps}.", ephemeral=True)


@bot.tree.command(name="vps-expire-all", description="Admin: expire ALL running VPS at once")
@app_commands.guild_only()
async def vps_expire_all(i: discord.Interaction, confirm: bool = False):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    if not confirm:
        n = q("select count(*) from vps where status='running'")[0][0]
        return await i.response.send_message(
            f"⚠️ This will expire **{n}** running VPS. Run again with `confirm: True`.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    names = [r[0] for r in q("select name from vps where status='running'")]
    done = 0
    for n in names:
        done += await expire_vps(n)
    await i.followup.send(f"✅ Expired {done} VPS.", ephemeral=True)



# ------------------------------------------------------------------ self-heal on command errors
@bot.tree.error
async def on_app_error(i: discord.Interaction, err):
    if isinstance(err, app_commands.CheckFailure):
        return
    orig = getattr(err, "original", err)
    print("command error:", repr(orig))

    async def say(t):
        try:
            if i.response.is_done():
                await i.followup.send(t, ephemeral=True)
            else:
                await i.response.send_message(t, ephemeral=True)
        except Exception:
            pass
    await say("⚠️ Something went wrong. **I'm fixing it automatically - please wait ~2 minutes, then run the command again.**")
    await log(f"⚠️ `/{i.command.name if i.command else '?'}` by <@{i.user.id}> failed: `{str(orig)[:300]}` - auto-fixing")
    ok = await heal_local()
    await say("✅ Fixed! Please run the command again." if ok else
              "❌ Could not fix it automatically. Admins have been notified.")
    await log("✅ Auto-fix finished." if ok else "❌ Auto-fix failed - check the server.")


# ------------------------------------------------------------------ extra commands
def fmt_bytes(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024


def bar(used, total, w=12):
    f = int(w * min(used / total, 1)) if total else 0
    return "█" * f + "░" * (w - f)


@bot.tree.command(name="vps-usage", description="Live CPU / RAM / disk usage of a VPS")
@app_commands.autocomplete(vps_name=vps_autocomplete)
async def vps_usage(i: discord.Interaction, vps_name: str):
    r = q("select owner_id,node,invites,status,expires from vps where name=?", vps_name.strip())
    if not r or (r[0][0] != i.user.id and not is_admin(i.user)):
        return await i.response.send_message("❌ VPS not found.", ephemeral=True)
    owner, node, inv, status, exp = r[0]
    if status != "running":
        return await i.response.send_message(f"VPS is **{status}**.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    await log(f"📈 <@{i.user.id}> checked usage of `{vps_name.strip()}`")
    u = await call(node, "usage", name=vps_name.strip())
    ram, cpu, disk = PLANS[inv]
    e = discord.Embed(title=f"📈 {vps_name.strip()}", color=0x57F287)
    e.add_field(name="State", value=u["status"])
    e.add_field(name="Processes", value=u["procs"])
    e.add_field(name="CPU time", value=f"{u['cpu_s'] / 60:.1f} min")
    e.add_field(name=f"RAM {fmt_bytes(u['mem'])} / {ram} GB", inline=False,
                value=f"`{bar(u['mem'], ram * 1024**3)}`")
    e.add_field(name=f"Disk {fmt_bytes(u['disk'])} / {disk} GB", inline=False,
                value=f"`{bar(u['disk'], disk * 1024**3)}`")
    e.add_field(name="Expires", value=f"<t:{exp}:R>")
    if u["ips"]:
        e.add_field(name="Internal IP", value=u["ips"][0])
    await i.followup.send(embed=e, ephemeral=True)


class ReinstallView(SafeView):
    def __init__(self, uid, name):
        super().__init__(timeout=120)
        self.uid, self.name = uid, name
        self._busy = False

    @discord.ui.button(label="Yes, wipe & reinstall", style=discord.ButtonStyle.danger)
    async def go(self, i, b):
        if i.user.id != self.uid:
            return await i.response.send_message("Not yours.", ephemeral=True)
        if self._busy:
            return await i.response.send_message("⏳ Already reinstalling - please wait.", ephemeral=True)
        self._busy = True
        self.stop()
        await i.response.edit_message(content="⏳ Reinstalling... 1-3 minutes.", view=None)
        r = q("select node,invites,os from vps where name=?", self.name)
        if not r:
            return await i.edit_original_response(content="❌ VPS not found.")
        node, inv, osk = r[0]
        ram, cpu, disk = PLANS[inv]
        notify = lambda t: i.edit_original_response(content=t)
        T = "♻️ VPS Reinstall Engine"
        OK = "VPS Reinstall Completed Successfully!"

        async def work():
            await call(node, "action", _notify=notify, name=self.name, action="delete")
            return await call(node, "create", _notify=notify, name=self.name, os_key=osk, cpu=cpu, ram=ram, disk=disk)
        try:
            res, pmsg = await run_with_progress(work(), i.channel, i.user, self.name, node, T, OK)
        except Exception as e:
            return await i.edit_original_response(content=f"❌ Reinstall failed: {e}")
        q("update vps set status='running' where name=?", self.name)
        await i.edit_original_response(content=f"✅ Reinstalled `{self.name}`. New access sent to your DMs.")
        await dm(self.uid, f"🔄 `{self.name}` reinstalled. New access:\n{creds_text(res)}")
        await finish_progress(pmsg, T, i.user, self.name, node, OK)
        await log(f"♻️ `{self.name}` reinstalled by <@{i.user.id}>")


@bot.tree.command(name="vps-reinstall", description="Wipe your VPS and reinstall its OS (same expiry, no invites used)")
@app_commands.autocomplete(vps_name=vps_autocomplete)
async def vps_reinstall(i: discord.Interaction, vps_name: str):
    r = q("select owner_id,status from vps where name=?", vps_name.strip())
    if not r or (r[0][0] != i.user.id and not is_admin(i.user)):
        return await i.response.send_message("❌ VPS not found.", ephemeral=True)
    if r[0][1] != "running":
        return await i.response.send_message("VPS must be running (renew it first if expired).", ephemeral=True)
    await i.response.send_message(f"⚠️ This **deletes everything** inside `{vps_name.strip()}`. Continue?",
                                  view=ReinstallView(r[0][0], vps_name.strip()), ephemeral=True)


@bot.tree.command(name="invites-give", description="Admin: give bonus invites to a user")
@app_commands.guild_only()
async def invites_give(i: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 1000]):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    now = int(time.time())
    for k in range(amount):
        q("insert into joins(user_id,inviter_id,kind,ts) values(?,?,?,?)",
          -secrets.randbelow(10**12) - 1, user.id, "valid", now)
    await i.response.send_message(f"✅ Gave **{amount}** invites to {user.mention}. "
                                  f"They now have {stats(user.id)['avail']} available.", ephemeral=True)


@bot.tree.command(name="invites-remove", description="Admin: take invites away from a user")
@app_commands.guild_only()
async def invites_remove(i: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 1000]):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    spend(user.id, amount)
    await i.response.send_message(f"✅ Removed **{amount}** invites from {user.mention}. "
                                  f"They now have {stats(user.id)['avail']} available.", ephemeral=True)


@bot.tree.command(name="health", description="Admin: server health, nodes and LXD status")
@app_commands.guild_only()
async def health(i: discord.Interaction):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    import shutil as _sh
    du = _sh.disk_usage("/")
    try:
        ram_total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        with open("/proc/meminfo") as f:
            ram_av = int(next(l for l in f if l.startswith("MemAvailable")).split()[1]) * 1024
    except Exception:
        ram_total = ram_av = 0
    try:
        load = os.getloadavg()[0]
    except Exception:
        load = 0
    rc, ver = await nodeapi.sh("lxc", "version")
    e = discord.Embed(title="🩺 Health", color=0x5865F2)
    e.add_field(name="Bot uptime", value=f"{int((time.time() - START_TS) // 3600)}h {int((time.time() - START_TS) % 3600 // 60)}m")
    e.add_field(name="CPU", value=f"{os.cpu_count()} cores · load {load:.2f}")
    if ram_total:
        e.add_field(name="RAM", value=f"{fmt_bytes(ram_total - ram_av)} / {fmt_bytes(ram_total)}")
    e.add_field(name="Disk /", value=f"{fmt_bytes(du.used)} / {fmt_bytes(du.total)}")
    e.add_field(name="LXD", value="✅ " + ver.replace("\n", " · ")[:80] if rc == 0 else f"❌ {ver[:80]}", inline=False)
    for name, mx, on in q("select name,max_vps,online from nodes"):
        c = q("select count(*) from vps where node=? and status!='deleted'", name)[0][0]
        e.add_field(name=f"{'🟢' if on else '🔴'} {name}", value=f"{c}/{mx} VPS", inline=True)
    cnt = {st: n for st, n in q("select status,count(*) from vps where status!='deleted' group by status")}
    e.add_field(name="VPS", inline=False,
                value=" · ".join(f"{k}: {v}" for k, v in cnt.items()) or "none")
    await i.followup.send(embed=e, ephemeral=True)


@bot.tree.command(name="reset-ssh", description="Regenerate SSHX link of a VPS by its container ID - sent to your DMs")
@app_commands.describe(container_id="Container ID, e.g. vps-86057435")
@app_commands.guild_only()
async def reset_ssh_cmd(i: discord.Interaction, container_id: str):
    name = container_id.strip()
    r = q("select owner_id,node,status from vps where name=?", name)
    if not r:
        return await i.response.send_message("❌ Container ID not found.", ephemeral=True)
    owner, node, status = r[0]
    if owner != i.user.id and not is_admin(i.user):
        return await i.response.send_message("❌ Not your VPS.", ephemeral=True)
    if status != "running":
        return await i.response.send_message(f"❌ VPS must be running (now: {status}).", ephemeral=True)
    await i.response.defer(ephemeral=True)
    T = "🔄 SSH Access Regeneration Engine"
    OK = "SSH Regeneration Completed Successfully!"
    try:
        res, pmsg = await run_with_progress(call(node, "ssh", name=name, reset=True),
                                            i.channel, i.user, name, node, T, OK)
    except Exception as e:
        return await i.followup.send(f"❌ {e}", ephemeral=True)
    sent = True
    try:
        await (await i.client.fetch_user(owner)).send(f"🔄 `{name}` — new access:\n{creds_text(res)}")
    except discord.HTTPException:
        sent = False
    await finish_progress(pmsg, T, i.user, name, node, OK, dm_ok=sent)
    await log(f"🔑 SSH regenerated for `{name}` by <@{i.user.id}> (owner <@{owner}>)")
    await i.followup.send("✅ New SSH sent to your DMs." if sent else
                          f"⚠️ DMs closed. New access:\n{creds_text(res)}", ephemeral=True)


@reset_ssh_cmd.autocomplete("container_id")
async def reset_ssh_ac(i: discord.Interaction, cur: str):
    rows = q("select name from vps where status='running'" + ("" if is_admin(i.user) else " and owner_id=?"),
             *(() if is_admin(i.user) else (i.user.id,)))
    return [app_commands.Choice(name=n, value=n) for n, in rows if cur.lower() in n.lower()][:25]


@bot.tree.command(name="diag", description="Admin: why is deploy stuck? shows LXD operations, disk and image-server reachability")
@app_commands.guild_only()
async def diag_cmd(i: discord.Interaction):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    txt = await nodeapi.diagnose()
    await i.followup.send(("```\n" + txt.replace(" | ", "\n") + "\n```")[:1900], ephemeral=True)


@bot.tree.command(name="fix", description="Admin: run the automatic repair for node1 (LXD restart + checks) now")
@app_commands.guild_only()
async def fix_cmd(i: discord.Interaction):
    if not is_admin(i.user):
        return await i.response.send_message("Admins only.", ephemeral=True)
    await i.response.send_message("🔧 Fixing node1... wait ~2 minutes.", ephemeral=True)
    ok = await heal_local()
    await i.edit_original_response(content="✅ node1 is healthy." if ok else "❌ Still broken - check the server logs.")


# ------------------------------------------------------------------ background loops
# ------------------------------------------------------------------ rotating "Watching" status
def created_total():
    """How many VPS were ever created (kept in settings because deleted VPS rows are removed)."""
    cur = get("created_total")
    finished = q("select count(*) from vps where status!='creating'")[0][0]
    return max(int(cur) if cur and cur.isdigit() else 0, finished)


def bump_created():
    """Called right after a VPS became 'running' (its row already exists)."""
    cur = get("created_total")
    if cur and cur.isdigit():
        put("created_total", int(cur) + 1)
    else:  # first time: start from the VPS that already exist (this new one is among them)
        put("created_total", q("select count(*) from vps where status!='creating'")[0][0])


def presence_lines():
    running = q("select count(*) from vps where status='running'")[0][0]
    return [f"🖥️ {running} VPS running",
            f"📦 {created_total()} VPS created",
            f"👑 Created by {CREATOR}",
            "☁️ Legacy Cloud V1"]


PRESENCE_IDX = [0]


@tasks.loop(seconds=30)
async def presence_loop():
    lines = presence_lines()
    text = lines[PRESENCE_IDX[0] % len(lines)]
    PRESENCE_IDX[0] += 1
    try:
        await bot.change_presence(status=discord.Status.online,
                                  activity=discord.Activity(type=discord.ActivityType.watching, name=text))
    except Exception as e:
        print("presence update failed:", e)


@presence_loop.before_loop
async def _presence_wait():
    await bot.wait_until_ready()


@tasks.loop(minutes=5)
async def housekeeping():
    now = int(time.time())
    for name, owner, node, exp, status in q(
            "select name,owner_id,node,expires,status from vps where status in ('running','suspended')"):
        if status == "running" and exp < now:
            try:
                await call(node, "action", name=name, action="stop")
            except Exception:
                pass
            q("update vps set status='suspended' where name=?", name)
            await dm(owner, f"⏰ VPS `{name}` expired and was stopped. Renew within {GRACE_DAYS} days via `/myvps` or `/renew`.")
        elif status == "suspended" and exp + GRACE_DAYS * 86400 < now:
            try:
                await call(node, "action", name=name, action="delete")
            except Exception:
                continue
            q("delete from vps where name=?", name)
            await dm(owner, f"🗑️ VPS `{name}` was deleted (not renewed).")


@tasks.loop(seconds=60)
async def poll_nodes():
    for name, in q("select name from nodes where is_local=1 or token is not null"):
        try:
            await call(name, "status")
            q("update nodes set online=1 where name=?", name)
            for ev in (await call(name, "events"))["events"]:
                r = q("select owner_id from vps where name=?", ev["name"])
                q("update vps set status='banned' where name=?", ev["name"])
                if r:
                    await dm(r[0][0], f"🚨 Your VPS `{ev['name']}` was stopped: crypto-mining activity detected.")
                await log(f"🚨 Mining detected in `{ev['name']}` on {name} (`{ev['match']}`) — container stopped.")
        except Exception:
            q("update nodes set online=0 where name=?", name)


@housekeeping.before_loop
@poll_nodes.before_loop
async def _wait():
    await bot.wait_until_ready()


if __name__ == "__main__":
    bot.run(TOKEN)
