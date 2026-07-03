import socket
import struct

UDP_PORT = 7080  # this must match remote_port in your Arduino code

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", UDP_PORT))

print(f"Listening for Arduino UDP telemetry on port {UDP_PORT}...")

while True:
    data, addr = sock.recvfrom(1024)

    time_ms = struct.unpack_from("<I", data, 1)[0]
    encoder_count = struct.unpack_from("<h", data, 5)[0]
    encoder_seen = bool(data[7])

    print(
        f"time={time_ms} ms, "
        f"encoder={encoder_count}, "
        f"seen={encoder_seen}"
    )# This Python file uses the following encoding: utf-8

# if __name__ == "__main__":
#     pass
