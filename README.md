# Swan Throttle Control GUI

Swan Throttle Control GUI is a Python Qt desktop application for communicating with an Arduino-based throttle controller over Ethernet.

The software is being developed for the SwanSEDS Rocketry Kilgharrah engine programme. It is intended to provide a graphical interface for connection monitoring, throttle control development, test setup, and future live status feedback.

## Project Status

This software is currently in development.

It is not yet flight-ready, test-stand-ready, or safety-certified. It should only be used in controlled development and test environments by people who understand the connected hardware.

## Features

- Python Qt desktop interface
- Designed using Qt Designer `.ui` files
- Ethernet communication with an Arduino
- Configurable Arduino IP address and port
- Connect and disconnect controls
- Ping/test connection button
- Arduino response display
- Custom application styling
- Footer branding
- About window
- Prepared for future throttle curve loading and live telemetry display

## Software Stack

This project uses:

- Python
- PySide6
- Qt Designer / Qt Creator
- Arduino Ethernet communication
- Git and GitHub

## Repository Structure

```text
SwanThrottleControl_GUI/
├── main.py
├── main.ui
├── about.ui
├── LICENSE
├── README.md
└── assets/
    ├── app_icon.png
    └── swandoc_logo.png
