"""Stands in for ffmpeg: prints 'JPEG' pictures, or fails the way ffmpeg does."""
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "stream"
out = sys.stdout.buffer


def frame(n):
    return b"\xff\xd8" + bytes([n % 200 + 1]) * 200 + b"\xff\xd9"


if mode == "unauthorized":
    sys.stderr.write("[rtsp] method DESCRIBE failed: 401 Unauthorized\n")
    sys.exit(1)
if mode == "unreachable":
    sys.stderr.write("Connection refused\n")
    sys.exit(1)
if mode == "one":
    out.write(frame(1))
    out.flush()
    sys.exit(0)
if mode == "burst":                 # five pictures, then silence (the camera went quiet)
    for i in range(5):
        out.write(frame(i))
        out.flush()
        time.sleep(0.02)
    time.sleep(30)
    sys.exit(0)
if mode == "crash":
    for i in range(3):
        out.write(frame(i))
        out.flush()
        time.sleep(0.02)
    sys.exit(1)
i = 0
while True:                      # "stream": about 25 pictures a second, for ever
    out.write(frame(i))
    out.flush()
    i += 1
    time.sleep(0.04)
