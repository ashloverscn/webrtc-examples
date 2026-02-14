import asyncio
import json
import time
import uuid
import threading
import logging
from typing import Dict, Optional
import cv2
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, MediaStreamTrack
import paho.mqtt.client as mqtt
from av import VideoFrame

# Setup detailed logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)-20s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("WebRTC-Camera")

# Enable aiortc debug logging
logging.getLogger('aiortc').setLevel(logging.DEBUG)
logging.getLogger('aioice').setLevel(logging.DEBUG)


class CameraVideoStreamTrack(MediaStreamTrack):
    """Video stream track that captures from OpenCV camera."""
    kind = "video"
    
    def __init__(self, camera_id=0):
        super().__init__()
        self.camera_id = camera_id
        self.cap = cv2.VideoCapture(camera_id)
        self.width = 640
        self.height = 480
        self.fps = 30
        
        # Set camera properties
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self.cap.set(cv2.CAP_PROP_FPS, self.fps)
        
        # Stats
        self.frame_count = 0
        self.start_time = time.time()
        self.last_frame_time = time.time()
        self.frame_times = []
        
        actual_width = self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        actual_height = self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        actual_fps = self.cap.get(cv2.CAP_PROP_FPS)
        logger.info(f"📹 Camera initialized: {actual_width}x{actual_height} @ {actual_fps}fps")
        
        if not self.cap.isOpened():
            logger.error("❌ Failed to open camera!")
        
    async def recv(self):
        pts, time_base = await self.next_timestamp()
        
        ret, frame = self.cap.read()
        if not ret:
            logger.warning("⚠️ Camera read failed - generating error frame")
            frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            cv2.putText(frame, "Camera Error", (50, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        
        # Calculate actual FPS
        current_time = time.time()
        frame_time = current_time - self.last_frame_time
        self.frame_times.append(frame_time)
        if len(self.frame_times) > 30:
            self.frame_times.pop(0)
        self.last_frame_time = current_time
        
        self.frame_count += 1
        if self.frame_count % 30 == 0:
            avg_frame_time = sum(self.frame_times) / len(self.frame_times)
            actual_fps = 1.0 / avg_frame_time if avg_frame_time > 0 else 0
            logger.debug(f"🎥 Frame {self.frame_count} captured | Actual FPS: {actual_fps:.2f}")
        
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        video_frame = VideoFrame.from_ndarray(frame, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        
        return video_frame
    
    def stop(self):
        super().stop()
        self.cap.release()
        logger.info("📹 Camera released")


class RemoteCameraSource:
    """WebRTC Camera Source - sends video to viewers, receives commands via data channel"""
    
    def __init__(self, camera_id=0, verbose_ice=True):
        self.peer_id = f"camera_{uuid.uuid4().hex[:6]}"
        self.camera_id = camera_id
        self.verbose_ice = verbose_ice
        
        # Store the main event loop reference for thread-safe scheduling
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
        # MQTT setup
        self.broker_url = "e5122a5328ea4986a0295fa6e037655a.s2.eu.hivemq.cloud"
        self.broker_port = 8883
        self.signaling_topic = "webrtc/signaling"
        self.announce_topic = "camera/announce"
        
        self.mqtt_client = mqtt.Client(client_id=f"cam_{self.peer_id}", protocol=mqtt.MQTTv5)
        self.mqtt_client.tls_set()
        self.mqtt_client.username_pw_set("admin", "admin1234S")
        self.mqtt_client.enable_logger(logger)
        
        # WebRTC
        self.pc: Optional[RTCPeerConnection] = None
        self.dc = None
        self.local_track = None
        self.viewer_id = None
        
        self.running = True
        self.connected = False
        
        # Connection states
        self.connection_state = "new"
        self.ice_connection_state = "new"
        self.ice_gathering_state = "new"
        self.signaling_state = "stable"
        
        # Stats
        self.frames_sent = 0
        self.start_time = time.time()
        self.ice_candidates_sent = 0
        self.ice_candidates_received = 0
        
        self.setup_mqtt()
        logger.info(f"📹 RemoteCameraSource initialized | Peer ID: {self.peer_id} | Camera ID: {camera_id}")
        
    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """Store reference to the main event loop for thread-safe operations"""
        self._loop = loop
        logger.debug(f"Event loop set: {loop}")
        
    def _run_coroutine_threadsafe(self, coro):
        """Helper to run coroutine from MQTT thread in the main event loop"""
        if self._loop is None:
            logger.error("❌ No event loop set! Cannot schedule coroutine.")
            return None
        
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            logger.debug(f"✅ Scheduled coroutine in main loop: {coro.__name__}")
            return future
        except Exception as e:
            logger.error(f"❌ Failed to schedule coroutine: {e}")
            return None
        
    def setup_mqtt(self):
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
        self.mqtt_client.on_publish = self.on_mqtt_publish
        self.mqtt_client.on_subscribe = self.on_mqtt_subscribe
        
    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        logger.info(f"✅ MQTT Connected | Result code: {rc} | Flags: {flags}")
        client.subscribe(self.signaling_topic)
        logger.info(f"📡 Subscribed to: {self.signaling_topic}")
        self.announce_camera()
        threading.Thread(target=self.announce_loop, daemon=True).start()
        
    def on_mqtt_subscribe(self, client, userdata, mid, granted_qos, properties=None):
        logger.debug(f"📋 MQTT Subscribed | Message ID: {mid} | QoS: {granted_qos}")
        
    def on_mqtt_publish(self, client, userdata, mid, properties=None):
        logger.debug(f"📤 MQTT Published | Message ID: {mid}")
        
    def announce_camera(self):
        """Announce that this camera is available"""
        announcement = {
            "type": "camera_available",
            "camera_id": self.peer_id,
            "timestamp": time.time(),
            "has_camera": True,
            "resolution": "640x480"
        }
        result = self.mqtt_client.publish(self.announce_topic, json.dumps(announcement))
        logger.info(f"📢 Announced camera: {self.peer_id} | MQTT result: {result.rc}")
        
    def announce_loop(self):
        while self.running:
            time.sleep(5)
            if not self.connected:
                logger.debug("🔄 Re-announcing camera availability...")
                self.announce_camera()
        
    def on_mqtt_disconnect(self, client, userdata, rc):
        logger.warning(f"⚠️ MQTT Disconnected | Result code: {rc}")
        
    def on_mqtt_message(self, client, userdata, msg):
        """Handle MQTT messages - runs in MQTT thread, use thread-safe scheduling"""
        try:
            payload = json.loads(msg.payload.decode())
            logger.debug(f"📨 MQTT Message | Topic: {msg.topic} | Payload: {json.dumps(payload, indent=2)}")
            
            if payload.get("to") != self.peer_id:
                logger.debug(f"⏭️  Message not for us (to: {payload.get('to')})")
                return
                
            msg_type = payload.get("type")
            from_peer = payload.get("from")
            data = payload.get("data")
            
            logger.info(f"📥 SIGNALING | {msg_type.upper()} from viewer {from_peer}")
            
            # Use thread-safe scheduling instead of create_task
            if msg_type == "view_request":
                self.viewer_id = from_peer
                self._run_coroutine_threadsafe(self.handle_view_request())
            elif msg_type == "answer":
                self._run_coroutine_threadsafe(self.handle_answer(data))
            elif msg_type == "ice":
                self.ice_candidates_received += 1
                logger.info(f"🧊 ICE Candidate received (#{self.ice_candidates_received})")
                self._run_coroutine_threadsafe(self.handle_remote_ice(data))
            else:
                logger.warning(f"⚠️ Unknown message type: {msg_type}")
                
        except Exception as e:
            logger.error(f"❌ Error handling MQTT message: {e}", exc_info=True)
            
    async def handle_view_request(self):
        """Handle a viewer requesting to watch our camera"""
        logger.info(f"🎥 Viewer {self.viewer_id} requested stream")
        
        # Create peer connection with detailed config
        config = {
            "iceServers": [
                {"urls": "stun:stun.l.google.com:19302"},
                {"urls": "stun:stun1.l.google.com:19302"},
                {"urls": "stun:stun2.l.google.com:19302"}
            ],
            "iceTransportPolicy": "all",
            "bundlePolicy": "max-bundle",
            "rtcpMuxPolicy": "require"
        }
        
        self.pc = RTCPeerConnection(configuration=config)
        logger.info(f"🔧 RTCPeerConnection created | Config: {json.dumps(config, indent=2)}")
        
        # Add camera track
        self.local_track = CameraVideoStreamTrack(self.camera_id)
        self.pc.addTrack(self.local_track)
        logger.info("➕ Video track added to peer connection")
        
        # Create data channel for receiving commands
        self.dc = self.pc.createDataChannel("camera_control", ordered=True)
        self.setup_data_channel()
        logger.info("📡 Data channel 'camera_control' created")
        
        # Setup all state change handlers
        @self.pc.on("connectionstatechange")
        async def on_connection_state_change():
            old_state = self.connection_state
            self.connection_state = self.pc.connectionState
            logger.info(f"🔗 Connection State: {old_state} -> {self.connection_state}")
            if self.connection_state == "connected":
                self.connected = True
                logger.info("✅ Peer connection fully established!")
            elif self.connection_state in ["failed", "disconnected", "closed"]:
                self.connected = False
                logger.warning(f"❌ Connection lost: {self.connection_state}")
                await self.cleanup()
        
        @self.pc.on("iceconnectionstatechange")
        async def on_ice_state_change():
            old_state = self.ice_connection_state
            self.ice_connection_state = self.pc.iceConnectionState
            logger.info(f"🧊 ICE Connection State: {old_state} -> {self.ice_connection_state}")
            
            if self.ice_connection_state == "checking":
                logger.info("🔍 ICE checking - gathering candidates...")
            elif self.ice_connection_state == "connected":
                logger.info("✅ ICE connected - ready to stream!")
            elif self.ice_connection_state == "completed":
                logger.info("🎉 ICE completed - optimal path found!")
            elif self.ice_connection_state == "failed":
                logger.error("💥 ICE failed - check STUN/TURN servers")
            elif self.ice_connection_state == "disconnected":
                logger.warning("⚠️ ICE disconnected - attempting recovery...")
            elif self.ice_connection_state == "closed":
                logger.info("🚪 ICE connection closed")
        
        @self.pc.on("icegatheringstatechange")
        async def on_ice_gathering_change():
            old_state = self.ice_gathering_state
            self.ice_gathering_state = self.pc.iceGatheringState
            logger.info(f"📶 ICE Gathering State: {old_state} -> {self.ice_gathering_state}")
            if self.ice_gathering_state == "gathering":
                logger.info("🌐 Started gathering ICE candidates...")
            elif self.ice_gathering_state == "complete":
                logger.info(f"✅ ICE gathering complete | Total sent: {self.ice_candidates_sent}")
        
        @self.pc.on("signalingstatechange")
        async def on_signaling_change():
            old_state = self.signaling_state
            self.signaling_state = self.pc.signalingState
            logger.info(f"📶 Signaling State: {old_state} -> {self.signaling_state}")
        
        @self.pc.on("icecandidate")
        async def on_ice_candidate(candidate):
            if candidate:
                self.ice_candidates_sent += 1
                candidate_data = {
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex,
                    "candidate": candidate.candidate
                }
                if self.verbose_ice:
                    logger.debug(f"🧊 Local ICE Candidate #{self.ice_candidates_sent}: {candidate.candidate[:80]}...")
                else:
                    logger.debug(f"🧊 Local ICE Candidate #{self.ice_candidates_sent} generated")
                self.send_signal("ice", candidate_data)
            else:
                logger.info("🛑 Null ICE candidate - end of candidates")
        
        @self.pc.on("track")
        async def on_track(track):
            logger.info(f"📥 Remote track received: {track.kind}")
        
        @self.pc.on("datachannel")
        async def on_datachannel(channel):
            logger.info(f"📨 New data channel: {channel.label}")
        
        # Create offer
        logger.info("📝 Creating offer...")
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        logger.info(f"📤 Local description set | Type: {offer.type} | SDP length: {len(offer.sdp)} chars")
        
        # Log SDP details
        sdp_lines = offer.sdp.split('\n')
        logger.debug(f"SDP Preview:\n" + '\n'.join(sdp_lines[:20]) + "\n...")
        
        self.send_signal("offer", {"sdp": offer.sdp, "type": offer.type})
        
    def setup_data_channel(self):
        @self.dc.on("message")
        def on_message(message):
            logger.info(f"📩 Data Channel Message: {message}")
            self.handle_command(message)
            
        @self.dc.on("open")
        def on_open():
            logger.info("📡 Data channel OPENED")
            if self.dc:
                self.dc.send(json.dumps({
                    "type": "welcome",
                    "camera_id": self.peer_id,
                    "message": "Camera ready"
                }))
            
        @self.dc.on("close")
        def on_close():
            logger.info("📡 Data channel CLOSED")
            
        @self.dc.on("error")
        def on_error(error):
            logger.error(f"💥 Data channel error: {error}")
            
        @self.dc.on("bufferedamountlow")
        def on_buffered_amount_low():
            logger.debug("📉 Data channel buffer low")
            
        @self.dc.on("closing")
        def on_closing():
            logger.debug("🚪 Data channel closing...")
            
    def handle_command(self, cmd):
        try:
            data = json.loads(cmd) if isinstance(cmd, str) else cmd
            action = data.get("action")
            logger.info(f"🎯 Command received: {action}")
            
            if action == "get_info":
                info = {
                    "camera_id": self.peer_id,
                    "has_camera": True,
                    "uptime": time.time() - self.start_time,
                    "frames_sent": self.local_track.frame_count if self.local_track else 0,
                    "resolution": "640x480",
                    "fps": 30,
                    "connection_state": self.connection_state,
                    "ice_state": self.ice_connection_state
                }
                response = json.dumps({"type": "info", "data": info})
                if self.dc and self.dc.readyState == "open":
                    self.dc.send(response)
                    logger.debug(f"📤 Sent info response: {info}")
                    
            elif action == "ping":
                response = json.dumps({"type": "pong", "time": time.time()})
                if self.dc and self.dc.readyState == "open":
                    self.dc.send(response)
                    logger.debug("📤 Sent pong")
                    
            elif action == "get_stats":
                stats = self.get_detailed_stats()
                response = json.dumps({"type": "stats", "data": stats})
                if self.dc and self.dc.readyState == "open":
                    self.dc.send(response)
                    
            else:
                logger.warning(f"⚠️ Unknown command: {action}")
                
        except Exception as e:
            logger.error(f"❌ Error handling command: {e}", exc_info=True)
    
    def get_detailed_stats(self):
        return {
            "peer_id": self.peer_id,
            "viewer_id": self.viewer_id,
            "connection_state": self.connection_state,
            "ice_connection_state": self.ice_connection_state,
            "ice_gathering_state": self.ice_gathering_state,
            "signaling_state": self.signaling_state,
            "ice_candidates_sent": self.ice_candidates_sent,
            "ice_candidates_received": self.ice_candidates_received,
            "connected": self.connected,
            "uptime": time.time() - self.start_time,
            "frames_captured": self.local_track.frame_count if self.local_track else 0
        }
            
    async def handle_answer(self, answer_data):
        logger.info("📥 Processing viewer answer...")
        try:
            answer = RTCSessionDescription(sdp=answer_data["sdp"], type=answer_data["type"])
            await self.pc.setRemoteDescription(answer)
            logger.info("✅ Remote description (answer) set successfully")
            
            sdp_lines = answer.sdp.split('\n')
            logger.debug(f"Remote SDP Preview:\n" + '\n'.join(sdp_lines[:15]) + "\n...")
            
        except Exception as e:
            logger.error(f"❌ Error setting remote description: {e}", exc_info=True)
        
    async def handle_remote_ice(self, candidate_data):
        try:
            logger.debug(f"Adding remote ICE candidate: {json.dumps(candidate_data, indent=2)}")
            candidate = RTCIceCandidate(
                sdpMid=candidate_data.get("sdpMid"),
                sdpMLineIndex=candidate_data.get("sdpMLineIndex"),
                candidate=candidate_data.get("candidate")
            )
            await self.pc.addIceCandidate(candidate)
            logger.info(f"✅ Remote ICE candidate added successfully")
        except Exception as e:
            logger.error(f"💥 ICE error: {e} | Candidate data: {candidate_data}", exc_info=True)
            
    def send_signal(self, msg_type: str, data: dict):
        if not self.viewer_id:
            logger.warning("⚠️ Cannot send signal: no viewer_id set")
            return
        payload = {
            "type": msg_type,
            "from": self.peer_id,
            "to": self.viewer_id,
            "data": data
        }
        result = self.mqtt_client.publish(self.signaling_topic, json.dumps(payload))
        logger.info(f"📤 SIGNALING | {msg_type.upper()} -> {self.viewer_id} | MQTT: {result.rc}")
        
    async def cleanup(self):
        logger.info("🧹 Starting cleanup...")
        if self.pc:
            logger.debug("Closing peer connection...")
            await self.pc.close()
            self.pc = None
            logger.info("✅ Peer connection closed")
        if self.local_track:
            logger.debug("Stopping local track...")
            self.local_track.stop()
            self.local_track = None
            logger.info("✅ Local track stopped")
        self.viewer_id = None
        self.connected = False
        self.connection_state = "closed"
        self.ice_connection_state = "closed"
        logger.info("🧹 Cleanup complete")
        
    def connect(self):
        logger.info(f"🔌 Connecting to MQTT broker: {self.broker_url}:{self.broker_port}")
        try:
            self.mqtt_client.connect(self.broker_url, self.broker_port, 60)
            threading.Thread(target=self.mqtt_client.loop_forever, daemon=True).start()
        except Exception as e:
            logger.error(f"❌ MQTT connection failed: {e}", exc_info=True)
        
    def disconnect(self):
        logger.info("🛑 Disconnecting...")
        self.running = False
        
        if self._loop:
            asyncio.run_coroutine_threadsafe(self.cleanup(), self._loop)
        else:
            asyncio.run(self.cleanup())
            
        self.mqtt_client.disconnect()
        logger.info("👋 Disconnected")
        
    def print_status(self):
        while self.running:
            time.sleep(10)
            if self.local_track:
                uptime = time.time() - self.start_time
                frames = self.local_track.frame_count
                actual_fps = frames / uptime if uptime > 0 else 0
                
                status_msg = (
                    f"\n{'='*60}\n"
                    f"📊 STATUS REPORT\n"
                    f"{'='*60}\n"
                    f"⏱️  Uptime:        {int(uptime)}s\n"
                    f"🎥 Frames:         {frames}\n"
                    f"📈 Actual FPS:     {actual_fps:.2f}\n"
                    f"🔗 Connection:     {self.connection_state}\n"
                    f"🧊 ICE State:      {self.ice_connection_state}\n"
                    f"📶 ICE Gathering:  {self.ice_gathering_state}\n"
                    f"📡 Signaling:      {self.signaling_state}\n"
                    f"👁️  Viewer:         {self.viewer_id or 'None'}\n"
                    f"🧊 ICE Candidates: Sent={self.ice_candidates_sent}, "
                    f"Received={self.ice_candidates_received}\n"
                    f"{'='*60}\n"
                )
                logger.info(status_msg)

async def main():
    print("\n" + "="*70)
    print("  WebRTC Remote Camera Source - DEBUG MODE")
    print("="*70)
    print("  Features:")
    print("  • Real camera capture with OpenCV")
    print("  • Detailed ICE and connection state logging")
    print("  • Real-time statistics and debugging info")
    print("  • Thread-safe async operations")
    print("="*70 + "\n")
    
    # Get the event loop
    loop = asyncio.get_running_loop()
    
    camera = RemoteCameraSource(camera_id=0, verbose_ice=True)
    
    # IMPORTANT: Set the event loop reference for thread-safe operations
    camera.set_event_loop(loop)
    
    camera.connect()
    
    # Start status printer
    threading.Thread(target=camera.print_status, daemon=True).start()
    
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
        camera.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
