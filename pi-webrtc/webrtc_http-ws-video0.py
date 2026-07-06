import asyncio
import json
import uuid
import logging
import numpy as np
import fractions
import cv2
import sys
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, MediaStreamTrack
from av import VideoFrame

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WebRTC-CameraServer")

class CameraVideoTrack(MediaStreamTrack):
    """
    Captures live frames from the system's hardware camera (Video 0)
    and pipes them straight out over the WebRTC media channel.
    """
    kind = "video"

    def __init__(self, camera_index=0):
        super().__init__()
        self.counter = 0
        self._time_base = fractions.Fraction(1, 90000)
        self.camera_index = camera_index
        
        # Open hardware camera capture context
        self.cap = cv2.VideoCapture(self.camera_index)
        if not self.cap.isOpened():
            logger.error(f"Failed to open hardware camera at index {self.camera_index}")
            raise RuntimeError(f"Could not open hardware camera index {self.camera_index}. Is it in use by another app?")
            
        # Get or fallback baseline properties
        self.fps = self.cap.get(cv2.CAP_PROP_FPS)
        if self.fps <= 0 or np.isnan(self.fps):
            self.fps = 30.0  
            
        self.frame_delay = 1.0 / self.fps
        logger.info(f"Initialized Live Camera Track (Index: {self.camera_index} at {self.fps} FPS)")

    async def recv(self):
        # Calculate WebRTC 90kHz presentation timestamps
        pts = int(self.counter * (90000 / self.fps))
        
        # Grab frame from live video capture card/device
        ret, frame = self.cap.read()
        
        if not ret:
            logger.warning("Camera frame read failed. Generating placeholder...")
            await asyncio.sleep(0.1)
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(frame, "CAMERA READ ERROR", (50, 240),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        
        # Convert frame matrix color layouts (BGR to standard WebRTC RGB structure)
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # Wrap array structure natively within PyAV object layers
        video_frame = VideoFrame.from_ndarray(frame_rgb, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = self._time_base
        
        self.counter += 1
        
        # Precise pace tracking matching the camera input rate
        await asyncio.sleep(self.frame_delay)
        return video_frame

    def stop(self):
        # Release system hardware hooks cleanly when client leaves
        if self.cap.isOpened():
            self.cap.release()
            logger.info(f"Hardware camera index {self.camera_index} released.")
        super().stop()


class WebRTCServer:
    def __init__(self):
        self.peer_id = f"live_cam_{uuid.uuid4().hex[:6]}"
        self.pc = None
        self.current_track = None

    async def handle_index(self, request):
        """HTTP Endpoint: Serves the Web Player UI embedded directly into the python script."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>WebRTC Live Camera</title>
            <style>
                body { font-family: system-ui, sans-serif; text-align: center; background: #111; color: #eee; padding: 40px; }
                .card { max-width: 640px; margin: 0 auto; background: #222; padding: 25px; border-radius: 12px; border: 1px solid #333; }
                video { width: 100%; background: #000; border-radius: 8px; margin-top: 20px; border: 1px solid #444; }
                button { background: #28a745; color: white; border: none; padding: 12px 28px; border-radius: 6px; cursor: pointer; font-size: 16px; font-weight: bold; }
                button:hover { background: #218838; }
                #status { color: #28a745; font-weight: bold; }
            </style>
        </head>
        <body>
            <div class="card">
                <h2>Live Hardware Camera Streamer</h2>
                <p>Status: <span id="status">Ready</span></p>
                <button id="startBtn">Start Live Camera Feed</button>
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
                        document.getElementById('status').innerText = "SDP Handshake Complete. Activating Feed...";
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
        logger.info("WebSocket Client handshake active.")

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    payload = json.loads(msg.data)
                    msg_type = payload.get("type")

                    if msg_type == "offer":
                        logger.info("📥 WebRTC Offer received via WebSocket connection.")
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
        
        # CHANGED: Hooks directly into system video source index 0 instead of a file string
        self.current_track = CameraVideoTrack(camera_index=0)
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
    print("🚀 UNIFIED LIVE CAMERA SERVER ONLINE")
    print("Open Link: http://localhost")
    print("-" * 60)
    
    web.run_app(app, host='0.0.0.0', port=80)
