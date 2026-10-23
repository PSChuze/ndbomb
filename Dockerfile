# The Bomberman Online (PS2) server: game and lobby, match relay, ticket check
# and the game's web pages. Which one a container runs is picked by the compose `command:`
# (python -u -m bomb.<module> ...).
#
# Standard library only: no requirements file. All mutable state lives under /data
# (profiles.json, players.json, support.json, news.json, faq.json) and logs under /logs; compose
# mounts both from the state folder. The image carries no game data, keys or certificates.
# Sign-in is the Feega service's (its own container, PSChuze/freega).
# bomb/build.json, when setup wrote one, names the release commit the image is built from.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BOMB_DATA_DIR=/data \
    BOMB_LOG_DIR=/logs

WORKDIR /app
COPY bomb /app/bomb

# Prove the package imports and the room self-test passes in the image's own Python. The
# self-test uses its own temp data and log dirs, so nothing is baked in.
RUN python -m bomb.selftest_rooms \
    && find /app -name __pycache__ -prune -exec rm -rf {} +

VOLUME ["/data", "/logs"]

# web 80 (patch check, BOMBERMAN WEB), ticket 9901, game 10020 (tcp + udp), match relay 10081
# (in the game process).
EXPOSE 80 9901 10020 10020/udp 10081

CMD ["python", "-u", "-m", "bomb.game"]
