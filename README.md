# Endgame Gear Mouse Battery Indicator

System tray application for **Endgame Gear wireless mice** (XM2w 4k, OP1w 4k, etc.) on Linux (KDE Plasma, GNOME, XFCE).

Shows battery level and lets you change onboard performance settings (CPI, polling rate, lift-off distance, motion sync, ripple control, power saving) — written directly to the mouse hardware.

Inspired by and built like [rog-mouse-battery-indicator](../rog-mouse-battery-indicator).

## Features

- Live battery percentage in the system tray (with charging & sleep states) plus real-time HID notification updates (`0x03`)
- Tooltip / status line: device model, battery %, active CPI, polling rate
- **Device & Firmware Info**: Mouse & Dongle firmware versions (`cmd 0x0E` / `0x0D`), automatic v1 vs v2 detection
- **Update status now** from the tray menu; left-click the icon to refresh
- **Refresh status interval** presets in the tray (default 10 min) and Preferences
- **CPI**: 1–4 onboard CPI levels, independent X/Y values, stage LED color indicators, and quick switch from the tray/dialog (on v2)
- **Polling rate**: 1000 Hz, 2000 Hz, 4000 Hz (plus 1000 Hz Power Saving & 125 Hz Office Mode on v2)
- **Lift-off distance (LOD)**: 1.0 / 2.0 mm on v1; 0.7–1.7 mm (0.1 mm steps) or 1.0 / 2.0 mm (Glass Mode) on v2
- **Sensor features**: Motion Sync, Ripple Control, Angle Snapping, Disable LED on Lift-Off, Motion Jitter Filter (v1), Sensor Glass Mode / Force Max Sensor FPS / Sensor Angle Tuning (v2)
- **Click & Switch settings**: Per-button Multiclick Filter and Kailh GX SPDT modes (*GX Speed Mode*, *GX Safe Mode*) for Left & Right buttons, Slamclick Filter
- **Button Mapping**: Left-handed Mode and per-button remapping (Mouse clicks, Scroll, Keyboard shortcuts + modifiers, CPI Loop, Fixed CPI, Media keys, Disable)
- **Power management & Maintenance**: Granular Standby and Deep sleep timers (1–120 min or disabled), Dongle Re-pairing, Factory Reset
- Lossless read-modify-write configuration stored **on the mouse hardware**
- Tray **Preferences** for icon colors, refresh interval, desktop low-battery notifications

## Supported Hardware

- **Endgame Gear XM2w 4k** (tested with High-Speed Dongle `3367:1970` and wired mode, PixArt PAW3395)
- **Endgame Gear XM2w 4k v2** (PixArt PAW3950, up to 30 000 CPI)
- **Endgame Gear OP1w 4k** (PixArt PAW3395)
- **Endgame Gear OP1w 4k v2** (PixArt PAW3950, up to 30 000 CPI)

> [!NOTE]
> Older wireless models like the **XM2we** use a completely different Compx CX52850 controller and are not supported by this tool. Wired-only models (OP1 8k, XM1r) do not have batteries and use different MCU firmware.

## Requirements

- Linux with system tray support
- Python 3.10+
- PySide6 (system package strongly recommended)
- Endgame Gear Dongle plugged in, mouse paired and powered on

## Installation

### Arch Linux / CachyOS / EndeavourOS (AUR)

You can build and install using the included `PKGBUILD`:

```bash
git clone https://github.com/zmudzin/egg-mouse-battery-indicator.git
cd egg-mouse-battery-indicator
makepkg -si
```

This automatically installs the package, sets up the udev rule in `/usr/lib/udev/rules.d/`, and provides the systemd user service.

### Other Distributions (pipx with system PySide6)

To avoid downloading a separate 250 MB PyPI Qt6 wheel that lacks native system dark themes and Wayland styling, install your distribution's system `pyside6` first:

- **Arch / CachyOS:** `sudo pacman -S pyside6`
- **Fedora:** `sudo dnf install python3-pyside6`
- **Ubuntu / Debian:** `sudo apt install python3-pyside6`

Then install using `--system-site-packages`:

```bash
pipx install --system-site-packages .
```

### Manual udev Rules

If installing via `pipx` or running from source, configure the udev rule to grant unprivileged user ACL access via `systemd-logind`:

```bash
sudo cp udev/70-endgame-gear.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=hidraw --action=change
```
*(Note: Named `70-` so it evaluates before `/usr/lib/udev/rules.d/73-seat-late.rules` which applies `uaccess` ACLs).*

## Autostart with systemd (Recommended)

To run the daemon in your user graphical session with automatic restart on failure:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/egg-mouse-battery-indicator.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now egg-mouse-battery-indicator.service
```

> [!TIP]
> If you previously had an autostart `.desktop` file in `~/.config/autostart/`, remove it (`rm ~/.config/autostart/egg-mouse-battery-indicator.desktop`) so it does not race with the systemd service.

### Desktop Environment Notes

- **GNOME Shell:** Tray icons are disabled by default in GNOME. Install the [AppIndicator and KStatusNotifierItem Support](https://extensions.gnome.org/extension/615/appindicator-support/) extension.
- **Hyprland / Sway:** On standalone Wayland compositors not launched via `uwsm` or systemd `graphical-session.target`, either start it in your compositor config (`exec-once = egg-mouse-battery-indicator`), or change the service unit to `WantedBy=default.target`.



