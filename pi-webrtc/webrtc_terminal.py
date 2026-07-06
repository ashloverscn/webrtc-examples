import asyncio
import json
import os
import sys
import uuid
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
import paho.mqtt.client as mqtt

# Simple clean logger
def log(msg):
    print(f"\r[* ] {msg}")

class WebRTCTerminalChat:
    def __init__(self):
        self.peer_id = f"peer_{uuid.uuid4().hex[:6]}"
        self.remote_id = None
        self.signaling_topic = "webrtc/signaling"
        self.pc = None
        self.channel = None
        self._loop = None

        # MQTT Setup
        self.mqtt_client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2, 
            client_id=self.peer_id, 
            protocol=mqtt.MQTTv5
        )
        self.mqtt_client.tls_set()
        self.mqtt_client.username_pw_set("admin", "admin1234S")

    def connect_mqtt(self):
        self.mqtt_client.on_connect = lambda c, u, f, rc, p: c.subscribe(self.signaling_topic)
        self.mqtt_client.on_message = self.on_mqtt_message
        try:
            self.mqtt_client.connect("e5122a5328ea4986a0295fa6e037655a.s2.eu.hivemq.cloud", 8883, 60)
            self.mqtt_client.loop_start()
            # Start background presence heartbeat
            asyncio.run_coroutine_threadsafe(self.presence_loop(), self._loop)
        except Exception as e:
            print(f"MQTT Connection Failed: {e}")

    async def presence_loop(self):
        while True:
            # Broadcast presence until a link is negotiated
            if not self.pc:
                msg = {"type": "presence", "from": self.peer_id}
                self.mqtt_client.publish(self.signaling_topic, json.dumps(msg))
            await asyncio.sleep(2)

    def on_mqtt_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            msg_type = payload.get("type")
            sender = payload.get("from")

            if sender == self.peer_id:
                return

            # Dynamic Peer Discovery: If we don't have a peer yet, the first one to say hi is it
            if msg_type == "presence" and not self.pc:
                self.remote_id = sender
                # To prevent racing, the alphabetically lower peer ID initiates the offer
                if self.peer_id < self.remote_id:
                    asyncio.run_coroutine_threadsafe(self.initiate_call(), self._loop)
                return

            # Route signaling messages explicitly addressed to us
            if payload.get("to") == self.peer_id:
                if msg_type == "offer":
                    asyncio.run_coroutine_threadsafe(self.handle_offer(sender, payload["data"]), self._loop)
                elif msg_type == "answer":
                    asyncio.run_coroutine_threadsafe(self.handle_answer(payload["data"]), self._loop)
                elif msg_type == "ice":
                    asyncio.run_coroutine_threadsafe(self.handle_ice(payload["data"]), self._loop)
        except Exception as e:
            pass

    def setup_peer_connection(self):
        self.pc = RTCPeerConnection()

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
            if self.pc.connectionState in ["failed", "closed"]:
                log("Connection closed.")
                os._exit(0)

    def setup_datachannel_handlers(self):
        @self.channel.on("open")
        def on_open():
            print(f"\n==================================================")
            print(f"🤝 WebRTC CHAT CONNECTED TO {self.remote_id}")
            print(f"Type your message and press Enter. Try it out!")
            print(f"==================================================\n")
            asyncio.run_coroutine_threadsafe(self.terminal_input_loop(), self._loop)

        @self.channel.on("message")
        def on_message(message):
            # Print incoming message without messing up current cursor line
            sys.stdout.write(f"\r\n[{self.remote_id}]: {message}\n> ")
            sys.stdout.flush()

    async def initiate_call(self):
        log(f"Initiating WebRTC connection with {self.remote_id}...")
        self.setup_peer_connection()
        
        # We are the caller, we create the data channel
        self.channel = self.pc.createDataChannel("chat")
        self.setup_datachannel_handlers()

        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        self.send_signal("offer", {"sdp": self.pc.localDescription.sdp, "type": self.pc.localDescription.type})

    async def handle_offer(self, sender, data):
        self.remote_id = sender
        log(f"Received connection request from {self.remote_id}. Answering...")
        self.setup_peer_connection()

        @self.pc.on("datachannel")
        def on_datachannel(channel):
            self.channel = channel
            self.setup_datachannel_handlers()

        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        self.send_signal("answer", {"sdp": self.pc.localDescription.sdp, "type": self.pc.localDescription.type})

    async def handle_answer(self, data):
        if self.pc:
            await self.pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))

    async def handle_ice(self, data):
        if self.pc:
            candidate = RTCIceCandidate(
                sdpMid=data["sdpMid"], sdpMLineIndex=data["sdpMLineIndex"], candidate=data["candidate"]
            )
            await self.pc.addIceCandidate(candidate)

    def send_signal(self, msg_type, data):
        payload = {"type": msg_type, "from": self.peer_id, "to": self.remote_id, "data": data}
        self.mqtt_client.publish(self.signaling_topic, json.dumps(payload))

    async def terminal_input_loop(self):
        """Asynchronously reads terminal input and pushes it through the data channel."""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await self._loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            sys.stdout.write("> ")
            sys.stdout.flush()
            line = await reader.readline()
            if not line:
                break
            message = line.decode().strip()
            if message:
                if self.channel and self.channel.readyState == "open":
                    self.channel.send(message)
                else:
                    log("Channel not ready.")

async def main():
    chat_node = WebRTCTerminalChat()
    chat_node._loop = asyncio.get_running_loop()
    chat_node.connect_mqtt()

    print("-" * 45)
    print(f"🚀 WEB RTC TERMINAL CHAT NODE ONLINE")
    print(f"YOUR ID: {chat_node.peer_id}")
    print(f"Waiting for peer discovery...")
    print("-" * 45)

    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting chat...")
