const WebSocket = require('ws');

const ws = new WebSocket('wss://192.168.29.9/ws');

ws.on('open', () => {
  console.log('Connected to WebSocket server');
  ws.send('Hello from Node.js client');
});

ws.on('message', message => {
  console.log('Received:', message);
});

ws.on('error', err => {
  console.error('WebSocket error:', err);
});

ws.on('close', () => {
  console.log('Connection closed');
});
