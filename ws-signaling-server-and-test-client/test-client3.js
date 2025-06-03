const WebSocket = require('ws');

const serverUrl = 'wss://192.168.29.9/ws/';

const ws = new WebSocket(serverUrl, {
  rejectUnauthorized: false, // Only if you use self-signed certs, remove if using trusted cert
});

ws.on('open', () => {
  console.log('Connected to WSS via NGINX');

  ws.send(JSON.stringify({
    type: 'offer',
    sdp: 'dummy sdp data',
    from: 'client1'
  }));
});

ws.on('message', (data) => {
  console.log('Received:', data.toString());
});

ws.on('error', (err) => {
  console.error('WSS error:', err);
});

ws.on('close', () => {
  console.log('Disconnected from WSS');
});
