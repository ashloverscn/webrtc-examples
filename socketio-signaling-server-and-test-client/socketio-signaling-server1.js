const express = require('express');
const http = require('http');
const { Server } = require('socket.io');

const app = express();
const server = http.createServer(app);
const io = new Server(server, {
  cors: { origin: "*" }
});

io.on('connection', (socket) => {
  console.log(`🔌 New client connected: ${socket.id}`);

  socket.on('join', (room) => {
    socket.join(room);
    console.log(`🧑‍🤝‍🧑 ${socket.id} joined room: ${room}`);
    socket.to(room).emit('peer-joined', socket.id);
  });

  socket.on('signal', (data) => {
    const { room, signalData, to } = data;
    if (to) {
      io.to(to).emit('signal', {
        from: socket.id,
        signalData
      });
    } else {
      socket.to(room).emit('signal', {
        from: socket.id,
        signalData
      });
    }
  });

  socket.on('disconnect', () => {
    console.log(`❌ Client disconnected: ${socket.id}`);
    socket.broadcast.emit('peer-left', socket.id);
  });
});

const PORT = process.env.PORT || 3000;
server.listen(PORT, '0.0.0.0', () => {
  console.log(`🚀 Signaling server running on http://0.0.0.0:${PORT}`);
});
