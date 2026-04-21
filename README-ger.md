# GPU Fan Controller (Nvidia Tesla P100)

Aktive Kühllösung für Nvidia Tesla P100 PCIe-GPUs, die über keine eigene aktive Kühlung verfügen. Ein Arduino Nano übernimmt die PWM-Lüftersteuerung und Tacho-Messung per Hardware; ein Python-Systemd-Daemon auf dem Linux-Host liest GPU-Temperaturen via `nvidia-smi` und regelt die Drehzahl über einen PID-Regler.

## Architektur

```
Linux Host
┌──────────────────────────────────────────────────┐
│  gpu_fan_control_advanced.py  (Systemd-Service)  │
│                                                  │
│  nvidia-smi ──> GPU-Temp ──> PID-Regler ──> PWM% │
│                                                  │
│  RPM-Rückmeldung <── Debounce-Alarm-Check        │
└──────────────────────┬──────────────────────┬────┘
                       │ USB Serial (9600)    │
                ┌──────▼───────┐              │
                │ Arduino Nano │              │
                │              │  PWM ────> [Lüfter 1-3]
                │  Pin 9: PWM  │  GND ────> [Lüfter 1-3]
                │  Pin 2: RPM  │  12V ────> (direkt vom NT)
                │  Pin 3: RPM  │
                │  Pin 4: RPM  │  Tacho <── [Lüfter 1-3]
                └──────────────┘
```

## Features

- **PID-Regler** mit Anti-Windup und Slew-Rate-Begrenzer (sanfte Drehzahländerungen, kein Springen)
- **Hardware-Failsafe**: Arduino schaltet nach 5 s ohne Hostbefehl automatisch auf 40% – unabhängig vom Host
- **Tacho-Überwachung** via Pin-Change-Interrupts auf bis zu 5 Lüftern gleichzeitig
- **Debounce-Alarmierung**: Lüfterausfall-Alarm erst nach 5 s kontinuierlichem Unterschreiten des Schwellwerts (verhindert Fehlalarme beim Anlaufen)
- **Multi-Zone**: Mehrere Arduinos/Zonen mit eigenen GPU-Gruppen und PID-Instanzen möglich
- **Systemd-Integration**: Autostart, automatischer Neustart, Logging via journald und syslog

## Dateiübersicht

| Datei | Beschreibung |
|---|---|
| `src/main.cpp` | Arduino-Sketch (PlatformIO) |
| `platformio.ini` | PlatformIO-Projektkonfiguration |
| `gpu_fan_control_advanced.py` | Python-Daemon (PID-Regler, Überwachung) |
| `gpu-fan-control.toml` | Konfigurationsdatei (wird nach `/etc/` installiert) |
| `gpu-fan-control.service` | Systemd-Unit-Datei |

## Hardware

**Getestetes Setup:**
- 4x Nvidia Tesla P100-PCIE-16GB
- 3x Arctic P9 Max (92mm, 500–5000 RPM, Silent-Mode bei <5% PWM)
- Arduino Nano v3 (ATmega328P, alter Bootloader)

**Wichtig:** Die 12V-Lüfterversorgung niemals über den Arduino führen – das zerstört das Board. Die Lüfter bekommen 12V direkt vom Netzteil; **GND von Netzteil und Arduino müssen verbunden sein**.

### Arduino Pinout

| Arduino-Pin | Funktion | Lüfter-Pin |
|---|---|---|
| Pin 9 | PWM-Ausgang (alle Lüfter gemeinsam) | Blau (PWM) |
| Pin 2 | Tacho-Eingang Lüfter 0 | Grün/Gelb |
| Pin 3 | Tacho-Eingang Lüfter 1 | Grün/Gelb |
| Pin 4 | Tacho-Eingang Lüfter 2 | Grün/Gelb |
| Pin 5 | Tacho-Eingang Lüfter 3 | Grün/Gelb |
| Pin 6 | Tacho-Eingang Lüfter 4 | Grün/Gelb |
| GND | Masse | Schwarz |

Tacho-Pins werden per `INPUT_PULLUP` hochgezogen. Die Bibliothek `PinChangeInterrupt` ermöglicht Interrupts auf allen digitalen Pins (nicht nur 2 und 3).

## Serielles Protokoll (Host ↔ Arduino)

**Host → Arduino** (alle ~2 s):
```
30\n        # Ganzzahl 1–100: gewünschte Lüfterleistung in %
```

**Arduino → Host** (Antwort auf jeden Befehl):
```
RPM:1200,1230,1200,0,0\n    # 5 Werte (Tacho-Index 0–4), unbenutzte Pins = 0
```

**Arduino → Host** (bei Timeout nach 5 s ohne Befehl):
```
TIMEOUT:Kein Befehl empfangen, Failsafe aktiv.\n
```

## Software-Setup

### Voraussetzungen

```bash
sudo apt install python3 python3-pip mailutils
pip3 install pyserial
```

Der ausführende User muss in der Gruppe `dialout` sein:
```bash
sudo usermod -aG dialout $USER
# danach neu einloggen
```

### Arduino flashen (PlatformIO)

```bash
cd fan-controller-backplane
pio run --target upload
```

Board: `nanoatmega328` (alter Bootloader, 57600 Baud Upload), Bibliothek: `NicoHood/PinChangeInterrupt @ ^1.2.9`

### Konfigurationsdatei installieren

Die gesamte Konfiguration liegt in `/etc/gpu-fan-control.toml`:

```bash
sudo cp gpu-fan-control.toml /etc/gpu-fan-control.toml
```

Dort lassen sich alle Parameter ohne Anfassen des Scripts anpassen:

```toml
[global]
update_interval        = 2.0    # Sekunden zwischen Regelzyklen
min_rpm_warn_threshold = 300    # RPM-Untergrenze für Lüfterausfall-Alarm
rpm_fail_debounce      = 5.0    # Sekunden Stillstand vor Alarm
fallback_speed         = 40     # % wenn nvidia-smi nicht verfügbar
admin_email            = "root"

[pid_defaults]
setpoint  = 75    # Zieltemperatur in °C
kp        = 2.5
ki        = 0.08
kd        = 4.0
min_speed = 5     # % Minimaldrehzahl
max_speed = 60    # % Maximaldrehzahl
max_slew  = 5     # % maximale Änderung pro Zyklus

[[controllers]]
name          = "Haupt-Zone (4x P100)"
port          = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_..."
gpus          = [0, 1, 2, 3]
active_tachos = [0, 1, 2]   # Index = Arduino-Pin minus 2

# Optionale PID-Overrides pro Zone:
# setpoint  = 80
# min_speed = 10
```

Nach jeder Änderung an der Config:
```bash
sudo systemctl restart gpu-fan-control.service
```

### Python-Skript konfigurieren

USB-ID des Arduino ermitteln und in `/etc/gpu-fan-control.toml` eintragen:
```bash
ls -l /dev/serial/by-id/
```

### Systemd-Service einrichten

```bash
# Skript deployen
sudo mkdir -p /opt/gpu-fan-control
sudo cp gpu_fan_control_advanced.py /opt/gpu-fan-control/

# Service installieren (User in gpu-fan-control.service ggf. anpassen)
sudo cp gpu-fan-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gpu-fan-control.service
```

### Updates deployen

```bash
sudo cp gpu_fan_control_advanced.py /opt/gpu-fan-control/
sudo systemctl restart gpu-fan-control.service
```

## Monitoring

```bash
# Live-Log
sudo journalctl -u gpu-fan-control.service -f

# Letzte 40 Zeilen
sudo journalctl -u gpu-fan-control.service -n 40 --no-pager

# Nur Fehler und Warnungen
sudo journalctl -t GPU-FAN-CTRL -p err
```

Normales Log-Format:
```
[Haupt-Zone (4x P100)] Temp: 76°C -> PWM: 32% | RPMs: 2460, 2490, 2460
```

## Warum kein Standard-Tool (thermald, fancontrol)?

- **thermald**: Intel-spezifisch, steuert CPU-Throttling, kennt keine NVIDIA-GPUs
- **fancontrol + lm-sensors**: Benötigt hwmon-Kernel-Treiber; Arduino-USB ist kein hwmon-Gerät
- **nvidia-settings**: Nur für Desktop-GPUs mit eigenem Lüfter; Tesla P100 hat keinen

Der Arduino-basierte Ansatz ist die einzige praktikable Lösung für Rechenkarten ohne eigene Kühlung an einem selbstgebauten Kühlrahmen.

## License

Copyright (c) 2026 Markus Reschka

This project is licensed under the **Polyform Noncommercial License 1.0.0**.  
Free to use, modify and distribute for **non-commercial purposes** only.  
See [LICENSE](LICENSE) for details.

## Author

Built with the help of GitHub Copilot.
