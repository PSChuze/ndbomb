"""The other installs on this machine: the shared DNS, an OpenLobby admin panel, and Feega.

Installs find each other by Docker labels and talk over one Docker network, `openlobby`:

  org.openlobby.role=dns        the one DNS server every install shares (OpenLobby's image,
                                `command: dns`); installs add their host names to it with
                                `python stub.py dns-names add <key> <name>...` inside it
  org.openlobby.role=http       the one web server on port 80 every install shares; installs send
                                their host names to their own web server with
                                `python stub.py http-hosts add <key> <upstream> <host>...`
  org.openlobby.role=panel      an admin panel; org.openlobby.panel.url is where setup pairs
  org.openlobby.game.key=feega  the Feega sign-in service Bomberman needs (PSChuze/freega)

Every Docker call goes through `docker()`, so tests can replace it.
"""
import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import zipfile

NETWORK = "openlobby"
DNS_IMAGE = os.environ.get("OPENLOBBY_IMAGE", "ghcr.io/prettyopenlobby/openlobby:latest")
DNS_PROJECT = "openlobby-dns"
HTTP_PROJECT = "openlobby-http"
FEEGA_REPO = os.environ.get("FREEGA_REPO", "PSChuze/freega")
# The Feega release setup installs when there is none; a newer one is the Feega host's choice.
FEEGA_TAG = os.environ.get("FREEGA_TAG", "v1.0.0")
FEEGA_PROJECT = "freega"
FEEGA_NAMES = ("lib.feega.sonyfinance.co.jp",)
FEEGA_URL = "http://feega:24030"

DNS_COMPOSE = """\
# The DNS server every game install on this machine shares (OpenLobby's resolver).
# Started by a game's setup because none was running; the others only add names to it.
services:
  dns:
    image: %(image)s
    command: ["dns"]
    restart: unless-stopped
    labels:
      org.openlobby.role: dns
    environment:
      POL_ADVERTISE: "%(advertise)s"
    ports:
      - "%(bind)s53:53/udp"
    volumes:
      - dns-data:/data
volumes:
  dns-data:
"""


HTTP_COMPOSE = """\
# The web server on port 80 every game install on this machine shares (OpenLobby's). Started by
# a game's setup because none was running; the others only add their host names to it.
services:
  http:
    image: %(image)s
    command: ["http"]
    restart: unless-stopped
    labels:
      org.openlobby.role: http
    ports:
      - "80:80"
    volumes:
      - http-data:/data
    networks:
      - openlobby
volumes:
  http-data:
networks:
  openlobby:
    external: true
"""


class Failed(Exception):
    """A step that did not work, with a sentence for the host."""


TOO_OLD = ("The OpenLobby image is too old for this, update it "
           "(docker pull %s, then start that container again)." % DNS_IMAGE)


def _stub_failed(what, code, out):
    """The Failed for a `stub.py` command that did not work."""
    if code == 1 and "usage" in out.lower():
        return Failed(TOO_OLD)
    if code == 2:
        return Failed("The %s refused them: %s" % (what, out.strip()))
    return Failed("The %s could not take them: %s" % (what, out.strip()[-200:]))


def docker(*args, capture=True, cwd=None, timeout=600):
    """Run `docker args...`; (exit code, output)."""
    try:
        r = subprocess.run(["docker"] + list(args), cwd=cwd, timeout=timeout, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.STDOUT if capture else None,
                           text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, (r.stdout or "") if capture else ""


def containers(label):
    """Names of the running containers with this label (key or key=value)."""
    code, out = docker("ps", "--filter", "label=" + label, "--format", "{{.Names}}")
    return [n.strip() for n in out.splitlines() if n.strip()] if code == 0 else []


def label(container, key):
    code, out = docker("inspect", "-f", '{{index .Config.Labels "%s"}}' % key, container)
    out = out.strip()
    return out if code == 0 and out != "<no value>" else ""


def ensure_network():
    if docker("network", "inspect", NETWORK)[0] != 0:
        code, out = docker("network", "create", NETWORK)
        if code != 0 and "already exists" not in out:
            raise Failed("Could not make the Docker network %s: %s" % (NETWORK, out.strip()))


# --- DNS -----------------------------------------------------------------------------------

def dns_container():
    found = containers("org.openlobby.role=dns")
    return found[0] if found else None


def start_dns(folder, advertise, bind=""):
    """Start the shared DNS from the OpenLobby image as its own compose project in `folder`.
    Only call this when dns_container() found none. Returns the container's name."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "compose.yml")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(DNS_COMPOSE % {"image": DNS_IMAGE, "advertise": advertise,
                               "bind": (bind + ":") if bind else "0.0.0.0:"})
    code, out = docker("compose", "-p", DNS_PROJECT, "-f", path, "up", "-d", cwd=folder)
    if code != 0:
        raise Failed("The shared DNS server did not start: %s" % out.strip().splitlines()[-1:])
    for _ in range(20):
        name = dns_container()
        if name:
            return name
        time.sleep(1)
    raise Failed("The shared DNS server did not start.")


def dns_names(container, key, names):
    """Answer `names` (and their subdomains) with this machine's address, under `key`.
    Adding again under the same key replaces that key's names."""
    code, out = docker("exec", container, "python", "stub.py", "dns-names", "add", key, *names)
    if code != 0:
        raise _stub_failed("DNS server", code, out)


def dns_remove(container, key):
    return docker("exec", container, "python", "stub.py", "dns-names", "remove", key)[0] == 0


def dns_list(container):
    code, out = docker("exec", container, "python", "stub.py", "dns-names", "list")
    return out if code == 0 else None


# --- the shared web server on port 80 -------------------------------------------------------

def http_container():
    found = containers("org.openlobby.role=http")
    return found[0] if found else None


def start_http(folder):
    """Start the shared web server from the OpenLobby image as its own compose project in
    `folder`. Only call this when http_container() found none. Returns the container's name."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "compose.yml")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(HTTP_COMPOSE % {"image": DNS_IMAGE})
    ensure_network()
    code, out = docker("compose", "-p", HTTP_PROJECT, "-f", path, "up", "-d", cwd=folder)
    if code != 0:
        raise Failed("The shared web server did not start: %s" % out.strip().splitlines()[-1:])
    for _ in range(20):
        name = http_container()
        if name:
            return name
        time.sleep(1)
    raise Failed("The shared web server did not start.")


def http_hosts(container, key, upstream, hosts):
    """Send requests for these exact host names to `upstream` (http://<alias>:<port> on the
    openlobby network). Adding again under the same key replaces that key's hosts."""
    code, out = docker("exec", container, "python", "stub.py", "http-hosts", "add", key, upstream, *hosts)
    if code != 0:
        raise _stub_failed("web server", code, out)


def http_remove(container, key):
    return docker("exec", container, "python", "stub.py", "http-hosts", "remove", key)[0] == 0


# --- an admin panel ------------------------------------------------------------------------

def panel_url():
    """The address of an OpenLobby admin panel running here, or ""."""
    for name in containers("org.openlobby.role=panel"):
        url = label(name, "org.openlobby.panel.url")
        if url:
            return url.rstrip("/")
    return ""


def _post(url, body, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {}
    except (OSError, ValueError) as e:
        raise Failed("The panel did not answer (%s)." % e)


def pair(panel, code, key, title, url, token, updater=None):
    """Pair with the panel's one-time code; returns the panel's answer, whose "pairing" is the
    secret to keep. `updater` is the panel's one-click update block (folder, command)."""
    body = {"code": code, "key": key, "title": title, "url": url, "token": token}
    if updater:
        body["updater"] = updater
    status, doc = _post(panel + "/api/pair", body)
    if status == 200 and doc.get("pairing"):
        return doc
    if status == 403:
        raise Failed("The panel did not take that code (wrong, used, or older than 10 minutes).")
    if status == 429:
        raise Failed("Too many tries. Wait a minute and try again.")
    raise Failed("The panel said no: %s" % (doc.get("error") or "HTTP %d" % status))


def pair_update(panel, key, pairing, token, url="", updater=None):
    """Send the panel a new token, address or update block; returns its answer."""
    body = {"key": key, "pairing": pairing, "token": token}
    if url:
        body["url"] = url
    if updater:
        body["updater"] = updater
    status, doc = _post(panel + "/api/pair/update", body)
    if status != 200:
        raise Failed("The panel did not take the change: %s" % (doc.get("error") or "HTTP %d" % status))
    return doc


# --- Feega ---------------------------------------------------------------------------------

def feega_running():
    return bool(containers("org.openlobby.game.key=feega"))


def unpack(archive, dest):
    """Unpack a source zip (one top folder, as GitHub makes them) into dest."""
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        tops = {n.split("/", 1)[0] for n in z.namelist()}
        strip = len(tops) == 1 and all("/" in n for n in names)
        for name in names:
            rel = name.split("/", 1)[1] if strip else name
            target = os.path.normpath(os.path.join(dest, rel))
            if not rel or not target.startswith(os.path.normpath(dest) + os.sep):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def env_lines(path, values):
    """The lines of an .env file (or its example) with `values` filled in."""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    left = dict(values)
    out = []
    for line in lines:
        k = line.lstrip("#").split("=", 1)[0].strip()
        if "=" in line and k in left:
            out.append("%s=%s" % (k, left.pop(k)))
        else:
            out.append(line)
    out += ["%s=%s" % kv for kv in left.items()]
    return out


def install_feega(folder, advertise, download=None, log=None):
    """Fetch Feega into `folder` (when it is not there yet), give it an .env, start it as the
    compose project `freega` and add its host name to the shared DNS. Returns its API address."""
    download = download or urllib.request.urlretrieve
    if not os.path.exists(os.path.join(folder, "compose.yml")):
        os.makedirs(folder, exist_ok=True)
        archive = os.path.join(folder, "freega.zip")
        url = "https://github.com/%s/archive/refs/tags/%s.zip" % (FEEGA_REPO, FEEGA_TAG)
        try:
            download(url, archive)
            unpack(archive, folder)
        except (OSError, zipfile.BadZipFile) as e:
            raise Failed("Could not download Feega from %s (%s)." % (url, e))
        finally:
            if os.path.exists(archive):
                os.remove(archive)
    env = os.path.join(folder, ".env")
    if not os.path.exists(env):
        lines = env_lines(os.path.join(folder, ".env.example"),
                          {"FEEGA_ADMIN_TOKEN": secrets.token_hex(16), "FEEGA_ADVERTISE": advertise})
        with open(env, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
    ensure_network()
    code, out = docker("compose", "-p", FEEGA_PROJECT, "--env-file", ".env", "up", "-d", "--build",
                       cwd=folder, timeout=1800)
    if log:
        log.write(out)
    if code != 0:
        raise Failed("Feega did not start. Its last lines: %s" % " / ".join(out.strip().splitlines()[-3:]))
    for _ in range(60):
        if feega_running():
            break
        time.sleep(2)
    else:
        raise Failed("Feega did not start.")
    dns = dns_container()
    if dns:
        dns_names(dns, "feega", FEEGA_NAMES)
    return FEEGA_URL
