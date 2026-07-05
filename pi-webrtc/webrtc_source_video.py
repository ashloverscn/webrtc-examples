import asyncio
import json
import time
import uuid
import threading
import logging
import numpy as np
import fractions
import cv2
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, MediaStreamTrack
import paho.mqtt.client as mqtt
from av import VideoFrame

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WebRTC-FileStream")

class FileVideoTrack(MediaStreamTrack):
    """
    Streams a local MP4 file cleanly through the WebRTC data pipeline.
    Loops seamlessly back to frame 0 when reaching EOF.
    """
    kind = "video"

    def __init__(self, video_path="./test.mp4"):
        super().__init__()
        self.counter = 0
        self._time_base = fractions.Fraction(1, 90000)
        self.video_path = video_path
        
        # Open video file context
        self.cap = cv2.VideoCapture(self.video_path)
        if not self.cap.isOpened():
            logger.error(f"Failed to open video file: {self.video_path}")
            raise FileNotFoundError(f"Could not open {self.video_path}")
            
        # Dynamically read media properties from file headers
        self.fps = self.cap.get(cv2.CAP_PROP_FPS)
        if self.fps <= 0 or np.isnan(self.fps):
            self.fps = 30.0  # Fallback baseline
            
        self.frame_delay = 1.0 / self.fps
        logger.info(f"Initialized Video Track for {self.video_path} ({self.fps} FPS)")

    async def recv(self):
        """Extracts individual file frames, converts layouts, and ticks timestamps."""
        # Calculate WebRTC 90kHz presentation timestamps
        pts = int(self.counter * (90000 / self.fps))
        
        # Process the raw video container stream frame
        ret, frame = self.cap.read()
        
        if not ret:
            # Loop seamlessly: Reset the capture frame context index to index 0
            logger.info("Looping video stream back to beginning...")
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = self.cap.read()
            if not ret:
                # Fallback backup option if container breaks completely
                await asyncio.sleep(0.1)
                frame = np.zeros((480, 640, 3), dtype=np.uint8)
                cv2.putText(frame, "FILE ERROR / READ FAILED", (50, 240),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        
        # Convert matrix color layouts (BGR to standard WebRTC RGB structure)
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # Wrap array structure natively within PyAV object layers
        video_frame = VideoFrame.from_ndarray(frame_rgb, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = self._time_base
        
        self.counter += 1
        
        # Precise paced pacing matching native playback file rate exactly
        await asyncio.sleep(self.frame_delay)
        
        return video_frame

    def stop(self):
        """Release container hooks properly when tracking boundaries drop."""
        if self.cap.isOpened():
            self.cap.release()
        super().stop()


class RemoteCameraSource:
    def __init__(self):
        self.peer_id = f"file_cam_{uuid.uuid4().hex[:6]}"
        self._loop = None
        self.signaling_topic = "webrtc/signaling"
        
        # MQTT Client Configuration
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
            logger.info("Connecting to HiveMQ Cloud...")
            self.mqtt_client.connect("e5122a5328ea4986a0295fa6e037655a.s2.eu.hivemq.cloud", 8883, 60)
            
            # Background engine loop workers
            threading.Thread(target=self.mqtt_client.loop_forever, daemon=True).start()
            threading.Thread(target=self.presence_loop, daemon=True).start()
        except Exception as e:
            logger.error(f"MQTT Connect Failed: {e}")

    def presence_loop(self):
        while self.running:
            msg = {"type": "presence", "from": self.peer_id}
            self.mqtt_client.publish(self.signaling_topic, json.dumps(msg))
            time.sleep(2)

    def on_mqtt_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
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
            if self.current_track:
                self.current_track.stop()

        self.pc = RTCPeerConnection()
        self.current_track = FileVideoTrack("./test.mp4")
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

        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))
        
        # FIX: Correct context scope binding applied (self.pc instead of raw global pc variable)
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
    
    print("-" * 40)
    print(f"🚀 MP4 FILE WEBRTC STREAMER ONLINE")
    print(f"DEVICE ID: {source.peer_id}")
    print("-" * 40)
    
    while True: 
        await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down stream...")
