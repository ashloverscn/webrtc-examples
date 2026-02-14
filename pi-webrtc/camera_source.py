import asyncio
import json
import time
import uuid
import threading
from typing import Dict, Optional
import cv2
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, MediaStreamTrack
import paho.mqtt.client as mqtt
from av import VideoFrame

class CameraVideoStreamTrack(MediaStreamTrack):
    """Video stream track that captures from OpenCV camera."""
    kind = "video"
    
    def __init__(self, camera_id=0):
        super().__init__()
        self.camera_id = camera_id
        self.cap = cv2.VideoCapture(camera_id)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        print(f"📹 Camera initialized: {self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)}x{self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)}")
        
    async def recv(self):
        pts, time_base = await self.next_timestamp()
        
        ret, frame = self.cap.read()
        if not ret:
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(frame, "Camera Error", (50, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        video_frame = VideoFrame.from_ndarray(frame, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        
        return video_frame
    
    def stop(self):
        super().stop()
        self.cap.release()
        print("📹 Camera released")

class RemoteCameraSource:
    """WebRTC Camera Source - sends video to viewers, receives commands via data channel"""
    
    def __init__(self, camera_id=0):
        self.peer_id = f"camera_{uuid.uuid4().hex[:6]}"
        self.camera_id = camera_id
        
        # MQTT setup
        self.broker_url = "e5122a5328ea4986a0295fa6e037655a.s2.eu.hivemq.cloud"
        self.broker_port = 8883
        self.signaling_topic = "webrtc/signaling"
        self.announce_topic = "camera/announce"
        
        self.mqtt_client = mqtt.Client(client_id=f"cam_{self.peer_id}", protocol=mqtt.MQTTv5)
        self.mqtt_client.tls_set()
        self.mqtt_client.username_pw_set("admin", "admin1234S")
        
        # WebRTC
        self.pc: Optional[RTCPeerConnection] = None
        self.dc = None
        self.local_track = None
        self.viewer_id = None  # Who is watching us
        
        self.running = True
        self.connected = False
        
        # Stats
        self.frames_sent = 0
        self.start_time = time.time()
        
        self.setup_mqtt()
        
    def setup_mqtt(self):
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
        
    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        print(f"✅ Camera source connected as {self.peer_id}")
        client.subscribe(self.signaling_topic)
        
        # Announce camera availability
        self.announce_camera()
        
        # Keep announcing every 5 seconds
        threading.Thread(target=self.announce_loop, daemon=True).start()
        
    def announce_camera(self):
        """Announce that this camera is available"""
        announcement = {
            "type": "camera_available",
            "camera_id": self.peer_id,
            "timestamp": time.time()
        }
        self.mqtt_client.publish(self.announce_topic, json.dumps(announcement))
        print(f"📢 Announced camera: {self.peer_id}")
        
    def announce_loop(self):
        while self.running:
            time.sleep(5)
            if not self.connected:
                self.announce_camera()
        
    def on_mqtt_disconnect(self, client, userdata, rc):
        print("⚠️ Disconnected from MQTT")
        
    def on_mqtt_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            
            # Only handle messages intended for us
            if payload.get("to") != self.peer_id:
                return
                
            msg_type = payload.get("type")
            from_peer = payload.get("from")
            data = payload.get("data")
            
            print(f"📥 {msg_type.upper()} from viewer {from_peer}")
            
            if msg_type == "view_request":
                # Viewer wants to connect
                self.viewer_id = from_peer
                asyncio.create_task(self.handle_view_request())
            elif msg_type == "answer":
                asyncio.create_task(self.handle_answer(data))
            elif msg_type == "ice":
                asyncio.create_task(self.handle_remote_ice(data))
                
        except Exception as e:
            print(f"Error handling message: {e}")
            
    async def handle_view_request(self):
        """Handle a viewer requesting to watch our camera"""
        print(f"🎥 Viewer {self.viewer_id} requested stream")
        
        # Create peer connection
        self.pc = RTCPeerConnection(configuration={
            "iceServers": [{"urls": "stun:stun.l.google.com:19302"}]
        })
        
        # Add camera track
        self.local_track = CameraVideoStreamTrack(self.camera_id)
        self.pc.addTrack(self.local_track)
        
        # Create data channel for receiving commands (PTZ, settings, etc.)
        self.dc = self.pc.createDataChannel("camera_control")
        self.setup_data_channel()
        
        @self.pc.on("iceconnectionstatechange")
        async def on_ice_state_change():
            print(f"🧊 ICE State: {self.pc.iceConnectionState}")
            if self.pc.iceConnectionState == "connected":
                self.connected = True
                print("✅ Viewer connected!")
            elif self.pc.iceConnectionState in ["failed", "disconnected", "closed"]:
                self.connected = False
                print("❌ Viewer disconnected")
                await self.cleanup()
        
        # Create and send offer
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        
        self.send_signal("offer", {"sdp": offer.sdp, "type": offer.type})
        
    def setup_data_channel(self):
        """Setup data channel to receive commands from viewer"""
        @self.dc.on("message")
        def on_message(message):
            print(f"📩 Command from viewer: {message}")
            self.handle_command(message)
            
        @self.dc.on("open")
        def on_open():
            print("📡 Control channel opened")
            if self.dc:
                self.dc.send(f"Camera {self.peer_id} ready")
            
        @self.dc.on("close")
        def on_close():
            print("📡 Control channel closed")
            
    def handle_command(self, cmd):
        """Handle commands from viewer (PTZ, resolution, etc.)"""
        try:
            data = json.loads(cmd) if isinstance(cmd, str) else cmd
            action = data.get("action")
            
            if action == "get_info":
                info = {
                    "camera_id": self.peer_id,
                    "uptime": time.time() - self.start_time,
                    "frames_sent": self.frames_sent,
                    "resolution": "640x480",
                    "fps": 30
                }
                if self.dc:
                    self.dc.send(json.dumps({"type": "info", "data": info}))
                    
            elif action == "ping":
                if self.dc:
                    self.dc.send(json.dumps({"type": "pong", "time": time.time()}))
                    
            else:
                print(f"Unknown command: {action}")
                
        except Exception as e:
            print(f"Error handling command: {e}")
            
    async def handle_answer(self, answer_data):
        """Handle answer from viewer"""
        answer = RTCSessionDescription(sdp=answer_data["sdp"], type=answer_data["type"])
        await self.pc.setRemoteDescription(answer)
        print("✅ Viewer answer processed")
        
    async def handle_remote_ice(self, candidate_data):
        """Handle ICE candidate from viewer"""
        try:
            candidate = RTCIceCandidate(
                sdpMid=candidate_data.get("sdpMid"),
                sdpMLineIndex=candidate_data.get("sdpMLineIndex"),
                candidate=candidate_data.get("candidate")
            )
            await self.pc.addIceCandidate(candidate)
        except Exception as e:
            print(f"ICE error: {e}")
            
    def send_signal(self, msg_type: str, data: dict):
        """Send signaling message to viewer"""
        if not self.viewer_id:
            return
        payload = {
            "type": msg_type,
            "from": self.peer_id,
            "to": self.viewer_id,
            "data": data
        }
        self.mqtt_client.publish(self.signaling_topic, json.dumps(payload))
        print(f"📤 {msg_type.upper()} -> {self.viewer_id}")
        
    async def cleanup(self):
        """Cleanup resources"""
        if self.pc:
            await self.pc.close()
            self.pc = None
        if self.local_track:
            self.local_track.stop()
            self.local_track = None
        self.viewer_id = None
        self.connected = False
        print("🧹 Cleanup complete")
        
    def connect(self):
        """Connect to MQTT broker"""
        self.mqtt_client.connect(self.broker_url, self.broker_port, 60)
        threading.Thread(target=self.mqtt_client.loop_forever, daemon=True).start()
        
    def disconnect(self):
        """Disconnect and cleanup"""
        self.running = False
        asyncio.create_task(self.cleanup())
        self.mqtt_client.disconnect()
        
    def print_status(self):
        """Print current status"""
        while self.running:
            time.sleep(10)
            if self.connected:
                uptime = time.time() - self.start_time
                print(f"📊 Status: Streaming to {self.viewer_id} | Uptime: {int(uptime)}s")

async def main():
    print("="*50)
    print("WebRTC Remote Camera Source")
    print("="*50)
    print("This application streams your camera to WebRTC viewers")
    print("Press Ctrl+C to exit")
    print("="*50 + "\n")
    
    camera = RemoteCameraSource(camera_id=0)
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