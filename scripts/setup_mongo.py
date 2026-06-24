"""MongoDB Community Edition setup helper.

Checks whether a local MongoDB instance is reachable on the default port.
If not, prints platform-specific installation instructions and optionally
runs the install command (Windows: winget, Linux/Mac: package manager).

Usage:
    python scripts/setup_mongo.py [--install]

    --install   attempt automated installation (Windows winget / Linux apt)
"""
from __future__ import annotations

import argparse
import platform
import socket
import subprocess
import sys

MONGO_URI = "mongodb://localhost:27017"
MONGO_PORT = 27017
MONGO_HOST = "localhost"


def _is_mongo_running() -> bool:
    """Quick TCP probe — returns True if something listens on 27017."""
    try:
        with socket.create_connection((MONGO_HOST, MONGO_PORT), timeout=2):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def _try_pymongo() -> str | None:
    """Return server version string via pymongo, or None if unavailable."""
    try:
        import pymongo  # type: ignore[import]
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        info = client.server_info()
        return info.get("version", "?")
    except Exception:
        return None


WINDOWS_INSTRUCTIONS = """\
  Option A — winget (recommended, requires Windows 10 1709+):
      winget install MongoDB.Server

  Option B — manual installer:
      https://www.mongodb.com/try/download/community
      → MSI package → "Complete" install → tick "Install MongoDB as a Service"

  Start / stop the service:
      net start MongoDB
      net stop MongoDB
"""

LINUX_INSTRUCTIONS = """\
  Ubuntu / Debian (MongoDB 7.0):
      sudo apt-get install -y gnupg curl
      curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc \\
        | sudo gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor
      echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] \\
        https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multirelease" \\
        | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list
      sudo apt-get update && sudo apt-get install -y mongodb-org
      sudo systemctl start mongod && sudo systemctl enable mongod

  RHEL / Fedora:
      sudo dnf install -y mongodb-org   # add the official repo first:
      # https://www.mongodb.com/docs/manual/tutorial/install-mongodb-on-red-hat/

  macOS (Homebrew):
      brew tap mongodb/brew
      brew install mongodb-community
      brew services start mongodb-community
"""


def _install_windows() -> int:
    print("Running: winget install MongoDB.Server")
    result = subprocess.run(["winget", "install", "MongoDB.Server"], check=False)
    return result.returncode


def _install_linux() -> int:
    print(
        "Auto-install assumes Ubuntu/Debian with apt. "
        "For other distros, follow the instructions above."
    )
    cmds = [
        ["sudo", "apt-get", "install", "-y", "gnupg", "curl"],
        [
            "bash", "-c",
            "curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc "
            "| sudo gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor",
        ],
        [
            "bash", "-c",
            'echo \'deb [ signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] '
            'https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multirelease\' '
            '| sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list',
        ],
        ["sudo", "apt-get", "update"],
        ["sudo", "apt-get", "install", "-y", "mongodb-org"],
        ["sudo", "systemctl", "start", "mongod"],
        ["sudo", "systemctl", "enable", "mongod"],
    ]
    for cmd in cmds:
        print(f"  $ {' '.join(cmd)}")
        r = subprocess.run(cmd, check=False)
        if r.returncode != 0:
            print(f"  [!] command failed (exit {r.returncode})")
            return r.returncode
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install", action="store_true",
        help="attempt automated installation for this platform",
    )
    args = parser.parse_args()

    os_name = platform.system()  # 'Windows', 'Linux', 'Darwin'
    print(f"Platform: {os_name} ({platform.release()})")
    print(f"Checking {MONGO_URI} …", end=" ", flush=True)

    if _is_mongo_running():
        version = _try_pymongo()
        if version:
            print(f"✓  MongoDB {version} is running.")
        else:
            print("✓  Port 27017 is open (install pymongo for version info).")
        print(f"\nConnection URI:  {MONGO_URI}")
        print("Copy this into the app Settings → MongoDB URI field.")
        sys.exit(0)

    print("✗  not reachable.\n")

    if os_name == "Windows":
        print("Installation instructions (Windows):\n")
        print(WINDOWS_INSTRUCTIONS)
    else:
        print("Installation instructions (Linux / macOS):\n")
        print(LINUX_INSTRUCTIONS)

    print(f"After installation, your URI will be:  {MONGO_URI}")
    print("Run this script again to verify, then paste the URI into app Settings.")

    if args.install:
        print("\n--- Attempting automated install ---")
        if os_name == "Windows":
            rc = _install_windows()
        elif os_name == "Linux":
            rc = _install_linux()
        else:
            print("Auto-install is not supported on macOS. Use Homebrew (see above).")
            rc = 1
        if rc == 0:
            print("\n✓  Installation succeeded. Re-run without --install to verify.")
        else:
            print(f"\n✗  Installation exited with code {rc}. Check the output above.")
        sys.exit(rc)


if __name__ == "__main__":
    main()
