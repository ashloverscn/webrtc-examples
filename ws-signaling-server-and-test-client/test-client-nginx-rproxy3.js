const WebSocket = require('ws');

const serverUrl = 'wss://192.168.29.9/ws/'; // Change to your actual WSS URL

const ws = new WebSocket(serverUrl, {
  rejectUnauthorized: false // Only needed if you're using a self-signed cert
});

ws.on('open', () => {
  console.log('[✔] Connected to secure signaling server via WSS');

  setTimeout(() => {
    const offer = {
      type: 'offer',
      sdp: 'v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n...',
      from: 'clientA'
    };
    console.log('[↑] Sending OFFER...');
    ws.send(JSON.stringify(offer));
  }, 1000);

  setTimeout(() => {
    const candidate = {
      type: 'candidate',
      candidate: 'candidate:1 1 UDP 2122252543 192.168.29.10 53705 typ host',
      from: 'clientA'
    };
    console.log('[↑] Sending ICE Candidate...');
    ws.send(JSON.stringify(candidate));
  }, 2500);

  setTimeout(() => {
    const ping = { type: 'ping', time: Date.now(), from: 'clientA' };
    console.log('[↑] Sending PING...');
    ws.send(JSON.stringify(ping));
  }, 4000);
});

ws.on('message', (data) => {
  try {
    const msg = JSON.parse(data);
    console.log(`[↓] Received: ${msg.type || 'UNKNOWN'} →`, msg);
  } catch (err) {
    console.warn('[!] Non-JSON message:', data.toString());
  }
});

ws.on('error', (err) => {
  console.error('[✖] Connection error:', err.message);
});

ws.on('close', () => {
  console.log('[×] Connection closed by server');
});
