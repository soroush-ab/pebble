/*
 * pebble_gimbal - turns the BGC gimbal controller into Pebble's base actuator.
 *
 * Board: BGC v3.x "MOS" 2-axis brushless gimbal controller.
 *        ATmega328P @ 16 MHz, bootloader confirmed at 57600 baud
 *        (STK500v1 sync, signature 1e 95 0f).
 * Motor: AX-2206 80T, 11.3 ohm/phase gimbal motor, direct drive.
 * Sensor: AS5600 magnetic encoder on the board's own I2C bus.
 *
 * WHY THIS BOARD RUNS THE MOTOR AND NOT THE ESP32
 *
 * Commutation is a hard real-time job, exactly like step generation was. It
 * cannot cross Wi-Fi. This board already has the MCU wired to its own gate
 * drivers and MOSFETs by the manufacturer, so using it as shipped avoids
 * cutting traces and - more importantly - avoids any chance of driving both
 * MOSFETs in a leg at once and destroying the power stage. The ESP32 sends
 * setpoints; this sketch owns the trajectory and the field.
 *
 * ---------------------------------------------------------------------------
 * BEFORE YOU FLASH - three things to check
 *
 * 1. PIN MAPPING. 9/10/11 is the PITCH channel on Martinez-family boards, and
 *    is also the only 3-PWM set SimpleFOC supports on an ATmega328P (Timer1 +
 *    Timer2). Roll (3/5/6) would collide with the timers. VERIFY against your
 *    board before applying motor power - run with the motor unplugged first
 *    and confirm the three gate signals move.
 *
 * 2. POLE PAIRS. A 2206 is usually 7, but count: pole pairs = magnets / 2.
 *    Wrong value = the motor buzzes, jumps, or refuses to align.
 *
 * 3. VOLTAGE LIMIT. 11.3 ohm/phase is high, which is what makes this safe
 *    without current sensing: I = V / R. At 3 V that is 0.27 A and ~0.8 W,
 *    comfortable. Do not raise VOLTAGE_LIMIT far without checking motor
 *    temperature - there is no current loop to protect you.
 *
 * ---------------------------------------------------------------------------
 * SERIAL PROTOCOL (to the ESP32, 57600 8N1, newline-terminated ASCII)
 *
 *   in   T<deg>    target angle, degrees
 *        E<0|1>    disable / enable the motor
 *        V<volts>  voltage limit (torque limit)
 *        Z         make the current position the zero reference
 *        ?         request one status line now
 *
 *   out  A<angle> T<target> V<volts> E<en> R<ready>      ~20 Hz
 *        !<message>                                       errors / notices
 *
 * ASCII on purpose: you can drive and diagnose the whole base from a serial
 * terminal with no tooling, which matters a lot during bring-up.
 *
 * NOTE: pins 0/1 are this UART *and* the USB bootloader. Unplug USB when the
 * ESP32 is wired to them, or they will both drive RX.
 */

#include <SimpleFOC.h>

// ---- configuration ------------------------------------------------------
#define PIN_PWM_A       9        // PITCH channel on Martinez-family boards
#define PIN_PWM_B      10
#define PIN_PWM_C      11
#define PIN_ENABLE     -1        // these boards have no separate enable line

#define POLE_PAIRS      7        // AX-2206: verify = magnets / 2
#define SUPPLY_V       12.0f
#define VOLTAGE_LIMIT   3.0f     // 3 V / 11.3 ohm = 0.27 A
#define VELOCITY_LIMIT 90.0f     // deg/s, matches the old stepper budget

#define LINK_BAUD      57600     // same rate the bootloader uses
#define STATUS_HZ         20

BLDCMotor motor = BLDCMotor(POLE_PAIRS);
BLDCDriver3PWM driver = BLDCDriver3PWM(PIN_PWM_A, PIN_PWM_B, PIN_PWM_C, PIN_ENABLE);
MagneticSensorI2C sensor = MagneticSensorI2C(AS5600_I2C);

static float target_deg = 0.0f;
static float zero_off   = 0.0f;   // software zero, set by 'Z'
static bool  enabled    = false;
static bool  foc_ready  = false;
static uint32_t last_status = 0;
static char line[24];
static uint8_t line_n = 0;

static const float D2R = PI / 180.0f;
static const float R2D = 180.0f / PI;

static void apply_target()
{
    // SimpleFOC works in radians; the link speaks degrees because every other
    // part of Pebble does (motions.yaml, the twin, the protocol).
    motor.move((target_deg + zero_off) * D2R);
}

void setup()
{
    Serial.begin(LINK_BAUD);
    Serial.println(F("!pebble_gimbal booting"));

    Wire.begin();
    Wire.setClock(400000);
    sensor.init();
    motor.linkSensor(&sensor);

    driver.voltage_power_supply = SUPPLY_V;
    driver.voltage_limit = VOLTAGE_LIMIT;
    if (!driver.init()) {
        Serial.println(F("!driver init FAILED - check pins and supply"));
        return;
    }
    motor.linkDriver(&driver);

    motor.voltage_limit   = VOLTAGE_LIMIT;
    motor.velocity_limit  = VELOCITY_LIMIT * D2R;
    motor.controller      = MotionControlType::angle;
    motor.foc_modulation   = FOCModulationType::SpaceVectorPWM;

    // Conservative gains. A direct-drive base on a lazy susan has low friction
    // and low inertia, so high P just makes it buzz.
    motor.PID_velocity.P = 0.2f;
    motor.PID_velocity.I = 2.0f;
    motor.PID_velocity.D = 0.0f;
    motor.LPF_velocity.Tf = 0.02f;
    motor.P_angle.P = 12.0f;

    motor.init();
    if (!motor.initFOC()) {
        Serial.println(F("!initFOC FAILED - check POLE_PAIRS, encoder, wiring"));
        return;
    }
    foc_ready = true;
    motor.disable();                       // never start energised
    Serial.print(F("!ready zero_elec="));
    Serial.println(motor.zero_electric_angle, 3);
}

static void handle(char *s)
{
    switch (s[0]) {
    case 'T':
        target_deg = atof(s + 1);
        break;
    case 'E':
        enabled = (atoi(s + 1) != 0);
        if (!foc_ready) {
            Serial.println(F("!cannot enable: FOC not ready"));
            enabled = false;
        } else if (enabled) {
            motor.enable();
        } else {
            motor.disable();
        }
        break;
    case 'V': {
        float v = atof(s + 1);
        if (v > 0.1f && v <= SUPPLY_V * 0.5f) {   // refuse to cook the motor
            motor.voltage_limit = v;
            driver.voltage_limit = v;
        } else {
            Serial.println(F("!voltage out of range"));
        }
        break;
    }
    case 'Z':
        zero_off = -(sensor.getAngle() * R2D) + target_deg;
        Serial.println(F("!zeroed"));
        break;
    case '?':
        last_status = 0;                   // force one out immediately
        break;
    default:
        break;
    }
}

void loop()
{
    if (foc_ready) {
        motor.loopFOC();                   // must run as fast as possible
        apply_target();
    }

    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n' || c == '\r') {
            if (line_n) { line[line_n] = 0; handle(line); line_n = 0; }
        } else if (line_n < sizeof(line) - 1) {
            line[line_n++] = c;
        }
    }

    const uint32_t now = millis();
    if (now - last_status >= (1000 / STATUS_HZ)) {
        last_status = now;
        Serial.print('A');
        Serial.print(foc_ready ? (sensor.getAngle() * R2D - zero_off) : 0.0f, 2);
        Serial.print(F(" T")); Serial.print(target_deg, 2);
        Serial.print(F(" V")); Serial.print(motor.voltage_limit, 2);
        Serial.print(F(" E")); Serial.print(enabled ? 1 : 0);
        Serial.print(F(" R")); Serial.println(foc_ready ? 1 : 0);
    }
}
