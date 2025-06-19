<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>WebRTC Signaling Auto-Test Client</title>
</head>
<body>
  <h2>🔗 WebRTC Signaling Auto-Test Client</h2>

  <p>Status: <span id="status">Connecting...</span></p>
  <p>Your Socket ID: <span id="socket-id">N/A</span></p>
  <p>Room: <strong>testroom</strong></p>

  <hr>

  <pre id="log" style="background:#f0f0f0;padding:10px;max-height:300px;overflow:auto;border:1px solid #ccc;"></pre>

  <!-- Load Socket.IO -->
  <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
  <script>
    const room = "testroom";
    const statusEl = document.getElementById("status");
    const socketIdEl = document.getElementById("socket-id");
    const logEl = document.getElementById("log");

    function log(...args) {
      logEl.textContent += args.map(a =>
        typeof a === 'object' ? JSON.stringify(a, null, 2) : a
      ).join(' ') + "\n";
      logEl.scrollTop = logEl.scrollHeight;
    }

    const socket = io("https://ash-temp-new-52546.portmap.io:52546", {
      transports: ["websocket"]
    });

    socket.on("connect", () => {
      statusEl.textContent = "Connected ✅";
      socketIdEl.textContent = socket.id;
      log("✅ Connected as:", socket.id);

      socket.emit("join", room);
      log("🚪 Joined room:", room);

      setTimeout(() => {
        const fakeSignal = { type: "test-offer", sdp: "dummy sdp" };
        socket.emit("signal", {
          room,
          signalData: fakeSignal
        });
        log("📤 Auto-sent test signal to room:", room, fakeSignal);
      }, 1500);
    });

    socket.on("disconnect", () => {
      statusEl.textContent = "Disconnected ❌";
      log("❌ Disconnected from server");
    });

    socket.on("connect_error", (err) => {
      statusEl.textContent = "Error ❌";
      log("❌ Connection error:", err.message);
    });

    socket.on("peer-joined", (peerId) => {
      log("👥 Peer joined:", peerId);
    });

    socket.on("peer-left", (peerId) => {
      log("👋 Peer left:", peerId);
    });

    socket.on("signal", ({ from, signalData }) => {
      log("📥 Signal received from", from, signalData);
    });
  </script>
</body>
</html>
