"""Runtime controls for live inference mode and macOS thermal awareness."""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import platform
import shutil
import subprocess  # nosec B404 - used only for the trusted local `macmon` CLI
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import ClassVar, TypedDict, cast


class FanReading(TypedDict, total=False):
    """One fan entry reported by the `macmon` CLI."""

    name: str
    rpm: float


class SensorSnapshot(TypedDict, total=False):
    """Open-ended hardware metrics payload produced by `macmon`."""

    temp: dict[str, float]
    fans: list[FanReading]


class MacThermalMonitor:
    """Read the public NSProcessInfo thermal state on macOS."""

    _objc = None
    _foundation = None
    _process_info = None
    _thermal_selector = None
    _message_object = None
    _message_integer = None

    @classmethod
    def supported(cls) -> bool:
        return platform.system() == "Darwin"

    @classmethod
    def state(cls) -> str | None:
        """Return nominal/fair/serious/critical, or None when unavailable."""
        if not cls.supported():
            return None
        try:
            cls._load_api()
            raw = cls._message_integer(cls._process_info, cls._thermal_selector)
            return {0: "nominal", 1: "fair", 2: "serious", 3: "critical"}.get(raw)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return None

    @classmethod
    def _load_api(cls) -> None:
        if cls._process_info is not None:
            return
        objc_path = ctypes.util.find_library("objc")
        if not objc_path:
            raise OSError("Objective-C runtime is unavailable")
        cls._objc = ctypes.CDLL(objc_path)
        cls._foundation = ctypes.CDLL(
            "/System/Library/Frameworks/Foundation.framework/Foundation"
        )

        get_class = cls._objc.objc_getClass
        get_class.argtypes = [ctypes.c_char_p]
        get_class.restype = ctypes.c_void_p
        register_selector = cls._objc.sel_registerName
        register_selector.argtypes = [ctypes.c_char_p]
        register_selector.restype = ctypes.c_void_p

        send_address = ctypes.cast(cls._objc.objc_msgSend, ctypes.c_void_p).value
        if send_address is None:
            raise OSError("Objective-C message dispatcher is unavailable")
        cls._message_object = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )(send_address)
        cls._message_integer = ctypes.CFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p
        )(send_address)

        process_info_class = get_class(b"NSProcessInfo")
        process_info_selector = register_selector(b"processInfo")
        cls._thermal_selector = register_selector(b"thermalState")
        cls._process_info = cls._message_object(process_info_class, process_info_selector)
        if not cls._process_info or not cls._thermal_selector:
            raise RuntimeError("Could not access NSProcessInfo thermal state")


class MacHardwareMonitor:
    """Read Apple Silicon temperatures and fan RPM through the optional macmon CLI."""

    @staticmethod
    def snapshot() -> tuple[SensorSnapshot | None, str | None]:
        if platform.system() != "Darwin":
            return None, "real temperature/fan readings are only implemented for macOS"
        executable = shutil.which("macmon")
        if not executable:
            return None, "macmon is not installed (install with `brew install macmon`)"
        try:
            result = subprocess.run(  # nosec B603 - fixed args, resolved executable, timeout set
                [executable, "pipe", "--samples", "1", "--interval", "500"],
                check=True,
                capture_output=True,
                text=True,
                timeout=8,
            )
            data = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return None, f"macmon read failed: {exc}"
        if not isinstance(data, dict):
            return None, "macmon returned an unexpected metrics format"
        return cast(SensorSnapshot, data), None

    @staticmethod
    def format_readings(data: SensorSnapshot | None, error: str | None) -> str:
        if data is None:
            return error or "sensor readings unavailable"
        temps = data.get("temp") or {}
        values = []
        for label, key in (("CPU", "cpu_temp_avg"), ("GPU", "gpu_temp_avg")):
            value = temps.get(key)
            if isinstance(value, (int, float)):
                values.append(f"{label} {value:.1f}°C")
        fans = data.get("fans") or []
        fan_readings = [
            f"{fan.get('name') or f'fan{index}'} {fan['rpm']:.0f} RPM"
            for index, fan in enumerate(fans)
            if isinstance(fan, dict) and isinstance(fan.get("rpm"), (int, float))
        ]
        if fan_readings:
            values.append("fans " + ", ".join(fan_readings))
        if not values:
            return "macmon returned no CPU/GPU temperature or fan readings"
        return "; ".join(values)

    @staticmethod
    def fan_rpms(data: SensorSnapshot | None) -> list[float]:
        if not data:
            return []
        return [
            float(fan["rpm"])
            for fan in data.get("fans", [])
            if isinstance(fan, dict) and isinstance(fan.get("rpm"), (int, float))
        ]


class RuntimeControl:
    """Shared mode file and thermal policy used by a running ingestion job."""

    VALID_MODES: ClassVar[frozenset[str]] = frozenset({"normal", "silent"})
    HOT_STATES: ClassVar[frozenset[str]] = frozenset({"serious", "critical"})

    def __init__(self, state_path: Path):
        self.state_path = state_path
        self._sensor_snapshot: SensorSnapshot | None = None
        self._sensor_error: str | None = None
        self._sensor_read_at = 0.0

    def hardware_snapshot(self, refresh: bool = False) -> tuple[SensorSnapshot | None, str | None]:
        if refresh or time.monotonic() - self._sensor_read_at >= 2.0:
            self._sensor_snapshot, self._sensor_error = MacHardwareMonitor.snapshot()
            self._sensor_read_at = time.monotonic()
        return self._sensor_snapshot, self._sensor_error

    def fan_active(self, snapshot: SensorSnapshot | None = None) -> bool:
        if snapshot is None:
            snapshot, _error = self.hardware_snapshot()
        return any(rpm > 0 for rpm in MacHardwareMonitor.fan_rpms(snapshot))

    def mode(self) -> str:
        try:
            value = self.state_path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            return "normal"
        return value if value in self.VALID_MODES else "normal"

    def set_mode(self, mode: str) -> None:
        value = mode.strip().lower()
        if value not in self.VALID_MODES:
            raise ValueError(f"Mode must be one of: {', '.join(sorted(self.VALID_MODES))}")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp.write_text(value + "\n", encoding="utf-8")
        os.replace(temp, self.state_path)

    def thermal_state(self) -> str | None:
        if self.mode() != "silent":
            return None
        return MacThermalMonitor.state()

    def report_thermal_state(
        self, checkpoint: str, report: Callable[[str], None] | None = None
    ) -> None:
        log = report or (lambda _message: None)
        if self.mode() != "silent":
            state = "inactive (normal mode)"
        else:
            state = self.thermal_state() or "unavailable"
        snapshot, sensor_error = self.hardware_snapshot(refresh=True)
        detail = MacHardwareMonitor.format_readings(snapshot, sensor_error)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        log(f"Thermal state after {checkpoint}: {state}; {detail} [{timestamp}]")

    def ollama_options(
        self,
        mode: str | None = None,
        fan_active: bool = False,
    ) -> dict[str, int]:
        selected_mode = mode or self.mode()
        if selected_mode != "silent":
            return {}
        cores = os.cpu_count() or 2
        if fan_active:
            return {"num_thread": 1, "num_batch": 32, "num_gpu": 0}
        return {"num_thread": max(1, cores // 4), "num_batch": 64}

    def wait_until_safe(
        self,
        report: Callable[[str], None] | None = None,
        poll_seconds: float = 10.0,
    ) -> None:
        log = report or (lambda _message: None)
        reported = False
        while self.thermal_state() in self.HOT_STATES:
            if not reported:
                log("macOS thermal state is serious; pausing local model work until it cools")
                reported = True
            time.sleep(poll_seconds)
        if reported:
            log("macOS thermal state is below serious; resuming local model work")

