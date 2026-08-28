"""
update.py: USB firmware-update apply path for the heron CLI.

Applies a signed .heronpkg to a Pico connected via mpremote. All
verification happens on the PC before touching the device; the device is
passive and receives staged files via mpremote fs cp.

Sequence:
  1. Find Pico serial port.
  2. Read device state (fw_version, has_update_key, active_slot).
  3. Verify bundle signature + file hashes on the PC.
  4. Stage files to .new paths on the Pico.
  5. Verify on-device hashes of .new files.
  6. Atomically rename .new into place.
  7. Write /fwver.json with the new version.
  8. machine.reset() the Pico.
  9. Poll /version over LAN until the new version is reported.

Auto-apply mode (no bundle argument) scans ~/.heron/updates/*.heronpkg,
picks the newest eligible bundle, and applies it.
"""

import glob as glob_mod
import hashlib
import json
import os
import subprocess
import sys
import time
from urllib.request import urlopen
from urllib.error import URLError

from . import bundle


WATCHED_DIR = os.path.join(os.path.expanduser("~"), ".heron", "updates")


def find_pico_serial():
    import glob
    candidates = glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")
    return candidates[0] if candidates else None


def mpremote_exec(port, code, timeout=15):
    cmd = ["mpremote", "connect", port, "exec", code]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result
    except subprocess.TimeoutExpired:
        print(f"mpremote timeout: {' '.join(cmd)}")
        return None
    except FileNotFoundError:
        print("ERROR: mpremote not found. Run: pip install mpremote")
        sys.exit(1)


def mpremote_fs_cp(port, local_path, remote_path, timeout=15):
    cmd = ["mpremote", "connect", port, "fs", "cp", local_path, f":{remote_path}"]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def read_device_version(port, host=None):
    """Read /version from the device via LAN if reachable, else via mpremote."""
    if host:
        try:
            with urlopen(f"http://{host}:8080/version", timeout=3) as r:
                return json.loads(r.read().decode("utf-8"))
        except (URLError, json.JSONDecodeError, Exception):
            pass

    # Fallback / initial read via mpremote.
    code = (
        "import json, provisioning; "
        "cfg = provisioning.load_config(); "
        "print(json.dumps({\"ssid\": cfg.get(\"ssid\"), \"pass\": cfg.get(\"pass\")}))"
    )
    r = mpremote_exec(port, code, timeout=10)
    if not r or r.returncode != 0:
        return None
    try:
        cfg = json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return None

    # Device may not be provisioned; if not, we cannot query LAN.
    if not cfg.get("ssid"):
        return None

    code = (
        "try:\n"
        "    import json\n"
        "except ImportError:\n"
        "    import ujson as json\n"
        "try:\n"
        "    import otp_keys\n"
        "    has_update_key = otp_keys.has_update_key()\n"
        "    active_slot = otp_keys.get_active_update_slot()\n"
        "except Exception:\n"
        "    has_update_key = False\n"
        "    active_slot = 0\n"
        "fw_version = 1\n"
        "try:\n"
        "    with open('/fwver.json', 'r') as f:\n"
        "        ver = json.load(f)\n"
        "    fw_version = int(ver.get('fw_version', 1))\n"
        "except Exception:\n"
        "    pass\n"
        "print(json.dumps({'fw_version': fw_version, 'has_update_key': has_update_key, 'active_slot': active_slot}))"
    )
    r = mpremote_exec(port, code, timeout=10)
    if not r or r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return None


def cleanup_new_files(port):
    """Remove any leftover .new files from a previous failed apply."""
    code = (
        "import os\n"
        "try:\n"
        "    files = os.listdir('/')\n"
        "except OSError:\n"
        "    files = []\n"
        "for name in files:\n"
        "    if name.endswith('.new') and (name[:-4] == 'main.py' or name.startswith('lib/')):\n"
        "        try:\n"
        "            os.remove(name)\n"
        "            print('removed', name)\n"
        "        except OSError as e:\n"
        "            print('failed', name, e)\n"
    )
    mpremote_exec(port, code, timeout=15)


def stage_file(port, local_path, remote_new_path):
    r = mpremote_fs_cp(port, local_path, remote_new_path, timeout=15)
    if not r or r.returncode != 0:
        print(f"ERROR: failed to stage {remote_new_path}: {r.stderr if r else 'no response'}")
        return False
    return True


def verify_staged_hash(port, remote_new_path, expected_hash):
    code = (
        f"import ubinascii, hashlib\n"
        f"try:\n"
        f"    with open('{remote_new_path}', 'rb') as f:\n"
        f"        data = f.read()\n"
        f"    h = ubinascii.hexlify(hashlib.sha256(data).digest()).decode()\n"
        f"    print(h)\n"
        f"except Exception as e:\n"
        f"    print('error', e)\n"
    )
    r = mpremote_exec(port, code, timeout=15)
    if not r or r.returncode != 0:
        return False
    actual = r.stdout.strip()
    if actual != expected_hash:
        print(f"ERROR: staged hash mismatch for {remote_new_path}")
        print(f"  expected: {expected_hash}")
        print(f"  actual:   {actual}")
        return False
    return True


def commit_update(port, fw_version):
    """Rename lib/*.new files into place, write /fwver.json, reboot.

    main.py was already overwritten directly during staging — we cannot
    rename a running main.py on MicroPython without crashing. The lib/
    files are not "running" in the same sense and can be renamed safely.
    """
    code = (
        "import os\n"
        "try:\n"
        "    import json\n"
        "except ImportError:\n"
        "    import ujson as json\n"
        "updated = False\n"
        "try:\n"
        "    files = os.listdir('/')\n"
        "except OSError:\n"
        "    files = []\n"
        "for name in files:\n"
        "    if not name.endswith('.new'):\n"
        "        continue\n"
        "    target = name[:-4]\n"
        "    if target != 'main.py' and not target.startswith('lib/'):\n"
        "        continue\n"
        "    try:\n"
        "        if target in files:\n"
        "            os.remove(target)\n"
        "        os.rename(name, target)\n"
        "        updated = True\n"
        "        print('committed', target)\n"
        "    except OSError as e:\n"
        "        print('failed', target, e)\n"
        "        raise\n"
        "ver = {'fw_version': " + str(fw_version) + ", 'applied_via': 'usb'}\n"
        "try:\n"
        "    with open('/fwver.json', 'r') as f:\n"
        "        existing = json.load(f)\n"
        "    existing.update(ver)\n"
        "    ver = existing\n"
        "except Exception:\n"
        "    pass\n"
        "with open('/fwver.json', 'w') as f:\n"
        "    json.dump(ver, f)\n"
        "print('wrote /fwver.json')\n"
        "print('resetting...')\n"
        "import machine\n"
        "machine.reset()\n"
    )
    r = mpremote_exec(port, code, timeout=15)
    # Reboot happens immediately; mpremote may exit with an error as the
    # serial port disappears. That is expected.
    return True


def wait_for_version(host, expected_version, timeout=60):
    """Poll /version on the device until it reports the expected version."""
    print(f"Waiting for device to come back with firmware version {expected_version}...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urlopen(f"http://{host}:8080/version", timeout=2) as r:
                data = json.loads(r.read().decode("utf-8"))
                if data.get("fw_version") == expected_version:
                    print(f"Device online: {data}")
                    return True
                print(f"  current version: {data.get('fw_version')}")
        except Exception:
            pass
        time.sleep(1)
    print("ERROR: device did not report expected version in time")
    return False


def _ensure_watched_dir():
    os.makedirs(WATCHED_DIR, exist_ok=True)


def _scan_bundles():
    """Return list of (path, Bundle-or-None) for .heronpkg files in watched dir."""
    _ensure_watched_dir()
    paths = sorted(glob_mod.glob(os.path.join(WATCHED_DIR, "*.heronpkg")))
    results = []
    for path in paths:
        try:
            with open(path, "rb") as f:
                data = f.read()
            # Parse without public key: we only need the manifest here.
            parsed = bundle.parse_bundle(data, public_key=None)
            results.append((path, parsed))
        except bundle.BundleError as e:
            print(f"  {C2.RED}✗{C2.RESET}  {os.path.basename(path)}  {C2.MUTED}manifest error: {e}{C2.RESET}")
            results.append((path, None))
        except Exception as e:
            print(f"  {C2.RED}✗{C2.RESET}  {os.path.basename(path)}  {C2.MUTED}read error: {e}{C2.RESET}")
            results.append((path, None))
    return results


class C2:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    MUTED = "\033[38;5;244m"
    FAINT = "\033[38;5;238m"
    GREEN = "\033[38;5;108m"
    RED = "\033[38;5;167m"
    AMBER = "\033[38;5;179m"
    SKY = "\033[38;5;110m"


if not (sys.stdout.isatty() and os.environ.get("NO_COLOR") is None):
    for _a in ("RESET", "BOLD", "DIM", "MUTED", "FAINT", "GREEN", "RED", "AMBER", "SKY"):
        setattr(C2, _a, "")


def _list_bundles(current_version=None):
    """Print all bundles in the watched folder."""
    results = _scan_bundles()
    if not results:
        print("No bundles found in ~/.heron/updates/")
        return

    print(f"  {C2.BOLD}Bundles{C2.RESET}  {C2.MUTED}in ~/.heron/updates/{C2.RESET}")
    print()
    print(f"  {'version':<10} {'min_from':<10} {'status':<14} {'file'}{C2.RESET}")
    print(f"  {C2.FAINT}{'─' * 62}{C2.RESET}")
    for path, parsed in results:
        name = os.path.basename(path)
        if parsed is None:
            status = f"{C2.RED}unreadable{C2.RESET}"
            ver = "-"
            min_from = "-"
        else:
            ver = parsed.fw_version
            min_from = parsed.min_from
            if current_version is not None:
                if ver <= current_version:
                    status = f"{C2.MUTED}not newer{C2.RESET}"
                elif min_from > current_version:
                    status = f"{C2.RED}min_from too low{C2.RESET}"
                else:
                    status = f"{C2.GREEN}eligible{C2.RESET}"
            else:
                status = f"{C2.AMBER}unknown{C2.RESET}"
        print(f"  {ver:<10} {min_from:<10} {status:<14} {name}")
    print()


def _pick_best_bundle(current_version):
    """Return (path, parsed) for the best eligible bundle, or (None, None)."""
    best = None
    best_ver = -1
    for path, parsed in _scan_bundles():
        if parsed is None:
            continue
        if parsed.fw_version <= current_version:
            continue
        if parsed.min_from > current_version:
            continue
        if parsed.fw_version > best_ver:
            best = (path, parsed)
            best_ver = parsed.fw_version
    return best if best else (None, None)


def _apply_bundle(args, bundle_path, parsed):
    """Apply a specific bundle to the Pico. This mirrors the previous cmd_update core."""
    # 1. Verify bundle signature and hashes on the PC first.
    print("Verifying bundle signature and hashes...")
    public_key = os.path.abspath(args.public_key)
    if not os.path.exists(public_key):
        print(f"ERROR: update public key not found: {public_key}")
        sys.exit(1)
    try:
        parsed = bundle.parse_bundle(_bundle_data(bundle_path), public_key=public_key)
    except bundle.BundleError as e:
        print(f"ERROR: bundle verification failed: {e}")
        sys.exit(1)
    if not parsed.file_hashes_match():
        print("ERROR: bundle file hashes do not match manifest")
        sys.exit(1)
    print(f"Bundle verified: version {parsed.fw_version}, {len(parsed.files)} files")

    # 2. Find Pico.
    port = args.port or find_pico_serial()
    if not port:
        print("ERROR: no Pico serial port found. Plug it in via USB.")
        sys.exit(1)
    print(f"Pico at {port}")

    # 3. Read device state.
    dev_ver = read_device_version(port, host=args.host)
    if dev_ver is None:
        print("WARNING: could not read device version; assuming fw_version=1")
        dev_ver = {"fw_version": 1, "has_update_key": False, "active_slot": 0}
    print(f"Device state: {dev_ver}")

    # 4. Eligibility checks.
    current = dev_ver.get("fw_version", 1)
    if not args.force and parsed.fw_version <= current:
        print(f"ERROR: bundle version {parsed.fw_version} is not newer than device version {current}")
        sys.exit(1)
    if not args.force and parsed.min_from > current:
        print(f"ERROR: bundle requires device version >= {parsed.min_from}, device is {current}")
        sys.exit(1)
    if not dev_ver.get("has_update_key"):
        print("ERROR: device has no burned update key. Run flash_known.py --burn-update-key first.")
        sys.exit(1)

    # 5. Stage files.
    cleanup_new_files(port)
    print("Staging files...")
    temp_dir = "/tmp/heron_update_stage"
    os.makedirs(temp_dir, exist_ok=True)
    staged = []
    for entry in parsed.manifest["files"]:
        path = entry["path"]
        data = parsed.files[path]
        expected_hash = entry["sha256"]
        local_tmp = os.path.join(temp_dir, os.path.basename(path))
        with open(local_tmp, "wb") as f:
            f.write(data)
        if path == "main.py":
            remote_target = path
            if not stage_file(port, local_tmp, remote_target):
                sys.exit(1)
            if not verify_staged_hash(port, remote_target, expected_hash):
                print("ERROR: staged file hash verification failed")
                sys.exit(1)
            staged.append((path, remote_target))
            print(f"  overwrote {path}")
        else:
            remote_new = path + ".new"
            if not stage_file(port, local_tmp, remote_new):
                sys.exit(1)
            if not verify_staged_hash(port, remote_new, expected_hash):
                print("ERROR: staged file hash verification failed")
                sys.exit(1)
            staged.append((path, remote_new))
            print(f"  staged {path}")

    # 6. Commit: rename into place, write /fwver.json, reboot.
    print("Committing update...")
    commit_update(port, parsed.fw_version)

    # 7. Wait for device to come back, either on LAN or by serial identify.
    if args.host:
        if wait_for_version(args.host, parsed.fw_version, timeout=args.timeout):
            print("Update complete.")
            return
    else:
        print("No --host provided; waiting for serial port to reappear...")
        start = time.time()
        while time.time() - start < args.timeout:
            if os.path.exists(port):
                time.sleep(3)  # give MicroPython time to boot
                dev_ver = read_device_version(port)
                if dev_ver and dev_ver.get("fw_version") == parsed.fw_version:
                    print(f"Update complete: {dev_ver}")
                    return
            time.sleep(1)
        print("ERROR: device did not come back with expected version")

    sys.exit(1)


def _bundle_data(bundle_path):
    with open(bundle_path, "rb") as f:
        return f.read()


def cmd_update(args):
    # --list: just print bundles and exit.
    if getattr(args, "list", False):
        # Try to get current device version for eligibility display.
        current = None
        port = args.port or find_pico_serial()
        if port:
            dev_ver = read_device_version(port, host=args.host)
            if dev_ver:
                current = dev_ver.get("fw_version")
        _list_bundles(current_version=current)
        return

    # Explicit bundle path: existing behavior.
    if getattr(args, "bundle", None):
        bundle_path = os.path.abspath(args.bundle)
        if not os.path.exists(bundle_path):
            print(f"ERROR: bundle not found: {bundle_path}")
            sys.exit(1)
        try:
            parsed = bundle.parse_bundle(_bundle_data(bundle_path), public_key=None)
        except bundle.BundleError as e:
            print(f"ERROR: bundle parse failed: {e}")
            sys.exit(1)
        _apply_bundle(args, bundle_path, parsed)
        return

    # Auto-discovery from watched folder.
    _ensure_watched_dir()
    port = args.port or find_pico_serial()
    auto = getattr(args, "auto", False)

    if not port:
        if auto:
            print("No Pico plugged in. Skipping auto-apply.")
            sys.exit(0)
        print("ERROR: no Pico serial port found. Plug it in via USB or provide --port.")
        sys.exit(1)

    dev_ver = read_device_version(port, host=args.host)
    if dev_ver is None:
        print("WARNING: could not read device version; assuming fw_version=1")
        dev_ver = {"fw_version": 1, "has_update_key": False, "active_slot": 0}
    current = dev_ver.get("fw_version", 1)

    bundle_path, parsed = _pick_best_bundle(current)
    if not bundle_path:
        print("No update available")
        sys.exit(0)

    print(f"Auto-selected bundle: {bundle_path} (version {parsed.fw_version})")
    _apply_bundle(args, bundle_path, parsed)


def add_subparser(sub):
    p = sub.add_parser("update", help="apply a signed .heronpkg to a Pico over USB")
    p.add_argument("bundle", nargs="?", default=None,
                   help="path to the .heronpkg file (omit to auto-pick from ~/.heron/updates/)")
    p.add_argument("--public-key", default="keys/update_public.pem",
                   help="path to update public key PEM (default: keys/update_public.pem)")
    p.add_argument("--port", default=None, help="Pico serial port (auto-detected if omitted)")
    p.add_argument("--host", default=None, help="device IP/hostname for post-update verification")
    p.add_argument("--timeout", type=int, default=60, help="seconds to wait for device to come back")
    p.add_argument("--force", action="store_true", help="skip version eligibility checks")
    p.add_argument("--list", action="store_true",
                   help="list all bundles in ~/.heron/updates/ with version/eligibility")
    p.add_argument("--auto", action="store_true",
                   help="silently exit 0 if no Pico is plugged in (for udev hooks)")
