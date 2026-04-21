#include <PinChangeInterrupt.h>

// --- Konfiguration ---
const int pwmPin = 9;              // PWM-Ausgang zum Lüfter
const int failsafePercent = 40;
const unsigned long timeout = 5000;

// Tacho Pins (Digitale Pins, wir nutzen 2, 3, 4, 5, 6)
const int NUM_FANS = 5;
const int tachoPins[NUM_FANS] = {2, 3, 4, 5, 6};

// --- Vorwärtsdeklarationen ---
void setFanSpeed(int percent);
void sendRPMData();

// --- Interne Variablen ---
unsigned long lastCommandTime = 0;
bool isFailsafeActive = true;

// Variablen für die Drehzahlmessung
volatile unsigned long tachoPulses[NUM_FANS] = {0, 0, 0, 0, 0};
unsigned int rpm[NUM_FANS] = {0, 0, 0, 0, 0};
unsigned long lastRpmCalcTime = 0;

// Interrupt-Service-Routinen (ISR) für jeden Pin
// Ein PC-Lüfter gibt pro Umdrehung 2 Impulse (Flankenwechsel) aus.
void isrFan0() { tachoPulses[0]++; }
void isrFan1() { tachoPulses[1]++; }
void isrFan2() { tachoPulses[2]++; }
void isrFan3() { tachoPulses[3]++; }
void isrFan4() { tachoPulses[4]++; }

void setup() {
  Serial.begin(9600);
  Serial.setTimeout(100);
  pinMode(pwmPin, OUTPUT);
  setFanSpeed(failsafePercent);

  // Tacho-Pins initialisieren und Interrupts anhängen
  for (int i = 0; i < NUM_FANS; i++) {
    pinMode(tachoPins[i], INPUT_PULLUP);
  }
  
  // Attach interrupts (reagieren auf fallende Flanke = RISING, FALLING, oder CHANGE. Wir nehmen FALLING)
  attachPCINT(digitalPinToPCINT(tachoPins[0]), isrFan0, FALLING);
  attachPCINT(digitalPinToPCINT(tachoPins[1]), isrFan1, FALLING);
  attachPCINT(digitalPinToPCINT(tachoPins[2]), isrFan2, FALLING);
  attachPCINT(digitalPinToPCINT(tachoPins[3]), isrFan3, FALLING);
  attachPCINT(digitalPinToPCINT(tachoPins[4]), isrFan4, FALLING);
}

void loop() {
  unsigned long currentMillis = millis();

  // 1. U/min (RPM) berechnen (jede Sekunde)
  if (currentMillis - lastRpmCalcTime >= 1000) {
    // Interrupts kurz deaktivieren, um die Variablen sicher auszulesen
    noInterrupts();
    for (int i = 0; i < NUM_FANS; i++) {
      // 2 Impulse pro Umdrehung. Berechnung: (Impulse / 2) * 60 (für 1 Minute)
      // Das entspricht: Impulse * 30
      rpm[i] = tachoPulses[i] * 30;
      tachoPulses[i] = 0; // Zähler zurücksetzen
    }
    interrupts();
    lastRpmCalcTime = currentMillis;
  }

  // 2. Befehle vom PC empfangen
  if (Serial.available() > 0) {
    int speedPercent = Serial.parseInt();
    Serial.find('\n'); // konsumiert \r\n und \n
    if (speedPercent >= 1 && speedPercent <= 100) {
      setFanSpeed(speedPercent);
      lastCommandTime = currentMillis;
      isFailsafeActive = false;
      sendRPMData();
    }
  }

  // 3. Failsafe-Prüfung
  if (!isFailsafeActive && (currentMillis - lastCommandTime > timeout)) {
    isFailsafeActive = true;
    setFanSpeed(failsafePercent);
    Serial.println("TIMEOUT:Kein Befehl empfangen, Failsafe aktiv.");
  }
}

void setFanSpeed(int percent) {
  int pwmValue = map(percent, 0, 100, 0, 255);
  pwmValue = constrain(pwmValue, 0, 255);
  analogWrite(pwmPin, pwmValue);
}

void sendRPMData() {
  // Sende Format: "RPM: 1200,1150,1220,0,1180\n"
  Serial.print("RPM:");
  for (int i = 0; i < NUM_FANS; i++) {
    Serial.print(rpm[i]);
    if (i < NUM_FANS - 1) {
      Serial.print(",");
    }
  }
  Serial.println();
}