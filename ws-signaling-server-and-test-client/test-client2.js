const WebSocket = require('ws');

const serverUrl = 'ws://192.168.29.9:8080';
let ws;
let reconnectInterval = 5000; // ms

function connect() {
  ws = new WebSocket(serverUrl);

  ws.on('open', () => {
    console.log('[*] Connected to signaling server.');

    // Send an offer after connection
    sendMessage({
      type: 'offer',
      sdp: 'dummy sdp offer data',
      from: 'client1'
    });

    // Send an ICE candidate after 3 seconds
    setTimeout(() => {
      sendMessage({
        type: 'candidate',
        candidate: 'dummy ice candidate',
        from: 'client1'
      });
    }, 3000);

    // Send an answer after 6 seconds
    setTimeout(() => {
      sendMessage({
        type: 'answer',
        sdp: 'dummy sdp answer data',
        from: 'client1'
      });
    }, 6000);

    // Keep sending ping every 25 seconds to keep connection alive
    const pingInterval = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.ping();
        console.log('[*] Sent ping');
      } else {
        clearInterval(pingInterval);
      }
    }, 25000);
  });

  ws.on('message', (data) => {
    try {
      const message = JSON.parse(data);
      console.log('[<-] Received:', message);
    } catch (err) {
      console.warn('[!] Non-JSON message:', data);
    }
  });

  ws.on('close', () => {
    console.log('[*] Connection closed. Reconnecting in 5 seconds...');
    setTimeout(connect, reconnectInterval);
  });

  ws.on('error', (err) => {
    console.error('[!] WebSocket error:', err);
  });

  ws.on('pong', () => {
    console.log('[*] Received pong from server.');
  });
}

function sendMessage(msg) {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
    console.log('[->] Sent:', msg);
  } else {
    console.warn('[!] Cannot send message, connection not open');
  }
}

connect();
