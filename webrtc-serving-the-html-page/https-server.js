const https = require('https');
const fs = require('fs');
const express = require('express');
const app = express();

const options = {
  key: fs.readFileSync('key.pem'),
  cert: fs.readFileSync('cert.pem')
};

app.use(express.static('.')); // Serve files from current directory

https.createServer(options, app).listen(8443, () => {
  console.log('HTTPS server running at https://<your-ip>:8443');
});
