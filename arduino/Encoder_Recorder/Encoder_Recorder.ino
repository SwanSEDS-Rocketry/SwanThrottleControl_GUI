#include <SoftwareSerial.h>

const int inputPin = 10; // Signal pin
int previousState = LOW;
int count = 0;
int currentState = 0;

SoftwareSerial uart(2, 3); // RX, TX

void setup() {
  pinMode(inputPin, INPUT);
  Serial.begin(115200);
  uart.begin(115200);

  uart.write(0xFF);
  sendInt(5678);
  sendInt(8191);
  sendInt(-1);
}

void sendInt(int16_t value) {
  // only use the least significant 7 bits
  // use the most significant bit in each byte to number them
  uint8_t sendBuffer[] = {
    value & 0x007F,
    ((value >> 7) & 0x007F) | 0x80
  };

  uart.write(sendBuffer, sizeof(sendBuffer));
}

void loop() {
  /*
  int currentState = digitalRead(inputPin);

  if (currentState != previousState) {
    count = count + 1;
    if (count % 10 == 0) {
      sendInt(count);
      Serial.println(count);
    }
  }

  if (count > 1990) {
    count = 0;
  }
  
  previousState = currentState;*/
}

