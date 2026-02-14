import asyncio
import json
import time
import uuid
import threading
from datetime import datetime
from typing import Dict, Optional
import cv2
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, MediaStreamTrack
from aiortc.contrib.media import MediaPlayer, MediaRelay
from aiortc.rtcrtpsender import RTCRtpSender
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
        
    async def recv(self):
        pts, time_base = await self.next_timestamp()
        
        ret, frame = self.cap.read()
        if not ret:
            # Return black frame if camera fails
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
        
        # Convert BGR to RGB
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        # Create video frame
        video_frame = VideoFrame.from_ndarray(frame, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        
        return video_frame
    
    def stop(self):
        super().stop()
        self.cap.release()

class WebRTCClient:
    def __init__(self):
        self.peer_id = f"peer_{uuid.uuid4().hex[:6]}"
        self.broker_url = "e5122a5328ea4986a0295fa6e037655a.s2.eu.hivemq.cloud"
        self.broker_port = 8883
        self.signaling_topic = "webrtc/signaling"
        self.public_chat_topic = "PublicChat"
        self.history_topic = "Last1008MSGS"
        
        self.mqtt_client = mqtt.Client(client_id=f"signaling_{self.peer_id}", protocol=mqtt.MQTTv5)
        self.mqtt_client.tls_set()
        self.mqtt_client.username_pw_set("admin", "admin1234S")
        
        self.pc: Optional[RTCPeerConnection] = None
        self.dc = None
        self.local_track = None
        self.remote_video_track = None
        
        self.peer_registry: Dict[str, float] = {}
        self.message_buffer = []
        self.target_peer_id = ""
        self.running = True
        
        # Callbacks for UI
        self.on_public_message = None
        self.on_private_message = None
        self.on_peer_list_update = None
        self.on_connection_state_change = None
        
        self.setup_mqtt()
        
    def setup_mqtt(self):
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
        
    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        print(f"✅ Connected to MQTT as {self.peer_id}")
        client.subscribe([(self.signaling_topic, 0), (self.public_chat_topic, 0), (self.history_topic, 0)])
        print("📡 Subscribed to topics")
        
        # Start presence announcements
        self.send_presence()
        threading.Thread(target=self.presence_loop, daemon=True).start()
        
    def on_mqtt_disconnect(self, client, userdata, rc):
        print("⚠️ Disconnected from MQTT")
        
    def on_mqtt_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            topic = msg.topic
            
            if topic == self.signaling_topic and payload.get("type") == "presence":
                self.peer_registry[payload["from"]] = time.time()
                return
                
            if topic == self.history_topic:
                if isinstance(payload, list):
                    self.message_buffer = payload
                    if self.on_public_message:
                        self.on_public_message(self.message_buffer)
                return
                
            if topic == self.public_chat_topic:
                if not all(k in payload for k in ["text", "from", "timestamp"]):
                    return
                if not any(m.get("timestamp") == payload["timestamp"] for m in self.message_buffer):
                    self.message_buffer.append(payload)
                    if len(self.message_buffer) > 1008:
                        self.message_buffer.pop(0)
                    self.publish_buffer()
                    if self.on_public_message:
                        self.on_public_message(self.message_buffer)
                return
            
            # Handle signaling messages
            if payload.get("to") != self.peer_id:
                return
                
            msg_type = payload.get("type")
            from_peer = payload.get("from")
            data = payload.get("data")
            
            print(f"📥 Received {msg_type.upper()} from {from_peer}")
            self.target_peer_id = from_peer
            
            if msg_type == "offer":
                asyncio.create_task(self.handle_offer(data))
            elif msg_type == "answer":
                asyncio.create_task(self.handle_answer(data))
            elif msg_type == "ice":
                asyncio.create_task(self.handle_remote_ice(data))
                
        except Exception as e:
            print(f"Error processing message: {e}")
            
    def send_presence(self):
        presence = json.dumps({"type": "presence", "from": self.peer_id})
        self.mqtt_client.publish(self.signaling_topic, presence)
        
    def presence_loop(self):
        while self.running:
            time.sleep(1)
            self.send_presence()
            self.update_peer_list()
            
    def update_peer_list(self):
        now = time.time()
        peers = {}
        for pid, last_seen in list(self.peer_registry.items()):
            if pid == self.peer_id:
                continue
            if now - last_seen > 5:
                del self.peer_registry[pid]
                continue
            peers[pid] = "online" if now - last_seen <= 1 else "disconnected"
        
        if self.on_peer_list_update:
            self.on_peer_list_update(peers)
            
    def publish_buffer(self):
        self.mqtt_client.publish(self.history_topic, json.dumps(self.message_buffer), retain=True)
        
    def send_public_message(self, text):
        if not text.strip():
            return
        msg_obj = {
            "from": self.peer_id,
            "text": text,
            "timestamp": int(time.time() * 1000)
        }
        self.mqtt_client.publish(self.public_chat_topic, json.dumps(msg_obj))
        self.message_buffer.append(msg_obj)
        if len(self.message_buffer) > 1008:
            self.message_buffer.pop(0)
        self.publish_buffer()
        if self.on_public_message:
            self.on_public_message(self.message_buffer)
            
    async def setup_peer_connection(self, is_offerer: bool):
        self.pc = RTCPeerConnection(configuration={
            "iceServers": [{"urls": "stun:stun.l.google.com:19302"}]
        })
        
        # Add local video track
        self.local_track = CameraVideoStreamTrack()
        self.pc.addTrack(self.local_track)
        
        # Data channel
        if is_offerer:
            self.dc = self.pc.createDataChannel("terminal")
            self.setup_data_channel()
        else:
            @self.pc.on("datachannel")
            def on_datachannel(channel):
                self.dc = channel
                self.setup_data_channel()
                
        @self.pc.on("track")
        def on_track(track):
            print(f"📹 Received remote track: {track.kind}")
            if not self.remote_video_track:
                self.remote_video_track = track
                # Start receiving frames
                asyncio.create_task(self.receive_remote_video(track))
            if self.on_connection_state_change:
                self.on_connection_state_change("connected", track)
                
        if is_offerer:
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)
            self.send_signal("offer", {"sdp": offer.sdp, "type": offer.type})
            
    async def receive_remote_video(self, track):
        """Receive and display remote video frames"""
        print("🎥 Remote video stream started")
        frame_count = 0
        while True:
            try:
                frame = await track.recv()
                frame_count += 1
                # Convert to OpenCV format for display
                img = frame.to_ndarray(format="bgr24")
                
                # Display every 30th frame to avoid flooding console
                if frame_count % 30 == 0:
                    print(f"📹 Received frame {frame_count} ({img.shape[1]}x{img.shape[0]})")
                
                # Show in window (optional - can comment out if no GUI needed)
                cv2.imshow("Remote Video", img)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
                    
            except Exception as e:
                print(f"Remote video error: {e}")
                break
        cv2.destroyWindow("Remote Video")
        
    def setup_data_channel(self):
        @self.dc.on("message")
        def on_message(message):
            print(f"> {message}")
            if self.on_private_message:
                self.on_private_message(message)
                
        @self.dc.on("open")
        def on_open():
            print("=== DataChannel Opened ===")
            if self.on_connection_state_change:
                self.on_connection_state_change("datachannel_open", None)
                
        @self.dc.on("close")
        def on_close():
            print("=== DataChannel Closed ===")
            
    async def initiate_call(self, target_peer: str):
        self.target_peer_id = target_peer
        await self.setup_peer_connection(is_offerer=True)
        
    async def handle_offer(self, offer_data):
        await self.setup_peer_connection(is_offerer=False)
        offer = RTCSessionDescription(sdp=offer_data["sdp"], type=offer_data["type"])
        await self.pc.setRemoteDescription(offer)
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        self.send_signal("answer", {"sdp": answer.sdp, "type": answer.type})
        
    async def handle_answer(self, answer_data):
        answer = RTCSessionDescription(sdp=answer_data["sdp"], type=answer_data["type"])
        await self.pc.setRemoteDescription(answer)
        
    async def handle_remote_ice(self, candidate_data):
        candidate = RTCIceCandidate(
            sdpMid=candidate_data.get("sdpMid"),
            sdpMLineIndex=candidate_data.get("sdpMLineIndex"),
            candidate=candidate_data.get("candidate")
        )
        await self.pc.addIceCandidate(candidate)
        
    def send_signal(self, msg_type: str, data: dict):
        if not self.target_peer_id:
            print("⚠️ Select a peer first.")
            return
        payload = {
            "type": msg_type,
            "from": self.peer_id,
            "to": self.target_peer_id,
            "data": data
        }
        self.mqtt_client.publish(self.signaling_topic, json.dumps(payload))
        print(f"📤 Sent {msg_type.upper()} to {self.target_peer_id}")
        
    def send_private_message(self, text):
        if self.dc and self.dc.readyState == "open":
            self.dc.send(text)
            print(f"< {text}")
            if self.on_private_message:
                self.on_private_message(f"[me]: {text}")
        else:
            print("⚠️ DataChannel not open")
                
    def connect(self):
        self.mqtt_client.connect(self.broker_url, self.broker_port, 60)
        threading.Thread(target=self.mqtt_client.loop_forever, daemon=True).start()
        
    def disconnect(self):
        self.running = False
        if self.pc:
            asyncio.create_task(self.pc.close())
        if self.local_track:
            self.local_track.stop()
        self.mqtt_client.disconnect()

# CLI Interface
class CLIInterface:
    def __init__(self):
        self.client = WebRTCClient()
        self.client.on_public_message = self.on_public_msg
        self.client.on_private_message = self.on_private_msg
        self.client.on_peer_list_update = self.on_peer_update
        
    def on_public_msg(self, messages):
        print("\n" + "="*50)
        print("PUBLIC CHAT")
        print("="*50)
        for msg in sorted(messages, key=lambda x: x.get("timestamp", 0))[-10:]:  # Last 10
            prefix = "[me]" if msg["from"] == self.client.peer_id else f"[{msg['from']}]"
            time_str = datetime.fromtimestamp(msg["timestamp"]/1000).strftime("%H:%M:%S")
            print(f"{time_str} {prefix}: {msg['text']}")
        print("="*50 + "\n")
        
    def on_private_msg(self, msg):
        print(f"\n[Private] {msg}\n")
        
    def on_peer_update(self, peers):
        if peers:
            print("\n" + "-"*30)
            print("ONLINE PEERS")
            print("-"*30)
            for pid, status in peers.items():
                indicator = "🟢" if status == "online" else "🟡"
                print(f"{indicator} {pid} ({status})")
            print("-"*30 + "\n")
            
    async def run(self):
        self.client.connect()
        print(f"\n🚀 WebRTC Client started as {self.client.peer_id}")
        print("Commands:")
        print("  /call <peer_id>  - Initiate video call")
        print("  /msg <text>      - Send private message (after call)")
        print("  /pub <text>      - Send public message")
        print("  /peers           - List online peers")
        print("  /local           - Show local camera (OpenCV window)")
        print("  /quit            - Exit\n")
        
        # Start local camera preview in background
        self.show_local = False
        self.local_cap = None
        
        while True:
            try:
                cmd = await asyncio.get_event_loop().run_in_executor(
                    None, input, 
                    "> "
                )
                cmd = cmd.strip()
                
                if cmd.startswith("/call "):
                    target = cmd[6:].strip()
                    if target:
                        await self.client.initiate_call(target)
                    else:
                        print("⚠️ Usage: /call <peer_id>")
                        
                elif cmd.startswith("/msg "):
                    msg = cmd[5:]
                    if msg:
                        self.client.send_private_message(msg)
                    else:
                        print("⚠️ Usage: /msg <text>")
                        
                elif cmd.startswith("/pub "):
                    msg = cmd[5:]
                    if msg:
                        self.client.send_public_message(msg)
                    else:
                        print("⚠️ Usage: /pub <text>")
                        
                elif cmd == "/peers":
                    self.on_peer_update({k: "online" if time.time() - v <= 1 else "disconnected" 
                                        for k, v in self.client.peer_registry.items() if k != self.client.peer_id})
                                        
                elif cmd == "/local":
                    # Show local camera in OpenCV window
                    if not self.show_local:
                        self.show_local = True
                        self.local_cap = cv2.VideoCapture(0)
                        print("📹 Opening local camera window... (press 'q' in window to close)")
                        threading.Thread(target=self._show_local_camera, daemon=True).start()
                    else:
                        print("⚠️ Local camera already showing")
                        
                elif cmd == "/quit":
                    self.show_local = False
                    if self.local_cap:
                        self.local_cap.release()
                    self.client.disconnect()
                    break
                    
                else:
                    print("Unknown command. Type /quit to exit.")
                    
            except Exception as e:
                print(f"Error: {e}")
                
        print("Goodbye!")
        
    def _show_local_camera(self):
        """Show local camera in separate thread"""
        while self.show_local and self.local_cap and self.local_cap.isOpened():
            ret, frame = self.local_cap.read()
            if ret:
                cv2.imshow("Local Camera", frame)
                if cv2.waitKey(30) & 0xFF == ord('q'):
                    self.show_local = False
                    break
        cv2.destroyWindow("Local Camera")
        if self.local_cap:
            self.local_cap.release()

if __name__ == "__main__":
    cli = CLIInterface()
    asyncio.run(cli.run())

