#!/bin/bash

set -e

###############################
# 1. Node.js Reinstallation
###############################

echo "=== [1/4] Removing existing Node.js and npm ==="
sudo apt remove --purge -y nodejs npm
sudo rm -rf /etc/apt/sources.list.d/nodesource.list
sudo rm -rf /etc/apt/keyrings/nodesource.gpg
sudo apt autoremove -y
sudo apt clean

echo "=== Cleaning old binaries and configs ==="
sudo rm -f /usr/local/bin/node
sudo rm -f /usr/local/bin/npm
sudo rm -rf /usr/local/lib/node_modules
sudo rm -rf ~/.npm
sudo rm -rf ~/.node-gyp

echo "=== Updating package index ==="
sudo apt update

echo "=== Installing required tools ==="
sudo apt install -y curl ca-certificates gnupg

echo "=== Adding NodeSource GPG key ==="
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | \
  sudo gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg

echo "=== Creating NodeSource repo for Node.js 20.x ==="
NODE_MAJOR=20
echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_$NODE_MAJOR.x nodistro main" | \
  sudo tee /etc/apt/sources.list.d/nodesource.list

echo "=== Updating package index again ==="
sudo apt update

echo "=== Installing Node.js and npm ==="
sudo apt install -y nodejs

echo "=== Verifying Node.js installation ==="
node -v
npm -v

echo "✅ Node.js 20.x LTS reinstalled successfully!"


###############################
# 2. WebSocket Server Setup
###############################

echo "=== [2/4] Removing old WebSocket server and systemd service ==="
sudo rm -f /var/www/html/ws-signaling-server-and-test-client/ws-signaling-server.js
sudo rm -f /etc/systemd/system/ws-signaling.service

echo "=== Creating server directory ==="
sudo mkdir -p /var/www/html/ws-signaling-server-and-test-client

echo "=== [3/4] Writing ws-signaling-server.js ==="
sudo tee /var/www/html/ws-signaling-server-and-test-client/ws-signaling-server.js > /dev/null <<'EOF'
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
EOF

###############################
# 3. Systemd Service Setup
###############################

echo "=== [4/4] Writing ws-signaling.service ==="
sudo tee /etc/systemd/system/ws-signaling.service > /dev/null <<'EOF'
[Unit]
Description=WebSocket WS-Signaling Server (Node.js)
After=network.target

[Service]
ExecStart=/usr/bin/node /var/www/html/ws-signaling-server-and-test-client/ws-signaling-server.js
WorkingDirectory=/var/www/html/ws-signaling-server-and-test-client
Restart=always
RestartSec=5
User=root
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
EOF

echo "=== Reloading systemd and starting WebSocket service ==="
sudo systemctl daemon-reexec
sudo systemctl daemon-reload
sudo systemctl enable ws-signaling
sudo systemctl restart ws-signaling

echo "✅ WebSocket signaling server installed and running!"
