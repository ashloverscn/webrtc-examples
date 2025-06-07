const https = require('https');
const fs = require('fs');
const WebSocket = require('ws');

// Load your self-signed cert and key
const serverOptions = {
  cert: fs.readFileSync('./cert.pem'), // path to your cert file
  key: fs.readFileSync('./key.pem')    // path to your private key file
};

// Create HTTPS server
const httpsServer = https.createServer(serverOptions);

// Create WebSocket server attached to HTTPS server (WSS)
const wss = new WebSocket.Server({ server: httpsServer });

let clients = [];

wss.on('connection', (ws, req) => {
  const clientIP = req.socket.remoteAddress;
  console.log(`[+] Client connected from ${clientIP}. Total clients: ${clients.length + 1}`);

  clients.push(ws);

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
      console.warn(`Invalid JSON from ${clientIP}:`, message);
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

// Listen on port 8080 with HTTPS + WSS
httpsServer.listen(8080, () => {
  console.log('HTTPS + WSS server listening on port 8080');
});
