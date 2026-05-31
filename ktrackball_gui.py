#!/usr/bin/env python3
"""
ktrackball GUI — assign actions to Kensington trackball buttons by position.

Six mappable slots:
  좌측 상단 · 우측 상단 · 상단(좌+우 동시) ·
  좌측 하단 · 우측 하단 · 하단(좌+우 동시)

The two "동시" slots are chords — pressing both corner buttons together fires
one action and suppresses the individual ones (handled by the daemon).

Runs as your normal user (Wayland-friendly). Detecting a button and saving the
config are delegated to the daemon script via `pkexec`.
"""

from __future__ import annotations

import os
import subprocess
import threading

try:
    import tomllib
except ModuleNotFoundError:
    raise SystemExit("Python 3.11+ required.")

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib  # noqa: E402

CONFIG_PATH = "/etc/ktrackball/config.toml"
HELPER = next((p for p in ("/opt/ktrackball/trackball_mapper.py",
                           os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "trackball_mapper.py"))
               if os.path.exists(p)), "/opt/ktrackball/trackball_mapper.py")
USER_TMP = os.path.join(GLib.get_user_cache_dir(), "ktrackball-pending.toml")

# Slot layout: (key, label, kind, members)
#   kind "single" -> one physical button you detect
#   kind "chord"  -> the two member slots pressed together
SLOTS = [
    ("top_left",     "좌측 상단",           "single", None),
    ("top_right",    "우측 상단",           "single", None),
    ("top_both",     "상단 (좌측+우측 동시)", "chord",  ("top_left", "top_right")),
    ("bottom_left",  "좌측 하단",           "single", None),
    ("bottom_right", "우측 하단",           "single", None),
    ("bottom_both",  "하단 (좌측+우측 동시)", "chord",  ("bottom_left", "bottom_right")),
]

CUSTOM_KEY = "__custom_key__"
CUSTOM_CMD = "__custom_cmd__"
NONE_SPEC = "__none__"

# Presets for single buttons (full set).
PRESETS: list[tuple[str, object]] = [
    ("일반 클릭 (그대로)",        {"type": "passthrough"}),
    ("뒤로 (Alt+←)",            {"type": "key", "keys": ["KEY_LEFTALT", "KEY_LEFT"]}),
    ("앞으로 (Alt+→)",          {"type": "key", "keys": ["KEY_LEFTALT", "KEY_RIGHT"]}),
    ("홈으로 (Alt+Home)",       {"type": "key", "keys": ["KEY_LEFTALT", "KEY_HOME"]}),
    ("Home 키",                {"type": "key", "keys": ["KEY_HOME"]}),
    ("End 키",                 {"type": "key", "keys": ["KEY_END"]}),
    ("Page Up",               {"type": "key", "keys": ["KEY_PAGEUP"]}),
    ("Page Down",             {"type": "key", "keys": ["KEY_PAGEDOWN"]}),
    ("맨 위로 (Ctrl+Home)",     {"type": "key", "keys": ["KEY_LEFTCTRL", "KEY_HOME"]}),
    ("맨 아래로 (Ctrl+End)",    {"type": "key", "keys": ["KEY_LEFTCTRL", "KEY_END"]}),
    ("새 탭 (Ctrl+T)",         {"type": "key", "keys": ["KEY_LEFTCTRL", "KEY_T"]}),
    ("탭 닫기 (Ctrl+W)",       {"type": "key", "keys": ["KEY_LEFTCTRL", "KEY_W"]}),
    ("새로고침 (F5)",          {"type": "key", "keys": ["KEY_F5"]}),
    ("복사 (Ctrl+C)",         {"type": "key", "keys": ["KEY_LEFTCTRL", "KEY_C"]}),
    ("붙여넣기 (Ctrl+V)",      {"type": "key", "keys": ["KEY_LEFTCTRL", "KEY_V"]}),
    ("볼 스크롤 (누르고 굴리기)", {"type": "scroll_hold"}),
    ("정밀 모드 (누르는 동안)",   {"type": "precision_hold"}),
    ("사용자 키조합…",          CUSTOM_KEY),
    ("명령 실행…",             CUSTOM_CMD),
]

# Chords can only carry one-shot actions (key / command).
def _chord_ok(spec) -> bool:
    if spec in (CUSTOM_KEY, CUSTOM_CMD):
        return True
    return isinstance(spec, dict) and spec["type"] == "key"


CHORD_PRESETS: list[tuple[str, object]] = \
    [("사용 안 함 (개별 버튼 유지)", NONE_SPEC)] + [p for p in PRESETS if _chord_ok(p[1])]


def spec_signature(spec: dict):
    if spec.get("type") == "key":
        return ("key", tuple(spec.get("keys", [])))
    return (spec.get("type"),)


def _reverse_map(presets):
    m = {}
    for label, spec in presets:
        if isinstance(spec, dict):
            m[spec_signature(spec)] = label
    return m


PRESET_BY_SIG = _reverse_map(PRESETS)
CHORD_BY_SIG = _reverse_map(CHORD_PRESETS)


def toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def render_inline(spec: dict) -> str:
    t = spec["type"]
    if t == "key":
        keys = ", ".join(f'"{k}"' for k in spec["keys"])
        return f'{{ type = "key", keys = [{keys}] }}'
    if t == "command":
        return f'{{ type = "command", command = "{toml_escape(spec["command"])}" }}'
    return f'{{ type = "{t}" }}'


def render_chord_block(members: list[str], spec: dict) -> str:
    arr = ", ".join(f'"{n}"' for n in members)
    lines = ["[[chords]]", f"buttons = [{arr}]"]
    if spec["type"] == "key":
        keys = ", ".join(f'"{k}"' for k in spec["keys"])
        lines += ['type = "key"', f"keys = [{keys}]"]
    elif spec["type"] == "command":
        lines += ['type = "command"', f'command = "{toml_escape(spec["command"])}"']
    return "\n".join(lines)


class Row:
    def __init__(self, app, slot):
        self.app = app
        self.key, self.label, self.kind, self.members = slot
        self.presets = PRESETS if self.kind == "single" else CHORD_PRESETS

        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box.set_margin_top(3); self.box.set_margin_bottom(3)

        name = Gtk.Label(label=self.label, xalign=0)
        name.set_width_chars(20)
        self.box.pack_start(name, False, False, 0)

        if self.kind == "single":
            self.code_label = Gtk.Label(label="(미감지)", xalign=0)
            self.code_label.set_width_chars(11)
            self.box.pack_start(self.code_label, False, False, 0)
            detect = Gtk.Button(label="버튼 감지")
            detect.connect("clicked", lambda *_: self.app.detect_button(self))
            self.box.pack_start(detect, False, False, 0)
        else:
            self.code_label = Gtk.Label(label="", xalign=0)
            self.code_label.set_width_chars(11 + 9)
            self.box.pack_start(self.code_label, False, False, 0)

        self.combo = Gtk.ComboBoxText()
        for lbl, _ in self.presets:
            self.combo.append_text(lbl)
        self.combo.connect("changed", self.on_combo_changed)
        self.box.pack_start(self.combo, False, False, 0)

        self.entry = Gtk.Entry()
        self.box.pack_start(self.entry, True, True, 0)

        self.combo.set_active(0)

    # -- combo / custom entry --------------------------------------------- #
    def on_combo_changed(self, _c):
        idx = self.combo.get_active()
        if idx < 0:
            return
        spec = self.presets[idx][1]
        if spec == CUSTOM_KEY:
            self.entry.set_sensitive(True)
            self.entry.set_placeholder_text("키 이름을 + 로: KEY_LEFTCTRL+KEY_T")
        elif spec == CUSTOM_CMD:
            self.entry.set_sensitive(True)
            self.entry.set_placeholder_text("실행할 명령: 예) firefox")
        else:
            self.entry.set_sensitive(False)
            self.entry.set_text("")

    def set_spec(self, spec: dict | None):
        if not spec or spec.get("type") is None:
            self.combo.set_active(0)
            return
        rev = PRESET_BY_SIG if self.kind == "single" else CHORD_BY_SIG
        label = rev.get(spec_signature(spec))
        if label:
            self._select(label); self.entry.set_text("")
        elif spec["type"] == "key":
            self._select("사용자 키조합…"); self.entry.set_text("+".join(spec["keys"]))
        elif spec["type"] == "command":
            self._select("명령 실행…"); self.entry.set_text(spec.get("command", ""))
        else:
            self.combo.set_active(0)
        self.on_combo_changed(self.combo)

    def _select(self, label):
        for i, (lbl, _) in enumerate(self.presets):
            if lbl == label:
                self.combo.set_active(i); return

    def get_spec(self) -> dict | None:
        idx = self.combo.get_active()
        spec = self.presets[idx][1]
        if spec == NONE_SPEC:
            return None
        if spec == CUSTOM_KEY:
            keys = [k.strip() for k in self.entry.get_text().split("+") if k.strip()]
            return {"type": "key", "keys": keys} if keys else None
        if spec == CUSTOM_CMD:
            cmd = self.entry.get_text().strip()
            return {"type": "command", "command": cmd} if cmd else None
        return dict(spec)

    # -- chord member display --------------------------------------------- #
    def refresh_chord(self):
        if self.kind != "chord":
            return
        a, b = (self.app.codes.get(m) for m in self.members)
        if a and b:
            self.code_label.set_text(f"{a}+{b}")
            self.combo.set_sensitive(True)
            self.entry.set_sensitive(self.presets[self.combo.get_active()][1]
                                     in (CUSTOM_KEY, CUSTOM_CMD))
        else:
            self.code_label.set_text("먼저 두 버튼 감지")
            self.combo.set_sensitive(False)
            self.entry.set_sensitive(False)


class App(Gtk.Window):
    def __init__(self):
        super().__init__(title="Kensington 트랙볼 설정")
        self.set_default_size(760, 420)
        self.set_border_width(12)
        self.codes: dict[str, str] = {}      # slot key -> BTN_ name (singles)
        self.extra_toplevel: dict = {}
        self.rows: dict[str, Row] = {}

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(outer)

        intro = Gtk.Label(xalign=0)
        intro.set_markup(
            "<b>트랙볼 버튼 위치별로 동작을 지정하세요.</b>\n"
            "단일 버튼은 ‘버튼 감지’로 인식하고, ‘동시’ 줄은 두 버튼을 함께 눌렀을 때의 "
            "동작입니다. 마지막에 <b>저장 후 적용</b>을 누르세요.")
        intro.set_line_wrap(True)
        outer.pack_start(intro, False, False, 0)

        grid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        outer.pack_start(grid, True, True, 0)
        for slot in SLOTS:
            row = Row(self, slot)
            self.rows[slot[0]] = row
            grid.pack_start(row.box, False, False, 0)

        # pointer (ball) speed slider — like a mouse-speed setting
        speed_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sl = Gtk.Label(label="볼 스피드", xalign=0); sl.set_width_chars(20)
        speed_box.pack_start(sl, False, False, 0)
        self.speed = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.2, 3.0, 0.05)
        self.speed.set_value(1.0)
        self.speed.set_hexpand(True)
        for v in (0.5, 1.0, 1.5, 2.0, 2.5):
            self.speed.add_mark(v, Gtk.PositionType.BOTTOM, f"{v}×")
        speed_box.pack_start(self.speed, True, True, 0)
        outer.pack_start(speed_box, False, False, 0)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.status = Gtk.Label(xalign=0)
        bar.pack_start(self.status, True, True, 0)
        save = Gtk.Button(label="저장 후 적용")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", self.on_save)
        bar.pack_end(save, False, False, 0)
        outer.pack_start(bar, False, False, 0)

        self.load_config()

    # -- load -------------------------------------------------------------- #
    def load_config(self):
        data = {}
        try:
            with open(CONFIG_PATH, "rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            self.set_status(f"{CONFIG_PATH} 없음 — 새로 시작합니다.")
        except Exception as exc:
            self.set_status(f"설정 읽기 오류: {exc}")

        self.extra_toplevel = {k: v for k, v in data.items()
                               if k not in ("buttons", "chords", "positions")}
        if not any(self.extra_toplevel.get(k) for k in
                   ("device_match", "device_names", "device_path")):
            self.extra_toplevel["device_match"] = ["Expert Wireless", "Slimblade"]
        self.speed.set_value(float(self.extra_toplevel.get("pointer_speed", 1.0)))

        positions = data.get("positions", {})
        self.codes = {k: v for k, v in positions.items() if k in self.rows}

        buttons = data.get("buttons", {})
        code_to_pos = {v: k for k, v in self.codes.items()}
        for code, spec in buttons.items():
            pos = code_to_pos.get(code)
            if pos and pos in self.rows:
                self.rows[pos].set_spec(spec)

        for codes_list, spec in [(c.get("buttons", []), c) for c in data.get("chords", [])]:
            cset = set(codes_list)
            for slot_key, member_keys in (("top_both", ("top_left", "top_right")),
                                          ("bottom_both", ("bottom_left", "bottom_right"))):
                members = {self.codes.get(m) for m in member_keys}
                if cset and cset == members:
                    self.rows[slot_key].set_spec(spec)

        for key, row in self.rows.items():
            if row.kind == "single" and self.codes.get(key):
                row.code_label.set_text(self.codes[key])
            row.refresh_chord()

    # -- detect ------------------------------------------------------------ #
    def detect_button(self, row: Row):
        self.set_status(f"‘{row.label}’ 버튼을 누르세요… (인증 창이 뜨면 먼저 인증)")

        def worker():
            try:
                out = subprocess.run(
                    ["pkexec", "python3", HELPER, "learn-once", "--timeout", "10"],
                    capture_output=True, text=True, timeout=40)
            except Exception as exc:
                GLib.idle_add(self.set_status, f"감지 실패: {exc}"); return
            name = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
            if out.returncode == 0 and name.startswith("BTN_"):
                GLib.idle_add(self._detected, row, name)
            else:
                msg = (out.stderr.strip() or "감지 실패").splitlines()[-1]
                GLib.idle_add(self.set_status, f"감지 실패: {msg}")

        threading.Thread(target=worker, daemon=True).start()

    def _detected(self, row: Row, name: str):
        # prevent the same physical button being assigned to two slots
        for k, v in list(self.codes.items()):
            if v == name and k != row.key:
                del self.codes[k]
                self.rows[k].code_label.set_text("(미감지)")
        self.codes[row.key] = name
        row.code_label.set_text(name)
        for r in self.rows.values():
            r.refresh_chord()
        self.set_status(f"‘{row.label}’ = {name}")

    # -- save -------------------------------------------------------------- #
    def build_toml(self) -> str:
        tl = self.extra_toplevel
        lines = ["# generated by ktrackball GUI", ""]
        if tl.get("device_match"):
            arr = ", ".join(f'"{toml_escape(s)}"' for s in tl["device_match"])
            lines.append(f"device_match = [{arr}]")
        if tl.get("device_names"):
            arr = ", ".join(f'"{toml_escape(s)}"' for s in tl["device_names"])
            lines.append(f"device_names = [{arr}]")
        if tl.get("device_path"):
            lines.append(f'device_path = "{toml_escape(tl["device_path"])}"')
        lines.append(f"chord_window_ms = {int(tl.get('chord_window_ms', 40))}")
        lines.append(f"pointer_speed = {round(self.speed.get_value(), 3)}")

        # positions (GUI metadata; daemon ignores it)
        if self.codes:
            lines += ["", "[positions]"]
            for k, v in self.codes.items():
                lines.append(f'{k} = "{v}"')

        lines += ["", "[buttons]"]
        for key in ("top_left", "top_right", "bottom_left", "bottom_right"):
            code = self.codes.get(key)
            spec = self.rows[key].get_spec()
            if code and spec:
                lines.append(f"{code} = {render_inline(spec)}")

        for slot_key, member_keys in (("top_both", ("top_left", "top_right")),
                                      ("bottom_both", ("bottom_left", "bottom_right"))):
            spec = self.rows[slot_key].get_spec()
            members = [self.codes.get(m) for m in member_keys]
            if spec and all(members):
                lines += ["", render_chord_block(members, spec)]
        return "\n".join(lines) + "\n"

    def on_save(self, _btn):
        # require both members before a chord can be used
        for slot_key, member_keys in (("top_both", ("top_left", "top_right")),
                                      ("bottom_both", ("bottom_left", "bottom_right"))):
            if self.rows[slot_key].get_spec() and not all(self.codes.get(m)
                                                          for m in member_keys):
                self.set_status(f"⚠ ‘{self.rows[slot_key].label}’를 쓰려면 "
                                "두 단일 버튼을 먼저 감지하세요.")
                return
        try:
            os.makedirs(os.path.dirname(USER_TMP), exist_ok=True)
            with open(USER_TMP, "w") as fh:
                fh.write(self.build_toml())
        except Exception as exc:
            self.set_status(f"임시 파일 쓰기 실패: {exc}"); return
        self.set_status("적용 중… (인증 창에서 비밀번호를 입력하세요)")

        def worker():
            try:
                out = subprocess.run(["pkexec", "python3", HELPER, "apply", USER_TMP],
                                     capture_output=True, text=True, timeout=60)
            except Exception as exc:
                GLib.idle_add(self.set_status, f"적용 실패: {exc}"); return
            if out.returncode == 0:
                GLib.idle_add(self.set_status, "✅ 저장 완료 — 트랙볼에 바로 적용됨.")
            else:
                msg = (out.stderr.strip() or out.stdout.strip()
                       or f"exit {out.returncode}").splitlines()[-1]
                GLib.idle_add(self.set_status, f"적용 실패: {msg}")

        threading.Thread(target=worker, daemon=True).start()

    def set_status(self, text: str):
        self.status.set_text(text)
        return False


def main():
    win = App()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
