#!/usr/bin/env python3
"""
LAN Voice Lite (LAN TeamSpeak-ish)
- Ultra-low latency voice over UDP on the same Wi‑Fi/LAN
- Room discovery (no IP typing): host broadcasts; others double-click to join
- Host-relay topology (recommended for home routers)
- Ephemeral in-RAM text chat (ring buffer)
- Join/leave SFX (OS built-in sounds)
- Refresh audio devices + hot-swap input/output while in call

Deps:
  pip install sounddevice pyinstaller
(NumPy not required)

Ports (UDP):
  VOICE_PORT      50005
  CTRL_PORT       50006
  DISCOVERY_PORT  50007
"""

import json
import os
import platform
import queue
import socket
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, messagebox

import sounddevice as sd


VOICE_PORT = 50005
CTRL_PORT = 50006
DISCOVERY_PORT = 50007

# Audio defaults (keep these small for low latency)
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_FRAME_MS = 5         # try 5, if Wi‑Fi stutters use 10
DEFAULT_JITTER_FRAMES = 3    # try 3, if Wi‑Fi stutters use 5

# Room discovery timing
ANNOUNCE_EVERY_SEC = 1.0
ROOM_STALE_AFTER_SEC = 3.5

# Membership timing
CLIENT_PING_EVERY_SEC = 2.0
MEMBER_STALE_AFTER_SEC = 6.0

# Ephemeral chat ring buffer
CHAT_MAX_MESSAGES = 500
CHAT_MAX_CHARS = 500


def now() -> float:
    return time.time()


def get_local_ip_hint() -> str:
    """Considered 'best effort'. Doesn't make external requests (no internet dependency)."""
    try:
        # UDP connect trick: no packets sent, just chooses interface
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"


def safe_json_loads(data: bytes) -> Optional[dict]:
    try:
        return json.loads(data.decode("utf-8", errors="ignore"))
    except Exception:
        return None


def play_sfx(kind: str):
    """
    Lightweight, cross-platform-ish SFX.
    kind: 'join' | 'leave' | 'start' | 'stop' | 'msg'
    """
    sysname = platform.system()
    try:
        if sysname == "Windows":
            import winsound
            # Use built-in system beeps (no extra files)
            mapping = {
                "start": winsound.MB_ICONASTERISK,
                "join": winsound.MB_OK,
                "leave": winsound.MB_ICONEXCLAMATION,
                "stop": winsound.MB_ICONHAND,
                "msg": winsound.MB_OK,
            }
            winsound.MessageBeep(mapping.get(kind, winsound.MB_OK))
        elif sysname == "Darwin":
            # macOS built-in aiff sounds (tiny + always present)
            sound_map = {
                "start": "/System/Library/Sounds/Pop.aiff",
                "join": "/System/Library/Sounds/Glass.aiff",
                "leave": "/System/Library/Sounds/Funk.aiff",
                "stop": "/System/Library/Sounds/Basso.aiff",
                "msg": "/System/Library/Sounds/Ping.aiff",
            }
            p = sound_map.get(kind, "/System/Library/Sounds/Ping.aiff")
            if os.path.exists(p):
                subprocess.Popen(["afplay", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                # fallback
                subprocess.Popen(["afplay", "/System/Library/Sounds/Ping.aiff"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            # Linux: simple bell fallback
            print("\a", end="", flush=True)
    except Exception:
        pass


@dataclass
class RoomInfo:
    room_id: str
    room_name: str
    host_ip: str
    users: int
    max_users: int
    last_seen: float = field(default_factory=now)


@dataclass
class MemberInfo:
    name: str
    ip: str
    ctrl_addr: Tuple[str, int]
    last_seen: float = field(default_factory=now)


class VoiceEngine:
    """
    UDP voice IO with tiny jitter buffer.
    - In client mode: send mic frames to host, receive mixed/forwarded frames, play them.
    - In host mode: receive frames from clients, play them, and relay to other clients.
      Host also sends its own mic frames to all clients.
    """
    def __init__(self, ui_event_q: queue.Queue):
        self.ui_q = ui_event_q

        self.is_running = False
        self.is_host = False

        self.sample_rate = DEFAULT_SAMPLE_RATE
        self.frame_ms = DEFAULT_FRAME_MS
        self.jitter_frames = DEFAULT_JITTER_FRAMES

        self.in_dev = None
        self.out_dev = None

        self.voice_sock: Optional[socket.socket] = None
        self.rx_thread: Optional[threading.Thread] = None

        self.in_stream = None
        self.out_stream = None

        self.frame_samples = int(self.sample_rate * self.frame_ms / 1000)
        self.frame_bytes = self.frame_samples * 2  # mono int16
        self.silence = b"\x00" * self.frame_bytes

        self.jb = deque(maxlen=max(30, self.jitter_frames * 8))
        self.jb_lock = threading.Lock()

        # Client send target
        self.peer_ip: Optional[str] = None

        # Host members (ip-only for voice forwarding)
        self._members_ip: List[str] = []
        self._members_lock = threading.Lock()

        # Stats
        self.tx = 0
        self.rx = 0

    def configure_audio(self, sample_rate: int, frame_ms: int, jitter_frames: int):
        if self.is_running:
            raise RuntimeError("Stop engine before reconfiguring.")
        self.sample_rate = int(sample_rate)
        self.frame_ms = int(frame_ms)
        self.jitter_frames = int(jitter_frames)

        self.frame_samples = int(self.sample_rate * self.frame_ms / 1000)
        self.frame_bytes = self.frame_samples * 2
        self.silence = b"\x00" * self.frame_bytes

        self.jb = deque(maxlen=max(30, self.jitter_frames * 8))
        with self.jb_lock:
            self.jb.clear()
            for _ in range(self.jitter_frames):
                self.jb.append(self.silence)

    def set_members(self, member_ips: List[str]):
        with self._members_lock:
            self._members_ip = list(dict.fromkeys(member_ips))  # de-dupe, keep order

    def start(self, *, mode_host: bool, peer_ip: Optional[str], in_dev, out_dev):
        if self.is_running:
            return

        self.is_host = mode_host
        self.peer_ip = peer_ip
        self.in_dev = in_dev
        self.out_dev = out_dev

        # Pre-fill jitter buffer
        with self.jb_lock:
            self.jb.clear()
            for _ in range(self.jitter_frames):
                self.jb.append(self.silence)

        # UDP socket
        self.voice_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.voice_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1_000_000)
        self.voice_sock.bind(("", VOICE_PORT))
        self.voice_sock.settimeout(0.5)

        # PortAudio low-latency hint
        try:
            sd.default.latency = ("low", "low")
        except Exception:
            pass

        self.is_running = True

        def rx_loop():
            while self.is_running:
                try:
                    data, addr = self.voice_sock.recvfrom(self.frame_bytes * 2)
                    if not data:
                        continue
                    if len(data) >= self.frame_bytes:
                        chunk = data[:self.frame_bytes]
                        self.rx += 1

                        # Playback locally
                        with self.jb_lock:
                            self.jb.append(chunk)

                        # If host: forward to all known members except sender
                        if self.is_host:
                            sender_ip = addr[0]
                            with self._members_lock:
                                targets = [ip for ip in self._members_ip if ip != sender_ip]
                            for ip in targets:
                                try:
                                    self.voice_sock.sendto(chunk, (ip, VOICE_PORT))
                                except Exception:
                                    pass
                except socket.timeout:
                    continue
                except OSError:
                    break
                except Exception:
                    continue

        self.rx_thread = threading.Thread(target=rx_loop, daemon=True)
        self.rx_thread.start()

        def in_cb(indata, frames, time_info, status):
            if not self.is_running:
                return

            # Convert to bytes for cross-platform safety
            payload = bytes(indata)
            if len(payload) < self.frame_bytes:
                return

            self.tx += 1

            try:
                if self.is_host:
                    # send to all members
                    with self._members_lock:
                        targets = list(self._members_ip)
                    for ip in targets:
                        try:
                            self.voice_sock.sendto(payload[:self.frame_bytes], (ip, VOICE_PORT))
                        except Exception:
                            pass
                else:
                    # send to host
                    if self.peer_ip:
                        self.voice_sock.sendto(payload[:self.frame_bytes], (self.peer_ip, VOICE_PORT))
            except Exception:
                pass

        def out_cb(outdata, frames, time_info, status):
            if not self.is_running:
                outdata[:] = self.silence
                return
            with self.jb_lock:
                if self.jb:
                    outdata[:] = self.jb.popleft()
                else:
                    outdata[:] = self.silence

        self.in_stream = sd.RawInputStream(
            samplerate=self.sample_rate,
            blocksize=self.frame_samples,
            dtype="int16",
            channels=1,
            device=self.in_dev,
            callback=in_cb,
        )
        self.out_stream = sd.RawOutputStream(
            samplerate=self.sample_rate,
            blocksize=self.frame_samples,
            dtype="int16",
            channels=1,
            device=self.out_dev,
            callback=out_cb,
        )

        self.in_stream.start()
        self.out_stream.start()

    def stop(self):
        self.is_running = False

        try:
            if self.in_stream:
                self.in_stream.stop()
                self.in_stream.close()
        except Exception:
            pass
        try:
            if self.out_stream:
                self.out_stream.stop()
                self.out_stream.close()
        except Exception:
            pass

        self.in_stream = None
        self.out_stream = None

        try:
            if self.voice_sock:
                self.voice_sock.close()
        except Exception:
            pass
        self.voice_sock = None

    def restart_audio_devices(self, in_dev, out_dev):
        """Hot-swap input/output devices without dropping networking threads (voice socket stays)."""
        if not self.is_running:
            self.in_dev = in_dev
            self.out_dev = out_dev
            return

        # Stop streams only
        try:
            if self.in_stream:
                self.in_stream.stop()
                self.in_stream.close()
        except Exception:
            pass
        try:
            if self.out_stream:
                self.out_stream.stop()
                self.out_stream.close()
        except Exception:
            pass

        self.in_dev = in_dev
        self.out_dev = out_dev

        # Recreate streams (socket/rx thread remain)
        try:
            sd.default.latency = ("low", "low")
        except Exception:
            pass

        def in_cb(indata, frames, time_info, status):
            if not self.is_running:
                return
            payload = bytes(indata)
            if len(payload) < self.frame_bytes:
                return
            self.tx += 1
            try:
                if self.is_host:
                    with self._members_lock:
                        targets = list(self._members_ip)
                    for ip in targets:
                        try:
                            self.voice_sock.sendto(payload[:self.frame_bytes], (ip, VOICE_PORT))
                        except Exception:
                            pass
                else:
                    if self.peer_ip:
                        self.voice_sock.sendto(payload[:self.frame_bytes], (self.peer_ip, VOICE_PORT))
            except Exception:
                pass

        def out_cb(outdata, frames, time_info, status):
            if not self.is_running:
                outdata[:] = self.silence
                return
            with self.jb_lock:
                if self.jb:
                    outdata[:] = self.jb.popleft()
                else:
                    outdata[:] = self.silence

        self.in_stream = sd.RawInputStream(
            samplerate=self.sample_rate,
            blocksize=self.frame_samples,
            dtype="int16",
            channels=1,
            device=self.in_dev,
            callback=in_cb,
        )
        self.out_stream = sd.RawOutputStream(
            samplerate=self.sample_rate,
            blocksize=self.frame_samples,
            dtype="int16",
            channels=1,
            device=self.out_dev,
            callback=out_cb,
        )
        self.in_stream.start()
        self.out_stream.start()


class LanVoiceApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LAN Voice Lite")
        self.resizable(False, False)

        self.ui_q: queue.Queue = queue.Queue()
        self.voice = VoiceEngine(self.ui_q)

        self.is_host = False
        self.in_room = False
        self.room_id: Optional[str] = None
        self.room_name: str = "Room"
        self.host_ip: Optional[str] = None

        self.rooms: Dict[str, RoomInfo] = {}
        self.members: Dict[str, MemberInfo] = {}  # key = ip

        self.chat = deque(maxlen=CHAT_MAX_MESSAGES)
        self.chat_enabled_var = tk.BooleanVar(value=False)  # default OFF for max performance

        # Sockets / threads
        self.discovery_sock = None
        self.discovery_thread = None

        self.announce_thread = None
        self.ctrl_sock = None
        self.ctrl_thread = None

        self.client_ctrl_sock = None
        self.client_ctrl_thread = None
        self.client_ping_thread = None

        # UI state vars
        self.my_ip = tk.StringVar(value=get_local_ip_hint())
        self.room_name_var = tk.StringVar(value="LAN Room")
        self.status_var = tk.StringVar(value="Idle")
        self.stats_var = tk.StringVar(value="TX=0 RX=0")

        self.frame_ms_var = tk.IntVar(value=DEFAULT_FRAME_MS)
        self.jitter_var = tk.IntVar(value=DEFAULT_JITTER_FRAMES)

        self.inputs, self.outputs, def_in, def_out = self._query_devices()
        self.in_dev_var = tk.StringVar(value=self._find_device_label(self.inputs, def_in))
        self.out_dev_var = tk.StringVar(value=self._find_device_label(self.outputs, def_out))

        self._build_ui()
        self._start_discovery_listener()
        self._on_chat_toggle()

        self.after(100, self._ui_pump)
        self.after(300, self._ui_refresh_rooms)
        self.after(250, self._ui_refresh_stats)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 8}
        root = ttk.Frame(self)
        root.grid(row=0, column=0, **pad)

        # Header
        hdr = ttk.Frame(root)
        hdr.grid(row=0, column=0, sticky="w")

        ttk.Label(hdr, text="My IP:").grid(row=0, column=0, sticky="e")
        ttk.Label(hdr, textvariable=self.my_ip).grid(row=0, column=1, sticky="w", padx=(6, 16))
        ttk.Label(hdr, textvariable=self.status_var).grid(row=0, column=2, sticky="w")

        # Controls
        ctrl = ttk.LabelFrame(root, text="Voice")
        ctrl.grid(row=1, column=0, sticky="we")

        ttk.Label(ctrl, text="Room name").grid(row=0, column=0, sticky="e", padx=6, pady=4)
        ttk.Entry(ctrl, textvariable=self.room_name_var, width=24).grid(row=0, column=1, sticky="w", pady=4)

        ttk.Button(ctrl, text="START VOICE (Host)", command=self._host_start).grid(row=0, column=2, padx=6)
        ttk.Button(ctrl, text="STOP / LEAVE", command=self._stop_or_leave).grid(row=0, column=3, padx=6)

        ttk.Label(ctrl, text="Frame (ms)").grid(row=1, column=0, sticky="e", padx=6, pady=4)
        ttk.Combobox(ctrl, state="readonly", values=[5, 10], width=6, textvariable=self.frame_ms_var).grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(ctrl, text="Jitter (frames)").grid(row=1, column=2, sticky="e", padx=6, pady=4)
        ttk.Combobox(ctrl, state="readonly", values=[3, 5, 7], width=6, textvariable=self.jitter_var).grid(row=1, column=3, sticky="w", pady=4)

        # Devices
        dev = ttk.LabelFrame(root, text="Audio Devices (hot-swap)")
        dev.grid(row=2, column=0, sticky="we", pady=(8, 0))

        ttk.Label(dev, text="Input").grid(row=0, column=0, sticky="e", padx=6, pady=4)
        self.in_combo = ttk.Combobox(dev, state="readonly", width=48, textvariable=self.in_dev_var)
        self.in_combo["values"] = [lbl for _, lbl in self.inputs]
        self.in_combo.grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(dev, text="Output").grid(row=1, column=0, sticky="e", padx=6, pady=4)
        self.out_combo = ttk.Combobox(dev, state="readonly", width=48, textvariable=self.out_dev_var)
        self.out_combo["values"] = [lbl for _, lbl in self.outputs]
        self.out_combo.grid(row=1, column=1, sticky="w", pady=4)

        ttk.Button(dev, text="Apply device change", command=self._apply_device_change).grid(row=0, column=2, rowspan=2, padx=8)
        ttk.Button(dev, text="Refresh list", command=self._refresh_devices).grid(row=0, column=3, rowspan=2, padx=8)

        # Rooms list
        rooms = ttk.LabelFrame(root, text="Join a room (double-click)")
        rooms.grid(row=3, column=0, sticky="we", pady=(8, 0))

        self.rooms_tree = ttk.Treeview(rooms, columns=("name", "host", "users"), show="headings", height=6)
        self.rooms_tree.heading("name", text="Room")
        self.rooms_tree.heading("host", text="Host IP")
        self.rooms_tree.heading("users", text="Users")
        self.rooms_tree.column("name", width=180)
        self.rooms_tree.column("host", width=130)
        self.rooms_tree.column("users", width=70, anchor="center")
        self.rooms_tree.grid(row=0, column=0, padx=8, pady=8)
        self.rooms_tree.bind("<Double-1>", self._join_selected_room)

        ttk.Button(rooms, text="JOIN selected", command=self._join_selected_room).grid(row=0, column=1, padx=8)

        # Chat + members
        bottom = ttk.Frame(root)
        bottom.grid(row=4, column=0, sticky="we", pady=(8, 0))

        chatf = ttk.LabelFrame(bottom, text="Chat (optional, RAM)")
        chatf.grid(row=0, column=0, sticky="w")

        # Chat toggle (OFF by default to keep resource usage minimal)
        chat_toggle_row = ttk.Frame(chatf)
        chat_toggle_row.grid(row=0, column=0, columnspan=2, sticky="we", padx=8, pady=(8, 0))
        self.chat_toggle = ttk.Checkbutton(
            chat_toggle_row,
            text="Enable chat (RAM)",
            variable=self.chat_enabled_var,
            command=self._on_chat_toggle,
        )
        self.chat_toggle.grid(row=0, column=0, sticky="w")

        self.chat_text = tk.Text(chatf, width=58, height=10, state="disabled")
        self.chat_text.grid(row=1, column=0, columnspan=2, padx=8, pady=8)

        self.chat_entry = ttk.Entry(chatf, width=46)
        self.chat_entry.grid(row=2, column=0, padx=8, pady=(0, 8), sticky="w")
        self.chat_entry.bind("<Return>", lambda e: self._send_chat())

        ttk.Button(chatf, text="Send", command=self._send_chat).grid(row=2, column=1, padx=8, pady=(0, 8), sticky="e")

        memf = ttk.LabelFrame(bottom, text="Members")
        memf.grid(row=0, column=1, padx=(10, 0), sticky="n")

        self.members_list = tk.Listbox(memf, width=26, height=12)
        self.members_list.grid(row=0, column=0, padx=8, pady=8)

        ttk.Label(root, textvariable=self.stats_var).grid(row=5, column=0, sticky="w", pady=(8, 0))

    def _on_chat_toggle(self):
        enabled = bool(self.chat_enabled_var.get())
        # Clear chat history when turning OFF (RAM-friendly)
        if not enabled:
            self.chat.clear()
        # Update UI widgets state
        state = "normal" if enabled else "disabled"
        try:
            self.chat_entry.configure(state=state)
        except Exception:
            pass
        try:
            self.chat_text.configure(state="normal")
            self.chat_text.delete("1.0", tk.END)
            self.chat_text.configure(state="disabled")
        except Exception:
            pass
    # ---------- Devices ----------
    def _hostapi_indices_by_name(self, hostapis, needles: List[str]) -> List[int]:
        out = []
        for i, h in enumerate(hostapis):
            name = str(h.get("name", "")).lower()
            if any(n.lower() in name for n in needles):
                out.append(i)
        return out

    def _query_devices(self):
        devices = list(sd.query_devices())
        hostapis = list(sd.query_hostapis())

        # Prefer the most useful host API per OS to avoid noisy device lists (esp. Windows).
        sysname = platform.system()
        preferred_hostapis: List[int] = []
        if sysname == "Windows":
            preferred_hostapis = self._hostapi_indices_by_name(hostapis, ["WASAPI"])
        elif sysname == "Darwin":
            preferred_hostapis = self._hostapi_indices_by_name(hostapis, ["Core Audio"])
        elif sysname == "Linux":
            preferred_hostapis = self._hostapi_indices_by_name(hostapis, ["ALSA", "PipeWire", "Pulse", "JACK"])

        device_items = list(enumerate(devices))
        if preferred_hostapis:
            filtered = [(i, d) for i, d in device_items if d.get("hostapi") in preferred_hostapis]
            if filtered:
                device_items = filtered

        inputs, outputs = [], []
        for i, d in device_items:
            name = d.get("name", f"Device {i}")
            if d.get("max_input_channels", 0) > 0:
                inputs.append((i, f"{i}: {name}"))
            if d.get("max_output_channels", 0) > 0:
                outputs.append((i, f"{i}: {name}"))
        try:
            def_in, def_out = sd.default.device
        except Exception:
            def_in, def_out = (None, None)
        return inputs, outputs, def_in, def_out

    def _find_device_label(self, dev_list, index):
        for i, lbl in dev_list:
            if i == index:
                return lbl
        return dev_list[0][1] if dev_list else ""

    def _parse_device_index(self, label: str) -> Optional[int]:
        try:
            return int(label.split(":", 1)[0].strip())
        except Exception:
            return None

    def _refresh_devices(self):
        try:
            self.inputs, self.outputs, def_in, def_out = self._query_devices()
            self.in_combo["values"] = [lbl for _, lbl in self.inputs]
            self.out_combo["values"] = [lbl for _, lbl in self.outputs]
            # keep current if possible
            if self.in_dev_var.get() not in self.in_combo["values"] and self.inputs:
                self.in_dev_var.set(self.inputs[0][1])
            if self.out_dev_var.get() not in self.out_combo["values"] and self.outputs:
                self.out_dev_var.set(self.outputs[0][1])
        except Exception as e:
            messagebox.showerror("Device refresh failed", str(e))

    def _apply_device_change(self):
        in_dev = self._parse_device_index(self.in_dev_var.get())
        out_dev = self._parse_device_index(self.out_dev_var.get())
        if in_dev is None or out_dev is None:
            messagebox.showerror("Devices", "Select valid input/output devices.")
            return

        try:
            self.voice.restart_audio_devices(in_dev, out_dev)
            self._set_status("Devices updated.")
        except Exception as e:
            messagebox.showerror("Device change failed", str(e))

    # ---------- Discovery ----------
    def _start_discovery_listener(self):
        if self.discovery_thread:
            return

        self.discovery_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.discovery_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.discovery_sock.bind(("", DISCOVERY_PORT))
        except OSError:
            # If port is in use on macOS, fallback to reuseport
            try:
                self.discovery_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                self.discovery_sock.bind(("", DISCOVERY_PORT))
            except Exception:
                raise

        self.discovery_sock.settimeout(0.5)

        def listen():
            while True:
                try:
                    data, addr = self.discovery_sock.recvfrom(2048)
                    msg = safe_json_loads(data)
                    if not msg or msg.get("type") != "ANNOUNCE":
                        continue
                    room_id = msg.get("room_id", "")
                    if not room_id:
                        continue
                    info = RoomInfo(
                        room_id=room_id,
                        room_name=msg.get("room_name", "Room"),
                        host_ip=msg.get("host_ip", addr[0]),
                        users=int(msg.get("users", 1)),
                        max_users=int(msg.get("max_users", 8)),
                        last_seen=now(),
                    )
                    self.rooms[room_id] = info
                except socket.timeout:
                    continue
                except Exception:
                    continue

        self.discovery_thread = threading.Thread(target=listen, daemon=True)
        self.discovery_thread.start()

    def _start_announcer(self):
        def broadcast_loop(room_id: str, room_name: str):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(0.2)
            while self.is_host and self.in_room:
                try:
                    msg = {
                        "type": "ANNOUNCE",
                        "room_id": room_id,
                        "room_name": room_name,
                        "host_ip": get_local_ip_hint(),
                        "users": max(1, len(self.members) + 1),
                        "max_users": 8,
                        "ts": int(now()),
                    }
                    data = json.dumps(msg).encode("utf-8")
                    sock.sendto(data, ("255.255.255.255", DISCOVERY_PORT))
                except Exception:
                    pass
                time.sleep(ANNOUNCE_EVERY_SEC)
            try:
                sock.close()
            except Exception:
                pass

        self.announce_thread = threading.Thread(
            target=broadcast_loop, args=(self.room_id, self.room_name), daemon=True
        )
        self.announce_thread.start()

    # ---------- Control (host) ----------
    def _start_ctrl_server(self):
        self.ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ctrl_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1_000_000)
        self.ctrl_sock.bind(("", CTRL_PORT))
        self.ctrl_sock.settimeout(0.5)

        def ctrl_loop():
            while self.is_host and self.in_room:
                try:
                    data, addr = self.ctrl_sock.recvfrom(4096)
                    msg = safe_json_loads(data)
                    if not msg:
                        continue
                    mtype = msg.get("type")
                    if mtype == "JOIN":
                        name = str(msg.get("name", "User"))[:32]
                        ip = addr[0]
                        self.members[ip] = MemberInfo(name=name, ip=ip, ctrl_addr=addr, last_seen=now())
                        self._host_broadcast_members(event=f"{name} joined", sfx="join")
                        # ack
                        self._ctrl_send(addr, {"type": "JOIN_OK", "room_id": self.room_id, "room_name": self.room_name})
                    elif mtype == "PING":
                        ip = addr[0]
                        if ip in self.members:
                            self.members[ip].last_seen = now()
                    elif mtype == "LEAVE":
                        ip = addr[0]
                        mi = self.members.pop(ip, None)
                        if mi:
                            self._host_broadcast_members(event=f"{mi.name} left", sfx="leave")
                    elif mtype == "CHAT":
                        if not self.chat_enabled_var.get():
                            continue
                        ip = addr[0]
                        name = self.members.get(ip, MemberInfo("User", ip, addr)).name
                        text = str(msg.get("msg", ""))[:CHAT_MAX_CHARS]
                        self._ui_chat_append(name, text)
                        self._host_relay_ctrl({"type": "CHAT", "name": name, "msg": text, "ts": int(now())})
                    # prune stale
                    self._host_prune_members()
                except socket.timeout:
                    self._host_prune_members()
                    continue
                except Exception:
                    continue

        self.ctrl_thread = threading.Thread(target=ctrl_loop, daemon=True)
        self.ctrl_thread.start()

    def _host_prune_members(self):
        cutoff = now() - MEMBER_STALE_AFTER_SEC
        removed = []
        for ip, mi in list(self.members.items()):
            if mi.last_seen < cutoff:
                removed.append(mi)
                self.members.pop(ip, None)
        if removed:
            self._host_broadcast_members(event="Member timed out", sfx="leave")

    def _ctrl_send(self, addr: Tuple[str, int], msg: dict):
        try:
            self.ctrl_sock.sendto(json.dumps(msg).encode("utf-8"), addr)
        except Exception:
            pass

    def _host_relay_ctrl(self, msg: dict):
        data = json.dumps(msg).encode("utf-8")
        for ip, mi in list(self.members.items()):
            try:
                self.ctrl_sock.sendto(data, mi.ctrl_addr)
            except Exception:
                pass

    def _host_broadcast_members(self, event: Optional[str] = None, sfx: Optional[str] = None):
        # Update voice engine recipients
        member_ips = list(self.members.keys())
        self.voice.set_members(member_ips)

        # Update UI list + optional event
        self._ui_members_refresh()
        if event:
            self._ui_chat_system(event)
        if sfx:
            threading.Thread(target=play_sfx, args=(sfx,), daemon=True).start()

        # Send members list to all clients (for UI)
        payload = {
            "type": "MEMBERS",
            "members": [{"name": mi.name, "ip": mi.ip} for mi in self.members.values()],
            "ts": int(now()),
        }
        self._host_relay_ctrl(payload)

    # ---------- Control (client) ----------
    def _start_client_ctrl(self, host_ip: str):
        self.client_ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.client_ctrl_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1_000_000)
        self.client_ctrl_sock.bind(("", 0))  # ephemeral
        self.client_ctrl_sock.settimeout(0.5)

        def rx():
            while (not self.is_host) and self.in_room:
                try:
                    data, addr = self.client_ctrl_sock.recvfrom(4096)
                    msg = safe_json_loads(data)
                    if not msg:
                        continue
                    mtype = msg.get("type")
                    if mtype == "JOIN_OK":
                        self._ui_chat_system(f"Joined: {msg.get('room_name','Room')}")
                        threading.Thread(target=play_sfx, args=("join",), daemon=True).start()
                    elif mtype == "CHAT":
                        if not self.chat_enabled_var.get():
                            continue
                        self._ui_chat_append(str(msg.get("name", "User")), str(msg.get("msg", ""))[:CHAT_MAX_CHARS])
                        threading.Thread(target=play_sfx, args=("msg",), daemon=True).start()
                    elif mtype == "MEMBERS":
                        # update members list (client-side UI only)
                        self.members.clear()
                        for m in msg.get("members", []):
                            ip = str(m.get("ip", ""))
                            name = str(m.get("name", "User"))[:32]
                            if ip:
                                self.members[ip] = MemberInfo(name=name, ip=ip, ctrl_addr=(host_ip, CTRL_PORT))
                        self._ui_members_refresh()
                except socket.timeout:
                    continue
                except Exception:
                    continue

        self.client_ctrl_thread = threading.Thread(target=rx, daemon=True)
        self.client_ctrl_thread.start()

        def ping():
            while (not self.is_host) and self.in_room:
                try:
                    self.client_ctrl_sock.sendto(
                        json.dumps({"type": "PING", "room_id": self.room_id}).encode("utf-8"),
                        (host_ip, CTRL_PORT),
                    )
                except Exception:
                    pass
                time.sleep(CLIENT_PING_EVERY_SEC)

        self.client_ping_thread = threading.Thread(target=ping, daemon=True)
        self.client_ping_thread.start()

    # ---------- Actions ----------
    def _host_start(self):
        if self.in_room:
            messagebox.showinfo("LAN Voice", "Already in a room.")
            return

        self.is_host = True
        self.in_room = True
        self.room_id = uuid.uuid4().hex[:8]
        self.room_name = self.room_name_var.get().strip() or "Room"
        self.host_ip = get_local_ip_hint()

        # audio config
        frame_ms = int(self.frame_ms_var.get())
        jitter = int(self.jitter_var.get())
        self.voice.configure_audio(DEFAULT_SAMPLE_RATE, frame_ms, jitter)

        in_dev = self._parse_device_index(self.in_dev_var.get())
        out_dev = self._parse_device_index(self.out_dev_var.get())
        if in_dev is None or out_dev is None:
            messagebox.showerror("Devices", "Select valid input/output devices.")
            self.in_room = False
            self.is_host = False
            return

        # start voice engine in host mode (no peer)
        self.voice.set_members([])
        self.voice.start(mode_host=True, peer_ip=None, in_dev=in_dev, out_dev=out_dev)

        # control + announce
        self._start_ctrl_server()
        self._start_announcer()

        self._set_status(f"Hosting '{self.room_name}'")
        self._ui_chat_system(f"Hosting room: {self.room_name}")
        threading.Thread(target=play_sfx, args=("start",), daemon=True).start()

    def _join_selected_room(self, _evt=None):
        if self.in_room:
            return

        item = self.rooms_tree.focus()
        if not item:
            return
        room_id = self.rooms_tree.item(item, "values")[3] if False else None  # placeholder
        # We store room_id in iid
        room_id = item
        info = self.rooms.get(room_id)
        if not info:
            return

        self.is_host = False
        self.in_room = True
        self.room_id = info.room_id
        self.room_name = info.room_name
        self.host_ip = info.host_ip

        frame_ms = int(self.frame_ms_var.get())
        jitter = int(self.jitter_var.get())
        self.voice.configure_audio(DEFAULT_SAMPLE_RATE, frame_ms, jitter)

        in_dev = self._parse_device_index(self.in_dev_var.get())
        out_dev = self._parse_device_index(self.out_dev_var.get())
        if in_dev is None or out_dev is None:
            messagebox.showerror("Devices", "Select valid input/output devices.")
            self.in_room = False
            return

        # Start client control channel
        self._start_client_ctrl(self.host_ip)

        # Send JOIN
        myname = os.getenv("USER") or os.getenv("USERNAME") or "User"
        join_msg = {"type": "JOIN", "room_id": self.room_id, "name": myname[:32]}
        try:
            self.client_ctrl_sock.sendto(json.dumps(join_msg).encode("utf-8"), (self.host_ip, CTRL_PORT))
        except Exception:
            pass

        # Start voice engine client mode
        self.voice.start(mode_host=False, peer_ip=self.host_ip, in_dev=in_dev, out_dev=out_dev)

        self._set_status(f"Joined '{self.room_name}' @ {self.host_ip}")
        self._ui_chat_system(f"Joining room: {self.room_name} ({self.host_ip})")

    def _stop_or_leave(self):
        if not self.in_room:
            return

        # If client: send leave
        if not self.is_host and self.host_ip and self.client_ctrl_sock:
            try:
                self.client_ctrl_sock.sendto(
                    json.dumps({"type": "LEAVE", "room_id": self.room_id}).encode("utf-8"),
                    (self.host_ip, CTRL_PORT),
                )
            except Exception:
                pass

        # Stop voice
        try:
            self.voice.stop()
        except Exception:
            pass

        # Stop ctrl sockets
        try:
            if self.ctrl_sock:
                self.ctrl_sock.close()
        except Exception:
            pass
        try:
            if self.client_ctrl_sock:
                self.client_ctrl_sock.close()
        except Exception:
            pass

        self.ctrl_sock = None
        self.client_ctrl_sock = None

        self.members.clear()
        self._ui_members_refresh()

        self.in_room = False
        was_host = self.is_host
        self.is_host = False
        self.room_id = None
        self.host_ip = None

        self._set_status("Idle")
        self._ui_chat_system("Left room.")
        threading.Thread(target=play_sfx, args=("stop" if was_host else "leave",), daemon=True).start()

    def _send_chat(self):
        if (not self.in_room) or (not self.chat_enabled_var.get()):
            return

        txt = self.chat_entry.get().strip()
        if not txt:
            return
        txt = txt[:CHAT_MAX_CHARS]
        self.chat_entry.delete(0, tk.END)

        # Local append
        myname = os.getenv("USER") or os.getenv("USERNAME") or "Me"
        self._ui_chat_append(myname[:32], txt)

        # Send
        msg = {"type": "CHAT", "room_id": self.room_id, "msg": txt}

        try:
            if self.is_host and self.ctrl_sock:
                # host relays to everyone
                self._host_relay_ctrl({"type": "CHAT", "name": myname[:32], "msg": txt, "ts": int(now())})
            elif (not self.is_host) and self.client_ctrl_sock and self.host_ip:
                self.client_ctrl_sock.sendto(json.dumps(msg).encode("utf-8"), (self.host_ip, CTRL_PORT))
        except Exception:
            pass

    # ---------- UI helpers ----------
    def _set_status(self, s: str):
        self.status_var.set(s)

    def _ui_chat_system(self, msg: str):
        self._ui_chat_append("•", msg)

    def _ui_chat_append(self, name: str, msg: str):
        if not self.chat_enabled_var.get():
            return
        name = str(name)[:32]
        msg = str(msg)[:CHAT_MAX_CHARS]
        self.chat.append((int(now()), name, msg))
        self._render_chat()

    def _render_chat(self):
        self.chat_text.configure(state="normal")
        self.chat_text.delete("1.0", tk.END)
        for ts, name, msg in list(self.chat)[-CHAT_MAX_MESSAGES:]:
            t = time.strftime("%H:%M:%S", time.localtime(ts))
            self.chat_text.insert(tk.END, f"[{t}] {name}: {msg}\n")
        self.chat_text.configure(state="disabled")
        self.chat_text.see(tk.END)

    def _ui_members_refresh(self):
        self.members_list.delete(0, tk.END)
        if self.is_host and self.in_room:
            self.members_list.insert(tk.END, "(You) Host")
        for ip, mi in sorted(self.members.items(), key=lambda x: x[1].name.lower()):
            self.members_list.insert(tk.END, f"{mi.name}  ({ip})")

    def _ui_refresh_rooms(self):
        # prune stale rooms
        cutoff = now() - ROOM_STALE_AFTER_SEC
        for rid, info in list(self.rooms.items()):
            if info.last_seen < cutoff:
                self.rooms.pop(rid, None)

        # repopulate tree
        for iid in self.rooms_tree.get_children():
            self.rooms_tree.delete(iid)

        for rid, info in sorted(self.rooms.items(), key=lambda kv: kv[1].room_name.lower()):
            # Don't show our own hosted room if hosting (optional)
            users = f"{info.users}/{info.max_users}"
            self.rooms_tree.insert("", tk.END, iid=rid, values=(info.room_name, info.host_ip, users))

        self.after(300, self._ui_refresh_rooms)

    def _ui_refresh_stats(self):
        self.stats_var.set(f"TX={self.voice.tx}  RX={self.voice.rx}")
        self.after(250, self._ui_refresh_stats)

    def _ui_pump(self):
        # placeholder for future queued UI events
        try:
            while True:
                _ = self.ui_q.get_nowait()
        except queue.Empty:
            pass
        self.after(100, self._ui_pump)

    def _on_close(self):
        try:
            self._stop_or_leave()
        except Exception:
            pass
        self.destroy()


def main():
    app = LanVoiceApp()
    app.mainloop()


if __name__ == "__main__":
    main()
