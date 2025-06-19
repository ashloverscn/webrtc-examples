const { io } = require('socket.io-client');
const https = require('https');

// ⛔️ WARNING: This disables certificate verification
const httpsAgent = new https.Agent({
  rejectUnauthorized: false
});

const socket = io("https://ash-temp-new-52546.portmap.io:52546", {
  transports: ["websocket"],
  agent: httpsAgent, // 👈 important fix
  reconnectionAttempts: 3,
  timeout: 5000
});

const ROOM = "testroom";

console.log("🔗 Connecting to signaling server...");

socket.on("connect", () => {
  console.log(`✅ Connected as ${socket.id}`);
  socket.emit("join", ROOM);
  console.log(`🚪 Joined room: ${ROOM}`);

  setTimeout(() => {
    const testSignal = {
      type: "node-test-offer",
      sdp: "this-is-a-dummy-sdp-from-node-client"
    };
    socket.emit("signal", {
      room: ROOM,
      signalData: testSignal
    });
    console.log("📤 Sent test signal:", testSignal);
  }, 2000);
});

socket.on("signal", ({ from, signalData }) => {
  console.log(`📥 Received signal from ${from}:`, signalData);
});

socket.on("peer-joined", (peerId) => {
  console.log(`👥 Peer joined: ${peerId}`);
});

socket.on("peer-left", (peerId) => {
  console.log(`👋 Peer left: ${peerId}`);
});

socket.on("disconnect", () => {
  console.log("❌ Disconnected from signaling server");
});

socket.on("connect_error", (err) => {
  console.error("❌ Connection error:", err.message);
});
