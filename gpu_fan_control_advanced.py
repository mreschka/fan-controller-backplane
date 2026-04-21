#!/usr/bin/env python3
import tomllib
import serial
import subprocess
import time
import syslog

CONFIG_PATH = "/etc/gpu-fan-control.toml"

# Globale Konfigurationsvariablen – werden beim Start aus CONFIG_PATH geladen
UPDATE_INTERVAL        = None
MIN_RPM_WARN_THRESHOLD = None
RPM_FAIL_DEBOUNCE      = None
FALLBACK_SPEED         = None
ADMIN_EMAIL            = None
CONTROLLERS            = []


class PIDController:
    def __init__(self, setpoint, kp, ki, kd, min_output, max_output, max_slew):
        self.setpoint   = setpoint
        self.kp         = kp
        self.ki         = ki
        self.kd         = kd
        self.min_output = min_output
        self.max_output = max_output
        self.max_slew   = max_slew
        self._integral   = 0.0
        self._prev_error = 0.0
        self._prev_output = float(self.min_output)
        self._last_time  = None

    def compute(self, measured):
        now = time.monotonic()
        if self._last_time is None:
            self._last_time = now
            return int(self._prev_output)
        dt = now - self._last_time
        self._last_time = now
        if dt <= 0:
            return int(self._prev_output)

        error = measured - self.setpoint

        # Integral mit Anti-Windup (Clamping)
        self._integral += error * dt
        windup_limit = (self.max_output - self.min_output) / max(self.ki, 1e-9)
        self._integral = max(-windup_limit, min(windup_limit, self._integral))

        derivative = (error - self._prev_error) / dt
        self._prev_error = error

        raw = self.min_output + self.kp * error + self.ki * self._integral + self.kd * derivative
        clamped = max(float(self.min_output), min(float(self.max_output), raw))

        # Slew-Rate-Begrenzer: verhindert abrupte Drehzahländerungen
        delta = clamped - self._prev_output
        if abs(delta) > self.max_slew:
            clamped = self._prev_output + (self.max_slew if delta > 0 else -self.max_slew)

        self._prev_output = clamped
        return int(round(clamped))


def load_config():
    """Lädt /etc/gpu-fan-control.toml und setzt alle globalen Konfigurationsvariablen."""
    global UPDATE_INTERVAL, MIN_RPM_WARN_THRESHOLD, RPM_FAIL_DEBOUNCE
    global FALLBACK_SPEED, ADMIN_EMAIL, CONTROLLERS

    try:
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        print(f"[FATAL] Konfigurationsdatei nicht gefunden: {CONFIG_PATH}", flush=True)
        raise SystemExit(1)
    except tomllib.TOMLDecodeError as e:
        print(f"[FATAL] Fehler beim Parsen der Konfigurationsdatei: {e}", flush=True)
        raise SystemExit(1)

    g = cfg.get("global", {})
    UPDATE_INTERVAL        = g.get("update_interval",        2.0)
    MIN_RPM_WARN_THRESHOLD = g.get("min_rpm_warn_threshold", 300)
    RPM_FAIL_DEBOUNCE      = g.get("rpm_fail_debounce",      5.0)
    FALLBACK_SPEED         = g.get("fallback_speed",         40)
    ADMIN_EMAIL            = g.get("admin_email",            "root")

    pid_defs = cfg.get("pid_defaults", {})

    CONTROLLERS = []
    for c in cfg.get("controllers", []):
        pid = PIDController(
            setpoint   = c.get("setpoint",  pid_defs.get("setpoint",  75)),
            kp         = c.get("kp",        pid_defs.get("kp",        2.5)),
            ki         = c.get("ki",        pid_defs.get("ki",        0.08)),
            kd         = c.get("kd",        pid_defs.get("kd",        4.0)),
            min_output = c.get("min_speed", pid_defs.get("min_speed", 5)),
            max_output = c.get("max_speed", pid_defs.get("max_speed", 60)),
            max_slew   = c.get("max_slew",  pid_defs.get("max_slew",  5)),
        )
        CONTROLLERS.append({
            "name":          c["name"],
            "port":          c["port"],
            "gpus":          c["gpus"],
            "active_tachos": c.get("active_tachos", []),
            "connection":    None,
            "failed_fans":   set(),
            "fan_low_since": {},
            "pid":           pid,
        })


def log_and_mail(subject, message, is_error=True):
    print(f"[{'ERROR' if is_error else 'INFO'}] {subject} - {message}")
    
    if is_error:
        syslog.syslog(syslog.LOG_ERR, f"GPU-FAN-CTRL: {subject} - {message}")
    else:
        syslog.syslog(syslog.LOG_INFO, f"GPU-FAN-CTRL: {subject} - {message}")

    if is_error:
        try:
            proc = subprocess.Popen(['mail', '-s', subject, ADMIN_EMAIL], stdin=subprocess.PIPE)
            proc.communicate(input=message.encode('utf-8'))
        except Exception as e:
            syslog.syslog(syslog.LOG_ERR, f"GPU-FAN-CTRL: Konnte Alarm-E-Mail nicht senden: {e}")

def get_gpu_temperatures():
    temps = {}
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=index,temperature.gpu', '--format=csv,noheader,nounits'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True
        )
        for line in result.stdout.split('\n'):
            line = line.strip()
            if line and ',' in line:
                idx_str, temp_str = line.split(',')
                temps[int(idx_str.strip())] = int(temp_str.strip())
        return temps
    except Exception as e:
        log_and_mail("Fehler nvidia-smi", f"Konnte GPUs nicht auslesen: {e}", is_error=True)
        return None

def check_fan_rpm(ctrl, rpm_string, target_speed):
    if not rpm_string.startswith("RPM:"):
        return
        
    try:
        rpm_data = rpm_string.replace("RPM:", "").strip()
        rpm_values = [int(x) for x in rpm_data.split(',')]
        
        # Gehe nur durch die Indizes, die in der Konfiguration als aktiv markiert wurden
        for i in ctrl["active_tachos"]:
            # Sicherheitscheck, falls der Arduino weniger Werte schickt als erwartet
            if i >= len(rpm_values):
                continue
                
            rpm = rpm_values[i]
            fan_id = f"Tacho-Port {i} (Arduino Pin {i+2})"
            
            if target_speed > 0 and rpm < MIN_RPM_WARN_THRESHOLD:
                if fan_id not in ctrl["fan_low_since"]:
                    ctrl["fan_low_since"][fan_id] = time.monotonic()
                low_duration = time.monotonic() - ctrl["fan_low_since"][fan_id]
                if low_duration >= RPM_FAIL_DEBOUNCE and fan_id not in ctrl["failed_fans"]:
                    ctrl["failed_fans"].add(fan_id)
                    msg = f"Kritischer Lüfterausfall erkannt!\nZone: {ctrl['name']}\nLüfter: {fan_id}\nIst-Drehzahl: {rpm} U/min\nSoll-PWM: {target_speed}%"
                    log_and_mail(f"LUEFTER AUSFALL in {ctrl['name']}", msg, is_error=True)

            else:
                ctrl["fan_low_since"].pop(fan_id, None)  # Entprellung zurücksetzen
                if rpm >= MIN_RPM_WARN_THRESHOLD and fan_id in ctrl["failed_fans"]:
                    ctrl["failed_fans"].remove(fan_id)
                    msg = f"Lüfter läuft wieder normal.\nZone: {ctrl['name']}\nLüfter: {fan_id}\nIst-Drehzahl: {rpm} U/min"
                    log_and_mail(f"Lüfter OK in {ctrl['name']}", msg, is_error=False)
                
    except ValueError:
        print(f"[{ctrl['name']}] Fehler beim Parsen der RPM Daten: {rpm_string}")


def main():
    load_config()
    syslog.openlog(facility=syslog.LOG_DAEMON)
    log_and_mail("Service Start", "GPU Lüftersteuerung wurde gestartet.", is_error=False)

    while True:
        gpu_temps = get_gpu_temperatures()

        for ctrl in CONTROLLERS:
            if ctrl["connection"] is None or not ctrl["connection"].is_open:
                try:
                    ctrl["connection"] = serial.Serial(ctrl["port"], 9600, timeout=1)
                    time.sleep(2)
                    ctrl["connection"].reset_input_buffer()  # veraltete Meldungen verwerfen
                    ctrl["fan_low_since"].clear()  # Debounce-Zustand zurücksetzen
                    log_and_mail("Arduino Verbunden", f"{ctrl['name']} erfolgreich an {ctrl['port']} angebunden.", is_error=False)
                except serial.SerialException:
                    if "conn_error_logged" not in ctrl:
                        log_and_mail("Arduino Offline", f"Controller {ctrl['name']} ist nicht erreichbar!", is_error=True)
                        ctrl["conn_error_logged"] = True
                    ctrl["connection"] = None
                    continue

            if "conn_error_logged" in ctrl:
                del ctrl["conn_error_logged"]

            if gpu_temps is not None:
                zone_temps = [gpu_temps[g] for g in ctrl["gpus"] if g in gpu_temps]
                max_temp = max(zone_temps) if zone_temps else None
                target_speed = ctrl["pid"].compute(max_temp) if max_temp is not None else None
            else:
                max_temp = None
                target_speed = FALLBACK_SPEED  # nvidia-smi nicht verfügbar → definierter Fallback

            if target_speed is not None:
                try:
                    command = f"{target_speed}\n"
                    ctrl["connection"].write(command.encode('utf-8'))

                    response = ctrl["connection"].readline().decode('utf-8').strip()

                    active_rpms = []
                    if response.startswith("RPM:"):
                        all_rpms = response.replace("RPM:", "").split(',')
                        for idx in ctrl["active_tachos"]:
                            if idx < len(all_rpms):
                                active_rpms.append(all_rpms[idx])
                        display_response = "RPMs: " + ", ".join(active_rpms)
                    else:
                        display_response = response

                    temp_str = f"{max_temp}°C" if max_temp is not None else "kein nvidia-smi"
                    print(f"[{ctrl['name']}] Temp: {temp_str} -> PWM: {target_speed}% | {display_response}")

                    if response.startswith("TIMEOUT:"):
                        log_and_mail(f"Arduino Timeout in {ctrl['name']}", response, is_error=True)
                    elif response:
                        check_fan_rpm(ctrl, response, target_speed)

                except serial.SerialException as e:
                    log_and_mail("Verbindungsabbruch", f"Verbindung zu {ctrl['name']} während der Kommunikation abgebrochen: {e}", is_error=True)
                    ctrl["connection"].close()
                    ctrl["connection"] = None

        time.sleep(UPDATE_INTERVAL)

if __name__ == '__main__':
    main()