/*
  SwanSEDS Liquid engine team
  Throttle Motor Control System — Comms (orchestrator)

  CL42T-V4.1 Closed-Loop Stepper Driver
  + 11HS20-0674D-PG14-E22-300 Geared Stepper Motor (1.8 deg step, ~13.73:1 gearbox)

  Role during calibration:
    1. Raise both calibration pins HIGH to start homing.
    2. Read the encoder count streamed from the encoder board.
    3. Once the count has moved and then held steady, the motor has hit the
       hard stop -> drop both pins LOW to end the sequence.

  Wiring:
    - Comms pin 6  -> Motor board calibrationPin (7)
    - Comms pin 9  -> Encoder board cal_Pin (8)
    - Comms uartEncoder: RX=5, TX=4  <-> Encoder board uart (RX=2, TX=3)
    - ALL THREE BOARDS MUST SHARE A COMMON GROUND.

  Note: only the encoder link is read here, so we only need one SoftwareSerial
  listener. The motor is commanded via the calibration pin, not UART.
*/

#include <SoftwareSerial.h>

SoftwareSerial uartEncoder(5, 4); // RX, TX  <- link to encoder board

// Pins that start/stop calibration on the other two boards
const int motorCallibrationPin   = 6;   // -> motor board pin 7
const int encoderCallibrationPin = 9;   // -> encoder board pin 8

// --- Stop-detection tuning ---
// The encoder jitters by ~+/-30 counts and drifts slowly even when the motor is
// held stationary, so a consecutive-sample comparison never settles. Instead we
// look at the SPAN (max-min) of a sliding window of recent readings:
//   - held stationary : span stays < ~140 counts (measured)
//   - steady motion    : span > ~370 counts over the same window (measured)
// A threshold of ~200 sits cleanly between the two.
const uint8_t  WIN            = 20;    // sliding-window length (samples ~= 0.4 s at 20 ms)
const int16_t  SPAN_THRESHOLD = 200;   // window span below this => stopped
const int16_t  MOVE_THRESHOLD = 200;   // must travel this far before "stopped" is armed
const long     MAX_JUMP       = 300;   // reject any reading that jumps more than this
                                       // between fresh samples (real max ~20/sample) -
                                       // backup guard against a bad-but-valid packet

enum State { IDLE, CALIBRATING, DONE };
State state = IDLE;

int16_t  startCount = 0;
int16_t  lastGood   = 0;
int16_t  win[WIN];        // ring buffer of recent readings
uint8_t  winHead  = 0;
uint8_t  winCount = 0;    // valid samples currently in the window
bool     hasMoved = false;
bool     seeded   = false;

void setup() {
  Serial.begin(115200);       // USB debug to PC
  uartEncoder.begin(38400);   // link to encoder board
  uartEncoder.listen();

  pinMode(motorCallibrationPin, OUTPUT);
  pinMode(encoderCallibrationPin, OUTPUT);
  digitalWrite(motorCallibrationPin, LOW);
  digitalWrite(encoderCallibrationPin, LOW);
}

// Non-blocking, self-resyncing frame parser.
// Frame: 0xAA 0x55 <lo> <hi> <chk>, chk = (lo+hi)&0xFF.
// Returns true and fills 'value' ONLY for a packet that passes the checksum,
// so corrupted / desynced bytes can never inject a garbage reading.
bool readUartEncoder(int16_t &value) {
  static uint8_t st = 0, lo = 0, hi = 0;
  while (uartEncoder.available() > 0) {
    uint8_t b = uartEncoder.read();
    switch (st) {
      case 0: st = (b == 0xAA) ? 1 : 0; break;
      case 1: st = (b == 0x55) ? 2 : (b == 0xAA ? 1 : 0); break;
      case 2: lo = b; st = 3; break;
      case 3: hi = b; st = 4; break;
      case 4:
        st = 0;
        if ((uint8_t)(lo + hi) == b) {                    // checksum OK
          value = (int16_t)((uint16_t)lo | ((uint16_t)hi << 8));
          return true;
        }
        break;                                            // bad checksum -> drop
    }
  }
  return false;
}

void loop() {
  int16_t v;
  bool fresh = readUartEncoder(v);

  switch (state) {

    case IDLE:
      // Start the sequence: tell motor to move and encoder to stream.
      digitalWrite(motorCallibrationPin, HIGH);
      digitalWrite(encoderCallibrationPin, HIGH);
      Serial.println("Calibration started");
      winCount = 0;
      winHead  = 0;
      hasMoved = false;
      seeded   = false;
      state = CALIBRATING;
      break;

    case CALIBRATING:
      if (fresh) {
        // Seed our reference from the first real reading.
        if (!seeded) {
          startCount = v;
          lastGood   = v;
          seeded = true;
        }

        // Backup outlier gate: a genuine reading can't jump more than ~20 counts
        // per 20 ms sample, so anything wilder is a glitch -> ignore it.
        if (labs((long)v - lastGood) > MAX_JUMP) break;
        lastGood = v;

        if (abs(v - startCount) > MOVE_THRESHOLD) hasMoved = true;

        // Push the new reading into the sliding window.
        win[winHead] = v;
        winHead = (winHead + 1) % WIN;
        if (winCount < WIN) winCount++;

        // Once the window is full, check its span (max - min).
        if (winCount == WIN) {
          int16_t lo = win[0], hi = win[0];
          for (uint8_t i = 1; i < WIN; i++) {
            if (win[i] < lo) lo = win[i];
            if (win[i] > hi) hi = win[i];
          }
          int16_t span = hi - lo;

          // Debug: watch these in the Serial Monitor.
          Serial.print(v);
          Serial.print("  span="); Serial.print(span);
          Serial.print("  moved="); Serial.println(hasMoved ? 1 : 0);

          // Moved, then the whole window collapsed to a small span -> hard stop.
          if (hasMoved && span < SPAN_THRESHOLD) {
            digitalWrite(motorCallibrationPin, LOW);
            digitalWrite(encoderCallibrationPin, LOW);
            Serial.print("Calibration complete at count: ");
            Serial.println(v);
            state = DONE;
          }
        } else {
          Serial.println(v);   // still filling the window
        }
      }
      break;

    case DONE:
      // Both pins already LOW. Hold here until reset / re-triggered.
      break;
  }
}


