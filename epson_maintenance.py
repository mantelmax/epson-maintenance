#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
epson_maintenance.py — Maintenance utility for Epson inkjet printers
(e.g. EcoTank ET-M1120)

Runs the maintenance functions that Epson normally only exposes through its
Windows/macOS printer software, directly on Linux:

    status          query printer state and ink levels (SNMP, network only)
    nozzle-check    print the printer's built-in nozzle test pattern
    head-clean      regular print head cleaning
    power-cleaning  Power Cleaning — intensive system flush

How it works
------------
The program speaks Epson's ESC/P2 "remote mode" and sends the same
maintenance commands that Epson's own utility and escputil (Gutenprint)
use:

    - TCP 9100 (RAW/JetDirect port) for Wi-Fi/network printers
    - alternatively USB through the USB printer device (e.g. /dev/usb/lp0)

Power Cleaning is the cleaning command "CH" with the power flag set
(0x10). The command sequences are independently cross-referenced:

    - escputil from Gutenprint (src/escputil/escputil.c, do_head_clean)
    - x900-otsakupuhastajat/emanage.py (J. Laird-Wah, public domain;
      reverse engineering of the CH power flag, sent over TCP 9100)
    - epson-reset and epson_escp2 (EcoTank implementations)

Warning: cleaning and especially Power Cleaning consume ink and fill the
waste ink pad faster. Only run them when the nozzle check shows problems.
Epson recommends keeping the printer plugged into mains power during
Power Cleaning.

Exit codes
----------
0   success
1   error (network/device) or aborted by the user
2   safety abort (printer is busy)
3   command sent, but completion could not be confirmed via SNMP — check the device
10  --mock: bytes were only displayed, nothing was sent
"""

import argparse
import glob
import os
import re
import socket
import struct
import sys
import time

ESC = b"\x1b"
RESET = ESC + b"@"
# ESC @ + ESC ( R 08 00 00 "REMOTE1" — as in escputil start_remote_sequence()
REMOTE1 = b"\x1b@\x1b(R\x08\x00\x00REMOTE1"
# "\033\000\000\000\033\000" — escputil end_remote_sequence()
REMOTE_END = b"\x1b\x00\x00\x00\x1b\x00"
# "\f\033\000\033\000" — form feed + resets (escputil do_print_cmd())
JOB_END = b"\x0c\x1b\x00\x1b\x00"

EXIT_MOCK = 10


# ---------------------------------------------------------------------------
# ESC/P2 remote mode sequences
# ---------------------------------------------------------------------------

def remote_cmd(name: str, arg: bytes) -> bytes:
    """Wrap a remote command: two-letter command + length (LE) + arguments."""
    return name.encode("ascii") + struct.pack("<H", len(arg)) + arg


def stream_nozzle_check() -> bytes:
    """escputil: VI 00 00; NC 00 10; NC 00 00 + form feed."""
    return (
        REMOTE1
        + remote_cmd("VI", b"\x00\x00")
        + remote_cmd("NC", b"\x00\x10")
        + remote_cmd("NC", b"\x00\x00")
        + REMOTE_END
        + JOB_END
    )


def stream_head_clean(group: int = 0, power: bool = False) -> bytes:
    """
    escputil: CH 00 <group> — Power Cleaning adds the 0x10 flag to the
    nozzle group (reverse engineering: emanage.py, group |= 0x10).
    Group 0 = all nozzle groups (the correct choice for the mono ET-M1120).
    """
    g = (group | 0x10) if power else group
    return REMOTE1 + remote_cmd("CH", bytes([0x00, g])) + REMOTE_END + JOB_END


def hexdump(data: bytes, indent: str = "    ") -> str:
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off : off + 16]
        hexfield = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{indent}{off:04x}  {hexfield:<47}  |{text}|")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SNMP status query (an embedded raw SNMPv1 GET; frame taken from emanage.py,
# J. Laird-Wah 2015, public domain)
# ---------------------------------------------------------------------------

SNMP_STATUS_REQUEST = bytes.fromhex(
    "303002010004067075626c6963a0230204"
    "0425b62302010002010030153013060f2b06"
    "0104018960010202010101040105 00".replace(" ", "")
)

STATUS_LABELS = {
    0: "Error",
    1: "Self test running",
    2: "Busy (printing)",
    3: "Waiting for data",
    4: "Ready (idle)",
    5: "Paused",
    7: "Cleaning in progress",
    15: "Nozzle check running",
}
BUSY = {1, 2, 7, 15}
IDLE = {3, 4}

INK_NAMES = {
    0x01: "Photo Black",
    0x03: "Cyan",
    0x04: "Magenta",
    0x05: "Yellow",
    0x06: "Light Cyan",
    0x07: "Light Magenta",
    0x0A: "Light Black",
    0x0B: "Matte Black",
}


def snmp_query(ip: str, packet: bytes, port: int, timeout: float) -> bytes:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("", 0))
        s.settimeout(timeout)
        s.sendto(packet, (ip, port))
        data, _ = s.recvfrom(65535)
        return data
    finally:
        s.close()


def parse_st2(data: bytes) -> dict:
    """Split an ST2 status block ('@BDC ST2\\r\\n...') into fields."""
    m = re.search(rb"@BDC ST2\r?\n", data)
    if not m:
        raise ValueError("no ST2 status response (unexpected SNMP format)")
    rest = data[m.end() :]
    report = {"ink": [], "tanks": None, "status": None, "status_text": None}
    while len(rest) >= 2:
        ftype, length = rest[0], rest[1]
        field, rest = rest[2 : 2 + length], rest[2 + length :]
        if len(field) < length:
            break
        if ftype == 0x01:  # device state
            if field:
                st = field[0]
                report["status"] = st
                report["status_text"] = STATUS_LABELS.get(st, f"State {st}")
        elif ftype == 0x0F:  # ink levels
            if len(field) < 3:
                continue
            entry_len = field[0]
            off = 1
            while off + 2 <= len(field):
                color, value = field[off], field[off + 2]
                off += max(entry_len, 3)
                name = INK_NAMES.get(color, f"Ink 0x{color:02x}")
                report["ink"].append((name, value if value <= 100 else None))
        elif ftype == 0x0D:  # maintenance tanks (waste ink)
            if len(field) >= 2:
                report["tanks"] = (field[0], field[1])
    return report


def get_status(ip: str, snmp_port: int, timeout: float) -> dict:
    return parse_st2(snmp_query(ip, SNMP_STATUS_REQUEST, snmp_port, timeout))


def print_status_report(b: dict) -> None:
    if b.get("status_text"):
        print(f"  Printer state : {b['status_text']}")
    for name, value in b.get("ink", []):
        if value is None:
            print(f"  Ink           : {name} (value not interpretable)")
        else:
            print(f"  Ink           : {name} ~{value}%")
    if b.get("tanks"):
        t1, t2 = b["tanks"]
        print(f"  Maintenance   : tank1 {t1}% / tank2 {t2}% (waste ink, if reported)")


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

def send_tcp(ip: str, port: int, stream: bytes) -> None:
    s = socket.create_connection((ip, port), timeout=8)
    try:
        s.sendall(RESET + stream)  # leading RESET, like emanage.py on connect
        s.shutdown(socket.SHUT_WR)
        time.sleep(0.5)  # give the print server a moment before closing
    finally:
        s.close()


def send_usb(path: str, stream: bytes) -> None:
    with open(path, "wb") as f:
        f.write(RESET + stream)


def usb_autodetect() -> str | None:
    for path in sorted(glob.glob("/dev/usb/lp*")):
        if os.access(path, os.W_OK):
            return path
    return None


# ---------------------------------------------------------------------------
# Control flow
# ---------------------------------------------------------------------------

def precheck(ip: str | None, snmp_port: int, timeout: float, ignore: bool) -> None:
    """Query the printer via SNMP; block the operation if it is busy."""
    if not ip:
        return
    try:
        b = get_status(ip, snmp_port, timeout)
    except Exception:
        print("Note: SNMP precheck unavailable (printer reachable?).")
        return
    if b.get("status") in BUSY and not ignore:
        print(f"ABORT: printer is busy ({b['status_text']}).")
        print("Another maintenance cycle would be wasted ink. Please wait.")
        sys.exit(2)
    print_status_report(b)


def wait_for_completion(ip: str | None, limit_s: float, snmp_port: int, timeout: float) -> int:
    """Wait for the action to start, then poll until the printer is idle again."""
    if not ip:
        print("No network target (--ip): progress cannot be monitored via SNMP.")
        print("Please watch the printer.")
        return 3
    print("\nWaiting for the action to start (up to 60 s) ...", flush=True)
    started = False
    for _ in range(30):
        time.sleep(2)
        try:
            b = get_status(ip, snmp_port, timeout)
        except Exception:
            print("SNMP unreachable while waiting — check the device.")
            return 3
        if b.get("status") in BUSY:
            started = True
            print(f"  Printer: {b['status_text']}", flush=True)
            break
    if not started:
        print("The printer state did not change. The action may not have been")
        print("accepted, or SNMP does not report it. Please check the device.")
        return 3
    print("Action running, waiting for completion ...", flush=True)
    t0 = time.monotonic()
    last = None
    while time.monotonic() - t0 < limit_s:
        time.sleep(3)
        try:
            b = get_status(ip, snmp_port, timeout)
        except Exception:
            continue
        if b.get("status") in IDLE:
            print("\nCompleted. Current state:")
            print_status_report(b)
            return 0
        if b.get("status_text") != last:
            last = b["status_text"]
            print(f"  Printer: {last}", flush=True)
    print("\nTime limit reached, printer is not back to 'Ready'. Check the device.")
    return 3


# ---------------------------------------------------------------------------
# Main program
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="epson_maintenance.py",
        description="Maintenance utility for Epson inkjet printers (e.g. ET-M1120) on Linux",
        epilog=(
            "Examples:\n"
            "  %(prog)s status --ip 192.168.1.50\n"
            "  %(prog)s nozzle-check --ip 192.168.1.50\n"
            "  %(prog)s head-clean --ip 192.168.1.50\n"
            "  %(prog)s power-cleaning --ip 192.168.1.50\n"
            "  %(prog)s power-cleaning --usb /dev/usb/lp0 --yes\n"
            "  %(prog)s power-cleaning --ip 192.168.1.50 --mock   (show bytes only)\n"
            "\n"
            "The printer's IP address is shown in your router or in the\n"
            "printer's web interface (http://<IP>); the network name is\n"
            "usually EPSONXXXXXXX.local, resolvable e.g. with:\n"
            "  getent hosts EPSONXXXXXXX.local"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "action",
        choices=["status", "nozzle-check", "head-clean", "power-cleaning"],
        help="maintenance function to run",
    )
    target = ap.add_mutually_exclusive_group()
    target.add_argument("--ip", help="IP address or hostname of the printer (network)")
    target.add_argument(
        "--usb",
        dest="usb",
        default=None,
        const="auto",
        nargs="?",
        help="USB device instead of network; without a path /dev/usb/lp* is autodetected",
    )
    ap.add_argument("--port", type=int, default=9100, help="raw printing port (default 9100)")
    ap.add_argument("--snmp-port", type=int, default=161, help="SNMP port (default 161)")
    ap.add_argument(
        "--group",
        type=int,
        default=0,
        help="nozzle group for cleaning (0 = all, 1-5 targeted); default 0",
    )
    ap.add_argument("--yes", action="store_true", help="skip confirmation prompts (for scripts)")
    ap.add_argument(
        "--mock",
        action="store_true",
        help="dry run: show the bytes that would be sent, send nothing",
    )
    ap.add_argument(
        "--raw-data",
        metavar="FILE",
        help="also write the command stream to FILE (debugging/analysis)",
    )
    ap.add_argument(
        "--wait-limit",
        type=float,
        default=360,
        help="maximum time to wait for completion in seconds (default 360)",
    )
    ap.add_argument(
        "--snmp-timeout",
        type=float,
        default=2.0,
        help="timeout for a single SNMP query in seconds (default 2.0)",
    )
    ap.add_argument(
        "--ignore-status",
        action="store_true",
        help="bypass the 'printer busy' safety check (not recommended)",
    )
    ap.add_argument("--version", action="version", version="epson_maintenance.py 1.0")
    args = ap.parse_args()

    if not args.ip and not args.usb:
        ap.error("please provide --ip ADDRESS or --usb (see --help)")

    ip = None
    if args.usb:
        path = args.usb if args.usb != "auto" else usb_autodetect()
        if not path:
            print("No writable /dev/usb/lp* device found.", file=sys.stderr)
            print(
                "Note: the USB printer device requires the 'usblp' kernel driver.\n"
                "  sudo modprobe usblp    (and reconnect the USB cable if needed)",
                file=sys.stderr,
            )
            return 1
        if args.usb == "auto":
            print(f"USB device: {path}")
    else:
        ip = args.ip
        path = None

    if args.action == "status":
        try:
            b = get_status(ip, args.snmp_port, args.snmp_timeout)
        except Exception as e:
            print(f"Status query failed: {e}", file=sys.stderr)
            return 1
        print_status_report(b)
        return 0

    # --- build the command stream ---
    if args.action == "nozzle-check":
        stream = stream_nozzle_check()
        description = "print nozzle check page"
    elif args.action == "head-clean":
        stream = stream_head_clean(args.group, power=False)
        description = "regular print head cleaning"
    else:
        stream = stream_head_clean(args.group, power=True)
        description = "POWER CLEANING (intensive flush)"

    if args.raw_data:
        with open(args.raw_data, "wb") as f:
            f.write(RESET + stream)
        print(f"Command stream written to {args.raw_data}.")

    if args.mock:
        total = RESET + stream
        print(f"[MOCK] {description} — the following stream would be sent ({len(total)} bytes):")
        print(hexdump(total))
        print("[MOCK] Nothing was sent.")
        return EXIT_MOCK

    # --- safety prompts ---
    if not args.yes:
        if args.action == "power-cleaning":
            print("Caution — Power Cleaning:")
            print("  - uses significantly more ink than a regular cleaning")
            print("  - fills the waste ink pad faster")
            print("  - takes several minutes; do not turn the printer off")
            print("  - Epson recommends running it only after 2-3 regular")
            print("    cleanings with nozzle checks brought no improvement")
        try:
            answer = input(f"Run '{description}' now? [yes/no] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("yes", "y"):
            print("Aborted.")
            return 1
        if args.action == "nozzle-check":
            print("Please load a sheet of paper (if none is loaded).")

    # --- precheck and send ---
    precheck(ip, args.snmp_port, args.snmp_timeout, args.ignore_status)
    try:
        if ip:
            send_tcp(ip, args.port, stream)
        else:
            send_usb(path, stream)
    except OSError as e:
        print(f"Sending failed: {e}", file=sys.stderr)
        return 1

    print(f"'{description}' sent.")
    if args.action == "nozzle-check":
        print("The test pattern is being printed. Remove the sheet and inspect the nozzles.")
    else:
        print(
            "This action takes a while "
            "(Power Cleaning roughly 1-3 minutes; ink is flushed through the system)."
        )

    return wait_for_completion(ip, args.wait_limit, args.snmp_port, args.snmp_timeout)


if __name__ == "__main__":
    sys.exit(main())
