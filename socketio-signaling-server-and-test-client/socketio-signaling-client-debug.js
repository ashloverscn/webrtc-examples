process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';

const { io } = require('socket.io-client');

const SERVER_URL = "https://ash-temp-new-52546.portmap.io:52546";
const ROOM = "testroom";

console.log("🔗 Connecting to signaling server...");

const socket = io(SERVER_URL, {
  transports: ["websocket"],
  reconnectionAttempts: 2,
  timeout: 5000,
});

socket.on("connect", () => {
  console.log(`✅ Connected as ${socket.id}`);
  socket.emit("join", ROOM);
  console.log(`🚪 Joined room: ${ROOM}`);
});

socket.on("connect_error", (err) => {
  console.error("❌ Connection error:");
  console.error(err);  // Full error object
});

socket.on("disconnect", () => {
  console.log("❌ Disconnected from signaling server");
});
