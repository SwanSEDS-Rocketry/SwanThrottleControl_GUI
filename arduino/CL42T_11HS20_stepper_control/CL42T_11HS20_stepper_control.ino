/*
  CL42T-V4.1 Closed-Loop Stepper Driver
  + 11HS20-0674D-PG14-E22-300 Geared Stepper Motor (1.8° step angle, 13.73...:1 gearbox)

  Wiring:
    Arduino D2  -> CL42T PUL+
    Arduino GND -> CL42T PUL-
    Arduino D3  -> CL42T DIR+
    Arduino GND -> CL42T DIR-
    Arduino D4  -> CL42T ENA+
    Arduino GND -> CL42T ENA-

  IMPORTANT:
    - Set driver selector switch S3 to "5V" mode (matches Arduino logic level).
    - Set the driver's microstep DIP switches (SW1-SW4) to match MICROSTEPS below.
    - Set the current-setting rotary switch (S1) to suit the motor's 0.67A/phase
      rating -- don't leave it at a setting higher than the motor can handle.
    - ENA is active LOW on most StepperOnline drivers (LOW = enabled). Confirm
      against your specific manual revision; some configurations are active HIGH.
      Code below assumes active LOW; flip ENABLE_ACTIVE_STATE if yours differs.
*/

const int PUL_PIN = 9;
const int DIR_PIN = 10;
const int ENA_PIN = 11;

const bool ENABLE_ACTIVE_STATE = LOW;   // LOW = drive enabled (typical). Set HIGH if your unit is active-high.

// --- Motor / gearbox parameters ---
const float STEP_ANGLE_DEG   = 1.8;     // motor's native step angle, no gearbox
const float GEAR_RATIO       = 13.76;    // PG14 gearbox
const int   MICROSTEPS       = 4;       // must match driver DIP switch setting

// Steps per full revolution of the OUTPUT (gearbox) shaft
const long STEPS_PER_OUTPUT_REV = (long)((360.0 / STEP_ANGLE_DEG) * MICROSTEPS * GEAR_RATIO);

// --- Motion parameters ---
unsigned int pulseDelayMicros = 2000;    // half-period; smaller = faster. Start conservative.

void setup() {
  pinMode(PUL_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(ENA_PIN, OUTPUT);

  digitalWrite(ENA_PIN, ENABLE_ACTIVE_STATE); // enable driver
  digitalWrite(DIR_PIN, HIGH);                // initial direction

  delay(1000); // ENA must lead DIR/PUL by a short settling time per driver timing diagram
}

// Generate one step pulse. CL42T datasheet specifies a minimum pulse width
// (a few microseconds); 800us half-period is well within range for most setups.
void stepPulse() {
  digitalWrite(PUL_PIN, HIGH);
  delayMicroseconds(pulseDelayMicros);
  digitalWrite(PUL_PIN, LOW);
  delayMicroseconds(pulseDelayMicros);
}

// Move a given number of output-shaft steps in a given direction
void moveSteps(long steps, bool clockwise) {
  digitalWrite(DIR_PIN, clockwise ? HIGH : LOW);
  delayMicroseconds(20); // tiny settle time after switching direction
  for (long i = 0; i < steps; i++) {
    stepPulse();
  }
}

void loop() {
  // Example: rotate output shaft one full revolution CW, pause, then CCW
  moveSteps(STEPS_PER_OUTPUT_REV, true);
  delay(1000);
  moveSteps(STEPS_PER_OUTPUT_REV, false);
  delay(1000);
}
