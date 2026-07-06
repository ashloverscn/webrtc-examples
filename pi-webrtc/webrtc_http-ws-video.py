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
logger = logging.getLogger("WebRTC-Server")

class FileVideoTrack(MediaStreamTrack):
    """
    Streams a local MP4 file cleanly through the WebRTC data pipeline.
    Loops seamlessly back to frame 0 when reaching EOF.
    """
    kind = "video"

    def __init__(self, video_path="./test.mp4"):
        super().__init__()
        self.counter = 0
        self._time_base = fractions.Fraction(1, 90000)
        self.video_path = video_path
        
        self.cap = cv2.VideoCapture(self.video_path)
        if not self.cap.isOpened():
            logger.error(f"Failed to open video file: {self.video_path}")
            raise FileNotFoundError(f"Could not open '{self.video_path}' in the current folder. Please place an MP4 file here.")
            
        self.fps = self.cap.get(cv2.CAP_PROP_FPS)
        if self.fps <= 0 or np.isnan(self.fps):
            self.fps = 30.0  
            
        self.frame_delay = 1.0 / self.fps
        logger.info(f"Initialized Video Track for {self.video_path} ({self.fps} FPS)")

    async def recv(self):
        pts = int(self.counter * (90000 / self.fps))
        ret, frame = self.cap.read()
        
        if not ret:
            logger.info("Looping video stream back to beginning...")
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = self.cap.read()
            if not ret:
                await asyncio.sleep(0.1)
                frame = np.zeros((480, 640, 3), dtype=np.uint8)
                cv2.putText(frame, "FILE ERROR / READ FAILED", (50, 240),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        video_frame = VideoFrame.from_ndarray(frame_rgb, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = self._time_base
        
        self.counter += 1
        await asyncio.sleep(self.frame_delay)
        return video_frame

    def stop(self):
        if self.cap.isOpened():
            self.cap.release()
        super().stop()


class WebRTCServer:
    def __init__(self):
        self.peer_id = f"file_cam_{uuid.uuid4().hex[:6]}"
        self.pc = None
        self.current_track = None

    async def handle_index(self, request):
        """HTTP Endpoint: Serves the Web Player UI embedded directly into the python script."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>WebRTC Player</title>
            <style>
                body { font-family: system-ui, sans-serif; text-align: center; background: #111; color: #eee; padding: 40px; }
                .card { max-width: 640px; margin: 0 auto; background: #222; padding: 25px; border-radius: 12px; border: 1px solid #333; }
                video { width: 100%; background: #000; border-radius: 8px; margin-top: 20px; border: 1px solid #444; }
                button { background: #007BFF; color: white; border: none; padding: 12px 28px; border-radius: 6px; cursor: pointer; font-size: 16px; font-weight: bold; }
                button:hover { background: #0056b3; }
                #status { color: #007BFF; font-weight: bold; }
            </style>
        </head>
        <body>
            <div class="card">
                <h2>Live MP4 WebRTC Streamer</h2>
                <p>Status: <span id="status">Ready</span></p>
                <button id="startBtn">Connect & Play Stream</button>
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
                    
                    // Request video track reception
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

                    // Create WebRTC SDP offer
                    const offer = await pc.createOffer();
                    await pc.setLocalDescription(offer);
                    
                    // Send signaling offer over the exact same socket
                    socket.send(JSON.stringify({
                        type: 'offer',
                        data: { sdp: offer.sdp, type: offer.type }
                    }));
                };

                socket.onmessage = async (event) => {
                    const msg = JSON.parse(event.data);
                    if (msg.type === 'answer') {
                        document.getElementById('status').innerText = "SDP Handshake Complete. Establishing WebRTC...";
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
        self.current_track = FileVideoTrack("./test.mp4")
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
    # Force Selector Loop Policy to eliminate Windows Proactor pipe socket bugs entirely
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    server_instance = WebRTCServer()
    app = web.Application()
    
    # Map simple routes to our class instance methods
    app.router.add_get('/', server_instance.handle_index)
    app.router.add_get('/ws', server_instance.handle_websocket)
    
    print("-" * 60)
    print("🚀 UNIFIED PYTHON WEB SERVER ONLINE")
    print("Open Link: http://localhost")
    print("-" * 60)
    
    # aiohttp's native app runner manages connection drops and lifecycles robustly
    web.run_app(app, host='0.0.0.0', port=80)
