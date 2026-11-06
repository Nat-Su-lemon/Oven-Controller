import math
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import cure_gui as gui


class SimulatorSafetyTests(unittest.TestCase):
    def setUp(self):
        # Deterministic clock and sensor; exercise actual simulator commands.
        with patch.object(gui.threading.Thread, "start"):
            self.sim = gui.SimLink()
        self.sim._read_sensor = lambda: 25.0
        self.drain()

    def drain(self):
        messages = []
        while not self.sim.out.empty():
            messages.append(self.sim.out.get_nowait())
        return messages

    def step(self, seconds):
        for _ in range(seconds):
            self.sim._step(1.0)

    def test_stuck_sensor_fault_freezes_clock_and_reports_reason(self):
        self.sim.write_line("GOTO 38")
        self.step(90)
        self.sim.write_line("CLEAR")  # no fault: leave active watchdog intact
        self.step(90)
        self.assertTrue(self.sim.heater)
        self.assertFalse(self.sim.fault)
        self.step(1)
        self.assertTrue(self.sim.fault)
        self.assertFalse(self.sim.heater)
        self.assertEqual(self.sim.fault_reason, "no_heat")
        frozen = self.sim.t
        self.step(10)
        self.assertEqual(self.sim.t, frozen)
        self.sim.write_line("START")
        self.sim.write_line("GOTO 0")
        self.assertEqual(self.sim.t, frozen)
        self.assertTrue(self.sim.fault)
        self.sim.write_line("STATUS")
        data = [line for line in self.drain() if line.startswith("DATA")][-1]
        tag, fields = gui.parse_line(data)
        self.assertEqual(tag, "DATA")
        self.assertEqual(fields["reason"], "no_heat")
        self.assertEqual(fields["heater"], "0")
        self.assertAlmostEqual(float(fields["R"]), 10000, delta=1)
        self.assertGreater(float(fields["adc"]), 190)
        self.sim.write_line("CLEAR")
        self.assertFalse(self.sim.fault)
        self.assertEqual(self.sim.fault_reason, "none")

    def test_pause_still_protects_heating(self):
        self.sim.write_line("GOTO 38")
        self.sim.write_line("PAUSE")
        self.step(181)
        self.assertEqual(self.sim.fault_reason, "no_heat")

    def test_invalid_sensor_immediate_off_and_latched_after_three(self):
        self.sim.write_line("GOTO 38")
        self.step(1)
        self.assertTrue(self.sim.heater)
        self.sim._read_sensor = lambda: math.nan
        self.step(1)
        self.assertFalse(self.sim.heater)
        self.assertFalse(self.sim.fault)
        self.step(2)
        self.assertEqual(self.sim.fault_reason, "sensor")
        self.sim.write_line("CLEAR")
        self.assertTrue(self.sim.fault)
        self.sim._read_sensor = lambda: 25.0
        self.sim.write_line("CLEAR")
        self.assertFalse(self.sim.fault)

    def test_overtemp_cannot_be_cleared_until_cool(self):
        self.sim._read_sensor = lambda: 210.0
        self.sim.write_line("START")
        self.assertFalse(self.sim.running)
        self.assertEqual(self.sim.fault_reason, "overtemp")
        self.sim.write_line("CLEAR")
        self.assertTrue(self.sim.fault)
        self.sim.write_line("ABORT")
        self.sim.write_line("DEFAULTS")
        self.assertTrue(self.sim.fault)
        self.sim._read_sensor = lambda: 25.0
        self.sim.write_line("CLEAR")
        self.assertFalse(self.sim.fault)

    def test_transient_invalid_samples_do_not_restart_watchdog(self):
        self.sim.write_line("GOTO 38")
        self.step(90)
        self.sim._read_sensor = lambda: math.nan
        self.step(1)
        self.sim._read_sensor = lambda: 25.0
        self.step(91)
        self.assertEqual(self.sim.fault_reason, "no_heat")

    def test_parameter_validation_matches_firmware(self):
        for command in ["SET maxtemp nan", "SET maxtemp inf", "SET maxtemp 999", "GOTO nan", "GOTO inf"]:
            self.sim.write_line(command)
            self.assertTrue(self.drain()[-1].startswith("ERR"), command)
        self.assertEqual(self.sim.p, gui.DEFAULTS)
        self.assertFalse(self.sim.running)

    def test_normal_full_cure_has_no_false_faults(self):
        self.sim._read_sensor = lambda: self.sim.oven
        self.sim.write_line("START")
        self.step(int(self.sim._total()) + 1)
        self.assertFalse(self.sim.fault)
        self.assertEqual(self.sim.phase, "COMPLETE")
        self.assertFalse(self.sim.heater)

    def test_constants_match_sketch(self):
        source = (Path(__file__).resolve().parents[1] / "firmware/cure_controller/cure_controller.ino").read_text()
        for name, value in [("A", gui.THERMISTOR_A), ("B", gui.THERMISTOR_B), ("C", gui.THERMISTOR_C),
                            ("R2", gui.DIVIDER_R)]:
            match = re.search(rf"const (?:float|uint32_t) {name} = ([\d.]+)", source)
            self.assertIsNotNone(match, name)
            self.assertEqual(float(match[1]), value, name)
        limits = re.findall(r'\{"(\w+)",\s*&[PW]\.\w+,\s*([\d.]+),\s*([\d.]+)\}', source)
        self.assertEqual(len(limits), len(gui.PARAMS))
        for name, low, high in limits:
            self.assertEqual(gui.PARAM_LIMITS[name], (float(low), float(high)))
        defaults = re.search(r'DEFAULT_WATCHDOG = \{([\d.]+), ([\d.]+)\}', source)
        self.assertEqual(float(defaults[1]), gui.DEFAULTS["heattimeout"])
        self.assertEqual(float(defaults[2]), gui.DEFAULTS["heatrise"])

    def test_adjustable_watchdog_timeout_and_defaults(self):
        self.sim.write_line("SET heattimeout 30")
        self.sim.write_line("SET heatrise 1.5")
        self.sim.write_line("PARAMS")
        _, fields = gui.parse_line(self.drain()[-1])
        self.assertEqual(float(fields["heattimeout"]), 30)
        self.assertEqual(float(fields["heatrise"]), 1.5)
        self.sim.write_line("GOTO 38")
        self.step(20)
        self.assertFalse(self.sim.fault)
        self.sim.write_line("SET heattimeout 10")
        self.step(1)
        self.assertEqual(self.sim.fault_reason, "no_heat")
        self.sim.write_line("SET heatrise 3")
        self.assertTrue(self.sim.fault)
        self.sim.write_line("DEFAULTS")
        self.assertEqual(self.sim.p, gui.DEFAULTS)
        self.assertTrue(self.sim.fault)

    def test_watchdog_rise_setting_changes_required_progress(self):
        for required_rise, should_fault in [(1.0, False), (3.0, True)]:
            with self.subTest(required_rise=required_rise):
                self.setUp()
                self.sim.write_line("SET heattimeout 10")
                self.sim.write_line(f"SET heatrise {required_rise}")
                self.sim.write_line("GOTO 38")
                self.step(1)
                self.sim._read_sensor = lambda: 27.0
                self.step(10)
                self.assertEqual(self.sim.fault, should_fault)

    def test_invalid_watchdog_values_are_rejected(self):
        for command in ["SET heattimeout 0", "SET heattimeout 1801", "SET heattimeout nan",
                        "SET heatrise 0", "SET heatrise 21", "SET heatrise inf"]:
            self.sim.write_line(command)
            self.assertTrue(self.drain()[-1].startswith("ERR"), command)
        self.assertEqual(self.sim.p, gui.DEFAULTS)


class FaultBannerTests(unittest.TestCase):
    def test_gui_reload_edit_and_send_watchdog_settings(self):
        class Value:
            def __init__(self, value):
                self.value = str(value)

            def get(self):
                return self.value

            def set(self, value):
                self.value = str(value)

        app = gui.CureGUI.__new__(gui.CureGUI)
        app.params = dict(gui.DEFAULTS)
        app.loaded_params = dict(gui.DEFAULTS)
        app.param_vars = {k: Value(v) for k, v in gui.DEFAULTS.items()}
        app.param_entries = {k: Mock() for k in gui.DEFAULTS}
        app.param_hint = Mock()
        app.start_temp = 25.0
        app.send = Mock()
        app._is_curing = lambda: False
        app._on_params({"heattimeout": "45.00", "heatrise": "1.50"})
        self.assertEqual(app.param_vars["heattimeout"].get(), "45")
        self.assertEqual(app.param_vars["heatrise"].get(), "1.5")
        self.assertEqual(app.loaded_params["heattimeout"], 45)
        app.param_vars["heattimeout"].set("60")
        app.param_vars["heatrise"].set("2.5")
        app.cmd_send_params()
        self.assertEqual([call.args[0] for call in app.send.call_args_list],
                         ["SET heattimeout 60", "SET heatrise 2.5"])
        app.send.reset_mock()
        app.param_vars["heattimeout"].set("0")
        with patch.object(gui.messagebox, "showerror") as error:
            app.cmd_send_params()
        error.assert_called_once()
        app.send.assert_not_called()

    def test_reconnect_recovers_reason_from_data_without_event(self):
        app = gui.CureGUI.__new__(gui.CureGUI)
        app.fault_reason = ""
        app.complete_up = None
        for name in ["hist_t", "hist_temp", "hist_set", "hist_heat"]:
            setattr(app, name, [])
        for name in ["temp_label", "set_label", "heater_label", "vac_label", "fault_label"]:
            setattr(app, name, Mock())
        app.status_vals = {k: Mock() for k in ["state", "t", "srem", "rem", "raw"]}
        app._on_data({"t": "300", "temp": "25", "set": "120", "state": "HOLD1",
                      "fault": "1", "heater": "0", "reason": "no_heat"})
        self.assertEqual(app.fault_reason, "no_heat")
        banner = app.fault_label.configure.call_args.kwargs["text"]
        self.assertIn("Insufficient temperature rise", banner)
        self.assertIn("heater off, clock frozen", banner)
        self.assertIn("reason", gui.CSV_FIELDS)
        app._on_data({"t": "300", "temp": "25", "state": "IDLE", "fault": "0", "reason": "none"})
        self.assertEqual(app.fault_reason, "")
        app.fault_label.grid_remove.assert_called_once()


if __name__ == "__main__":
    unittest.main()
