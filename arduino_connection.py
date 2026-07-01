import csv
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
)
from PySide6.QtNetwork import QTcpSocket, QAbstractSocket


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

        self.upload_sequence_button = window.findChild(QPushButton, "uploadSeqButton")

        self.sequence_graph_frame = window.findChild(QFrame, "sequenceGraphFrame")

        self.check_widgets_exist()
        self.setup_defaults()
        self.setup_socket()
        self.setup_buttons()
        self.setup_sequence_graph()

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
                        # Allows header row like: time_ms, throttle
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

        self.socket.write(struct.pack("<H", 0xFFFF))
        self.socket.flush()

        self.set_status("Ping sent")

    def calibrate_arduino(self):
        if self.socket.state() != QAbstractSocket.ConnectedState:
            self.set_status("Not connected")
            print("Cannot calibrate: not connected")
            return

        self.socket.write(struct.pack("<H", 0xCA1B))
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