import csv
import sys
from pathlib import Path


# ----------------------------
# Settings from your Arduino code
# ----------------------------

GEARBOX_RATIO = 13.73
STEPS_DIV = 800

# Your Arduino code divides by 1.0 / 1000.0
# This means h is effectively being converted from metres to mm.
H_SCALE = 1.0 / 1000.0

DEFAULT_INPUT_CSV = "test_sequence.csv"
OUTPUT_CSV = "converted_sequence.csv"


# ----------------------------
# Throttle to h equation
# ----------------------------

def throttle_to_h(throttle: float) -> float:
    """
    Same polynomial as your Arduino CalcSteps() function.
    Input throttle should be 0.0 to 1.0.
    Output h is in metres, based on your current equation.
    """
    h = (
        -0.000351114 * throttle**4
        + 0.00101334 * throttle**3
        - 0.000935964 * throttle**2
        + 0.00077505 * throttle
    )
    return h


def calculate_step_command(current_throttle: float, commanded_throttle: float, duration_ms: int):
    """
    Converts one throttle movement into stepper command data.
    """

    current_h = throttle_to_h(current_throttle)
    commanded_h = throttle_to_h(commanded_throttle)

    delta_h = current_h - commanded_h

    raw_steps = (delta_h * GEARBOX_RATIO * STEPS_DIV) / H_SCALE

    # Direction from throttle change, same idea as your Arduino code.
    # If this moves the motor the wrong way, swap these two values.
    if commanded_throttle >= current_throttle:
        direction = 1
    else:
        direction = 0

    # Step count must always be positive for the Arduino for-loop.
    steps = abs(round(raw_steps))

    if steps < 5 or duration_ms <= 0:
        interval_us = 0
        step_rate_steps_per_s = 0
    else:
        duration_s = duration_ms / 1000.0
        step_rate_steps_per_s = steps / duration_s
        interval_us = round(1_000_000 / step_rate_steps_per_s)

    return {
        "current_throttle": current_throttle,
        "commanded_throttle": commanded_throttle,
        "duration_ms": duration_ms,
        "current_h": current_h,
        "commanded_h": commanded_h,
        "delta_h": delta_h,
        "raw_steps": raw_steps,
        "steps": steps,
        "direction": direction,
        "step_rate_steps_per_s": step_rate_steps_per_s,
        "interval_us": interval_us,
    }


def read_throttle_csv(filename: Path):
    """
    Reads a CSV with:
        time_ms, throttle

    Example:
        time_ms,throttle
        0,0
        250,0.25
        1750,0.25
        2000,0.5

    Header row is optional.
    """

    points = []

    with open(filename, newline="") as file:
        reader = csv.reader(file)

        for row_number, row in enumerate(reader, start=1):
            if not row:
                continue

            if len(row) < 2:
                print(f"Skipping row {row_number}: not enough columns")
                continue

            try:
                time_ms = int(float(row[0]))
                throttle = float(row[1])
            except ValueError:
                # Allows header rows like: time_ms,throttle
                continue

            if time_ms < 0:
                raise ValueError(f"Invalid negative time at row {row_number}: {time_ms}")

            if throttle < 0.0 or throttle > 1.0:
                raise ValueError(
                    f"Invalid throttle at row {row_number}: {throttle}. "
                    "Throttle should be between 0.0 and 1.0."
                )

            points.append((time_ms, throttle))

    return points


def convert_sequence(points):
    converted = []

    for i, (time_ms, commanded_throttle) in enumerate(points):
        if i == 0:
            previous_time_ms = 0
            current_throttle = commanded_throttle
            duration_ms = time_ms
        else:
            previous_time_ms, current_throttle = points[i - 1]
            duration_ms = time_ms - previous_time_ms

        if duration_ms < 0:
            raise ValueError(f"Time goes backwards at point {i}: {time_ms} ms")

        command = calculate_step_command(
            current_throttle=current_throttle,
            commanded_throttle=commanded_throttle,
            duration_ms=duration_ms,
        )

        command["point"] = i
        command["time_ms"] = time_ms

        converted.append(command)

    return converted


def write_converted_csv(filename: Path, converted):
    fieldnames = [
        "point",
        "time_ms",
        "duration_ms",
        "current_throttle",
        "commanded_throttle",
        "current_h",
        "commanded_h",
        "delta_h",
        "raw_steps",
        "steps",
        "direction",
        "step_rate_steps_per_s",
        "interval_us",
    ]

    with open(filename, "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()

        for row in converted:
            writer.writerow(row)


def create_default_test_sequence(input_path: Path):
    print(f"{input_path.name} not found.")
    print("Creating default test_sequence.csv from hardcoded Arduino-style data.")

    default_points = [
        (0, 0.0),
        (250, 0.25),
        (1750, 0.25),
        (2000, 0.50),
        (3500, 0.50),
        (3750, 0.75),
        (5250, 0.75),
        (5500, 1.00),
        (7000, 1.00),
    ]

    with open(input_path, "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["time_ms", "throttle"])
        writer.writerows(default_points)


def get_input_path(script_dir: Path):
    """
    If the GUI passes a selected file path, use that.
    Otherwise, fall back to test_sequence.csv.
    """

    if len(sys.argv) > 1:
        return Path(sys.argv[1])

    return script_dir / DEFAULT_INPUT_CSV


def main():
    script_dir = Path(__file__).parent

    input_path = get_input_path(script_dir)

    # Always write converted_sequence.csv next to this script,
    # regardless of where the selected input file came from.
    output_path = script_dir / OUTPUT_CSV

    if not input_path.exists():
        # Only auto-create a default test file when using the fallback input.
        if input_path.name == DEFAULT_INPUT_CSV:
            create_default_test_sequence(input_path)
        else:
            raise FileNotFoundError(f"Selected input CSV not found: {input_path}")

    points = read_throttle_csv(input_path)

    if not points:
        raise RuntimeError(f"No valid points found in input CSV: {input_path}")

    converted = convert_sequence(points)
    write_converted_csv(output_path, converted)

    print(f"Read {len(points)} throttle points from {input_path}")
    print(f"Wrote converted step commands to {output_path}")
    print()

    for row in converted:
        print(
            f"Point {row['point']}: "
            f"t={row['time_ms']} ms, "
            f"duration={row['duration_ms']} ms, "
            f"target={row['commanded_throttle']:.2f}, "
            f"dir={row['direction']}, "
            f"steps={row['steps']}, "
            f"interval={row['interval_us']} us"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Conversion failed: {error}", file=sys.stderr)
        sys.exit(1)