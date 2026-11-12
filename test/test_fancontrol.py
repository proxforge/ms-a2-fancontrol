#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ms-a2-fancontrol without hardware: hwmon and DMI as directories, EC as a fake."""
import contextlib
import importlib.machinery
import io
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader("fancontrol", os.path.join(HERE, "..", "ms-a2-fancontrol"))
_spec = importlib.util.spec_from_loader("fancontrol", _loader)
fc = importlib.util.module_from_spec(_spec)
_loader.exec_module(fc)


class FakeEC:
    """EC RAM with the firmware values of BIOS 1.02."""

    def __init__(self, reject_writes=False):
        self.ram = [0] * 256
        self.ram[0x2F] = 0xB1
        for addr, value in zip(fc.SYS_LEVEL_ADDRS + fc.CPU_LEVEL_ADDRS, fc.SYS_LEVEL_DEFAULTS + fc.CPU_LEVEL_DEFAULTS):
            self.ram[addr] = value
        self.writes = []
        self.reject_writes = reject_writes

    def read(self, addr):
        return self.ram[addr]

    def write(self, addr, value):
        self.writes.append((addr, value))
        if not self.reject_writes:
            self.ram[addr] = value

    def tach_rpm(self):
        return (1512, 3199)

    def sys_levels(self):
        return [self.ram[a] for a in fc.SYS_LEVEL_ADDRS]

    def cpu_levels(self):
        return [self.ram[a] for a in fc.CPU_LEVEL_ADDRS]


def make_hwmon(root, nvme, nvme_s1, dimm, cpu=None):
    for i, (comp, s1) in enumerate(zip(nvme, nvme_s1)):
        base = os.path.join(root, "hwmon%d" % i)
        os.makedirs(base)
        with open(os.path.join(base, "name"), "w") as fh:
            fh.write("nvme\n")
        with open(os.path.join(base, "temp1_input"), "w") as fh:
            fh.write("%d\n" % (comp * 1000))
        with open(os.path.join(base, "temp2_input"), "w") as fh:
            fh.write("%d\n" % (s1 * 1000))
    for j, temp in enumerate(dimm):
        base = os.path.join(root, "hwmon%d" % (10 + j))
        os.makedirs(base)
        with open(os.path.join(base, "name"), "w") as fh:
            fh.write("spd5118\n")
        with open(os.path.join(base, "temp1_input"), "w") as fh:
            fh.write("%d\n" % (temp * 1000))
    if cpu is not None:
        base = os.path.join(root, "hwmon20")
        os.makedirs(base)
        with open(os.path.join(base, "name"), "w") as fh:
            fh.write("k10temp\n")
        with open(os.path.join(base, "temp1_input"), "w") as fh:
            fh.write("%d\n" % (cpu * 1000))


class ControlTest(unittest.TestCase):
    cfg = dict(fc.DEFAULTS)

    def test_cool_stays_at_firmware(self):
        self.assertEqual(fc.demand({"nvme": 45, "nvme_s1": 60, "dimm": 40, "cpu": 60}, self.cfg), (26, 16, "firmware"))

    def test_warm_nvme_raises_system_fan_linearly(self):
        sys_level, cpu_level, _source = fc.demand({"nvme": 56, "nvme_s1": 70, "dimm": 40, "cpu": 60}, self.cfg)
        self.assertEqual(sys_level, 38)
        self.assertEqual(cpu_level, 16)

    def test_hot_nvme_full_level(self):
        self.assertEqual(fc.demand({"nvme": 65, "nvme_s1": 78, "dimm": 40, "cpu": 60}, self.cfg)[:2], (50, 16))

    def test_controller_sensor_forces_emergency(self):
        self.assertEqual(fc.demand({"nvme": 51, "nvme_s1": 85, "dimm": 40, "cpu": 60}, self.cfg), (60, 60, "emergency"))

    def test_emergency_per_sensor(self):
        base = {"nvme": 45, "nvme_s1": 60, "dimm": 40, "cpu": 60}
        for key, value in (("nvme", 70), ("dimm", 68), ("cpu", 95)):
            temps = dict(base, **{key: value})
            self.assertEqual(fc.demand(temps, self.cfg)[:2], (60, 60), key)
        self.assertEqual(fc.demand(dict(base, dimm=67), self.cfg), (26, 50, "ram"))

    def test_warm_ram_raises_cpu_fan(self):
        self.assertEqual(fc.demand({"nvme": 45, "nvme_s1": 60, "dimm": 62, "cpu": 60}, self.cfg), (26, 50, "ram"))

    def test_no_temperatures_emergency(self):
        self.assertEqual(fc.demand(None, self.cfg), (60, 60, "emergency"))

    def test_hot_cpu_raises_cpu_fan_linearly(self):
        temps = {"nvme": 45, "nvme_s1": 60, "dimm": 40, "cpu": 85}
        self.assertEqual(fc.demand(temps, self.cfg), (26, 33, "cpu"))
        self.assertEqual(fc.demand(dict(temps, cpu=90), self.cfg), (26, 50, "cpu"))
        self.assertEqual(fc.demand(dict(temps, cpu=80), self.cfg), (26, 16, "firmware"))

    def test_higher_of_ram_and_cpu_wins(self):
        temps = {"nvme": 45, "nvme_s1": 60, "dimm": 56, "cpu": 82}
        self.assertEqual(fc.demand(temps, self.cfg), (26, 33, "ram"))
        self.assertEqual(fc.demand(dict(temps, cpu=88), self.cfg), (26, 43, "cpu"))

    def test_emergency_hysteresis(self):
        temps = {"nvme": 45, "nvme_s1": 60, "dimm": 40, "cpu": 91}
        self.assertEqual(fc.demand(temps, self.cfg)[2], "cpu")
        self.assertEqual(fc.demand(temps, self.cfg, active=True)[2], "emergency")
        self.assertEqual(fc.demand(dict(temps, cpu=89), self.cfg, active=True), (26, 47, "cpu"))
        self.assertEqual(fc.emergency_reasons(dict(temps, cpu=96, dimm=69), self.cfg), ["dimm 69 C", "cpu 96 C"])

    def test_lower_slowly_raise_immediately(self):
        self.assertEqual(fc.smooth(40, 26, 1), 39)
        self.assertEqual(fc.smooth(30, 45, 1), 45)
        self.assertEqual(fc.smooth(None, 26, 1), 26)

    def test_curve_never_below_firmware(self):
        self.assertEqual(fc.curve(30, fc.SYS_LEVEL_DEFAULTS), (30, 36, 36))
        self.assertEqual(fc.curve(0, fc.CPU_LEVEL_DEFAULTS), fc.CPU_LEVEL_DEFAULTS)


class EcTest(unittest.TestCase):
    def test_apply_and_restore(self):
        ec = FakeEC()
        self.assertTrue(fc.apply_levels(ec, 45, 30))
        self.assertEqual(ec.sys_levels(), [45, 45, 45])
        self.assertEqual(ec.cpu_levels(), [30, 30, 30, 30, 30, 33, 35])
        fc.restore(ec)
        self.assertEqual(ec.sys_levels(), list(fc.SYS_LEVEL_DEFAULTS))
        self.assertEqual(ec.cpu_levels(), list(fc.CPU_LEVEL_DEFAULTS))

    def test_unchanged_writes_nothing(self):
        ec = FakeEC()
        self.assertFalse(fc.apply_levels(ec, 26, 16))
        self.assertEqual(ec.writes, [])

    def test_write_error_is_reported(self):
        with self.assertRaises(IOError):
            fc.apply_levels(FakeEC(reject_writes=True), 40, 16)


class StopPostTest(unittest.TestCase):
    """ExecStopPost: SERVICE_RESULT from systemd decides between firmware values and emergency level."""

    def test_regular_stop_restores_firmware(self):
        ec = FakeEC()
        fc.apply_levels(ec, 45, 30)
        fc.stop_post(ec, dict(fc.DEFAULTS), "success")
        self.assertEqual(ec.sys_levels(), list(fc.SYS_LEVEL_DEFAULTS))
        self.assertEqual(ec.cpu_levels(), list(fc.CPU_LEVEL_DEFAULTS))

    def test_crash_sets_emergency(self):
        for result in ("signal", "exit-code", "core-dump", "timeout", ""):
            ec = FakeEC()
            fc.stop_post(ec, dict(fc.DEFAULTS), result)
            self.assertEqual(ec.sys_levels(), [60, 60, 60], result)
            self.assertEqual(ec.cpu_levels(), [60] * 7, result)

    def test_emergency_write_error_raises_nothing(self):
        fc.stop_post(FakeEC(reject_writes=True), dict(fc.DEFAULTS), "signal")


@unittest.skipIf(sys.platform == "win32", "flock is Linux only")
class LockTest(unittest.TestCase):
    """PortEC against a file as /dev/port: status 0x66 = OBF set, IBF clear; data 0x62 = 0x2a.
    Port writes are discarded, otherwise the command would overwrite the status byte in the file."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.port = os.path.join(self.dir, "port")
        data = bytearray(0x100)
        data[0x66], data[0x62] = 0x01, 0x2A
        with open(self.port, "wb") as fh:
            fh.write(data)
        self.lock = os.path.join(self.dir, "lock")

    def port_ec(self):
        ec = fc.PortEC(self.port, self.lock)
        ec._outb = lambda _port, _value: None
        return ec

    def test_read_without_foreign_lock(self):
        self.assertEqual(self.port_ec().read(0x30), 0x2A)

    def test_read_waits_for_foreign_lock(self):
        import fcntl

        holder = os.open(self.lock, os.O_RDWR | os.O_CREAT)
        fcntl.flock(holder, fcntl.LOCK_EX)
        result = []
        ec = self.port_ec()
        reader = threading.Thread(target=lambda: result.append(ec.read(0x30)))
        reader.start()
        time.sleep(0.2)
        self.assertEqual(result, [])
        fcntl.flock(holder, fcntl.LOCK_UN)
        reader.join(2)
        self.assertEqual(result, [0x2A])


def make_dmi(product="MS-A2", bios="1.02"):
    root = tempfile.mkdtemp()
    for name, value in (("product_name", product), ("bios_version", bios)):
        with open(os.path.join(root, name), "w") as fh:
            fh.write(value + "\n")
    return root


class PlatformTest(unittest.TestCase):
    def dmi(self, product="MS-A2", bios="1.02"):
        return make_dmi(product, bios)

    def test_ms_a2_is_accepted(self):
        fc.check_platform(FakeEC(), self.dmi())

    def test_other_device_is_rejected(self):
        with self.assertRaises(RuntimeError):
            fc.check_platform(FakeEC(), self.dmi(product="Other"))

    def test_other_bios_is_rejected(self):
        with self.assertRaises(RuntimeError):
            fc.check_platform(FakeEC(), self.dmi(bios="1.03"))

    def test_unexpected_ec_values_are_rejected(self):
        ec = FakeEC()
        ec.ram[0x30] = 10
        with self.assertRaises(RuntimeError):
            fc.check_platform(ec, self.dmi())


class DumpTest(unittest.TestCase):
    def dump(self, ec, bios="1.03"):
        dmi = make_dmi(bios=bios)
        hwmon = tempfile.mkdtemp()
        make_hwmon(hwmon, nvme=[58], nvme_s1=[74], dimm=[47], cpu=60)
        cpuinfo = os.path.join(hwmon, "cpuinfo")
        with open(cpuinfo, "w") as fh:
            fh.write("processor\t: 0\nmodel name\t: AMD Ryzen 9 9955HX 16-Core Processor\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fc.dump(ec, dmi, hwmon, cpuinfo)
        return out.getvalue()

    def test_dump_on_other_bios_reads_only(self):
        ec = FakeEC()
        text = self.dump(ec)
        self.assertEqual(ec.writes, [])
        self.assertIn("bios_version         1.03", text)
        self.assertIn("9955HX", text)
        self.assertIn("nvme                 1", text)
        self.assertIn("30:  1a 00 00 24 00 00 24", text)
        self.assertIn("profile 0x2f        0xb1", text)
        self.assertIn("system fan levels   [26, 36, 36]", text)
        self.assertIn("cpu 1512 rpm, system 3199 rpm", text)

    def test_dump_survives_ec_errors(self):
        class FailingEC(FakeEC):
            def read(self, addr):
                if addr == 0x30:
                    raise TimeoutError("EC timeout: obf")
                return super().read(addr)

            def tach_rpm(self):
                return None

        text = self.dump(FailingEC())
        self.assertIn("30:  -- 00", text)
        self.assertIn("not readable", text)

    def test_unsupported_exits_with_status_3(self):
        for command, dmi in (("run", make_dmi(bios="1.03")), ("status", make_dmi(bios="1.03")),
                             ("restore", make_dmi(bios="1.03")), ("stop-post", make_dmi(bios="1.03")),
                             ("dump", make_dmi(product="Other"))):
            ec = FakeEC()
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(fc.main(["ms-a2-fancontrol", command], lambda: ec, dmi), fc.EXIT_UNSUPPORTED)
            self.assertIn("nothing written", err.getvalue())
            self.assertEqual(ec.writes, [])

    def test_dump_needs_ms_a2(self):
        fc.check_product(make_dmi(bios="9.99"))
        with self.assertRaises(RuntimeError):
            fc.check_product(make_dmi(product="Other"))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self._old_root = fc.HWMON_ROOT
        self._old_state = fc.STATE_FILE
        fc.STATE_FILE = os.path.join(tempfile.mkdtemp(), "state")

    def tearDown(self):
        fc.HWMON_ROOT = self._old_root
        fc.STATE_FILE = self._old_state

    def run_cycles(self, ec, cycles):
        # read_temps uses the parameter default; redirect it to the test directory
        original = fc.read_temps
        fc.read_temps = lambda: original(self.root)
        try:
            fc.run(ec, dict(fc.DEFAULTS), sleep=lambda _s: None, cycles=cycles)
        finally:
            fc.read_temps = original

    def test_controls_and_restores_at_the_end(self):
        make_hwmon(self.root, nvme=[58, 61], nvme_s1=[74, 77], dimm=[47, 46])
        ec = FakeEC()
        self.run_cycles(ec, 2)
        first_sys_write = next(value for addr, value in ec.writes if addr == 0x30)
        self.assertGreater(first_sys_write, 26)
        self.assertEqual(ec.sys_levels(), list(fc.SYS_LEVEL_DEFAULTS))
        self.assertEqual(ec.cpu_levels(), list(fc.CPU_LEVEL_DEFAULTS))

    def test_missing_sensors_emergency(self):
        ec = FakeEC()
        self.run_cycles(ec, 1)
        self.assertIn((0x30, 60), ec.writes)
        self.assertIn((0x13, 60), ec.writes)

    def test_hot_cpu_emergency(self):
        make_hwmon(self.root, nvme=[50, 50], nvme_s1=[65, 65], dimm=[45, 45], cpu=96)
        ec = FakeEC()
        self.run_cycles(ec, 1)
        self.assertIn((0x30, 60), ec.writes)
        self.assertIn((0x25, 60), ec.writes)

    def test_error_in_loop_sets_emergency(self):
        make_hwmon(self.root, nvme=[50, 50], nvme_s1=[65, 65], dimm=[45, 45])
        ec = FakeEC()
        original = fc.demand
        fc.demand = lambda *_args: (_ for _ in ()).throw(RuntimeError("test error"))
        try:
            with self.assertRaises(RuntimeError):
                self.run_cycles(ec, 1)
        finally:
            fc.demand = original
        self.assertEqual(ec.sys_levels(), [60, 60, 60])
        self.assertEqual(ec.cpu_levels(), [60] * 7)

    def test_restart_after_emergency_lowers_slowly(self):
        make_hwmon(self.root, nvme=[45, 45], nvme_s1=[60, 60], dimm=[40, 40])
        ec = FakeEC()
        fc.apply_levels(ec, 60, 60)
        ec.writes.clear()
        self.run_cycles(ec, 2)
        sys_writes = [value for addr, value in ec.writes if addr == 0x30]
        self.assertEqual(sys_writes[:2], [59, 58])

    def test_cpu_sawtooth_is_gone(self):
        """CPU at 95 C: emergency until it is below 90 C, then the CPU curve instead of firmware level 16."""
        cpu_temps = iter([95, 93, 91, 89, 89, 89])
        ec = FakeEC()
        original = fc.read_temps
        fc.read_temps = lambda: {"nvme": 45, "nvme_s1": 60, "dimm": 45, "cpu": next(cpu_temps)}
        states = []
        original_write = fc.write_state
        fc.write_state = lambda state, path=None: states.append(dict(state))
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                fc.run(ec, dict(fc.DEFAULTS), sleep=lambda _s: None, cycles=6)
        finally:
            fc.read_temps = original
            fc.write_state = original_write
        self.assertEqual([s["source"] for s in states], ["emergency"] * 3 + ["cpu"] * 3)
        self.assertEqual([s["cpu_level"] for s in states], [60, 60, 60, 59, 58, 57])
        self.assertEqual(states[-1]["want_cpu"], 47)
        self.assertEqual(states[-1]["emergencies"], 1)
        self.assertIn("emergency #1: cpu 95 C", out.getvalue())
        self.assertIn("emergency over", out.getvalue())

    def test_state_file_written_and_removed(self):
        make_hwmon(self.root, nvme=[50, 50], nvme_s1=[65, 65], dimm=[45, 45], cpu=85)
        seen = []
        original_write = fc.write_state

        def capture(state, path=None):
            original_write(state, path)
            with open(fc.STATE_FILE) as fh:
                seen.append(json.load(fh))
        fc.write_state = capture
        try:
            self.run_cycles(FakeEC(), 1)
        finally:
            fc.write_state = original_write
        self.assertEqual((seen[0]["source"], seen[0]["cpu_level"]), ("cpu", 33))
        self.assertFalse(os.path.exists(fc.STATE_FILE))

    def test_without_k10temp_cpu_zero(self):
        make_hwmon(self.root, nvme=[50, 50], nvme_s1=[65, 65], dimm=[45, 45])
        self.assertEqual(fc.read_temps(self.root)["cpu"], 0.0)


class StatusTest(unittest.TestCase):
    def test_status_shows_source_and_emergencies(self):
        now = 1_000_000
        state = {"start": now - 2 * 3600, "emergencies": 3, "last_emergency": now - 600, "emergency": False,
                 "time": now - 5, "sys_level": 44, "cpu_level": 41, "want_sys": 44, "want_cpu": 40, "source": "cpu"}
        self.assertEqual(fc.describe_state(state, now), [
            "Service           cpu fan floor 41 (wanted 40, set by cpu), system fan floor 44 (wanted 44)",
            "Emergencies       3 since start 2 h 00 min ago, last 10 min ago"])
        state.update(emergency=True, time=now - 300)
        lines = fc.describe_state(state, now)
        self.assertTrue(lines[1].endswith(", ACTIVE"))
        self.assertIn("not running", lines[2])

    def test_status_without_service(self):
        root = tempfile.mkdtemp()
        make_hwmon(root, nvme=[50], nvme_s1=[65], dimm=[45], cpu=70)
        original = fc.read_temps
        fc.read_temps = lambda: original(root)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                fc.status(FakeEC(), os.path.join(root, "missing"))
        finally:
            fc.read_temps = original
        self.assertIn("no state (service not running)", out.getvalue())


class ConfigTest(unittest.TestCase):
    def write(self, text):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        return path

    def test_values_from_file(self):
        cfg = fc.load_config(self.write('NVME_LOW=48\nNVME_HIGH="60"  # comment\nUNKNOWN=1\n'))
        self.assertEqual((cfg["NVME_LOW"], cfg["NVME_HIGH"]), (48, 60))

    def test_missing_file_gives_defaults(self):
        self.assertEqual(fc.load_config("/nonexistent/ms-a2-fancontrol"), fc.DEFAULTS)

    def test_implausible_values_are_rejected(self):
        for text in ("NVME_LOW=70\n", "SYS_LEVEL_MAX=20\n", "CPU_LEVEL_MAX=99\n", "EMERGENCY_LEVEL=45\n",
                     "EMERGENCY_LEVEL=70\n", "DIMM_EMERGENCY=60\n", "CPU_LOW=90\n", "CPU_EMERGENCY=90\n",
                     "EMERGENCY_HYST=-1\n"):
            with self.assertRaises(ValueError):
                fc.load_config(self.write(text))


if __name__ == "__main__":
    unittest.main()
