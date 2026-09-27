#!/usr/bin/env python3
"""
Cure oven monitor and controller (desktop GUI).

Reads the tagged serial lines printed by cure_controller.ino (DATA, PARAMS,
EVENT, OK, ERR, INFO), plots the cure live against the planned profile,
logs everything to CSV, and sends commands back to the board.

    pip install pyserial matplotlib
    python cure_gui.py            # choose the serial port in the window
    python cure_gui.py --sim      # no hardware: built-in simulator at 60x speed

The board talks on Serial1 (pins D0/D1), so pick the USB-to-TTL adapter's port.
Only one program can hold a port at a time, so close any other serial monitor.
CSV logs go to Documents/CureOvenLogs unless --log-dir is given.
"""

import argparse
import csv
import datetime
import math
import os
import queue
import random
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import matplotlib

matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # the simulator still works without pyserial
    serial = None

BAUD = 115200
SIM_PORT = "Simulator (no hardware)"

PARAMS = [
    ("ramp1", "Ramp 1 time", "min"),
    ("hold1temp", "Hold 1 temperature", "°C"),
    ("hold1", "Hold 1 time", "min"),
    ("ramp2", "Ramp 2 time", "min"),
    ("hold2temp", "Hold 2 temperature", "°C"),
    ("hold2", "Hold 2 time", "min"),
    ("hyst", "Hysteresis (±)", "°C"),
    ("maxtemp", "Over-temp cutoff", "°C"),
]
DEFAULTS = {
    "ramp1": 38.0, "hold1temp": 120.0, "hold1": 240.0, "ramp2": 24.0,
    "hold2temp": 180.0, "hold2": 120.0, "hyst": 2.0, "maxtemp": 200.0,
}
CURING_STATES = ("RAMP1", "HOLD1", "RAMP2", "HOLD2")
CSV_FIELDS = ["wallclock", "t", "state", "paused", "fault", "temp", "set", "err",
              "heater", "st", "srem", "rem", "vac", "adc", "R", "up", "note"]

# Palette
BG = "#E9EDF0"
INK = "#1C2630"
MUTED = "#5B6975"
HEAT = "#D9480F"
SETPT = "#1F6FB2"
PROFILE = "#9AA5AE"
GOOD = "#2B8A3E"
WARN = "#B7791F"
BAD = "#C92A2A"


# ---------------------------------------------------------------- helpers

def parse_line(line):
    """'DATA t=1.0 temp=22.1' -> ('DATA', {'t': '1.0', 'temp': '22.1'})"""
    parts = line.strip().split()
    if not parts:
        return None, {}
    fields = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            fields[k] = v
    return parts[0], fields


def to_float(s, default=float("nan")):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def fmt_duration(seconds):
    if seconds is None or math.isnan(seconds):
        return "--"
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def fmt_temp(t):
    return "--" if t is None or math.isnan(t) or math.isinf(t) else f"{t:.1f} °C"


def profile_points(p, start):
    """Planned cure profile as (minutes, temps) for plotting."""
    t1 = p["ramp1"]
    t2 = t1 + p["hold1"]
    t3 = t2 + p["ramp2"]
    t4 = t3 + p["hold2"]
    return [0, t1, t2, t3, t4], [start, p["hold1temp"], p["hold1temp"], p["hold2temp"], p["hold2temp"]]


# ---------------------------------------------------------------- links

class SerialLink:
    """Line-based connection to the Arduino."""

    def __init__(self, port, baud=BAUD):
        if serial is None:
            raise RuntimeError("pyserial is not installed (pip install pyserial)")
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = baud
        self.ser.timeout = 0.5
        # Asking for DTR low avoids the Uno's auto-reset on some systems.
        # Not every OS/driver honors it; the firmware resumes from EEPROM anyway.
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()

    def readline(self):
        raw = self.ser.readline()
        if not raw:
            return None
        return raw.decode("ascii", errors="replace").strip()

    def write_line(self, text):
        self.ser.write((text.strip() + "\n").encode("ascii", errors="replace"))

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


class SimLink:
    """Pretends to be the board: same commands, same output, 60x speed."""

    SPEED = 60.0          # simulated seconds per real second
    PRINT_EVERY = 0.5     # real seconds between DATA lines
    AMBIENT = 22.0
    HEAT_RATE = 8.0       # °C/min added while the heater is on
    LOSS = 0.025          # 1/min, Newtonian cooling toward ambient

    def __init__(self):
        self.lock = threading.Lock()
        self.out = queue.Queue()
        self.p = dict(DEFAULTS)
        self.start_temp = self.AMBIENT
        self.oven = self.AMBIENT
        self.temp = self.AMBIENT
        self.running = self.paused = self.fault = self.heater = False
        self.vac_announced = False
        self.phase = "IDLE"
        self.t = 0.0
        self.up = 0.0
        self.setpoint = 0.0
        self._stop = threading.Event()
        self._emit(f"INFO simulator boot ({self.SPEED:g}x speed)")
        self._emit_params()
        self._emit("INFO idle, send START to begin a cure")
        threading.Thread(target=self._run, daemon=True).start()

    # link interface
    def readline(self):
        try:
            return self.out.get(timeout=0.5)
        except queue.Empty:
            return None

    def write_line(self, text):
        with self.lock:
            self._handle(text.strip())

    def close(self):
        self._stop.set()

    # firmware emulation
    def _emit(self, s):
        self.out.put(s)

    def _total(self):
        return 60 * (self.p["ramp1"] + self.p["hold1"] + self.p["ramp2"] + self.p["hold2"])

    def _phase_at(self, t):
        p = self.p
        segs = [
            ("RAMP1", p["ramp1"] * 60, lambda f: self.start_temp + (p["hold1temp"] - self.start_temp) * f),
            ("HOLD1", p["hold1"] * 60, lambda f: p["hold1temp"]),
            ("RAMP2", p["ramp2"] * 60, lambda f: p["hold1temp"] + (p["hold2temp"] - p["hold1temp"]) * f),
            ("HOLD2", p["hold2"] * 60, lambda f: p["hold2temp"]),
        ]
        a = 0.0
        for name, dur, sp in segs:
            if t < a + dur:
                return name, sp((t - a) / dur), a, a + dur
            a += dur
        return "COMPLETE", 0.0, a, a

    def _emit_params(self):
        body = " ".join(f"{k}={self.p[k]:.2f}" for k, _, _ in PARAMS)
        self._emit(f"PARAMS {body} start={self.start_temp:.2f} total={self._total() / 60:.2f}")

    def _emit_data(self):
        name, _, a, b = self._phase_at(self.t)
        cur = self.running and self.phase != "COMPLETE"
        vac = self.phase == "COMPLETE" and self.temp <= 65
        self._emit(
            f"DATA t={self.t:.1f} state={self.phase} paused={int(self.paused)} fault={int(self.fault)} "
            f"temp={self.temp:.2f} set={self.setpoint:.2f} err={self.setpoint - self.temp:.2f} "
            f"heater={int(self.heater)} st={(self.t - a) if cur else 0:.0f} srem={(b - self.t) if cur else 0:.0f} "
            f"rem={(self._total() - self.t) if cur else 0:.0f} vac={int(vac)} adc=0.0 R=0.0 up={int(self.up)}"
        )

    def _start(self, at_s):
        if at_s <= 0 or not self.running:
            self.start_temp = self.temp
        self.running, self.paused, self.fault, self.vac_announced = True, False, False, False
        self.t = at_s
        self._emit(f"EVENT start t={self.t:.1f} start={self.start_temp:.2f}")
        self._emit_params()

    def _handle(self, line):
        parts = line.split()
        if not parts:
            return
        cmd = parts[0].upper()
        args = parts[1:]
        if cmd == "START":
            self.phase = "IDLE"
            self._start(0)
            self._emit("OK start")
        elif cmd == "GOTO":
            m = to_float(args[0] if args else None)
            if math.isnan(m) or m < 0 or m * 60 > self._total():
                self._emit("ERR usage: GOTO <minutes>, within the cycle length")
                return
            self._start(m * 60)
            self._emit(f"OK goto {m:.2f}")
        elif cmd in ("PAUSE", "RESUME"):
            if not self.running:
                self._emit("ERR not running")
                return
            self.paused = cmd == "PAUSE"
            self._emit(f"EVENT {cmd.lower()}")
            self._emit(f"OK {cmd.lower()}")
        elif cmd == "ABORT":
            self.running = self.paused = self.heater = False
            self.phase, self.setpoint = "IDLE", 0.0
            self._emit("EVENT abort")
            self._emit("OK abort")
        elif cmd == "CLEAR":
            self.fault = False
            self._emit("EVENT fault_cleared")
            self._emit("OK clear")
        elif cmd == "SET":
            if len(args) != 2 or args[0].lower() not in self.p or math.isnan(to_float(args[1])):
                self._emit("ERR usage: SET <name> <value>")
                return
            self.p[args[0].lower()] = to_float(args[1])
            self._emit(f"OK set {args[0].lower()}={to_float(args[1]):.2f}")
            self._emit_params()
        elif cmd == "DEFAULTS":
            self.p = dict(DEFAULTS)
            self._emit("OK defaults")
            self._emit_params()
        elif cmd == "PARAMS":
            self._emit_params()
        elif cmd == "STATUS":
            self._emit_data()
        elif cmd == "HELP":
            self._emit("INFO commands: START | GOTO <min> | PAUSE | RESUME | ABORT | CLEAR")
            self._emit("INFO           SET <name> <value> | DEFAULTS | PARAMS | STATUS | HELP")
        else:
            self._emit(f"ERR unknown command: {parts[0]}")

    def _step(self, h):
        self.up += h
        if self.running and not self.paused and not self.fault:
            self.t += h
        if self.running:
            name, sp, _, _ = self._phase_at(self.t)
            self.setpoint = sp
            if name != self.phase:
                self._emit(f"EVENT state from={self.phase} to={name} t={self.t:.1f}")
                self.phase = name
                if name == "COMPLETE":
                    self.running = self.paused = False
                    self._emit("EVENT complete")
        else:
            self.setpoint = 0.0
        self.temp = self.oven + random.gauss(0, 0.15)
        if self.temp > self.p["maxtemp"] and not self.fault:
            self.fault = True
            self._emit(f"EVENT fault reason=overtemp temp={self.temp:.2f}")
        if self.running and not self.fault and self.phase != "COMPLETE":
            if not self.heater and self.temp < self.setpoint - self.p["hyst"]:
                self.heater = True
            elif self.heater and self.temp > self.setpoint + self.p["hyst"]:
                self.heater = False
        else:
            self.heater = False
        if self.phase == "COMPLETE" and self.temp <= 65 and not self.vac_announced:
            self.vac_announced = True
            self._emit(f"EVENT vacuum_safe temp={self.temp:.2f}")
        dmin = h / 60.0
        self.oven += (self.HEAT_RATE if self.heater else 0.0) * dmin - self.LOSS * (self.oven - self.AMBIENT) * dmin

    def _run(self):
        last = time.monotonic()
        next_print = last
        carry = 0.0
        while not self._stop.is_set():
            time.sleep(0.05)
            now = time.monotonic()
            carry += (now - last) * self.SPEED
            last = now
            with self.lock:
                while carry >= 1.0:
                    self._step(1.0)
                    carry -= 1.0
                if now >= next_print:
                    next_print = now + self.PRINT_EVERY
                    self._emit_data()


# ---------------------------------------------------------------- GUI

class CureGUI:
    def __init__(self, root, log_dir, start_sim=False):
        self.root = root
        self.log_dir = log_dir
        self.link = None
        self.reader_stop = threading.Event()
        self.q = queue.Queue()

        self.params = dict(DEFAULTS)
        self.loaded_params = dict(DEFAULTS)
        self.start_temp = 22.0
        self.latest = {}
        self.fault_reason = ""
        self.hist_t, self.hist_temp, self.hist_set, self.hist_heat = [], [], [], []
        self.plot_dirty = True
        self.complete_up = None

        self.csv_file = None
        self.csv_writer = None
        self.cmd_history = []
        self.cmd_index = 0

        root.title("Cure oven")
        root.configure(bg=BG)
        root.minsize(1000, 700)
        height = max(700, min(880, root.winfo_screenheight() - 80))
        root.geometry(f"1280x{height}")
        self._style()
        self._build()
        self.refresh_ports()
        self._set_connected(False)

        if start_sim:
            self.port_var.set(SIM_PORT)
            self.toggle_connect()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll_queue)
        root.after(1000, self.refresh_plot)

    # ---------- layout

    def _style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure(".", background=BG, foreground=INK)
        s.configure("TLabelframe", background=BG)
        s.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=("TkDefaultFont", 10, "bold"))
        s.configure("TLabel", background=BG)
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("Big.TLabel", font=("TkDefaultFont", 24, "bold"))
        s.configure("Mid.TLabel", font=("TkDefaultFont", 13))
        s.configure("TCheckbutton", background=BG)

    def _build(self):
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Port").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(top, textvariable=self.port_var, width=38, state="readonly")
        self.port_box.pack(side="left", padx=6)
        ttk.Button(top, text="Refresh ports", command=self.refresh_ports).pack(side="left")
        self.connect_btn = ttk.Button(top, text="Connect", command=self.toggle_connect)
        self.connect_btn.pack(side="left", padx=6)
        self.log_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Log to CSV", variable=self.log_var).pack(side="left", padx=(16, 4))
        self.log_label = ttk.Label(top, text="", style="Muted.TLabel")
        self.log_label.pack(side="left")
        ttk.Button(top, text="Open logs", command=self.open_log_folder).pack(side="left", padx=(6, 0))
        self.conn_label = ttk.Label(top, text="Not connected", style="Muted.TLabel")
        self.conn_label.pack(side="right")

        body = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, width=340)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        right = ttk.PanedWindow(body, orient="vertical")
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        self._build_status(left)
        self._build_controls(left)
        self._build_params(left)
        self._build_plot(right)
        self._build_console(right)

    def _build_status(self, parent):
        f = ttk.LabelFrame(parent, text="Now", padding=(10, 6))
        f.pack(fill="x")
        self.temp_label = ttk.Label(f, text="--", style="Big.TLabel")
        self.temp_label.grid(row=0, column=0, columnspan=2, sticky="w")
        self.set_label = ttk.Label(f, text="Setpoint --", style="Mid.TLabel", foreground=SETPT)
        self.set_label.grid(row=1, column=0, columnspan=2, sticky="w")

        self.heater_label = tk.Label(f, text="Heater off", bg=PROFILE, fg="white",
                                     font=("TkDefaultFont", 11, "bold"), padx=10, pady=3)
        self.heater_label.grid(row=0, column=2, sticky="ne")

        rows = [("State", "state"), ("Cycle time", "t"), ("Phase remaining", "srem"),
                ("Total remaining", "rem"), ("Sensor", "raw")]
        self.status_vals = {}
        for i, (label, key) in enumerate(rows, start=2):
            ttk.Label(f, text=label, style="Muted.TLabel").grid(row=i, column=0, sticky="w")
            v = ttk.Label(f, text="--")
            v.grid(row=i, column=1, columnspan=2, sticky="w", padx=(10, 0))
            self.status_vals[key] = v

        self.vac_label = tk.Label(f, text="", bg=BG, font=("TkDefaultFont", 11, "bold"), anchor="w",
                                  justify="left", wraplength=300)
        self.vac_label.grid(row=8, column=0, columnspan=3, sticky="we", pady=(4, 0))
        self.fault_label = tk.Label(f, text="", bg=BG, fg=BAD, font=("TkDefaultFont", 11, "bold"),
                                    anchor="w", justify="left", wraplength=300)
        self.fault_label.grid(row=9, column=0, columnspan=3, sticky="we")
        self.fault_label.grid_remove()   # shown only while faulted
        f.columnconfigure(1, weight=1)

    def _build_controls(self, parent):
        f = ttk.LabelFrame(parent, text="Run", padding=(10, 6))
        f.pack(fill="x", pady=(6, 0))
        self.ctrl_buttons = []
        specs = [("Start cure", self.cmd_start), ("Pause", lambda: self.send("PAUSE")),
                 ("Resume", lambda: self.send("RESUME")), ("Abort", self.cmd_abort),
                 ("Clear fault", lambda: self.send("CLEAR")), ("Status", lambda: self.send("STATUS"))]
        for i, (text, cmd) in enumerate(specs):
            b = ttk.Button(f, text=text, command=cmd)
            b.grid(row=i // 3, column=i % 3, sticky="we", padx=2, pady=2)
            self.ctrl_buttons.append(b)
        g = ttk.Frame(f)
        g.grid(row=2, column=0, columnspan=3, sticky="we", pady=(6, 0))
        ttk.Label(g, text="Jump to minute").pack(side="left")
        self.goto_var = tk.StringVar()
        e = ttk.Entry(g, textvariable=self.goto_var, width=8)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda _e: self.cmd_goto())
        b = ttk.Button(g, text="Jump", command=self.cmd_goto)
        b.pack(side="left")
        self.ctrl_buttons.append(b)
        for c in range(3):
            f.columnconfigure(c, weight=1)

    def _build_params(self, parent):
        f = ttk.LabelFrame(parent, text="Cure parameters", padding=(10, 6))
        f.pack(fill="both", expand=True, pady=(6, 0))
        self.param_vars = {}
        self.param_entries = {}
        for i, (key, label, unit) in enumerate(PARAMS):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=1)
            var = tk.StringVar(value=f"{DEFAULTS[key]:g}")
            ent = tk.Entry(f, textvariable=var, width=8, justify="right", relief="solid", bd=1)
            ent.grid(row=i, column=1, sticky="e", padx=4)
            ttk.Label(f, text=unit, style="Muted.TLabel").grid(row=i, column=2, sticky="w")
            var.trace_add("write", lambda *_a: self._update_param_hints())
            self.param_vars[key] = var
            self.param_entries[key] = ent
        self.param_hint = ttk.Label(f, text="", style="Muted.TLabel", justify="left", wraplength=300)
        self.param_hint.grid(row=len(PARAMS) + 1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        bf = ttk.Frame(f)
        bf.grid(row=len(PARAMS), column=0, columnspan=3, sticky="we", pady=(6, 0))
        self.param_buttons = [
            ttk.Button(bf, text="Send", command=self.cmd_send_params),
            ttk.Button(bf, text="Reload", command=lambda: self.send("PARAMS")),
            ttk.Button(bf, text="Defaults", command=self.cmd_defaults),
        ]
        for i, b in enumerate(self.param_buttons):
            b.grid(row=0, column=i, sticky="we", padx=(0, 4))
            bf.columnconfigure(i, weight=1)
        f.columnconfigure(0, weight=1)
        self._update_param_hints()

    def _build_plot(self, paned):
        frame = ttk.Frame(paned)
        self.fig = Figure(figsize=(7, 4.6), dpi=100, facecolor=BG)
        gs = self.fig.add_gridspec(2, 1, height_ratios=[5, 1], hspace=0.08)
        self.ax = self.fig.add_subplot(gs[0])
        self.ax_h = self.fig.add_subplot(gs[1], sharex=self.ax)
        for a in (self.ax, self.ax_h):
            a.set_facecolor("white")
            a.grid(True, color="#E3E7EA", linewidth=0.8)
            for side in ("top", "right"):
                a.spines[side].set_visible(False)
        self.ax.set_ylabel("Temperature (°C)")
        self.ax_h.set_xlabel("Cycle time (min)")
        self.ax_h.set_yticks([0, 1], ["off", "on"])
        self.ax_h.set_ylim(-0.2, 1.3)
        self.ax.tick_params(labelbottom=False)
        (self.l_profile,) = self.ax.plot([], [], color=PROFILE, linestyle="--", linewidth=1.5, label="Planned")
        (self.l_set,) = self.ax.plot([], [], color=SETPT, linewidth=1.2, label="Setpoint")
        (self.l_temp,) = self.ax.plot([], [], color=HEAT, linewidth=1.8, label="Measured")
        self.v_now = self.ax.axvline(0, color=INK, linewidth=0.8, alpha=0.4)
        self.ax.axhline(65, color=GOOD, linewidth=0.8, alpha=0.5, linestyle=":")
        self.ax.legend(loc="upper left", frameon=False)
        (self.l_heat,) = self.ax_h.plot([], [], color=HEAT, linewidth=1.2, drawstyle="steps-post")
        self.fig.subplots_adjust(left=0.08, right=0.98, top=0.97, bottom=0.12)
        self.canvas = FigureCanvasTkAgg(self.fig, master=frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        paned.add(frame, weight=3)

    def _build_console(self, paned):
        frame = ttk.Frame(paned)
        head = ttk.Frame(frame)
        head.pack(fill="x", pady=(6, 2))
        ttk.Label(head, text="Serial", style="Muted.TLabel").pack(side="left")
        self.show_data_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(head, text="Show DATA lines", variable=self.show_data_var).pack(side="left", padx=10)
        ttk.Button(head, text="Clear", command=lambda: self.console.delete("1.0", "end")).pack(side="right")
        self.console = scrolledtext.ScrolledText(frame, height=9, font=("TkFixedFont", 10),
                                                 bg="white", fg=INK, relief="flat")
        self.console.pack(fill="both", expand=True)
        for tag, color in (("ERR", BAD), ("EVENT", SETPT), ("OK", GOOD), ("TX", MUTED), ("DATA", PROFILE)):
            self.console.tag_configure(tag, foreground=color)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(4, 0))
        self.cmd_var = tk.StringVar()
        self.cmd_entry = ttk.Entry(row, textvariable=self.cmd_var)
        self.cmd_entry.pack(side="left", fill="x", expand=True)
        self.cmd_entry.bind("<Return>", lambda _e: self.cmd_send_console())
        self.cmd_entry.bind("<Up>", lambda _e: self._history(-1))
        self.cmd_entry.bind("<Down>", lambda _e: self._history(1))
        self.send_btn = ttk.Button(row, text="Send", command=self.cmd_send_console)
        self.send_btn.pack(side="left", padx=(6, 0))
        paned.add(frame, weight=1)

    # ---------- connection

    def refresh_ports(self):
        self.port_map = {}
        if serial is not None:
            for p in serial.tools.list_ports.comports():
                label = f"{p.device}  ({p.description})" if p.description else p.device
                self.port_map[label] = p.device
        self.port_map[SIM_PORT] = SIM_PORT
        values = list(self.port_map)
        self.port_box["values"] = values
        if self.port_var.get() not in self.port_map:
            arduino = [v for v in values if "arduino" in v.lower() or "ch340" in v.lower() or "usb" in v.lower()]
            self.port_var.set(arduino[0] if arduino else values[0])

    def toggle_connect(self):
        if self.link:
            self.disconnect()
            return
        choice = self.port_var.get()
        device = self.port_map.get(choice)
        if not device:
            return
        try:
            self.link = SimLink() if device == SIM_PORT else SerialLink(device)
        except Exception as e:
            self.link = None
            messagebox.showerror("Could not open port",
                                 f"{e}\n\nIf the Arduino IDE Serial Monitor is open, close it and try again.")
            return
        self.reader_stop.clear()
        threading.Thread(target=self._reader, args=(self.link,), daemon=True).start()
        self._open_log(choice)
        self._set_connected(True, choice)
        self.root.after(1500, lambda: self.send("PARAMS", echo=False))

    def disconnect(self, reason=None):
        self.reader_stop.set()
        if self.link:
            self.link.close()
        self.link = None
        self._close_log()
        self._set_connected(False)
        if reason:
            self.log_console(f"-- disconnected: {reason}", "ERR")

    def _reader(self, link):
        while not self.reader_stop.is_set():
            try:
                line = link.readline()
            except Exception as e:
                self.q.put(("__error__", str(e)))
                return
            if line:
                self.q.put(("line", line))

    def _set_connected(self, on, name=""):
        self.connect_btn.configure(text="Disconnect" if on else "Connect")
        self.port_box.configure(state="disabled" if on else "readonly")
        short = "Simulator" if name == SIM_PORT else name.split("  (")[0]
        self.conn_label.configure(text=f"Connected: {short}" if on else "Not connected",
                                  foreground=GOOD if on else MUTED)
        state = "normal" if on else "disabled"
        for b in self.ctrl_buttons + self.param_buttons + [self.send_btn]:
            b.configure(state=state)

    # ---------- logging

    def _open_log(self, port_label):
        if not self.log_var.get():
            self.log_label.configure(text="")
            return
        os.makedirs(self.log_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = "_sim" if port_label == SIM_PORT else ""
        path = os.path.join(self.log_dir, f"cure_{stamp}{suffix}.csv")
        self.csv_file = open(path, "w", newline="")
        self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=CSV_FIELDS, extrasaction="ignore")
        self.csv_writer.writeheader()
        self.log_label.configure(text=os.path.basename(path))

    def open_log_folder(self):
        path = os.path.abspath(self.log_dir)
        try:
            os.makedirs(path, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa: S606 (Windows only)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            messagebox.showinfo("Log folder", path)

    def _close_log(self):
        if self.csv_file:
            self.csv_file.close()
        self.csv_file = self.csv_writer = None

    def _log_csv(self, fields=None, note=""):
        if not self.csv_writer:
            return
        row = dict(fields or {})
        row["wallclock"] = datetime.datetime.now().isoformat(timespec="seconds")
        row["note"] = note
        self.csv_writer.writerow(row)
        self.csv_file.flush()

    # ---------- incoming

    def poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "__error__":
                    self.disconnect(payload)
                else:
                    self.handle_line(payload)
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)

    def handle_line(self, line):
        tag, f = parse_line(line)
        if tag == "DATA":
            self._on_data(f)
            self._log_csv(f)
            if self.show_data_var.get():
                self.log_console(line, "DATA")
            return

        is_fault = tag == "EVENT" and line.split()[1:2] == ["fault"]
        self.log_console(line, "ERR" if is_fault else (tag if tag in ("ERR", "EVENT", "OK") else None))
        self._log_csv(note=line)

        if tag == "PARAMS":
            self._on_params(f)
        elif tag == "EVENT":
            parts = line.split()
            name = parts[1] if len(parts) > 1 else ""
            if name == "start" and to_float(f.get("t"), 0) == 0:
                self._clear_history()
            elif name == "fault":
                self.fault_reason = f.get("reason", "unknown")
            elif name == "fault_cleared":
                self.fault_reason = ""
            elif name == "complete":
                self.root.bell()
            elif name == "vacuum_safe":
                self.root.bell()

    def _on_data(self, f):
        self.latest = f
        t_min = to_float(f.get("t")) / 60.0
        temp = to_float(f.get("temp"))
        setp = to_float(f.get("set"))
        heater = f.get("heater") == "1"
        state = f.get("state", "?")
        paused = f.get("paused") == "1"
        fault = f.get("fault") == "1"

        # Plot while curing; after COMPLETE the cycle clock stops, so keep the
        # cool-down on the chart by advancing x with the board's uptime.
        if state in CURING_STATES:
            self.complete_up = None
            x = t_min
        elif state == "COMPLETE" and self.hist_t:
            up = to_float(f.get("up"), 0)
            if self.complete_up is None:
                self.complete_up = up
            x = t_min + (up - self.complete_up) / 60.0
        else:
            x = None
        if x is not None:
            self.hist_t.append(x)
            self.hist_temp.append(temp if not math.isinf(temp) else float("nan"))
            self.hist_set.append(setp if state in CURING_STATES else float("nan"))
            self.hist_heat.append(1 if heater else 0)
            self.plot_dirty = True

        self.temp_label.configure(text=fmt_temp(temp), foreground=BAD if math.isnan(temp) else INK)
        self.set_label.configure(text=f"Setpoint {setp:.1f} °C" if state in CURING_STATES else "Setpoint --")
        self.heater_label.configure(text="Heater on" if heater else "Heater off", bg=HEAT if heater else PROFILE)

        shown = state + (" (paused)" if paused else "") + (" (FAULT)" if fault else "")
        self.status_vals["state"].configure(text=shown, foreground=BAD if fault else (WARN if paused else INK))
        self.status_vals["t"].configure(text=fmt_duration(to_float(f.get("t"))))
        curing = state in CURING_STATES
        self.status_vals["srem"].configure(text=fmt_duration(to_float(f.get("srem"))) if curing else "--")
        self.status_vals["rem"].configure(text=fmt_duration(to_float(f.get("rem"))) if curing else "--")
        self.status_vals["raw"].configure(text=f"ADC {f.get('adc', '--')}   R {f.get('R', '--')} Ω")

        if state == "COMPLETE" and f.get("vac") == "1":
            self.vac_label.configure(text="Part at or below 65 °C: safe to release vacuum", fg=GOOD)
        elif curing or state == "COMPLETE":
            self.vac_label.configure(text="Keep vacuum on", fg=WARN)
        else:
            self.vac_label.configure(text="")

        if fault:
            reason = self.fault_reason or "unknown"
            hint = {"sensor": "Check thermistor wiring.",
                    "overtemp": "Above over-temp cutoff."}.get(reason, "")
            self.fault_label.configure(text=f"FAULT ({reason}): heater off, clock frozen. {hint} "
                                            "Fix it, then Clear fault.")
            self.fault_label.grid()
        else:
            self.fault_label.configure(text="")
            self.fault_label.grid_remove()

    def _on_params(self, f):
        for key, _, _ in PARAMS:
            if key in f:
                new = to_float(f[key])
                var = self.param_vars[key]
                # Only overwrite fields the user hasn't edited since the last load.
                if to_float(var.get()) == self.loaded_params.get(key) or math.isnan(to_float(var.get())):
                    var.set(f"{new:g}")
                self.loaded_params[key] = new
                self.params[key] = new
        if "start" in f:
            self.start_temp = to_float(f["start"], self.start_temp)
        self._update_param_hints()
        self.plot_dirty = True

    def _clear_history(self):
        self.hist_t, self.hist_temp, self.hist_set, self.hist_heat = [], [], [], []
        self.plot_dirty = True

    # ---------- outgoing

    def send(self, text, echo=True):
        if not self.link:
            return
        try:
            self.link.write_line(text)
        except Exception as e:
            self.disconnect(str(e))
            return
        if echo:
            self.log_console(f"> {text}", "TX")
        self._log_csv(note=f"> {text}")

    def _is_curing(self):
        return self.latest.get("state") in CURING_STATES

    def cmd_start(self):
        if self._is_curing() and not messagebox.askyesno(
                "Restart cure?", "A cure is in progress. Restart from minute 0?"):
            return
        if self._unsent_params() and messagebox.askyesno(
                "Unsent changes", "You have parameter changes that haven't been sent. Send them first?"):
            self.cmd_send_params(confirm=False)
        self.send("START")

    def cmd_abort(self):
        if messagebox.askyesno("Abort cure?", "Turn the heater off and end the cure?"):
            self.send("ABORT")

    def cmd_goto(self):
        m = to_float(self.goto_var.get())
        if math.isnan(m) or m < 0:
            messagebox.showerror("Jump to minute", "Enter the number of minutes into the cycle, e.g. 45.")
            return
        self.send(f"GOTO {m:g}")

    def cmd_defaults(self):
        if self._is_curing() and not messagebox.askyesno(
                "Restore defaults?", "A cure is in progress. Restoring defaults changes it immediately. Continue?"):
            return
        for key, _, _ in PARAMS:
            self.param_vars[key].set(f"{DEFAULTS[key]:g}")
        self.send("DEFAULTS")

    def _unsent_params(self):
        out = {}
        for key, _, _ in PARAMS:
            v = to_float(self.param_vars[key].get())
            if not math.isnan(v) and abs(v - self.loaded_params.get(key, float("nan"))) > 1e-6:
                out[key] = v
            elif math.isnan(v):
                out[key] = v
        return out

    def cmd_send_params(self, confirm=True):
        changes = self._unsent_params()
        bad = [k for k, v in changes.items() if math.isnan(v)]
        if bad:
            messagebox.showerror("Invalid value", "Check these fields: " + ", ".join(bad))
            return
        if not changes:
            self.log_console("-- no parameter changes to send", "TX")
            return
        if confirm and self._is_curing() and not messagebox.askyesno(
                "Change running cure?", "A cure is in progress. These changes take effect immediately. Continue?"):
            return
        for key, v in changes.items():
            self.send(f"SET {key} {v:g}")

    def cmd_send_console(self):
        text = self.cmd_var.get().strip()
        if not text:
            return
        self.cmd_history.append(text)
        self.cmd_index = len(self.cmd_history)
        self.cmd_var.set("")
        self.send(text)

    def _history(self, step):
        if not self.cmd_history:
            return "break"
        self.cmd_index = max(0, min(len(self.cmd_history), self.cmd_index + step))
        self.cmd_var.set(self.cmd_history[self.cmd_index] if self.cmd_index < len(self.cmd_history) else "")
        self.cmd_entry.icursor("end")
        return "break"

    # ---------- display

    def log_console(self, text, tag=None):
        self.console.insert("end", text + "\n", (tag,) if tag else ())
        lines = int(self.console.index("end-1c").split(".")[0])
        if lines > 3000:
            self.console.delete("1.0", f"{lines - 3000}.0")
        self.console.see("end")

    def _update_param_hints(self):
        vals = {k: to_float(v.get()) for k, v in self.param_vars.items()}
        for key, ent in self.param_entries.items():
            v = vals[key]
            if math.isnan(v):
                ent.configure(bg="#FBE3E3")
            elif abs(v - self.loaded_params.get(key, v)) > 1e-6:
                ent.configure(bg="#FFF3C4")   # edited, not yet sent
            else:
                ent.configure(bg="white")
        if any(math.isnan(v) for v in vals.values()):
            self.param_hint.configure(text="Enter numbers in every field.")
            return
        r1 = (vals["hold1temp"] - self.start_temp) / vals["ramp1"] if vals["ramp1"] > 0 else float("inf")
        r2 = (vals["hold2temp"] - vals["hold1temp"]) / vals["ramp2"] if vals["ramp2"] > 0 else float("inf")
        total = vals["ramp1"] + vals["hold1"] + vals["ramp2"] + vals["hold2"]
        warn = ""
        if vals["maxtemp"] <= vals["hold2temp"] + vals["hyst"]:
            warn = "\nCutoff is too close to hold 2 temp!"
        self.param_hint.configure(
            text=f"Ramps {r1:.2f} / {r2:.2f} °C/min, total {fmt_duration(total * 60)}\n"
                 f"Yellow = edited, not sent yet{warn}")
        self.plot_dirty = True

    def refresh_plot(self):
        if self.plot_dirty:
            self.plot_dirty = False
            px, py = profile_points(self.params, self.start_temp)
            self.l_profile.set_data(px, py)
            self.l_temp.set_data(self.hist_t, self.hist_temp)
            self.l_set.set_data(self.hist_t, self.hist_set)
            self.l_heat.set_data(self.hist_t, self.hist_heat)
            now = self.hist_t[-1] if self.hist_t else 0
            self.v_now.set_xdata([now, now])
            xmax = max(px[-1], now) * 1.03 + 1
            self.ax.set_xlim(0, xmax)
            finite = [v for v in self.hist_temp if not math.isnan(v)]
            ymax = max([max(py)] + finite) + 15
            ymin = min([min(py)] + finite) - 10
            self.ax.set_ylim(min(0, ymin), ymax)
            self.canvas.draw_idle()
        self.root.after(1000, self.refresh_plot)

    def on_close(self):
        self.disconnect()
        self.root.destroy()


def default_log_dir():
    """Documents/CureOvenLogs, which works no matter where the app is launched from."""
    home = os.path.expanduser("~")
    docs = os.path.join(home, "Documents")
    return os.path.join(docs if os.path.isdir(docs) else home, "CureOvenLogs")


def main():
    ap = argparse.ArgumentParser(description="Cure oven monitor and controller")
    ap.add_argument("--sim", action="store_true", help="start connected to the built-in simulator")
    ap.add_argument("--log-dir", default=default_log_dir(),
                    help="folder for CSV logs (default: Documents/CureOvenLogs)")
    # parse_known_args: macOS can pass extra arguments to apps started from Finder
    args, _unknown = ap.parse_known_args()
    root = tk.Tk()
    CureGUI(root, log_dir=args.log_dir, start_sim=args.sim)
    root.mainloop()


if __name__ == "__main__":
    main()
