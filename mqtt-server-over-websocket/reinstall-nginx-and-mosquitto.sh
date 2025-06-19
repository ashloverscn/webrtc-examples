#!/bin/bash

set -e

echo "=== [1/6] Removing existing NGINX and modules..."

sudo systemctl stop nginx || true

sudo apt purge -y nginx nginx-common nginx-full \
  libnginx-mod-stream-geoip2 libnginx-mod-http-geoip2 \
  libnginx-mod-http-auth-pam libnginx-mod-http-dav-ext \
  libnginx-mod-http-echo libnginx-mod-http-subs-filter \
  libnginx-mod-http-upstream-fair libnginx-mod-stream \
  libmaxminddb0

echo "=== [2/6] Removing NGINX /etc/nginx configuration..."
sudo rm -rf /etc/nginx

sudo apt autoremove -y
sudo apt clean

echo "=== [2/6] Installing NGINX and required modules..."

sudo apt update
sudo apt install -y nginx-full \
  libnginx-mod-stream-geoip2 libnginx-mod-http-geoip2 \
  libnginx-mod-http-auth-pam libnginx-mod-http-dav-ext \
  libnginx-mod-http-echo libnginx-mod-http-subs-filter \
  libnginx-mod-http-upstream-fair libnginx-mod-stream \
  openssl

echo "=== Setting up SSL certificates..."

sudo mkdir -p /etc/nginx/ssl
sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
 -keyout /etc/nginx/ssl/key.pem \
 -out /etc/nginx/ssl/cert.pem \
 -subj "/C=IN/ST=State/L=City/O=Org/CN=localhost"

echo "=== Applying NGINX configuration..."

sudo tee /etc/nginx/sites-available/default >/dev/null <<EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name localhost;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2 default_server;
    listen [::]:443 ssl http2 default_server;

    server_name localhost;

    root /var/www/html;
    index index.html index.htm;

    ssl_certificate     /etc/nginx/ssl/cert.pem;
    ssl_certificate_key /etc/nginx/ssl/key.pem;

    location / {
        try_files \$uri \$uri/ =404;
        autoindex on;
        autoindex_exact_size off;
        autoindex_localtime on;
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8080/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "Upgrade";
        proxy_set_header Host \$host;
        proxy_cache_bypass \$http_upgrade;
    }

    location /mqtt/ {
        proxy_pass http://127.0.0.1:9001/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_cache_bypass \$http_upgrade;
    }

    # Socket.IO WebSocket proxy
    location /socket.io/ {
        proxy_pass http://localhost:3000/socket.io/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }

    location /health {
        proxy_pass http://localhost:3000/health;
    }    
}
EOF

echo "=== Restarting NGINX..."
sudo nginx -t && sudo systemctl restart nginx

echo "=== ✅ NGINX reinstallation complete! Visit https://<your-ip>/ ==="

echo "=== [3/6] Removing existing Mosquitto and cleaning config..."

sudo systemctl stop mosquitto || true

sudo apt purge -y mosquitto mosquitto-clients
sudo apt autoremove -y
sudo apt clean

sudo rm -rf /etc/mosquitto /var/lib/mosquitto /var/log/mosquitto

echo "=== [4/6] Installing Mosquitto and clients..."

sudo apt update
sudo apt install -y mosquitto mosquitto-clients

echo "=== [5/6] Rebuilding Mosquitto configuration..."

sudo mkdir -p /etc/mosquitto/conf.d /var/lib/mosquitto /var/log/mosquitto

sudo tee /etc/mosquitto/mosquitto.conf > /dev/null <<EOF
pid_file /run/mosquitto/mosquitto.pid
persistence true
persistence_location /var/lib/mosquitto/
log_dest file /var/log/mosquitto/mosquitto.log
include_dir /etc/mosquitto/conf.d
EOF

sudo rm -f /etc/mosquitto/conf.d/*.conf

sudo tee /etc/mosquitto/conf.d/wss.conf > /dev/null <<EOF
listener 1883
protocol mqtt

listener 9001
protocol websockets
allow_anonymous true
password_file /etc/mosquitto/passwd
EOF

echo "=== [6/6] Creating password file and starting Mosquitto..."

sudo mosquitto_passwd -b -c /etc/mosquitto/passwd admin admin1234S
sudo chown mosquitto: /etc/mosquitto/passwd
sudo chmod 600 /etc/mosquitto/passwd

sudo systemctl enable mosquitto
sudo systemctl restart mosquitto

echo "=== ✅ Mosquitto installed and configured with WebSocket on port 9001 ==="
echo "=== 🔐 Login: admin / admin1234S ==="
