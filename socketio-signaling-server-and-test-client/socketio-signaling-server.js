const express = require('express');
const http = require('http');
const { Server } = require('socket.io');

const PORT = process.env.PORT || 3000;
const HOST = '0.0.0.0';

const app = express();
const server = http.createServer(app);
const io = new Server(server, {
  cors: { origin: "*" }
});

app.get('/health', (req, res) => {
  res.send('✅ Signaling server is alive');
});

io.on('connection', (socket) => {
  console.log(`🔌 Client connected: ${socket.id}`);

  socket.on('join', (room) => {
    socket.join(room);
    console.log(`👥 ${socket.id} joined room: ${room}`);
    socket.to(room).emit('peer-joined', socket.id);
  });

  socket.on('signal', ({ room, signalData, to }) => {
    if (to) {
      io.to(to).emit('signal', { from: socket.id, signalData });
      console.log(`📤 ${socket.id} ➡️ ${to} | signal:`, signalData?.type || '[object]');
    } else if (room) {
      socket.to(room).emit('signal', { from: socket.id, signalData });
      console.log(`📢 ${socket.id} 🕊 broadcast in ${room} |`, signalData?.type || '[object]');
    }
  });

  socket.on('presence-check', (peerId) => {
    const isOnline = io.sockets.sockets.has(peerId);
    socket.emit('presence-response', { peerId, isOnline });
  });

  socket.on('list-peers', () => {
    const peers = [...io.sockets.sockets.keys()];
    socket.emit('peer-list', peers);
  });

  socket.on('disconnect', () => {
    console.log(`❌ Disconnected: ${socket.id}`);
    for (const room of socket.rooms) {
      if (room !== socket.id) {
        socket.to(room).emit('peer-left', socket.id);
      }
    }
  });
});

server.listen(PORT, HOST, () => {
  console.log(`🚀 Signaling server running on http://${HOST}:${PORT}`);
});
