const WebSocket = require('ws');
const wss = new WebSocket.Server({ host: '0.0.0.0', port: 8080 });

let clients = [];

function heartbeat() {
  this.isAlive = true;
}

wss.on('connection', (ws, req) => {
  const clientIP = req.socket.remoteAddress;
  ws.isAlive = true;
  ws.on('pong', heartbeat);

  clients.push(ws);
  console.log(`[+] Client connected from ${clientIP}. Total clients: ${clients.length}`);

  ws.on('message', message => {
    try {
      const data = JSON.parse(message);
      console.log(`[>] Message from ${clientIP}:`, data.type || "UNKNOWN", data);

      // Broadcast to all other clients
      clients.forEach(client => {
        if (client !== ws && client.readyState === WebSocket.OPEN) {
          client.send(JSON.stringify(data));
          console.log(`[<] Relayed ${data.type} to another client.`);
        }
      });

    } catch (err) {
      console.warn(`[!] Invalid JSON from ${clientIP}:`, message);
    }
  });

  ws.on('close', () => {
    clients = clients.filter(c => c !== ws);
    console.log(`[-] Client from ${clientIP} disconnected. Total clients: ${clients.length}`);
  });

  ws.on('error', err => {
    console.error(`[!] Error from ${clientIP}:`, err);
  });
});

// Ping clients every 30s to detect dead ones
const interval = setInterval(() => {
  clients.forEach(ws => {
    if (ws.isAlive === false) {
      console.log(`[*] Terminating unresponsive client.`);
      ws.terminate();
      clients = clients.filter(c => c !== ws);
      return;
    }
    ws.isAlive = false;
    ws.ping(() => {});
  });
}, 30000);

wss.on('close', () => {
  clearInterval(interval);
});
