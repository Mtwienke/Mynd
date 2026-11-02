import json
from unittest.mock import MagicMock, patch

import pytest

from mynd.ai.runtime import MacHardwareMonitor, MacThermalMonitor, RuntimeControl


@pytest.fixture
def runtime_control(tmp_path):
    state_path = tmp_path / ".mynd-mode"
    return RuntimeControl(state_path)


def test_runtime_control_mode_persistence(runtime_control):
    # Default mode
    assert runtime_control.mode() == "normal"

    # Set to silent
    runtime_control.set_mode("silent")
    assert runtime_control.mode() == "silent"

    # Set to invalid mode should raise ValueError
    with pytest.raises(ValueError, match="Mode must be one of"):
        runtime_control.set_mode("invalid_mode")


def test_runtime_control_mode_fallback(tmp_path):
    state_path = tmp_path / ".mynd-mode"
    state_path.write_text("garbage_value", encoding="utf-8")
    rc = RuntimeControl(state_path)
    assert rc.mode() == "normal"


def test_runtime_control_ollama_options(runtime_control):
    # Normal mode: no options
    assert runtime_control.ollama_options(mode="normal") == {}

    # Silent mode, fan inactive: limited threads
    with patch("os.cpu_count", return_value=8):
        opts = runtime_control.ollama_options(mode="silent", fan_active=False)
        assert opts["num_thread"] == 2
        assert opts["num_batch"] == 64

    # Silent mode, fan active: strict limits
    opts = runtime_control.ollama_options(mode="silent", fan_active=True)
    assert opts["num_thread"] == 1
    assert opts["num_gpu"] == 0
    assert opts["num_batch"] == 32


def test_runtime_control_thermal_state_logic(runtime_control):
    with patch("mynd.ai.runtime.MacThermalMonitor.state", return_value="serious"):
        # Normal mode -> None
        with patch.object(runtime_control, "mode", return_value="normal"):
            assert runtime_control.thermal_state() is None

        # Silent mode -> "serious"
        with patch.object(runtime_control, "mode", return_value="silent"):
            assert runtime_control.thermal_state() == "serious"


def test_runtime_control_wait_until_safe(runtime_control):
    # Sequence of states: serious, serious, nominal
    with (
        patch.object(
            runtime_control, "thermal_state", side_effect=["serious", "serious", "nominal"]
        ),
        patch("time.sleep") as mock_sleep,
    ):
        runtime_control.wait_until_safe(poll_seconds=1.0)

        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(1.0)


def test_mac_hardware_monitor_snapshot_no_macmon(runtime_control):
    with patch("shutil.which", return_value=None):
        snapshot, error = MacHardwareMonitor.snapshot()
        assert snapshot is None
        assert "macmon is not installed" in error


def test_mac_hardware_monitor_snapshot_success(runtime_control):
    mock_data = {
        "temp": {"cpu_temp_avg": 55.5, "gpu_temp_avg": 48.2},
        "fans": [{"name": "Fan 0", "rpm": 1200.0}, {"name": "Fan 1", "rpm": 1100.0}],
    }
    mock_stdout = json.dumps(mock_data)

    with (
        patch("shutil.which", return_value="/usr/local/bin/macmon"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(stdout=mock_stdout, returncode=0)

        snapshot, error = MacHardwareMonitor.snapshot()
        assert snapshot == mock_data
        assert error is None


def test_mac_hardware_monitor_format_readings():
    data = {
        "temp": {"cpu_temp_avg": 55.54, "gpu_temp_avg": 48.21},
        "fans": [{"name": "Fan 0", "rpm": 1200.5}, {"name": "Fan 1", "rpm": 1100.1}],
    }
    formatted = MacHardwareMonitor.format_readings(data, None)
    assert "CPU 55.5°C" in formatted
    assert "GPU 48.2°C" in formatted
    assert "fans Fan 0 1200 RPM, Fan 1 1100 RPM" in formatted


def test_mac_thermal_monitor_supported():
    with patch("platform.system", return_value="Darwin"):
        assert MacThermalMonitor.supported() is True
    with patch("platform.system", return_value="Linux"):
        assert MacThermalMonitor.supported() is False
