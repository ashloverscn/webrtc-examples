const { io } = require('socket.io-client');

const SERVER_URL = "https://ash-temp-new-52546.portmap.io:52546"; // Update if needed
const ROOM = "testroom";

console.log("🔗 Connecting to signaling server...");

const socket = io(SERVER_URL, {
  transports: ["websocket"],
  reconnectionAttempts: 3,
  timeout: 5000,
});

socket.on("connect", () => {
  console.log(`✅ Connected as ${socket.id}`);
  
  socket.emit("join", ROOM);
  console.log(`🚪 Joined room: ${ROOM}`);

  // Send a test signal after 2 seconds
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
