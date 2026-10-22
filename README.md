# ndbomb

A reference implementation of a server for Bomberman Online (Net de Bomberman, PlayStation 2
HDD), and a base for hosting your own. It answers what the game connects to after sign-in: the
patch check, the ticket check, the game and lobby server (rooms, chat, profiles, rankings), the
match relay, and the BOMBERMAN WEB pages (news, FAQ, support mail).

Players sign in with a feega-ID. That service, Feega, is its own server,
[freega](https://github.com/PSChuze/freega), shared with other games on the same machine. The
setup program starts it for you when none is running.

The server holds no game data and needs none: everything it sends is built from the protocol.

## Quick start

1. Download **Bomberman Server Setup** for your computer from the [Releases](../../releases)
   page: `Bomberman-Server-Setup.exe` (Windows), `Bomberman-Server-Setup-mac.zip` (Mac with Apple
   silicon) or `Bomberman-Server-Setup-linux-x86_64`.
2. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (on Linux, Docker
   Engine) and start it.
3. Double-click Bomberman Server Setup and answer three questions: who will play, the server's
   name, and where support mail should go.

Setup does the rest: it starts Feega and the shared DNS and web servers if they are not running
yet, adds the game's host names to them, starts this server, asks your router to open the ports
if friends play over the internet, and tells you what to tell your players. Run it again for a
menu: start and stop, settings, update, backups, support mail, logs.

From a clone of this repository, `python setup.py` (Python 3.8 or newer, nothing else to install)
is the same program. `python setup.py --dry-run` asks the questions and shows what it would do
without touching anything.

## How players connect

Players need a game install whose network settings and DNS server point at this server. Setup's
last line gives the address to use. They sign in with any feega-ID and password: a new feega-ID
is made on its first sign-in. Their install must trust the certificate authority of the Feega
server they sign in to (see the freega README).

## By hand (without the setup program)

```sh
docker network inspect openlobby >/dev/null 2>&1 || docker network create openlobby
cp .env.example .env        # then set BOMB_GAME_ADDR and BOMB_ADMIN_TOKEN
docker compose up -d
docker compose exec -T game python -m bomb.smoke
```

You also need Feega running on the `openlobby` network (`http://feega:24030`, see freega's
INTERFACE.md), a web server on port 80 that sends the pkg01 and pkg02 names to
`http://bomb-web:80`, and a DNS server that answers the game's host names with this machine's
address:

    ps2bb-pkg01.bomberman-online.com     news, FAQ and support mail (web, port 80)
    ps2bb-pkg02.bomberman-online.com     patch check (web, port 80)
    ps2bb-pkg03.bomberman-online.com     nickname registration (only with BOMB_REQUIRE_NICKNAME=1)
    ps2bb-pkgtkt.bomberman-online.com    ticket check (port 9901)
    ps2bb-pkggame01.bomberman-online.com game and lobby (port 10020)

plus Feega's `lib.feega.sonyfinance.co.jp`. With the shared OpenLobby DNS server (the container
labelled `org.openlobby.role=dns`):

    docker exec <dns container> python stub.py dns-names add bomb ps2bb-pkg01.bomberman-online.com ps2bb-pkg02.bomberman-online.com ps2bb-pkg03.bomberman-online.com ps2bb-pkgtkt.bomberman-online.com ps2bb-pkggame01.bomberman-online.com

and with the shared OpenLobby web server on port 80 (labelled `org.openlobby.role=http`):

    docker exec <http container> python stub.py http-hosts add bomb http://bomb-web:80 ps2bb-pkg01.bomberman-online.com ps2bb-pkg02.bomberman-online.com

Setup starts either one from the OpenLobby image when none is running.

Logs: `docker compose logs -f game`, and one file per service in `state/logs`.

### Ports

| port | what |
|---|---|
| 80 TCP | patch check and BOMBERMAN WEB, through the shared web server |
| 9901 TCP | ticket check |
| 10020 TCP and UDP | game and lobby |
| 10081 TCP | match relay |
| 14020, 14080 TCP | Feega and its certificate download (its own container) |

The consoles always use these numbers. For players outside your network, forward all of them on
your router (setup asks the router to do it).

### Players at home and over the internet

The lobby list hands each console the address to connect to, as a dotted IP
(`BOMB_GAME_ADDR`). When some players are at home and others come over the internet,
`BOMB_ADVERTISE_MAP` gives each network its own address (setup writes it on Linux). Docker
Desktop on Windows and macOS shows every player with one address, so there the map cannot tell
them apart and everyone gets `BOMB_GAME_ADDR`; players at home then need a router that lets them
reach your public address from inside.

The shared DNS server answers home networks only. Friends over the internet need their own way
to resolve the names above to your public address.

### Matches

A match runs between the consoles: the room's host is the match master and the others link to
it. With `BOMB_RELAY_PORT=10081` (the default) they link through this server's relay, so no
console needs an open port. The server never sends UDP to a console; the consoles' UDP
heartbeats are answered over the game connection. `BOMB_RELAY_PORT=0` makes the members connect
straight to the host's TCP 10080, which only works on one LAN.

### Nickname registration

`BOMB_REQUIRE_NICKNAME=1` makes new players register a nickname, as the original service did.
The registration screen posts over TLS 1.0 to `ps2bb-pkg03.bomberman-online.com` on port 443.
Players' installs carry one certificate authority, freega's, so that certificate must be signed
by the freega CA: add `ps2bb-pkg03.bomberman-online.com` to freega's `FEEGA_TLS_NAMES` and use
freega's server certificate in a TLS 1.0 front on 443 that forwards to the web service. Port 443
cannot be shared by host name (the console sends no SNI) and this repository does not provide
that front, so the setting is off; a player's feega-ID is their name.

## Running it

- **News and FAQ** (BOMBERMAN WEB): edit `state/data/news.json` and `faq.json`, lists of
  `{"title": "...", "body": "..."}`. They are read on every request.
- **Support mail**: players write from BOMBERMAN WEB > Contact Support. Read and answer in setup's
  menu, an admin panel's inbox, or `docker compose exec web python -m bomb.support list | show N |
  reply N "text" | close N`. Set `BOMB_SUPPORT_WEBHOOK` to also post each new ticket to a Discord
  channel.
- **Announcements, lobby tips, warnings**: `docker compose exec game python -m bomb.admin`
  (`banner`, `chat`, `warn`, `tip`, `who`).
- **Backups**: `state/data` holds player ids, rankings, profiles and support mail. Accounts and
  passwords are Feega's.

## OpenLobby admin panel

The game service has an admin port for an [OpenLobby](https://github.com/PrettyOpenLobby/OpenLobby)
panel on the `openlobby` network (`http://bomb:24090`, never published). Setup offers to pair
with a panel it finds on the same machine. Without pairing, give the panel
`POL_ADMIN_GAME_BOMB_URL=http://bomb:24090` and `POL_ADMIN_GAME_BOMB_TOKEN` = your
`BOMB_ADMIN_TOKEN`.

## Development

```sh
python -m unittest discover -s tests -t .
python -m bomb.selftest_rooms        # scripted players through a whole match
python -m bomb.selftest_adminport
python -m bomb.fakeplayer --host 127.0.0.1 --name BOT --create "bot room"
```

The server is standard-library Python 3.12; each module's docstring describes its part of the
protocol, with addresses in the game's MAIN.BIN.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).

ndbomb is an independent fan project, not affiliated with or endorsed by Konami, Hudson Soft or Sony. Bomberman, Bomberman Online and Net de Bomberman are trademarks of their owners. No game data is included.
