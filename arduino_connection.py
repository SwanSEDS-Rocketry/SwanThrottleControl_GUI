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
TCP_RESET_COMMAND = 0x5253  # 'RS'
UDP_TELEMETRY_PORT = 7080

# Voltage scaling.
# These multiply the Arduino analogue pin voltage.
# If the Arduino analogue pin receives direct 0-5 V, leave as 1.0.
# Later, if using resistor dividers, set these to the divider ratios.
VOLT_5_SCALE = 2.525
VOLT_24_SCALE = 7.221058
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

        # Optional reset button.
        # If this does not exist in Qt Designer yet, the GUI will still load.
        self.reset_arduino_button = window.findChild(QPushButton, "resetArduinoButton")

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
        self.lcd_throttle_programmed = window.findChild(QLCDNumber, "lcdThrottleProgrammed")

        # Error display widgets.
        self.error_display = window.findChild(QTextEdit, "errorDisplay")
        self.clear_error_button = window.findChild(QPushButton, "clearErrorButton")

        # Arm indicator light.
        self.arm_indicator = window.findChild(QLabel, "armIndicator")

        # TCP status lines are newline terminated, but TCP can join or split
        # messages. Keep a buffer so ARMED/DISARMED are parsed reliably.
        self.tcp_rx_buffer = ""
        self.system_armed = False

        self.error_history = []
        self.max_errors = 50

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
        if self.status_label is not None:
            self.status_label.setText(text)
        QApplication.processEvents()
        print(text)

    def report_error(self, error_type, error_message, details=""):
        """
        Comprehensive error reporting function.

        Args:
            error_type: Type of error, e.g. CONNECTION, HARDWARE, DATA.
            error_message: Brief error message.
            details: Additional error details.
        """
        from datetime import datetime

        timestamp = datetime.now().strftime("%H:%M:%S")
        full_error = f"[{timestamp}] {error_type}: {error_message}"

        if details:
            full_error += f"\n Details: {details}"

        self.error_history.append(full_error)

        if len(self.error_history) > self.max_errors:
            self.error_history.pop(0)

        if self.error_display is not None:
            self.error_display.setPlainText(full_error)

        print(f"ERROR: {full_error}")
        self.set_status(f"{error_type}: {error_message}")

    def clear_errors(self):
        self.error_history.clear()

        if self.error_display is not None:
            self.error_display.setPlainText("")

        self.set_status("Errors cleared")

    def get_error_history(self):
        return self.error_history

    def set_armed_indicator(self, is_armed):
        """
        Set the arm indicator light state.
        Green = ARMED.
        Red = DISARMED.
        """
        is_armed = bool(is_armed)
        self.system_armed = is_armed

        if self.arm_indicator is None:
            return

        if is_armed:
            self.arm_indicator.setStyleSheet(
                """
                QLabel {
                    background-color: #00ff00;
                    border: 2px solid #008000;
                    border-radius: 8px;
                    color: black;
                    font-weight: bold;
                    qproperty-alignment: AlignCenter;
                }
                """
            )
            self.arm_indicator.setText("ARMED")
        else:
            self.arm_indicator.setStyleSheet(
                """
                QLabel {
                    background-color: #ff0000;
                    border: 2px solid #8b0000;
                    border-radius: 8px;
                    color: white;
                    font-weight: bold;
                    qproperty-alignment: AlignCenter;
                }
                """
            )
            self.arm_indicator.setText("DISARMED")

    def set_reset_button_enabled(self, enabled):
        if self.reset_arduino_button is not None:
            self.reset_arduino_button.setEnabled(enabled)

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

        # Optional widgets.
        if self.reset_arduino_button is None:
            print("Warning: Could not find QPushButton resetArduinoButton")
        if self.volt_5 is None:
            print("Warning: Could not find QLCDNumber volt_5")
        if self.volt_24 is None:
            print("Warning: Could not find QLCDNumber volt_24")
        if self.volt_48 is None:
            print("Warning: Could not find QLCDNumber volt_48")
        if self.lcd_throttle_actual is None:
            print("Warning: Could not find QLCDNumber lcdThrottleActual")
        if self.lcd_throttle_programmed is None:
            print("Warning: Could not find QLCDNumber lcdThrottleProgrammed")
        if self.arm_indicator is None:
            print("Warning: Could not find QLabel armIndicator")

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
        self.set_reset_button_enabled(False)
        self.sequence_name_label.setText("No Sequence Loaded")

        # Safe default.
        self.set_armed_indicator(False)

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

        if self.reset_arduino_button is not None:
            self.reset_arduino_button.clicked.connect(self.reset_arduino)

        if self.clear_error_button is not None:
            self.clear_error_button.clicked.connect(self.clear_errors)

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
                    v5 = "n/a" if telemetry["volt_5"] is None else f"{telemetry['volt_5']:.2f}"
                    v24 = "n/a" if telemetry["volt_24"] is None else f"{telemetry['volt_24']:.2f}"
                    v48 = "n/a" if telemetry["volt_48"] is None else f"{telemetry['volt_48']:.2f}"

                    print(
                        "UDP telemetry: "
                        f"encoder={telemetry['encoder_count']}, "
                        f"armed={telemetry['armed']}, "
                        f"calibrated={telemetry['calibrated']}, "
                        f"V5={v5}, "
                        f"V24={v24}, "
                        f"V48={v48}, "
                        f"raw=({telemetry['voltage1_raw']}, "
                        f"{telemetry['voltage2_raw']}, "
                        f"{telemetry['voltage3_raw']})"
                    )
                elif telemetry["type"] == "encoder_sample":
                    value_type = "percent" if telemetry.get("is_percent") else "count"
                    print(
                        "UDP encoder sample: "
                        f"encoder={telemetry['encoder_count']} ({value_type})"
                    )

    def parse_udp_packet(self, data):
        if len(data) < 1:
            return None

        packet_type = data[0:1]

        # Current Arduino 14-byte telemetry packet:
        #
        # Byte 0 = 'T'
        # Bytes 1-4 = millis uint32
        # Bytes 5-6 = int16: centipercent when calibrated, raw count otherwise
        # Byte 7 = flags:
        #   bit0 encoder seen
        #   bit1 sequence running
        #   bit2 calibrating
        #   bit3 manual seek active
        #   bit4 calibrated / encoder field is centipercent
        #   bit5 START_PIN/ARM_PIN HIGH = armed
        # Bytes 8-13 = three raw ADC readings
        if packet_type == b"T":
            if len(data) == 14:
                time_ms = struct.unpack_from("<I", data, 1)[0]
                encoder_field = struct.unpack_from("<h", data, 5)[0]
                flags = data[7]

                encoder_seen = bool(flags & 0x01)
                sequence_running = bool(flags & 0x02)
                calibrating = bool(flags & 0x04)
                manual_seek_active = bool(flags & 0x08)
                calibrated = bool(flags & 0x10)
                armed = bool(flags & 0x20)

                voltage1_raw = struct.unpack_from("<H", data, 8)[0]
                voltage2_raw = struct.unpack_from("<H", data, 10)[0]
                voltage3_raw = struct.unpack_from("<H", data, 12)[0]

                voltage1_pin = voltage1_raw * 5.0 / 1023.0
                voltage2_pin = voltage2_raw * 5.0 / 1023.0
                voltage3_pin = voltage3_raw * 5.0 / 1023.0

                volt_5_value = voltage1_pin * VOLT_5_SCALE
                volt_24_value = voltage2_pin * VOLT_24_SCALE
                volt_48_value = voltage3_pin * VOLT_48_SCALE

                throttle_percent = encoder_field / 100.0 if calibrated else None

                return {
                    "type": "telemetry",
                    "time_ms": time_ms,
                    "encoder_count": encoder_field,
                    "encoder_seen": encoder_seen,
                    "sequence_running": sequence_running,
                    "calibrating": calibrating,
                    "manual_seek_active": manual_seek_active,
                    "calibrated": calibrated,
                    "armed": armed,
                    "throttle_percent": throttle_percent,
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

            # Old 8-byte telemetry compatibility.
            # This old packet does not include the arm bit, so fail safe to DISARMED.
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
                    "manual_seek_active": False,
                    "calibrated": False,
                    "armed": False,
                    "throttle_percent": None,
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

        # Encoder sample packet sent during sequences/manual seek:
        #
        # Byte 0 = 'E'
        # Bytes 1-4 = millis uint32
        # Bytes 5-6 = int16 value
        # Byte 7 = isPercent flag: 1 means value is centipercent
        if packet_type == b"E" and len(data) >= 8:
            time_ms = struct.unpack_from("<I", data, 1)[0]
            encoder_field = struct.unpack_from("<h", data, 5)[0]
            is_percent = bool(data[7])

            return {
                "type": "encoder_sample",
                "time_ms": time_ms,
                "encoder_count": encoder_field,
                "is_percent": is_percent,
                "throttle_percent": encoder_field / 100.0 if is_percent else None,
            }

        return None

    def deltaH(self, encoder_field):
        current_h = (
            -0.000351114 * encoder_field * encoder_field * encoder_field * encoder_field
            + 0.00101334 * encoder_field * encoder_field * encoder_field
            - 0.000935964 * encoder_field * encoder_field
            + 0.00077505 * encoder_field
        )

        return current_h


    def update_telemetry_display(self, telemetry):
        if telemetry["type"] == "telemetry":
            self.set_armed_indicator(telemetry.get("armed") is True)

        if "encoder_count" in telemetry and self.encoder_position_label is not None:
            self.encoder_position_label.setText(str(telemetry["encoder_count"]))

        encoder_raw = telemetry.get("encoder_count")

        if self.lcd_throttle_actual is not None:
            if encoder_raw is not None:
                self.lcd_throttle_actual.display(encoder_raw)
            else:
                self.lcd_throttle_actual.display(0)

        if self.lcd_throttle_programmed is not None:
            if encoder_raw is not None:
                delta_h_value = self.deltaH(encoder_raw)
                self.lcd_throttle_programmed.display(round(delta_h_value, 4))
                print(f"encoder_raw={encoder_raw}, deltaH={delta_h_value}")
            else:
                self.lcd_throttle_programmed.display(0.0)

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
            min(MANUAL_THROTTLE_MAX_PERCENT, throttle_percent),
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
            min(MANUAL_THROTTLE_MAX_PERCENT, throttle_percent),
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
            min(MANUAL_THROTTLE_MAX_PERCENT, float(throttle_percent)),
        )

        if self.socket.state() != QAbstractSocket.ConnectedState:
            self.report_error(
                "CONNECTION",
                "Cannot send throttle",
                "Socket is not connected to Arduino",
            )
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
                self.report_error(
                    "DATA",
                    "No valid graph data",
                    f"File '{filename}' contains no valid time/throttle pairs",
                )
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
        except FileNotFoundError:
            self.report_error("DATA", "Sequence file not found", filename)
        except Exception as error:
            self.report_error("DATA", "Failed to update graph", str(error))
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
            self.report_error(
                "VALIDATION",
                "Invalid port number",
                f"Port '{port_text}' is not a valid integer",
            )
            return None, None

        if not 1 <= port <= 65535:
            self.report_error(
                "VALIDATION",
                "Port out of range",
                f"Port must be 1-65535, got {port}",
            )
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
            self.set_reset_button_enabled(False)
            return

        ip, port = self.get_ip_and_port()

        if ip is None or port is None:
            return

        self.set_status(f"Connecting to {ip}:{port}...")
        self.connect_button.setText("Cancel")
        self.ping_button.setEnabled(False)
        self.calibrate_button.setEnabled(False)
        self.set_reset_button_enabled(False)
        self.socket.abort()
        self.socket.connectToHost(ip, port)

    def ping_arduino(self):
        if self.socket.state() != QAbstractSocket.ConnectedState:
            self.report_error(
                "CONNECTION",
                "Cannot ping Arduino",
                "Socket is not connected",
            )
            return

        self.socket.write(struct.pack("<H", TCP_PING_COMMAND))
        self.socket.flush()
        self.set_status("Ping sent")

    def reset_arduino(self):
        if self.socket.state() != QAbstractSocket.ConnectedState:
            self.report_error(
                "CONNECTION",
                "Cannot reset Arduino",
                "Socket is not connected",
            )
            return

        self.socket.write(struct.pack("<H", TCP_RESET_COMMAND))
        self.socket.flush()
        self.set_status("Reset command sent")
        print("Reset command sent")

    def calibrate_arduino(self):
        if self.socket.state() != QAbstractSocket.ConnectedState:
            self.report_error(
                "CONNECTION",
                "Cannot calibrate Arduino",
                "Socket is not connected",
            )
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
        self.set_reset_button_enabled(True)
        print("Connected to Arduino")

    def on_disconnected(self):
        self.stop_sequence_playhead()
        self.tcp_rx_buffer = ""
        self.set_armed_indicator(False)
        self.set_status("Disconnected")
        self.connect_button.setText("Connect")
        self.ping_button.setEnabled(False)
        self.calibrate_button.setEnabled(False)
        self.set_reset_button_enabled(False)
        print("Disconnected from Arduino")

    def on_error(self, socket_error):
        error_text = self.socket.errorString()
        self.stop_sequence_playhead()
        self.tcp_rx_buffer = ""
        self.set_armed_indicator(False)
        self.connect_button.setText("Connect")
        self.ping_button.setEnabled(False)
        self.calibrate_button.setEnabled(False)
        self.set_reset_button_enabled(False)
        self.report_error("CONNECTION", "Socket error occurred", error_text)
        print(f"Socket error: {error_text}")

    def on_ready_read(self):
        data = bytes(self.socket.readAll())

        if not data:
            return

        self.tcp_rx_buffer += data.decode("utf-8", errors="replace")

        # Arduino TCP messages are newline terminated. TCP can join messages
        # together, so process every complete line separately and keep any
        # partial line in self.tcp_rx_buffer until the next readyRead.
        while "\n" in self.tcp_rx_buffer:
            line, self.tcp_rx_buffer = self.tcp_rx_buffer.split("\n", 1)
            self.handle_tcp_line(line.strip())

        # Defensive fallback: if a whole message arrives without a newline,
        # still handle the known short status tokens.
        token = self.tcp_rx_buffer.strip()

        if token in (
            "ARMED",
            "DISARMED",
            "PONG",
            "CALIBRATING",
            "CALIBRATION_DONE",
            "MANUAL_THROTTLE_DONE",
            "RESETTING",
        ):
            self.tcp_rx_buffer = ""
            self.handle_tcp_line(token)

    def handle_tcp_line(self, text):
        if not text:
            return

        # Exact ARMED -> armed.
        # Exact DISARMED -> disarmed.
        # Other status strings do not make the system armed.
        # The live UDP bit also continuously updates the indicator.
        if text == "ARMED":
            self.set_armed_indicator(True)
            self.set_status("System ARMED")
            return

        if text == "DISARMED":
            self.set_armed_indicator(False)
            self.set_status("System DISARMED")
            return

        if text == "PONG":
            self.set_status("Arduino replied: PONG")
            print("Arduino replied: PONG")
        elif text == "CALIBRATING":
            self.set_status("Arduino calibrating...")
            print("Arduino calibrating...")
        elif text == "CALIBRATION_DONE":
            self.set_status("Calibration complete")
            print("Calibration complete")
        elif text == "MANUAL_THROTTLE_DONE":
            self.set_status("Manual throttle complete")
            print("Manual throttle complete")
        elif text == "MANUAL_THROTTLE_SENT":
            # Backwards compatibility with older Arduino code.
            self.set_status("Arduino accepted manual throttle")
            print("Arduino accepted manual throttle")
        elif text == "SEQUENCE_RECEIVED":
            self.set_status("Sequence received by Arduino")
        elif text == "WAITING_FOR_START_PIN":
            self.set_status("Waiting for arm/start pin")
        elif text == "WAITING_FOR_ARM_PIN":
            # Backwards compatibility with older Arduino code.
            self.set_status("Waiting for arm/start pin")
        elif text == "SEQUENCE_RUNNING":
            self.set_status("Sequence running")
            self.start_sequence_playhead()
        elif text == "SEQUENCE_DONE":
            self.stop_sequence_playhead()
            self.set_status("Sequence complete")
        elif text == "RESETTING":
            self.stop_sequence_playhead()
            self.set_armed_indicator(False)
            self.set_reset_button_enabled(False)
            self.set_status("Arduino resetting...")
            print("Arduino resetting...")
        elif text.startswith("ERROR_"):
            self.stop_sequence_playhead()
            self.report_error(
                "ARDUINO",
                text,
                "Arduino reported an error over TCP",
            )
        else:
            self.set_status(f"Arduino: {text}")
            print(f"Arduino TCP line: {text}")

    def select_throttle_sequence(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self.window,
            "Select Throttle Sequence",
            "",
            "CSV Files (*.csv);;All Files (*)",
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

            # Do not start the playhead here.
            # The latest Arduino waits for START_PIN/ARM_PIN before moving
            # and sends SEQUENCE_RUNNING when motion actually begins.
            # handle_tcp_line() starts the playhead then.
        except subprocess.CalledProcessError as error:
            self.stop_sequence_playhead()
            error_details = f"Return code: {error.returncode}\nCommand: {error.cmd}"

            if error.stdout:
                error_details += f"\nSTDOUT: {error.stdout}"
            if error.stderr:
                error_details += f"\nSTDERR: {error.stderr}"

            self.report_error(
                "CONVERSION",
                "Throttle sequence conversion failed",
                error_details,
            )
        except Exception as error:
            self.stop_sequence_playhead()
            self.report_error(
                "HARDWARE",
                "Failed to upload throttle sequence",
                str(error),
            )
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

                commands.append(
                    {
                        "duration_ms": duration_ms,
                        "steps": steps,
                        "direction": direction,
                        "interval_us": interval_us,
                        "commanded_throttle": commanded_throttle,
                    }
                )

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
