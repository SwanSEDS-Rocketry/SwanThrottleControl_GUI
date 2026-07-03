import csv
import socket
import struct
import subprocess
import sys

from pathlib import Path

import pyqtgraph as pg

from PySide6.QtCore import QObject, QTimer, QElapsedTimer
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QPushButton,
    QFileDialog,
    QApplication,
    QVBoxLayout,
    QFrame,
    QTextEdit,
    QSlider,
    QLCDNumber,
)
from PySide6.QtNetwork import QTcpSocket, QAbstractSocket


TCP_PING_COMMAND = 0xFFFF
TCP_CALIBRATE_COMMAND = 0xCA1B
TCP_MANUAL_THROTTLE_COMMAND = 0x4D54  # 'MT'

UDP_TELEMETRY_PORT = 7080

# Voltage scaling.
# These multiply the Arduino analogue pin voltage.
# If the Arduino analogue pin receives direct 0-5 V, leave as 1.0.
# Later, if using resistor dividers, set these to the divider ratios.
VOLT_5_SCALE = 1.0
VOLT_24_SCALE = 1.0
VOLT_48_SCALE = 1.0

# Manual throttle safety limits.
# Slider uses tenths of a percent, so:
# 20.0% = 200
# 100.0% = 1000
MANUAL_THROTTLE_MIN_PERCENT = 20.0
MANUAL_THROTTLE_MAX_PERCENT = 100.0


class ArduinoConnection(QObject):
    def __init__(self, window):
        super().__init__()

        self.window = window
        self.socket = QTcpSocket(self)

        self.sequence_buffer_capacity = 512

        self.ip_edit = window.findChild(QLineEdit, "IPEdit")
        self.port_edit = window.findChild(QLineEdit, "portEdit")
        self.connect_button = window.findChild(QPushButton, "connectButton")
        self.ping_button = window.findChild(QPushButton, "PingButton")
        self.calibrate_button = window.findChild(QPushButton, "calibrateButton")
        self.status_label = window.findChild(QLabel, "ConnectionLabel")

        self.select_sequence_button = window.findChild(QPushButton, "selectSequenceButton")
        self.sequence_name_label = window.findChild(QLabel, "sequenceName")
        self.selected_sequence_file = None

        self.sequence_times_ms = []
        self.sequence_duration_ms = 0

        self.sequence_playhead_timer = QTimer(self)
        self.sequence_playhead_timer.timeout.connect(self.update_sequence_playhead)

        self.sequence_elapsed_timer = QElapsedTimer()
        self.sequence_playhead_running = False

        self.manual_throttle_slider = window.findChild(QSlider, "manualThrottleSlider")
        self.manual_throttle_text = window.findChild(QTextEdit, "manualThrottleText")
        self.throttle_set_button = window.findChild(QPushButton, "throttleSetButton")

        self.updating_manual_throttle_ui = False
        self.manual_throttle_percent = MANUAL_THROTTLE_MIN_PERCENT

        self.upload_sequence_button = window.findChild(QPushButton, "uploadSeqButton")

        self.sequence_graph_frame = window.findChild(QFrame, "sequenceGraphFrame")

        # Optional telemetry display widgets.
        self.encoder_position_label = window.findChild(QLabel, "encoderPositionLabel")

        # These are QLCDNumber widgets in your GUI.
        self.volt_5 = window.findChild(QLCDNumber, "volt_5")
        self.volt_24 = window.findChild(QLCDNumber, "volt_24")
        self.volt_48 = window.findChild(QLCDNumber, "volt_48")
        self.lcd_throttle_actual = window.findChild(QLCDNumber, "lcdThrottleActual")

        self.latest_telemetry = None
        self.telemetry_print_timer = QElapsedTimer()
        self.telemetry_print_timer.start()

        self.check_widgets_exist()
        self.setup_defaults()
        self.setup_socket()
        self.setup_buttons()
        self.setup_sequence_graph()
        self.setup_manual_throttle_controls()
        self.setup_voltage_lcds()
        self.setup_udp_telemetry()

    def set_status(self, text):
        self.status_label.setText(text)
        QApplication.processEvents()
        print(text)

    def check_widgets_exist(self):
        missing = []

        if self.ip_edit is None:
            missing.append("IPEdit")
        if self.port_edit is None:
            missing.append("portEdit")
        if self.connect_button is None:
            missing.append("connectButton")
        if self.ping_button is None:
            missing.append("PingButton")
        if self.calibrate_button is None:
            missing.append("calibrateButton")
        if self.status_label is None:
            missing.append("ConnectionLabel")
        if self.select_sequence_button is None:
            missing.append("selectSequenceButton")
        if self.sequence_name_label is None:
            missing.append("sequenceName")
        if self.upload_sequence_button is None:
            missing.append("uploadSeqButton")
        if self.sequence_graph_frame is None:
            missing.append("sequenceGraphFrame")
        if self.manual_throttle_slider is None:
            missing.append("manualThrottleSlider")
        if self.manual_throttle_text is None:
            missing.append("manualThrottleText")
        if self.throttle_set_button is None:
            missing.append("throttleSetButton")

        # These are optional for now so the app does not crash if the names
        # are slightly different in Qt Designer.
        if self.volt_5 is None:
            print("Warning: Could not find QLCDNumber volt_5")
        if self.volt_24 is None:
            print("Warning: Could not find QLCDNumber volt_24")
        if self.volt_48 is None:
            print("Warning: Could not find QLCDNumber volt_48")

        if missing:
            raise RuntimeError(
                f"Could not find these widgets in main.ui: {', '.join(missing)}"
            )

    def setup_defaults(self):
        self.ip_edit.setText("192.168.10.2")
        self.port_edit.setText("5000")
        self.set_status("Disconnected")
        self.connect_button.setText("Connect")
        self.ping_button.setEnabled(False)
        self.calibrate_button.setEnabled(False)
        self.sequence_name_label.setText("No Sequence Loaded")

    def setup_socket(self):
        self.socket.connected.connect(self.on_connected)
        self.socket.disconnected.connect(self.on_disconnected)
        self.socket.errorOccurred.connect(self.on_error)
        self.socket.readyRead.connect(self.on_ready_read)

    def setup_buttons(self):
        self.connect_button.clicked.connect(self.connect_or_disconnect)
        self.ping_button.clicked.connect(self.ping_arduino)
        self.calibrate_button.clicked.connect(self.calibrate_arduino)
        self.select_sequence_button.clicked.connect(self.select_throttle_sequence)
        self.upload_sequence_button.clicked.connect(self.upload_throttle_sequence)
        self.throttle_set_button.clicked.connect(self.set_manual_throttle)

    def setup_voltage_lcds(self):
        for lcd in (self.volt_5, self.volt_24, self.volt_48):
            if lcd is not None:
                lcd.setDigitCount(6)
                lcd.display(0.0)

    def setup_udp_telemetry(self):
        try:
            self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp_socket.bind(("0.0.0.0", UDP_TELEMETRY_PORT))
            self.udp_socket.setblocking(False)

            self.udp_timer = QTimer(self)
            self.udp_timer.timeout.connect(self.read_udp_telemetry)
            self.udp_timer.start(50)

            print(f"UDP telemetry listening on port {UDP_TELEMETRY_PORT}")

        except OSError as error:
            self.udp_socket = None
            self.udp_timer = None
            print(f"Failed to start UDP telemetry listener: {error}")

    def read_udp_telemetry(self):
        if self.udp_socket is None:
            return

        while True:
            try:
                data, addr = self.udp_socket.recvfrom(1024)
            except BlockingIOError:
                break
            except OSError as error:
                print(f"UDP telemetry read error: {error}")
                break

            telemetry = self.parse_udp_packet(data)

            if telemetry is None:
                continue

            self.latest_telemetry = telemetry
            self.update_telemetry_display(telemetry)

            # Print once per second so the terminal does not get spammed.
            if self.telemetry_print_timer.elapsed() >= 1000:
                self.telemetry_print_timer.restart()

                if telemetry["type"] == "telemetry":
                    print(
                        "UDP telemetry: "
                        f"encoder={telemetry['encoder_count']}, "
                        f"V5={telemetry['volt_5']:.2f}, "
                        f"V24={telemetry['volt_24']:.2f}, "
                        f"V48={telemetry['volt_48']:.2f}, "
                        f"raw=({telemetry['voltage1_raw']}, "
                        f"{telemetry['voltage2_raw']}, "
                        f"{telemetry['voltage3_raw']})"
                    )
                elif telemetry["type"] == "encoder_sample":
                    print(
                        "UDP encoder sample: "
                        f"encoder={telemetry['encoder_count']}"
                    )

    def parse_udp_packet(self, data):
        if len(data) < 1:
            return None

        packet_type = data[0:1]

        # New 14-byte telemetry packet:
        # Byte 0      = 'T'
        # Bytes 1-4   = millis uint32
        # Bytes 5-6   = encoder int16
        # Byte 7      = flags
        # Bytes 8-9   = voltage 1 raw uint16, mapped to volt_5
        # Bytes 10-11 = voltage 2 raw uint16, mapped to volt_24
        # Bytes 12-13 = voltage 3 raw uint16, mapped to volt_48
        if packet_type == b"T":
            if len(data) == 14:
                time_ms = struct.unpack_from("<I", data, 1)[0]
                encoder_count = struct.unpack_from("<h", data, 5)[0]

                flags = data[7]
                encoder_seen = bool(flags & 0x01)
                sequence_running = bool(flags & 0x02)
                calibrating = bool(flags & 0x04)

                voltage1_raw = struct.unpack_from("<H", data, 8)[0]
                voltage2_raw = struct.unpack_from("<H", data, 10)[0]
                voltage3_raw = struct.unpack_from("<H", data, 12)[0]

                # Arduino analogue pin voltage, before external scaling.
                voltage1_pin = voltage1_raw * 5.0 / 1023.0
                voltage2_pin = voltage2_raw * 5.0 / 1023.0
                voltage3_pin = voltage3_raw * 5.0 / 1023.0

                # Real displayed voltages after applying scale factors.
                volt_5_value = voltage1_pin * VOLT_5_SCALE
                volt_24_value = voltage2_pin * VOLT_24_SCALE
                volt_48_value = voltage3_pin * VOLT_48_SCALE

                return {
                    "type": "telemetry",
                    "time_ms": time_ms,
                    "encoder_count": encoder_count,
                    "encoder_seen": encoder_seen,
                    "sequence_running": sequence_running,
                    "calibrating": calibrating,
                    "voltage1_raw": voltage1_raw,
                    "voltage2_raw": voltage2_raw,
                    "voltage3_raw": voltage3_raw,
                    "voltage1_pin": voltage1_pin,
                    "voltage2_pin": voltage2_pin,
                    "voltage3_pin": voltage3_pin,
                    "volt_5": volt_5_value,
                    "volt_24": volt_24_value,
                    "volt_48": volt_48_value,
                }

            # Old 8-byte telemetry packet compatibility.
            if len(data) == 8:
                time_ms = struct.unpack_from("<I", data, 1)[0]
                encoder_count = struct.unpack_from("<h", data, 5)[0]
                encoder_seen = bool(data[7])

                return {
                    "type": "telemetry",
                    "time_ms": time_ms,
                    "encoder_count": encoder_count,
                    "encoder_seen": encoder_seen,
                    "sequence_running": False,
                    "calibrating": False,
                    "voltage1_raw": None,
                    "voltage2_raw": None,
                    "voltage3_raw": None,
                    "voltage1_pin": None,
                    "voltage2_pin": None,
                    "voltage3_pin": None,
                    "volt_5": None,
                    "volt_24": None,
                    "volt_48": None,
                }

            return None

        # Encoder sample packet sent during sequences.
        # Byte 0    = 'E'
        # Bytes 1-4 = millis uint32
        # Bytes 5-6 = encoder int16
        if packet_type == b"E" and len(data) >= 7:
            time_ms = struct.unpack_from("<I", data, 1)[0]
            encoder_count = struct.unpack_from("<h", data, 5)[0]

            return {
                "type": "encoder_sample",
                "time_ms": time_ms,
                "encoder_count": encoder_count,
            }

        return None

    def update_telemetry_display(self, telemetry):
        if "encoder_count" in telemetry and self.encoder_position_label is not None:
            self.encoder_position_label.setText(str(telemetry["encoder_count"]))

        # Display encoder data on the LCD widget
        if "encoder_count" in telemetry and self.lcd_throttle_actual is not None:
            self.lcd_throttle_actual.display(telemetry["encoder_count"] / 100)

        if telemetry["type"] != "telemetry":
            return

        if telemetry["volt_5"] is not None and self.volt_5 is not None:
            self.volt_5.display(round(telemetry["volt_5"], 2))

        if telemetry["volt_24"] is not None and self.volt_24 is not None:
            self.volt_24.display(round(telemetry["volt_24"], 2))

        if telemetry["volt_48"] is not None and self.volt_48 is not None:
            self.volt_48.display(round(telemetry["volt_48"], 2))

    def setup_manual_throttle_controls(self):
        slider_min = int(round(MANUAL_THROTTLE_MIN_PERCENT * 10.0))
        slider_max = int(round(MANUAL_THROTTLE_MAX_PERCENT * 10.0))

        self.manual_throttle_slider.setMinimum(slider_min)
        self.manual_throttle_slider.setMaximum(slider_max)

        self.updating_manual_throttle_ui = True
        self.manual_throttle_slider.setValue(slider_min)
        self.manual_throttle_text.setPlainText(f"{MANUAL_THROTTLE_MIN_PERCENT:.1f}")
        self.manual_throttle_percent = MANUAL_THROTTLE_MIN_PERCENT
        self.updating_manual_throttle_ui = False

        self.manual_throttle_slider.valueChanged.connect(
            self.manual_throttle_slider_changed
        )
        self.manual_throttle_text.textChanged.connect(
            self.manual_throttle_text_changed
        )

    def manual_throttle_slider_changed(self, value):
        if self.updating_manual_throttle_ui:
            return

        throttle_percent = value / 10.0
        throttle_percent = max(
            MANUAL_THROTTLE_MIN_PERCENT,
            min(MANUAL_THROTTLE_MAX_PERCENT, throttle_percent)
        )

        self.manual_throttle_percent = throttle_percent

        self.updating_manual_throttle_ui = True
        self.manual_throttle_text.setPlainText(f"{throttle_percent:.1f}")
        self.updating_manual_throttle_ui = False

        self.set_status(f"Manual throttle target: {throttle_percent:.1f}%")

    def manual_throttle_text_changed(self):
        if self.updating_manual_throttle_ui:
            return

        text = self.manual_throttle_text.toPlainText().strip()

        if text.endswith("%"):
            text = text[:-1].strip()

        try:
            throttle_percent = float(text)
        except ValueError:
            return

        throttle_percent = max(
            MANUAL_THROTTLE_MIN_PERCENT,
            min(MANUAL_THROTTLE_MAX_PERCENT, throttle_percent)
        )

        self.manual_throttle_percent = throttle_percent

        slider_value = int(round(throttle_percent * 10.0))

        self.updating_manual_throttle_ui = True
        self.manual_throttle_slider.setValue(slider_value)
        self.updating_manual_throttle_ui = False

        self.set_status(f"Manual throttle target: {throttle_percent:.1f}%")

    def set_manual_throttle(self):
        self.send_manual_throttle(self.manual_throttle_percent)

    def send_manual_throttle(self, throttle_percent):
        throttle_percent = max(
            MANUAL_THROTTLE_MIN_PERCENT,
            min(MANUAL_THROTTLE_MAX_PERCENT, float(throttle_percent))
        )

        if self.socket.state() != QAbstractSocket.ConnectedState:
            self.set_status("Not connected")
            print("Cannot send manual throttle: not connected")
            return

        self.socket.write(struct.pack("<H", TCP_MANUAL_THROTTLE_COMMAND))
        self.socket.write(struct.pack("<f", throttle_percent))
        self.socket.flush()

        self.set_status(f"Manual throttle sent: {throttle_percent:.1f}%")
        print(f"Manual throttle sent: {throttle_percent:.1f}%")

    def setup_sequence_graph(self):
        self.sequence_plot = pg.PlotWidget()

        self.sequence_plot.setBackground("w")
        self.sequence_plot.showGrid(x=True, y=True)

        self.sequence_plot.setTitle("Throttle Sequence")
        self.sequence_plot.setLabel("bottom", "Time", units="ms")
        self.sequence_plot.setLabel("left", "Throttle", units="%")

        self.sequence_plot.setYRange(0, 100)

        self.sequence_curve = self.sequence_plot.plot(
            [],
            [],
            pen=pg.mkPen(width=2),
            symbol="o",
            symbolSize=6,
        )

        self.sequence_playhead_line = pg.InfiniteLine(
            pos=0,
            angle=90,
            movable=False,
            pen=pg.mkPen(width=2),
        )

        self.sequence_plot.addItem(self.sequence_playhead_line)
        self.sequence_playhead_line.setVisible(False)

        layout = QVBoxLayout(self.sequence_graph_frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.sequence_plot)

    def update_sequence_graph(self, filename):
        times_ms = []
        throttles_percent = []

        try:
            with open(filename, newline="") as file:
                reader = csv.reader(file)

                for row in reader:
                    if not row or len(row) < 2:
                        continue

                    try:
                        time_ms = float(row[0])
                        throttle = float(row[1])
                    except ValueError:
                        continue

                    times_ms.append(time_ms)
                    throttles_percent.append(throttle * 100.0)

            if not times_ms:
                self.set_status("No valid graph data found")
                return

            self.sequence_curve.setData(times_ms, throttles_percent)

            self.sequence_times_ms = times_ms
            self.sequence_duration_ms = max(times_ms)

            self.sequence_playhead_line.setValue(min(times_ms))
            self.sequence_playhead_line.setVisible(True)

            self.sequence_plot.setYRange(0, 100)

            if min(times_ms) != max(times_ms):
                self.sequence_plot.setXRange(min(times_ms), max(times_ms))

            self.set_status("Throttle sequence graphed")

        except Exception as error:
            self.set_status("Graph update failed")
            print(f"Graph update failed: {error}")

    def start_sequence_playhead(self):
        if self.sequence_duration_ms <= 0:
            self.set_status("Cannot animate sequence: invalid duration")
            return

        self.sequence_playhead_line.setVisible(True)
        self.sequence_playhead_line.setValue(0)

        self.sequence_elapsed_timer.restart()
        self.sequence_playhead_running = True

        self.sequence_playhead_timer.start(50)

        print("Sequence playhead started")

    def update_sequence_playhead(self):
        if not self.sequence_playhead_running:
            return

        elapsed_ms = self.sequence_elapsed_timer.elapsed()

        self.sequence_playhead_line.setValue(elapsed_ms)

        if elapsed_ms >= self.sequence_duration_ms:
            self.sequence_playhead_line.setValue(self.sequence_duration_ms)
            self.stop_sequence_playhead()
            self.set_status("Sequence finished")

    def stop_sequence_playhead(self):
        self.sequence_playhead_timer.stop()
        self.sequence_playhead_running = False

        print("Sequence playhead stopped")

    def get_ip_and_port(self):
        ip = self.ip_edit.text().strip()
        port_text = self.port_edit.text().strip()

        try:
            port = int(port_text)
        except ValueError:
            self.set_status("Invalid port")
            return None, None

        if not 1 <= port <= 65535:
            self.set_status("Port must be 1-65535")
            return None, None

        return ip, port

    def connect_or_disconnect(self):
        state = self.socket.state()

        if state == QAbstractSocket.ConnectedState:
            self.set_status("Disconnecting...")
            self.socket.disconnectFromHost()
            return

        if state in (
            QAbstractSocket.ConnectingState,
            QAbstractSocket.HostLookupState,
        ):
            self.set_status("Cancelling connection...")
            self.socket.abort()
            self.connect_button.setText("Connect")
            self.ping_button.setEnabled(False)
            self.calibrate_button.setEnabled(False)
            return

        ip, port = self.get_ip_and_port()

        if ip is None or port is None:
            return

        self.set_status(f"Connecting to {ip}:{port}...")
        self.connect_button.setText("Cancel")
        self.ping_button.setEnabled(False)
        self.calibrate_button.setEnabled(False)

        self.socket.abort()
        self.socket.connectToHost(ip, port)

    def ping_arduino(self):
        if self.socket.state() != QAbstractSocket.ConnectedState:
            self.set_status("Not connected")
            print("Cannot ping: not connected")
            return

        self.socket.write(struct.pack("<H", TCP_PING_COMMAND))
        self.socket.flush()

        self.set_status("Ping sent")

    def calibrate_arduino(self):
        if self.socket.state() != QAbstractSocket.ConnectedState:
            self.set_status("Not connected")
            print("Cannot calibrate: not connected")
            return

        self.socket.write(struct.pack("<H", TCP_CALIBRATE_COMMAND))
        self.socket.flush()

        self.set_status("Calibration command sent")
        print("Calibration command sent")

    def on_connected(self):
        self.set_status("Connected")
        self.connect_button.setText("Disconnect")
        self.ping_button.setEnabled(True)
        self.calibrate_button.setEnabled(True)
        print("Connected to Arduino")

    def on_disconnected(self):
        self.stop_sequence_playhead()
        self.set_status("Disconnected")
        self.connect_button.setText("Connect")
        self.ping_button.setEnabled(False)
        self.calibrate_button.setEnabled(False)
        print("Disconnected from Arduino")

    def on_error(self, socket_error):
        error_text = self.socket.errorString()
        self.stop_sequence_playhead()
        self.set_status(f"Error: {error_text}")
        self.connect_button.setText("Connect")
        self.ping_button.setEnabled(False)
        self.calibrate_button.setEnabled(False)
        print(f"Socket error: {error_text}")

    def on_ready_read(self):
        data = bytes(self.socket.readAll())

        if not data:
            return

        text = data.decode("utf-8", errors="replace").strip()

        if text == "PONG":
            self.set_status("Arduino replied: PONG")
            print("Arduino replied: PONG")
        elif text == "CALIBRATING":
            self.set_status("Arduino calibrating...")
            print("Arduino calibrating...")
        elif text == "CALIBRATION_DONE":
            self.set_status("Calibration complete")
            print("Calibration complete")
        elif text == "MANUAL_THROTTLE_SENT":
            self.set_status("Arduino accepted manual throttle")
            print("Arduino accepted manual throttle")
        else:
            self.set_status(f"Arduino: {text}")
            print(f"Arduino TCP data: {data}")

    def select_throttle_sequence(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self.window,
            "Select Throttle Sequence",
            "",
            "CSV Files (*.csv);;All Files (*)"
        )

        if not file_path:
            self.set_status("No sequence selected")
            return

        self.selected_sequence_file = file_path
        file_name = Path(file_path).name

        self.sequence_name_label.setText(file_name)

        self.set_status("Throttle sequence selected")
        print(f"Selected throttle sequence: {file_path}")

        self.update_sequence_graph(file_path)

    def upload_throttle_sequence(self):
        if self.selected_sequence_file is None:
            self.set_status("No sequence selected")
            return

        if self.socket.state() != QAbstractSocket.ConnectedState:
            self.set_status("Not connected")
            return

        project_dir = Path(__file__).parent

        selected_file = Path(self.selected_sequence_file)
        convert_script = project_dir / "convert_throttle.py"
        converted_file = project_dir / "converted_sequence.csv"

        if not selected_file.exists():
            self.set_status("Selected sequence file not found")
            print(f"Missing selected file: {selected_file}")
            return

        if not convert_script.exists():
            self.set_status("convert_throttle.py not found")
            print(f"Missing file: {convert_script}")
            return

        try:
            self.upload_sequence_button.setEnabled(False)

            self.set_status("Preparing sequence...")
            print(f"Selected sequence: {selected_file}")

            self.set_status("Converting sequence...")
            print("Running convert_throttle.py")

            convert_result = subprocess.run(
                [sys.executable, str(convert_script), str(selected_file)],
                cwd=project_dir,
                text=True,
                capture_output=True,
                check=True,
            )

            if convert_result.stdout:
                print("convert_throttle.py STDOUT:")
                print(convert_result.stdout)

            if convert_result.stderr:
                print("convert_throttle.py STDERR:")
                print(convert_result.stderr)

            if not converted_file.exists():
                self.set_status("Converted sequence file not found")
                print(f"Missing converted file: {converted_file}")
                return

            self.set_status("Sequence converted")

            self.set_status("Sending sequence...")

            result = self.send_converted_sequence_csv(converted_file)

            self.set_status(
                f"Sequence sent: {result['command_count']} commands, "
                f"{result['payload_size']} bytes"
            )

            self.start_sequence_playhead()

        except subprocess.CalledProcessError as error:
            self.stop_sequence_playhead()
            self.set_status("Upload failed")

            print("Upload failed")
            print(f"Command: {error.cmd}")
            print(f"Return code: {error.returncode}")

            if error.stdout:
                print("STDOUT:")
                print(error.stdout)

            if error.stderr:
                print("STDERR:")
                print(error.stderr)

        except Exception as error:
            self.stop_sequence_playhead()
            self.set_status("Upload failed")
            print(f"Upload failed: {error}")

        finally:
            self.upload_sequence_button.setEnabled(True)

    def load_converted_sequence_csv(self, filename):
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
                    raise RuntimeError(f"Missing column in CSV: {error}") from error
                except ValueError:
                    continue

                if direction not in (0, 1):
                    raise RuntimeError(f"Invalid direction value: {direction}")

                if duration_ms < 0:
                    raise RuntimeError(f"Invalid duration_ms: {duration_ms}")

                if steps < 0:
                    raise RuntimeError(f"Invalid steps: {steps}")

                if interval_us < 0:
                    raise RuntimeError(f"Invalid interval_us: {interval_us}")

                commands.append({
                    "duration_ms": duration_ms,
                    "steps": steps,
                    "direction": direction,
                    "interval_us": interval_us,
                    "commanded_throttle": commanded_throttle,
                })

        return commands

    def build_converted_sequence_payload(self, commands):
        payload = b""

        for command in commands:
            payload += struct.pack(
                "<IiBIf",
                command["duration_ms"],
                command["steps"],
                command["direction"],
                command["interval_us"],
                command["commanded_throttle"],
            )

        return payload

    def send_converted_sequence_csv(self, filename="converted_sequence.csv"):
        if self.socket.state() != QAbstractSocket.ConnectedState:
            self.set_status("Not connected")
            raise RuntimeError("Not connected to Arduino")

        commands = self.load_converted_sequence_csv(filename)

        if not commands:
            raise RuntimeError("No valid commands found in converted CSV")

        payload = self.build_converted_sequence_payload(commands)

        if len(payload) > self.sequence_buffer_capacity:
            raise RuntimeError(
                f"Payload too large: {len(payload)} bytes. "
                f"Arduino buffer is {self.sequence_buffer_capacity} bytes."
            )

        size_header = struct.pack("<H", len(payload))

        self.socket.write(size_header)
        self.socket.write(payload)
        self.socket.flush()

        print(f"Sent {len(commands)} commands")
        print(f"Payload size: {len(payload)} bytes")

        return {
            "command_count": len(commands),
            "payload_size": len(payload),
        }