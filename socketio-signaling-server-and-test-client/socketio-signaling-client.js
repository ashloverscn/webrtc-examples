const { io } = require("socket.io-client");

// Use Pi's IP address instead of hostname
const socket = io("http://192.168.29.9:3000");

socket.on("connect", () => {
  console.log("✅ Connected to server as:", socket.id);

  const room = "testroom";
  socket.emit("join", room);
  console.log("🚪 Joined room:", room);

  // Send test signal
  setTimeout(() => {
    const fakeSignal = { type: "offer", sdp: "dummy sdp" };
    console.log("📤 Sending signal to room:", room);
    socket.emit("signal", { room, signalData: fakeSignal });
  }, 1000);
});

socket.on("signal", ({ from, signalData }) => {
  console.log("📥 Signal received from", from, signalData);
});

socket.on("peer-joined", (peerId) => {
  console.log("👥 Peer joined:", peerId);
});

socket.on("disconnect", () => {
  console.log("❌ Disconnected from server");
});

socket.on("connect_error", (err) => {
  console.error("❌ Connection error:", err.message);
});
