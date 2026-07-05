import time
import os
from picamera2 import Picamera2

# Configuration
WIDTH, HEIGHT = 640, 480
V4L2_DEVICE = "/dev/video17"

print(f"Initializing Picamera2 at {WIDTH}x{HEIGHT}...")
picam = Picamera2()

# Configure Picamera2 to output standard raw YUV420 frames
# This matches the pixel format v4l2loopback expects by default
config = picam.create_video_configuration(main={"size": (WIDTH, HEIGHT), "format": "YUV420"})
picam.configure(config)

print("Starting camera sensor...")
picam.start()

print(f"Opening virtual device {V4L2_DEVICE}...")
try:
    # Open the loopback device as a write-only binary file with unbuffered streams
    v4l2_out = open(V4L2_DEVICE, "wb", buffering=0)
except Exception as e:
    print(f"Error opening {V4L2_DEVICE}: {e}")
    print("Please verify v4l2loopback is loaded on that specific video index.")
    picam.stop()
    exit(1)

print(f"Throughput active: Physical Camera -> {V4L2_DEVICE}")
print("Press Ctrl+C to stop.")

try:
    while True:
        # 1. Grab the raw uncompressed YUV420 frame array from memory
        frame = picam.capture_array()
        
        # 2. Write the raw bytes directly to the virtual video device file descriptor
        v4l2_out.write(frame.tobytes())
        
        # 3. Target ~30 frames per second to prevent high CPU utilization
        time.sleep(1 / 30)

except KeyboardInterrupt:
    print("\nStopping throughput stream...")
finally:
    print("Releasing camera and closing device...")
    picam.stop()
    v4l2_out.close()
