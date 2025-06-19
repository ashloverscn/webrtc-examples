const { io } = require("socket.io-client");
const https = require("https");

const httpsAgent = new https.Agent({
  rejectUnauthorized: false // Accept self-signed certificates
});

const socket = io("https://ash-temp-new-52546.portmap.io:52546", {
  transports: ["websocket"],
  agent: httpsAgent,
  reconnectionAttempts: 3,
  timeout: 5000
});

const ROOM = "testroom";
let trackedPeerId = null;

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
  if (!trackedPeerId) {
    trackedPeerId = from;
    console.log(`📌 Tracking peer: ${trackedPeerId}`);
  }
});

socket.on("peer-joined", (peerId) => {
  console.log(`👥 Peer joined: ${peerId}`);
  trackedPeerId = peerId;
  console.log(`📌 Tracking peer: ${trackedPeerId}`);
});

socket.on("peer-left", (peerId) => {
  console.log(`👋 Peer left: ${peerId}`);
  if (peerId === trackedPeerId) {
    console.log(`📡 Peer ${peerId} is now Offline ❌`);
  }
});

socket.on("presence-response", ({ peerId, isOnline }) => {
  const presence = isOnline ? "Online ✅" : "Offline ❌";
  console.log(`📡 Presence of ${peerId}: ${presence}`);
});

socket.on("disconnect", () => {
  console.log("❌ Disconnected from signaling server");
});

socket.on("connect_error", (err) => {
  console.error("❌ Connection error:", err.message);
});

// Periodically check presence of tracked peer
setInterval(() => {
  if (trackedPeerId) {
    socket.emit("presence-check", trackedPeerId);
  }
}, 3000);
