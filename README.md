# LAN Voice Lite

🎧 **Ultra low-latency voice chat over LAN** (same Wi-Fi / same house).  
If your PCs are a meter apart, your packets shouldn’t travel across the internet just to come back.

This app is intentionally minimal and performance-first. UI polish is welcome only if it does **not** add latency or heavy dependencies.

## ⚡ Features

- Near-zero latency voice over UDP on LAN
- Room discovery via broadcast (no manual IP typing)
- Host-relay topology (host forwards frames to all members)
- Join/leave SFX using OS built-in sounds
- Hot-swap audio devices without dropping the room
- Optional chat with a hard OFF toggle (zero overhead when off)
- Tiny buffers (5ms frames, small jitter buffer)

## ⬇️ Downloads

Ready-to-run builds are published on the [Releases](https://github.com/jab0c/LanVoice/releases) page (Windows `.exe` + macOS `.app` zipped).
Latest build: [Latest Release](https://github.com/jab0c/LanVoice/releases/latest)

## 🧠 How It Works

```
[Host]
  ├─ announces room via UDP broadcast (50007)
  ├─ receives voice frames (50005)
  └─ relays voice to all members

[Client]
  ├─ listens for rooms (50007)
  ├─ joins host over control UDP (50006)
  └─ streams voice to host (50005)
```

Ports:
- Voice: `50005/UDP`
- Control: `50006/UDP`
- Discovery: `50007/UDP`

Audio format:
- Mono `int16` frames
- Default frame size: `5ms`
- Default jitter buffer: `3` frames

## 📦 Requirements

- Python 3.9+
- Dependencies:
  1. `sounddevice`
  2. `pyinstaller` (only for building)

Install deps:

```bash
pip install sounddevice pyinstaller
```

## ▶️ Run From Source

```bash
python3 lan_voice_lite.py
```

## 🛠️ Build (One Command)

A single script adapts to the OS you run it on.

### macOS

```bash
python3 build.py --install
```

This will:
- Build the `.app` with PyInstaller
- Patch `Info.plist` with mic permission text
- Codesign the app (ad-hoc)
- Copy to `/Applications`
- Reset mic permissions (TCC) and open the app

### Windows

```bash
py build.py
```

This produces a single `.exe` in the `dist/` folder by default.

Notes:
- Cross-building is not supported by PyInstaller. Build Windows on Windows and macOS on macOS.
- The build script uses `onefile` on Windows and `onedir` on macOS by default.
- Default macOS bundle id is `com.lanvoice.app` (override with `--bundle-id` if you want).

## 🪟 Windows Audio Device List

PortAudio can expose a noisy list of drivers on Windows. This app filters to WASAPI devices first (closest to what users see in the Windows sound panel). If WASAPI is unavailable, it falls back to the full list.

## 🧰 Troubleshooting

### macOS mic permission does not show

1. Build/install with `python3 build.py --install`
2. Click `START VOICE` to trigger the mic prompt

If it still fails, try:

```bash
tccutil reset Microphone com.lanvoice.app
killall tccd
```

### Wi-Fi stutter

Try:
- Frame size: `10ms`
- Jitter: `5`

## 🤝 Contributing

PRs and issues are welcome. This is a performance-first project, so please keep dependencies light and avoid changes that add latency or heavy CPU cost.

Suggested workflow:
- Fork the repo
- Create a feature branch
- Keep changes small and focused
- Include notes on latency impact

Maintainer: [@jab0c](https://github.com/jab0c)

## 📄 License

See `LICENSE` for the custom terms.
