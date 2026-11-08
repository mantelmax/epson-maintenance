# Epson Printer Maintenance for Linux

Head cleaning, **Power Cleaning**, nozzle check and status queries for Epson
inkjet printers (developed on an **EcoTank ET-M1120**) — directly from Linux,
without Epson's Windows/macOS printer software and without any driver.

## Why this exists

Up to this project, **Power Cleaning was simply not executable on Linux.**
Epson only offers it inside its proprietary Windows and macOS printer
utilities — there is no Epson maintenance software for Linux at all. The
usual advice for Linux users with a clogged EcoTank printhead was to boot
Windows just to flush it, and that is exactly how this repository came
about.

It turned out that Power Cleaning, like every other maintenance function,
is nothing more than an ESC/P2 remote command (`CH 00 10`) sent over the
printer's own printing channel — the same channel every operating system
already uses to print. The "Windows only" restriction is a property of
Epson's software, not of the printer. This tool sends those commands
directly over the network (or USB), so the full maintenance suite —
including Power Cleaning — now runs on Linux.

## Features

- **Power Cleaning** — the intensive flush normally locked behind Epson's
  Windows utility, with a confirmation prompt and full ink-usage warnings
- **Regular print head cleaning** (all nozzle groups or a targeted group 1-5)
- **Nozzle check** — prints the printer's built-in test pattern
- **Status & ink levels** via SNMP: printer state, ink percentage per tank,
  waste ink pad level (when reported)
- Safety pre-flight: refuses to run while the printer is already cleaning
  or printing, and monitors the whole job via SNMP until it is done
- No dependencies for the CLI (pure Python 3 standard library)
- Optional dark-themed GUI (PySide6/Qt)

## Compatibility

### Operating systems

| OS | CLI (network) | GUI (network) | USB mode |
|---|---|---|---|
| **Linux** (any distribution) | fully supported | fully supported | supported (`usblp`) |
| macOS / other Unix | expected to work | expected to work | not supported |
| Windows | expected to work | expected to work | not supported |

- Developed and tested on **CachyOS (Arch Linux) x86_64**.
- Network mode (TCP 9100 + SNMP UDP 161) uses only portable Python standard
  library calls, so it is expected to run on any system with Python 3.9+ —
  but this was not tested outside Linux.
- USB mode is Linux-specific: it writes to the raw printer device
  (`/dev/usb/lp*`, provided by the `usblp` kernel module).
- The GUI additionally requires PySide6; the CLI has zero dependencies.

### Printers

Developed on the **Epson EcoTank ET-M1120** (monochrome EcoTank). The
underlying ESC/P2 remote commands are the common language of modern Epson
inkjet printers, so every network-capable Epson inkjet with ESC/P2 remote
maintenance is expected to work — the EcoTank series (ET-xxxx), Expression,
WorkForce and modern Stylus models:

- Nozzle check and regular cleaning are byte-identical with the streams
  escputil (Gutenprint) sends for modern EcoTank/Expression models; proven
  on an ET-2750 (stream reference) and ET-2820/ET-2825 (field tests of the
  epson-reset project).
- Power Cleaning (`CH 00 10`) is confirmed by two independent reverse
  engineering projects (see credits) and is expected to work across the
  EcoTank line; live validation on the ET-M1120 is still pending.
- Status and ink levels rely on Epson's SNMP `@BDC ST2` frames, which the
  development printer answers, as do the reference projects' printers. The
  EcoTank ink layout (a 2-byte payload length after the marker, then per-tank
  entries of `[slot, colourCode, percent]`) is decoded and was validated
  against a captured ET-2820 frame in the epson-reset suite and live on the
  ET-M1120.

Caveats: models differ in nozzle groups and in what their SNMP layout
reports. Ink levels decode for the EcoTank models tested (ET-M1120, ET-2820);
on a model that performs the cleanings but reports a different ink field,
everything else still works — and `--group` lets you target nozzle groups
1-5 where a model needs it.

## Files

| File | Purpose |
|---|---|
| `epson_maintenance.py` | Command line tool (also the protocol library) |
| `epson_maintenance_gui.py` | Graphical front end (imports the CLI module) |
| `tests/` | Emulated-printer test suite, no hardware needed |

Both programs must sit in the same directory.

## Requirements

- Python 3.9+ (no pip packages needed for the CLI)
- A printer connected via **network** (Wi-Fi/Ethernet) — recommended —
  or via **USB** on Linux (`usblp` kernel module, `/dev/usb/lp*`)
- For the GUI only: PySide6 — on Arch/CachyOS: `sudo pacman -S python-pyside6`

## Quick start (CLI)

Find your printer's IP address (router device list, or the printer's own web
interface at `http://<ip>`; network name is usually `EPSONXXXXXXX.local`).

```bash
# state and ink levels
python3 epson_maintenance.py status --ip 192.168.178.175

# print the nozzle test pattern (have paper ready)
python3 epson_maintenance.py nozzle-check --ip 192.168.178.175

# regular head cleaning
python3 epson_maintenance.py head-clean --ip 192.168.178.175

# Power Cleaning (asks for confirmation; uses a lot of ink)
python3 epson_maintenance.py power-cleaning --ip 192.168.178.175

# dry run: print the exact bytes instead of sending them
python3 epson_maintenance.py power-cleaning --ip 192.168.178.175 --mock

# USB instead of network (--usb auto-detects /dev/usb/lp*)
sudo python3 epson_maintenance.py power-cleaning --usb --yes
```

Exit codes: `0` success, `1` error/user abort, `2` blocked (printer busy),
`3` sent but completion unverified, `10` mock run (nothing sent).

## Quick start (GUI)

```bash
python3 epson_maintenance_gui.py
```

Enter the printer IP, click **Fetch status** — the ink bars appear and the
three action buttons unlock. Power Cleaning opens a confirmation dialog that
spells out the trade-offs before anything is sent. The IP is remembered in
`~/.config/epson_maintenance/gui.json`; the status refreshes automatically
every 15 seconds and all activity is written to the log pane.

## How it works

Epson inkjet printers share a maintenance command language ("remote mode")
inside ESC/P2. A session looks like this over TCP 9100:

```text
1b 40                                ESC @   (reset)
1b 40 1b 28 52 08 00 00 "REMOTE1"    enter remote mode
<CMD> <len LE> <args>                the actual command
1b 00 00 00 1b 00                    leave remote mode
0c 1b 00 1b 00                       form feed + resets
```

The commands used by this tool (the `power` flag `0x10` is the community
reverse-engineered bit for deep cleaning):

| Action | Command | Bytes |
|---|---|---|
| Nozzle check | `VI 00 00`, `NC 00 10`, `NC 00 00` | `56 49 02 00 00 00 4e 43 02 00 00 10 4e 43 02 00 00 00` |
| Regular cleaning | `CH 00 00` | `43 48 02 00 00 00` |
| Regular cleaning, group N (1-5) | `CH 00 0N` | `43 48 02 00 00 0N` |
| **Power Cleaning** | `CH 00 10` | `43 48 02 00 00 10` |
| **Power Cleaning**, group N | `CH 00 1N` | `43 48 02 00 00 1N` |

Status and ink levels are read through SNMP (UDP 161) with Epson's `@BDC ST2`
status block: after the `@BDC ST2` marker comes a 2-byte little-endian payload
length, then type/length/value fields. The ink field (0x0F) holds one entry
per tank — `[slot, colourCode, percent]` — so the colour name comes from the
2nd byte, not the (arbitrary) slot. State, error code, ink percentages,
serial number and waste-ink tanks (0x0D, when reported) are decoded from it.
The same channel serves as a safety gate (no new jobs while busy) and to
monitor a running cleaning until the printer reports idle again.

## Safety notes

- Cleaning cycles flush ink into the waste ink pad. Running cleanings in a
  loop and ignoring the nozzle check is how pads overflow.
- **Power Cleaning** consumes a lot of ink. Epson's own guidance applies:
  keep the printer on mains power, expect roughly 1-3 minutes runtime, and
  only escalate to Power Cleaning after 2-3 regular cleanings + nozzle
  checks did not help.
- The tool always queries the printer first and refuses to stack a new
  action onto a running one (use `--ignore-status` if you really must).
- Use `--mock` first if you want to see exactly what would be sent.

## Tests

The whole suite runs against an emulated printer (a TCP + SNMP mock included
in `tests/`) — no hardware attached, and nothing is sent anywhere:

```bash
python3 tests/mock_test.py           # CLI: byte-for-byte against the escputil reference
python3 tests/gui_test.py            # GUI business logic without Qt
python3 tests/gui_offscreen_test.py  # real window, offscreen (needs PySide6)
```

32 automated checks in total: exact byte streams, send path, busy blocking,
full job monitoring, USB path, dry run and the GUI wiring.

## Tested status & limitations

- The byte streams are **cross-referenced against escputil** (Gutenprint, the
  reference Linux Epson tool) and reproduce its output one-to-one; nozzle
  check and regular cleaning are the same sequences escputil itself sends.
- Power Cleaning (`CH 00 10`) is independently confirmed by two reverse
  engineering projects (see credits) but is not part of escputil; it is
  expected to work on EcoTank models, yet a live field test on the ET-M1120
  is still pending.
- SNMP status works against the development printer: state, ink level
  (Black ~100%) and serial number are reported. The EcoTank ST2 ink decoding
  is cross-checked against a captured ET-2820 frame in the epson-reset test
  suite (Black 11 / Yellow 65 / Magenta 63 / Cyan 66); exotic fields fall back
  to a readable raw display.
- Printhead alignment is not implemented (escputil can do it over USB).

## Troubleshooting

- **`status` times out** — printer and computer must be on the same network;
  some routers block SNMP (UDP 161) between stations. The maintenance
  commands still work over TCP 9100 even if status shows nothing.
- **Action sent, but no state change observed** — check the printer display /
  web UI; if SNMP is blocked, the tool cannot track progress (exit code 3).
- **USB: no `/dev/usb/lp*`** — load the kernel module: `sudo modprobe usblp`,
  then reconnect the printer.
- **Power Cleaning declines** — Epson's utility normally demands USB; if
  your model refuses the command over the network, retry with `--usb`.

## Credits & sources

- `escputil` from [Gutenprint](https://gutenprint.sourceforge.net/) — command
  reference for nozzle check, regular cleaning and the remote-mode framing.
- [x900-otsakupuhastajat / emanage.py](https://github.com/abrasive/x900-otsakupuhastajat)
  (J. Laird-Wah, public domain) — reverse engineering of the `CH` power
  cleaning flag and the SNMP status frames used here.
- [epson-reset](https://github.com/yusnel-rojas/epson-reset) and
  [epson_escp2](https://github.com/Ircama/epson_escp2) — EcoTank
  implementations that corroborate the byte sequences and the network path.

## Disclaimer

This project is not affiliated with or endorsed by Seiko Epson Corporation.
Epson and EcoTank are trademarks of their respective owners. The commands were
derived from public reverse engineering and open-source references; use at
your own risk.

## License

Released under the [MIT License](LICENSE).
