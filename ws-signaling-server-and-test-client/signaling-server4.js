const WebSocket = require('ws');
const wss = new WebSocket.Server({ port: 8080 });

let peers = [];

wss.on('connection', function connection(ws) {
  peers.push(ws);

  ws.on('message', function incoming(message) {
    // Broadcast to other peers
    peers.forEach(function each(client) {
      if (client !== ws && client.readyState === WebSocket.OPEN) {
        client.send(message);
      }
    });
  });

  ws.on('close', function () {
    peers = peers.filter(p => p !== ws);
  });
});
