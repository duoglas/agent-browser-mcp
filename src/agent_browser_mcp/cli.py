from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from .server import ROOT, ensure_config_js, get_driver, chrome_extension_dir, mcp

SERVICE_LABEL = "com.agent-browser-mcp.bridge"


def cmd_extension_path() -> int:
    path = chrome_extension_dir()
    ensure_config_js()
    print(path)
    return 0


def cmd_print_hermes_config() -> int:
    print(
        "mcp_servers:\n"
        "  agent_browser:\n"
        "    command: agent-browser-mcp\n"
        "    timeout: 120\n"
        "    connect_timeout: 60"
    )
    return 0


def _port_open(host: str, port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(1)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def cmd_doctor() -> int:
    ensure_config_js()
    driver = get_driver()
    ws_port = getattr(driver, "port", 18765)
    http_port = ws_port + 1
    sessions = []
    err = None
    try:
        sessions = driver.get_all_sessions()
    except Exception as e:
        err = str(e)
    payload = {
        "extension_path": str(chrome_extension_dir()),
        "config_js": str((chrome_extension_dir() / 'config.js').resolve()),
        "remote_mode": getattr(driver, "is_remote", False),
        "tmwebdriver_host": getattr(driver, "host", "127.0.0.1"),
        "tmwebdriver_ws_port": ws_port,
        "tmwebdriver_http_port": http_port,
        "ws_port_open": _port_open(getattr(driver, "host", "127.0.0.1"), ws_port),
        "http_port_open": _port_open(getattr(driver, "host", "127.0.0.1"), http_port),
        "connected_tabs": len(sessions),
        "tabs": sessions,
        "error": err,
        "next_steps": [
            "Load the unpacked extension in chrome://extensions from extension_path.",
            "Open a normal http/https page in Chrome.",
            "Run `hermes mcp test agent_browser` after adding the MCP config.",
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_serve() -> int:
    """Run TMWebDriver as a standalone, always-on bridge owner.

    Both Claude clients then connect in remote mode, so quitting either client never tears down
    the bridge. Intended to be launched by launchd at login (see `install-service`).

    If a client transiently owns the port (e.g. started before this service), we wait in a single
    long-lived process and claim ownership the moment the port frees — no launchd restart churn."""
    from .tmwebdriver import TMWebDriver

    ensure_config_js()
    host = os.environ.get("AGENT_BROWSER_TMWD_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_BROWSER_TMWD_PORT", "18765"))
    waited = False
    while True:
        driver = TMWebDriver(host=host, port=port)  # binds only if the port is free
        if not driver.is_remote:
            break
        if not waited:
            print("[serve] bridge port owned by another process; waiting to take over...",
                  file=sys.stderr, flush=True)
            waited = True
        time.sleep(15)
    print(f"[serve] TMWebDriver bridge owning ws://{host}:{port} (http {port + 1}). Ctrl-C to stop.",
          flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def _log_dir() -> Path:
    d = Path.home() / "Library" / "Logs" / "agent-browser-mcp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_install_service() -> int:
    """Write and load a launchd LaunchAgent that keeps the bridge running at login (macOS)."""
    if sys.platform != "darwin":
        print("install-service currently supports macOS (launchd) only.", file=sys.stderr)
        return 1
    plist = _plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    log_dir = _log_dir()
    args = [sys.executable, "-m", "agent_browser_mcp.cli", "serve"]
    args_xml = "\n".join(f"      <string>{a}</string>" for a in args)
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        f'  <key>Label</key><string>{SERVICE_LABEL}</string>\n'
        '  <key>ProgramArguments</key>\n'
        f'  <array>\n{args_xml}\n  </array>\n'
        '  <key>RunAtLoad</key><true/>\n'
        '  <key>KeepAlive</key><true/>\n'
        '  <key>ThrottleInterval</key><integer>10</integer>\n'
        f'  <key>StandardOutPath</key><string>{log_dir / "bridge.out.log"}</string>\n'
        f'  <key>StandardErrorPath</key><string>{log_dir / "bridge.err.log"}</string>\n'
        '</dict>\n'
        '</plist>\n',
        encoding="utf-8",
    )
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{SERVICE_LABEL}"],
                   capture_output=True)  # ignore if not loaded
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # Fall back to legacy load for older macOS
        subprocess.run(["launchctl", "load", "-w", str(plist)], capture_output=True)
    print(json.dumps({
        "label": SERVICE_LABEL,
        "plist": str(plist),
        "program": args,
        "logs": str(log_dir),
        "status": "loaded",
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_uninstall_service() -> int:
    if sys.platform != "darwin":
        print("uninstall-service supports macOS (launchd) only.", file=sys.stderr)
        return 1
    plist = _plist_path()
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{SERVICE_LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    if plist.exists():
        plist.unlink()
    print(json.dumps({"label": SERVICE_LABEL, "plist": str(plist), "status": "removed"},
                     ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-browser-mcp",
        description="Real-browser MCP server with TMWebDriver/CDP bridge, screenshots, and physical input.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("extension-path", help="Print the unpacked Chrome extension path")
    sub.add_parser("doctor", help="Run local diagnostics and print JSON status")
    sub.add_parser("print-hermes-config", help="Print a ready-to-paste Hermes MCP config snippet")
    sub.add_parser("serve", help="Run the always-on TMWebDriver bridge (used by the launchd service)")
    sub.add_parser("install-service", help="Install + load a launchd LaunchAgent so the bridge runs at login (macOS)")
    sub.add_parser("uninstall-service", help="Unload + remove the launchd LaunchAgent (macOS)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "extension-path":
        return cmd_extension_path()
    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "print-hermes-config":
        return cmd_print_hermes_config()
    if args.command == "serve":
        return cmd_serve()
    if args.command == "install-service":
        return cmd_install_service()
    if args.command == "uninstall-service":
        return cmd_uninstall_service()

    ensure_config_js()
    get_driver()
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
