import asyncio
import json
import os
import sys
import uuid
import tkinter as tk
from tkinter import messagebox, ttk
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
import paho.mqtt.client as mqtt

class WebRTCGuiChat:
    def __init__(self, root, loop):
        self.root = root
        self.loop = loop
        self.peer_id = f"peer_{uuid.uuid4().hex[:4]}"
        self.remote_id = None
        self.signaling_topic = "webrtc/signaling"
        
        self.pc = None
        self.channel = None
        
        # State containers
        self.online_peers = {}        
        self.incoming_offers = {}      

        # Window Config
        self.root.title(f"WebRTC Chat - {self.peer_id}")
        self.root.geometry("650x550")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.setup_ui()

        # Setup MQTT client
        self.mqtt_client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2, 
            client_id=self.peer_id, 
            protocol=mqtt.MQTTv5
        )
        self.mqtt_client.tls_set()
        self.mqtt_client.username_pw_set("admin", "admin1234S")

    def setup_ui(self):
        # Discovery Hub
        self.top_frame = ttk.LabelFrame(self.root, text=" P2P Node Discovery Hub ", padding=10)
        self.top_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(self.top_frame, text="Active Network Peers:").grid(row=0, column=0, sticky="w")
        self.peer_listbox = tk.Listbox(self.top_frame, height=4, selectmode=tk.SINGLE)
        self.peer_listbox.grid(row=1, column=0, columnspan=2, sticky="ew", pady=5)

        self.btn_call = ttk.Button(self.top_frame, text="📞 Call Selected Peer", command=self.on_call_clicked)
        self.btn_call.grid(row=2, column=0, sticky="ew", padx=2)

        self.btn_accept = ttk.Button(self.top_frame, text="📥 No Incoming Requests", command=self.on_accept_clicked, state="disabled")
        self.btn_accept.grid(row=2, column=1, sticky="ew", padx=2)

        self.top_frame.columnconfigure(0, weight=1)
        self.top_frame.columnconfigure(1, weight=1)

        # Connection Logs
        self.log_frame = ttk.LabelFrame(self.root, text=" Network Event Monitor ", padding=5)
        self.log_frame.pack(fill="x", padx=10, pady=5)
        self.log_display = tk.Text(self.log_frame, height=4, state="disabled", background="#f0f0f0", wrap="word")
        self.log_display.pack(fill="x")

        # Repurposed Messaging Engine (The Chat Box itself is the editor)
        self.chat_frame = ttk.LabelFrame(self.root, text=" Direct Chat Box ", padding=10)
        self.chat_frame.pack(fill="both", expand=True, padx=10, pady=5)

        # Left normal so you can click and type directly inside it
        self.chat_display = tk.Text(self.chat_frame, state="normal", wrap="word", font=("Courier", 11))
        self.chat_display.pack(fill="both", expand=True, pady=5)
        
        # Guide banner inside the window
        self.chat_display.insert(tk.END, "=== SYSTEM: Wait for connection, then start typing here ===\n\n")
        self.chat_display.see(tk.END)

        # Bind the Return key directly on the big chat field
        self.chat_display.bind("<Return>", self.on_chat_box_return)

    def console_log(self, text):
        def log():
            self.log_display.config(state="normal")
            self.log_display.insert(tk.END, f">> {text}\n")
            self.log_display.config(state="disabled")
            self.log_display.see(tk.END)
        self.loop.call_soon_threadsafe(log)

    def connect_mqtt(self):
        self.mqtt_client.on_connect = lambda c, u, f, rc, p: self.on_mqtt_connect(rc)
        self.mqtt_client.on_message = self.on_mqtt_message
        try:
            self.console_log("Connecting to HiveMQ cloud pipeline...")
            self.mqtt_client.connect("e5122a5328ea4986a0295fa6e037655a.s2.eu.hivemq.cloud", 8883, 60)
            self.mqtt_client.loop_start()
            
            asyncio.run_coroutine_threadsafe(self.presence_broadcast(), self.loop)
            asyncio.run_coroutine_threadsafe(self.peer_expiry_monitor(), self.loop)
        except Exception as e:
            self.console_log(f"Connection error: {e}")

    def on_mqtt_connect(self, rc):
        self.mqtt_client.subscribe(self.signaling_topic)
        self.console_log("Connected successfully to cloud network!")

    async def presence_broadcast(self):
        while True:
            msg = {"type": "presence", "from": self.peer_id}
            self.mqtt_client.publish(self.signaling_topic, json.dumps(msg))
            await asyncio.sleep(2)

    async def peer_expiry_monitor(self):
        while True:
            await asyncio.sleep(1)
            now = asyncio.get_event_loop().time()
            expired = [pid for pid, ts in self.online_peers.items() if now - ts > 5]
            if expired:
                for pid in expired:
                    del self.online_peers[pid]
                    if pid in self.incoming_offers:
                        del self.incoming_offers[pid]
                self._update_gui_lists_sync()

    def on_mqtt_message(self, client, userdata, msg):
        payload_raw = msg.payload.decode()
        self.loop.call_soon_threadsafe(lambda: self._process_signal_in_loop(payload_raw))

    def _process_signal_in_loop(self, raw_data):
        try:
            payload = json.loads(raw_data)
            msg_type = payload.get("type")
            sender = payload.get("from")

            if sender == self.peer_id:
                return

            if msg_type == "presence":
                if sender not in self.online_peers:
                    self.console_log(f"Discovered online node: {sender}")
                self.online_peers[sender] = asyncio.get_event_loop().time()
                self._update_gui_lists_sync()

            elif payload.get("to") == self.peer_id:
                if msg_type == "offer":
                    self.console_log(f"Incoming call request from {sender}!")
                    self.incoming_offers[sender] = payload["data"]
                    self._update_gui_lists_sync()
                elif msg_type == "answer" and self.pc:
                    self.console_log(f"Handshake Answer returned from {sender}.")
                    asyncio.create_task(self.pc.setRemoteDescription(
                        RTCSessionDescription(sdp=payload["data"]["sdp"], type=payload["data"]["type"])
                    ))
                elif msg_type == "ice" and self.pc:
                    data = payload["data"]
                    candidate = RTCIceCandidate(
                        sdpMid=data["sdpMid"], sdpMLineIndex=data["sdpMLineIndex"], candidate=data["candidate"]
                    )
                    asyncio.create_task(self.pc.addIceCandidate(candidate))
        except Exception as e:
            self.console_log(f"Signaling error: {e}")

    def _update_gui_lists_sync(self):
        def refresh():
            current_selection = self.peer_listbox.get(tk.ACTIVE)
            self.peer_listbox.delete(0, tk.END)
            for pid in self.online_peers.keys():
                self.peer_listbox.insert(tk.END, pid)
                
            if current_selection and current_selection in self.online_peers:
                idx = list(self.online_peers.keys()).index(current_selection)
                self.peer_listbox.activate(idx)

            offers = list(self.incoming_offers.keys())
            if offers:
                self.btn_accept.config(text=f"🔥 Accept Call from {offers[0]}", state="normal")
            else:
                self.btn_accept.config(text="📥 No Incoming Requests", state="disabled")
        self.loop.call_soon_threadsafe(refresh)

    def append_incoming_text(self, sender, text):
        def write():
            # Injects the incoming text on a clean new line above your cursor
            self.chat_display.insert(tk.END, f"\n[{sender}]: {text}\n")
            self.chat_display.see(tk.END)
        self.loop.call_soon_threadsafe(write)

    def on_call_clicked(self):
        selection = self.peer_listbox.get(tk.ACTIVE)
        if not selection:
            messagebox.showwarning("Empty Target", "Select an active peer ID from the box first!")
            return
        asyncio.run_coroutine_threadsafe(self.dial_peer(selection), self.loop)

    def on_accept_clicked(self):
        offers = list(self.incoming_offers.keys())
        if not offers:
            return
        asyncio.run_coroutine_threadsafe(self.accept_incoming_call(offers[0]), self.loop)

    def on_chat_box_return(self, event):
        """Grabs the last line typed into the box on Enter and transmits it."""
        if not self.channel or self.channel.readyState != "open":
            return "break"

        # Get text from the current line
        current_line_index = self.chat_display.index("insert linestart")
        end_line_index = self.chat_display.index("insert lineend")
        raw_line = self.chat_display.get(current_line_index, end_line_index).strip()

        # Clean off any system tags if present
        if raw_line.startswith("[You]:"):
            msg = raw_line.replace("[You]:", "", 1).strip()
        else:
            msg = raw_line

        if msg:
            # Transmit line text to remote data channel target
            self.loop.call_soon_threadsafe(lambda: self.channel.send(msg))
            
            # Reformat current line locally to look nice
            self.chat_display.delete(current_line_index, end_line_index)
            self.chat_display.insert(current_line_index, f"[You]: {msg}")
            
            # Let default Enter handle creating a clean new line underneath
            return None 
        
        return "break"

    def init_peer_connection(self):
        self.pc = RTCPeerConnection()

        @self.pc.on("icecandidate")
        async def on_candidate(candidate):
            if candidate:
                self.send_signaling_msg("ice", {
                    "sdpMid": candidate.sdpMid, 
                    "sdpMLineIndex": candidate.sdpMLineIndex, 
                    "candidate": candidate.candidate
                })

        @self.pc.on("connectionstatechange")
        async def on_state_change():
            self.console_log(f"WebRTC State Shift: {self.pc.connectionState}")
            if self.pc.connectionState in ["failed", "closed"]:
                self.append_incoming_text("SYSTEM", "P2P connection dropped.")

    def attach_datachannel_listeners(self):
        @self.channel.on("open")
        def on_open():
            self.append_incoming_text("SYSTEM", "Connected directly! Type here & hit enter...")
            def force_focus():
                self.chat_display.focus_force()
            self.loop.call_soon_threadsafe(force_focus)

        @self.channel.on("message")
        def on_message(msg):
            self.append_incoming_text(self.remote_id, msg)

    async def dial_peer(self, target_id):
        self.remote_id = target_id
        self.console_log(f"Generating offer for {target_id}...")
        self.init_peer_connection()
        self.channel = self.pc.createDataChannel("chat")
        self.attach_datachannel_listeners()

        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        self.send_signaling_msg("offer", {"sdp": self.pc.localDescription.sdp, "type": self.pc.localDescription.type})

    async def accept_incoming_call(self, target_id):
        self.remote_id = target_id
        self.console_log(f"Answering offer from {target_id}...")
        self.init_peer_connection()

        @self.pc.on("datachannel")
        def on_datachannel(channel):
            self.channel = channel
            self.attach_datachannel_listeners()

        offer_sdp = self.incoming_offers[target_id]
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp["sdp"], type=offer_sdp["type"]))
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        self.send_signaling_msg("answer", {"sdp": self.pc.localDescription.sdp, "type": self.pc.localDescription.type})

    def send_signaling_msg(self, msg_type, data):
        payload = {"type": msg_type, "from": self.peer_id, "to": self.remote_id, "data": data}
        self.mqtt_client.publish(self.signaling_topic, json.dumps(payload))

    def on_close(self):
        self.mqtt_client.loop_stop()
        self.root.destroy()
        os._exit(0)

async def run_tk_loop(root, interval=0.03):
    try:
        while True:
            root.update()
            await asyncio.sleep(interval)
    except tk.TclError:
        pass

if __name__ == "__main__":
    root = tk.Tk()
    async_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(async_loop)
    
    app = WebRTCGuiChat(root, async_loop)
    app.connect_mqtt()

    async_loop.run_until_complete(run_tk_loop(root))
