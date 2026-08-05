"""
setup_and_launch.py
--------------------
NEXUS One-Click Setup & Automated Launcher.

Fully automated connection fixes included:
  1. Windows Firewall Rule Creation (Inbound TCP 7000 & 8080)
  2. Router UPnP Port Forwarding Discovery
  3. Automatic Reverse SSH Tunneling (CGNAT / NAT Bypass)
  4. Multi-IP LAN + WAN + Tunnel Config Generation
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

# ── 0. Windows DLL Path Resolution & Preload ──────────────────────────────────
if sys.platform == "win32":
    dll_candidates = []

    if getattr(sys, "frozen", False):
        bundle_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        dll_candidates.extend([
            bundle_dir,
            bundle_dir / "dlls",
            bundle_dir / "Library" / "bin",
            Path(sys.executable).parent,
            Path(sys.executable).parent / "DLLs",
        ])

    dll_candidates.extend([
        Path(sys.prefix) / "DLLs",
        Path(sys.prefix) / "Library" / "bin",
        Path(sys.prefix),
        Path(getattr(sys, "base_prefix", sys.prefix)) / "DLLs",
        Path(getattr(sys, "base_prefix", sys.prefix)) / "Library" / "bin",
        Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32",
    ])

    for p in dll_candidates:
        if p.exists() and p.is_dir():
            p_str = str(p.resolve())
            if p_str not in os.environ.get("PATH", ""):
                os.environ["PATH"] = p_str + os.path.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(p_str)
                except Exception:
                    pass

    try:
        import ctypes
    except Exception:
        for p in dll_candidates:
            if not p.exists():
                continue
            for ffi_name in ("libffi-8.dll", "libffi-7.dll", "libffi.dll", "ffi.dll"):
                ffi_path = p / ffi_name
                if ffi_path.exists():
                    try:
                        ctypes.CDLL(str(ffi_path))
                    except Exception:
                        pass


# ── 1. Path & Import Setup ───────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    BASE_DIR   = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR)).resolve()
else:
    BASE_DIR   = Path(__file__).resolve().parent
    BUNDLE_DIR = BASE_DIR

ENV_FILE   = BASE_DIR / ".env"
STATE_FILE = BASE_DIR / ".nexus_state.json"
CERTS_DIR  = BASE_DIR / "certs"

DASHBOARD_HTML_CANDIDATES = [
    BASE_DIR / "dashboard.html",
    BASE_DIR / "ui" / "dashboard.html",
    BASE_DIR / "ui" / "console.html",
    BUNDLE_DIR / "dashboard.html",
    BUNDLE_DIR / "ui" / "dashboard.html",
    BUNDLE_DIR / "ui" / "console.html",
]

def find_dashboard_html() -> Path | None:
    for candidate in DASHBOARD_HTML_CANDIDATES:
        if candidate.exists():
            return candidate
    return None

for path_str in (str(BASE_DIR), str(BUNDLE_DIR)):
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

# ── Console Formatting Utilities ──────────────────────────────────────────────
if sys.platform == "win32":
    os.system("color")

def _c(code, text): return f"\033[{code}m{text}\033[0m"
def ok(m):   print(_c("92", f"  ✓  {m}"))
def info(m): print(_c("96", f"  →  {m}"))
def warn(m): print(_c("93", f"  ⚠  {m}"))
def err(m):  print(_c("91", f"  ✗  {m}"))
def hdr(m):  print(_c("1;96", f"\n{'─'*54}\n  {m}\n{'─'*54}"))
def dim(m):  print(_c("2",  f"     {m}"))


# ════════════════════════════════════════════════════════════════════════════
# AUTOMATION SOLUTION 1 — Windows Firewall Management
# ════════════════════════════════════════════════════════════════════════════

def auto_configure_windows_firewall(ports: list[int]) -> None:
    """Automatically adds Windows Firewall inbound rules for specified ports."""
    if sys.platform != "win32":
        return

    hdr("Automated Network Setup — Windows Firewall")
    for port in ports:
        rule_name = f"NEXUS_Inbound_Port_{port}"
        info(f"Checking Windows Firewall for port {port}...")

        # Check if rule exists
        chk = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule_name}"],
            capture_output=True, text=True
        )

        if "No rules match" in chk.stdout or chk.returncode != 0:
            info(f"Adding inbound rule for TCP port {port}...")
            cmd = [
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={rule_name}", "dir=in", "action=allow",
                "protocol=TCP", f"localport={port}"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                ok(f"Firewall rule added successfully for port {port}")
            else:
                warn(f"Could not automatically add firewall rule for port {port} (Requires Admin)")
                dim("If remote connection fails on LAN, run CMD as Administrator and execute:")
                dim(f"netsh advfirewall firewall add rule name={rule_name} dir=in action=allow protocol=TCP localport={port}")
        else:
            ok(f"Firewall rule for port {port} already exists")


# ════════════════════════════════════════════════════════════════════════════
# AUTOMATION SOLUTION 2 — UPnP Automatic Router Port Forwarding
# ════════════════════════════════════════════════════════════════════════════

def auto_setup_upnp_port_mapping(port: int) -> bool:
    """Attempts UPnP SSDP discovery and SOAP port mapping on local gateway router."""
    hdr("Automated Network Setup — Router UPnP Port Forwarding")
    info(f"Discovering UPnP gateway router for port {port}...")

    ssdp_request = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n"
    )

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(2.5)
        sock.sendto(ssdp_request.encode(), ("239.255.255.250", 1900))

        location_url = None
        while True:
            try:
                data, _ = sock.recvfrom(2048)
                resp = data.decode(errors="ignore")
                for line in resp.splitlines():
                    if line.lower().startswith("location:"):
                        location_url = line.split(":", 1)[1].strip()
                        break
                if location_url:
                    break
            except socket.timeout:
                break
        sock.close()

        if not location_url:
            warn("UPnP router discovery timed out (UPnP may be disabled on router)")
            return False

        info(f"Found Router UPnP endpoint: {location_url}")
        req = urllib.request.Request(location_url)
        with urllib.request.urlopen(req, timeout=3) as response:
            xml_data = response.read().decode()

        # Extract control URL
        control_match = re.search(r"<controlURL>(.*?)</controlURL>", xml_data, re.IGNORECASE)
        if not control_match:
            warn("Could not find UPnP control URL in router description")
            return False

        control_path = control_match.group(1)
        parsed_loc = urlparse(location_url)
        control_url = f"{parsed_loc.scheme}://{parsed_loc.netloc}{control_path}"

        local_ip = get_lan_ip()
        soap_body = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:AddPortMapping xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">
      <NewRemoteHost></NewRemoteHost>
      <NewExternalPort>{port}</NewExternalPort>
      <NewProtocol>TCP</NewProtocol>
      <NewInternalPort>{port}</NewInternalPort>
      <NewInternalClient>{local_ip}</NewInternalClient>
      <NewEnabled>1</NewEnabled>
      <NewPortMappingDescription>NEXUS Relay</NewPortMappingDescription>
      <NewLeaseDuration>0</NewLeaseDuration>
    </u:AddPortMapping>
  </s:Body>
</s:Envelope>"""

        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": '"urn:schemas-upnp-org:service:WANIPConnection:1#AddPortMapping"',
        }

        post_req = urllib.request.Request(control_url, data=soap_body.encode("utf-8"), headers=headers)
        with urllib.request.urlopen(post_req, timeout=4) as post_resp:
            if post_resp.status in (200, 201):
                ok(f"UPnP Port Forwarding mapped successfully! ({port} -> {local_ip}:{port})")
                return True
    except Exception as e:
        warn(f"UPnP port mapping attempt finished: {e}")
    return False


# ════════════════════════════════════════════════════════════════════════════
# AUTOMATION SOLUTION 3 — Automatic Internet Reverse SSH Tunneling
# ════════════════════════════════════════════════════════════════════════════

GLOBAL_TUNNEL_PROCS: list[subprocess.Popen] = []

def auto_start_public_tunnel(
    local_port: int, label: str = "service", attempts: int = 3, timeout: float = 20.0
) -> str | None:
    """
    Spawns an automated reverse SSH tunnel to create a public WSS address.
    Requires no router configuration and bypasses CGNAT completely — this
    is the correct default path, not a fallback, for anyone whose ISP puts
    them behind carrier-grade NAT (a raw "public" IP in that situation
    isn't actually routed to your router at all; no amount of port
    forwarding or firewall configuration can fix that, only an
    outbound-initiated tunnel like this one can).

    IMPORTANT — this must be called AFTER whatever's listening on
    `local_port` is actually up. pinggy establishes the reverse forward
    immediately; if nothing is listening yet, the tunnel URL it hands back
    will accept connections and then immediately fail them. The caller
    (async_main) is responsible for this ordering.

    Retries `attempts` times with a fixed per-attempt `timeout` — a single
    10s attempt (the old default) isn't generous enough for pinggy's
    negotiation over a slow or congested link, and a transient failure
    here shouldn't take down the only NAT-traversal path that works
    regardless of router config or CGNAT.
    """
    hdr(f"Automated Network Setup — Public Tunnel ({label})")

    for attempt in range(1, attempts + 1):
        info(f"Establishing zero-config public internet tunnel via SSH... (attempt {attempt}/{attempts})")
        tunnel_url = _try_start_tunnel_once(local_port, timeout)
        if tunnel_url:
            if _verify_tunnel_reachable(tunnel_url):
                ok(f"Public Tunnel Active ({label}): {tunnel_url}")
                return tunnel_url
            warn(f"Tunnel URL was created but didn't respond to a connection test: {tunnel_url}")
            warn("This usually means the local service wasn't listening yet, or the tunnel")
            warn("dropped immediately after connecting. Retrying...")
        if attempt < attempts:
            time.sleep(2)

    warn(f"Could not establish a working public tunnel for {label} after {attempts} attempts.")
    return None


def _try_start_tunnel_once(local_port: int, timeout: float) -> str | None:
    cmd = [
        "ssh",
        # Never prompt interactively — if auth fails, fail immediately
        # with an error message rather than hanging on a password prompt.
        # This was the root cause of the screenshot issue: the process
        # was blocking on `qr@a.pinggy.io's password:` at the tty while
        # our stdout-reading loop timed out without ever seeing the URL.
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        # pinggy's free/anonymous tunnel requires no auth (SSH public key
        # or nothing) — if the server asks for a password, we want an
        # instant failure rather than a 20-second hang.
        "-o", "PasswordAuthentication=no",
        "-o", "ServerAliveInterval=15",
        "-o", "ConnectTimeout=10",
        "-p", "443",
        f"-R0:localhost:{local_port}",
        "qr@a.pinggy.io"
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,   # cut off the tty entirely
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,     # capture separately so we can
            text=True,                  # merge both streams below
            bufsize=1,
        )
        GLOBAL_TUNNEL_PROCS.append(proc)

        import selectors
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
        sel.register(proc.stderr, selectors.EVENT_READ, "stderr")

        start_time = time.time()
        tunnel_url = None
        all_output = []

        while time.time() - start_time < timeout:
            if proc.poll() is not None:
                # drain remaining output before giving up
                for stream in (proc.stdout, proc.stderr):
                    try:
                        all_output.append(stream.read())
                    except Exception:
                        pass
                break

            ready = sel.select(timeout=0.2)
            for key, _ in ready:
                line = key.fileobj.readline()
                if not line:
                    continue
                all_output.append(line)
                dim(f"  [tunnel] {line.rstrip()}")

                # pinggy prints its URL on stderr in the format:
                #   https://XXXX.a.pinggy.io     (HTTPS tunnel URL)
                #   tcp://XXXX.a.pinggy.io:PORT   (TCP tunnel URL)
                # Some versions also print it on stdout. We watch both.
                m = re.search(
                    r"(https?://[a-zA-Z0-9\.\-]+\.(?:pinggy\.io|pinggy\.link)"
                    r"|tcp://[a-zA-Z0-9\.\-]+\.(?:pinggy\.io|pinggy\.link):[0-9]+)",
                    line,
                )
                if m:
                    raw_url = m.group(1)
                    if raw_url.startswith("https://"):
                        tunnel_url = raw_url.replace("https://", "wss://")
                    elif raw_url.startswith("http://"):
                        tunnel_url = raw_url.replace("http://", "ws://")
                    elif raw_url.startswith("tcp://"):
                        tunnel_url = raw_url.replace("tcp://", "wss://")
                    if tunnel_url:
                        break

            if tunnel_url:
                break

        sel.close()

        if tunnel_url:
            return tunnel_url

        # Give a helpful diagnostic — show what pinggy actually printed so
        # the user can see if it's an auth problem, a network block, etc.
        joined = "".join(all_output).strip()
        if "Permission denied" in joined or "password" in joined.lower():
            warn("SSH auth failed — pinggy rejected the connection.")
            warn("This usually means your SSH public key needs to be set up.")
            warn("Run: ssh-keygen  (accept defaults) then retry.")
        elif "Connection refused" in joined or "connect to host" in joined.lower():
            warn("Could not reach a.pinggy.io:443 — check your internet/firewall.")
        elif joined:
            warn("Tunnel connected but no URL was found in pinggy's output. Raw output:")
            for line in joined.splitlines()[:10]:
                dim(f"  {line}")
        else:
            warn("Public SSH tunnel produced no output within timeout.")

    except FileNotFoundError:
        warn("SSH client not found in PATH — skipping automated tunnel creation.")
        warn("Install OpenSSH:  Windows: winget install Microsoft.OpenSSH.Client")
        warn("                  Linux: sudo apt install openssh-client")
    except Exception as e:
        warn(f"Tunnel creation failed: {e}")

    return None


def _verify_tunnel_reachable(tunnel_url: str) -> bool:
    """
    Best-effort confirmation that the tunnel actually round-trips, rather
    than trusting "pinggy printed a URL" as proof it works. Catches
    exactly the ordering bug this function's docstring warns about: a
    tunnel established before the local service was listening will still
    print a URL, but nothing will be on the other end of it.

    A plain TCP connect, not a full WS handshake — cheap, and sufficient
    to distinguish "something is listening end-to-end" from "connection
    refused/timed out". Never raises; treat False as "couldn't confirm",
    not a hard failure — the tunnel may still work even if this advisory
    check itself is blocked by a restrictive local network.
    """
    try:
        parsed = urlparse(tunnel_url.replace("wss://", "https://").replace("ws://", "http://"))
        host = parsed.hostname
        port = parsed.port or 443
        if not host:
            return False
        with socket.create_connection((host, port), timeout=6):
            return True
    except Exception as e:
        dim(f"Tunnel reachability check inconclusive: {e}")
        return False


# ════════════════════════════════════════════════════════════════════════════
# IP Discovery Utilities
# ════════════════════════════════════════════════════════════════════════════

def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_public_ip() -> str:
    """
    Discover the machine's public/WAN-facing IP.

    Checks NEXUS_PUBLIC_IP first — set this explicitly if this host has no
    direct internet egress to the lookup services below (corporate proxy,
    outbound firewall, air-gapped test network, etc.), or if the discovered
    IP isn't the one that actually routes inbound traffic to this machine
    (e.g. behind a NAT you've manually port-forwarded on a router with a
    different public IP than what a plain outbound request would reveal).

    IMPORTANT: if every lookup service fails AND no override is set, this
    falls back to get_lan_ip() — meaning the "public" IP becomes a private
    RFC1918 address that is NOT reachable from outside the LAN. Callers
    (run_setup, print_and_save_instructions) are responsible for detecting
    that condition (public_ip == lan_ip) and warning the user loudly; this
    function only logs why each individual lookup attempt failed.
    """
    override = os.environ.get("NEXUS_PUBLIC_IP", "").strip()
    if override:
        if _valid_ip(override):
            ok(f"Using NEXUS_PUBLIC_IP override: {override}")
            return override
        warn(f"NEXUS_PUBLIC_IP is set but not a valid IPv4 address ({override!r}) — ignoring it")

    services = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://checkip.amazonaws.com",
        "https://icanhazip.com",
    ]
    for url in services:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                ip = resp.read().decode().strip()
                if _valid_ip(ip):
                    return ip
                dim(f"Public IP lookup via {url} returned an unparseable response: {ip!r}")
        except Exception as e:
            dim(f"Public IP lookup via {url} failed: {e}")
            continue

    warn("Could not reach any public IP lookup service — this host may have no direct")
    warn("internet egress (proxy/firewall/air-gapped network). Falling back to LAN IP,")
    warn("which will NOT be reachable from outside this network. Set NEXUS_PUBLIC_IP")
    warn("manually to fix this, or rely on the tunnel URL if one gets created below.")
    return get_lan_ip()


def _valid_ip(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def _wan_ip_is_fallback(public_ip: str, lan_ip: str) -> bool:
    """
    True when get_public_ip() had to degrade to the LAN IP (every external
    lookup service failed and no NEXUS_PUBLIC_IP override was set). Public
    and LAN IPs occupy disjoint address spaces in practice, so an exact
    string match here is a reliable signal that the fallback happened, not
    a coincidence.
    """
    return public_ip == lan_ip


# ════════════════════════════════════════════════════════════════════════════
# Setup, Certs, and Config Writing
# ════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def write_env(public_ip: str, lan_ip: str, agent_token: str, jwt_secret: str,
              relay_port: int, api_port: int) -> None:
    content = f"""# NEXUS Remote Access — auto-generated
NEXUS_ENVIRONMENT=development
NEXUS_DEBUG=true
NEXUS_APP_NAME=NEXUS Remote Access

NEXUS_AUTH_SECRET_KEY={jwt_secret}
NEXUS_AUTH_MFA_ENABLED=false
NEXUS_AUTH_MAX_LOGIN_ATTEMPTS=10
NEXUS_AUTH_ALLOWED_ORIGINS=*

NEXUS_RELAY_HOST=0.0.0.0
NEXUS_RELAY_PORT={relay_port}
NEXUS_RELAY_HEARTBEAT_INTERVAL=30
NEXUS_RELAY_SESSION_TIMEOUT=7200

NEXUS_TLS_REQUIRE_CLIENT_CERT=false

NEXUS_AGENT_SCREEN_FPS=15
NEXUS_AGENT_SCREEN_QUALITY=75
NEXUS_AGENT_RECONNECT_MAX_RETRIES=20

NEXUS_LOG_LEVEL=INFO
NEXUS_LOG_FORMAT=console
"""
    ENV_FILE.write_text(content, encoding="utf-8")
    os.environ["NEXUS_AUTH_SECRET_KEY"] = jwt_secret
    os.environ["NEXUS_RELAY_PORT"] = str(relay_port)
    os.environ["NEXUS_RELAY_HOST"] = "0.0.0.0"


def generate_certs() -> None:
    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    cert_file = CERTS_DIR / "nexus-relay-server.crt"
    key_file  = CERTS_DIR / "nexus-relay-server.key"

    if cert_file.exists() and key_file.exists():
        ok("Certificates present")
        return

    info("Generating TLS certificates...")
    def _sync(c, k):
        cert_file.write_bytes(c)
        key_file.write_bytes(k)
        (CERTS_DIR / "relay.crt").write_bytes(c)
        (CERTS_DIR / "relay.key").write_bytes(k)

    try:
        import ipaddress
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nexus.local")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName("nexus.local"),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        kb = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
        cb = cert.public_bytes(serialization.Encoding.PEM)
        _sync(cb, kb)
        ok("Self-signed TLS certificates generated")
    except Exception as e:
        warn(f"Certificate generation notice: {e}")


def run_setup(relay_port: int, api_port: int, force_reset: bool) -> dict:
    state = load_state()

    if ENV_FILE.exists() and not force_reset and state.get("agent_token"):
        hdr("Existing Config Detected")
        cached_public_ip = state.get("public_ip", "?")
        cached_lan_ip = state.get("lan_ip", "?")
        ok(f"Public WAN IP : {cached_public_ip}")
        ok(f"Local LAN IP  : {cached_lan_ip}")
        ok(f"Agent Token   : {state['agent_token'][:20]}…")
        if _wan_ip_is_fallback(cached_public_ip, cached_lan_ip):
            warn("This cached config has WAN IP == LAN IP from a previous run where public")
            warn("IP discovery failed. It will keep being reused as-is until you run with")
            warn("--reset (or set NEXUS_PUBLIC_IP and then --reset) to rediscover it.")
        generate_certs()
        return state

    hdr("Step 1 — Network IP Discovery")
    lan_ip = get_lan_ip()
    public_ip = get_public_ip()
    ok(f"Local LAN IP  : {lan_ip}")
    if _wan_ip_is_fallback(public_ip, lan_ip):
        warn(f"Public WAN IP : {public_ip}  (fallback — see warning above)")
    else:
        ok(f"Public WAN IP : {public_ip}")

    hdr("Step 2 — Generating Credentials")
    agent_token = secrets.token_urlsafe(32)
    jwt_secret  = secrets.token_urlsafe(48)
    ok(f"Agent token : {agent_token[:20]}…")

    hdr("Step 3 — Environment Configuration")
    write_env(public_ip, lan_ip, agent_token, jwt_secret, relay_port, api_port)

    state = {
        "public_ip": public_ip,
        "lan_ip": lan_ip,
        "agent_token": agent_token,
        "jwt_secret": jwt_secret,
        "relay_port": relay_port,
        "api_port": api_port
    }
    save_state(state)
    generate_certs()
    return state


# ════════════════════════════════════════════════════════════════════════════
# Service Launchers
# ════════════════════════════════════════════════════════════════════════════

def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


async def wait_for_port_async(port: int, timeout: int = 15, label: str = "") -> bool:
    for i in range(timeout):
        if port_in_use(port):
            ok(f"{label or f'Port {port}'} is online")
            return True
        await asyncio.sleep(1)
    return False


async def start_relay(relay_port: int, plain_tunnel_port: int = 0):
    from core.relay import RelayServer
    from config.settings import settings

    settings.relay.host = "0.0.0.0"
    settings.relay.port = relay_port
    settings.relay.plain_tunnel_port = plain_tunnel_port

    cert_file = CERTS_DIR / "nexus-relay-server.crt"
    key_file  = CERTS_DIR / "nexus-relay-server.key"
    if not cert_file.exists():
        cert_file = CERTS_DIR / "relay.crt"
        key_file  = CERTS_DIR / "relay.key"

    if hasattr(settings, "tls"):
        tls_cfg = settings.tls
        if cert_file.exists() and key_file.exists():
            tls_cfg.cert_file = cert_file
            tls_cfg.key_file = key_file
            tls_cfg.require_client_cert = False

    relay = RelayServer()
    try:
        await relay.start()
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        if hasattr(relay, "stop") and callable(relay.stop):
            await relay.stop()
        raise


async def start_dashboard(api_port: int):
    import uvicorn
    from ui.dashboard import app
    config = uvicorn.Config(app, host="0.0.0.0", port=api_port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


# ════════════════════════════════════════════════════════════════════════════
# Config Instructions Generator
# ════════════════════════════════════════════════════════════════════════════

def print_and_save_instructions(
    state: dict, relay_tunnel_url: str | None, dashboard_tunnel_url: str | None
) -> None:
    public_ip   = state.get("public_ip", "127.0.0.1")
    lan_ip      = state.get("lan_ip", "127.0.0.1")
    agent_token = state.get("agent_token", "")
    relay_port  = state.get("relay_port", 7000)
    api_port    = state.get("api_port", 8080)

    proto = "wss" if (CERTS_DIR / "nexus-relay-server.crt").exists() else "ws"

    wan_broken = _wan_ip_is_fallback(public_ip, lan_ip)

    lan_cmd    = f"python connect_remote.py --relay {proto}://{lan_ip}:{relay_port} --token {agent_token}"
    wan_cmd    = f"python connect_remote.py --relay {proto}://{public_ip}:{relay_port} --token {agent_token}"
    tunnel_cmd = f"python connect_remote.py --relay {relay_tunnel_url} --token {agent_token}" if relay_tunnel_url else None

    # The tunnel is now the DEFAULT recommendation, not a fallback for when
    # the direct WAN path fails. Reasoning: a "public" IP from a plain
    # outbound lookup is not proof of inbound reachability — firewalls,
    # missing port forwards, and (increasingly common) ISP-level carrier-
    # grade NAT can all make that address unreachable from outside even
    # though it printed a real-looking number. The tunnel is outbound-
    # initiated, so none of those three things can block it. The direct
    # WAN command is kept below as an advanced/manual option for people
    # who have confirmed port forwarding works and want to skip the
    # tunnel hop for latency reasons — not as the thing to try first.
    if tunnel_cmd:
        primary_cmd = tunnel_cmd
    elif wan_broken:
        primary_cmd = lan_cmd
    else:
        primary_cmd = wan_cmd

    hdr("HOW TO CONNECT THE REMOTE PC")

    if not tunnel_cmd:
        warn("No public tunnel could be established this run (check --no-tunnel, whether")
        warn("an SSH client is available, or your outbound internet connectivity).")
        if wan_broken:
            warn("Public WAN IP also could not be determined, so the command below will")
            warn("only work for a machine already on this LAN.")
        else:
            warn("Falling back to the direct WAN command below — this REQUIRES port")
            warn("forwarding on your router and will NOT work if your ISP uses carrier-")
            warn("grade NAT (very common on residential/mobile connections). If the")
            warn("remote PC can't connect, that's almost certainly why — re-run this")
            warn("script to retry the tunnel rather than debugging router settings.")

    print(f"""
  {_c('1', 'Command to run on the Remote PC:')}

  {_c('1;92', primary_cmd)}

  {_c('2', '(This command and fallback URLs have been auto-saved to SEND_TO_REMOTE_PC.txt)')}
""")

    print(_c("1;96", "  ╔════════════════════════════════════════════════════════════════════╗"))
    if relay_tunnel_url:
        print(_c("1;92", f"  ║  Relay (Public, recommended) : {relay_tunnel_url}"))
    print(_c("1;96", f"  ║  Relay (Same LAN only)       : {proto}://{lan_ip}:{relay_port}"))
    if not relay_tunnel_url:
        tag = "  [BROKEN — same as LAN IP]" if wan_broken else "  [requires port forwarding — untested]"
        color = "1;91" if wan_broken else "1;93"
        print(_c(color, f"  ║  Relay (Direct WAN, advanced): {proto}://{public_ip}:{relay_port}{tag}"))
    print(_c("1;93", f"  ║  Agent Token                 : {agent_token}"))
    print(_c("1;96", "  ╠════════════════════════════════════════════════════════════════════╣"))
    print(_c("1;96", f"  ║  Dashboard (this machine)    : http://localhost:{api_port}/console"))
    if dashboard_tunnel_url:
        dash_public = dashboard_tunnel_url.replace("wss://", "https://").replace("ws://", "http://") + "/console"
        print(_c("1;92", f"  ║  Dashboard (from anywhere)   : {dash_public}"))
    print(_c("1;96", "  ╚════════════════════════════════════════════════════════════════════╝"))
    print()

    if relay_tunnel_url or dashboard_tunnel_url:
        warn("Note: tunnel URLs above are freshly generated and WILL be different the")
        warn("next time you restart this script — that's inherent to the free anonymous")
        warn("tunnel service being used, not something wrong with your setup. For a")
        warn("permanent address that never changes, see README.md's section on that.")

    print(_c("2", "  A failed `ping` to any of these addresses does NOT necessarily mean the"))
    print(_c("2", "  connection is broken — many networks block ICMP ping while still allowing"))
    print(_c("2", "  the actual WebSocket connection through. Test with the connect command"))
    print(_c("2", "  itself, not ping."))
    print()

    snippet_file = BASE_DIR / "SEND_TO_REMOTE_PC.txt"
    content = (
        f"NEXUS Remote Support — Connection Configuration\n"
        f"{'='*60}\n\n"
        f"AUTOMATED ONE-LINE RUN COMMAND:\n"
        f"  {primary_cmd}\n\n"
        f"{'='*60}\n"
        f"DETAILED CONNECTION ENDPOINTS:\n"
    )
    if relay_tunnel_url:
        content += f"Relay URL (public, recommended) : {relay_tunnel_url}\n"
    content += (
        f"Relay URL (same LAN only)       : {proto}://{lan_ip}:{relay_port}\n"
        f"Relay URL (direct WAN, advanced): {proto}://{public_ip}:{relay_port}\n"
        f"Agent Token                     : {agent_token}\n\n"
        f"Dashboard (this machine)        : http://localhost:{api_port}/console\n"
    )
    if dashboard_tunnel_url:
        dash_public = dashboard_tunnel_url.replace("wss://", "https://").replace("ws://", "http://") + "/console"
        content += f"Dashboard (from anywhere)       : {dash_public}\n"
    if not relay_tunnel_url:
        content += (
            f"\n"
            f"NOTE: No public tunnel was established this run, so the direct WAN URL\n"
            f"above requires port forwarding on your router and will NOT work if your\n"
            f"ISP uses carrier-grade NAT. Re-run setup_and_launch.py to retry the tunnel.\n"
        )
    if relay_tunnel_url or dashboard_tunnel_url:
        content += (
            f"\n"
            f"NOTE: The public URL(s) above are freshly generated and will change the\n"
            f"next time this script restarts — see README.md for how to get a permanent\n"
            f"address instead.\n"
        )
    content += (
        f"\n"
        f"NOTE: A failed `ping` to any address above does not prove the connection is\n"
        f"broken — many networks block ICMP while still allowing the real connection\n"
        f"through. Test with the run command itself, not ping.\n"
    )
    snippet_file.write_text(content, encoding="utf-8")
    ok("Instruction file updated → SEND_TO_REMOTE_PC.txt")


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

async def async_main(args):
    RELAY_PORT = args.port_relay
    API_PORT   = args.port_api

    print(_c("1;96", """
  ███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗
  ████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝
  ██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗
  ██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║
  ██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║
  ╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝
  Remote Access System — Automated Setup & Launcher
"""))

    # Automated Fixes Execution
    if not args.no_firewall:
        auto_configure_windows_firewall([RELAY_PORT, API_PORT])

    if not args.no_upnp:
        auto_setup_upnp_port_mapping(RELAY_PORT)
        auto_setup_upnp_port_mapping(API_PORT)

    state = run_setup(RELAY_PORT, API_PORT, force_reset=args.reset)

    # Plain (non-TLS), loopback-only port the relay also listens on
    # specifically for the reverse tunnel to target — see the
    # plain_tunnel_port docstring in config/settings.py. Picked to avoid
    # colliding with the dashboard's API_PORT.
    plain_tunnel_port = RELAY_PORT + 1
    if plain_tunnel_port == API_PORT:
        plain_tunnel_port += 1

    hdr("Starting Core Services")
    relay_task = asyncio.create_task(start_relay(RELAY_PORT, plain_tunnel_port))
    dash_task  = asyncio.create_task(start_dashboard(API_PORT))

    await wait_for_port_async(RELAY_PORT, label="Relay Server")
    await wait_for_port_async(API_PORT, label="Dashboard API")
    await wait_for_port_async(plain_tunnel_port, label="Relay Tunnel Listener")

    # Tunnels are started AFTER the services above confirm they're actually
    # listening — starting them earlier (the old order) meant the tunnel
    # would hand back a URL that looked valid but connected to nothing,
    # since nothing was on the other end of the reverse forward yet.
    relay_tunnel_url = None
    dashboard_tunnel_url = None
    if not args.no_tunnel:
        relay_tunnel_url = auto_start_public_tunnel(plain_tunnel_port, label="Remote Agents / Relay")
        dashboard_tunnel_url = auto_start_public_tunnel(API_PORT, label="Dashboard Console")

    print_and_save_instructions(state, relay_tunnel_url, dashboard_tunnel_url)

    dashboard_path = find_dashboard_html()
    if not args.no_browser and dashboard_path:
        webbrowser.open(f"http://localhost:{API_PORT}/console")

    ok("All services fully initialized. Press Ctrl+C to stop.")

    try:
        await asyncio.gather(relay_task, dash_task)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        for proc in GLOBAL_TUNNEL_PROCS:
            proc.terminate()
        relay_task.cancel()
        dash_task.cancel()
        ok("NEXUS services stopped cleanly.")


def main():
    parser = argparse.ArgumentParser(description="NEXUS Automated Launcher")
    parser.add_argument("--reset", action="store_true", help="Force reset configuration")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser")
    parser.add_argument("--no-firewall", action="store_true", help="Skip Windows Firewall automation")
    parser.add_argument("--no-upnp", action="store_true", help="Skip UPnP router port forwarding")
    parser.add_argument("--no-tunnel", action="store_true", help="Skip public SSH tunnel creation")
    parser.add_argument("--port-api", type=int, default=8080)
    parser.add_argument("--port-relay", type=int, default=7000)
    args = parser.parse_args()

    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()