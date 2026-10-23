"""Bomberman Server Setup: python setup.py (or the "Bomberman Server Setup" program)

The first run asks three questions (who plays, the server's name, where support mail goes),
writes .env, makes sure Feega (the sign-in service) and the shared DNS server run, starts the
server in Docker and checks it. Later runs show a menu: start and stop, settings, update,
backups, support mail, logs. Needs Docker and nothing else (Python 3.8+ when run as a script,
standard library only).

`python setup.py --update --yes` updates the server folder to the newest release without a
question (an admin panel's Update button runs it, with no terminal).
"""
import argparse
import datetime
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

from hostsetup import openlobby
from hostsetup import progress as proglib
from hostsetup import upnp

REPO = os.environ.get("BOMB_SETUP_REPO", "PSChuze/ndbomb")
RELEASES = "https://github.com/%s/releases" % REPO
DOCKER_DOWNLOAD = "https://www.docker.com/products/docker-desktop/"
DOCKER_LINUX = "https://docs.docker.com/engine/install/"
PORT_HELP = "https://portforward.com/router.htm"
DEFAULT_NAME = "Bomberman Server"
FROZEN = getattr(sys, "frozen", False)

KEY = "bomb"
TITLE = "Bomberman Online"
ADMIN_URL = "http://bomb:24090"
# The names the game connects to. Feega's own name (lib.feega.sonyfinance.co.jp) is added by
# Feega under its key.
HOST_NAMES = ("ps2bb-pkg01.bomberman-online.com", "ps2bb-pkg02.bomberman-online.com",
              "ps2bb-pkg03.bomberman-online.com", "ps2bb-pkgtkt.bomberman-online.com",
              "ps2bb-pkggame01.bomberman-online.com")
# The names the shared web server on port 80 sends to this server's web service.
HTTP_HOSTS = ("ps2bb-pkg01.bomberman-online.com", "ps2bb-pkg02.bomberman-online.com")
WEB_URL = "http://bomb-web:80"
# (port, protocol) players must reach; the consoles' ports are fixed. Port 80 is the shared web
# server's, which sends the game's pages here.
GAME_PORTS = ((80, "TCP"), (9901, "TCP"), (10020, "TCP"), (10020, "UDP"), (10081, "TCP"))
FEEGA_PORTS = ((14020, "TCP"), (14080, "TCP"))
# Files of ours in the server folder that an update must never replace.
KEEP = (".env", "compose.override.yml", "state", "backups", "logs")
# What an admin panel runs in the server folder for its Update button.
UPDATE_COMMAND = ["python", "setup.py", "--update", "--yes"]
UPDATE_MINUTES = 60


def use_root(path):
    """Point every path at the server folder `path`."""
    global ROOT, ENV, ENV_EXAMPLE, BACKUPS, CHOICES, LOG, VERSION_FILE
    ROOT = os.path.abspath(path)
    ENV = os.path.join(ROOT, ".env")
    ENV_EXAMPLE = os.path.join(ROOT, ".env.example")
    BACKUPS = os.path.join(ROOT, "backups")
    CHOICES = os.path.join(ROOT, "state", "setup.json")
    LOG = os.path.join(ROOT, "logs", "setup.log")
    VERSION_FILE = os.path.join(ROOT, ".setup-version")


use_root(os.path.dirname(os.path.abspath(__file__)))


# --- terminal ----------------------------------------------------------------------------

def say(text=""):
    print(text, flush=True)


def ask(question, default=""):
    shown = " [%s]" % default if default else ""
    try:
        answer = input("%s%s: " % (question, shown)).strip()
    except EOFError:
        answer = ""
    return answer or default


def ask_yes(question, default=True):
    answer = ask(question + (" (Y/n)" if default else " (y/N)")).lower()
    return default if not answer else answer.startswith("y")


def ask_choice(question, options, default=1):
    """Number options from 1 and return the 1-based pick."""
    if question:
        say(question)
    for i, text in enumerate(options, 1):
        say("  %d. %s" % (i, text))
    while True:
        answer = ask("Type a number", str(default))
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        say("  Type one of the numbers above.")


def heading(text):
    say()
    say(text)
    say("-" * len(text))


def log_file():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    f = open(LOG, "a", encoding="utf-8")
    f.write("\n=== %s ===\n" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return f


# --- the server's files -------------------------------------------------------------------

def find_root(args_folder=None):
    """The server folder: the one given, else the folder this runs from when it holds the
    server, else "Bomberman Server" in the home folder (made when missing)."""
    if args_folder:
        return os.path.abspath(args_folder)
    if not FROZEN:
        return os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.dirname(sys.executable), os.getcwd()):
        if os.path.exists(os.path.join(d, "compose.yml")):
            return d
    return os.path.join(os.path.expanduser("~"), DEFAULT_NAME)


def bundled(name):
    """A file packed into the setup program, or None."""
    base = getattr(sys, "_MEIPASS", None)
    path = os.path.join(base, name) if base else None
    return path if path and os.path.exists(path) else None


def read_manifest(root):
    try:
        with open(os.path.join(root, ".setup-files"), encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except OSError:
        return []


def unpack_server(archive, root, version=""):
    """Unpack a source zip of this repository into root, replacing the files the last one
    put there and leaving the host's own (.env, state, ...) alone. Returns the paths it
    wrote, relative to root."""
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        tops = {n.split("/", 1)[0] for n in z.namelist()}
        strip = len(tops) == 1 and all("/" in n for n in names)
        old = set(read_manifest(root))
        written = []
        for name in names:
            rel = name.split("/", 1)[1] if strip else name
            if not rel or rel.split("/", 1)[0] in KEEP:
                continue
            target = os.path.normpath(os.path.join(root, rel))
            if not target.startswith(os.path.normpath(root) + os.sep):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(name) as src, open(target + ".tmp", "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(target + ".tmp", target)
            written.append(rel)
    for rel in old - set(written):
        try:
            os.remove(os.path.join(root, rel))
        except OSError:
            pass
    with open(os.path.join(root, ".setup-files"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sorted(written)) + "\n")
    if version:
        with open(os.path.join(root, ".setup-version"), "w", encoding="utf-8") as f:
            f.write(version + "\n")
    return written


def write_build(root, commit, tag="", built_at=None):
    """bomb/build.json: the commit the server folder holds, which the server cannot learn
    from a git archive. The admin panel's update notice compares it with the newest release."""
    if not commit:
        return
    built_at = built_at or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = os.path.join(root, "bomb", "build.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"commit": commit, "tag": tag, "built_at": built_at}, f)
        f.write("\n")


def archive_commit(archive):
    """The commit a git archive zip was made from (git writes it as the zip comment), or ""."""
    try:
        with zipfile.ZipFile(archive) as z:
            comment = z.comment.decode("ascii", "replace").strip().lower()
    except (OSError, zipfile.BadZipFile):
        return ""
    return comment if re.match(r"^[0-9a-f]{40}$", comment) else ""


def installed_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def ensure_server_files():
    """Put the server's files in ROOT when they are not there yet."""
    if os.path.exists(os.path.join(ROOT, "compose.yml")):
        return True
    archive = bundled("server.zip")
    if not archive:
        say("The server's files are not in %s, and this setup program does not carry them." % ROOT)
        say("Download Bomberman Server Setup again from %s" % RELEASES)
        return False
    os.makedirs(ROOT, exist_ok=True)
    version = ""
    if bundled("server-version.txt"):
        with open(bundled("server-version.txt"), encoding="utf-8") as f:
            version = f.read().strip()
    unpack_server(archive, ROOT, version)
    if bundled("server-build.json"):
        try:
            with open(bundled("server-build.json"), encoding="utf-8") as f:
                info = json.load(f)
            write_build(ROOT, info.get("commit"), info.get("tag") or version, info.get("built_at"))
        except (OSError, ValueError, AttributeError):
            pass
    say("The server's files are in %s" % ROOT)
    return True


def latest_release():
    """(tag, source zip URL) of the newest release, or None."""
    url = "https://api.github.com/repos/%s/releases/latest" % REPO
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"}),
                                    timeout=10) as r:
            info = json.load(r)
    except (OSError, ValueError):
        return None
    tag = info.get("tag_name")
    return (tag, "https://github.com/%s/archive/refs/tags/%s.zip" % (REPO, tag)) if tag else None


# --- docker ------------------------------------------------------------------------------

def run(cmd, check=True, capture=False, **kw):
    """Run a command in the server folder; its output goes to the terminal unless captured."""
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    result = subprocess.run(cmd, cwd=ROOT if os.path.isdir(ROOT) else None, **kw)
    if check and result.returncode != 0:
        raise SystemExit("Failed (exit %d): %s" % (result.returncode, " ".join(cmd)))
    return result


def compose(*args, **kw):
    return run(["docker", "compose"] + list(args), **kw)


def add_docker_to_path():
    """Docker installed while this window was open is not on its PATH yet."""
    places = [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Docker", "Docker", "resources",
                           "bin"),
              "/usr/local/bin", "/Applications/Docker.app/Contents/Resources/bin",
              os.path.join(os.path.expanduser("~"), ".docker", "bin")]
    path = os.environ.get("PATH", "").split(os.pathsep)
    for p in places:
        if os.path.isdir(p) and p not in path:
            path.append(p)
    os.environ["PATH"] = os.pathsep.join(path)


def docker_state():
    """"ok", "missing", "stopped", "denied" (Linux user not allowed) or "old"."""
    add_docker_to_path()
    if not shutil.which("docker"):
        return "missing"
    try:
        info = run(["docker", "info"], check=False, capture=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "stopped"
    if info.returncode != 0:
        return "denied" if "permission denied" in (info.stdout or "").lower() else "stopped"
    if run(["docker", "compose", "version"], check=False, capture=True).returncode != 0:
        return "old"
    return "ok"


def start_docker_desktop():
    """Ask Docker Desktop to start; True when there was one to start."""
    if os.name == "nt":
        exe = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Docker", "Docker",
                           "Docker Desktop.exe")
        if os.path.exists(exe):
            subprocess.Popen([exe], close_fds=True)
            return True
    elif sys.platform == "darwin" and os.path.isdir("/Applications/Docker.app"):
        return subprocess.run(["open", "-a", "Docker"]).returncode == 0
    return False


def wait_for_docker(wait=True):
    """Return once Docker works. Explains what is missing and waits for the host to fix it
    (or returns False at once with wait=False)."""
    started = False
    while True:
        state = docker_state()
        if state == "ok":
            return True
        say()
        if state == "missing":
            if sys.platform.startswith("linux"):
                say("Docker is not installed. Install Docker Engine: %s" % DOCKER_LINUX)
            else:
                say("Docker Desktop is not installed. This server runs inside it.")
                say("Download it here, install it and start it: %s" % DOCKER_DOWNLOAD)
                say("If it asks you to sign in, you can skip that.")
        elif state == "stopped" and not started and wait and start_docker_desktop():
            started = True
            say("Starting Docker Desktop. This can take a minute or two.")
            for _ in range(60):
                time.sleep(3)
                if docker_state() == "ok":
                    say("Docker Desktop is running.")
                    return True
            say("Docker Desktop has not started yet. If it shows a message (terms to accept,")
            say("an update to install), answer it.")
        elif state == "stopped":
            if sys.platform.startswith("linux"):
                say("Docker is installed but not running. Start it with: sudo systemctl start docker")
            else:
                say("Docker Desktop is installed but not running. Open it and wait until it says")
                say("it is running.")
        elif state == "denied":
            say("Your user may not use Docker yet. Run this, then log out and back in:")
            say("  sudo usermod -aG docker $USER")
        else:
            say("This Docker is too old (it has no 'docker compose'). Update it: %s" % DOCKER_DOWNLOAD)
        if not wait:
            return False
        ask("Press Enter to check again (or close this window to stop)")


def docker_desktop():
    """True on Docker Desktop, which hides the players' addresses behind one."""
    out = run(["docker", "info", "--format", "{{.OperatingSystem}}"], check=False, capture=True).stdout
    return "docker desktop" in (out or "").lower()


def server_running():
    out = compose("ps", "--status", "running", "--services", check=False, capture=True).stdout or ""
    return "game" in out.split()


def smoke(quiet=False, tries=1):
    """Run the stack's own check (bomb/smoke.py) inside the game container."""
    for n in range(tries):
        r = compose("exec", "-T", "game", "python", "-m", "bomb.smoke", check=False, capture=True)
        if r.returncode == 0:
            break
        if n + 1 < tries:
            time.sleep(3)
    if not quiet or r.returncode:
        say(r.stdout.rstrip())
    say("The server answers as it should." if r.returncode == 0 else
        "A check failed. See the lines above and the server's log.")
    return r.returncode == 0


# --- settings ----------------------------------------------------------------------------

def read_env(path=None):
    path = path or ENV
    values = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*([A-Z_][A-Z0-9_]*)=(.*)$", line.rstrip("\n"))
                if m:
                    values[m.group(1)] = m.group(2).strip()
    return values


def write_env(values, path=None, template=None):
    """Write values into the template's lines (a commented-out `#KEY=` line is switched on, a
    value of None comments a line out); keys the template does not have go at the end.
    Comments stay as they are."""
    path, template = path or ENV, template or ENV_EXAMPLE
    source = path if os.path.exists(path) else template
    with open(source, encoding="utf-8") as f:
        lines = f.read().splitlines()
    left = dict(values)
    out = []
    for line in lines:
        m = re.match(r"(#?)([A-Z_][A-Z0-9_]*)=", line)
        if m and m.group(2) in left:
            value = left.pop(m.group(2))
            out.append(("#" + line.lstrip("#")) if value is None else "%s=%s" % (m.group(2), value))
            continue
        out.append(line)
    for key, value in left.items():
        if value is not None:
            out.append("%s=%s" % (key, value))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)


def read_choices():
    try:
        with open(CHOICES, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_choices(choices):
    os.makedirs(os.path.dirname(CHOICES), exist_ok=True)
    with open(CHOICES, "w", encoding="utf-8", newline="\n") as f:
        json.dump(choices, f, indent=1)
        f.write("\n")


def lan_address():
    """This machine's address on the local network (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return ""


def public_address():
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=4) as r:
            ip = r.read().decode().strip()
        socket.inet_aton(ip)
        return ip
    except (OSError, ValueError):
        return ""


def is_ip(text):
    parts = text.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts) and len(text) <= 15


def who_plays(env, choices):
    """"home" or "internet": as chosen, or guessed from the address."""
    if choices.get("who") in ("home", "internet"):
        return choices["who"]
    return "home" if upnp.is_private(env.get("BOMB_GAME_ADDR", "")) else "internet"


def advertise_map(who, address, lan, desktop):
    """BOMB_ADVERTISE_MAP: players at home get the LAN address and everyone else the public
    one. Not on Docker Desktop, where every player shows with one address."""
    if who != "internet" or desktop or not lan or lan == address:
        return ""
    net = ".".join(lan.split(".")[:2]) + ".0.0/16" if lan.startswith("192.168.") else (
        lan.split(".")[0] + ".0.0.0/8" if lan.startswith("10.") else
        ".".join(lan.split(".")[:2]) + ".0.0/12")
    return "%s=%s,0.0.0.0/0=%s" % (net, lan, address)


def first_settings(who, address, webhook, env, desktop):
    """The .env values for the answers. Everything not asked keeps its value in env (the
    template on a first run)."""
    changes = {"BOMB_GAME_ADDR": address,
               "BOMB_ADVERTISE_MAP": advertise_map(who, address, lan_address(), desktop),
               "BOMB_SUPPORT_WEBHOOK": webhook}
    if not env.get("BOMB_ADMIN_TOKEN"):
        changes["BOMB_ADMIN_TOKEN"] = secrets.token_hex(16)
    return changes


def ports_to_open(with_feega):
    return list(GAME_PORTS) + (list(FEEGA_PORTS) if with_feega else [])


def port_ranges(ports):
    """[(first, last)] runs of consecutive ports, for a short table."""
    out = []
    for p in sorted(set(ports)):
        if out and p == out[-1][1] + 1:
            out[-1] = (out[-1][0], p)
        else:
            out.append((p, p))
    return out


def state_dir(env):
    return os.path.join(ROOT, env.get("BOMB_STATE") or "state")


def write_news(env, server_name, text):
    path = os.path.join(state_dir(env), "data", "news.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    news = [{"title": ("Welcome to %s" % server_name)[:40], "body": text}]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(news, f, ensure_ascii=False, indent=1)
        f.write("\n")


# --- the three questions -----------------------------------------------------------------

def ask_who(current="home"):
    heading("Question 1 of 3: Who will play on this server?")
    pick = ask_choice("", ["Just people on my home network", "Friends over the internet"],
                      2 if current == "internet" else 1)
    return "internet" if pick == 2 else "home"


def ask_address(who, current=""):
    """The address consoles connect to: this machine's for home, the internet one otherwise.
    The game takes only a dotted IP here."""
    if who == "home":
        lan = lan_address()
        if lan:
            say("Players at home will connect to this computer's address: %s" % lan)
            return lan
        say("Setup could not find this computer's address on your network.")
        default = current
    else:
        public = public_address()
        default = current if current and not upnp.is_private(current) else public
        if public:
            say("Your internet address is %s." % public)
        say("Friends connect to this address. The game needs the numbers (like 203.0.113.5),")
        say("not a name. Press Enter to use it.")
    while True:
        address = ask("Address", default)
        if is_ip(address):
            return address
        say("  Type the address as four numbers with dots, like 203.0.113.5.")


def ask_name(current=DEFAULT_NAME):
    heading("Question 2 of 3: Give your server a name")
    say("Players see it in the news on BOMBERMAN WEB.")
    return ask("Server name", current) or DEFAULT_NAME


def ask_webhook(current=""):
    heading("Question 3 of 3: Where should support mail go?")
    say("Players can send you mail from BOMBERMAN WEB > Contact Support. You answer from this")
    say("setup's menu or an admin panel. To also get each one in a Discord channel, paste the")
    say("channel's webhook URL (Channel settings, Integrations, Webhooks). Press Enter to skip.")
    while True:
        url = ask("Webhook URL", current)
        if not url or url.startswith("https://"):
            return url
        say("  A webhook URL starts with https://")


# --- the other installs: Feega, DNS, a panel ----------------------------------------------

def sibling(name):
    """A folder next to the server folder, for an install this setup starts for others."""
    return os.path.join(os.path.dirname(ROOT), name)


def dns_address(env):
    """The address a DNS server this setup starts answers with. It answers home networks only,
    so this is the LAN address even when friends play over the internet."""
    return lan_address() or env.get("BOMB_GAME_ADDR", "")


def ensure_feega(env, choices, progress, log):
    """Feega running (started from its release when there is none). True when it runs."""
    if openlobby.feega_running():
        return True
    progress.begin("Starting Feega (the sign-in service)", "a few minutes the first time")
    try:
        folder = choices.get("feega_folder") or sibling("Freega")
        openlobby.install_feega(folder, dns_address(env), log=log)
        choices["feega_folder"] = folder
        choices["feega_ours"] = True
        write_choices(choices)
    except openlobby.Failed as e:
        progress.end(False)
        say(str(e))
        return False
    progress.end(True)
    return True


def ensure_dns(env, choices, progress, log):
    """The shared DNS server running, with this game's names. True when it worked."""
    progress.begin("Adding the game's names to the DNS server", "a few seconds")
    try:
        name = openlobby.dns_container()
        if not name:
            folder = choices.get("dns_folder") or sibling("OpenLobby DNS")
            name = openlobby.start_dns(folder, dns_address(env))
            choices["dns_folder"] = folder
            write_choices(choices)
        openlobby.dns_names(name, KEY, HOST_NAMES)
        if choices.get("feega_ours"):
            openlobby.dns_names(name, "feega", openlobby.FEEGA_NAMES)
    except openlobby.Failed as e:
        progress.end(False)
        say(str(e))
        return False
    progress.end(True)
    return True


def ensure_http(choices, progress, log):
    """The shared web server on port 80 running, sending this game's pages here."""
    progress.begin("Adding the game's pages to the web server on port 80", "a few seconds")
    try:
        name = openlobby.http_container()
        if not name:
            folder = choices.get("http_folder") or sibling("OpenLobby HTTP")
            name = openlobby.start_http(folder)
            choices["http_folder"] = folder
            write_choices(choices)
        openlobby.http_hosts(name, KEY, WEB_URL, HTTP_HOSTS)
    except openlobby.Failed as e:
        progress.end(False)
        say(str(e))
        return False
    progress.end(True)
    return True


def interactive():
    """Someone at a terminal who can answer questions."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def choose_panel(found):
    pick = ask_choice("More than one OpenLobby admin panel is running here. Which one should manage %s?" % TITLE,
                      ["%s (%s)" % p for p in found])
    return found[pick - 1][1]


def find_panel():
    """The panel to pair with: the one paired before when it is still running, else the only one,
    else the host's pick. "" when there is none; None when there are several and no terminal to
    choose (said so)."""
    try:
        return openlobby.panel_url(choose=choose_panel if interactive() else None,
                                   prefer=read_choices().get("panel_url", ""))
    except openlobby.Failed as e:
        say(str(e))
        return None


def offer_panel(env, ask_first=True):
    """Pair with an OpenLobby admin panel running here, when the host wants that."""
    panel = find_panel()
    if not panel:
        if panel == "" and not ask_first:
            say("No OpenLobby admin panel is running on this computer.")
        return
    if ask_first and not ask_yes("An OpenLobby admin panel is running here. Manage %s from it?" % TITLE):
        return
    say("In the panel, open Settings, Modules, \"Pair with a setup program\" for a code.")
    while True:
        code = ask("Code (Enter to skip)")
        if not code:
            return
        try:
            answer = openlobby.pair(panel, code, KEY, TITLE, ADMIN_URL, env["BOMB_ADMIN_TOKEN"],
                                    updater=updater_block())
        except openlobby.Failed as e:
            say(str(e))
            continue
        write_env({"OPENLOBBY_PAIRING": answer["pairing"]})
        choices = read_choices()
        choices["panel_url"] = panel
        write_choices(choices)
        updater_sent(answer)
        say("Paired. %s shows in the panel." % TITLE)
        return


def updater_block():
    """How the panel runs an update here: the server folder as this computer sees it."""
    return {"folder": ROOT, "command": list(UPDATE_COMMAND), "timeout_minutes": UPDATE_MINUTES}


def updater_sent(answer):
    """Remember the folder the panel has, and pass on its answer to the update block."""
    choices = read_choices()
    choices["updater_folder"] = ROOT
    write_choices(choices)
    if answer.get("updater") == "requested":
        say("The panel's owner must approve one-click updates for %s on Settings > Modules." % TITLE)


def resend_updater():
    """Tell a paired panel where the server folder is when it has moved since pairing."""
    env = read_env()
    if not env.get("OPENLOBBY_PAIRING") or read_choices().get("updater_folder") == ROOT:
        return
    panel = find_panel()
    if not panel:
        return
    try:
        updater_sent(openlobby.pair_update(panel, KEY, env["OPENLOBBY_PAIRING"], env.get("BOMB_ADMIN_TOKEN", ""),
                                           updater=updater_block()))
    except openlobby.Failed as e:
        say(str(e))


# --- steps -------------------------------------------------------------------------------

def failed(what, tail):
    say()
    say(what)
    reason = proglib.explain(tail)
    if reason:
        say(reason)
    say("The last lines it wrote:")
    for line in tail[-12:]:
        say("  " + line)
    say("Everything it wrote is in %s" % LOG)


def start_server(progress, log):
    progress.begin("Getting the server program ready", "a minute or two the first time")
    code, tail = proglib.run_logged(["docker", "compose", "build"], log, cwd=ROOT, progress=progress)
    progress.end(code == 0)
    if code != 0:
        failed("Getting the server program ready failed.", tail)
        return False
    progress.begin("Starting the server", "a few seconds")
    code, tail = proglib.run_logged(["docker", "compose", "up", "-d"], log, cwd=ROOT, progress=progress)
    progress.end(code == 0)
    if code != 0:
        failed("Starting the server failed.", tail)
        return False
    progress.begin("Checking the server", "a few seconds")
    ok = smoke(quiet=True, tries=10)
    progress.end(ok)
    return ok


def complete_setup(env, choices):
    """Start what is missing: the network, Feega, the DNS names, the web front, the server."""
    heading("Starting")
    progress = proglib.Progress()
    with log_file() as log:
        try:
            openlobby.ensure_network()
        except openlobby.Failed as e:
            say(str(e))
            return False
        if not ensure_feega(env, choices, progress, log):
            say("Bomberman needs Feega to sign players in. Fix the cause, then run this setup again.")
            return False
        dns_ok = ensure_dns(env, choices, progress, log)
        http_ok = ensure_http(choices, progress, log)
        if not start_server(progress, log):
            return False
    if not dns_ok:
        say("The server runs, but players' consoles need a DNS server that knows its names (README).")
    if not http_ok:
        say("The server runs, but the patch check and BOMBERMAN WEB need the shared web server on")
        say("port 80 (README). Without it the game stops at the patch check.")
    finish(read_env(), read_choices())
    return True


# --- router ------------------------------------------------------------------------------

def open_router_ports(choices, quiet=False):
    """Ask the router to forward the ports. Returns (all open?, the router's address)."""
    lan = lan_address()
    ports = ports_to_open(choices.get("feega_ours"))
    if not quiet:
        say("Asking your router to open the ports...")
    gateway = upnp.find_gateway(lan) if lan else None
    if not gateway:
        if not quiet:
            say("Your router did not answer (its UPnP setting may be off). The ports need opening by hand.")
        return False, ""
    results = upnp.open_ports(gateway, ports, lan)
    try:
        external = gateway.external_ip()
    except upnp.UpnpError:
        external = ""
    bad = {p: r for p, r in results.items() if r not in ("opened", "already")}
    if not quiet or bad:
        if not bad:
            say("Your router opened the ports for this computer (%s)." % lan)
        else:
            say("Your router opened %d of %d ports. Not opened:" % (len(results) - len(bad), len(ports)))
            for (port, proto), r in bad.items():
                say("  %d %s: %s" % (port, proto, "another device uses it" if r == "taken" else r))
    if external and upnp.is_private(external):
        say()
        say("Note: your router has no internet address of its own (%s). Your internet provider" % external)
        say("probably shares one address between customers, or there is a second router in front")
        say("of this one. Friends outside your home will likely not reach the server.")
    return not bad, gateway.host


def show_port_help(choices, router=""):
    lan = lan_address() or "this computer"
    heading("Open these ports on your router")
    say("Your router needs to send these ports to this computer (%s)." % lan)
    say("Open the router's settings page (usually http://%s, or the address printed on" % (router or "192.168.0.1"))
    say('the router) and look for "Port forwarding" (or "Virtual server", or "NAT").')
    say()
    say("  Ports            Type   To")
    for proto in ("TCP", "UDP"):
        ports = [p for p, kind in ports_to_open(choices.get("feega_ours")) if kind == proto]
        for first, last in port_ranges(ports):
            shown = str(first) if first == last else "%d-%d" % (first, last)
            say("  %-16s %-6s %s" % (shown, proto, lan))
    say()
    say("Steps for many router models: %s" % PORT_HELP)


# --- finishing ---------------------------------------------------------------------------

def finish(env, choices):
    host = env.get("BOMB_GAME_ADDR", "?")
    say()
    say("=" * 72)
    say("Your server is running.")
    say()
    say("Tell your players: set the game's DNS server to %s and sign in with any feega-ID" % host)
    say("and password.")
    if who_plays(env, choices) == "internet":
        say()
        say("Friends over the internet also need a DNS server that answers them: the shared one")
        say("answers home networks only (see the README, Players at home and over the internet).")
        if choices.get("router") == "open":
            say("Your router has the ports open.")
        else:
            say("Friends can join once the ports above are open on your router.")
    say()
    say("The server keeps running when you close this window, as long as Docker runs.")
    say("Run Bomberman Server Setup again to stop it, change settings or read support mail.")
    say("=" * 72)


def first_run(dry_run=False):
    say("Bomberman Server Setup")
    say("======================")
    say("This sets up a Bomberman Online (PS2) server on this computer.")
    say("Answer three questions, then setup starts everything. It takes a few minutes.")
    env = read_env(ENV_EXAMPLE)
    choices = read_choices()
    who = ask_who()
    address = ask_address(who)
    name = ask_name()
    webhook = ask_webhook()
    desktop = docker_desktop() if not dry_run else sys.platform in ("win32", "darwin")
    changes = first_settings(who, address, webhook, env, desktop)
    choices.update(who=who, name=name)

    heading("Ready")
    say("  Players:       %s, at %s" % ("people on your home network" if who == "home"
                                        else "friends over the internet", address))
    say("  Name:          %s" % name)
    say("  Support mail:  %s" % ("kept here and posted to Discord" if webhook else "kept here"))
    say("Everything else uses the usual settings. You can change them later: run Bomberman")
    say("Server Setup again and choose Change settings.")
    if dry_run:
        say()
        say("Dry run: nothing was written. Setup would write these settings to %s:" % ENV)
        for key, value in changes.items():
            if key != "BOMB_ADMIN_TOKEN":
                say("  %s=%s" % (key, value))
        say("  BOMB_ADMIN_TOKEN: made at random")
        return
    if not ask_yes("Start?"):
        return
    write_env(changes)
    env = read_env()
    write_choices(choices)
    write_news(env, name, "Have fun, and tell the people who run this server about any bugs.")
    say()
    say("Details of every step go to %s" % LOG)
    if who == "internet":
        heading("Router")
        ok, router = open_router_ports(choices)
        choices["router"] = "open" if ok else "closed"
        write_choices(choices)
        if not ok:
            show_port_help(choices, router)
    if complete_setup(env, choices):
        offer_panel(read_env())


# --- menu --------------------------------------------------------------------------------

def restart_if_running(was_running):
    if was_running:
        say("Restarting the server with the new settings...")
        compose("up", "-d")
        smoke(tries=10)


def change_settings():
    """The settings menu. Returns once the host goes back."""
    while True:
        env, choices = read_env(), read_choices()
        who = who_plays(env, choices)
        items = [
            ("Who will play (now: %s, at %s)" % ("home network" if who == "home" else "friends over the internet",
                                                 env.get("BOMB_GAME_ADDR")), change_who),
            ("Server name and news (now: %s)" % (choices.get("name") or DEFAULT_NAME), change_name),
            ("Support mail to Discord (now: %s)" % ("on" if env.get("BOMB_SUPPORT_WEBHOOK") else "off"),
             change_webhook),
            ("Pair with an OpenLobby admin panel (now: %s)" % ("paired" if env.get("OPENLOBBY_PAIRING")
                                                               else "not paired"), change_panel),
            ("Make a new admin token", change_token),
        ]
        heading("Change settings")
        for i, (title, _) in enumerate(items, 1):
            say("  %d. %s" % (i, title))
        say("  0. Back")
        pick = ask("Type a number", "0")
        if pick == "0":
            return
        if pick.isdigit() and 1 <= int(pick) <= len(items):
            running = server_running()
            if items[int(pick) - 1][1](env, read_choices()):
                restart_if_running(running)


def change_who(env, choices):
    who = ask_who(who_plays(env, choices))
    address = ask_address(who, env.get("BOMB_GAME_ADDR", ""))
    choices["who"] = who
    write_env({"BOMB_GAME_ADDR": address,
               "BOMB_ADVERTISE_MAP": advertise_map(who, address, lan_address(), docker_desktop())})
    if who == "internet":
        ok, router = open_router_ports(choices)
        choices["router"] = "open" if ok else "closed"
        if not ok:
            show_port_help(choices, router)
    write_choices(choices)
    say("Saved. Players' consoles need their DNS server set to the new address.")
    return True


def change_name(env, choices):
    name = ask("Server name", choices.get("name") or DEFAULT_NAME)
    text = ask("News text", "Have fun, and tell the people who run this server about any bugs.")
    write_news(env, name, text)
    choices["name"] = name
    write_choices(choices)
    say("Saved. Players see the new news the next time they open BOMBERMAN WEB.")
    say("For more news items or the FAQ, edit %s and faq.json beside it." %
        os.path.join(state_dir(env), "data", "news.json"))
    return False


def change_webhook(env, choices):
    write_env({"BOMB_SUPPORT_WEBHOOK": ask_webhook(env.get("BOMB_SUPPORT_WEBHOOK", ""))})
    return True


def change_panel(env, choices):
    offer_panel(env, ask_first=False)
    return False


def change_token(env, choices):
    token = secrets.token_hex(16)
    write_env({"BOMB_ADMIN_TOKEN": token})
    panel = find_panel() if env.get("OPENLOBBY_PAIRING") else ""
    if panel:
        try:
            updater_sent(openlobby.pair_update(panel, KEY, env["OPENLOBBY_PAIRING"], token,
                                               updater=updater_block()))
            say("The panel has the new token.")
        except openlobby.Failed as e:
            say(str(e))
    return True


def start_server_menu():
    if server_running():
        say("The server is already running.")
        return
    env, choices = read_env(), read_choices()
    if who_plays(env, choices) == "internet":
        open_router_ports(choices, quiet=True)
    complete_setup(env, choices)


def stop_server():
    if not server_running():
        say("The server is not running.")
        return
    if ask_yes("Stop the server? Players in the game are disconnected.", False):
        compose("stop")
        say("Stopped. It starts again when you choose Start the server. Feega and the DNS server")
        say("keep running, since other games may use them.")


class UpdateFailed(Exception):
    """An update that stopped; the message is the one sentence it ends on."""


def running_services():
    """The compose services running now (stderr kept apart: compose warns there)."""
    try:
        r = subprocess.run(["docker", "compose", "ps", "--status", "running", "--services"], cwd=ROOT,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        r = None
    if r is None or r.returncode != 0:
        raise UpdateFailed("Docker did not say which services run, so nothing was changed.")
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def build_images(folder, progress, log, what):
    """docker compose build in folder; UpdateFailed (old version still running) when it fails."""
    progress.begin("Building %s" % what, "a few minutes")
    code, tail = proglib.run_logged(["docker", "compose", "build"], log, cwd=folder, progress=progress)
    progress.end(code == 0)
    if code != 0:
        failed("Building %s failed." % what, tail)
        raise UpdateFailed("Building %s failed; the server still runs the old version." % what)


def restart_running(running, progress, log):
    """Recreate the services that ran before the update with the new images, and check them.
    Services that were stopped stay stopped. Returns what to say about it."""
    if not running:
        return "the server was stopped and stays stopped"
    progress.begin("Restarting %s" % ", ".join(running), "a few seconds")
    code, tail = proglib.run_logged(["docker", "compose", "up", "-d", "--no-build"] + running, log, cwd=ROOT,
                                    progress=progress)
    progress.end(code == 0)
    if code != 0:
        failed("Restarting the server failed.", tail)
        raise UpdateFailed("The new version is in place but did not start; see %s." % LOG)
    if "game" not in running:
        return "restarted %s" % ", ".join(running)
    progress.begin("Checking the server", "a few seconds")
    ok = smoke(quiet=True, tries=10)
    progress.end(ok)
    if not ok:
        raise UpdateFailed("The new version runs but its check failed; see the lines above.")
    return "restarted %s and the check passed" % ", ".join(running)


def download(url, path, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r, open(path, "wb") as f:
        shutil.copyfileobj(r, f)


def remove_old_stages():
    """Drop what an update that was cut off left behind."""
    for name in os.listdir(ROOT):
        if name.startswith(".update-"):
            shutil.rmtree(os.path.join(ROOT, name), ignore_errors=True)


def update(yes=False):
    """Update the server folder to the newest release: download and unpack it beside the old
    files, build it, and only then swap the files and restart what was running, so a failure
    before the swap leaves the old version running. Returns the closing line; raises
    UpdateFailed. With yes it never asks anything."""
    progress = proglib.Progress()
    with log_file() as log:
        if os.path.exists(os.path.join(ROOT, ".git")):
            say("This server folder is a git checkout: pulling the newest commits.")
            if run(["git", "pull", "--ff-only"], check=False, stdin=subprocess.DEVNULL).returncode != 0:
                raise UpdateFailed("git pull failed (local changes?), so nothing was changed.")
            running = running_services()
            build_images(ROOT, progress, log, "the server program")
            return "Updated from git: %s." % restart_running(running, progress, log)

        say("Looking for the newest release on GitHub.")
        release = latest_release()
        if not release:
            raise UpdateFailed("Could not reach GitHub to find the newest release, so nothing was changed.")
        tag, url = release
        if tag == installed_version():
            if yes or not ask_yes("This is the newest version (%s). Rebuild the server program anyway?" % tag,
                                  False):
                return "Already up to date (%s)." % tag
            running = running_services()
            build_images(ROOT, progress, log, "the server program")
            return "Rebuilt %s: %s." % (tag, restart_running(running, progress, log))

        say("Updating from %s to %s." % (installed_version() or "an unknown version", tag))
        remove_old_stages()
        stage = tempfile.mkdtemp(prefix=".update-", dir=ROOT)
        try:
            archive = os.path.join(stage, "source.zip")
            say("Downloading %s." % tag)
            try:
                download(url, archive)
                new = os.path.join(stage, "server")
                os.makedirs(new)
                unpack_server(archive, new)
            except (OSError, zipfile.BadZipFile) as e:
                raise UpdateFailed("The download of %s failed (%s), so nothing was changed." % (tag, e))
            # the build reads the host's settings the way the running server does
            for name in (".env", "compose.override.yml"):
                if os.path.exists(os.path.join(ROOT, name)):
                    shutil.copy2(os.path.join(ROOT, name), os.path.join(new, name))
            running = running_services()
            build_images(new, progress, log, tag)
            say("Putting %s's files in %s." % (tag, ROOT))
            unpack_server(archive, ROOT, tag)
            write_build(ROOT, archive_commit(archive), tag)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        done = restart_running(running, progress, log)
    if FROZEN:
        say("A new Bomberman Server Setup is also on %s" % RELEASES)
    return "Updated to %s: %s." % (tag, done)


def update_menu():
    try:
        say(update())
    except UpdateFailed as e:
        say(str(e))


def update_now(yes):
    """setup.py --update: exit code 0 when updated or already current, and the last line
    printed is the outcome in one sentence."""
    try:
        if not os.path.exists(os.path.join(ROOT, "compose.yml")):
            raise UpdateFailed("%s holds no Bomberman server, so there is nothing to update." % ROOT)
        if not os.path.exists(ENV):
            raise UpdateFailed("This server is not set up yet; run Bomberman Server Setup first.")
        os.chdir(ROOT)
        if not wait_for_docker(wait=False):
            raise UpdateFailed("Docker is not running, so nothing was changed.")
        say(update(yes=yes))
        return 0
    except UpdateFailed as e:
        say(str(e))
    except KeyboardInterrupt:
        say("The update was stopped.")
    except Exception as e:      # the last line must still say what happened
        say("The update stopped on an unexpected error (%s: %s)." % (type(e).__name__, e))
    return 1


def backup():
    env = read_env()
    data = os.path.join(state_dir(env), "data")
    if not os.path.isdir(data):
        say("There is nothing to back up yet.")
        return
    os.makedirs(BACKUPS, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = shutil.make_archive(os.path.join(BACKUPS, "bomb-%s" % stamp), "zip", data)
    say("Saved %s" % archive)
    say("It holds the players' ids, rankings, profiles and support mail. Accounts and passwords")
    say("are Feega's: back up its folder too.")


def restore():
    zips = sorted(f for f in os.listdir(BACKUPS) if f.endswith(".zip")) if os.path.isdir(BACKUPS) else []
    if not zips:
        say("No backups in %s." % BACKUPS)
        return
    for i, name in enumerate(zips, 1):
        say("  %d. %s" % (i, name))
    pick = ask("Restore which one (number, Enter to cancel)")
    if not pick.isdigit() or not 1 <= int(pick) <= len(zips):
        return
    say("This replaces the rankings, profiles and support mail with the ones in %s." % zips[int(pick) - 1])
    if not ask_yes("Go on?", False):
        return
    running = server_running()
    compose("stop", check=False)
    data = os.path.join(state_dir(read_env()), "data")
    shutil.rmtree(data, ignore_errors=True)
    os.makedirs(data)
    with zipfile.ZipFile(os.path.join(BACKUPS, zips[int(pick) - 1])) as z:
        z.extractall(data)
    say("Restored.")
    restart_if_running(running)


def support_mail():
    if not server_running():
        say("Start the server first.")
        return
    compose("exec", "-T", "web", "python", "-m", "bomb.support", "list", check=False)
    n = ask("Ticket number to read and answer (Enter to go back)")
    if not n.isdigit():
        return
    compose("exec", "-T", "web", "python", "-m", "bomb.support", "show", n, check=False)
    text = ask("Your reply (Enter for none)")
    if text:
        compose("exec", "-T", "web", "python", "-m", "bomb.support", "reply", n, text, check=False)
    if ask_yes("Close the ticket?", False):
        compose("exec", "-T", "web", "python", "-m", "bomb.support", "close", n, check=False)


def logs():
    say("Showing what the server is doing. Press Ctrl+C to go back to the menu.")
    try:
        compose("logs", "-f", "--tail", "200", "game", check=False)
    except KeyboardInterrupt:
        pass


def router_again():
    env, choices = read_env(), read_choices()
    if who_plays(env, choices) != "internet":
        say("Your server is set up for your home network only, so the router needs no open ports.")
        say("Choose Change settings, then Who will play, to change that.")
        return
    ok, router = open_router_ports(choices)
    choices["router"] = "open" if ok else "closed"
    write_choices(choices)
    if not ok:
        show_port_help(choices, router)


def tell_players():
    finish(read_env(), read_choices())


def remove():
    say("This stops the server, deletes its containers and takes its names off the shared DNS and")
    say("web servers. Your data in %s stays. Feega and the shared servers keep running" % state_dir(read_env()))
    say("for other games.")
    if not ask_yes("Remove the server?", False):
        return
    compose("down", check=False)
    dns = openlobby.dns_container()
    if dns:
        openlobby.dns_remove(dns, KEY)
    http = openlobby.http_container()
    if http:
        openlobby.http_remove(http, KEY)
    say("Removed. Delete the folder %s to remove everything." % ROOT)


MENU = [("Start the server", start_server_menu),
        ("Stop the server", stop_server),
        ("Change settings", change_settings),
        ("Update", update_menu),
        ("Make a backup", backup),
        ("Restore a backup", restore),
        ("Read and answer support mail", support_mail),
        ("Show what the server is doing (logs)", logs),
        ("Check that the server works", smoke),
        ("Open the router ports again", router_again),
        ("Show what to tell players", tell_players),
        ("Remove the server", remove)]


def menu():
    while True:
        env = read_env()
        heading("Bomberman server: %s (players use %s)" % ("running" if server_running() else "stopped",
                                                           env.get("BOMB_GAME_ADDR", "?")))
        for i, (title, _) in enumerate(MENU, 1):
            say("  %2d. %s" % (i, title))
        say("   0. Quit")
        pick = ask("Type a number", "0")
        if pick == "0":
            return
        if pick.isdigit() and 1 <= int(pick) <= len(MENU):
            try:
                MENU[int(pick) - 1][1]()
            except KeyboardInterrupt:
                say()
            except SystemExit as e:
                say(str(e))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="Bomberman Server Setup", description=(
        "Sets up and runs a Bomberman Online (PS2) server in Docker. The first run asks three "
        "questions; later runs show a menu."))
    ap.add_argument("--folder", help="the server folder (default: the one this runs from, or "
                                     "'Bomberman Server' in your home folder for the setup program)")
    ap.add_argument("--update", action="store_true",
                    help="update the server to the newest release, rebuild it and restart what was running")
    ap.add_argument("--yes", action="store_true", help="never ask anything (with --update, run without a terminal)")
    ap.add_argument("--dry-run", action="store_true",
                    help="ask the questions, then show what would be done; writes nothing and needs no Docker")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    use_root(find_root(args.folder))
    if args.update:
        return update_now(args.yes)
    try:
        if args.dry_run:
            say("Server folder: %s%s" % (ROOT, "" if os.path.exists(os.path.join(ROOT, "compose.yml")) else
                                         " (the server's files would be unpacked here)"))
            say("Docker: %s" % docker_state())
            first_run(dry_run=True)
            return
        if not ensure_server_files():
            return
        os.chdir(ROOT)
        wait_for_docker()
        if not os.path.exists(ENV):
            first_run()
            return
        resend_updater()
        menu()
    except KeyboardInterrupt:
        say()
        sys.exit(1)


if __name__ == "__main__":
    code = 1
    try:
        code = main() or 0
    finally:
        if FROZEN and "--yes" not in sys.argv[1:]:
            # A double-clicked program's window closes when it ends: keep it open to be read.
            try:
                input("Press Enter to close this window.")
            except (EOFError, KeyboardInterrupt):
                pass
    sys.exit(code)
