void setup() {
  pinMode(13, OUTPUT);
}

void loop() {
  digitalWrite(13, HIGH);
  delay(100);
  //delayMicroseconds(10);
  digitalWrite(13, LOW);
  delay(100);
  //delayMicroseconds(10);
}

