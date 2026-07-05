import asyncio
import json
import time
import uuid
import threading
import logging
import fractions
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, MediaStreamTrack
import paho.mqtt.client as mqtt
from av import VideoFrame
from picamera2 import Picamera2

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WebRTC-PiCam")

WIDTH, HEIGHT = 640, 480

# --- Global Frame Sync & Stream Controls ---
latest_frame_bytes = None
frame_ready_event = asyncio.Event()
streaming_allowed = asyncio.Event()  # Gating flag: blocks pipeline until WebRTC is connected
loop_ref = None
picam = None

def native_frame_callback(request):
    """Asynchronous background hardware frame receiver thread hook."""
    global latest_frame_bytes, loop_ref
    array = request.make_array("main")
    if array is not None:
        latest_frame_bytes = array.tobytes()
        if loop_ref:
            loop_ref.call_soon_threadsafe(frame_ready_event.set)


class CameraVideoTrack(MediaStreamTrack):
    """
    Feeds a real-time hardware stream directly out of the PiCamera2 ISP pipe.
    """
    kind = "video"

    def __init__(self):
        super().__init__()
        self.counter = 0
        self._time_base = fractions.Fraction(1, 90000)
        logger.info("Hardware PiCamera2 Video Track Initialized")

    async def recv(self):
        """Asynchronously requests and processes raw YUV420 planar frames."""
        global latest_frame_bytes, frame_ready_event, streaming_allowed
        
        # 1. Gate frames until ICE negotiation succeeds and state transitions to connected
        if not streaming_allowed.is_set():
            logger.debug("WebRTC not fully connected. Holding frame transmission...")
            await streaming_allowed.wait()
        
        # 2. Block until a fresh frame passes the background callback thread
        await frame_ready_event.wait()
        frame_ready_event.clear()
        
        if latest_frame_bytes is None:
            await asyncio.sleep(0.01)
            return await self.recv()

        # Manually construct custom PyAV frames to fix system-level NotImplementedError issues
        frame = VideoFrame(width=WIDTH, height=HEIGHT, format="yuv420p")
        
        y_size = WIDTH * HEIGHT
        uv_size = y_size // 4
        raw_data = latest_frame_bytes
        
        try:
            frame.planes[0].update(raw_data[0:y_size])                  # Y Plane
            frame.planes[1].update(raw_data[y_size:y_size + uv_size])     # U Plane
            frame.planes[2].update(raw_data[y_size + uv_size:])           # V Plane
        except Exception as plane_err:
            logger.debug(f"Plane data misalignment skip: {plane_err}")
            return await self.recv()
        
        # Incremental timeline stepping setup
        frame.pts = self.counter * 3000  
        frame.time_base = self._time_base
        
        self.counter += 1
        return frame


class RemoteCameraSource:
    def __init__(self):
        self.peer_id = f"picam_{uuid.uuid4().hex[:6]}"
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
            
            threading.Thread(target=self.mqtt_client.loop_forever, daemon=True).start()
            threading.Thread(target=self.presence_loop, daemon=True).start()
        except Exception as e:
            logger.error(f"MQTT Connect Failed: {e}")

    def presence_loop(self):
        while self.running:
            msg = {"type": "presence", "from": self.peer_id}
            try:
                self.mqtt_client.publish(self.signaling_topic, json.dumps(msg))
            except Exception as e:
                logger.debug(f"Failed to publish presence payload: {e}")
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
            logger.info(f"WebRTC Connection State: {self.pc.connectionState}")
            if self.pc.connectionState == "connected":
                logger.info("🔥 ICE Negotiation succeeded. Starting video frame transmission!")
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
    global loop_ref, picam
    loop_ref = asyncio.get_running_loop()
    
    # Pre-initialize camera here so hardware is active and ready before any viewer connects
    logger.info("[*] Pre-initializing Picamera2 hardware subsystems...")
    picam = Picamera2()
    config = picam.create_video_configuration(main={"format": "YUV420", "size": (WIDTH, HEIGHT)})
    picam.configure(config)
    picam.post_callback = native_frame_callback
    picam.start()
    logger.info("🚀 Camera processing pipeline is running and buffered.")

    source = RemoteCameraSource()
    source._loop = loop_ref
    source.connect()
    
    print("-" * 40)
    print(f"🚀 READY-GATED PICAMERA2 WEBRTC ONLINE")
    print(f"DEVICE ID: {source.peer_id}")
    print("-" * 40)
    
    try:
        while True: 
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        if picam:
            logger.info("[*] Stopping Picamera2 camera device context...")
            picam.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down stream...")
