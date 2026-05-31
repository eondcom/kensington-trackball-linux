#!/usr/bin/env python3
"""
ktrackball — Kensington trackball button mapper for Linux (Wayland/X11 agnostic).

Works at the kernel evdev/uinput level, so it does not depend on X11 tools
(xinput/xdotool) and runs fine under Wayland compositors such as Pop!_OS COSMIC.

It grabs the trackball's event device exclusively, forwards normal pointer
motion / scrolling through a virtual uinput device, and translates configured
physical buttons into actions:

  - "passthrough" : emit the same button (normal click)
  - "key"         : inject a key combo  (e.g. Alt+Left -> browser Back)
  - "command"     : run a shell command as the desktop user
  - "scroll_hold" : while held, ball movement becomes scrolling
  - "precision_hold": while held, pointer movement is slowed down

Subcommands:
  run            run the daemon (used by the systemd service)
  learn          interactive: press each trackball button to see its code/name
  list           list available input devices
  check-config   validate the config file and resolve all key/button names
"""

from __future__ import annotations

import argparse
import os
import select
import shutil
import subprocess
import sys
import time

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    print("This tool requires Python 3.11+ (for tomllib).", file=sys.stderr)
    sys.exit(1)

try:
    import evdev
    from evdev import InputDevice, UInput, ecodes as e
except ModuleNotFoundError:
    print("python-evdev is not installed. Install it with:\n"
          "  sudo apt-get install -y python3-evdev", file=sys.stderr)
    sys.exit(1)


DEFAULT_CONFIG = "/etc/ktrackball/config.toml"

# Standard pointer buttons we always make available on the virtual device.
MOUSE_BUTTONS = [
    e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE,
    e.BTN_SIDE, e.BTN_EXTRA, e.BTN_BACK, e.BTN_FORWARD, e.BTN_TASK,
]


# --------------------------------------------------------------------------- #
# Name <-> code helpers
# --------------------------------------------------------------------------- #
def code_to_name(code: int) -> str:
    """Return a readable name (e.g. 'BTN_SIDE') for an EV_KEY code."""
    names = e.bytype[e.EV_KEY].get(code)
    if isinstance(names, (list, tuple)):
        # Prefer a BTN_* name for buttons, else first.
        for n in names:
            if n.startswith("BTN_"):
                return n
        return names[0]
    return names or f"code:{code}"


def name_to_code(name: str) -> int:
    """Resolve 'KEY_LEFT' / 'BTN_SIDE' (or a raw int string) to an EV_KEY code."""
    name = name.strip()
    if name.isdigit():
        return int(name)
    code = e.ecodes.get(name)
    if code is None:
        raise ValueError(f"Unknown key/button name: {name!r}")
    return code


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self, path: str):
        with open(path, "rb") as fh:
            data = tomllib.load(fh)

        # Match the trackball by any of (in priority order):
        #   device_path    fixed /dev/input/eventXX
        #   device_names   exact pointer-interface names
        #   device_match   case-insensitive name substrings -> matches the same
        #                  trackball over USB receiver OR Bluetooth, since both
        #                  connection types share a product string.
        self.device_names: list[str] = []
        if data.get("device_name"):
            self.device_names.append(data["device_name"])
        self.device_names.extend(data.get("device_names", []))
        self.device_match: list[str] = [s.lower() for s in data.get("device_match", [])]
        self.device_path = data.get("device_path")
        if not self.device_names and not self.device_match and not self.device_path:
            raise ValueError(
                "config must set device_path, device_names or device_match")

        self.run_as_user = data.get("run_as_user") or _default_desktop_user()
        self.pointer_speed = float(data.get("pointer_speed", 1.0))   # 1.0 = unchanged
        self.precision_factor = float(data.get("precision_factor", 0.35))
        self.scroll_divisor = float(data.get("scroll_divisor", 18.0))
        self.scroll_invert = bool(data.get("scroll_invert", True))

        self.chord_window = float(data.get("chord_window_ms", 40)) / 1000.0

        # Single-button actions: { BTN_code: spec, ... }
        self.actions: dict[int, dict] = {}
        for btn_name, spec in data.get("buttons", {}).items():
            code = name_to_code(btn_name)
            self._validate_spec(f"button {btn_name}", spec, allow_hold=True)
            self.actions[code] = spec

        # Chords: two (or more) buttons pressed together fire one action and
        # suppress the individual button actions. Only one-shot actions
        # (key/command) make sense for a chord.
        self.chords: list[tuple[frozenset[int], dict]] = []
        for entry in data.get("chords", []):
            names = entry.get("buttons")
            if not isinstance(names, list) or len(names) < 2:
                raise ValueError("chord: 'buttons' must list at least 2 button names")
            codes = frozenset(name_to_code(n) for n in names)
            spec = {k: v for k, v in entry.items() if k != "buttons"}
            self._validate_spec(f"chord {names}", spec, allow_hold=False)
            self.chords.append((codes, spec))

        self.chorded_buttons: set[int] = set()
        for codes, _ in self.chords:
            self.chorded_buttons |= codes

    @staticmethod
    def _validate_spec(where: str, spec: dict, allow_hold: bool):
        if not isinstance(spec, dict) or "type" not in spec:
            raise ValueError(f"{where}: must be a table with a 'type'")
        atype = spec["type"]
        if atype == "key":
            spec["_keys"] = [name_to_code(k) for k in spec["keys"]]
        elif atype == "command":
            if "command" not in spec:
                raise ValueError(f"{where}: command action needs 'command'")
        elif atype == "passthrough":
            pass
        elif atype in ("scroll_hold", "precision_hold"):
            if not allow_hold:
                raise ValueError(f"{where}: {atype} cannot be used as a chord action")
        else:
            raise ValueError(f"{where}: unknown action type {atype!r}")

    def all_injected_keys(self) -> set[int]:
        keys: set[int] = set(MOUSE_BUTTONS)
        for spec in list(self.actions.values()) + [s for _, s in self.chords]:
            if spec["type"] == "key":
                keys.update(spec["_keys"])
        return keys

    def matches(self, name: str) -> bool:
        if name in self.device_names:
            return True
        low = name.lower()
        return any(sub in low for sub in self.device_match)


def _default_desktop_user() -> str | None:
    """Best-effort guess of the graphical user (for 'command' actions)."""
    try:
        out = subprocess.check_output(
            ["loginctl", "list-sessions", "--no-legend"], text=True)
        graphical = None
        for line in out.splitlines():
            parts = line.split()          # SESSION  UID  USER  SEAT  TTY ...
            if len(parts) >= 3:
                if "seat0" in parts:      # a local graphical seat
                    return parts[2]
                graphical = graphical or parts[2]
        if graphical:
            return graphical
    except Exception:
        pass
    return os.environ.get("SUDO_USER")


# --------------------------------------------------------------------------- #
# Device discovery
# --------------------------------------------------------------------------- #
def find_device(cfg: Config) -> str | None:
    if cfg.device_path and os.path.exists(cfg.device_path):
        return cfg.device_path
    for path in evdev.list_devices():
        try:
            d = InputDevice(path)
        except Exception:
            continue
        # Match the pointer interface (it carries the buttons + REL axes).
        if cfg.matches(d.name) and e.EV_REL in d.capabilities():
            d.close()
            return path
        d.close()
    return None


# --------------------------------------------------------------------------- #
# Command execution (as the desktop user, with a Wayland-capable env)
# --------------------------------------------------------------------------- #
def run_command(command: str, user: str | None):
    env_user_id = None
    if user:
        try:
            import pwd
            pw = pwd.getpwnam(user)
            env_user_id = (pw.pw_uid, pw.pw_gid, pw.pw_dir)
        except KeyError:
            env_user_id = None

    if env_user_id is None:
        subprocess.Popen(command, shell=True, start_new_session=True)
        return

    uid, gid, home = env_user_id
    runtime = f"/run/user/{uid}"
    env = {
        "HOME": home,
        "USER": user,
        "LOGNAME": user,
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "XDG_RUNTIME_DIR": runtime,
        "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", "wayland-1"),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
    }

    def demote():
        os.setgid(gid)
        os.setuid(uid)

    subprocess.Popen(command, shell=True, env=env,
                     preexec_fn=demote, start_new_session=True)


# --------------------------------------------------------------------------- #
# The daemon
# --------------------------------------------------------------------------- #
class Mapper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.scroll_active = False
        self.precision_active = False
        self._scroll_acc_x = 0.0
        self._scroll_acc_y = 0.0
        self._prec_acc_x = 0.0
        self._prec_acc_y = 0.0
        self._ps_acc_x = 0.0
        self._ps_acc_y = 0.0
        # buffered REL events for the current SYN frame
        self._pending_rel: list[tuple[int, int]] = []
        # chord bookkeeping (only for buttons that appear in a chord)
        self._pending: dict[int, float] = {}      # code -> decision deadline
        self._consumed: set[int] = set()           # pressed, swallowed by a chord
        self._held_passthrough: set[int] = set()   # deferred click press emitted
        self._active_holds: dict[int, str] = {}     # code -> 'scroll' | 'precision'

    # -- key injection ----------------------------------------------------- #
    def tap_keys(self, ui: UInput, keys: list[int]):
        for k in keys:                 # press in order (modifiers first)
            ui.write(e.EV_KEY, k, 1)
        ui.syn()
        for k in reversed(keys):       # release in reverse
            ui.write(e.EV_KEY, k, 0)
        ui.syn()

    def _fire_oneshot(self, ui: UInput, spec: dict):
        if spec["type"] == "key":
            self.tap_keys(ui, spec["_keys"])
        elif spec["type"] == "command":
            run_command(spec["command"], self.cfg.run_as_user)

    def _set_hold(self, mode: str, on: bool):
        if mode == "scroll":
            self.scroll_active = on
            if not on:
                self._scroll_acc_x = self._scroll_acc_y = 0.0
        elif mode == "precision":
            self.precision_active = on
            if not on:
                self._prec_acc_x = self._prec_acc_y = 0.0

    # -- per-event handling ------------------------------------------------ #
    def handle(self, ui: UInput, ev, now: float):
        if ev.type == e.EV_KEY:
            self._on_key(ui, ev, now)
        elif ev.type == e.EV_REL:
            self._handle_rel(ev)
        elif ev.type == e.EV_SYN and ev.code == e.SYN_REPORT:
            self._flush(ui)
        # other event types (MSC etc.) are intentionally dropped

    def next_timeout(self, now: float):
        """Seconds until the next pending chord decision, or None."""
        if not self._pending:
            return None
        return max(0.0, min(self._pending.values()) - now)

    def flush_pending(self, ui: UInput, now: float):
        """Commit single-button actions whose chord window has elapsed."""
        for code in [c for c, dl in self._pending.items() if dl <= now]:
            self._pending.pop(code, None)
            self._commit_single(ui, code, held=True)

    def _on_key(self, ui: UInput, ev, now: float):
        code, val = ev.code, ev.value
        if code not in self.cfg.chorded_buttons:
            self._handle_key_immediate(ui, ev)     # non-chorded: act now
            return
        if val == 1:
            self._chorded_press(ui, code, now)
        elif val == 0:
            self._chorded_release(ui, code)
        # val == 2 (autorepeat) ignored for chorded buttons

    def _chorded_press(self, ui: UInput, code: int, now: float):
        self._pending[code] = now + self.cfg.chord_window
        held = set(self._pending)
        for codes, spec in self.cfg.chords:
            if codes <= held:                       # all members down & undecided
                self._fire_oneshot(ui, spec)
                for c in codes:
                    self._pending.pop(c, None)
                    self._consumed.add(c)
                return

    def _chorded_release(self, ui: UInput, code: int):
        if code in self._consumed:
            self._consumed.discard(code)
        elif code in self._held_passthrough:
            self._held_passthrough.discard(code)
            ui.write(e.EV_KEY, code, 0)
            ui.syn()
        elif code in self._active_holds:
            self._set_hold(self._active_holds.pop(code), False)
        elif code in self._pending:                 # released within the window
            self._pending.pop(code, None)
            self._commit_single(ui, code, held=False)

    def _commit_single(self, ui: UInput, code: int, held: bool):
        spec = self.cfg.actions.get(code) or {"type": "passthrough"}
        t = spec["type"]
        if t in ("key", "command"):
            self._fire_oneshot(ui, spec)
        elif t == "passthrough":
            ui.write(e.EV_KEY, code, 1)
            ui.syn()
            if held:
                self._held_passthrough.add(code)    # release will emit the release
            else:
                ui.write(e.EV_KEY, code, 0)         # quick tap -> full click now
                ui.syn()
        elif t in ("scroll_hold", "precision_hold") and held:
            mode = "scroll" if t == "scroll_hold" else "precision"
            self._active_holds[code] = mode
            self._set_hold(mode, True)

    def _handle_key_immediate(self, ui: UInput, ev):
        spec = self.cfg.actions.get(ev.code)
        if spec is None:
            # unmapped button -> passthrough
            ui.write(e.EV_KEY, ev.code, ev.value)
            ui.syn()
            return

        atype = spec["type"]
        if atype == "passthrough":
            ui.write(e.EV_KEY, ev.code, ev.value)
            ui.syn()
        elif atype == "key":
            if ev.value == 1:          # on press only -> single keystroke
                self.tap_keys(ui, spec["_keys"])
        elif atype == "command":
            if ev.value == 1:
                run_command(spec["command"], self.cfg.run_as_user)
        elif atype == "scroll_hold":
            self._set_hold("scroll", ev.value != 0)
        elif atype == "precision_hold":
            self._set_hold("precision", ev.value != 0)

    def _handle_rel(self, ev):
        self._pending_rel.append((ev.code, ev.value))

    def _flush(self, ui: UInput):
        if not self._pending_rel:
            ui.syn()
            return

        if self.scroll_active:
            self._flush_scroll(ui)
        elif self.precision_active:
            self._flush_precision(ui)
        else:
            self._flush_normal(ui)
        self._pending_rel.clear()

    def _flush_normal(self, ui: UInput):
        sp = self.cfg.pointer_speed
        for code, val in self._pending_rel:
            if sp != 1.0 and code == e.REL_X:
                self._ps_acc_x += val * sp
                out = int(self._ps_acc_x); self._ps_acc_x -= out
            elif sp != 1.0 and code == e.REL_Y:
                self._ps_acc_y += val * sp
                out = int(self._ps_acc_y); self._ps_acc_y -= out
            else:
                out = val
            if out:
                ui.write(e.EV_REL, code, out)
        ui.syn()

    def _flush_scroll(self, ui: UInput):
        dx = sum(v for c, v in self._pending_rel if c == e.REL_X)
        dy = sum(v for c, v in self._pending_rel if c == e.REL_Y)
        sign = -1.0 if self.cfg.scroll_invert else 1.0
        self._scroll_acc_y += sign * dy / self.cfg.scroll_divisor
        self._scroll_acc_x += dx / self.cfg.scroll_divisor
        emitted = False
        ticks_y = int(self._scroll_acc_y)
        ticks_x = int(self._scroll_acc_x)
        if ticks_y:
            ui.write(e.EV_REL, e.REL_WHEEL, ticks_y)
            self._scroll_acc_y -= ticks_y
            emitted = True
        if ticks_x:
            ui.write(e.EV_REL, e.REL_HWHEEL, ticks_x)
            self._scroll_acc_x -= ticks_x
            emitted = True
        if emitted:
            ui.syn()

    def _flush_precision(self, ui: UInput):
        f = self.cfg.precision_factor
        emitted = False
        for code, val in self._pending_rel:
            if code == e.REL_X:
                self._prec_acc_x += val * f
                out = int(self._prec_acc_x)
                self._prec_acc_x -= out
            elif code == e.REL_Y:
                self._prec_acc_y += val * f
                out = int(self._prec_acc_y)
                self._prec_acc_y -= out
            else:
                out = val
            if out:
                ui.write(e.EV_REL, code, out)
                emitted = True
        if emitted:
            ui.syn()


def build_uinput(src: InputDevice, cfg: Config) -> UInput:
    caps = src.capabilities(absinfo=False)
    for drop in (e.EV_SYN, e.EV_MSC, e.EV_FF, e.EV_FF_STATUS, e.EV_PWR, e.EV_LED):
        caps.pop(drop, None)
    caps[e.EV_KEY] = sorted(set(caps.get(e.EV_KEY, [])) | cfg.all_injected_keys())
    rel = set(caps.get(e.EV_REL, []))
    rel.update([e.REL_WHEEL, e.REL_HWHEEL])
    caps[e.EV_REL] = sorted(rel)
    return UInput(caps, name="ktrackball-virtual", vendor=0x1209, product=0x0001)


def run_daemon(cfg: Config):
    target = (", ".join(cfg.device_names + [f"*{m}*" for m in cfg.device_match])
              or cfg.device_path)
    print(f"[ktrackball] starting; target device: {target}", flush=True)
    while True:
        path = find_device(cfg)
        if not path:
            print("[ktrackball] device not found, retrying in 3s...", flush=True)
            time.sleep(3)
            continue
        try:
            dev = InputDevice(path)
            print(f"[ktrackball] opened {path} ({dev.name})", flush=True)
            ui = build_uinput(dev, cfg)
            dev.grab()
            mapper = Mapper(cfg)
            while True:
                # wake on input, or when a pending chord decision is due
                timeout = mapper.next_timeout(time.time())
                r, _, _ = select.select([dev.fd], [], [], timeout)
                if r:
                    for ev in dev.read():
                        mapper.handle(ui, ev, time.time())
                mapper.flush_pending(ui, time.time())
        except (OSError, IOError) as exc:
            print(f"[ktrackball] device error ({exc}); will reconnect", flush=True)
            time.sleep(2)
        except KeyboardInterrupt:
            print("\n[ktrackball] stopping.", flush=True)
            break
        finally:
            try:
                dev.ungrab(); dev.close()
            except Exception:
                pass
            try:
                ui.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# learn / list / check
# --------------------------------------------------------------------------- #
def cmd_learn(args):
    cfg = None
    path = args.device
    if not path and args.config and os.path.exists(args.config):
        cfg = Config(args.config)
        path = find_device(cfg)
    if not path:
        print("Specify --device /dev/input/eventXX or a valid --config.",
              file=sys.stderr)
        print("Run 'trackball_mapper.py list' to see devices.", file=sys.stderr)
        return 1
    dev = InputDevice(path)
    print(f"Listening on {path} ({dev.name}).")
    print("Press each trackball button. Move the ball to confirm motion. Ctrl+C to quit.\n")
    try:
        dev.grab()
    except OSError:
        print("(could not grab exclusively — that's fine for learning)\n")
    try:
        for ev in dev.read_loop():
            if ev.type == e.EV_KEY:
                state = {0: "release", 1: "press", 2: "hold"}.get(ev.value, ev.value)
                print(f"  button  code={ev.code:<4} name={code_to_name(ev.code):<14} {state}")
            elif ev.type == e.EV_REL:
                rname = e.bytype[e.EV_REL].get(ev.code, ev.code)
                if ev.code in (e.REL_WHEEL, e.REL_HWHEEL):
                    print(f"  scroll  {rname} {ev.value:+d}")
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        try:
            dev.ungrab()
        except Exception:
            pass
        dev.close()
    return 0


def cmd_list(args):
    for path in sorted(evdev.list_devices()):
        try:
            d = InputDevice(path)
        except Exception:
            continue
        caps = d.capabilities()
        kinds = []
        if e.EV_REL in caps:
            kinds.append("pointer")
        if e.EV_KEY in caps and e.KEY_A in caps.get(e.EV_KEY, []):
            kinds.append("keyboard")
        print(f"{path:<20} {d.name!r:<45} {','.join(kinds) or '-'}")
        d.close()
    return 0


def cmd_check(args):
    cfg = Config(args.config)
    print(f"OK: config parsed.")
    print(f"  device_names: {cfg.device_names}")
    print(f"  device_match: {cfg.device_match}")
    print(f"  device_path : {cfg.device_path}")
    print(f"  run_as_user : {cfg.run_as_user}")
    print(f"  mapped buttons:")
    for code, spec in cfg.actions.items():
        extra = ""
        if spec["type"] == "key":
            extra = " -> " + "+".join(code_to_name(k) for k in spec["_keys"])
        elif spec["type"] == "command":
            extra = f" -> {spec['command']!r}"
        print(f"    {code_to_name(code):<12} {spec['type']}{extra}")
    found = find_device(cfg)
    print(f"  resolved device path: {found or '(not currently connected)'}")
    return 0


def cmd_run(args):
    cfg = Config(args.config)
    run_daemon(cfg)
    return 0


SERVICE = "ktrackball.service"


def _service_active() -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", SERVICE]).returncode == 0


def cmd_learn_once(args):
    """Capture a single button press and print its name. Used by the GUI.

    Pauses the daemon (it grabs the device exclusively), reads one press,
    then restores the daemon. Must run as root (reads /dev/input)."""
    cfg = Config(args.config)
    resume = _service_active()
    if resume:
        subprocess.run(["systemctl", "stop", SERVICE])
    dev = None
    result = None
    try:
        path = None
        t0 = time.time()
        while time.time() - t0 < 5:
            path = find_device(cfg)
            if path:
                break
            time.sleep(0.2)
        if not path:
            print("ERROR: trackball not found", file=sys.stderr)
            return 2
        dev = InputDevice(path)
        try:
            dev.grab()
        except OSError:
            pass
        deadline = time.time() + args.timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            r, _, _ = select.select([dev.fd], [], [], remaining)
            if not r:
                break
            for ev in dev.read():
                if ev.type == e.EV_KEY and ev.value == 1:
                    result = code_to_name(ev.code)
                    break
            if result:
                break
    finally:
        if dev is not None:
            try:
                dev.ungrab()
            except Exception:
                pass
            dev.close()
        if resume:
            subprocess.run(["systemctl", "start", SERVICE])
    if result:
        print(result)
        return 0
    print("ERROR: no button pressed (timeout)", file=sys.stderr)
    return 3


def cmd_apply(args):
    """Validate a config file and install it as the active config, then
    restart the daemon. Must run as root (writes /etc, controls systemd)."""
    Config(args.source)  # raises on invalid config
    dst_dir = os.path.dirname(DEFAULT_CONFIG)
    os.makedirs(dst_dir, exist_ok=True)
    shutil.copyfile(args.source, DEFAULT_CONFIG)
    os.chmod(DEFAULT_CONFIG, 0o644)
    os.chown(DEFAULT_CONFIG, 0, 0)
    subprocess.run(["systemctl", "restart", SERVICE])
    print("applied")
    return 0


def main():
    # --config is accepted both before AND after the subcommand, so that
    # `... learn --config X` works as naturally as `... --config X learn`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=DEFAULT_CONFIG,
                        help="path to config.toml")

    p = argparse.ArgumentParser(prog="trackball_mapper",
                                description="Kensington trackball button mapper",
                                parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="run the daemon",
                   parents=[common]).set_defaults(func=cmd_run)
    sub.add_parser("list", help="list input devices",
                   parents=[common]).set_defaults(func=cmd_list)
    sub.add_parser("check-config", help="validate config",
                   parents=[common]).set_defaults(func=cmd_check)

    lp = sub.add_parser("learn", help="show codes for buttons you press",
                        parents=[common])
    lp.add_argument("--device", help="/dev/input/eventXX (overrides config)")
    lp.set_defaults(func=cmd_learn)

    lo = sub.add_parser("learn-once", parents=[common],
                        help="capture one button press, print its name (for the GUI)")
    lo.add_argument("--timeout", type=float, default=10.0)
    lo.set_defaults(func=cmd_learn_once)

    ap = sub.add_parser("apply", parents=[common],
                        help="validate + install a config file and restart the daemon")
    ap.add_argument("source", help="path to a config.toml to install")
    ap.set_defaults(func=cmd_apply)

    args = p.parse_args()
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
