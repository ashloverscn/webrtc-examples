import asyncio
import json
import time
import uuid
import threading
import queue
from typing import Dict, Optional
import cv2
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, MediaStreamTrack
from aiortc.contrib.media import MediaPlayer, MediaRelay
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
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
        
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
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
        self.remote_frame = None
        self.remote_frame_lock = threading.Lock()
        
        self.peer_registry: Dict[str, float] = {}
        self.message_buffer = []
        self.target_peer_id = ""
        self.running = True
        self.connected = False
        
        self.setup_mqtt()
        
    def setup_mqtt(self):
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
        
    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        print(f"✅ Connected to MQTT as {self.peer_id}")
        client.subscribe([(self.signaling_topic, 0), (self.public_chat_topic, 0), (self.history_topic, 0)])
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
                    self.message_buffer = payload[-50:]  # Keep last 50
                return
                
            if topic == self.public_chat_topic:
                if not all(k in payload for k in ["text", "from", "timestamp"]):
                    return
                if not any(m.get("timestamp") == payload["timestamp"] for m in self.message_buffer):
                    self.message_buffer.append(payload)
                    if len(self.message_buffer) > 1008:
                        self.message_buffer.pop(0)
                    self.publish_buffer()
                return
            
            if payload.get("to") != self.peer_id:
                return
                
            msg_type = payload.get("type")
            from_peer = payload.get("from")
            data = payload.get("data")
            
            print(f"📥 {msg_type.upper()} from {from_peer}")
            self.target_peer_id = from_peer
            
            if msg_type == "offer":
                asyncio.create_task(self.handle_offer(data))
            elif msg_type == "answer":
                asyncio.create_task(self.handle_answer(data))
            elif msg_type == "ice":
                asyncio.create_task(self.handle_remote_ice(data))
                
        except Exception as e:
            print(f"Error: {e}")
            
    def send_presence(self):
        presence = json.dumps({"type": "presence", "from": self.peer_id})
        self.mqtt_client.publish(self.signaling_topic, presence)
        
    def presence_loop(self):
        while self.running:
            time.sleep(1)
            self.send_presence()
            
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
            
    async def setup_peer_connection(self, is_offerer: bool):
        self.pc = RTCPeerConnection(configuration={
            "iceServers": [{"urls": "stun:stun.l.google.com:19302"}]
        })
        
        self.local_track = CameraVideoStreamTrack()
        self.pc.addTrack(self.local_track)
        
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
            print(f"📹 Remote track received: {track.kind}")
            if track.kind == "video":
                asyncio.create_task(self.handle_remote_track(track))
                
        if is_offerer:
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)
            self.send_signal("offer", {"sdp": offer.sdp, "type": offer.type})
            
    async def handle_remote_track(self, track):
        while True:
            try:
                frame = await track.recv()
                img = frame.to_ndarray(format="bgr24")
                with self.remote_frame_lock:
                    self.remote_frame = img
            except Exception as e:
                print(f"Track error: {e}")
                break
                
    def setup_data_channel(self):
        @self.dc.on("message")
        def on_message(message):
            print(f"[Private] > {message}")
            
        @self.dc.on("open")
        def on_open():
            print("=== DataChannel Opened ===")
            self.connected = True
            
        @self.dc.on("close")
        def on_close():
            print("=== DataChannel Closed ===")
            self.connected = False
            
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
        if not self.target_peer_id:
            print("⚠️ No target peer")
            return
        payload = {
            "type": msg_type,
            "from": self.peer_id,
            "to": self.target_peer_id,
            "data": data
        }
        self.mqtt_client.publish(self.signaling_topic, json.dumps(payload))
        print(f"📤 {msg_type.upper()} -> {self.target_peer_id}")
        
    def send_private_message(self, text):
        if self.dc and self.dc.readyState == "open":
            self.dc.send(text)
            print(f"[Private] < {text}")
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

class GUIApp:
    def __init__(self):
        self.client = WebRTCClient()
        self.show_local = True
        self.show_chat = True
        self.window_name = "WebRTC Camera Viewer - Python"
        self.input_buffer = ""
        self.input_mode = None  # None, "call", "msg", "pub"
        self.peers_list = []
        self.selected_peer_idx = -1
        
    def get_remote_frame(self):
        with self.client.remote_frame_lock:
            return self.client.remote_frame.copy() if self.client.remote_frame is not None else None
        
    def draw_ui(self, local_frame, remote_frame):
        h, w = 480, 640
        
        # Prepare local view (mirrored)
        if local_frame is not None:
            local_view = cv2.flip(local_frame, 1)
            local_view = cv2.resize(local_view, (w//2, h//2))
        else:
            local_view = np.zeros((h//2, w//2, 3), dtype=np.uint8)
            
        # Add label
        cv2.putText(local_view, "LOCAL", (10, 30), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            
        # Prepare remote view
        if remote_frame is not None:
            remote_view = cv2.resize(remote_frame, (w//2, h//2))
        else:
            remote_view = np.zeros((h//2, w//2, 3), dtype=np.uint8)
            cv2.putText(remote_view, "NO REMOTE VIDEO", (w//4 - 80, h//4), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (100, 100, 100), 1)
            cv2.putText(remote_view, "Press 'C' to call", (w//4 - 70, h//4 + 30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)
        
        # Add label
        status_color = (0, 255, 0) if self.client.connected else (0, 0, 255)
        status_text = "REMOTE (Connected)" if self.client.connected else "REMOTE (Disconnected)"
        cv2.putText(remote_view, status_text, (10, 30), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, status_color, 2)
        
        # Combine videos side by side
        video_row = np.hstack([local_view, remote_view])
        
        # Create info panel
        info_panel = np.zeros((180, w, 3), dtype=np.uint8)
        
        # Draw peer info
        y_offset = 30
        cv2.putText(info_panel, f"My ID: {self.client.peer_id}", (10, y_offset), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 1)
        y_offset += 30
        target = self.client.target_peer_id or "None"
        cv2.putText(info_panel, f"Target: {target[:20]}", (10, y_offset), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)
        y_offset += 30
        status = "CONNECTED" if self.client.connected else "DISCONNECTED"
        color = (0, 255, 0) if self.client.connected else (0, 0, 255)
        cv2.putText(info_panel, f"Status: {status}", (10, y_offset), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        
        # Draw peers list (right side)
        x_offset = 350
        cv2.putText(info_panel, "ONLINE PEERS (Press 1-9):", (x_offset, 30), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)
        y = 60
        self.peers_list = []
        
        # Sort peers by online status
        sorted_peers = sorted(
            [(pid, last_seen) for pid, last_seen in self.client.peer_registry.items() 
             if pid != self.client.peer_id],
            key=lambda x: time.time() - x[1]
        )
        
        for i, (pid, last_seen) in enumerate(sorted_peers[:8]):  # Max 8 peers
            self.peers_list.append(pid)
            is_online = time.time() - last_seen <= 1
            is_selected = pid == self.client.target_peer_id
            
            if is_selected:
                prefix = ">>"
                color = (0, 255, 255)
            else:
                prefix = f"{i+1}."
                color = (0, 255, 0) if is_online else (0, 165, 255)
                
            status_symbol = "🟢" if is_online else "🟡"
            text = f"{prefix} {pid[:12]}..."
            cv2.putText(info_panel, text, (x_offset, y), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1 if not is_selected else 2)
            y += 25
        
        # Create chat panel
        chat_panel = np.zeros((220, w, 3), dtype=np.uint8)
        cv2.putText(chat_panel, "PUBLIC CHAT (Press 'P' to send):", (10, 25), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
        
        y = 55
        # Show last 6 messages
        recent_msgs = sorted(self.client.message_buffer, 
                           key=lambda x: x.get("timestamp", 0))[-6:]
        
        for msg in recent_msgs:
            is_me = msg["from"] == self.client.peer_id
            prefix = "[me]" if is_me else f"[{msg['from'][:6]}]"
            text = f"{prefix}: {msg['text'][:45]}"
            color = (0, 255, 100) if is_me else (200, 200, 200)
            cv2.putText(chat_panel, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
            y += 28
        
        # Combine all panels
        display = np.vstack([video_row, info_panel, chat_panel])
        
        # Input overlay if in input mode
        if self.input_mode:
            overlay = display.copy()
            h_total, w_total = display.shape[:2]
            # Draw input box at bottom
            cv2.rectangle(overlay, (0, h_total-60), (w_total, h_total), (40, 40, 40), -1)
            cv2.addWeighted(overlay, 0.85, display, 0.15, 0, display)
            
            prompts = {
                "call": "Enter peer ID to call:",
                "msg": "Enter private message:",
                "pub": "Enter public message:"
            }
            prompt = prompts.get(self.input_mode, "Input:")
            full_text = f"{prompt} {self.input_buffer}_"
            cv2.putText(display, full_text, (10, h_total-25), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        
        # Instructions bar
        instructions = np.zeros((35, w, 3), dtype=np.uint8)
        help_text = "C-Call | M-Message | P-Public | 1-9-Select | Q-Quit"
        cv2.putText(instructions, help_text, (10, 25), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
        
        display = np.vstack([display, instructions])
        
        return display
        
    async def run(self):
        self.client.connect()
        print(f"🚀 Started as {self.client.peer_id}")
        print("Opening GUI window...")
        
        cap = cv2.VideoCapture(0)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        
        cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(self.window_name, 640, 800)
        
        last_remote_check = 0
        
        while True:
            ret, local_frame = cap.read()
            if not ret:
                local_frame = np.zeros((480, 640, 3), dtype=np.uint8)
            
            # Get remote frame (thread-safe)
            remote_frame = self.get_remote_frame()
            
            # Create display
            display = self.draw_ui(local_frame, remote_frame)
            
            cv2.imshow(self.window_name, display)
            
            # Handle input with shorter timeout for responsiveness
            key = cv2.waitKey(1) & 0xFF
            
            if self.input_mode:
                if key == 27:  # ESC - cancel
                    self.input_mode = None
                    self.input_buffer = ""
                elif key == 13:  # Enter - submit
                    await self.handle_input_submit()
                elif key == 8:  # Backspace
                    self.input_buffer = self.input_buffer[:-1]
                elif 32 <= key <= 126:  # Printable chars
                    self.input_buffer += chr(key)
            else:
                if key == ord('q') or key == 27:  # Q or ESC
                    break
                elif key == ord('c'):
                    self.input_mode = "call"
                    self.input_buffer = ""
                elif key == ord('m'):
                    if self.client.target_peer_id:
                        self.input_mode = "msg"
                        self.input_buffer = ""
                    else:
                        print("⚠️ Select a peer first (press 1-9)")
                elif key == ord('p'):
                    self.input_mode = "pub"
                    self.input_buffer = ""
                elif ord('1') <= key <= ord('9'):
                    idx = key - ord('1')
                    if idx < len(self.peers_list):
                        self.client.target_peer_id = self.peers_list[idx]
                        print(f"Selected: {self.peers_list[idx]}")
            
            await asyncio.sleep(0.001)  # Very short sleep for responsiveness
        
        # Cleanup
        cap.release()
        cv2.destroyAllWindows()
        self.client.disconnect()
        print("Goodbye!")
        
    async def handle_input_submit(self):
        """Handle submission of input mode"""
        if self.input_mode == "call" and self.input_buffer:
            await self.client.initiate_call(self.input_buffer)
        elif self.input_mode == "msg" and self.input_buffer:
            self.client.send_private_message(self.input_buffer)
        elif self.input_mode == "pub" and self.input_buffer:
            self.client.send_public_message(self.input_buffer)
        
        self.input_mode = None
        self.input_buffer = ""

if __name__ == "__main__":
    app = GUIApp()
    asyncio.run(app.run())

