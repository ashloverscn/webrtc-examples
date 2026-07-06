import asyncio
import json
import os
import sys
import uuid
import tkinter as tk
from tkinter import messagebox, ttk, filedialog
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
import paho.mqtt.client as mqtt

CHUNK_SIZE = 16384  # 16 KB optimized chunks for WebRTC DataChannels

class WebRTCFileTransfer:
    def __init__(self, root, loop):
        self.root = root
        self.loop = loop
        self.peer_id = f"peer_{uuid.uuid4().hex[:4]}"
        self.remote_id = None
        self.signaling_topic = "webrtc/file_signaling"
        
        self.pc = None
        self.channel = None
        
        # Internal Transfer Memory Map
        self.online_peers = {}        
        self.incoming_offers = {}      
        self.incoming_file_metadata = {}
        self.received_chunks = []

        # Window Setup
        self.root.title(f"P2P File Stream Engine - {self.peer_id}")
        self.root.geometry("600x450")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.setup_ui()

        # Connect Signaler Pipeline
        self.mqtt_client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2, 
            client_id=self.peer_id, 
            protocol=mqtt.MQTTv5
        )
        self.mqtt_client.tls_set()
        self.mqtt_client.username_pw_set("admin", "admin1234S")

    def setup_ui(self):
        # 1. Peer Discovery Segment
        self.top_frame = ttk.LabelFrame(self.root, text=" P2P Node Discovery Hub ", padding=10)
        self.top_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(self.top_frame, text="Discovered Mesh Nodes:").grid(row=0, column=0, sticky="w")
        self.peer_listbox = tk.Listbox(self.top_frame, height=4, selectmode=tk.SINGLE)
        self.peer_listbox.grid(row=1, column=0, columnspan=2, sticky="ew", pady=5)

        self.btn_call = ttk.Button(self.top_frame, text="🔗 Connect to Node", command=self.on_connect_clicked)
        self.btn_call.grid(row=2, column=0, sticky="ew", padx=2)

        self.btn_accept = ttk.Button(self.top_frame, text="📥 No Incoming Requests", command=self.on_accept_clicked, state="disabled")
        self.btn_accept.grid(row=2, column=1, sticky="ew", padx=2)

        self.top_frame.columnconfigure(0, weight=1)
        self.top_frame.columnconfigure(1, weight=1)

        # 2. Dedicated File Stream Deck
        self.file_frame = ttk.LabelFrame(self.root, text=" Shared P2P File Pipeline ", padding=10)
        self.file_frame.pack(fill="x", padx=10, pady=5)

        self.btn_select_file = ttk.Button(self.file_frame, text="📁 Select File to Stream", command=self.on_select_file_clicked, state="disabled")
        self.btn_select_file.pack(fill="x", pady=2)

        self.progress_bar = ttk.Progressbar(self.file_frame, orient="horizontal", mode="determinate")
        self.progress_bar.pack(fill="x", pady=5)

        self.lbl_status = ttk.Label(self.file_frame, text="Status: Dynamic Channel Idle", font=("Helvetica", 10, "bold"))
        self.lbl_status.pack(anchor="w")

        # 3. System Monitor
        self.log_frame = ttk.LabelFrame(self.root, text=" Live Core Activity Monitor ", padding=5)
        self.log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        self.log_display = tk.Text(self.log_frame, state="disabled", background="#f4f4f4", wrap="word")
        self.log_display.pack(fill="both", expand=True)

    def console_log(self, text):
        def log():
            self.log_display.config(state="normal")
            self.log_display.insert(tk.END, f">> {text}\n")
            self.log_display.config(state="disabled")
            self.log_display.see(tk.END)
        self.loop.call_soon_threadsafe(log)

    def set_status(self, text):
        self.loop.call_soon_threadsafe(lambda: self.lbl_status.config(text=f"Status: {text}"))

    def set_progress(self, val):
        self.loop.call_soon_threadsafe(lambda: self.progress_bar.config(value=val))

    def connect_mqtt(self):
        self.mqtt_client.on_connect = lambda c, u, f, rc, p: self.on_mqtt_connect(rc)
        self.mqtt_client.on_message = self.on_mqtt_message
        try:
            self.console_log("Connecting to core MQTT control broker...")
            self.mqtt_client.connect("e5122a5328ea4986a0295fa6e037655a.s2.eu.hivemq.cloud", 8883, 60)
            self.mqtt_client.loop_start()
            
            asyncio.run_coroutine_threadsafe(self.presence_broadcast(), self.loop)
            asyncio.run_coroutine_threadsafe(self.peer_expiry_monitor(), self.loop)
        except Exception as e:
            self.console_log(f"Broker error: {e}")

    def on_mqtt_connect(self, rc):
        self.mqtt_client.subscribe(self.signaling_topic)
        self.console_log("Mesh signaling route connected successfully.")

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
                    self.console_log(f"Found node available for sync: {sender}")
                self.online_peers[sender] = asyncio.get_event_loop().time()
                self._update_gui_lists_sync()

            elif payload.get("to") == self.peer_id:
                if msg_type == "offer":
                    self.console_log(f"Incoming connection request from {sender}")
                    self.incoming_offers[sender] = payload["data"]
                    self._update_gui_lists_sync()
                elif msg_type == "answer" and self.pc:
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
            self.console_log(f"Signaling routing leak: {e}")

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
                self.btn_accept.config(text=f"🔥 Accept Link from {offers[0]}", state="normal")
            else:
                self.btn_accept.config(text="📥 No Incoming Requests", state="disabled")
        self.loop.call_soon_threadsafe(refresh)

    def on_connect_clicked(self):
        selection = self.peer_listbox.get(tk.ACTIVE)
        if not selection:
            messagebox.showwarning("Node Empty", "Select an active peer target node first.")
            return
        asyncio.run_coroutine_threadsafe(self.dial_peer(selection), self.loop)

    def on_accept_clicked(self):
        offers = list(self.incoming_offers.keys())
        if not offers:
            return
        asyncio.run_coroutine_threadsafe(self.accept_incoming_call(offers[0]), self.loop)

    def on_select_file_clicked(self):
        file_path = filedialog.askopenfilename()
        if file_path:
            asyncio.run_coroutine_threadsafe(self.stream_file_payload(file_path), self.loop)

    def init_peer_connection(self):
        self.pc = RTCPeerConnection()

        @self.pc.on("icecandidate")
        async def on_candidate(candidate):
            if candidate:
                self.send_signaling_msg("ice", {
                    "sdpMid": candidate.sdpMid, "sdpMLineIndex": candidate.sdpMLineIndex, "candidate": candidate.candidate
                })

        @self.pc.on("connectionstatechange")
        async def on_state_change():
            self.console_log(f"WebRTC Link State Change: {self.pc.connectionState}")
            if self.pc.connectionState in ["failed", "closed"]:
                self.set_status("Link Broken/Disconnected")
                self.loop.call_soon_threadsafe(lambda: self.btn_select_file.config(state="disabled"))

    def attach_datachannel_listeners(self):
        @self.channel.on("open")
        def on_open():
            self.console_log("Data pipeline established perfectly.")
            self.set_status("Connected and Ready to Sync")
            self.loop.call_soon_threadsafe(lambda: self.btn_select_file.config(state="normal"))

        @self.channel.on("message")
        def on_message(msg):
            if isinstance(msg, str):
                try:
                    meta = json.loads(msg)
                    if meta.get("type") == "file_start":
                        self.incoming_file_metadata = meta
                        self.received_chunks = []
                        self.set_status(f"Receiving: {meta['name']}")
                        self.console_log(f"Incoming download: {meta['name']} ({meta['size']} bytes)")
                    elif meta.get("type") == "file_end":
                        self.save_received_file()
                except Exception as e:
                    self.console_log(f"Frame validation drop: {e}")
            else:
                self.received_chunks.append(msg)
                current_bytes = sum(len(c) for c in self.received_chunks)
                total = self.incoming_file_metadata.get("size", 1)
                
                # Receiver-side Throttling to prevent receiver flicker
                progress = int((current_bytes / total) * 100)
                if progress % 2 == 0 or progress == 100:
                    self.set_progress(progress)

    async def stream_file_payload(self, file_path):
        if not self.channel or self.channel.readyState != "open":
            self.console_log("Cannot send: Data channel is not open yet.")
            return
            
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        
        self.console_log(f"Starting stream for {file_name}...")
        self.set_status(f"Uploading: {file_name}")

        meta_header = {"type": "file_start", "name": file_name, "size": file_size}
        self.channel.send(json.dumps(meta_header))
        await asyncio.sleep(0.2)

        bytes_sent = 0
        max_buffer_size = 1024 * 1024  # 1 MB Backpressure Limit
        last_reported_progress = -1

        with open(file_path, "rb") as f:
            while bytes_sent < file_size:
                # Flow Control: Halt loop if WebRTC buffer is currently backed up
                while self.channel.bufferedAmount > max_buffer_size:
                    await asyncio.sleep(0.01)

                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                
                self.channel.send(chunk)
                bytes_sent += len(chunk)
                
                # THROTTLING FIX: Only trigger Tkinter layout updates when integer percentage changes
                current_progress = int((bytes_sent / file_size) * 100)
                if current_progress != last_reported_progress:
                    self.set_progress(current_progress)
                    last_reported_progress = current_progress
                
                # Tiny tick sleep to allow background execution frames to clear cleanly
                await asyncio.sleep(0.001)

        # Confirm buffer is completely flushed before dispatching the final flag
        while self.channel.bufferedAmount > 0:
            await asyncio.sleep(0.05)

        self.channel.send(json.dumps({"type": "file_end"}))
        self.console_log("Streaming transaction successfully uploaded!")
        self.set_status("Upload complete")
        self.set_progress(0)

    def save_received_file(self):
        file_name = self.incoming_file_metadata.get("name", "synced_payload.bin")
        output_path = os.path.expanduser(f"~/Downloads/{file_name}")
        
        try:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "wb") as f:
                for chunk in self.received_chunks:
                    f.write(chunk)
            self.console_log(f"Success! File saved cleanly to: {output_path}")
            self.set_status("Download Complete!")
            self.set_progress(0)
            messagebox.showinfo("Stream complete", f"File saved directly to:\n{output_path}")
        except Exception as e:
            self.console_log(f"Disk IO error compiling incoming chunks: {e}")
            self.set_status("Write crash")

    async def dial_peer(self, target_id):
        self.remote_id = target_id
        self.console_log(f"Negotiating handshakes with {target_id}...")
        self.init_peer_connection()
        self.channel = self.pc.createDataChannel("file_stream")
        self.attach_datachannel_listeners()

        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        self.send_signaling_msg("offer", {"sdp": self.pc.localDescription.sdp, "type": self.pc.localDescription.type})

    async def accept_incoming_call(self, target_id):
        self.remote_id = target_id
        self.console_log(f"Hooking tunnel directly to {target_id}...")
        self.init_peer_connection()

        @self.pc.on("datachannel")
        def on_datachannel(channel):
            self.channel = channel
            self.attach_datachannel_listeners()
            if self.channel.readyState == "open":
                self.console_log("Remote pipeline detected open instantly.")
                self.set_status("Connected and Ready to Sync")
                self.loop.call_soon_threadsafe(lambda: self.btn_select_file.config(state="normal"))

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
    
    app = WebRTCFileTransfer(root, async_loop)
    app.connect_mqtt()

    async_loop.run_until_complete(run_tk_loop(root))
