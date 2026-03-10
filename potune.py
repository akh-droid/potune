#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

APP_TITLE = "Power UI Lite"

SELF_PATH = os.path.realpath(__file__)
PKEXEC_PATH = "/usr/bin/pkexec"
PYTHON_PATH = "/usr/bin/python3"
ASUSCTL_PATH = "/usr/bin/asusctl"
EPP_TOOL_PATH = "/usr/bin/x86_energy_perf_policy"

RESULT_PREFIX = "RESULT_JSON="
MAX_PROC_OUTPUT = 65536

SYSFS_PROFILE_PATHS = (
    "/sys/firmware/acpi/platform_profile",
    "/sys/devices/platform/asus-nb-wmi/platform_profile",
)

EPP_STRING_TO_NUM = {
    "performance": 0,
    "balance_performance": 96,
    "balanced_performance": 96,
    "balance": 128,
    "balanced": 128,
    "balance_power": 192,
    "balanced_power": 192,
    "power": 192,
    "power_save": 255,
    "powersave": 255,
}

_GOVERNOR_PATHS_CACHE: Optional[list[str]] = None
_EPP_RE = re.compile(r"\bepp\b\s*[:=]?\s*(\d{1,3})\b", re.IGNORECASE)
_EPP_RE2 = re.compile(r"\bhwp(?:\.|\s*)epp\b\s*[:=]?\s*(\d{1,3})\b", re.IGNORECASE)


@dataclass
class StepResult:
    ok: bool
    msg: str


@dataclass
class ApplyResult:
    asus: StepResult
    gov: StepResult
    epp: StepResult

    @property
    def all_ok(self) -> bool:
        return self.asus.ok and self.gov.ok and self.epp.ok

    def to_dict(self) -> dict:
        return {
            "asus": {"ok": self.asus.ok, "msg": self.asus.msg},
            "gov": {"ok": self.gov.ok, "msg": self.gov.msg},
            "epp": {"ok": self.epp.ok, "msg": self.epp.msg},
            "all_ok": self.all_ok,
        }


def is_root() -> bool:
    return os.geteuid() == 0


def is_executable_file(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def write_text(path: str, value: str) -> tuple[bool, str]:
    try:
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write(value)
        return True, f"ok ({path}={value})"
    except OSError as e:
        return False, f"{path}: {e}"


def run_cmd(cmd: list[str], timeout_s: float = 4.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"Timeout after {timeout_s:.1f}s: {' '.join(cmd)}"
    except OSError as e:
        return 1, "", str(e)


def get_power_source() -> str:
    base = "/sys/class/power_supply"
    if not os.path.isdir(base):
        return "Unknown"

    try:
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if read_text(os.path.join(p, "type")) == "Mains":
                return "AC" if read_text(os.path.join(p, "online")) == "1" else "Battery"
    except OSError:
        pass

    return "Unknown"


def available_asus_profiles() -> list[str]:
    for path in SYSFS_PROFILE_PATHS:
        txt = read_text(f"{path}_choices")
        if txt:
            vals = [x.strip().lower() for x in txt.split() if x.strip()]
            filtered = [v for v in vals if v in ("quiet", "balanced", "performance")]
            if filtered:
                return filtered
    return ["quiet", "balanced", "performance"]


def read_current_asus_profile() -> Optional[str]:
    for path in SYSFS_PROFILE_PATHS:
        if os.path.exists(path):
            v = read_text(path)
            if v:
                return v.lower()

    if is_executable_file(ASUSCTL_PATH):
        for cmd in ([ASUSCTL_PATH, "profile", "get"], [ASUSCTL_PATH, "profile", "-p"]):
            rc, out, _ = run_cmd(cmd)
            if rc == 0 and out:
                for line in out.splitlines():
                    low = line.lower()
                    if "active profile" in low:
                        return low.split(":", 1)[-1].strip()

    return None


def set_asus_profile(value: str) -> StepResult:
    value = (value or "").strip().lower()
    if value not in ("quiet", "balanced", "performance"):
        return StepResult(False, f"Invalid ASUS profile: {value!r}")

    last = ""

    if is_executable_file(ASUSCTL_PATH):
        for cmd in (
            [ASUSCTL_PATH, "profile", "set", value],
            [ASUSCTL_PATH, "profile", "-P", value],
            [ASUSCTL_PATH, "profile", "-p", value],
        ):
            rc, out, err = run_cmd(cmd, timeout_s=5.0)
            if rc == 0:
                return StepResult(True, f"asusctl ok ({' '.join(cmd)})")
            last = err or out or "asusctl command failed"
    else:
        last = "asusctl not found"

    for path in SYSFS_PROFILE_PATHS:
        if os.path.exists(path):
            ok, msg = write_text(path, value)
            if ok:
                return StepResult(True, msg)
            last = msg

    return StepResult(False, f"ASUS profile failed: {last}")


def _parse_epp_from_text(text: str) -> Optional[int]:
    if not text:
        return None

    lines = text.splitlines()
    cpu0_first = [ln for ln in lines if ln.strip().lower().startswith("cpu0")]

    for ln in cpu0_first + lines:
        m = _EPP_RE.search(ln) or _EPP_RE2.search(ln)
        if not m:
            continue
        try:
            v = int(m.group(1))
        except (TypeError, ValueError):
            continue
        if 0 <= v <= 255:
            return v

    return None


def _read_epp_from_tool(tool_path: str) -> Optional[int]:
    for cmd in (
        [tool_path],
        [tool_path, "--cpu", "0"],
        [tool_path, "--read"],
        [tool_path, "--read", "--cpu", "0"],
    ):
        try:
            p = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue

        if p.returncode != 0:
            continue

        val = _parse_epp_from_text("\n".join(x for x in (p.stdout, p.stderr) if x))
        if isinstance(val, int):
            return val

    return None


def _read_epp_from_sysfs() -> Optional[int]:
    for path in (
        "/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference",
        "/sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference",
    ):
        raw = read_text(path)
        if not raw:
            continue

        low = raw.lower()

        if raw.isdigit():
            v = int(raw)
            if 0 <= v <= 255:
                return v

        if low.startswith("0x"):
            try:
                v = int(low, 16)
            except ValueError:
                v = None
            if isinstance(v, int) and 0 <= v <= 255:
                return v

        mapped = EPP_STRING_TO_NUM.get(low)
        if isinstance(mapped, int):
            return mapped

    return None


def read_current_epp_num() -> Optional[int]:
    val = _read_epp_from_sysfs()
    if isinstance(val, int):
        return val

    if is_executable_file(EPP_TOOL_PATH):
        return _read_epp_from_tool(EPP_TOOL_PATH)

    return None


def set_epp_numeric(epp_num: int, verify_tries: int = 6, verify_delay: float = 0.2) -> StepResult:
    if not is_executable_file(EPP_TOOL_PATH):
        return StepResult(False, "x86_energy_perf_policy not found")

    epp_num = max(0, min(255, int(epp_num)))
    last_read = None

    def read_back() -> Optional[int]:
        cur = _read_epp_from_sysfs()
        if cur is None:
            cur = _read_epp_from_tool(EPP_TOOL_PATH)
        return cur

    def write_once() -> tuple[int, str, str]:
        return run_cmd(
            [EPP_TOOL_PATH, "--cpu", "all", "--hwp-epp", str(epp_num)],
            timeout_s=5.0,
        )

    rc, out, err = write_once()
    if rc != 0:
        return StepResult(False, f"x86_energy_perf_policy failed: {err or out or '(no output)'}")

    for _ in range(verify_tries):
        time.sleep(verify_delay)
        last_read = read_back()
        if last_read == epp_num:
            return StepResult(True, f"EPP set to {epp_num}")

    rc, out, err = write_once()
    if rc != 0:
        return StepResult(False, f"EPP retry failed: {err or out or '(no output)'}")

    for _ in range(verify_tries):
        time.sleep(verify_delay)
        last_read = read_back()
        if last_read == epp_num:
            return StepResult(True, f"EPP set to {epp_num}")

    return StepResult(False, f"Set {epp_num} but read back {last_read}")


def _governor_paths() -> list[str]:
    global _GOVERNOR_PATHS_CACHE
    if _GOVERNOR_PATHS_CACHE is None:
        _GOVERNOR_PATHS_CACHE = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")
    return _GOVERNOR_PATHS_CACHE


def available_governors() -> list[str]:
    txt = read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors")
    if not txt:
        return ["powersave", "performance"]

    vals = txt.split()
    ordered = [x for x in ("powersave", "performance", "schedutil") if x in vals]
    ordered += [x for x in vals if x not in ordered]
    return ordered


def read_current_governor() -> Optional[str]:
    return read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")


def set_governor(value: str) -> StepResult:
    paths = _governor_paths()
    if not paths:
        return StepResult(False, "Governor sysfs not found")

    value = (value or "").strip()
    allowed = set(available_governors())
    if value not in allowed:
        return StepResult(False, f"Governor {value!r} not available")

    ok = 0
    last_err = ""
    for path in paths:
        try:
            with open(path, "w", encoding="utf-8", errors="replace") as f:
                f.write(value)
            ok += 1
        except OSError as e:
            last_err = f"{path}: {e}"

    if ok == len(paths):
        return StepResult(True, f"Governor set on {ok} CPUs")
    if ok > 0:
        return StepResult(False, f"Governor partially set ({ok}/{len(paths)}). {last_err}")
    return StepResult(False, f"Governor failed. {last_err}")


def apply_all(asus: str, epp_num: int, gov: str) -> ApplyResult:
    target_asus = (asus or "").strip().lower()
    target_gov = (gov or "").strip()

    current_asus = (read_current_asus_profile() or "").strip().lower()
    current_gov = (read_current_governor() or "").strip()

    asus_reapplied = False
    gov_reapplied = False

    if current_asus == target_asus:
        asus_res = StepResult(True, f"ASUS unchanged ({target_asus})")
    else:
        asus_res = set_asus_profile(target_asus)
        asus_reapplied = asus_res.ok

    if current_gov == target_gov:
        gov_res = StepResult(True, f"Governor unchanged ({target_gov})")
    else:
        gov_res = set_governor(target_gov)
        gov_reapplied = gov_res.ok

    if asus_reapplied or gov_reapplied:
        time.sleep(0.35)

    epp_res = set_epp_numeric(
        epp_num,
        verify_tries=8 if (asus_reapplied or gov_reapplied) else 4,
        verify_delay=0.2,
    )

    return ApplyResult(asus=asus_res, gov=gov_res, epp=epp_res)


def parse_root_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--apply-root", action="store_true")
    p.add_argument("--asus", default="balanced")
    p.add_argument("--epp", type=int, default=128)
    p.add_argument("--gov", default="powersave")
    return p.parse_args(argv)


def make_result_line(result: ApplyResult) -> str:
    return RESULT_PREFIX + json.dumps(result.to_dict(), separators=(",", ":"))


def validate_result(result: object) -> bool:
    if not isinstance(result, dict):
        return False

    if set(result.keys()) != {"asus", "gov", "epp", "all_ok"}:
        return False

    if not isinstance(result.get("all_ok"), bool):
        return False

    for key in ("asus", "gov", "epp"):
        item = result.get(key)
        if not isinstance(item, dict):
            return False
        if set(item.keys()) != {"ok", "msg"}:
            return False
        if not isinstance(item.get("ok"), bool):
            return False
        if not isinstance(item.get("msg"), str):
            return False

    return True


def maybe_run_root_mode() -> None:
    if "--apply-root" not in sys.argv:
        return

    ns = parse_root_args(sys.argv[1:])
    result = apply_all(ns.asus, ns.epp, ns.gov)
    print(make_result_line(result), flush=True)
    sys.exit(0 if result.all_ok else 2)


maybe_run_root_mode()

from PyQt6.QtCore import Qt, QProcess
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


def epp_bucket(n: int) -> str:
    if n <= 16:
        return "Performance (Max)"
    if n <= 84:
        return "Performance"
    if n <= 115:
        return "Balanced Performance"
    if n <= 160:
        return "Balanced"
    if n <= 220:
        return "Balanced Power"
    return "Power Saving"


def append_limited(current: str, extra: str, max_len: int = MAX_PROC_OUTPUT) -> str:
    if len(current) >= max_len:
        return current
    remaining = max_len - len(current)
    return current + extra[:remaining]


def extract_result(stdout_text: str) -> Optional[dict]:
    for line in reversed(stdout_text.splitlines()):
        if not line.startswith(RESULT_PREFIX):
            continue

        payload = line[len(RESULT_PREFIX):]
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return None

        if validate_result(obj):
            return obj
        return None

    return None


class PowerUILite(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setMinimumWidth(430)

        self.proc: Optional[QProcess] = None
        self._stdout = ""
        self._stderr = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        title = QLabel(APP_TITLE)
        title.setStyleSheet("font-weight: 700;")
        root.addWidget(title)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)

        self.asus_combo = QComboBox()
        for v in available_asus_profiles():
            self.asus_combo.addItem(v)

        self.gov_combo = QComboBox()
        for v in available_governors():
            self.gov_combo.addItem(v)

        self.epp_spin = QSpinBox()
        self.epp_spin.setRange(0, 255)

        self.epp_slider = QSlider(Qt.Orientation.Horizontal)
        self.epp_slider.setRange(0, 255)

        self.epp_label = QLabel()

        form.addRow("ASUS profile", self.asus_combo)
        form.addRow("Governor", self.gov_combo)
        form.addRow("EPP value", self.epp_spin)
        form.addRow("EPP slider", self.epp_slider)
        form.addRow("EPP bucket", self.epp_label)

        root.addLayout(form)

        btns = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.reset_btn = QPushButton("Reset")
        self.apply_btn = QPushButton("Apply")
        btns.addWidget(self.refresh_btn)
        btns.addWidget(self.reset_btn)
        btns.addStretch(1)
        btns.addWidget(self.apply_btn)
        root.addLayout(btns)

        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.epp_slider.valueChanged.connect(self._sync_from_slider)
        self.epp_spin.valueChanged.connect(self._sync_from_spin)
        self.refresh_btn.clicked.connect(self.load_current_into_ui)
        self.reset_btn.clicked.connect(self.on_reset)
        self.apply_btn.clicked.connect(self.on_apply)

        self.load_current_into_ui()

    def _set_epp(self, value: int):
        value = max(0, min(255, int(value)))
        s1 = self.epp_slider.blockSignals(True)
        s2 = self.epp_spin.blockSignals(True)
        self.epp_slider.setValue(value)
        self.epp_spin.setValue(value)
        self.epp_slider.blockSignals(s1)
        self.epp_spin.blockSignals(s2)
        self.epp_label.setText(epp_bucket(value))

    def _sync_from_slider(self, value: int):
        self._set_epp(value)

    def _sync_from_spin(self, value: int):
        self._set_epp(value)

    def refresh_status(self):
        asus = read_current_asus_profile() or "unknown"
        gov = read_current_governor() or "unknown"
        epp = read_current_epp_num()
        power = get_power_source()
        epp_txt = str(epp) if isinstance(epp, int) else "unknown"
        self.status.setText(f"ASUS={asus} | Gov={gov} | EPP={epp_txt} | Power={power}")

    def load_current_into_ui(self):
        asus = read_current_asus_profile()
        if asus:
            idx = self.asus_combo.findText(asus)
            if idx >= 0:
                self.asus_combo.setCurrentIndex(idx)

        gov = read_current_governor()
        if gov:
            idx = self.gov_combo.findText(gov)
            if idx >= 0:
                self.gov_combo.setCurrentIndex(idx)

        epp = read_current_epp_num()
        self._set_epp(epp if isinstance(epp, int) else 128)
        self.refresh_status()

    def on_reset(self):
        for combo, value in ((self.asus_combo, "balanced"), (self.gov_combo, "powersave")):
            idx = combo.findText(value)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        self._set_epp(128)
        self.status.setText("Reset to defaults")

    def _set_busy(self, busy: bool, text: str):
        self.apply_btn.setEnabled(not busy)
        self.reset_btn.setEnabled(not busy)
        self.refresh_btn.setEnabled(not busy)
        self.status.setText(text)

    def on_apply(self):
        asus = self.asus_combo.currentText()
        gov = self.gov_combo.currentText()
        epp = self.epp_spin.value()

        if is_root():
            self._show_result(apply_all(asus, epp, gov).to_dict())
            self.load_current_into_ui()
            return

        if not is_executable_file(PKEXEC_PATH):
            QMessageBox.critical(self, "Missing pkexec", f"Missing executable: {PKEXEC_PATH}")
            return

        if not is_executable_file(PYTHON_PATH):
            QMessageBox.critical(self, "Missing python3", f"Missing executable: {PYTHON_PATH}")
            return

        self.proc = QProcess(self)
        self.proc.setProgram(PKEXEC_PATH)
        self.proc.setArguments([
            PYTHON_PATH,
            "-I",
            SELF_PATH,
            "--apply-root",
            "--asus", asus,
            "--epp", str(epp),
            "--gov", gov,
        ])
        self.proc.readyReadStandardOutput.connect(self._read_stdout)
        self.proc.readyReadStandardError.connect(self._read_stderr)
        self.proc.finished.connect(self._on_proc_finished)

        self._stdout = ""
        self._stderr = ""
        self._set_busy(True, "Applying...")
        self.proc.start()

    def _read_stdout(self):
        if self.proc:
            chunk = bytes(self.proc.readAllStandardOutput()).decode(errors="ignore")
            self._stdout = append_limited(self._stdout, chunk)

    def _read_stderr(self):
        if self.proc:
            chunk = bytes(self.proc.readAllStandardError()).decode(errors="ignore")
            self._stderr = append_limited(self._stderr, chunk)

    def _on_proc_finished(self, exit_code: int, _status):
        self._set_busy(False, "Done")

        if exit_code in (126, 127):
            QMessageBox.warning(self, "Cancelled", "pkexec was cancelled or denied.")
            self.refresh_status()
            return

        result = extract_result(self._stdout)
        if result is None:
            detail = self._stderr or self._stdout or "No output"
            QMessageBox.critical(self, "Apply failed", detail)
            self.refresh_status()
            return

        self._show_result(result)
        self.load_current_into_ui()

    def _show_result(self, result: dict):
        lines = []
        for name in ("asus", "gov", "epp"):
            item = result.get(name, {})
            prefix = "OK" if item.get("ok") else "FAIL"
            lines.append(f"{prefix} {name}: {item.get('msg', '')}")

        text = "\n".join(lines)
        if result.get("all_ok"):
            QMessageBox.information(self, "Applied", text)
        else:
            QMessageBox.warning(self, "Applied with issues", text)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    font = QFont()
    font.setPointSize(10)
    app.setFont(font)

    w = PowerUILite()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
