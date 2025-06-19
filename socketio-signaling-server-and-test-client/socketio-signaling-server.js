const express = require('express');
const http = require('http');
const { Server } = require('socket.io');

const PORT = process.env.PORT || 3000;
const HOST = '0.0.0.0'; // Listen on all interfaces

const app = express();
const server = http.createServer(app);
const io = new Server(server, {
  cors: { origin: "*" } // Relax CORS for testing; tighten later for production
});

// Optional health check endpoint
app.get('/health', (req, res) => {
  res.send('✅ Signaling server is alive');
});

// Event handling
io.on('connection', (socket) => {
  console.log(`🔌 Client connected: ${socket.id}`);

  // Join a room
  socket.on('join', (room) => {
    socket.join(room);
    console.log(`🧑‍🤝‍🧑 ${socket.id} joined room: ${room}`);

    // Notify others in the room
    socket.to(room).emit('peer-joined', socket.id);
  });

  // Handle incoming signaling messages
  socket.on('signal', ({ room, signalData, to }) => {
    if (to) {
      // Direct signaling to specific peer
      io.to(to).emit('signal', { from: socket.id, signalData });
      console.log(`📡 ${socket.id} ➡️ ${to} | signal:`, signalData?.type || '[object]');
    } else if (room) {
      // Broadcast to all in the room except sender
      socket.to(room).emit('signal', { from: socket.id, signalData });
      console.log(`📡 ${socket.id} 🕸 broadcast in ${room} |`, signalData?.type || '[object]');
    }
  });

  // Clean up on disconnect
  socket.on('disconnect', () => {
    console.log(`❌ Disconnected: ${socket.id}`);
    socket.broadcast.emit('peer-left', socket.id);
  });
});

// Start server
server.listen(PORT, HOST, () => {
  console.log(`🚀 Signaling server running on http://${HOST}:${PORT}`);
});
