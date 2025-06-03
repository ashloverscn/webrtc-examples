const WebSocket = require('ws');

const serverUrl = 'ws://192.168.29.9:8080';

const ws = new WebSocket(serverUrl);

ws.on('open', () => {
  console.log('Connected to server.');

  const testMessage = {
    type: 'offer',
    sdp: 'dummy sdp data',
    from: 'client1'
  };
  ws.send(JSON.stringify(testMessage));
  console.log('Sent test message:', testMessage);
});

ws.on('message', (data) => {
  try {
    const message = JSON.parse(data);
    console.log('Received message:', message);
  } catch (e) {
    console.warn('Received non-JSON message:', data);
  }
});

ws.on('close', () => {
  console.log('Disconnected from server.');
});

ws.on('error', (err) => {
  console.error('WebSocket error:', err);
});
