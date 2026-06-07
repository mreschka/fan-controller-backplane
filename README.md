# GPU Fan Controller for Nvidia Tesla P100

An active cooling solution for Nvidia Tesla P100 PCIe GPUs, which have no built-in active cooling. An Arduino Nano handles PWM fan control and tachometer measurement in hardware; a Python systemd daemon on the Linux host reads GPU temperatures via `nvidia-smi` and regulates fan speed using a PID controller.

## Architecture

```
Linux Host
┌──────────────────────────────────────────────────────┐
│  gpu_fan_control_advanced.py  (systemd service)      │
│                                                      │
│  nvidia-smi ──> GPU temp ──> PID controller ──> PWM% │
│                                                      │
│  RPM feedback <── Debounce alarm check               │
└──────────────────────┬──────────────────────┬────────┘
                       │ USB Serial (9600)    │
                ┌──────▼───────┐              │
                │ Arduino Nano │              │
                │              │  PWM ────> [Fan 1-3]
                │  Pin 9: PWM  │  GND ────> [Fan 1-3]
                │  Pin 2: RPM  │  12V ────> (directly from PSU)
                │  Pin 3: RPM  │
                │  Pin 4: RPM  │  Tach <── [Fan 1-3]
                └──────────────┘
```

## Features

- **PID controller** with anti-windup and slew-rate limiter (smooth speed changes, no sudden jumps)
- **Hardware failsafe**: Arduino automatically falls back to 40% if no host command is received for 5 s — independent of the host
- **Tachometer monitoring** via pin-change interrupts on up to 5 fans simultaneously
- **Debounce alarms**: Fan failure alert only after 5 s of continuous low RPM (prevents false alarms during spin-up)
- **Multi-zone support**: Multiple Arduinos/zones with separate GPU groups and PID instances
- **systemd integration**: Autostart, automatic restart, logging via journald and syslog

## File Overview

| File | Description |
|---|---|
| `src/main.cpp` | Arduino sketch (PlatformIO) |
| `platformio.ini` | PlatformIO project configuration |
| `gpu_fan_control_advanced.py` | Python daemon (PID controller, monitoring) |
| `gpu-fan-control.toml` | Configuration file (installed to `/etc/`) |
| `gpu-fan-control.service` | systemd unit file |
| `install.sh` | Automated installer |
| `README-ger.md` | German version of this document |

## Hardware

**Tested setup:**
- 4× Nvidia Tesla P100-PCIE-16GB
- 3× Arctic P9 Max (92 mm, 500–5000 RPM, silent mode below 5% PWM)
- Arduino Nano v3 (ATmega328P, old bootloader)

**Important:** Never route 12 V fan power through the Arduino — this will destroy the board. Fans receive 12 V directly from the PSU; **GND from PSU and Arduino must be connected**.

### Arduino Pinout

| Arduino Pin | Function | Fan Wire |
|---|---|---|
| Pin 9 | PWM output (shared by all fans) | Blue (PWM) |
| Pin 2 | Tachometer input fan 0 | Green/Yellow |
| Pin 3 | Tachometer input fan 1 | Green/Yellow |
| Pin 4 | Tachometer input fan 2 | Green/Yellow |
| Pin 5 | Tachometer input fan 3 | Green/Yellow |
| Pin 6 | Tachometer input fan 4 | Green/Yellow |
| GND | Ground | Black |

Tach pins are pulled up via `INPUT_PULLUP`. The `PinChangeInterrupt` library enables interrupts on all digital pins (not just 2 and 3).

## Serial Protocol (Host ↔ Arduino)

**Host → Arduino** (every ~2 s):
```
30\n        # Integer 1–100: desired fan speed in %
```

**Arduino → Host** (reply to each command):
```
RPM:1200,1230,1200,0,0\n    # 5 values (tach index 0–4), unused pins = 0
```

**Arduino → Host** (on timeout after 5 s without command):
```
TIMEOUT:No command received, failsafe active.\n
```

## Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/mreschka/fan-controller-backplane/main/install.sh | sudo bash
```

## Manual Setup

### Prerequisites

```bash
sudo apt install python3 python3-pip mailutils
pip3 install pyserial
```

The user running the service must be in the `dialout` group:
```bash
sudo usermod -aG dialout $USER
# log out and back in afterwards
```

### Flash the Arduino (PlatformIO)

```bash
cd fan-controller-backplane
pio run --target upload
```

Board: `nanoatmega328` (old bootloader, 57600 baud upload), library: `NicoHood/PinChangeInterrupt @ ^1.2.9`

### Install Configuration File

All configuration lives in `/etc/gpu-fan-control.toml`:

```bash
sudo cp gpu-fan-control.toml /etc/gpu-fan-control.toml
```

Adjust to your setup:

```toml
[global]
update_interval        = 2.0    # seconds between control cycles
min_rpm_warn_threshold = 300    # RPM threshold for fan failure alarm
rpm_fail_debounce      = 5.0    # seconds of low RPM before alarm fires
fallback_speed         = 40     # % speed when nvidia-smi is unavailable
admin_email            = "root"
error_mail_interval    = 86400  # seconds between identical error mails (86400 = 24h)

[pid_defaults]
setpoint  = 75    # target temperature in °C
kp        = 2.5
ki        = 0.08
kd        = 4.0
min_speed = 5     # % minimum speed
max_speed = 60    # % maximum speed
max_slew  = 5     # % max change per control cycle

[[controllers]]
name          = "Main Zone (4x P100)"
port          = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_..."
gpus          = [0, 1, 2, 3]
active_tachos = [0, 1, 2]   # index = Arduino pin minus 2

# Optional per-zone PID overrides:
# setpoint  = 80
# min_speed = 10
```

Find your Arduino USB ID with:
```bash
ls -l /dev/serial/by-id/
```

### Install systemd Service

```bash
# Deploy script
sudo mkdir -p /opt/gpu-fan-control
sudo cp gpu_fan_control_advanced.py /opt/gpu-fan-control/

# Install service (adjust User= in gpu-fan-control.service if needed)
sudo cp gpu-fan-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gpu-fan-control.service
```

### Deploy Updates

```bash
sudo cp gpu_fan_control_advanced.py /opt/gpu-fan-control/
sudo systemctl restart gpu-fan-control.service
```

## Monitoring

```bash
# Live log
sudo journalctl -u gpu-fan-control.service -f

# Last 40 lines
sudo journalctl -u gpu-fan-control.service -n 40 --no-pager

# Errors only
sudo journalctl -t GPU-FAN-CTRL -p err
```

Normal log output:
```
[Main Zone (4x P100)] Temp: 76°C -> PWM: 32% | RPMs: 2460, 2490, 2460
```

## Why Not a Standard Tool (thermald, fancontrol)?

- **thermald**: Intel-specific, controls CPU throttling, has no knowledge of NVIDIA GPUs
- **fancontrol + lm-sensors**: Requires hwmon kernel driver; Arduino USB is not an hwmon device
- **nvidia-settings**: Only for desktop GPUs with their own fan; Tesla P100 has none

The Arduino-based approach is the only practical solution for compute cards without integrated cooling mounted on a custom cooling frame.

## License

Copyright (c) 2026 Markus Reschka

This project is licensed under the **Polyform Noncommercial License 1.0.0**.  
Free to use, modify and distribute for **non-commercial purposes** only.  
See [LICENSE](LICENSE) for details.

## Author

Built with the help of GitHub Copilot.
