import asyncio
import json
import time
import uuid
import threading
import logging
import fractions
import cv2
import numpy as np
import mss  # High performance desktop capture frame mechanism
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, MediaStreamTrack
import paho.mqtt.client as mqtt
from av import VideoFrame

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WebRTC-ScreenShare")

# Downscaled target resolution dimensions for WebRTC frame pipeline processing
WIDTH, HEIGHT = 640, 480

# --- Global Frame Sync & Stream Controls ---
latest_frame_bytes = None
frame_ready_event = asyncio.Event()
streaming_allowed = asyncio.Event()  
loop_ref = None
capture_thread = None
running_capture = False

def screen_capture_loop():
    """Background thread utilizing mss to continuously isolate active monitors."""
    global latest_frame_bytes, loop_ref, running_capture
    
    logger.info("Starting background desktop screen capture loop...")
    
    with mss.mss() as sct:
        # monitor[1] specifies primary desktop workstation area bounds
        monitor = sct.monitors[1] 
        
        while running_capture:
            start_time = time.time()
            try:
                sct_img = sct.grab(monitor)
                
                # Convert screen pixel arrays from raw BGRA down to standard BGR
                frame = np.array(sct_img)[:, :, :3]
                
                # Adjust dimensions to align with structural constraints
                if frame.shape[1] != WIDTH or frame.shape[0] != HEIGHT:
                    frame = cv2.resize(frame, (WIDTH, HEIGHT))
                    
                # Format to planar sequence configurations expected by downstream engine
                yuv_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420)
                latest_frame_bytes = yuv_frame.tobytes()
                
                if loop_ref:
                    loop_ref.call_soon_threadsafe(frame_ready_event.set)
                    
            except Exception as capture_err:
                logger.error(f"Error encountered throughout screen display slice processing: {capture_err}")
                
            # Cap pipeline throughput around ~30 FPS frame cycles
            elapsed = time.time() - start_time
            sleep_time = max(0, (1 / 30) - elapsed)
            time.sleep(sleep_time)


class CameraVideoTrack(MediaStreamTrack):
    """Feeds processed desktop matrix assets directly into WebRTC structural tracks."""
    kind = "video"

    def __init__(self):
        super().__init__()
        self.counter = 0
        self._time_base = fractions.Fraction(1, 90000)
        logger.info("Desktop Screen Stream Track Initialized")

    async def recv(self):
        """Asynchronously extracts current global memory representations for transit packaging."""
        global latest_frame_bytes, frame_ready_event, streaming_allowed
        
        if not streaming_allowed.is_set():
            logger.debug("WebRTC handshake pending setup. Holding queue transmissions...")
            await streaming_allowed.wait()
        
        await frame_ready_event.wait()
        frame_ready_event.clear()
        
        if latest_frame_bytes is None:
            await asyncio.sleep(0.01)
            return await self.recv()

        frame = VideoFrame(width=WIDTH, height=HEIGHT, format="yuv420p")
        
        y_size = WIDTH * HEIGHT
        uv_size = y_size // 4
        raw_data = latest_frame_bytes
        
        try:
            frame.planes[0].update(raw_data[0:y_size])                  
            frame.planes[1].update(raw_data[y_size:y_size + uv_size])     
            frame.planes[2].update(raw_data[y_size + uv_size:])           
        except Exception as plane_err:
            logger.debug(f"Plane configuration parsing alignment conflict dropped: {plane_err}")
            return await self.recv()
        
        frame.pts = self.counter * 3000  
        frame.time_base = self._time_base
        
        self.counter += 1
        return frame


class RemoteCameraSource:
    def __init__(self):
        self.peer_id = f"desktop_{uuid.uuid4().hex[:6]}"
        self._loop = None
        self.signaling_topic = "webrtc/signaling"
        
        self.mqtt_client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2, 
            client_id=f"desktop_{self.peer_id}", 
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
            
            threading.Thread(target=self.mqtt_client.loop_forever, daemon=True).start()
            threading.Thread(target=self.presence_loop, daemon=True).start()
        except Exception as e:
            logger.error(f"MQTT Transport Setup Failure: {e}")

    def presence_loop(self):
        while self.running:
            msg = {"type": "presence", "from": self.peer_id}
            try:
                self.mqtt_client.publish(self.signaling_topic, json.dumps(msg))
            except Exception as e:
                logger.debug(f"Failed to publish heartbeat metadata payload: {e}")
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
            logger.error(f"Signaling Error Encountered: {e}")

    async def handle_offer(self, data):
        global streaming_allowed
        if self.pc: 
            await self.pc.close()
            streaming_allowed.clear()

        self.pc = RTCPeerConnection()
        self.current_track = CameraVideoTrack()
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
            logger.info(f"WebRTC Connection State Updated: {self.pc.connectionState}")
            if self.pc.connectionState == "connected":
                logger.info("🔥 ICE Handshake Complete! Active transmission cycle engaging.")
                streaming_allowed.set()
            elif self.pc.connectionState in ["failed", "closed"]:
                streaming_allowed.clear()
                if self.current_track: 
                    self.current_track.stop()

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
    global loop_ref, capture_thread, running_capture
    loop_ref = asyncio.get_running_loop()
    
    running_capture = True
    capture_thread = threading.Thread(target=screen_capture_loop, daemon=True)
    capture_thread.start()
    
    logger.info("🚀 Desktop screen capture pipeline is running and buffered.")

    source = RemoteCameraSource()
    source._loop = loop_ref
    source.connect()
    
    print("-" * 40)
    print(f"🚀 READY-GATED DESKTOP WEBRTC ONLINE")
    print(f"DEVICE ID: {source.peer_id}")
    print("-" * 40)
    
    try:
        while True: 
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("[*] Halting execution operations...")
        running_capture = False
        if capture_thread:
            capture_thread.join(timeout=1.0)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down stream...")
