import asyncio
import json
import time
import uuid
import threading
import logging
import numpy as np
import fractions
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, MediaStreamTrack
import paho.mqtt.client as mqtt
from av import VideoFrame

# Attempt to import Picamera2
try:
    from picamera2 import Picamera2
except ImportError:
    print("Error: picamera2 not found. Install with 'sudo apt install python3-picamera2'")

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WebRTC-PiCam2")

class PiCamera2VideoTrack(MediaStreamTrack):
    """Captures frames using the modern Picamera2 libcamera backend."""
    kind = "video"

    def __init__(self):
        super().__init__()
        self.counter = 0
        self._time_base = fractions.Fraction(1, 90000)
        
        # Initialize Picamera2
        self.picam2 = Picamera2()
        
        # Configure for 640x480 at 30fps
        config = self.picam2.create_video_configuration(main={"size": (640, 480)})
        self.picam2.configure(config)
        
        # Start the camera
        self.picam2.start()
        logger.info("Picamera2 started successfully.")

    async def recv(self):
        """Captures a frame from the Picamera2 request queue."""
        pts = self.counter * 3000  # 90000 / 30fps
        
        # Capture a single frame as a numpy array (RGB by default in Picamera2)
        try:
            # capture_array() is synchronous; it's fast enough for 30fps 
            # but in production, a background thread-buffer is better.
            frame = self.picam2.capture_array()
        except Exception as e:
            logger.error(f"Capture error: {e}")
            frame = np.zeros((480, 640, 3), dtype=np.uint8)

        # Create the VideoFrame object
        # Picamera2 array is already RGB, so no cvtColor is needed
        video_frame = VideoFrame.from_ndarray(frame, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = self._time_base
        
        self.counter += 1
        
        # Sync with requested framerate
        await asyncio.sleep(1/30)
        
        return video_frame

    def stop(self):
        """Cleanly stop the camera hardware."""
        self.picam2.stop()
        self.picam2.close()
        super().stop()

class RemoteCameraSource:
    def __init__(self):
        self.peer_id = f"pi_libcam_{uuid.uuid4().hex[:6]}"
        self._loop = None
        self.signaling_topic = "webrtc/signaling"
        
        self.mqtt_client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2, 
            client_id=f"cam_{self.peer_id}", 
            protocol=mqtt.MQTTv5
        )
        self.mqtt_client.tls_set()
        self.mqtt_client.username_pw_set("admin", "admin1234S")
        
        self.pc = None
        self.viewer_id = None
        self.running = True
        self.current_track = None

    def connect(self):
        self.mqtt_client.on_connect = lambda c, u, f, rc, p: c.subscribe(self.signaling_topic)
        self.mqtt_client.on_message = self.on_mqtt_message
        try:
            self.mqtt_client.connect("e5122a5328ea4986a0295fa6e037655a.s2.eu.hivemq.cloud", 8883, 60)
            threading.Thread(target=self.mqtt_client.loop_forever, daemon=True).start()
            threading.Thread(target=self.presence_loop, daemon=True).start()
        except Exception as e:
            logger.error(f"MQTT Connect Failed: {e}")

    def presence_loop(self):
        while self.running:
            msg = {"type": "presence", "from": self.peer_id}
            self.mqtt_client.publish(self.signaling_topic, json.dumps(msg))
            time.sleep(1)

    def on_mqtt_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            if payload.get("to") != self.peer_id: return
            
            msg_type = payload.get("type")
            if msg_type == "offer":
                self.viewer_id = payload.get("from")
                logger.info(f"📥 Offer from {self.viewer_id}")
                asyncio.run_coroutine_threadsafe(self.handle_offer(payload.get("data")), self._loop)
            elif msg_type == "ice" and self.pc:
                asyncio.run_coroutine_threadsafe(self.handle_ice(payload.get("data")), self._loop)
        except Exception as e:
            logger.error(f"Signaling Error: {e}")

    async def handle_offer(self, data):
        if self.pc: await self.pc.close()

        self.pc = RTCPeerConnection()
        self.current_track = PiCamera2VideoTrack()
        self.pc.addTrack(self.current_track)
        
        @self.pc.on("icecandidate")
        async def on_candidate(candidate):
            if candidate:
                self.send_signal("ice", {
                    "sdpMid": candidate.sdpMid, 
                    "sdpMLineIndex": candidate.sdpMLineIndex, 
                    "candidate": candidate.candidate
                })

        @self.pc.on("connectionstatechange")
        async def on_state_change():
            logger.info(f"Connection state: {self.pc.connectionState}")
            if self.pc.connectionState in ["failed", "closed"]:
                if self.current_track: self.current_track.stop()

        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        
        self.send_signal("answer", {"sdp": self.pc.localDescription.sdp, "type": self.pc.localDescription.type})

    async def handle_ice(self, data):
        if self.pc:
            candidate = RTCIceCandidate(sdpMid=data["sdpMid"], sdpMLineIndex=data["sdpMLineIndex"], candidate=data["candidate"])
            await self.pc.addIceCandidate(candidate)

    def send_signal(self, msg_type, data):
        payload = {"type": msg_type, "from": self.peer_id, "to": self.viewer_id, "data": data}
        self.mqtt_client.publish(self.signaling_topic, json.dumps(payload))

async def main():
    source = RemoteCameraSource()
    source._loop = asyncio.get_running_loop()
    source.connect()
    print(f"\n🚀 PICAMERA2 WEBRTC STREAMER ONLINE")
    print(f"ID: {source.peer_id}\n")
    while True: await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopping...")
