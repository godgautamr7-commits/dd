#!/usr/bin/env python3
"""Startup checklist for the bot. Uses only the standard library so it can run before anything else.
Every requirement gets a spinner while it is checked/installed, then a green tick (or a red cross)."""
import asyncio
import importlib
import itertools
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

TTY = sys.stdout.isatty()
G, R, Y, D, X = ("\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[0m") if TTY else ("",) * 5
FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Spinner:
    def __init__(self, label):
        self.label, self.stop_ev = label, threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        for f in itertools.cycle(FRAMES):
            if self.stop_ev.is_set():
                return
            sys.stdout.write(f"\r{Y}{f}{X} {self.label} ...")
            sys.stdout.flush()
            time.sleep(0.08)

    def __enter__(self):
        if TTY:
            self.t.start()
        else:
            print(f"... {self.label}", flush=True)
        return self

    def __exit__(self, *a):
        self.stop_ev.set()
        if TTY:
            self.t.join()
            sys.stdout.write("\r" + " " * (len(self.label) + 12) + "\r")


def step(label, fn, fatal=True, repair=None):
    """Run fn() under a spinner. If it fails and a repair command is given, run the repair and try again.
    fn returns an optional detail string, or raises on failure."""
    detail, err, fixed = "", None, False
    with Spinner(label):
        time.sleep(0.3)  # so the spinner is visible even for instant checks
        for attempt in (1, 2):
            try:
                detail, err = fn() or "", None
                break
            except Exception as e:
                err = e
                if attempt == 1 and repair:
                    fixed = True
                    for cmd in ([repair] if isinstance(repair, str) else repair):
                        try:
                            run(cmd, timeout=900)
                        except Exception:
                            pass
                    _lxd_done["v"] = False
                else:
                    break
    if err is None:
        extra = "fixed automatically" if fixed else ""
        d = ", ".join(x for x in (detail, extra) if x)
        print(f"{G}✔{X} {label}" + (f" {D}({d}){X}" if d else ""), flush=True)
        return True
    print(f"{R}✘{X} {label} {D}— {err}{X}", flush=True)
    if fatal:
        print(f"\n{R}Cannot continue. Fix the problem above and run the bot again.{X}")
        sys.exit(1)
    return False


def run(cmd, timeout=1500):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


# ---------------------------------------------------------------- individual checks
def chk_python():
    if sys.version_info < (3, 9):
        raise RuntimeError("Python 3.9+ needed")
    return sys.version.split()[0]


def chk_root():
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise RuntimeError("run as root (sudo python3 bot.py) - LXD needs it")
    return "root"


def pkg(module, pip_name):
    def f():
        try:
            importlib.import_module(module)
            return "installed"
        except ImportError:
            pass
        for extra in ("", " --break-system-packages"):
            rc, out = run(f'"{sys.executable}" -m pip install -q {pip_name}{extra}')
            if rc == 0:
                break
        else:
            raise RuntimeError(f"pip install {pip_name} failed: {out[-150:]}")
        importlib.invalidate_caches()
        importlib.import_module(module)
        return "auto-installed"
    return f


def chk_pip():
    if run(f'"{sys.executable}" -m pip --version')[0] != 0:
        raise RuntimeError("pip missing")
    return "ok"


def chk_env():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    if not os.getenv("DISCORD_TOKEN") or "PASTE" in os.getenv("DISCORD_TOKEN", ""):
        raise RuntimeError("DISCORD_TOKEN missing in .env")
    ids = os.getenv("ADMIN_IDS", "")
    if not any(x.strip().isdigit() for x in ids.replace(",", " ").split()):
        raise RuntimeError("ADMIN_IDS not set in .env (put your Discord user ID)")
    return "token + admin ids ok"


_lxd_done = {"v": False}


def _ensure_lxd_once():
    if not _lxd_done["v"]:
        import agent
        note = asyncio.run(agent.ensure_lxd())
        _lxd_done["v"] = True
        return note
    return ""


def chk_iptables():
    if shutil.which("iptables"):
        return "installed"
    _ensure_lxd_once()
    if not shutil.which("iptables"):
        raise RuntimeError("could not install iptables")
    return "auto-installed"


def chk_snapd():
    if shutil.which("lxc") or shutil.which("snap"):
        return "ok"
    _ensure_lxd_once()
    if not shutil.which("snap") and not shutil.which("lxc"):
        raise RuntimeError("could not install snapd")
    return "auto-installed"


def chk_lxd_installed():
    if shutil.which("lxc") or os.path.exists("/snap/bin/lxc"):
        os.environ["PATH"] += ":/snap/bin"
        return "installed"
    note = _ensure_lxd_once()
    return note or "auto-installed"


def chk_lxd_init():
    os.environ["PATH"] += ":/snap/bin"
    rc, out = run("lxc storage list --format json")
    if rc == 0 and out.strip() not in ("", "[]"):
        return "storage ready"
    note = _ensure_lxd_once()
    return note or "initialised"


def chk_firewall():
    import agent
    asyncio.run(agent.apply_protection())
    return "anti-abuse rules applied"


def chk_db():
    sqlite3.connect("bot.db").execute("select 1").fetchone()
    return "bot.db ok"


LXD_REPAIR = [
    "dpkg --configure -a", "apt-get -f install -y -qq", "systemctl restart snapd",
    "snap refresh lxd", "systemctl restart snap.lxd.daemon", "lxd waitready --timeout 90",
]


def chk_env_file():
    here = os.path.dirname(os.path.abspath(__file__))
    envp, txt = os.path.join(here, ".env"), os.path.join(here, "env.txt")
    if not os.path.exists(envp) and os.path.exists(txt):
        shutil.copy(txt, envp)
        return "created .env from env.txt"
    if not os.path.exists(envp):
        raise RuntimeError(".env file missing")
    return ".env found"


def chk_specs():
    du = shutil.disk_usage("/")
    ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    free_gb = du.free // 10**9
    if free_gb < 15:
        raise RuntimeError(f"only {free_gb}GB free disk - VPS need much more")
    return f"{os.cpu_count()} CPU · {ram:.1f}GB RAM · {free_gb}GB free disk"


def chk_lxd_profile():
    """default profile must have a root disk and a network, otherwise containers cannot start."""
    os.environ["PATH"] += ":/snap/bin"
    rc, out = run("lxc profile show default")
    if rc:
        raise RuntimeError("cannot read LXD profile")
    rc, pools = run("lxc storage list --format csv")
    pool = (pools.splitlines() or ["default"])[0].split(",")[0] or "default"
    if "root:" not in out:
        run(f"lxc profile device add default root disk path=/ pool={pool}")
    if "eth0:" not in out:
        if run("lxc network show lxdbr0")[0]:
            run("lxc network create lxdbr0")
        run("lxc profile device add default eth0 nic network=lxdbr0 name=eth0")
    rc, out = run("lxc profile show default")
    if "root:" not in out or "eth0:" not in out:
        raise RuntimeError("default profile still missing root disk / network")
    return f"pool {pool}"


def chk_lxd_images():
    rc, out = run("lxc image list ubuntu: 22.04 --format csv", timeout=180)
    if rc:
        raise RuntimeError("cannot reach LXD image servers: " + out[-120:])
    return "image servers reachable"


def chk_container_selftest():
    """Really launch + delete a tiny container once, so we know containers work (also caches the image)."""
    here = os.path.dirname(os.path.abspath(__file__))
    marker = os.path.join(here, ".lxd_selftest_ok")
    if os.path.exists(marker):
        return "passed before"
    run("lxc delete lb-selftest --force")
    rc, out = run("lxc launch ubuntu:22.04 lb-selftest", timeout=900)
    if rc:
        run("lxc delete lb-selftest --force")
        raise RuntimeError("container launch failed: " + out[-150:])
    ok = False
    for _ in range(30):
        if run("lxc exec lb-selftest -- true")[0] == 0:
            ok = True
            break
        time.sleep(2)
    run("lxc delete lb-selftest --force")
    if not ok:
        raise RuntimeError("container started but is not usable")
    open(marker, "w").write("ok")
    return "container launched + removed"


SERVICE_NAME = "legacy-vps-bot"
SERVICE_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"


def _has_systemd():
    return bool(shutil.which("systemctl")) and os.path.isdir("/run/systemd/system")


def launch_background():
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "bot.py")
    if _has_systemd():
        unit = (
            "[Unit]\nDescription=Legacy VPS Discord bot\nAfter=network-online.target snap.lxd.daemon.service\n"
            "Wants=network-online.target\n\n[Service]\n"
            f"WorkingDirectory={here}\nEnvironment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin\n"
            f"Environment=PYTHONUNBUFFERED=1\nExecStart={sys.executable} {script} --foreground\n"
            "Restart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n")
        open(SERVICE_PATH, "w").write(unit)
        run("systemctl daemon-reload")
        run(f"systemctl enable {SERVICE_NAME}")
        run(f"systemctl restart {SERVICE_NAME}")
        time.sleep(4)
        if run(f"systemctl is-active {SERVICE_NAME}")[1].strip() != "active":
            raise RuntimeError(f"service did not start - see: journalctl -u {SERVICE_NAME} -n 50")
        return "systemd service, auto-starts on reboot"
    # no systemd: detached process
    pidf = os.path.join(here, "bot.pid")
    _kill_pidfile(pidf)
    p = subprocess.Popen([sys.executable, script, "--foreground"], cwd=here, start_new_session=True,
                         stdout=open(os.path.join(here, "bot.log"), "a"), stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL)
    open(pidf, "w").write(str(p.pid))
    return f"background process pid {p.pid}, log: bot.log"


def _kill_pidfile(pidf):
    try:
        os.kill(int(open(pidf).read().strip()), 15)
    except Exception:
        pass


def control(cmd):
    here = os.path.dirname(os.path.abspath(__file__))
    if _has_systemd():
        if cmd == "logs":
            os.execvp("journalctl", ["journalctl", "-u", SERVICE_NAME, "-f", "-n", "50"])
        rc, out = run(f"systemctl {cmd} {SERVICE_NAME}")
        print(out or f"{SERVICE_NAME}: {cmd} done")
    elif cmd == "stop":
        _kill_pidfile(os.path.join(here, "bot.pid"))
        print("stopped")
    elif cmd == "logs":
        os.execvp("tail", ["tail", "-n", "50", "-f", os.path.join(here, "bot.log")])
    else:
        print("status/restart: run `python3 bot.py` again")
    sys.exit(0)


def run_all():
    print(f"\n{G}━━ Startup checks ━━{X}")
    step("Python version", chk_python)
    step("Root access", chk_root)
    step("pip", chk_pip, repair="apt-get install -y -qq python3-pip")
    step("discord.py", pkg("discord", "'discord.py>=2.3'"), repair=f'"{sys.executable}" -m pip install -q --upgrade pip --break-system-packages')
    step("python-dotenv", pkg("dotenv", "python-dotenv"))
    step("aiohttp", pkg("aiohttp", "aiohttp"))
    step(".env file", chk_env_file)
    step(".env config", chk_env)
    step("Server specs", chk_specs, fatal=False)
    step("iptables", chk_iptables, repair="apt-get update -qq")
    step("snapd", chk_snapd, repair=["apt-get update -qq", "systemctl enable --now snapd.socket"])
    step("LXD installed", chk_lxd_installed, repair=LXD_REPAIR)
    step("LXD initialised (storage + network)", chk_lxd_init, repair=LXD_REPAIR)
    step("LXD default profile (disk + network)", chk_lxd_profile, repair=LXD_REPAIR)
    step("LXD image servers", chk_lxd_images, fatal=False,
         repair=["systemctl restart snap.lxd.daemon", "lxd waitready --timeout 60"])
    step("LXD container test", chk_container_selftest, fatal=False, repair=LXD_REPAIR)
    step("Firewall / anti-abuse protection", chk_firewall, fatal=False)
    step("Database", chk_db)
    print(f"{G}━━ All checks done ━━{X}\n", flush=True)


def main_entry(argv):
    """python3 bot.py            -> run checks (auto-fix), then start the bot in the background and exit
       python3 bot.py --foreground -> run checks quietly and run the bot in this process (used by the service)
       python3 bot.py stop|restart|status|logs -> control the background bot"""
    if len(argv) > 1 and argv[1] in ("stop", "restart", "status", "logs"):
        control(argv[1])
    run_all()
    if "--foreground" in argv or os.getenv("INVOCATION_ID"):
        return
    step("Starting bot in background", launch_background)
    print(f"\nBot is running in the background. Controls:\n"
          f"  python3 bot.py logs      - live logs\n  python3 bot.py status    - is it running\n"
          f"  python3 bot.py restart   - restart\n  python3 bot.py stop      - stop\n")
    sys.exit(0)


if __name__ == "__main__":
    run_all()
