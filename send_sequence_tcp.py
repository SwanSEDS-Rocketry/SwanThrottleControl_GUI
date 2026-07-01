import socket
import struct
import csv

# Change these to match your setup
ARDUINO_IP = "192.168.10.2"   # Arduino Ethernet IP
PORT = 5000

CSV_FILE = "converted_sequence.csv"
SEQUENCE_BUFFER_CAPACITY = 512


def load_converted_sequence_csv(filename):
    commands = []

    with open(filename, newline="") as file:
        reader = csv.DictReader(file)

        for row in reader:
            try:
                duration_ms = int(float(row["duration_ms"]))
                steps = int(float(row["steps"]))
                direction = int(float(row["direction"]))
                interval_us = int(float(row["interval_us"]))
                commanded_throttle = float(row["commanded_throttle"])

            except KeyError as error:
                raise RuntimeError(f"Missing column in CSV: {error}")

            except ValueError:
                # Skip invalid rows
                continue

            if direction not in [0, 1]:
                raise RuntimeError(f"Invalid direction value: {direction}")

            if duration_ms < 0:
                raise RuntimeError(f"Invalid duration_ms value: {duration_ms}")

            if steps < 0:
                raise RuntimeError(f"Invalid steps value: {steps}")

            if interval_us < 0:
                raise RuntimeError(f"Invalid interval_us value: {interval_us}")

            commands.append({
                "duration_ms": duration_ms,
                "steps": steps,
                "direction": direction,
                "interval_us": interval_us,
                "commanded_throttle": commanded_throttle,
            })

    return commands


def build_payload(commands):
    payload = b""

    for command in commands:
        payload += struct.pack(
            "<IiBIf",
            command["duration_ms"],          # uint32_t
            command["steps"],                # int32_t
            command["direction"],            # uint8_t
            command["interval_us"],          # uint32_t
            command["commanded_throttle"],   # float
        )

    return payload


def send_sequence():
    commands = load_converted_sequence_csv(CSV_FILE)

    if len(commands) == 0:
        raise RuntimeError("No valid command points found in converted CSV file.")

    payload = build_payload(commands)

    if len(payload) > SEQUENCE_BUFFER_CAPACITY:
        raise RuntimeError(
            f"Sequence is too large: {len(payload)} bytes. "
            f"Arduino buffer is only {SEQUENCE_BUFFER_CAPACITY} bytes."
        )

    print(f"Loaded {len(commands)} converted command points")
    print(f"Payload size: {len(payload)} bytes")
    print(f"Bytes per command: {len(payload) // len(commands)}")
    print(f"Connecting to Arduino at {ARDUINO_IP}:{PORT}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
        tcp.settimeout(5)

        tcp.connect((ARDUINO_IP, PORT))
        print("Connected")

        # Arduino expects a uint16_t first, saying how many bytes are coming
        tcp.sendall(struct.pack("<H", len(payload)))

        # Then send the actual binary sequence data
        tcp.sendall(payload)

        print("Converted sequence sent successfully")

    print()
    print("Sent commands:")
    for i, command in enumerate(commands):
        print(
            f"{i}: "
            f"duration={command['duration_ms']} ms, "
            f"steps={command['steps']}, "
            f"dir={command['direction']}, "
            f"interval={command['interval_us']} us, "
            f"target={command['commanded_throttle']:.2f}"
        )


if __name__ == "__main__":
    send_sequence()