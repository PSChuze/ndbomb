"""Build the Bomberman Server Setup program: python packaging/build_setup.py [--version v1.2.3]

One file made with PyInstaller (pip install pyinstaller==6.11.1), written to
dist/. It carries this repository's committed files as server.zip and unpacks
them into the host's server folder on its first run, so a host needs only the
program and Docker. Build each platform's program on that platform.

server-build.json beside it names the commit and the build time; setup writes them to
bomb/build.json in the server folder, so the server knows which release it runs (a git archive
has no .git) and the admin panel can say when a newer one is out.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = {"win32": "Bomberman-Server-Setup", "darwin": "Bomberman-Server-Setup",
         "linux": "Bomberman-Server-Setup-linux-x86_64"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="dev", help="the release tag the program reports and updates from")
    ap.add_argument("--commit", help="the commit packed (default: git rev-parse HEAD)")
    args = ap.parse_args()
    commit = args.commit or subprocess.run(["git", "-C", HERE, "rev-parse", "HEAD"], check=True,
                                           capture_output=True, text=True).stdout.strip()
    work = os.path.join(HERE, "build", "setup")
    os.makedirs(work, exist_ok=True)
    archive = os.path.join(work, "server.zip")
    subprocess.run(["git", "-C", HERE, "archive", "--format=zip", "--prefix=ndbomb/", "-o", archive, "HEAD"],
                   check=True)
    version = os.path.join(work, "server-version.txt")
    with open(version, "w", encoding="utf-8") as f:
        f.write(args.version + "\n")
    build = os.path.join(work, "server-build.json")
    with open(build, "w", encoding="utf-8") as f:
        json.dump({"commit": commit, "tag": args.version,
                   "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, f)
        f.write("\n")
    name = NAMES.get(sys.platform, NAMES["linux"])
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
                    "--name", name, "--distpath", os.path.join(HERE, "dist"), "--workpath", os.path.join(work, "work"),
                    "--specpath", work, "--add-data", archive + os.pathsep + ".",
                    "--add-data", version + os.pathsep + ".", "--add-data", build + os.pathsep + ".",
                    "--paths", HERE,
                    os.path.join(HERE, "setup.py")], check=True, cwd=HERE)
    print(os.path.join(HERE, "dist", name + (".exe" if sys.platform == "win32" else "")))


if __name__ == "__main__":
    main()
