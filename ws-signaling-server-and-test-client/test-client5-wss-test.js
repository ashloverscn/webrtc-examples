const WebSocket = require('ws');

const serverUrl = 'wss://192.168.29.9/ws/';
const delay = ms => new Promise(res => setTimeout(res, ms));

function createClient(name) {
  const ws = new WebSocket(serverUrl, {
    rejectUnauthorized: false // For self-signed certs only
  });

  ws.name = name;

  ws.on('open', async () => {
    console.log(`[${name}] Connected`);

    if (name === 'ClientA') {
      await delay(1000);
      const offer = {
        type: 'offer',
        from: 'ClientA',
        to: 'ClientB',
        sdp: 'v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n...dummy-offer...'
      };
      console.log(`[${name}] ➜ Sending OFFER`);
      ws.send(JSON.stringify(offer));
    }

    // Send ICE after delay
    await delay(3000);
    const candidate = {
      type: 'candidate',
      from: name,
      to: name === 'ClientA' ? 'ClientB' : 'ClientA',
      candidate: `candidate:1 1 UDP 2122252543 192.168.1.2 3478 typ host`
    };
    console.log(`[${name}] ➜ Sending ICE Candidate`);
    ws.send(JSON.stringify(candidate));

    // Send BYE after a while
    await delay(5000);
    const bye = {
      type: 'bye',
      from: name,
      to: name === 'ClientA' ? 'ClientB' : 'ClientA'
    };
    console.log(`[${name}] ➜ Sending BYE`);
    ws.send(JSON.stringify(bye));
  });

  ws.on('message', (msg) => {
    const data = JSON.parse(msg);
    if (data.to !== name) return;

    if (data.type === 'offer') {
      console.log(`[${name}] ⬅ Received OFFER`);
      const answer = {
        type: 'answer',
        from: name,
        to: data.from,
        sdp: 'v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n...dummy-answer...'
      };
      console.log(`[${name}] ➜ Sending ANSWER`);
      ws.send(JSON.stringify(answer));
    } else if (data.type === 'answer') {
      console.log(`[${name}] ⬅ Received ANSWER`);
    } else if (data.type === 'candidate') {
      console.log(`[${name}] ⬅ Received ICE Candidate`);
    } else if (data.type === 'bye') {
      console.log(`[${name}] ⬅ Received BYE`);
    } else {
      console.log(`[${name}] ⬅ Unknown message:`, data);
    }
  });

  ws.on('close', () => console.log(`[${name}] Connection closed`));
  ws.on('error', err => console.error(`[${name}] Error:`, err.message));
}

// Launch both clients
createClient('ClientA');
setTimeout(() => createClient('ClientB'), 500); // slight delay to simulate joining
