const int STEP_PIN = 9;   // to PUL-
const int DIR_PIN  = 10;   // to DIR-
const int ENA_PIN  = 11;   // to ENA-

const int stepsPerRev = 1600; // match SW1-SW4 on CL42T

void setup() {
  pinMode(STEP_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(ENA_PIN, OUTPUT);

  digitalWrite(STEP_PIN, HIGH); // idle high for common-anode wiring
  digitalWrite(DIR_PIN, LOW);

  // Enable driver
  // If motor does not enable, try HIGH instead.
  digitalWrite(ENA_PIN, LOW);
}

void loop() {
  digitalWrite(DIR_PIN, LOW);
  moveSteps(stepsPerRev);   // one revolution
  delay(1000);

  digitalWrite(DIR_PIN, HIGH);
  moveSteps(stepsPerRev);   // one revolution back
  delay(1000);
}

void moveSteps(int steps) {
  for (int i = 0; i < steps; i++) {
    digitalWrite(STEP_PIN, LOW);
    delayMicroseconds(20);
    digitalWrite(STEP_PIN, HIGH);
    delayMicroseconds(800);
  }
}