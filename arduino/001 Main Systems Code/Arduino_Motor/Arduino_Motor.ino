/*
  SwanSEDS Liquid engine team
  Throttle Motor Controller

  CL42T-V4.1 Closed-Loop Stepper Driver
  + 11HS20-0674D-PG14-E22-300 Geared Stepper Motor (1.8 deg step, ~13.73:1 gearbox)

  Role:
    This is the ONLY board that ever pulses STEP/DIR/ENA. It is a "dumb
    executor" -- the Comms/Ethernet board is the brain and tells it what to
    do over a framed UART link:
      - CAL_START : step continuously in a given direction/speed
      - CAL_STOP  : stop, then back off N steps, then ack CAL_DONE
      - ESTOP     : stop immediately, no backoff

    Throttle sequences and manual throttle positioning are now driven
    entirely by Comms via closed-loop encoder feedback, reusing CAL_START/
    CAL_STOP for each waypoint -- Comms is the only board with encoder
    access and calibration data, so it makes no sense for Motor to execute
    a pre-baked open-loop step sequence that can't reflect this unit's real
    calibrated span. There is no sequence-upload protocol here anymore.

  Wiring:
    - PUL_PIN 2, DIR_PIN 3, ENA_PIN 4 -> CL42T driver
    - commsSerial RX=8, TX=9  <-> Comms board's motor link (Comms TX2/D16 ->
      here RX=8, Comms RX2/D17 <- here TX=9)
    - COMMON GROUND with the other boards.
*/

#include <SoftwareSerial.h>

const int PUL_PIN = 2;
const int DIR_PIN = 3;
const int ENA_PIN = 4;

const bool ENABLE_ACTIVE_STATE = LOW;   // LOW = CL42T driver enabled

SoftwareSerial commsSerial(8, 9); // RX, TX -- link to Comms board

// ============================================================
// Framed protocol (must match the Comms sketch exactly)
// Frame: 0xAA 0x55 CMD LEN payload[LEN] CHK
//   CHK = (CMD + LEN + sum(payload)) & 0xFF
// ============================================================

const uint8_t CMD_CAL_START = 0x01; // payload: [direction(1), interval_us(4 LE)]
const uint8_t CMD_CAL_STOP  = 0x02; // payload: [backoff_steps(4 LE)]
const uint8_t CMD_ESTOP     = 0x03; // payload: none

const uint8_t ACK_CAL_DONE  = 0x81;
const uint8_t ACK_ESTOPPED  = 0x82;
const uint8_t ACK_ERROR     = 0x8F; // payload: [error_code(1)]

const uint8_t ERR_BAD_CHECKSUM = 1;

// ----------------------------
// Calibration/seek state (continuous stepping, updated once per loop())
// ----------------------------

bool calibrating = false;
bool calDirection = HIGH;
unsigned int calPulseDelayMicros = 300; // half-period; set from CAL_START payload

void setup() {
  Serial.begin(115200);          // USB debug
  commsSerial.begin(38400);      // link to Comms board
  commsSerial.listen();

  pinMode(PUL_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(ENA_PIN, OUTPUT);

  digitalWrite(ENA_PIN, ENABLE_ACTIVE_STATE);   // enable the driver
  digitalWrite(DIR_PIN, LOW);
  digitalWrite(PUL_PIN, LOW);
  delay(1000);   // ENA must lead DIR/PUL per the driver timing diagram

  Serial.println("Motor board ready");
}

// One step pulse. CL42T needs a minimum pulse width of a few microseconds.
void stepPulse(unsigned int halfPeriodUs) {
  digitalWrite(PUL_PIN, HIGH);
  delayMicroseconds(halfPeriodUs);
  digitalWrite(PUL_PIN, LOW);
  delayMicroseconds(halfPeriodUs);
}

// ----------------------------
// Send a framed packet to Comms. Works even if commsSerial isn't the
// currently-"listening" instance -- TX is independent of listen state.
// ----------------------------

void sendFrame(uint8_t cmd, const uint8_t* payload, uint8_t len) {
  uint8_t chk = cmd + len;
  commsSerial.write(0xAA);
  commsSerial.write(0x55);
  commsSerial.write(cmd);
  commsSerial.write(len);
  for (uint8_t i = 0; i < len; i++) {
    commsSerial.write(payload[i]);
    chk += payload[i];
  }
  commsSerial.write(chk);
}

void sendAckSimple(uint8_t ack) { sendFrame(ack, nullptr, 0); }

void sendError(uint8_t errCode) {
  sendFrame(ACK_ERROR, &errCode, 1);
}

// ----------------------------
// Non-blocking small-frame parser (commands from Comms).
// Returns true when a complete, checksum-valid frame is ready.
// ----------------------------

bool readCommand(uint8_t &cmd, uint8_t* payload, uint8_t &len) {
  static uint8_t st = 0, expectedLen = 0, idx = 0, chk = 0, gotCmd = 0;

  while (commsSerial.available() > 0) {
    uint8_t b = commsSerial.read();
    switch (st) {
      case 0: st = (b == 0xAA) ? 1 : 0; break;
      case 1: st = (b == 0x55) ? 2 : (b == 0xAA ? 1 : 0); break;
      case 2: gotCmd = b; chk = b; st = 3; break;
      case 3:
        expectedLen = b; idx = 0; chk += b;
        st = (expectedLen == 0) ? 5 : 4;
        break;
      case 4:
        payload[idx++] = b;
        chk += b;
        if (idx >= expectedLen) st = 5;
        break;
      case 5:
        st = 0;
        if (chk == b) {
          cmd = gotCmd;
          len = expectedLen;
          return true;
        } else {
          sendError(ERR_BAD_CHECKSUM);
        }
        break;
    }
  }
  return false;
}

uint32_t readU32LE(const uint8_t* p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

void loop() {
  // Continuous stepping -- one pulse per loop iteration keeps command
  // checking frequent, so CAL_STOP/ESTOP are seen promptly.
  if (calibrating) {
    digitalWrite(DIR_PIN, calDirection);
    stepPulse(calPulseDelayMicros);
    //Serial.println("Calibrating");
  }

  uint8_t cmd, len, payload[8];
  if (readCommand(cmd, payload, len)) {
    switch (cmd) {

      case CMD_CAL_START: {
        calDirection = payload[0] ? HIGH : LOW;
        uint32_t interval_us = readU32LE(&payload[1]);
        calPulseDelayMicros = (unsigned int)(interval_us / 2);
        calibrating = true;
        break;
      }

      case CMD_CAL_STOP: {
        calibrating = false;
        uint32_t backoff = readU32LE(payload);
        digitalWrite(DIR_PIN, calDirection == HIGH ? LOW : HIGH);
        for (uint32_t i = 0; i < backoff; i++) stepPulse(calPulseDelayMicros);
        sendAckSimple(ACK_CAL_DONE);
        break;
      }

      case CMD_ESTOP: {
        calibrating = false;
        sendAckSimple(ACK_ESTOPPED);
        break;
      }
    }
  }
}



