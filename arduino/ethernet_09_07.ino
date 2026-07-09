/*
  SwanSEDS Liquid engine team
  Comms/Ethernet board -- the middleman between the laptop, the Motor board,
  and the Encoder board.

  BOARD: Arduino Mega + Arduino Ethernet Shield Rev2.
  This uses hardware Serial1/Serial2 for the Motor and Encoder links instead
  of SoftwareSerial. That's a deliberate hardware choice: a single
  SoftwareSerial instance can only receive on one link at a time, but this
  board needs to receive from BOTH Motor (acks) and Encoder (continuous
  telemetry) at once, especially while a throttle sequence is running.
  Separate hardware UARTs receive independently with no risk of dropped
  bytes on either link, and the Mega's much larger SRAM also removes the
  memory pressure the Uno was hitting.

  Responsibilities:
    - Talks to the laptop over Ethernet (TCP for commands/sequences, UDP for
      telemetry + recalibrate).
    - Talks to the Motor board over Serial2 with a framed protocol: sends
      CAL_START / CAL_STOP / ESTOP / RUN_SEQ commands, receives acks. This
      board NEVER touches STEP/DIR/ENA directly -- Motor board owns the
      driver.
    - Talks to the Encoder board over Serial1, which streams continuously
      once ENC_CAL_PIN is raised (done once at boot and left high). Runs the
      sliding-window stop-detector during calibration, and during throttle
      sequences forwards every new encoder sample to the laptop live over
      UDP.

  Wiring (Ethernet Shield Rev2 uses the ICSP header for SPI, plus D10 as the
  W5500 chip-select and D4 as the SD chip-select -- both boards, but D11-13
  are free for other use on a Mega since SPI runs over ICSP):
    - Serial2: TX2(16) -> Motor board RX(8), RX2(17) <- Motor board TX(9)
    - Serial1: TX1(18) -> Encoder board RX(8), RX1(19) <- Encoder board TX(9)
    - D12       -> Encoder board cal_Pin (driven HIGH once at boot)
    - D11       <- ARM_PIN / START_PIN, external arm/start-permission input
    - A1, A2, A3 -> 5V / 24V / 48V rail voltage dividers (raw ADC reads)
*/

#include <SPI.h>
#include <Ethernet.h>
#include <EthernetUdp.h>
#include <SD.h>
#include <Wire.h>
#include <RTClib.h>   // Adafruit RTClib -- supports the PCF8523 directly
#include <math.h>     // lround() for float target -> encoder count rounding

// StepCommand is defined here, immediately after the includes, rather than
// down near getCommand() where it's used. This is required, not stylistic:
// the Arduino builder auto-generates forward declarations for every
// function in the sketch and inserts them right after the last #include,
// BEFORE compiling the rest of the file. Since getCommand() returns
// StepCommand, that auto-generated prototype needs the type to already be
// known at this point in the file -- if the struct were defined further
// down (where it's actually used), the auto-inserted prototype would
// reference an as-yet-undefined type and fail with "StepCommand does not
// name a type", even though the code below is otherwise correct.
struct StepCommand {
  uint32_t duration_ms;
  int32_t steps;          // no longer used -- kept only to match the wire format
  uint8_t direction;      // no longer used -- direction is derived from calibration instead
  uint32_t interval_us;   // used as the seek speed for this waypoint
  float commanded_throttle; // fraction 0.0-1.0 -- fed through h_of_throttle() for the open-loop step calculation
};

// ----------------------------
// Board links (hardware serial -- no SoftwareSerial needed on a Mega)
// ----------------------------

#define motorLink   Serial2
#define encoderLink Serial1

const int ENC_CAL_PIN = 12;   // -> Encoder board cal_Pin; held HIGH permanently

const int ETH_CS_PIN = 10; // W5500 chip-select
const int SD_CS_PIN  = 4;  // shield's SD card chip-select

// PCF8523 RTC over I2C (Mega's dedicated SDA=20/SCL=21 pins -- no conflict
// with anything else on this board). Used ONLY to name log files uniquely
// across power cycles; individual sample rows still use millis() since the
// RTC's 1-second resolution is far coarser than the ~20ms encoder rate.
RTC_PCF8523 rtc;
bool rtcReady = false;

// SD card is a local backup log, in case a UDP packet gets dropped or the
// laptop disconnects mid-run. Every calibration and every throttle sequence
// gets its own CSV file: "millis_ms,encoder_count" per line.
bool sdReady = false;
File logFile;
bool loggingActive = false;

// ----------------------------
// Voltage analogue inputs
// ----------------------------
// ENC_CAL_PIN is D12, not an analog pin, so A0 is currently unused; these
// three voltage dividers are on A1-A3.

const int VOLTAGE_5_PIN  = A3;
const int VOLTAGE_24_PIN = A2;
const int VOLTAGE_48_PIN = A1;


// ============================================================
// Motor-link framed protocol (must match Motor sketch exactly)
// Frame: 0xAA 0x55 CMD LEN payload[LEN] CHK
// ============================================================

const uint8_t CMD_CAL_START      = 0x01;
const uint8_t CMD_CAL_STOP       = 0x02;
const uint8_t CMD_ESTOP          = 0x03;
const uint8_t CMD_RUN_SEQ_START  = 0x04;
const uint8_t CMD_RUN_SEQ_EXECUTE = 0x05;
const uint8_t CMD_MANUAL_THROTTLE = 0x06; // payload: [target_percent as float, 4 bytes]

const uint8_t ACK_CAL_DONE       = 0x81;
const uint8_t ACK_ESTOPPED       = 0x82;
const uint8_t ACK_SEQ_RECEIVED   = 0x83;
const uint8_t ACK_SEQ_DONE       = 0x84;
const uint8_t ACK_ERROR          = 0x8F;

// Encoder reset: two consecutive 0xFE bytes tell the Encoder board to zero
// its count. Sent once, right after a SUCCESSFUL calibration, so 0 becomes
// the home reference. Must match the Encoder sketch's RESET_MARKER.
const uint8_t ENCODER_RESET_MARKER = 0xFE;

// External start-permission input: a throttle sequence is only allowed to
// actually begin moving once this pin reads HIGH. Driven by other test
// system control hardware -- this board never drives it. Same physical pin
// as the arm indicator below -- "armed" and "start permission" are the same
// signal, just named for two different consumers (sequence gating vs. the
// GUI's arm/disarm display).
const int START_PIN = 11;
const int ARM_PIN = START_PIN;
const unsigned long START_PIN_TIMEOUT_MS = 5UL * 60UL * 1000UL; // abort if never seen HIGH

bool systemArmed = false;

// ----------------------------
// Calibration parameters (Comms is the brain; Motor just executes)
//
// Single-stop calibration: drive CAL_DIRECTION until the encoder stalls
// against the 20% (idle) stop, zero the encoder there, and stop. There is
// no second phase anymore -- the motor is NOT driven the other way to find
// a 100% reference. That means there is no known upper boundary; only the
// zero/home stop is calibrated. See isWithinCalibratedRangeGuard() and the
// manual throttle / sequence targeting below for what this changes.
// ----------------------------

const uint8_t  CAL_DIRECTION        = LOW;  // direction that finds the 20% (idle) stop
const uint32_t CAL_INTERVAL_US      = 600;   // step period during homing (larger = slower)
const uint32_t CAL_BACKOFF_STEPS    = 200;   // back off the hard stop after homing

// Sliding-window stop detector (validated on the bench):
//  - held/stalled: 20-sample window span stays < ~140 counts
//  - moving:       window span > ~370 counts
const uint8_t  CAL_WIN            = 20;
const int16_t  CAL_SPAN_THRESHOLD = 200;
const int16_t  CAL_MOVE_THRESHOLD = 200;
const long     CAL_MAX_JUMP       = 300;
const unsigned long CAL_TIMEOUT_MS = 30000; // abort (ESTOP) if no stop detected in time

// ----------------------------
// Sleeve height model (ported from motor_control_code.ino) -- used by
// executeSequence() for OPEN-LOOP step/direction calculation. This is a
// forward model only (throttle fraction -> height h in metres, measured
// from the fully-closed position, h=0 at throttle=0). It is deliberately
// NOT inverted anywhere: the throttle value reported to the laptop is the
// exact commanded input value, not a numerically-inverted height, since we
// already have it exactly and inverting a quartic just to reproduce the
// same number adds risk (solver precision, non-monotonic regions) for no
// benefit. If "throttle" needs to instead reflect the ENCODER's actual
// measured position rather than the commanded one, that requires a
// counts-per-metre conversion this file doesn't provide.
//
// IMPORTANT: unlike manual throttle / calibration (which stay closed-loop,
// unchanged), sequence execution using this model is OPEN-LOOP -- Motor
// runs the computed step count blind, with no real-time position check
// against it. See the comment above executeSequence() for what that trades
// away.
// ----------------------------

const float LOWER_HARDSTOP_H = 0.0001153f; // m from fully closed; single-stop calibration zeros here (~20% throttle)
const float UPPER_HARDSTOP_H = 0.0005013f; // m from fully closed; ~100% throttle (not calibrated against -- known mechanical value only)

const float GEARBOX_RATIO = 19.19f;
const int   STEPS_DIV     = 400;

float h_of_throttle(float throttle) {
  float t2 = throttle * throttle;
  float t3 = t2 * throttle;
  float t4 = t3 * throttle;
  return (-0.000351114f * t4 + 0.00101334f * t3 - 0.000935964f * t2 + 0.00077505f * throttle)*3.6;
}

// Calibration results. calStop1Count is always 0 by construction (the
// encoder is zeroed the instant the stop is found). calDirectionIncreasesCount
// records which physical direction moves the count UP, observed during the
// single homing pass -- this replaces the old two-point calibration's use
// of calStop2Count's sign for the same purpose. Neither is meaningful until
// calibrated == true.
bool calibrated = false;
const int16_t calStop1Count = 0;
bool calDirectionIncreasesCount = false;

// ----------------------------
// TCP command constants
// ----------------------------

constexpr uint16_t TCP_PING_COMMAND      = 0xFFFF;
constexpr uint16_t TCP_CALIBRATE_COMMAND = 0xCA1B;
constexpr uint16_t TCP_MANUAL_THROTTLE_COMMAND = 0x4D54;  // 'MT'

// ----------------------------
// Sequence buffer. Sequences are now executed here via closed-loop seeking
// (see executeSequence()), not forwarded to Motor for open-loop execution --
// Motor's own step counts assume a generic geometry, not this unit's real
// calibrated span, so they can't be trusted directly. A 512-byte buffer is
// fine on a Mega's 8KB SRAM (it was the thing that overflowed the Uno).
// ----------------------------

constexpr size_t sequence_buffer_max = 512;
constexpr size_t STEP_COMMAND_SIZE = 17;     // 4 + 4 + 1 + 4 + 4

uint8_t sequence_buffer[sequence_buffer_max];

StepCommand getCommand(size_t index) {
  StepCommand cmd;
  size_t offset = index * STEP_COMMAND_SIZE;
  memcpy(&cmd.duration_ms, sequence_buffer + offset, 4); offset += 4;
  memcpy(&cmd.steps, sequence_buffer + offset, 4); offset += 4;
  memcpy(&cmd.direction, sequence_buffer + offset, 1); offset += 1;
  memcpy(&cmd.interval_us, sequence_buffer + offset, 4); offset += 4;
  memcpy(&cmd.commanded_throttle, sequence_buffer + offset, 4);
  return cmd;
}

bool running_throttle_sequence = false;
bool calibrating_throttle = false;
bool manual_seek_active = false;

// ----------------------------
// Manual throttle seek tuning
//
// No deceleration profiling: the seek steps at the same constant speed as
// calibration (CAL_INTERVAL_US) all the way in, then stops. Encoder samples
// arrive ~every 20ms, and at CAL_INTERVAL_US the motor covers roughly
// 15-20 counts per sample on this hardware (per bench data), so an exact
// stop isn't possible -- expect the final position to land within roughly
// one sample's travel of the tolerance band, not exactly on it. If tighter
// positioning is needed, the fix is slowing down (or actively decelerating)
// as the target approaches -- not implemented here.
// ----------------------------

const int16_t MANUAL_SEEK_TOLERANCE_COUNTS = 15; // "close enough" band

// Allow a small encoder overshoot past the calibrated end stops before
// calling it a hard out-of-range fault. The motor can travel between encoder
// samples, so a strict 0-count guard at 20%/100% causes false
// ERROR_OUT_OF_RANGE trips when commanding the exact end points.
const int16_t CALIBRATED_RANGE_GUARD_COUNTS = 60;

const unsigned long MANUAL_SEEK_TIMEOUT_MS = 30000; // abort if unreachable in time


// Manual throttle from the Python GUI is sent as a FLOAT PERCENT, not an
// encoder count. Single-stop calibration only gives us the low/20% home
// reference, so 100% is defined using this fixed measured encoder span.
// Tune this value if 100% lands slightly short/long on the real valve.
const float MANUAL_THROTTLE_MIN_PERCENT = 20.0f;
const float MANUAL_THROTTLE_MAX_PERCENT = 100.0f;
const int16_t MANUAL_THROTTLE_LOW_TO_HIGH_COUNTS = 3236;


// ----------------------------
// Networking globals
// ----------------------------

byte mac[] = { 0xA8, 0x61, 0x0A, 0xAE, 0xB3, 0x70 };
byte ip[] = { 192, 168, 10, 2 };
uint16_t port = 5000;

byte remote_ip[] = { 192, 168, 10, 1 };
uint16_t remote_port = 7080;

EthernetServer server = EthernetServer(port);
EthernetClient client;
EthernetUDP udp;

enum class UDPCommand : int8_t {
  NONE = 0,
  RECALIBRATE = 1,
  ESTOP = 2,
};

// ----------------------------
// Latest encoder reading, kept up to date by pollEncoder() every loop().
// ----------------------------

int16_t latestEncoderCount = 0;
bool    encoderEverSeen = false;

// Set true when a status change needs to be pushed immediately, rather than
// waiting for the normal 100 ms UDP heartbeat interval.
bool forceUdpTelemetryNow = false;

// ----------------------------
// Setup
// ----------------------------

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println(" ---- Arduino Starting ---- ");
  delay(500);

  motorLink.begin(38400);
  encoderLink.begin(38400);

  pinMode(ENC_CAL_PIN, OUTPUT);
  digitalWrite(ENC_CAL_PIN, HIGH);  // encoder streams continuously from now on

  // Driven by external test system control hardware -- plain INPUT since
  // that hardware actively drives both HIGH and LOW. Switch to INPUT_PULLUP
  // if it's an open-collector/active-low-only signal instead.
  pinMode(START_PIN, INPUT);

  // Ethernet and the SD card share the SPI bus -- hold both chip-selects
  // HIGH before either library touches the bus, so they don't contend
  // during init.
  pinMode(ETH_CS_PIN, OUTPUT);
  digitalWrite(ETH_CS_PIN, HIGH);
  pinMode(SD_CS_PIN, OUTPUT);
  digitalWrite(SD_CS_PIN, HIGH);

  // Some cards (especially older/cheaper ones, or long shield traces) don't
  // reliably init at full SPI speed. Try full speed first; if that fails,
  // retry once at half speed before giving up.
  if (SD.begin(SD_CS_PIN)) {
    sdReady = true;
    Serial.println("SD card ready (full speed)");
  } else {
    Serial.println("SD card init failed at full speed, retrying at half speed...");
    if (SD.begin(SPI_HALF_SPEED, SD_CS_PIN)) {
      sdReady = true;
      Serial.println("SD card ready (half speed)");
    } else {
      sdReady = false;
      Serial.println("SD card init FAILED -- continuing without local logging");
      Serial.println("Check: card formatted FAT16/FAT32 (not exFAT), fully seated, and <=32GB.");
    }
  }

  Wire.begin();
  if (rtc.begin()) {
    rtcReady = true;
    if (!rtc.initialized() || rtc.lostPower()) {
      Serial.println("RTC lost power / not set -- setting from compile time");
      rtc.adjust(DateTime(F(__DATE__), F(__TIME__)));
    }
    DateTime now = rtc.now();
    Serial.print("RTC time: ");
    Serial.println(now.timestamp());
  } else {
    rtcReady = false;
    Serial.println("RTC (PCF8523) not found -- log filenames will fall back to millis()");
  }

  Serial.println("Starting Ethernet...");
  Ethernet.begin(mac, ip);
  Serial.print("Arduino IP: ");
  Serial.println(Ethernet.localIP());

  udp.begin(port);
  Serial.print("UDP started on port ");
  Serial.println(port);

  server.begin();
  Serial.print("TCP server started on port ");
  Serial.println(port);
}

void loop() {
  serviceLiveUpdates();

  if (!running_throttle_sequence && !calibrating_throttle && !manual_seek_active) {
    checkForTcpSequence();
    checkForUdpCommand();
  }
}

// ----------------------------
// TCP helper
// ----------------------------

void sendTcpLine(const char* message) {
  if (client && client.connected()) {
    client.print(message);
    client.print("\n");
    client.flush();
  }
}

// ----------------------------
// Arm indicator helpers
//
// ARM_PIN (D11) HIGH = ARMED. Sent to the laptop as plain TCP lines ("ARMED" /
// "DISARMED"), the same channel as other status messages -- the Python GUI
// watches for these to drive its arm indicator and to gate whether it will
// even attempt to start a sequence.
// ----------------------------

bool readArmPin() {
  return digitalRead(ARM_PIN) == HIGH;
}

void sendArmStateTcp() {
  if (readArmPin()) {
    sendTcpLine("ARMED");
  } else {
    sendTcpLine("DISARMED");
  }
}

void setArmedState(bool armed) {
  systemArmed = armed;

  if (systemArmed) {
    Serial.println("System ARMED");
    sendTcpLine("ARMED");
  } else {
    Serial.println("System DISARMED");
    sendTcpLine("DISARMED");
  }
}

// Call every loop() iteration. Polls the pin at most every 100ms and only
// sends a TCP line when the state actually changes (plus once on the very
// first check), so this doesn't spam the connection every iteration.
void updateArmedStateFromPin() {
  static unsigned long last_arm_check = 0;
  static bool last_sent_arm_state = false;
  static bool first_run = true;

  const unsigned long arm_check_interval_ms = 100;

  if (millis() - last_arm_check < arm_check_interval_ms) {
    return;
  }

  last_arm_check = millis();

  bool current_arm_state = readArmPin();

  systemArmed = current_arm_state;

  if (first_run || current_arm_state != last_sent_arm_state) {
    first_run = false;
    last_sent_arm_state = current_arm_state;

    if (current_arm_state) {
      Serial.println("System ARMED");
      sendTcpLine("ARMED");
    } else {
      Serial.println("System DISARMED");
      sendTcpLine("DISARMED");
    }

    // Make the GUI update immediately when the arm input changes, even if
    // the normal UDP heartbeat interval has not elapsed yet.
    forceUdpTelemetryNow = true;
    sendUdpTelemetry();
  }
}

// Keep all live inputs/outputs refreshed while the sketch is inside blocking
// calibration, manual seek, or sequence waits. Without this, the GUI can miss
// arm/disarm changes until the blocking operation returns to loop().
void serviceLiveUpdates() {
  pollEncoder();
  updateArmedStateFromPin();
  sendUdpTelemetry();
}

// Used to gate the START of any commanded motion (sequence execution,
// manual throttle seek). A single digitalRead() is vulnerable to a noise
// glitch on a floating/undriven pin reading momentarily HIGH -- this
// requires several consecutive HIGH reads, spaced out, before treating the
// pin as genuinely armed. Deliberately asymmetric with how motion is
// monitored once underway (see seekToCount()): starting requires sustained
// confirmation, but stopping happens on a single LOW reading with no
// debounce, because the safe direction is always to stop.
//
// NOTE: this is a firmware mitigation, not a substitute for correct
// hardware. If the external interlock is open-collector/relay-contact
// based (i.e. it doesn't actively drive the pin LOW when disarmed), the
// pin can float and read unpredictably when disarmed -- the real fix is an
// external pull-down resistor (e.g. 10k from ARM_PIN to GND) so the pin
// defaults to a defined LOW/disarmed state. Classic AVR boards like the
// Mega have no internal pull-down, only pull-up, so this can't be fixed
// from software alone.
const uint8_t ARM_CONFIRM_READS = 5;
const unsigned long ARM_CONFIRM_SPACING_MS = 2;

bool isConfirmedArmed() {
  for (uint8_t i = 0; i < ARM_CONFIRM_READS; i++) {
    if (digitalRead(ARM_PIN) != HIGH) return false;
    delay(ARM_CONFIRM_SPACING_MS);
  }
  return true;
}


// ============================================================
// Motor-link helpers
// ============================================================

void writeMotorFrame(uint8_t cmd, const uint8_t* payload, uint8_t len) {
  uint8_t chk = cmd + len;
  motorLink.write((uint8_t)0xAA);
  motorLink.write((uint8_t)0x55);
  motorLink.write(cmd);
  motorLink.write(len);
  for (uint8_t i = 0; i < len; i++) {
    motorLink.write(payload[i]);
    chk += payload[i];
  }
  motorLink.write(chk);
}

void writeU32LE(uint8_t* p, uint32_t v) {
  p[0] = v & 0xFF; p[1] = (v >> 8) & 0xFF; p[2] = (v >> 16) & 0xFF; p[3] = (v >> 24) & 0xFF;
}
void writeU16LE(uint8_t* p, uint16_t v) {
  p[0] = v & 0xFF; p[1] = (v >> 8) & 0xFF;
}

// Feed one incoming byte from Motor into the small-frame parser. Returns
// true when a complete, checksum-valid frame is ready.
bool feedMotorByte(uint8_t b, uint8_t &cmdOut, uint8_t* payloadOut, uint8_t &lenOut) {
  static uint8_t st = 0, cmd = 0, expectedLen = 0, idx = 0, chk = 0;
  static uint8_t payload[8];

  switch (st) {
    case 0: st = (b == 0xAA) ? 1 : 0; break;
    case 1: st = (b == 0x55) ? 2 : (b == 0xAA ? 1 : 0); break;
    case 2: cmd = b; chk = b; st = 3; break;
    case 3:
      expectedLen = b; idx = 0; chk += b;
      st = (expectedLen == 0) ? 5 : 4;
      break;
    case 4:
      payload[idx++] = b; chk += b;
      if (idx >= expectedLen) st = 5;
      break;
    case 5:
      st = 0;
      if (chk == b) {
        cmdOut = cmd;
        lenOut = expectedLen;
        memcpy(payloadOut, payload, expectedLen);
        return true;
      }
      break;
  }
  return false;
}

// Blocking wait (with timeout) for a specific ack from Motor. Simple case:
// used when we don't need to do anything else while waiting (e.g. after
// CAL_STOP, or after ESTOP).
bool waitForMotorAck(uint8_t expectedAck, unsigned long timeout_ms) {
  uint8_t cmd, len, payload[8];
  unsigned long t0 = millis();

  while (millis() - t0 < timeout_ms) {
    if (motorLink.available() > 0) {
      if (feedMotorByte(motorLink.read(), cmd, payload, len)) {
        if (cmd == ACK_ERROR) {
          Serial.print("Motor reported error code ");
          Serial.println(len > 0 ? payload[0] : 255);
          return false;
        }
        if (cmd == expectedAck) return true;
      }
    }
  }
  Serial.println("Timed out waiting for motor ack");
  return false;
}

// ----------------------------
// Encoder frame parser (continuous, non-blocking)
// Frame: 0xAA 0x55 <lo> <hi> <chk>, chk = (lo + hi) & 0xFF
// ----------------------------

bool readEncoderByte(uint8_t b, int16_t &value) {
  static uint8_t st = 0, lo = 0, hi = 0;
  switch (st) {
    case 0: st = (b == 0xAA) ? 1 : 0; break;
    case 1: st = (b == 0x55) ? 2 : (b == 0xAA ? 1 : 0); break;
    case 2: lo = b; st = 3; break;
    case 3: hi = b; st = 4; break;
    case 4:
      st = 0;
      if ((uint8_t)(lo + hi) == b) {
        value = (int16_t)((uint16_t)lo | ((uint16_t)hi << 8));
        return true;
      }
      break;
  }
  return false;
}

// Call every loop() iteration: drains whatever is waiting on the encoder
// link and updates latestEncoderCount. Returns true if a NEW sample arrived
// (so callers that want every sample, not just the latest, can react).
bool pollEncoder() {
  bool gotOne = false;
  while (encoderLink.available() > 0) {
    int16_t v;
    if (readEncoderByte(encoderLink.read(), v)) {
      latestEncoderCount = v;
      encoderEverSeen = true;
      gotOne = true;
    }
  }
  return gotOne;
}

// Tell the Encoder board to zero its count. Sent only after a SUCCESSFUL
// calibration, so 0 becomes the home reference for subsequent readings.
void resetEncoderCount() {
  encoderLink.write(ENCODER_RESET_MARKER);
  encoderLink.write(ENCODER_RESET_MARKER);
  Serial.println("Sent encoder reset command");
}

// New packet type, sent once per waypoint from executeSequence() (not per
// sample -- this is model-computed data, not a live encoder reading).
// Deliberately a NEW marker ('H') rather than repurposing 'E' or 'T', so
// existing laptop-side parsing of those is completely undisturbed.
// Format: ['H'] [millis u32 LE] [throttle f32 LE] [h_sleeve_raw f32 LE]
//         [displacement_from_min_hardstop f32 LE] = 17 bytes.
//   throttle: the exact commanded input value for this waypoint (0.0-1.0)
//   h_sleeve_raw: h_of_throttle(throttle) -- metres from fully closed
//   displacement_from_min_hardstop: h_sleeve_raw - LOWER_HARDSTOP_H --
//     metres from the calibrated zero (negative if somehow below it)
void sendSleeveDataUdp(float throttle, float h_sleeve_raw, float displacement_from_min_hardstop) {
  uint8_t pkt[17];
  pkt[0] = 'H';
  uint32_t t = millis();
  pkt[1] = t & 0xFF; pkt[2] = (t >> 8) & 0xFF; pkt[3] = (t >> 16) & 0xFF; pkt[4] = (t >> 24) & 0xFF;
  memcpy(&pkt[5], &throttle, 4);
  memcpy(&pkt[9], &h_sleeve_raw, 4);
  memcpy(&pkt[13], &displacement_from_min_hardstop, 4);

  udp.beginPacket(remote_ip, remote_port);
  udp.write(pkt, sizeof(pkt));
  udp.endPacket();
}

// Forward one encoder sample to the laptop immediately, as its own UDP
// packet. Always the raw encoder count now (the laptop displays encoder
// count directly, not throttle percent). Byte 7 is unused padding, kept
// for wire-format compatibility with the old isPercent flag.
// Format: ['E'] [millis uint32 LE] [count int16 LE] [pad] = 8 bytes.
void sendEncoderSample(int16_t value) {
  uint8_t pkt[8];
  pkt[0] = 'E';
  uint32_t t = millis();
  pkt[1] = t & 0xFF; pkt[2] = (t >> 8) & 0xFF; pkt[3] = (t >> 16) & 0xFF; pkt[4] = (t >> 24) & 0xFF;

  pkt[5] = value & 0xFF;
  pkt[6] = (value >> 8) & 0xFF;
  pkt[7] = 0;

  udp.beginPacket(remote_ip, remote_port);
  udp.write(pkt, 8);
  udp.endPacket();
}

// ============================================================
// Local SD backup logging (one CSV file per calibration run / sequence run)
// ============================================================

// Filenames always end in ".csv" (fits the SD library's 8.3 limit: 8-char
// name, 3-char extension) using DDHHMMSS from the RTC to stay unique across
// power cycles, e.g. "14153042.csv" = day 14, 15:30:42. Falls back to a
// millis()-based name if the RTC isn't present. typeExt ("CAL"/"SEQ"/"MAN")
// distinguishes the kind of run -- since it no longer fits in the
// extension, it's written as the first line inside the file instead.
void startLogging(const char* typeExt) {
  if (!sdReady) return;

  char filename[13];
  if (rtcReady) {
    DateTime now = rtc.now();
    snprintf(filename, sizeof(filename), "%02d%02d%02d%02d.csv",
             now.day(), now.hour(), now.minute(), now.second());
  } else {
    snprintf(filename, sizeof(filename), "%06lX.csv", millis() & 0xFFFFFFUL);
  }

  logFile = SD.open(filename, FILE_WRITE);
  if (logFile) {
    logFile.print("type,");
    logFile.println(typeExt); // CAL / SEQ / MAN -- distinguishes the run now that it's not in the extension
    if (strcmp(typeExt, "SEQ") == 0) {
      // row_type distinguishes a per-sample encoder reading (fields after
      // encoder_count blank) from a per-waypoint summary (all fields
      // filled) -- kept in one file/one column count for easy loading.
      logFile.println("millis_ms,row_type,encoder_count,waypoint_index,commanded_throttle,h_sleeve_raw,displacement_from_min_hardstop,steps_commanded,direction");
    } else {
      logFile.println("millis_ms,encoder_count"); // CAL / MAN: always raw counts
    }
    loggingActive = true;
    Serial.print("Logging to ");
    Serial.println(filename);
  } else {
    loggingActive = false;
    Serial.print("ERROR: could not open log file ");
    Serial.println(filename);
  }
}

// Per-sample row for a SEQ log during the timed open-loop wait -- same
// column count as the waypoint summary row, with the waypoint-specific
// fields left blank.
void logSequenceSample(int16_t value) {
  if (!loggingActive) return;
  logFile.print(millis()); logFile.print(",SAMPLE,");
  logFile.print(value);
  logFile.println(",,,,,,"); // waypoint_index..direction left blank
  logFile.flush();
}

// One row per waypoint in a SEQ log, written before executing it.
void logWaypointSummary(size_t waypoint_index, float commanded_throttle,
                         float h_sleeve_raw, float displacement_from_min_hardstop,
                         long steps_commanded, int direction) {
  if (!loggingActive) return;
  logFile.print(millis()); logFile.print(",WAYPOINT,");
  logFile.print(latestEncoderCount); logFile.print(",");
  logFile.print(waypoint_index); logFile.print(",");
  logFile.print(commanded_throttle, 4); logFile.print(",");
  logFile.print(h_sleeve_raw, 8); logFile.print(",");
  logFile.print(displacement_from_min_hardstop, 8); logFile.print(",");
  logFile.print(steps_commanded); logFile.print(",");
  logFile.println(direction);
  logFile.flush();
}

// Logs the raw encoder count -- always, now (the laptop displays encoder
// count directly, not throttle percent).
void logSample(int16_t value) {
  if (!loggingActive) return;
  logFile.print(millis());
  logFile.print(',');
  logFile.println(value);
  logFile.flush(); // written promptly so a crash/reset doesn't lose the file
}

void stopLogging() {
  if (!loggingActive) return;
  logFile.close();
  loggingActive = false;
}

// ============================================================
// Calibration -- orchestrates Motor (via Serial2) and Encoder (via Serial1)
// ============================================================

// Runs the sliding-window stop-detector against whatever direction Motor is
// currently stepping in (Comms must have already sent CAL_START). Blocks
// until a stop is detected or timeout_ms elapses. On success, fills
// stopValue with the encoder count at the moment the stop was declared and
// returns true. Every sample seen is also logged to the SD card.
bool detectStop(int16_t &stopValue, unsigned long timeout_ms) {
  int16_t win[CAL_WIN];
  uint8_t head = 0, count = 0;
  int16_t startCount = 0, lastGood = 0;
  bool seeded = false, hasMoved = false;
  unsigned long t0 = millis();

  while (millis() - t0 < timeout_ms) {
    // Encoder and Motor links are independent hardware UARTs, so we can
    // freely check both every iteration with no listen()-switching needed.
    if (pollEncoder()) {
      int16_t v = latestEncoderCount;
      logSample(v);

      if (!seeded) { startCount = v; lastGood = v; seeded = true; }

      if (labs((long)v - lastGood) <= CAL_MAX_JUMP) {
        lastGood = v;
        if (labs((long)v - startCount) > CAL_MOVE_THRESHOLD) hasMoved = true;

        win[head] = v;
        head = (head + 1) % CAL_WIN;
        if (count < CAL_WIN) count++;

        if (count == CAL_WIN) {
          int16_t lo = win[0], hi = win[0];
          for (uint8_t i = 1; i < CAL_WIN; i++) {
            if (win[i] < lo) lo = win[i];
            if (win[i] > hi) hi = win[i];
          }
          if (hasMoved && (hi - lo) < CAL_SPAN_THRESHOLD) {
            stopValue = v;
            Serial.print("Hard stop detected at encoder count ");
            Serial.println(v);
            return true;
          }
        }
      }
    }
    serviceLiveUpdates(); // keep the laptop's telemetry/heartbeat alive during this blocking wait
  }
  return false;
}

void startMotorStepping(uint8_t direction, uint32_t intervalUs) {
  uint8_t payload[5];
  payload[0] = direction ? 1 : 0;
  writeU32LE(&payload[1], intervalUs);
  writeMotorFrame(CMD_CAL_START, payload, 5);
}

// A brief move AWAY from the stop, done before the real homing drive
// starts. Without this, if the valve happens to already be resting at or
// very near the stop (e.g. left over from a prior run that ended abnormally,
// or just wherever it was last positioned), the stall-detector's "has it
// actually moved" check has no room to trigger cleanly -- it would just sit
// there not-yet-moved until CAL_TIMEOUT_MS expires, rather than homing.
//
// This is timed, not step-counted: Motor free-runs continuously once
// started (the same CMD_CAL_START used everywhere else), so this just lets
// it run for roughly the time CAL_PRE_BACKOFF_STEPS would take at
// CAL_INTERVAL_US, then halts in place (0 backoff -- there's no stop to
// back off FROM here, this is just "stop where you are now"). Timing-based
// step counts are approximate (serial/loop overhead), which is fine for
// this purpose -- it only needs to create clearance, not hit an exact
// distance.
const uint32_t CAL_PRE_BACKOFF_STEPS = 100;

void preCalibrationBackoff() {
  Serial.println("Pre-calibration backoff: moving away from the stop before homing...");

  uint8_t awayDirection = (CAL_DIRECTION == HIGH) ? LOW : HIGH;
  startMotorStepping(awayDirection, CAL_INTERVAL_US);

  unsigned long moveTimeMs = (CAL_PRE_BACKOFF_STEPS * (unsigned long)CAL_INTERVAL_US * 2UL) / 1000UL;
  unsigned long t0 = millis();
  while (millis() - t0 < moveTimeMs) {
    pollEncoder();
    sendUdpTelemetry();
  }

  uint8_t stopPayload[4];
  writeU32LE(stopPayload, 0);
  writeMotorFrame(CMD_CAL_STOP, stopPayload, 4);
  waitForMotorAck(ACK_CAL_DONE, 2000);
}

// Two-point calibration: find the 20% (idle) stop driving CAL_DIRECTION,
// zero the encoder there, then drive the opposite way to the 100% (full
// open) stop and record the span. Returns true only if BOTH stops were
// found and Motor acked the final backoff -- manual throttle positioning
// requires both references to exist, so a partial/failed run leaves
// `calibrated` false and refuses to guess.
// Single-stop calibration: drive CAL_DIRECTION until the encoder stalls
// (the 20% idle stop), zero the encoder right there, back off so the
// driver isn't left straining against it, and stop. Unlike the old
// two-point version, there is no second phase and no far-end reference --
// only calStop1Count (always 0) is known after this. Returns true only if
// the stop was found and Motor acked the backoff.
bool calibrateThrottle() {
  calibrating_throttle = true;
  calibrated = false;
  Serial.println("Starting single-stop throttle calibration...");

  startLogging("CAL");

  preCalibrationBackoff();

  // Record the count just before the real homing drive, so we can tell
  // afterward which physical direction increases vs. decreases the count --
  // needed by getSeekDirections() for manual throttle / sequence seeking,
  // now that there's no second calibrated point to derive that sign from.
  int16_t beforeMove = latestEncoderCount;

  startMotorStepping(CAL_DIRECTION, CAL_INTERVAL_US);
  int16_t stopValue;
  if (!detectStop(stopValue, CAL_TIMEOUT_MS)) {
    Serial.println("ERROR: timeout finding 20% stop, sending ESTOP");
    writeMotorFrame(CMD_ESTOP, nullptr, 0);
    waitForMotorAck(ACK_ESTOPPED, 2000);
    sendTcpLine("ERROR_CALIBRATION_TIMEOUT_STOP1");
    stopLogging();
    calibrating_throttle = false;
    return false;
  }

  calDirectionIncreasesCount = (stopValue > beforeMove);

  // Zero right here, at the moment the stop is found -- this is "20%
  // throttle, encoder position set to 0" as specified. The backoff below
  // moves slightly away from this zero afterward, same as before.
  resetEncoderCount();

  // Back off the hard stop so the driver isn't left straining against it.
  uint8_t stopPayload[4];
  writeU32LE(stopPayload, CAL_BACKOFF_STEPS);
  writeMotorFrame(CMD_CAL_STOP, stopPayload, 4);
  bool ok = waitForMotorAck(ACK_CAL_DONE, 5000);

  stopLogging();

  if (!ok) {
    sendTcpLine("ERROR_CALIBRATION_MOTOR_NO_ACK");
    calibrating_throttle = false;
    return false;
  }

  calibrated = true;

  Serial.println("Calibration complete. Encoder zeroed at the 20% stop.");

  calibrating_throttle = false;
  return true;
}

// Figures out which physical direction increases vs. decreases the encoder
// count. This used to be derived from the sign of calStop2Count (the old
// two-point calibration's far reference); now that calibration only finds
// one stop, it's derived instead from calDirectionIncreasesCount, observed
// directly during that single homing pass.
void getSeekDirections(uint8_t &dirIncrease, uint8_t &dirDecrease) {
  uint8_t oppositeDirection = (CAL_DIRECTION == HIGH) ? LOW : HIGH;
  dirIncrease = calDirectionIncreasesCount ? CAL_DIRECTION : oppositeDirection;
  dirDecrease = calDirectionIncreasesCount ? oppositeDirection : CAL_DIRECTION;
}

// Only the home/zero stop is calibrated now -- there is no second stop, so
// there is no known upper boundary anymore. This can only guard against
// going PAST the home stop on the decreasing side (with a small margin for
// normal one-sample overshoot, same idea as before). There is deliberately
// NO upper-bound fault: without a second calibrated reference, the
// controller has no way to know where "too far" is in the increasing
// direction. If a hard limit on that side matters operationally, it needs
// a different source (a real physical stop, or a separately configured
// soft limit) -- not implemented here.
bool isWithinCalibratedRangeGuard(int16_t v) {
  return (v >= calStop1Count - CALIBRATED_RANGE_GUARD_COUNTS);
}

// Closed-loop seek to a target encoder count. Reuses the same CMD_CAL_START
// continuous-stepping command calibration uses -- Motor needs no new
// firmware for this. Steps at constant speed (stepIntervalUs) toward the
// target, switching direction only when needed, until within
// MANUAL_SEEK_TOLERANCE_COUNTS or timeout_ms elapses. Every sample is
// logged and forwarded live. Returns true if the target was reached.
//
// IMPORTANT: the tolerance-based arrival check is symmetric -- on its own
// it would accept resting up to MANUAL_SEEK_TOLERANCE_COUNTS on either
// side of the target as a valid "arrival". isWithinCalibratedRangeGuard()
// below only catches going too far PAST the home/zero stop on the
// decreasing side -- there is no equivalent check on the increasing side
// anymore, since single-stop calibration has no second reference to define
// where "too far" is over there.
//
// This does NOT eliminate overshoot -- Motor free-runs between ~20ms
// encoder samples with no deceleration, so a real excursion of up to about
// one sample's worth of travel (the same 15-20 counts MANUAL_SEEK_TOLERANCE_COUNTS
// was tuned against) is physically possible before this check can catch
// it. What changes is that it's now DETECTED and HALTED immediately rather
// than silently reported as success. If the actual physical stops aren't
// installed yet, there's currently nothing else physically preventing that
// momentary overshoot -- reducing stepIntervalUs (or decelerating) as the
// target approaches would shrink it further, but isn't implemented here.
//
// Checks ARM_PIN on every iteration (a single read, no debounce -- any
// glitch to LOW halts immediately, since stopping is always the safe
// choice).
bool seekToCount(int16_t targetCount, uint32_t stepIntervalUs, unsigned long timeout_ms) {
  uint8_t dirIncrease, dirDecrease;
  getSeekDirections(dirIncrease, dirDecrease);

  uint8_t currentDir = 0xFF; // sentinel so the first direction command always sends
  unsigned long t0 = millis();

  while (millis() - t0 < timeout_ms) {
    if (digitalRead(ARM_PIN) != HIGH) {
      Serial.println("ARM_PIN dropped LOW mid-seek -- halting");
      writeMotorFrame(CMD_ESTOP, nullptr, 0);
      waitForMotorAck(ACK_ESTOPPED, 2000);
      return false;
    }

    if (pollEncoder()) {
      int16_t v = latestEncoderCount;
      logSample(v);
      sendEncoderSample(v);

      int16_t diff = targetCount - v;

      // Arrival check comes before the hard range guard. This allows a tiny
      // normal overshoot at exactly 20% or 100% to count as reached instead
      // of falsely tripping ERROR_OUT_OF_RANGE.
      if (labs((long)diff) <= MANUAL_SEEK_TOLERANCE_COUNTS) {
        return true;
      }

      // Hard boundary guard. Only fault if the encoder is meaningfully beyond
      // the calibrated span, not merely a few counts past an end point.
      if (!isWithinCalibratedRangeGuard(v)) {
        Serial.print("ERROR: position "); Serial.print(v);
        Serial.println(" is beyond the calibrated 20%-100% guard range -- emergency stop");
        writeMotorFrame(CMD_ESTOP, nullptr, 0);
        waitForMotorAck(ACK_ESTOPPED, 2000);
        return false;
      }

      uint8_t desiredDir = (diff > 0) ? dirIncrease : dirDecrease;
      if (desiredDir != currentDir) {
        startMotorStepping(desiredDir, stepIntervalUs);
        currentDir = desiredDir;
      }
    }
    serviceLiveUpdates(); // keep the laptop's telemetry/heartbeat alive during this blocking wait
  }
  return false;
}

// ----------------------------
// Manual throttle command -- a direct one-shot target sent to Motor,
// bypassing the sequence upload/execute flow entirely.
// ----------------------------

void handleManualThrottleCommand() {
  float throttle_percent = 0.0f;

  unsigned long start_time = millis();
  const unsigned long timeout_ms = 1000;

  while (client.available() < (int)sizeof(float)) {
    if (millis() - start_time > timeout_ms) {
      Serial.println("ERROR: manual throttle value timeout");
      sendTcpLine("ERROR_MANUAL_THROTTLE_TIMEOUT");
      return;
    }

    // Keep telemetry and the arm indicator alive while waiting for the float payload to arrive.
    serviceLiveUpdates();
  }

  client.read((uint8_t*)&throttle_percent, sizeof(float));

  Serial.print("RX manual throttle float percent = ");
  Serial.println(throttle_percent, 4);

  if (!isfinite(throttle_percent)) {
    Serial.println("ERROR: manual throttle payload was not a finite float");
    sendTcpLine("ERROR_BAD_MANUAL_THROTTLE");
    return;
  }

  // Manual positioning is meaningless without a calibrated home reference --
  // seeking has no valid zero point otherwise, so refuse rather than move
  // somewhere undefined.
  if (!calibrated) {
    Serial.println("ERROR: manual throttle requested before calibration completed");
    sendTcpLine("ERROR_NOT_CALIBRATED");
    return;
  }

  // Manual throttle is percent from the GUI. Allow a tiny tolerance for
  // float/typing noise, then clamp to the allowed 20-100% travel range.
  if (throttle_percent < (MANUAL_THROTTLE_MIN_PERCENT - 0.1f) ||
      throttle_percent > (MANUAL_THROTTLE_MAX_PERCENT + 0.1f)) {
    Serial.print("ERROR: manual throttle percent out of range: ");
    Serial.println(throttle_percent, 4);
    sendTcpLine("ERROR_OUT_OF_RANGE");
    return;
  }

  throttle_percent = constrain(
    throttle_percent,
    MANUAL_THROTTLE_MIN_PERCENT,
    MANUAL_THROTTLE_MAX_PERCENT
  );

  // This was previously missing entirely -- manual throttle could move the
  // motor regardless of ARM_PIN state. Require the same debounced
  // confirmation sequences use before allowing any motion to start.
  if (!isConfirmedArmed()) {
    Serial.println("ERROR: manual throttle requested while disarmed");
    sendTcpLine("ERROR_NOT_ARMED");
    return;
  }

  // Convert GUI percent to encoder count using the low-stop-only calibration:
  //   20%  -> encoder 0
  //   100% -> encoder MANUAL_THROTTLE_LOW_TO_HIGH_COUNTS
  float normalised =
    (throttle_percent - MANUAL_THROTTLE_MIN_PERCENT) /
    (MANUAL_THROTTLE_MAX_PERCENT - MANUAL_THROTTLE_MIN_PERCENT);

  int16_t target_count = (int16_t)lround(
    normalised * (float)MANUAL_THROTTLE_LOW_TO_HIGH_COUNTS
  );

  // Clamp defensively to the configured software travel. This prevents a
  // valid 100.0% command from tripping a false range error because of float
  // rounding, while still keeping the target inside the intended span.
  target_count = constrain(
    target_count,
    calStop1Count,
    (int16_t)(calStop1Count + MANUAL_THROTTLE_LOW_TO_HIGH_COUNTS)
  );

  Serial.print("Manual throttle target: ");
  Serial.print(throttle_percent, 2);
  Serial.print("% -> encoder count ");
  Serial.println(target_count);

  manual_seek_active = true;
  startLogging("MAN");

  bool arrived = seekToCount(target_count, CAL_INTERVAL_US, MANUAL_SEEK_TIMEOUT_MS);

  bool outOfRangeMidSeek = (!arrived && !isWithinCalibratedRangeGuard(latestEncoderCount));
  bool disarmedMidSeek = (!arrived && !outOfRangeMidSeek && digitalRead(ARM_PIN) != HIGH);

  // Safety-net stop -- harmless if seekToCount() already halted things via
  // its own internal ESTOP (out-of-range / disarmed cases); ensures a
  // plain timeout (neither of those) still gets halted here.
  uint8_t stopPayload[4];
  writeU32LE(stopPayload, 0);
  writeMotorFrame(CMD_CAL_STOP, stopPayload, 4);
  waitForMotorAck(ACK_CAL_DONE, 2000);

  stopLogging();
  manual_seek_active = false;

  if (outOfRangeMidSeek) {
    Serial.println("ERROR: manual throttle exceeded the calibrated lower guard range");
    sendTcpLine("ERROR_OUT_OF_RANGE");
  } else if (disarmedMidSeek) {
    Serial.println("ERROR: disarmed mid-seek");
    sendTcpLine("ERROR_DISARMED_MID_SEQUENCE");
  } else if (arrived) {
    Serial.println("Manual throttle target reached");
    sendTcpLine("MANUAL_THROTTLE_DONE");
  } else {
    Serial.println("ERROR: manual throttle seek timed out");
    sendTcpLine("ERROR_MANUAL_THROTTLE_TIMEOUT");
  }
}

// ----------------------------
// TCP receive: converted sequence or command
//
// The whole sequence is read into sequence_buffer (safe on a Mega's 8KB
// SRAM), then executed locally via closed-loop seeking -- see
// executeSequence(). It is NOT forwarded to Motor for open-loop execution:
// the CSV's own step counts assume a generic geometry, not this unit's real
// calibrated span, so only commanded_throttle (converted through the real
// calibration) can be trusted.
// ----------------------------

void checkForTcpSequence() {
  EthernetClient new_client = server.available();

  if (new_client) {
    client = new_client;
    Serial.println("TCP client connected / data available");
  }

  if (client && client.available() >= sizeof(uint16_t)) {
    uint16_t to_read = 0;
    client.read((uint8_t*)&to_read, sizeof(uint16_t));

    if (to_read == TCP_PING_COMMAND) {
      Serial.println("Ping received from GUI");
      sendTcpLine("PONG");
      return;
    }

    if (to_read == TCP_CALIBRATE_COMMAND) {
      Serial.println("Calibration command received from GUI");
      sendTcpLine("CALIBRATING");
      bool ok = calibrateThrottle();
      if (ok) sendTcpLine("CALIBRATION_DONE");
      // On failure, the specific error line was already sent inside
      // calibrateThrottle() -- deliberately NOT sending CALIBRATION_DONE
      // here so the laptop can't mistake a failed run for success.
      return;
    }

    if (to_read == TCP_MANUAL_THROTTLE_COMMAND) {
      handleManualThrottleCommand();
      return;
    }

    Serial.print("Incoming sequence bytes: ");
    Serial.println(to_read);

    if (to_read == 0) {
      Serial.println("ERROR: zero-byte sequence");
      sendTcpLine("ERROR_ZERO_BYTE_SEQUENCE");
      client.stop();
      return;
    }

    if (to_read > sequence_buffer_max) {
      Serial.println("ERROR: sequence too large");
      sendTcpLine("ERROR_SEQUENCE_TOO_LARGE");
      client.stop();
      return;
    }

    if (to_read % STEP_COMMAND_SIZE != 0) {
      Serial.println("ERROR: bad converted sequence size");
      sendTcpLine("ERROR_BAD_SEQUENCE_SIZE");
      client.stop();
      return;
    }

    // Sequences are meaningless without calibration -- there's no fallback
    // interpretation of commanded_throttle without the real reference
    // points, so refuse up front rather than guess.
    if (!calibrated) {
      Serial.println("ERROR: sequence requested before calibration completed");
      sendTcpLine("ERROR_NOT_CALIBRATED");
      client.stop();
      return;
    }

    size_t total_read = 0;
    unsigned long start_time = millis();
    const unsigned long timeout_ms = 2000;

    while (total_read < to_read) {
      int got = client.read(sequence_buffer + total_read, to_read - total_read);
      if (got > 0) {
        total_read += got;
        start_time = millis();
      }
      serviceLiveUpdates();
      if (millis() - start_time > timeout_ms) {
        Serial.println("ERROR: TCP read timeout mid-sequence");
        sendTcpLine("ERROR_TCP_READ_TIMEOUT");
        client.stop();
        return;
      }
    }

    Serial.print("Command count: ");
    Serial.println(to_read / STEP_COMMAND_SIZE);

    executeSequence(to_read / STEP_COMMAND_SIZE);
  }
}

// ----------------------------
// Execute a received sequence by closed-loop seeking to each waypoint's
// commanded_throttle in turn. Positional accuracy takes priority over
// exact timing: each row's duration_ms is NOT enforced as a deadline (the
// seek runs until arrived or MANUAL_SEEK_TIMEOUT_MS elapses); its
// interval_us IS ... (12 KB left)