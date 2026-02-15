#!/usr/bin/env python3
"""
WebRTC Serial Terminal via MQTT Signaling - Python Port
Dependencies: pip install aiortc paho-mqtt
"""

import asyncio
import json
import random
import ssl
import threading
import time
from datetime import datetime
from typing import Dict, Optional

import paho.mqtt.client as mqtt
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp


class WebRTCTerminal:
    # Hardcoded credentials from original
    BROKER_URL = "e5122a5328ea4986a0295fa6e037655a.s2.eu.hivemq.cloud"
    BROKER_PORT = 8883  # MQTT over TLS
    TOPIC = "webrtc/signaling"
    USERNAME = "admin"
    PASSWORD = "admin1234S"
    
    def __init__(self):
        self.peer_id = f"peer_{random.randint(100000, 999999)}"
        self.target_peer_id: Optional[str] = None
        self.pc: Optional[RTCPeerConnection] = None
        self.dc = None
        
        # Peer registry: peer_id -> last_seen_timestamp
        self.peer_registry: Dict[str, float] = {}
        self.registry_lock = threading.Lock()
        
        # UI state
        self.terminal_lines: list[str] = []
        self.input_buffer = ""
        self.running = True
        self.dc_open = False
        
        # MQTT client
        self.mqtt_client = mqtt.Client(client_id=f"signaling_{self.peer_id}")
        self.mqtt_client.username_pw_set(self.USERNAME, self.PASSWORD)
        self.mqtt_client.tls_set(cert_reqs=ssl.CERT_NONE)
        self.mqtt_client.tls_insecure_set(True)
        
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message
        
        # Async loop for WebRTC
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        
    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        
    def log(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}"
        self.terminal_lines.append(line)
        # Keep last 100 lines
        if len(self.terminal_lines) > 100:
            self.terminal_lines.pop(0)
        self._draw_ui()
        
    def _draw_ui(self):
        """Simple terminal UI using ANSI escape codes"""
        # Clear screen
        print("\033[2J\033[H", end="")
        
        # Header
        print(f"\033[1;36mWebRTC Serial Terminal (MQTT Signaling)\033[0m")
        print(f"\033[90mPeer ID: {self.peer_id} | Target: {self.target_peer_id or 'None'}\033[0m")
        print("─" * 60)
        
        # Active peers
        print("\033[1;33mActive Peers:\033[0m")
        with self.registry_lock:
            now = time.time()
            for pid, last_seen in sorted(self.peer_registry.items()):
                age = now - last_seen
                if pid == self.peer_id:
                    print(f"  \033[90m[me] {pid}\033[0m")
                elif age > 5:
                    continue  # Expired
                elif age > 1:
                    print(f"  \033[31m[offline] {pid}\033[0m")
                else:
                    marker = " \033[32m<-- TARGET\033[0m" if pid == self.target_peer_id else ""
                    print(f"  \033[32m[online]\033[0m {pid}{marker}")
        print("─" * 60)
        
        # Terminal output
        print("\033[1;33mTerminal Output:\033[0m")
        for line in self.terminal_lines[-15:]:  # Show last 15 lines
            # Colorize
            if "Sent" in line or "<" in line:
                print(f"\033[32m{line}\033[0m")  # Green for sent
            elif "from" in line or ">" in line:
                print(f"\033[36m{line}\033[0m")  # Cyan for received
            elif "⚠️" in line or "Error" in line or "not open" in line:
                print(f"\033[31m{line}\033[0m")  # Red for errors
            else:
                print(line)
        print("─" * 60)
        
        # Input prompt
        status = "\033[32m[OPEN]\033[0m" if self.dc_open else "\033[31m[CLOSED]\033[0m"
        print(f"\033[1mDataChannel {status}\033[0m")
        print(f"\033[1m> {self.input_buffer}\033[0m\033[K", end="", flush=True)
        
    def _on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.log(f"✅ Connected to MQTT as {self.peer_id}")
            client.subscribe(self.TOPIC)
            # Announce presence
            self._send_presence()
            # Start presence heartbeat
            threading.Thread(target=self._presence_loop, daemon=True).start()
        else:
            self.log(f"❌ MQTT connection failed: {rc}")
            
    def _presence_loop(self):
        while self.running:
            self._send_presence()
            time.sleep(1)
            
    def _send_presence(self):
        payload = json.dumps({"type": "presence", "from": self.peer_id})
        self.mqtt_client.publish(self.TOPIC, payload)
        # Update own registry
        with self.registry_lock:
            self.peer_registry[self.peer_id] = time.time()
            
    def _on_mqtt_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
            msg_type = data.get("type")
            from_peer = data.get("from")
            to_peer = data.get("to")
            
            # Update registry for presence messages
            if msg_type == "presence":
                with self.registry_lock:
                    self.peer_registry[from_peer] = time.time()
                return
                
            # Ignore messages not for us
            if to_peer != self.peer_id:
                return
                
            self.log(f"📥 {msg_type.upper()} from {from_peer}")
            self.target_peer_id = from_peer
            
            # Handle signaling messages
            if msg_type == "offer":
                asyncio.run_coroutine_threadsafe(self._handle_offer(data["data"]), self.loop)
            elif msg_type == "answer":
                asyncio.run_coroutine_threadsafe(self._handle_answer(data["data"]), self.loop)
            elif msg_type == "ice":
                asyncio.run_coroutine_threadsafe(self._handle_ice(data["data"]), self.loop)
                
        except Exception as e:
            self.log(f"⚠️ Error handling message: {e}")
            
    def _send_signal(self, msg_type: str, data: dict):
        if not self.target_peer_id:
            self.log("⚠️ No target peer selected")
            return
            
        payload = {
            "type": msg_type,
            "from": self.peer_id,
            "to": self.target_peer_id,
            "data": data
        }
        self.mqtt_client.publish(self.TOPIC, json.dumps(payload))
        self.log(f"📤 Sent {msg_type.upper()} to {self.target_peer_id}")
        
    async def _setup_peer_connection(self, is_offerer: bool):
        """Setup WebRTC peer connection"""
        if self.pc:
            await self.pc.close()
            
        self.pc = RTCPeerConnection({
            "iceServers": [{"urls": "stun:stun.l.google.com:19302"}]
        })
        
        if is_offerer:
            self.dc = self.pc.createDataChannel("serial")
            self._setup_data_channel()
            
        @self.pc.on("datachannel")
        def on_datachannel(channel):
            self.dc = channel
            self._setup_data_channel()
            
        @self.pc.on("icecandidate")
        async def on_icecandidate(candidate):
            if candidate:
                self._send_signal("ice", {
                    "candidate": candidate_to_sdp(candidate),
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex
                })
                
        if is_offerer:
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)
            self._send_signal("offer", {
                "type": self.pc.localDescription.type,
                "sdp": self.pc.localDescription.sdp
            })
            
    def _setup_data_channel(self):
        """Configure data channel callbacks"""
        
        @self.dc.on("open")
        def on_open():
            self.dc_open = True
            self.log("=== DataChannel Opened ===")
            self._draw_ui()
            
        @self.dc.on("close")
        def on_close():
            self.dc_open = False
            self.log("=== DataChannel Closed ===")
            self._draw_ui()
            
        @self.dc.on("message")
        def on_message(message):
            if isinstance(message, str):
                self.log(f"> {message}")
            else:
                self.log(f"> [Binary data: {len(message)} bytes]")
                
    async def _handle_offer(self, offer_data: dict):
        """Handle incoming offer"""
        await self._setup_peer_connection(False)
        
        offer = RTCSessionDescription(
            sdp=offer_data["sdp"],
            type=offer_data["type"]
        )
        await self.pc.setRemoteDescription(offer)
        
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        self._send_signal("answer", {
            "type": self.pc.localDescription.type,
            "sdp": self.pc.localDescription.sdp
        })
        
    async def _handle_answer(self, answer_data: dict):
        """Handle incoming answer"""
        answer = RTCSessionDescription(
            sdp=answer_data["sdp"],
            type=answer_data["type"]
        )
        await self.pc.setRemoteDescription(answer)
        
    async def _handle_ice(self, candidate_data: dict):
        """Handle incoming ICE candidate"""
        if self.pc and self.pc.remoteDescription:
            try:
                candidate = candidate_from_sdp(candidate_data["candidate"])
                candidate.sdpMid = candidate_data.get("sdpMid")
                candidate.sdpMLineIndex = candidate_data.get("sdpMLineIndex")
                await self.pc.addIceCandidate(candidate)
            except Exception as e:
                self.log(f"⚠️ Error adding ICE candidate: {e}")
            
    def initiate_call(self, target: str):
        """Start a call to target peer"""
        self.target_peer_id = target
        self.log(f"Initiating call to {target}...")
        asyncio.run_coroutine_threadsafe(self._setup_peer_connection(True), self.loop)
        
    def send_message(self, message: str):
        """Send message via data channel"""
        if self.dc and self.dc.readyState == "open":
            self.loop.call_soon_threadsafe(self.dc.send, message)
            self.log(f"< {message}")
        else:
            self.log("⚠️ DataChannel not open")
            
    def cleanup_expired_peers(self):
        """Remove peers not seen for >5 seconds"""
        now = time.time()
        with self.registry_lock:
            expired = [pid for pid, ts in self.peer_registry.items() 
                      if pid != self.peer_id and now - ts > 5]
            for pid in expired:
                del self.peer_registry[pid]
                
    def run(self):
        """Main entry point"""
        self.loop_thread.start()
        
        # Connect MQTT
        try:
            self.mqtt_client.connect(self.BROKER_URL, self.BROKER_PORT, 60)
            self.mqtt_client.loop_start()
        except Exception as e:
            print(f"Failed to connect: {e}")
            return
            
        # Main input loop
        try:
            import sys
            import select
            import termios
            import tty
            
            while self.running:
                self.cleanup_expired_peers()
                self._draw_ui()
                
                # Set terminal to raw mode for single char input
                old_settings = termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())
                
                try:
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        char = sys.stdin.read(1)
                        
                        if char == '\n':  # Enter
                            cmd = self.input_buffer.strip()
                            if cmd:
                                if cmd.startswith("/call "):
                                    target = cmd[6:].strip()
                                    self.initiate_call(target)
                                elif cmd == "/quit":
                                    break
                                else:
                                    self.send_message(cmd)
                            self.input_buffer = ""
                        elif char in ('\x7f', '\b'):  # Backspace
                            self.input_buffer = self.input_buffer[:-1]
                        elif ord(char) >= 32:  # Printable
                            self.input_buffer += char
                finally:
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                    
        except KeyboardInterrupt:
            pass
        except Exception as e:
            self.log(f"⚠️ Input error: {e}")
        finally:
            self.running = False
            self.mqtt_client.loop_stop()
            if self.pc:
                asyncio.run_coroutine_threadsafe(self.pc.close(), self.loop)
            self.loop.call_soon_threadsafe(self.loop.stop)
            # Reset terminal
            import os
            os.system('reset' if os.name != 'nt' else 'cls')
            print("Goodbye!")


if __name__ == "__main__":
    terminal = WebRTCTerminal()
    print("Starting WebRTC Terminal...")
    print("Commands: /call <peer_id>  - Initiate connection")
    print("          /quit            - Exit")
    print("Or just type to send messages when connected")
    input("Press Enter to start...")
    terminal.run()
