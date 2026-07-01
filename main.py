import sys
from pathlib import Path

from PySide6.QtCore import QFile, QIODevice
from PySide6.QtGui import QAction
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton

from arduino_connection import ArduinoConnection


APP_TITLE = "SwanSEDS | Kilgharrah Throttle Control Software v0.1.1"


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
    window.setWindowTitle(APP_TITLE)
    return window


def load_about_window(parent=None):
    about_window = load_ui_file("about.ui", parent)
    about_window.setWindowTitle("About")

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


def main():
    app = QApplication(sys.argv)

    apply_light_theme(app)

    window = load_main_window()
    apply_window_specific_styles(window)

    arduino = ArduinoConnection(window)
    menu_actions = MenuActions(window)

    # Keep references alive so Qt does not garbage collect them.
    window.arduino_connection = arduino
    window.menu_actions = menu_actions

    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())