// Pin definitions

int STEP_PIN = 9;
int DIR_PIN  = 10;
int ENA_PIN  = 11;

int ENC_A = 19;
int ENC_AN = 20;
int ENC_B = 21;
int ENC_BN = 22;


// Import csv targetdata and extract to arrays throttle and time| TO BE DONE |
float targetdata[] = {};
float throttledata[] = {0,.25,.25,.50,.50,.75,.75,1,1};
float timedata[] = {0,250,1750,2000,3500,3750,5250,5500,7000};

float GearboxRatio = 13.73;
int StepsDiv = 800;

int current_point = 0;      // current list element     Declaring Variables
float current_throttle = 0;   // current throttle out of 200
float commanded_throttle = 0;
int time_step = 0;
float delta_throttle = 0;
float current_h = 0;
float commanded_h = 0;
int Direction = 0;
int no_points = 0;
long no_steps = 0;
long encoderPosition = 0;
float delta_h = 0.0;
float stepRate = 0.0;
unsigned long interval = 0;


void setup()
{
  Serial.begin(115200);
  delay(1000);

  pinMode(STEP_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(ENA_PIN, OUTPUT);

  digitalWrite(ENA_PIN, LOW);

  delay(1000);

  //pinMode(ENC_A, INPUT_PULLUP);
  //pinMode(ENC_B, INPUT_PULLUP);

  no_points = sizeof(throttledata) / sizeof(throttledata[0]);

}




void loop()
{
  if (current_point < no_points)
  {
  Serial.println("new point");
  
    commanded_throttle = throttledata[current_point];
    if (current_point == 0)
      {
        time_step = (timedata[0]);
        current_throttle = (throttledata[0]);
      }
    else
    {
        time_step = (timedata[current_point] - timedata[(current_point-1)]);
        current_throttle = throttledata[(current_point-1)];
    }


    Serial.print("time_step = ");
    Serial.println(time_step);

    CalcSteps(commanded_throttle, current_throttle);

    DirectionCalc(commanded_throttle, current_throttle);

    executeSteps(no_steps, Direction, time_step);

    updateposition();

    printStatus();

    current_point++;
    Serial.print("current point =");
    Serial.println(current_point);
  }
  
}


int DirectionCalc(int commanded_throttle, int current_throttle)
{
  delta_throttle = (commanded_throttle - current_throttle);

  if (delta_throttle >= 0)
    Direction = 1;
  else if (delta_throttle < 0)
    Direction = 0;

  return Direction;
}

long CalcSteps(float commanded_throttle, float current_throttle)
{
  Serial.print("current throttle =");
  Serial.println(current_throttle);
  Serial.print("commanded throttle =");
  Serial.println(commanded_throttle);
  current_h = -0.000351114 * current_throttle * current_throttle * current_throttle * current_throttle
          + 0.00101334 * current_throttle * current_throttle * current_throttle
          - 0.000935964 * current_throttle * current_throttle
          + 0.00077505 * current_throttle;
  Serial.print("current h =");
  Serial.println(current_h, 8);
  commanded_h = (-0.000351114 * commanded_throttle * commanded_throttle * commanded_throttle * commanded_throttle)
            + (0.00101334 * commanded_throttle * commanded_throttle * commanded_throttle)
            - (0.000935964 * commanded_throttle * commanded_throttle)
            + (0.00077505 * commanded_throttle);
  Serial.print("commanded h = ");
  Serial.println(commanded_h, 8);
  delta_h = (current_h - commanded_h);
  Serial.print("delta_h =");
  Serial.println(delta_h, 8);

  no_steps = ((delta_h * GearboxRatio * StepsDiv) / (1.0 / 1000.0));

  Serial.print("no_steps =");
  Serial.println(no_steps, 8);

  return no_steps;
}

void executeSteps(long no_steps, int Direction, int time_step)
{
  if (no_steps < 5 and no_steps > -5)
  {
    delay(time_step);
    Serial.println("waiting");
  }
  else
    stepRate = (no_steps / (time_step));

    interval = (1000 / stepRate);

    for (int i = 0; i < no_steps; i++)
    {
     digitalWrite(DIR_PIN, Direction);

     digitalWrite(STEP_PIN, HIGH);
     delayMicroseconds(interval / 2);

     digitalWrite(STEP_PIN, LOW);
     delayMicroseconds(interval / 2);
    }
}

void updateposition()
{
  current_throttle = (commanded_throttle);
}

void printStatus()
{
  //Serial.print("Target: ");
  //Serial.print(commanded_throttle);

  //Serial.print(" Actual: ");
  //Serial.print(encoderPosition);

  //Serial.print(" Error: ");
  //Serial.println(commanded_throttle - encoderPosition);
  
  Serial.print(timedata[current_point]);
  Serial.print(",");
  Serial.println(current_throttle);
}

