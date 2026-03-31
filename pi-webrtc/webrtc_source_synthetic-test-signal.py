import asyncio
import json
import time
import uuid
import threading
import logging
import numpy as np
import fractions
import cv2
import psutil
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, MediaStreamTrack
import paho.mqtt.client as mqtt
from av import VideoFrame

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WebRTC-Synth")

class SyntheticVideoTrack(MediaStreamTrack):
    """
    Generates a synthetic animated graphic using OpenCV.
    Useful for testing WebRTC pipelines without physical camera hardware.
    """
    kind = "video"

    def __init__(self):
        super().__init__()
        self.counter = 0
        self._time_base = fractions.Fraction(1, 90000)
        
        # Canvas Dimensions
        self.width, self.height = 640, 480
        
        # Bouncing Ball State
        self.circle_pos = [self.width // 2, self.height // 2]
        self.circle_vel = [8, 5]
        self.circle_color = (0, 255, 0) # Green (BGR)

        logger.info("Synthetic Video Track Initialized (OpenCV Backend)")

    async def recv(self):
        """Generates and returns a single synthetic video frame."""
        pts = self.counter * 3000  # Based on 30fps (90000 / 30)
        
        # 1. Create Background (Dark Slate)
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[:] = (30, 20, 20) 

        # 2. Draw a subtle grid
        for x in range(0, self.width, 40):
            cv2.line(frame, (x, 0), (x, self.height), (45, 35, 35), 1)
        for y in range(0, self.height, 40):
            cv2.line(frame, (0, y), (self.width, y), (45, 35, 35), 1)

        # 3. Update Bouncing Ball Physics
        for i in range(2):
            self.circle_pos[i] += self.circle_vel[i]
            # Bounce off walls
            limit = self.width if i == 0 else self.height
            if self.circle_pos[i] <= 25 or self.circle_pos[i] >= limit - 25:
                self.circle_vel[i] *= -1

        # 4. Draw Graphic Elements
        # Bouncing Circle
        cv2.circle(frame, (self.circle_pos[0], self.circle_pos[1]), 25, self.circle_color, -1)
        cv2.circle(frame, (self.circle_pos[0], self.circle_pos[1]), 28, (255, 255, 255), 2)

        # UI Overlays
        cpu_usage = psutil.cpu_percent()
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        
        cv2.putText(frame, "OPENCV SYNTHETIC SOURCE", (20, 40), 
                    cv2.FONT_HERSHEY_DUPLEX, 0.7, (255, 255, 255), 2)
        cv2.putText(frame, f"Time: {timestamp}", (20, 430), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
        cv2.putText(frame, f"CPU Load: {cpu_usage}%", (20, 455), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255) if cpu_usage < 80 else (0, 0, 255), 1)
        cv2.putText(frame, f"Frame: {self.counter}", (500, 455), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)

        # 5. Convert BGR (OpenCV) to RGB (WebRTC Standard)
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # 6. Wrap in PyAV VideoFrame
        video_frame = VideoFrame.from_ndarray(frame_rgb, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = self._time_base
        
        self.counter += 1
        
        # Maintain ~30 FPS
        await asyncio.sleep(1/30)
        
        return video_frame

class RemoteCameraSource:
    def __init__(self):
        self.peer_id = f"synth_cam_{uuid.uuid4().hex[:6]}"
        self._loop = None
        self.signaling_topic = "webrtc/signaling"
        
        # MQTT Client Configuration
        self.mqtt_client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2, 
            client_id=f"cam_{self.peer_id}", 
            protocol=mqtt.MQTTv5
        )
        
        # IMPORTANT: These credentials match your snippet
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
            logger.info("Connecting to HiveMQ Cloud...")
            self.mqtt_client.connect("e5122a5328ea4986a0295fa6e037655a.s2.eu.hivemq.cloud", 8883, 60)
            
            # Run MQTT in background thread
            threading.Thread(target=self.mqtt_client.loop_forever, daemon=True).start()
            # Run Presence heartbeat in background thread
            threading.Thread(target=self.presence_loop, daemon=True).start()
        except Exception as e:
            logger.error(f"MQTT Connect Failed: {e}")

    def presence_loop(self):
        """Informs the signaling server that this camera is online."""
        while self.running:
            msg = {"type": "presence", "from": self.peer_id}
            self.mqtt_client.publish(self.signaling_topic, json.dumps(msg))
            time.sleep(2)

    def on_mqtt_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            # Only handle messages explicitly sent to us
            if payload.get("to") != self.peer_id: 
                return
            
            msg_type = payload.get("type")
            if msg_type == "offer":
                self.viewer_id = payload.get("from")
                logger.info(f"📥 Received Offer from {self.viewer_id}")
                asyncio.run_coroutine_threadsafe(self.handle_offer(payload.get("data")), self._loop)
            elif msg_type == "ice" and self.pc:
                asyncio.run_coroutine_threadsafe(self.handle_ice(payload.get("data")), self._loop)
        except Exception as e:
            logger.error(f"Signaling Error: {e}")

    async def handle_offer(self, data):
        if self.pc: 
            await self.pc.close()

        self.pc = RTCPeerConnection()
        self.current_track = SyntheticVideoTrack()
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
            logger.info(f"WebRTC Connection State: {self.pc.connectionState}")
            if self.pc.connectionState in ["failed", "closed"]:
                if self.current_track: 
                    self.current_track.stop()

        # Set Remote Description and Create Answer
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        
        self.send_signal("answer", {
            "sdp": self.pc.localDescription.sdp, 
            "type": self.pc.localDescription.type
        })

    async def handle_ice(self, data):
        if self.pc:
            candidate = RTCIceCandidate(
                sdpMid=data["sdpMid"], 
                sdpMLineIndex=data["sdpMLineIndex"], 
                candidate=data["candidate"]
            )
            await self.pc.addIceCandidate(candidate)

    def send_signal(self, msg_type, data):
        payload = {
            "type": msg_type, 
            "from": self.peer_id, 
            "to": self.viewer_id, 
            "data": data
        }
        self.mqtt_client.publish(self.signaling_topic, json.dumps(payload))

async def main():
    source = RemoteCameraSource()
    source._loop = asyncio.get_running_loop()
    source.connect()
    
    print("-" * 30)
    print(f"🚀 SYNTHETIC WEBRTC SOURCE ONLINE")
    print(f"DEVICE ID: {source.peer_id}")
    print("-" * 30)
    
    # Keep the main loop alive
    while True: 
        await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down stream...")
