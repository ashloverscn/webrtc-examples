import asyncio
import json
import time
import uuid
import threading
import logging
import cv2
import numpy as np
import fractions
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, MediaStreamTrack
import paho.mqtt.client as mqtt
from av import VideoFrame

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WebRTC-Test")

class TestVideoTrack(MediaStreamTrack):
    """Generates a synthetic test pattern with a moving ball."""
    kind = "video"

    def __init__(self):
        super().__init__()
        self.counter = 0
        self.width = 640
        self.height = 480
        # 90kHz is the standard clock for WebRTC video
        self._time_base = fractions.Fraction(1, 90000)

    async def recv(self):
        """
        Calculates PTS (Presentation Time Stamp) manually 
        to avoid 'next_timestamp' AttributeErrors in newer aiortc versions.
        """
        # At 30fps, each frame is 3000 ticks apart (90000 / 30)
        pts = self.counter * 3000
        
        # Create a dark blue background
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[:] = [45, 25, 25] 

        # Draw a moving "bouncing" green circle
        orbit_radius = 150
        x = int(self.width / 2 + orbit_radius * np.cos(self.counter * 0.1))
        y = int(self.height / 2 + orbit_radius * np.sin(self.counter * 0.1))
        
        cv2.circle(frame, (x, y), 45, (0, 255, 0), -1)
        
        # Add text overlays
        cv2.putText(frame, f"PI TEST SIGNAL: {time.strftime('%H:%M:%S')}", (40, 60), 
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        cv2.putText(frame, f"Frame Count: {self.counter}", (40, 100), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (180, 180, 180), 1)

        # Create the VideoFrame
        video_frame = VideoFrame.from_ndarray(frame, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = self._time_base
        
        self.counter += 1
        
        # Control the frame rate (approx 30 FPS)
        await asyncio.sleep(1/30)
        
        return video_frame

class RemoteCameraSource:
    def __init__(self):
        self.peer_id = f"camera_{uuid.uuid4().hex[:6]}"
        self._loop = None
        self.signaling_topic = "webrtc/signaling"
        
        # Updated for Paho-MQTT v2.x (Python 3.13)
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
            if payload.get("to") != self.peer_id and payload.get("type") != "presence":
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
        self.pc = RTCPeerConnection()
        self.pc.addTrack(TestVideoTrack())
        
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
            logger.info(f"Connection state is {self.pc.connectionState}")

        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        
        self.send_signal("answer", {
            "sdp": self.pc.localDescription.sdp, 
            "type": self.pc.localDescription.type
        })
        logger.info(f"📤 Answer sent to {self.viewer_id}")

    async def handle_ice(self, data):
        if self.pc:
            candidate = RTCIceCandidate(
                sdpMid=data["sdpMid"], 
                sdpMLineIndex=data["sdpMLineIndex"], 
                candidate=data["candidate"]
            )
            await self.pc.addIceCandidate(candidate)

    def send_signal(self, msg_type, data):
        payload = {"type": msg_type, "from": self.peer_id, "to": self.viewer_id, "data": data}
        self.mqtt_client.publish(self.signaling_topic, json.dumps(payload))

async def main():
    source = RemoteCameraSource()
    source._loop = asyncio.get_running_loop()
    source.connect()
    print(f"\n🚀 TEST SIGNAL GENERATOR ONLINE")
    print(f"ID: {source.peer_id}\n")
    while True: 
        await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopping...")
