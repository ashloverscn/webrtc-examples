import asyncio
import json
import uuid
import logging
import numpy as np
import fractions
import sys
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, MediaStreamTrack
from av import VideoFrame

# Native Raspberry Pi camera components
from picamera2 import Picamera2

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WebRTC-PiServer")

class PiCameraVideoTrack(MediaStreamTrack):
    """
    Captures live frames directly from the Raspberry Pi PiCamera2 module
    using the optimized hardware ISP pipeline.
    """
    kind = "video"

    def __init__(self, width=640, height=480, fps=30.0):
        super().__init__()
        self.counter = 0
        self._time_base = fractions.Fraction(1, 90000)
        self.fps = fps
        self.frame_delay = 1.0 / self.fps
        
        logger.info("Initializing Picamera2 core components...")
        self.picam2 = Picamera2()
        
        # Configure the hardware pipeline for continuous video streaming
        config = self.picam2.create_video_configuration(
            main={"format": "RGB888", "size": (width, height)}
        )
        self.picam2.configure(config)
        self.picam2.start()
        logger.info(f"🚀 Picamera2 started successfully at {width}x{height} @ {self.fps} FPS")

    async def recv(self):
        # Calculate WebRTC 90kHz presentation timestamps
        pts = int(self.counter * (90000 / self.fps))
        
        try:
            # Capture frame natively as an RGB NumPy array directly from hardware memory
            frame_rgb = self.picam2.capture_array("main")
            
            # Wrap array structure natively within PyAV object layers
            video_frame = VideoFrame.from_ndarray(frame_rgb, format="rgb24")
            video_frame.pts = pts
            video_frame.time_base = self._time_base
            
            self.counter += 1
            
            # Pacing match matching the target framerate
            await asyncio.sleep(self.frame_delay)
            return video_frame
            
        except Exception as e:
            logger.error(f"Error capturing frame from Picamera2: {e}")
            # Standby placeholder frame generation if camera encounters a hardware hitch
            await asyncio.sleep(0.1)
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            return VideoFrame.from_ndarray(frame, format="rgb24")

    def stop(self):
        # Cleanly release the Raspberry Pi hardware camera memory locks
        try:
            if hasattr(self, 'picam2'):
                self.picam2.stop()
                self.picam2.close()
                logger.info("Picamera2 hardware module cleanly released.")
        except Exception as e:
            logger.error(f"Exception while closing Picamera2: {e}")
        super().stop()


class WebRTCServer:
    def __init__(self):
        self.peer_id = f"pi_cam_{uuid.uuid4().hex[:6]}"
        self.pc = None
        self.current_track = None

    async def handle_index(self, request):
        """HTTP Endpoint: Serves the Web Player UI embedded directly into the script."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Raspberry Pi WebRTC Camera</title>
            <style>
                body { font-family: system-ui, sans-serif; text-align: center; background: #111; color: #eee; padding: 40px; }
                .card { max-width: 640px; margin: 0 auto; background: #222; padding: 25px; border-radius: 12px; border: 1px solid #333; }
                video { width: 100%; background: #000; border-radius: 8px; margin-top: 20px; border: 1px solid #444; }
                button { background: #d71920; color: white; border: none; padding: 12px 28px; border-radius: 6px; cursor: pointer; font-size: 16px; font-weight: bold; }
                button:hover { background: #b11218; }
                #status { color: #d71920; font-weight: bold; }
            </style>
        </head>
        <body>
            <div class="card">
                <h2>Raspberry Pi PiCamera2 WebRTC Feed</h2>
                <p>Status: <span id="status">Ready</span></p>
                <button id="startBtn">Start Pi Camera Feed</button>
                <video id="remoteVideo" autoplay playsinline controls></video>
            </div>
            <script>
                const wsProtocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
                const socket = new WebSocket(`${wsProtocol}${window.location.host}/ws`);
                let pc = null;

                document.getElementById('startBtn').onclick = async () => {
                    document.getElementById('status').innerText = "Initializing connection...";
                    
                    if(pc) { pc.close(); }
                    pc = new RTCPeerConnection();
                    
                    pc.addTransceiver('video', { direction: 'recvonly' });
                    
                    pc.ontrack = (event) => {
                        document.getElementById('status').innerText = "Streaming Live!";
                        document.getElementById('remoteVideo').srcObject = event.streams[0];
                    };

                    pc.onicecandidate = (event) => {
                        if (event.candidate) {
                            socket.send(JSON.stringify({
                                type: 'ice',
                                data: {
                                    sdpMid: event.candidate.sdpMid,
                                    sdpMLineIndex: event.candidate.sdpMLineIndex,
                                    candidate: event.candidate.candidate
                                }
                            }));
                        }
                    };

                    const offer = await pc.createOffer();
                    await pc.setLocalDescription(offer);
                    
                    socket.send(JSON.stringify({
                        type: 'offer',
                        data: { sdp: offer.sdp, type: offer.type }
                    }));
                };

                socket.onmessage = async (event) => {
                    const msg = JSON.parse(event.data);
                    if (msg.type === 'answer') {
                        document.getElementById('status').innerText = "Handshake Complete. Receiving Media...";
                        await pc.setRemoteDescription(new RTCSessionDescription(msg.data));
                    } else if (msg.type === 'ice') {
                        await pc.addIceCandidate(new RTCIceCandidate(msg.data));
                    }
                };
            </script>
        </body>
        </html>
        """
        return web.Response(text=html_content, content_type='text/html')

    async def handle_websocket(self, request):
        """WebSocket Endpoint: Handles WebRTC signaling handshakes."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        logger.info("WebSocket Client connected to Raspberry Pi.")

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    payload = json.loads(msg.data)
                    msg_type = payload.get("type")

                    if msg_type == "offer":
                        logger.info("📥 WebRTC Offer received via WebSocket.")
                        await self.handle_offer(payload.get("data"), ws)
                    elif msg_type == "ice":
                        await self.handle_ice(payload.get("data"))
        except Exception as e:
            logger.error(f"WebSocket execution error: {e}")
        finally:
            logger.info("WebSocket Client connection lifecycle ended.")
        return ws

    async def handle_offer(self, data, ws):
        if self.pc: 
            await self.pc.close()
            if self.current_track:
                self.current_track.stop()

        self.pc = RTCPeerConnection()
        
        # Hooks directly into Raspberry Pi Picamera2 track
        self.current_track = PiCameraVideoTrack(width=640, height=480, fps=30.0)
        self.pc.addTrack(self.current_track)
        
        @self.pc.on("icecandidate")
        async def on_candidate(candidate):
            if candidate and not ws.closed:
                await ws.send_str(json.dumps({
                    "type": "ice", 
                    "data": {
                        "sdpMid": candidate.sdpMid, 
                        "sdpMLineIndex": candidate.sdpMLineIndex, 
                        "candidate": candidate.candidate
                    }
                }))

        @self.pc.on("connectionstatechange")
        async def on_state_change():
            logger.info(f"WebRTC Connection State: {self.pc.connectionState}")
            if self.pc.connectionState in ["failed", "closed"]:
                if self.current_track: 
                    self.current_track.stop()

        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        
        if not ws.closed:
            await ws.send_str(json.dumps({
                "type": "answer", 
                "data": {
                    "sdp": self.pc.localDescription.sdp, 
                    "type": self.pc.localDescription.type
                }
            }))

    async def handle_ice(self, data):
        if self.pc:
            candidate = RTCIceCandidate(
                sdpMid=data["sdpMid"], 
                sdpMLineIndex=data["sdpMLineIndex"], 
                candidate=data["candidate"]
            )
            await self.pc.addIceCandidate(candidate)


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    server_instance = WebRTCServer()
    app = web.Application()
    
    app.router.add_get('/', server_instance.handle_index)
    app.router.add_get('/ws', server_instance.handle_websocket)
    
    print("-" * 60)
    print("🚀 RASPBERRY PI PICAMERA2 WEBRTC SERVER ONLINE")
    print("Open Link: http://<your_pi_ip_address_here>:8080")
    print("-" * 60)
    
    # CHANGED: Run server binding on unprivileged port 8080 to fix permission restrictions
    web.run_app(app, host='0.0.0.0', port=8080)
