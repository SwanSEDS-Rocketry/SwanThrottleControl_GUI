import sys
from pathlib import Path

from PySide6.QtCore import QFile, QIODevice
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QLineEdit, QPushButton
from PySide6.QtNetwork import QTcpSocket, QAbstractSocket
from PySide6.QtGui import QAction


def load_ui_file(file_name, parent=None):
    ui_path = Path(__file__).with_name(file_name)
    ui_file = QFile(str(ui_path))

    if not ui_file.open(QIODevice.ReadOnly):
        raise RuntimeError(f"Could not open UI file: {ui_path}")

    loader = QUiLoader()
    window = loader.load(ui_file, parent)
    ui_file.close()

    if window is None:
        raise RuntimeError(f"Failed to load UI from {file_name}")

    return window


def load_main_window():
    window = load_ui_file("main.ui")
    window.setWindowTitle("Swan Throttle Control")
    return window


def load_about_window(parent=None):
    about_window = load_ui_file("about.ui", parent)
    about_window.setWindowTitle("About Swan Throttle Control")

    close_button = about_window.findChild(QPushButton, "closeButton")
    if close_button is not None:
        close_button.clicked.connect(about_window.close)

    return about_window


def apply_light_theme(app):
    app.setStyle("Fusion")

    app.setStyleSheet("""
        QMainWindow {
            background-color: #f0f0f0;
            color: black;
        }

        QWidget#centralwidget {
            background-color: #f0f0f0;
            color: black;
        }

        QGroupBox {
            background-color: #f0f0f0;
            color: black;
            border: 1px solid #9a9a9a;
            border-radius: 4px;
            margin-top: 10px;
            font-weight: bold;
        }

        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px 0 4px;
            background-color: #f0f0f0;
            color: black;
        }

        QLabel {
            background-color: transparent;
            color: black;
        }

        QPushButton {
            background-color: #e6e6e6;
            color: black;
            border: 1px solid #8a8a8a;
            border-radius: 3px;
            padding: 4px 8px;
        }

        QPushButton:hover {
            background-color: #dcdcdc;
        }

        QPushButton:pressed {
            background-color: #c8c8c8;
        }

        QLCDNumber {
            background-color: white;
            color: black;
        }

        QStatusBar {
            background-color: #f0f0f0;
            color: black;
        }
    """)


class ArduinoConnection:
    def __init__(self, window):
        self.window = window
        self.socket = QTcpSocket()

        self.ip_edit = window.findChild(QLineEdit, "IPEdit")
        self.port_edit = window.findChild(QLineEdit, "portEdit")
        self.connect_button = window.findChild(QPushButton, "connectButton")
        self.ping_button = window.findChild(QPushButton, "PingButton")
        self.status_label = window.findChild(QLabel, "ConnectionLabel")

        self.check_widgets_exist()
        self.setup_defaults()
        self.setup_socket()
        self.setup_buttons()

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
        if self.status_label is None:
            missing.append("ConnectionLabel")

        if missing:
            raise RuntimeError(f"Could not find these widgets in main.ui: {', '.join(missing)}")

    def setup_defaults(self):
        self.ip_edit.setText("192.168.10.2")
        self.port_edit.setText("5000")
        self.status_label.setText("Disconnected")
        self.ping_button.setEnabled(False)

    def setup_socket(self):
        self.socket.connected.connect(self.on_connected)
        self.socket.disconnected.connect(self.on_disconnected)
        self.socket.readyRead.connect(self.on_ready_read)
        self.socket.errorOccurred.connect(self.on_error)

    def setup_buttons(self):
        self.connect_button.clicked.connect(self.connect_or_disconnect)
        self.ping_button.clicked.connect(self.ping_arduino)

    def connect_or_disconnect(self):
        if self.socket.state() == QAbstractSocket.ConnectedState:
            self.socket.disconnectFromHost()
            return

        ip = self.ip_edit.text().strip()
        port_text = self.port_edit.text().strip()

        try:
            port = int(port_text)
        except ValueError:
            self.status_label.setText("Invalid port")
            return

        if not 1 <= port <= 65535:
            self.status_label.setText("Port must be 1-65535")
            return

        self.status_label.setText(f"Connecting to {ip}:{port}...")
        self.socket.abort()
        self.socket.connectToHost(ip, port)

    def send_command(self, command):
        if self.socket.state() != QAbstractSocket.ConnectedState:
            self.status_label.setText("Not connected")
            return

        message = command.strip() + "\n"
        self.socket.write(message.encode("utf-8"))
        self.status_label.setText(f"Sent: {command}")

    def ping_arduino(self):
        self.send_command("PING")

    def on_connected(self):
        self.status_label.setText("Connected")
        self.connect_button.setText("Disconnect")
        self.ping_button.setEnabled(True)

    def on_disconnected(self):
        self.status_label.setText("Disconnected")
        self.connect_button.setText("Connect")
        self.ping_button.setEnabled(False)

    def on_ready_read(self):
        while self.socket.canReadLine():
            line = bytes(self.socket.readLine()).decode("utf-8", errors="replace").strip()
            self.status_label.setText(f"Arduino: {line}")
            print(f"Arduino replied: {line}")

    def on_error(self):
        self.status_label.setText(f"Error: {self.socket.errorString()}")
        print(f"Socket error: {self.socket.errorString()}")


class MenuActions:
    def __init__(self, window):
        self.window = window
        self.about_window = None

        self.action_about = window.findChild(QAction, "actionAbout")

        if self.action_about is not None:
            self.action_about.triggered.connect(self.show_about_window)
        else:
            print("Warning: Could not find actionAbout")

    def show_about_window(self):
        self.about_window = load_about_window(self.window)
        self.about_window.show()


def apply_window_specific_styles(window):
    menu_bar = window.menuBar()

    if menu_bar is not None:
        menu_bar.setStyleSheet("""
            QMenuBar {
                background-color: #d0d0d0;
                color: black;
                border-bottom: 1px solid #8a8a8a;
            }

            QMenuBar::item {
                background-color: transparent;
                padding: 5px 10px;
            }

            QMenuBar::item:selected {
                background-color: #bcbcbc;
            }

            QMenuBar::item:pressed {
                background-color: #adadad;
            }

            QMenu {
                background-color: #eeeeee;
                color: black;
                border: 1px solid #999999;
            }

            QMenu::item {
                padding: 5px 24px 5px 24px;
            }

            QMenu::item:selected {
                background-color: #cfcfcf;
            }
        """)

    footer_frame = window.findChild(QFrame, "footerFrame")

    footer_title = window.findChild(QLabel, "footerTitleLabel")
    if footer_title is None:
        footer_title = window.findChild(QLabel, "footerTitellabel")

    footer_year = window.findChild(QLabel, "footerYearLabel")

    if footer_frame is not None:
        footer_frame.setStyleSheet("""
            #footerFrame {
                background-color: #8dc8e8;
                border-top: 3px solid #adadad;
            }
        """)

    if footer_title is not None:
        footer_title.setStyleSheet("""
            QLabel {
                background-color: transparent;
                color: white;
                font-weight: bold;
                padding-left: 10px;
            }
        """)

    if footer_year is not None:
        footer_year.setStyleSheet("""
            QLabel {
                background-color: transparent;
                color: white;
                font-weight: bold;
                padding-right: 10px;
            }
        """)


if __name__ == "__main__":
    app = QApplication(sys.argv)

    apply_light_theme(app)

    window = load_main_window()
    apply_window_specific_styles(window)

    arduino = ArduinoConnection(window)
    menu_actions = MenuActions(window)

    window.show()

    sys.exit(app.exec())