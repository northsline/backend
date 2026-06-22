"""
known — terminal stats for your Known device.

Usage:
    known                       pretty summary (default)
    known report --table        compact table format
    known report --json         raw JSON
    known report --compact      one-line summary
    known watch                 live refresh every 5s
    known follow <ip>           live stream for one device
    known diff                  what changed since last run
    known monitor               silent check, exit 1 if alert
    known allow add <pattern>   add allowlist entry
    known allow rm <id>         remove allowlist entry
    known allow ls              list allowlist entries
    known debug                 raw internal state
    known --host 192.168.1.42   manual device IP
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ── config ──────────────────────────────────────────────────────────────

PORT = 8080
MDNS = "known.local"
SAVE_FILE = os.path.join(os.path.expanduser("~"), ".known-host")
SNAPSHOT_FILE = os.path.join(os.path.expanduser("~"), ".known-snapshot")
WATCH_INTERVAL = 5
FOLLOW_INTERVAL = 2
MONITOR_INTERVAL = 10
TIMEOUT = 3

# ── palette ────────────────────────────────────────────────────────────

class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    GRAY    = "\033[38;5;240m"
    MUTED   = "\033[38;5;244m"
    FAINT   = "\033[38;5;238m"
    GREEN   = "\033[38;5;108m"
    RED     = "\033[38;5;167m"
    AMBER   = "\033[38;5;179m"
    SKY     = "\033[38;5;110m"
    LILAC   = "\033[38;5;140m"

if not (sys.stdout.isatty() and os.environ.get("NO_COLOR") is None):
    for _a in ("RESET", "BOLD", "DIM", "GRAY", "MUTED", "FAINT",
               "GREEN", "RED", "AMBER", "SKY", "LILAC"):
        setattr(C, _a, "")


# ── terminal helpers ────────────────────────────────────────────────────

def clear():
    print("\033[2J\033[H", end="")


def spinner(msg, done_event):
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    i = 0
    while not done_event.is_set():
        sys.stdout.write(f"\r  {C.MUTED}{frames[i % len(frames)]}{C.RESET}  {msg}")
        sys.stdout.flush()
        i += 1
        time.sleep(0.06)
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


# ── discovery ──────────────────────────────────────────────────────────

def resolve_mdns(timeout_s=2):
    old = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout_s)
        return socket.gethostbyname(MDNS)
    except Exception:
        return None
    finally:
        socket.setdefaulttimeout(old)


def check_health(ip):
    try:
        resp = urlopen(f"http://{ip}:{PORT}/health", timeout=TIMEOUT)
        return json.loads(resp.read()).get("status") == "ok"
    except Exception:
        return False


def save_host(ip):
    try:
        with open(SAVE_FILE, "w") as f:
            f.write(ip)
    except Exception:
        pass


def load_host():
    try:
        with open(SAVE_FILE) as f:
            return f.read().strip()
    except Exception:
        return None


def discover():
    ip = resolve_mdns()
    if ip and check_health(ip):
        save_host(ip)
        return ip
    saved = load_host()
    if saved and check_health(saved):
        return saved
    return None


def discover_with_loading(host=None):
    done = threading.Event()
    dummy = threading.Thread(target=lambda: done.wait(), daemon=True)
    dummy.start()

    if host:
        msg = f"connecting to {host}…"
    else:
        msg = "scanning for known.local…"
    sp = threading.Thread(target=spinner, args=(msg, done), daemon=True)
    sp.start()

    if host:
        ip = host if check_health(host) else None
        if ip:
            save_host(ip)
    else:
        ip = discover()

    done.set()
    sp.join(timeout=0.5)
    return ip


# ── api ────────────────────────────────────────────────────────────────

def fetch(ip, path, method="GET", data=None):
    """GET or PUT/DELETE. Returns parsed JSON or {"error": ...}."""
    url = f"http://{ip}:{PORT}{path}"
    try:
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            req = Request(url, data=body, method=method)
            req.add_header("Content-Type", "application/json")
        elif method != "GET":
            req = Request(url, method=method)
        else:
            req = Request(url)
        resp = urlopen(req, timeout=TIMEOUT)
        return json.loads(resp.read())
    except HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except URLError:
        return {"error": "unreachable"}
    except Exception as e:
        return {"error": str(e)}


def fetch_all(ip):
    return (
        fetch(ip, "/stats"),
        fetch(ip, "/devices"),
        fetch(ip, "/audit/weekly?limit=30"),
    )


# ── formatting helpers ─────────────────────────────────────────────────

def fmt_num(n):
    return f"{n:,}"


def fmt_time(ts):
    if not ts:
        return "—"
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    except Exception:
        return "—"


def fmt_duration(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    m = int(seconds // 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


def fmt_ago(ts):
    if not ts:
        return "—"
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    m = int(delta // 60)
    if m < 60:
        return f"{m}m ago"
    h = int(delta // 3600)
    if h < 24:
        return f"{h}h ago"
    return f"{int(delta // 86400)}d ago"


def truncate(s, width):
    return s if len(s) <= width else s[: width - 1] + "…"


def pad(s, width):
    return s.ljust(width)[:width]


# ── PRETTY format ──────────────────────────────────────────────────────

def pretty_header(ip, subtitle=None):
    print()
    print(f"  {C.BOLD}Known{C.RESET}", end="")
    if subtitle:
        print(f"  {C.FAINT}·{C.RESET}  {C.MUTED}{subtitle}{C.RESET}")
    else:
        print()
    print(f"  {C.FAINT}{ip}{C.RESET}")
    print()


def pretty_stats(stats):
    if "error" in stats:
        print(f"  {C.RED}stats unavailable — {stats['error']}{C.RESET}")
        print()
        return

    total_q   = stats.get("total_queries", 0)
    domains   = stats.get("unique_domains", 0)
    dev_count = stats.get("device_count", 0)
    flagged   = stats.get("flagged_count", 0)
    period    = stats.get("period_start", 0)

    w = 14
    gutter = 3

    def num_cell(val, color=C.BOLD):
        print(f"  {color}{str(val).rjust(w)}{C.RESET}" + " " * gutter, end="")

    def lbl_cell(label):
        print(f"  {C.MUTED}{label.rjust(w)}{C.RESET}" + " " * gutter, end="")

    flagged_color = C.AMBER if flagged else C.BOLD

    num_cell(fmt_num(total_q))
    num_cell(fmt_num(domains))
    num_cell(fmt_num(dev_count))
    num_cell(str(flagged), flagged_color)
    print()
    print()

    lbl_cell("queries")
    lbl_cell("domains")
    lbl_cell("devices")
    lbl_cell("flagged")
    print()
    print()

    if period:
        print(f"  {C.MUTED}up {fmt_duration(time.time() - period)}{C.RESET}")
    print()


def pretty_devices(devices_data):
    if not devices_data or "error" in devices_data:
        if devices_data and "error" in devices_data:
            print(f"  {C.RED}devices unavailable — {devices_data['error']}{C.RESET}")
        else:
            print(f"  {C.MUTED}no devices seen yet{C.RESET}")
        print()
        return

    devs = sorted(devices_data, key=lambda d: d.get("query_count", 0), reverse=True)

    print(f"  {C.BOLD}Devices{C.RESET}")
    print()
    print(f"  {C.FAINT}{'name':<22} {'ip':<16} {'queries':>8}   {'last seen'}{C.RESET}")
    print(f"  {C.FAINT}{'─' * 62}{C.RESET}")

    for d in devs:
        name = truncate(d.get("name", "?"), 21)
        ip   = d.get("ip", "—")
        q    = d.get("query_count", 0)
        ago  = fmt_ago(d.get("last_seen", 0))
        ago_c = f"{C.MUTED}{ago}{C.RESET}"
        print(f"  {pad(name, 22)} {pad(ip, 16)} {str(q).rjust(8)}   {ago_c}")

    print()


def pretty_activity(audit_data, limit=15):
    if not audit_data or "error" in audit_data:
        if audit_data and "error" in audit_data:
            print(f"  {C.RED}log unavailable — {audit_data['error']}{C.RESET}")
        else:
            print(f"  {C.MUTED}no queries logged yet{C.RESET}")
        print()
        return

    entries = audit_data[-limit:]

    print(f"  {C.BOLD}Activity{C.RESET}")
    print()
    print(f"  {C.FAINT}{'time':<10} {'source':<16} {'domain'}{C.RESET}")
    print(f"  {C.FAINT}{'─' * 62}{C.RESET}")

    for e in entries:
        t   = fmt_time(e.get("timestamp", 0))
        src = truncate(e.get("source", "—"), 15)
        dom = truncate(e.get("domain", "—"), 36)
        print(f"  {C.MUTED}{pad(t, 10)}{C.RESET} {pad(src, 16)} {dom}")

    if len(audit_data) > limit:
        print(f"\n  {C.FAINT}+{len(audit_data) - limit} more{C.RESET}")
    print()


# ── TABLE format ───────────────────────────────────────────────────────

def render_pretty(ip, stats, devices, audit, scope="all"):
    subtitle = None if scope == "all" else scope
    pretty_header(ip, subtitle)
    if scope in ("all", "stats"):
        pretty_stats(stats)
    if scope in ("all", "devices"):
        pretty_devices(devices)
    if scope in ("all", "activity"):
        pretty_activity(audit)


def render_table(ip, stats, devices, audit, scope="all"):
    if scope in ("all", "stats"):
        if "error" in stats:
            print(f"stats: error — {stats['error']}")
        else:
            total_q   = stats.get("total_queries", 0)
            domains   = stats.get("unique_domains", 0)
            dev_count = stats.get("device_count", 0)
            flagged   = stats.get("flagged_count", 0)
            period    = stats.get("period_start", 0)
            uptime    = fmt_duration(time.time() - period) if period else "—"
            print(f"  queries    domains    devices    flagged    uptime")
            print(f"  {fmt_num(total_q):<10} {fmt_num(domains):<10} {dev_count:<10} {flagged:<10} {uptime}")
            print()

    if scope in ("all", "devices"):
        if not devices or "error" in devices:
            if devices and "error" in devices:
                print(f"devices: error — {devices['error']}")
            else:
                print("  no devices")
            print()
        else:
            devs = sorted(devices, key=lambda d: d.get("query_count", 0), reverse=True)
            print(f"  name                    ip               queries    last seen")
            print(f"  {'─' * 68}")
            for d in devs:
                name = truncate(d.get("name", "?"), 22)
                ip   = d.get("ip", "—")
                q    = d.get("query_count", 0)
                ago  = fmt_ago(d.get("last_seen", 0))
                print(f"  {pad(name, 24)} {pad(ip, 16)} {str(q).rjust(8)}    {ago}")
            print()

    if scope in ("all", "activity"):
        if not audit or "error" in audit:
            if audit and "error" in audit:
                print(f"activity: error — {audit['error']}")
            else:
                print("  no queries")
            print()
        else:
            entries = audit[-20:]
            print(f"  time         source            domain")
            print(f"  {'─' * 68}")
            for e in entries:
                t   = fmt_time(e.get("timestamp", 0))
                src = truncate(e.get("source", "—"), 16)
                dom = truncate(e.get("domain", "—"), 38)
                print(f"  {pad(t, 12)} {pad(src, 18)} {dom}")
            if len(audit) > 20:
                print(f"  +{len(audit) - 20} more")
            print()


def render_compact(ip, stats, devices, audit, scope="all"):
    if "error" in stats:
        print(f"known · {ip} · error: {stats['error']}")
        return

    total_q   = stats.get("total_queries", 0)
    domains   = stats.get("unique_domains", 0)
    dev_count = stats.get("device_count", 0)
    flagged   = stats.get("flagged_count", 0)
    period    = stats.get("period_start", 0)
    uptime    = fmt_duration(time.time() - period) if period else "—"

    parts = [
        f"known · {ip}",
        f"{fmt_num(total_q)} queries",
        f"{fmt_num(domains)} domains",
        f"{dev_count} devices",
    ]
    if flagged:
        parts.append(f"{flagged} flagged")
    parts.append(f"up {uptime}")
    print(" · ".join(parts))


def render_json(ip, stats, devices, audit, scope="all"):
    payload = {}
    if scope in ("all", "stats"):
        payload["stats"] = stats
    if scope in ("all", "devices"):
        payload["devices"] = devices
    if scope in ("all", "activity"):
        payload["activity"] = audit
    payload["device"] = {"ip": ip, "port": PORT}
    print(json.dumps(payload, indent=2))


FORMATS = {
    "pretty":  render_pretty,
    "table":   render_table,
    "compact": render_compact,
    "json":    render_json,
}


def run_report(ip, fmt, scope):
    stats, devices, audit = fetch_all(ip)
    renderer = FORMATS.get(fmt, render_pretty)
    renderer(ip, stats, devices, audit, scope=scope)


# ── not found ──────────────────────────────────────────────────────────

def view_not_found():
    print()
    print(f"  {C.BOLD}Known{C.RESET}")
    print()
    print(f"  {C.RED}●{C.RESET}  not found")
    print()
    print(f"  {C.MUTED}tried known.local + saved ip{C.RESET}")
    print(f"  {C.MUTED}make sure Known is powered on and on your network{C.RESET}")
    print()
    print(f"  {C.FAINT}try:{C.RESET}  {C.DIM}known --host 192.168.1.42{C.RESET}")
    print()


# ── debug ──────────────────────────────────────────────────────────────

def view_debug(ip):
    data = fetch(ip, "/debug")
    print(f"  {C.BOLD}Debug{C.RESET}")
    print()
    print(json.dumps(data, indent=2))


# ════════════════════════════════════════════════════════════════════════
# FEATURE: follow
# ════════════════════════════════════════════════════════════════════════

def cmd_follow(args):
    ip = discover_with_loading(args.host)
    if not ip:
        view_not_found()
        sys.exit(1)

    target = args.ip
    seen_timestamps = set()

    # Seed: grab current entries so we don't replay history
    initial = fetch(ip, "/audit/weekly?limit=150")
    if isinstance(initial, list):
        for e in initial:
            if e.get("source") == target:
                seen_timestamps.add(e.get("timestamp", 0))

    # Resolve device name if we can
    dev_name = target
    devices = fetch(ip, "/devices")
    if isinstance(devices, list):
        for d in devices:
            if d.get("ip") == target:
                dev_name = d.get("name", target)
                break

    clear()
    print(f"  {C.BOLD}Known{C.RESET}  {C.FAINT}·{C.RESET}  {C.MUTED}following{C.RESET}")
    print(f"  {C.MUTED}{dev_name}{C.RESET}  {C.FAINT}({target}){C.RESET}")
    print(f"  {C.FAINT}{'─' * 52}{C.RESET}")
    print(f"  {C.FAINT}waiting for queries…  ctrl+c to stop{C.RESET}")
    print()

    try:
        while True:
            entries = fetch(ip, "/audit/weekly?limit=150")
            if not isinstance(entries, list):
                time.sleep(FOLLOW_INTERVAL)
                continue

            new = []
            for e in entries:
                if e.get("source") != target:
                    continue
                ts = e.get("timestamp", 0)
                if ts in seen_timestamps:
                    continue
                seen_timestamps.add(ts)
                new.append(e)

            # Keep the seen set bounded
            if len(seen_timestamps) > 500:
                seen_timestamps = set(sorted(seen_timestamps)[-300:])

            for e in new:
                t = fmt_time(e.get("timestamp", 0))
                dom = e.get("domain", "—")
                print(f"  {C.MUTED}{t}{C.RESET}  {dom}")

            time.sleep(FOLLOW_INTERVAL)
    except KeyboardInterrupt:
        print(f"\n  {C.MUTED}stopped{C.RESET}")


# ════════════════════════════════════════════════════════════════════════
# FEATURE: diff
# ════════════════════════════════════════════════════════════════════════

def take_snapshot(ip):
    """Capture current state for later diffing."""
    stats, devices, audit = fetch_all(ip)
    return {
        "timestamp": time.time(),
        "stats": stats if "error" not in stats else {},
        "device_ips": [d.get("ip") for d in devices] if isinstance(devices, list) else [],
        "device_names": {d.get("ip"): d.get("name") for d in devices} if isinstance(devices, list) else {},
        "domains": list({e.get("domain") for e in audit if isinstance(audit, list)}),
        "flagged_count": stats.get("flagged_count", 0) if "error" not in stats else 0,
        "total_queries": stats.get("total_queries", 0) if "error" not in stats else 0,
    }


def save_snapshot(snap):
    try:
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(snap, f)
    except Exception:
        pass


def load_snapshot():
    try:
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def cmd_diff(args):
    ip = discover_with_loading(args.host)
    if not ip:
        view_not_found()
        sys.exit(1)

    current = take_snapshot(ip)
    previous = load_snapshot()

    if not previous:
        # First run — save and report baseline
        save_snapshot(current)
        pretty_header(ip, "diff · baseline")
        print(f"  {C.MUTED}no previous snapshot — saved baseline{C.RESET}")
        print()
        print(f"  {C.BOLD}{fmt_num(current['total_queries'])}{C.RESET} {C.MUTED}queries{C.RESET}")
        print(f"  {C.BOLD}{len(current['device_ips'])}{C.RESET} {C.MUTED}devices{C.RESET}")
        print(f"  {C.BOLD}{len(current['domains'])}{C.RESET} {C.MUTED}domains{C.RESET}")
        print()
        print(f"  {C.FAINT}run `known diff` again to see what changed{C.RESET}")
        print()
        return

    # Compute diff
    old_ips = set(previous.get("device_ips", []))
    new_ips = set(current["device_ips"])
    old_domains = set(previous.get("domains", []))
    new_domains = set(current["domains"])
    old_flagged = previous.get("flagged_count", 0)
    new_flagged = current["flagged_count"]
    old_queries = previous.get("total_queries", 0)
    new_queries = current["total_queries"]

    added_devices = new_ips - old_ips
    removed_devices = old_ips - new_ips
    added_domains = new_domains - old_domains
    removed_domains = old_domains - new_domains
    query_delta = new_queries - old_queries
    flagged_delta = new_flagged - old_flagged

    old_names = previous.get("device_names", {})
    new_names = current.get("device_names", {})

    # Save current as new snapshot
    save_snapshot(current)

    # Render
    elapsed = current["timestamp"] - previous.get("timestamp", current["timestamp"])
    pretty_header(ip, f"diff · {fmt_duration(elapsed)} since last check")

    has_changes = (
        added_devices or removed_devices or
        added_domains or removed_domains or
        flagged_delta or query_delta
    )

    if not has_changes:
        print(f"  {C.GREEN}●{C.RESET}  {C.MUTED}nothing changed{C.RESET}")
        print()
        return

    # Query delta
    if query_delta:
        sign = "+" if query_delta > 0 else ""
        color = C.BOLD if query_delta > 0 else C.MUTED
        print(f"  {color}{sign}{fmt_num(query_delta)}{C.RESET} {C.MUTED}queries{C.RESET}")
    else:
        print(f"  {C.MUTED}0 new queries{C.RESET}")

    # Flagged delta
    if flagged_delta:
        sign = "+" if flagged_delta > 0 else ""
        print(f"  {C.AMBER}{sign}{flagged_delta}{C.RESET} {C.MUTED}flagged{C.RESET}")
    print()

    # New devices
    if added_devices:
        print(f"  {C.GREEN}new devices{C.RESET}")
        for dip in sorted(added_devices):
            name = new_names.get(dip, "?")
            print(f"    {C.BOLD}{name}{C.RESET}  {C.FAINT}{dip}{C.RESET}")
        print()

    # Removed devices
    if removed_devices:
        print(f"  {C.RED}gone{C.RESET}")
        for dip in sorted(removed_devices):
            name = old_names.get(dip, "?")
            print(f"    {C.MUTED}{name}{C.RESET}  {C.FAINT}{dip}{C.RESET}")
        print()

    # New domains
    if added_domains:
        show = sorted(added_domains)[:15]
        more = len(added_domains) - len(show)
        print(f"  {C.GREEN}new domains{C.RESET}  {C.FAINT}({len(added_domains)}){C.RESET}")
        for d in show:
            print(f"    {d}")
        if more > 0:
            print(f"    {C.FAINT}+{more} more{C.RESET}")
        print()

    # Removed domains (usually noise — devices stopped querying something)
    if removed_domains:
        show = sorted(removed_domains)[:10]
        more = len(removed_domains) - len(show)
        print(f"  {C.FAINT}not seen since{C.RESET}  {C.FAINT}({len(removed_domains)}){C.RESET}")
        for d in show:
            print(f"    {C.FAINT}{d}{C.RESET}")
        if more > 0:
            print(f"    {C.FAINT}+{more} more{C.RESET}")
        print()

    # If flagged increased, highlight it
    if flagged_delta > 0:
        print(f"  {C.AMBER}⚠  flagged count increased — check dashboard{C.RESET}")
        print()


# ════════════════════════════════════════════════════════════════════════
# FEATURE: monitor
# ════════════════════════════════════════════════════════════════════════

def cmd_monitor(args):
    ip = discover_with_loading(args.host)
    if not ip:
        # Device unreachable is an alert
        print(f"known: device unreachable")
        sys.exit(1)

    stats, devices, audit = fetch_all(ip)

    alerts = []

    if "error" in stats:
        alerts.append("stats endpoint error")
    else:
        flagged = stats.get("flagged_count", 0)
        if flagged > 0:
            alerts.append(f"{flagged} flagged queries")

    # Check for new devices vs snapshot
    previous = load_snapshot()
    if previous:
        old_ips = set(previous.get("device_ips", []))
        new_ips = set(d.get("ip") for d in devices) if isinstance(devices, list) else set()
        new_devs = new_ips - old_ips
        if new_devs:
            names = {d.get("ip"): d.get("name", d.get("ip")) for d in devices} if isinstance(devices, list) else {}
            for dip in new_devs:
                alerts.append(f"new device: {names.get(dip, dip)}")

        old_flagged = previous.get("flagged_count", 0)
        current_flagged = stats.get("flagged_count", 0) if "error" not in stats else 0
        if current_flagged > old_flagged:
            alerts.append(f"flagged increased ({old_flagged} → {current_flagged})")

    # Update snapshot
    snap = take_snapshot(ip)
    save_snapshot(snap)

    if alerts:
        for a in alerts:
            print(f"known: {a}")
        sys.exit(1)
    else:
    # Silent on success — composable with cron
        sys.exit(0)


# ════════════════════════════════════════════════════════════════════════
# FEATURE: allow
# ════════════════════════════════════════════════════════════════════════

def cmd_allow(args):
    ip = discover_with_loading(args.host)
    if not ip:
        view_not_found()
        sys.exit(1)

    action = args.allow_action

    if action == "add":
        pattern = args.pattern
        if not pattern:
            print(f"  {C.RED}pattern required{C.RESET}")
            sys.exit(1)
        result = fetch(ip, "/allowlist", method="PUT", data={"pattern": pattern})
        if "error" in result:
            print(f"  {C.RED}failed — {result['error']}{C.RESET}")
            sys.exit(1)
        print(f"  {C.GREEN}●{C.RESET}  {C.BOLD}{pattern}{C.RESET} {C.MUTED}added to allowlist{C.RESET}")
        if result.get("id"):
            print(f"  {C.FAINT}id: {result['id']}{C.RESET}")
        print()

    elif action == "rm":
        entry_id = args.id
        if not entry_id:
            print(f"  {C.RED}id required{C.RESET}")
            sys.exit(1)
        result = fetch(ip, f"/allowlist/{entry_id}", method="DELETE")
        if "error" in result:
            print(f"  {C.RED}failed — {result['error']}{C.RESET}")
            sys.exit(1)
        print(f"  {C.GREEN}●{C.RESET}  {C.MUTED}removed {entry_id}{C.RESET}")
        print()

    elif action == "ls":
        entries = fetch(ip, "/allowlist")
        if not isinstance(entries, list):
            if "error" in entries:
                print(f"  {C.RED}failed — {entries['error']}{C.RESET}")
            else:
                print(f"  {C.MUTED}allowlist empty{C.RESET}")
            print()
            return

        if not entries:
            print(f"  {C.MUTED}allowlist empty{C.RESET}")
            print()
            return

        pretty_header(ip, "allowlist")
        print(f"  {C.FAINT}{'pattern':<30} {'id':<22} {'added'}{C.RESET}")
        print(f"  {C.FAINT}{'─' * 62}{C.RESET}")
        for e in entries:
            pattern = truncate(e.get("pattern", "?"), 29)
            eid = truncate(e.get("id", "?"), 21)
            added = fmt_ago(e.get("created_at", 0))
            print(f"  {pad(pattern, 30)} {pad(eid, 22)} {C.MUTED}{added}{C.RESET}")
        print()

    else:
        print(f"  {C.RED}unknown allow action: {action}{C.RESET}")
        sys.exit(1)


# ── subcommands ───────────────────────────────────────────────────────

def cmd_report(args):
    ip = discover_with_loading(args.host)
    if not ip:
        view_not_found()
        sys.exit(1)

    fmt = "pretty"
    if args.table:
        fmt = "table"
    elif args.json:
        fmt = "json"
    elif args.compact:
        fmt = "compact"

    scope = args.scope or "all"
    run_report(ip, fmt, scope)


def cmd_watch(args):
    ip = discover_with_loading(args.host)
    if not ip:
        view_not_found()
        sys.exit(1)
    try:
        while True:
            clear()
            run_report(ip, "pretty", "all")
            print(f"  {C.FAINT}↻ every {WATCH_INTERVAL}s  ·  ctrl+c to stop{C.RESET}")
            time.sleep(WATCH_INTERVAL)
    except KeyboardInterrupt:
        print(f"\n  {C.MUTED}stopped{C.RESET}")


def cmd_debug(args):
    ip = discover_with_loading(args.host)
    if not ip:
        view_not_found()
        sys.exit(1)
    view_debug(ip)


# ── entry point ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="known",
        description="terminal stats for your Known device",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "commands:\n"
            "  known                       pretty summary (default)\n"
            "  known report                report with formatting options\n"
            "  known watch                 live refresh every 5s\n"
            "  known follow <ip>           live stream for one device\n"
            "  known diff                  what changed since last run\n"
            "  known monitor               silent check, exit 1 if alert\n"
            "  known allow add <pattern>   add allowlist entry\n"
            "  known allow rm <id>         remove allowlist entry\n"
            "  known allow ls              list allowlist entries\n"
            "  known debug                 raw internal state\n"
            "\n"
            "report formats:\n"
            "  --table / --json / --compact\n"
            "\n"
            "report scope:\n"
            "  --scope all / stats / devices / activity\n"
            "\n"
            "examples:\n"
            "  known report --table\n"
            "  known follow 192.168.1.20\n"
            "  known diff\n"
            "  known monitor && echo 'all clear'\n"
            "  known allow add *.doubleclick.net\n"
        ),
    )
    parser.add_argument("--host", metavar="IP", help="manual device IP (skip mDNS)")

    sub = parser.add_subparsers(dest="command")

    # report
    p_report = sub.add_parser("report", help="formatted report")
    fmt_group = p_report.add_mutually_exclusive_group()
    fmt_group.add_argument("--table", action="store_true", help="compact table format")
    fmt_group.add_argument("--json", action="store_true", help="raw JSON output")
    fmt_group.add_argument("--compact", action="store_true", help="one-line summary")
    p_report.add_argument(
        "--scope", choices=["all", "stats", "devices", "activity"],
        default="all", help="filter sections (default: all)"
    )

    # watch
    sub.add_parser("watch", help="live refresh every 5s")

    # follow
    p_follow = sub.add_parser("follow", help="live stream for one device")
    p_follow.add_argument("ip", help="device IP to follow")

    # diff
    sub.add_parser("diff", help="what changed since last run")

    # monitor
    sub.add_parser("monitor", help="silent check, exit 1 if alert")

    # allow
    p_allow = sub.add_parser("allow", help="manage allowlist")
    allow_sub = p_allow.add_subparsers(dest="allow_action")
    p_allow_add = allow_sub.add_parser("add", help="add pattern to allowlist")
    p_allow_add.add_argument("pattern", help="domain pattern (e.g. *.doubleclick.net)")
    p_allow_rm = allow_sub.add_parser("rm", help="remove allowlist entry")
    p_allow_rm.add_argument("id", help="entry id")
    allow_sub.add_parser("ls", help="list allowlist entries")

    # debug
    sub.add_parser("debug", help="raw internal state")

    args = parser.parse_args()

    if args.command == "report":
        cmd_report(args)
    elif args.command == "watch":
        cmd_watch(args)
    elif args.command == "follow":
        cmd_follow(args)
    elif args.command == "diff":
        cmd_diff(args)
    elif args.command == "monitor":
        cmd_monitor(args)
    elif args.command == "allow":
        cmd_allow(args)
    elif args.command == "debug":
        cmd_debug(args)
    else:
        # no subcommand: default pretty report
        ip = discover_with_loading(args.host)
        if not ip:
            view_not_found()
            sys.exit(1)
        run_report(ip, "pretty", "all")


if __name__ == "__main__":
    main()