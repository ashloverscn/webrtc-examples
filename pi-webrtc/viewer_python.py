import asyncio
import json
import time
import uuid
import threading
from typing import Optional
import cv2
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, MediaStreamTrack
import paho.mqtt.client as mqtt

class RemoteCameraViewer:
    """Python-based WebRTC Camera Viewer - receives video from remote camera"""
    
    def __init__(self):
        self.viewer_id = f"viewer_{uuid.uuid4().hex[:6]}"
        
        # MQTT setup
        self.broker_url = "e5122a5328ea4986a0295fa6e037655a.s2.eu.hivemq.cloud"
        self.broker_port = 8883
        self.signaling_topic = "webrtc/signaling"
        self.announce_topic = "camera/announce"
        
        self.mqtt_client = mqtt.Client(client_id=self.viewer_id, protocol=mqtt.MQTTv5)
        self.mqtt_client.tls_set()
        self.mqtt_client.username_pw_set("admin", "admin1234S")
        
        # WebRTC
        self.pc: Optional[RTCPeerConnection] = None
        self.dc = None
        self.camera_id = None
        
        # Video display
        self.remote_frame = None
        self.frame_lock = threading.Lock()
        self.connected = False
        self.running = True
        
        # Stats
        self.frames_received = 0
        self.start_time = None
        
        self.setup_mqtt()
        
    def setup_mqtt(self):
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        
    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        print(f"✅ Viewer connected as {self.viewer_id}")
        client.subscribe([self.signaling_topic, self.announce_topic])
        print("📡 Listening for cameras...")
        
    def on_mqtt_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            
            if msg.topic == self.announce_topic:
                if payload.get("type") == "camera_available":
                    camera_id = payload.get("camera_id")
                    print(f"\n📹 Camera available: {camera_id}")
                    print(f"   Type 'connect {camera_id}' to view")
                    
            elif msg.topic == self.signaling_topic:
                if payload.get("to") != self.viewer_id:
                    return
                    
                msg_type = payload.get("type")
                data = payload.get("data")
                
                if msg_type == "offer":
                    asyncio.create_task(self.handle_offer(data))
                elif msg_type == "ice":
                    asyncio.create_task(self.handle_remote_ice(data))
                    
        except Exception as e:
            print(f"Error: {e}")
            
    async def connect_to_camera(self, camera_id: str):
        """Send connection request to camera"""
        self.camera_id = camera_id
        print(f"🔌 Requesting connection to {camera_id}...")
        
        request = {
            "type": "view_request",
            "from": self.viewer_id,
            "to": camera_id,
            "data": {}
        }
        self.mqtt_client.publish(self.signaling_topic, json.dumps(request))
        
    async def handle_offer(self, offer_data):
        """Handle offer from camera and establish connection"""
        print("🎥 Received camera offer, establishing connection...")
        
        # Create peer connection (recvonly - we only receive)
        self.pc = RTCPeerConnection(configuration={
            "iceServers": [{"urls": "stun:stun.l.google.com:19302"}]
        })
        
        # Handle incoming video track
        @self.pc.on("track")
        def on_track(track):
            print(f"📹 Video track received: {track.kind}")
            if track.kind == "video":
                asyncio.create_task(self.receive_video(track))
                
        @self.pc.on("icecandidate")
        async def on_icecandidate(candidate):
            if candidate:
                self.send_signaling_message("ice", candidate)
                
        @self.pc.on("iceconnectionstatechange")
        async def on_ice_state_change():
            print(f"🧊 ICE State: {self.pc.iceConnectionState}")
            if self.pc.iceConnectionState == "connected":
                self.connected = True
                self.start_time = time.time()
                print("✅ Connected to camera!")
            elif self.pc.iceConnectionState in ["failed", "disconnected", "closed"]:
                self.connected = False
                print("❌ Disconnected from camera")
                
        # Handle data channel (camera control)
        @self.pc.on("datachannel")
        def on_datachannel(channel):
            self.dc = channel
            @self.dc.on("open")
            def on_open():
                print("📡 Control channel opened")
                # Request camera info
                self.dc.send(json.dumps({"action": "get_info"}))
            @self.dc.on("message")
            def on_message(message):
                try:
                    data = json.loads(message)
                    if data.get("type") == "info":
                        info = data.get("data", {})
                        print(f"\n📊 Camera Info:")
                        print(f"   Resolution: {info.get('resolution', 'unknown')}")
                        print(f"   Uptime: {info.get('uptime', 0):.1f}s")
                        print(f"   Frames sent: {info.get('frames_sent', 0)}")
                except:
                    print(f"📩 Camera message: {message}")
        
        # Set remote description and create answer
        offer = RTCSessionDescription(sdp=offer_data["sdp"], type=offer_data["type"])
        await self.pc.setRemoteDescription(offer)
        
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        
        # Send answer
        self.send_signaling_message("answer", {"sdp": answer.sdp, "type": answer.type})
        print("✅ Answer sent, waiting for video...")
        
    async def receive_video(self, track):
        """Receive and display video frames"""
        print("🎥 Video stream started")
        
        while True:
            try:
                frame = await track.recv()
                img = frame.to_ndarray(format="bgr24")
                
                with self.frame_lock:
                    self.remote_frame = img
                    self.frames_received += 1
                    
            except Exception as e:
                print(f"Video error: {e}")
                break
                
        print("🎥 Video stream ended")
        
    async def handle_remote_ice(self, candidate_data):
        """Handle ICE candidate from camera"""
        try:
            candidate = RTCIceCandidate(
                sdpMid=candidate_data.get("sdpMid"),
                sdpMLineIndex=candidate_data.get("sdpMLineIndex"),
                candidate=candidate_data.get("candidate")
            )
            await self.pc.addIceCandidate(candidate)
        except Exception as e:
            print(f"ICE error: {e}")
            
    def send_signaling_message(self, msg_type: str, data: dict):
        """Send signaling message to camera"""
        if not self.camera_id:
            return
        payload = {
            "type": msg_type,
            "from": self.viewer_id,
            "to": self.camera_id,
            "data": data
        }
        self.mqtt_client.publish(self.signaling_topic, json.dumps(payload))
        
    def display_loop(self):
        """Display received video in OpenCV window"""
        window_name = f"Remote Camera Viewer - {self.viewer_id}"
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window_name, 640, 480)
        
        while self.running:
            with self.frame_lock:
                frame = self.remote_frame.copy() if self.remote_frame is not None else None
                
            if frame is not None:
                # Add overlay info
                h, w = frame.shape[:2]
                overlay = frame.copy()
                
                # Status bar
                cv2.rectangle(overlay, (0, 0), (w, 40), (0, 0, 0), -1)
                cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)
                
                status = "LIVE" if self.connected else "DISCONNECTED"
                color = (0, 255, 0) if self.connected else (0, 0, 255)
                cv2.putText(frame, f"Status: {status}", (10, 30), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
                
                if self.connected and self.start_time:
                    uptime = time.time() - self.start_time
                    fps = self.frames_received / uptime if uptime > 0 else 0
                    info_text = f"Frames: {self.frames_received} | FPS: {fps:.1f}"
                    cv2.putText(frame, info_text, (w - 300, 30), 
                               cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
                
                cv2.imshow(window_name, frame)
            else:
                # Show waiting screen
                waiting = np.zeros((480, 640, 3), dtype=np.uint8)
                cv2.putText(waiting, "Waiting for video...", (150, 240), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
                if self.camera_id:
                    cv2.putText(waiting, f"Camera: {self.camera_id}", (200, 280), 
                               cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
                cv2.imshow(window_name, waiting)
                
            if cv2.waitKey(30) & 0xFF == ord('q'):
                self.running = False
                break
                
        cv2.destroyWindow(window_name)
        
    def command_loop(self):
        """Handle user commands"""
        print("\nCommands:")
        print("  connect <camera_id> - Connect to camera")
        print("  disconnect          - Disconnect from camera")
        print("  ping                - Ping camera")
        print("  info                - Get camera info")
        print("  quit                - Exit")
        print()
        
        while self.running:
            try:
                cmd = input("> ").strip().split()
                if not cmd:
                    continue
                    
                if cmd[0] == "connect" and len(cmd) > 1:
                    asyncio.create_task(self.connect_to_camera(cmd[1]))
                elif cmd[0] == "disconnect":
                    if self.pc:
                        asyncio.create_task(self.pc.close())
                    self.connected = False
                    print("Disconnected")
                elif cmd[0] == "ping":
                    if self.dc and self.dc.readyState == "open":
                        self.dc.send(json.dumps({"action": "ping"}))
                        print("Ping sent")
                    else:
                        print("Not connected")
                elif cmd[0] == "info":
                    if self.dc and self.dc.readyState == "open":
                        self.dc.send(json.dumps({"action": "get_info"}))
                    else:
                        print("Not connected")
                elif cmd[0] == "quit":
                    self.running = False
                    break
                else:
                    print("Unknown command")
                    
            except Exception as e:
                print(f"Error: {e}")
                
    def connect(self):
        self.mqtt_client.connect(self.broker_url, self.broker_port, 60)
        threading.Thread(target=self.mqtt_client.loop_forever, daemon=True).start()
        
    def disconnect(self):
        self.running = False
        if self.pc:
            asyncio.create_task(self.pc.close())
        self.mqtt_client.disconnect()

async def main():
    print("="*60)
    print("WebRTC Remote Camera Viewer (Python)")
    print("="*60)
    print("\nThis viewer connects to remote WebRTC cameras")
    print("Press 'q' in video window to exit")
    print("="*60 + "\n")
    
    viewer = RemoteCameraViewer()
    viewer.connect()
    
    # Start display loop in thread
    display_thread = threading.Thread(target=viewer.display_loop, daemon=True)
    display_thread.start()
    
    # Run command loop in main thread
    viewer.command_loop()
    
    print("\nShutting down...")
    viewer.disconnect()

if __name__ == "__main__":
    asyncio.run(main())