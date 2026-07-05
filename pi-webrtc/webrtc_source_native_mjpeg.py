import asyncio
import json
import time
import uuid
import threading
import logging
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
import paho.mqtt.client as mqtt
from picamera2 import Picamera2

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WebRTC-NativeMJPEG")

WIDTH, HEIGHT = 640, 480

# --- Global Frame Sync & Stream Controls ---
latest_jpeg_bytes = None
frame_ready_event = asyncio.Event()
loop_ref = None
picam = None

def native_frame_callback(request):
    """Asynchronous background hardware frame receiver thread hook."""
    global latest_jpeg_bytes, loop_ref
    # Pull the native compressed MJPEG buffer directly from the camera hardware pool
    fb = request.get_buffer("main")
    if fb is not None:
        latest_jpeg_bytes = bytes(fb)
        if loop_ref:
            loop_ref.call_soon_threadsafe(frame_ready_event.set)


class RemoteCameraSource:
    def __init__(self):
        self.peer_id = f"mjpeg_picam_{uuid.uuid4().hex[:6]}"
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
        self.data_channel = None
        self.stream_task = None

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
        if self.pc: 
            await self.pc.close()
            if self.stream_task:
                self.stream_task.cancel()

        self.pc = RTCPeerConnection()
        
        # Open an un-ordered, non-retransmitting Data Channel optimal for raw frame data transfers
        self.data_channel = self.pc.createDataChannel("mjpeg-stream", ordered=False, maxRetransmits=0)
        logger.info("📦 MJPEG Data Channel instantiated.")

        @self.data_channel.on("open")
        def on_dc_open():
            logger.info("🔥 MJPEG Data Channel connected. Starting frame transmission pipeline.")
            self.stream_task = asyncio.create_task(self.mjpeg_stream_loop())

        @self.data_channel.on("close")
        def on_dc_close():
            logger.info("❌ MJPEG Data Channel dropped.")
            if self.stream_task:
                self.stream_task.cancel()

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
                if self.stream_task:
                    self.stream_task.cancel()

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

    async def mjpeg_stream_loop(self):
        """Pulls the hot native MJPEG binary buffer and pumps it down the Data Channel wire."""
        global latest_jpeg_bytes, frame_ready_event
        logger.info("Streaming engine loop actively running.")
        
        try:
            while self.data_channel and self.data_channel.readyState == "open":
                await frame_ready_event.wait()
                frame_ready_event.clear()

                if latest_jpeg_bytes is not None:
                    # Low-overhead binary push across WebRTC layer
                    self.data_channel.send(latest_jpeg_bytes)
                    
                # Graceful async loop yielding
                await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            logger.info("Streaming loop execution halted cleanly.")
        except Exception as err:
            logger.error(f"Streaming loop error: {err}")

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
    
    # Pre-initialize camera here using correct native 'MJPEG' string literal profile
    logger.info("[*] Pre-initializing Picamera2 hardware subsystems (Native MJPEG)...")
    picam = Picamera2()
    
    # Use "MJPEG" explicitly for hardware-accelerated compressed frame processing
    config = picam.create_video_configuration(main={"format": "MJPEG", "size": (WIDTH, HEIGHT)})
    picam.configure(config)
    picam.post_callback = native_frame_callback
    picam.start()
    logger.info("🚀 Camera processing pipeline active in Native MJPEG Mode.")

    source = RemoteCameraSource()
    source._loop = loop_ref
    source.connect()
    
    print("-" * 50)
    print(f"🚀 PICAMERA2 NATIVE MJPEG WEBRTC RUNNING")
    print(f"DEVICE ID: {source.peer_id}")
    print("-" * 50)
    
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
