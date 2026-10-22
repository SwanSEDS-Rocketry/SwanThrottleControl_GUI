/*
  SwanSEDS Liquid engine team
  Throttle Motor Encoder Counter

  CL42T-V4.1 Closed-Loop Stepper Driver
  + 11HS20-0674D-PG14-E22-300 Geared Stepper Motor (1.8 deg step, ~13.73:1 gearbox)

  Role:
    Continuously decode the quadrature encoder. While cal_Pin is HIGH (driven
    by the Comms board), stream the current count so Comms can detect motion/
    stops. Also listens for a RESET command from Comms (sent once homing
    succeeds) that zeroes the count, so 0 becomes the home reference.

  Wiring:
    - cal_Pin 2  <- Comms board D12
    - uart: RX=8, TX=9  <-> Comms board Serial1 (RX1/D19, TX1/D18)
    - Encoder A -> D3, Encoder B -> D4
    - COMMON GROUND with the other boards.
*/

#include <SoftwareSerial.h>

// ===== CALIBRATION HANDSHAKE PIN =====
const int cal_Pin = 2;              // HIGH while comms is calibrating

// ===== ENCODER QUADRATURE PINS =====
const uint8_t PIN_A = 3;
const uint8_t PIN_B = 4;

// ===== ENCODER SPEC =====
const int  ENCODER_PPR    = 300;                 // pulses per rev (from datasheet)
const long COUNTS_PER_REV = ENCODER_PPR * 4L;    // 4x decoding

// ===== STATE =====
long    encoderCount = 0;
uint8_t lastState = 0;

unsigned long lastSendTime = 0;
const unsigned long SEND_INTERVAL_MS = 20;   // stream ~50x/s while calibrating

// Link to comms board. 38400 is far more reliable on SoftwareSerial than 115200.
SoftwareSerial uart(8, 9); // RX, TX

// Reset command: Comms sends two consecutive 0xFE bytes to zero the count.
// 0xFE never appears in the outgoing data frames (which start 0xAA 0x55), so
// requiring two in a row makes an accidental trigger from line noise very
// unlikely without needing a full checksum for a one-shot command.
const uint8_t RESET_MARKER = 0xFE;
uint8_t resetState = 0;

// Quadrature transition table: index = (prevState<<2)|curState
const int8_t qtable[16] = {
   0, -1,  1,  0,
   1,  0,  0, -1,
  -1,  0,  0,  1,
   0,  1, -1,  0
};

void setup() {
  Serial.begin(115200);   // USB debug
  uart.begin(38400);      // link to comms

  pinMode(cal_Pin, INPUT);
  // Use INPUT_PULLUP for open-collector encoders; plain INPUT if push-pull.
  pinMode(PIN_A, INPUT_PULLUP);
  pinMode(PIN_B, INPUT_PULLUP);

  // Seed the quadrature state so the first read isn't a spurious edge.
  uint8_t a = digitalRead(PIN_A);
  uint8_t b = digitalRead(PIN_B);
  lastState = (a << 1) | b;
}

// Framed packet: 0xAA 0x55 <lo> <hi> <chk>
//   - two sync bytes reduce false locks
//   - chk = (lo + hi) & 0xFF lets the receiver reject corrupted packets
// This matters because SoftwareSerial WILL occasionally drop a byte; without a
// checksum a single dropped byte turns into a wild garbage value at the comms end.
void sendInt(int16_t value) {
  uint8_t lo  = (uint8_t)(value & 0xFF);
  uint8_t hi  = (uint8_t)((value >> 8) & 0xFF);
  uint8_t chk = (uint8_t)(lo + hi);
  uart.write(0xAA);
  uart.write(0x55);
  uart.write(lo);
  uart.write(hi);
  uart.write(chk);
}

void loop() {
  // 1) Service the encoder FIRST, every loop, so edges aren't missed.
  uint8_t a = digitalRead(PIN_A);
  uint8_t b = digitalRead(PIN_B);
  uint8_t curState = (a << 1) | b;
  if (curState != lastState) {
    uint8_t index = (lastState << 2) | curState;
    encoderCount += qtable[index];
    lastState = curState;
  }

  // 2) Check for an incoming reset command from Comms.
  while (uart.available() > 0) {
    uint8_t b = uart.read();
    if (b == RESET_MARKER) {
      resetState++;
      if (resetState >= 2) {
        encoderCount = 0;
        resetState = 0;
        Serial.println("Encoder count reset to 0");
      }
    } else {
      resetState = 0;
    }
  }

  // 3) Stream the count to comms while calibration is active.
  if (digitalRead(cal_Pin) == HIGH) {
    if (millis() - lastSendTime >= SEND_INTERVAL_MS) {
      lastSendTime = millis();
      // NOTE: sent as int16_t. If homing travel can exceed +/-32767 counts,
      // switch the protocol to a 4-byte long on both this board and comms.
      sendInt((int16_t)encoderCount);
    }
  }
}


