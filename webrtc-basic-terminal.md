✅ Goal
Establish a peer-to-peer WebRTC DataChannel between two browsers, and send text (like serial terminal data) back and forth.

📦 Requirements
No backend (you’ll manually copy/paste signaling data for now)

Pure HTML + JS

Modern browser (Chrome, Firefox, Edge)

📄 index.html (Put this on both sides)
html
Copy
Edit
<!DOCTYPE html>
<html>
<head>
  <title>WebRTC Serial Terminal</title>
  <style>
    textarea { width: 100%; height: 100px; }
    pre { background: #111; color: #0f0; padding: 10px; height: 200px; overflow-y: scroll; }
  </style>
</head>
<body>
  <h2>WebRTC Serial Terminal</h2>

  <textarea id="localDesc" placeholder="Local Description (paste to remote)"></textarea><br>
  <button onclick="createOffer()">Create Offer</button>
  <button onclick="createAnswer()">Create Answer</button><br><br>

  <textarea id="remoteDesc" placeholder="Paste Remote Description Here"></textarea><br>
  <button onclick="setRemote()">Set Remote Description</button>

  <h3>Terminal Output</h3>
  <pre id="terminalOutput"></pre>

  <input type="text" id="inputField" placeholder="Type a command..." onkeydown="checkEnter(event)">
  <button onclick="sendMessage()">Send</button>

  <script>
    let pc = new RTCPeerConnection();
    let dc;
    const terminal = document.getElementById('terminalOutput');

    pc.ondatachannel = event => {
      dc = event.channel;
      setupDataChannel();
    };

    function createOffer() {
      dc = pc.createDataChannel("serial");
      setupDataChannel();
      pc.createOffer().then(offer => {
        pc.setLocalDescription(offer);
        document.getElementById('localDesc').value = JSON.stringify(offer);
      });
    }

    function createAnswer() {
      pc.createAnswer().then(answer => {
        pc.setLocalDescription(answer);
        document.getElementById('localDesc').value = JSON.stringify(answer);
      });
    }

    function setRemote() {
      const remoteDesc = JSON.parse(document.getElementById('remoteDesc').value);
      pc.setRemoteDescription(new RTCSessionDescription(remoteDesc));
    }

    function setupDataChannel() {
      dc.onmessage = event => {
        terminal.innerText += "> " + event.data + "\n";
        terminal.scrollTop = terminal.scrollHeight;
      };
      dc.onopen = () => {
        terminal.innerText += "=== DataChannel Opened ===\n";
      };
      dc.onclose = () => {
        terminal.innerText += "=== DataChannel Closed ===\n";
      };
    }

    function sendMessage() {
      const input = document.getElementById('inputField');
      if (dc && dc.readyState === "open") {
        dc.send(input.value);
        terminal.innerText += "< " + input.value + "\n";
        input.value = "";
        terminal.scrollTop = terminal.scrollHeight;
      }
    }

    function checkEnter(e) {
      if (e.key === "Enter") sendMessage();
    }
  </script>
</body>
</html>
🔁 Usage
Open the page in two browsers/tabs/devices.

On Caller:

Click Create Offer, then copy the local description and paste it into Callee’s "Remote Description".

On Callee:

Paste offer, click Set Remote Description.

Click Create Answer, copy the answer and paste it into Caller’s "Remote Description".

On Caller:

Paste answer and click Set Remote Description.

Now type in the input and send text between them – like a basic serial terminal.

🔧 Next Steps
Once this works:

Add automatic signaling via WebSocket or WebRTC signaling server.

Add timestamps, input history, or binary data support (for real serial-like behavior).

Bridge WebRTC data to a real serial port using Node.js + serialport library.

Would you like help with the next step—e.g., turning this into a real serial-to-browser bridge or automating the signaling?








