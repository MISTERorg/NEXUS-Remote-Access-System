"""
nexus_agent_entry.py
---------------------
Entry point for nexus_agent.exe.
"""
from __future__ import annotations
import os, sys, argparse
from pathlib import Path

if sys.platform == "win32":
    os.system("color")

def _c(code, t): return f"\033[{code}m{t}\033[0m"

def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--relay", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--name",  default=None)
    known, _ = parser.parse_known_args()

    relay = known.relay
    token = known.token
    name  = known.name

    print(_c("1;96", """
  ███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗
  Remote Support Agent — Connecting to your helper
"""))

    if not relay:
        print(_c("93", "  Paste the Relay URL your helper sent you:"))
        print(_c("2",  "  (looks like: ws://102.45.67.89:7000)"))
        relay = input("  Relay URL > ").strip()

    if not token:
        print(_c("93", "\n  Paste the Agent Token your helper sent you:"))
        token = input("  Token     > ").strip()

    if not relay or not token:
        print(_c("91", "\n  ✗  Relay URL and Token are both required. Closing."))
        input("  Press Enter to exit...")
        sys.exit(1)

    sys.argv = ["connect_remote.py", "--relay", relay, "--token", token]
    if name:
        sys.argv += ["--name", name]

    base = Path(__file__).resolve().parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from connect_remote import main as agent_main
    agent_main()

if __name__ == "__main__":
    main()
