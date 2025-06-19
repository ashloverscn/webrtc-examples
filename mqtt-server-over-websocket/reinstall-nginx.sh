#!/bin/bash

set -e

echo "=== Removing existing NGINX and modules..."

sudo systemctl stop nginx || true

# Remove NGINX and associated modules
sudo apt purge -y nginx nginx-common nginx-full \
  libnginx-mod-stream-geoip2 libnginx-mod-http-geoip2 \
  libnginx-mod-http-auth-pam libnginx-mod-http-dav-ext \
  libnginx-mod-http-echo libnginx-mod-http-subs-filter \
  libnginx-mod-http-upstream-fair libnginx-mod-stream \
  libmaxminddb0

# Clean leftovers
sudo apt autoremove -y
sudo apt clean

echo "=== Removing NGINX /etc/nginx configuration..."
sudo rm -rf /etc/nginx

echo "=== Installing NGINX and all required modules..."

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
# HTTP to HTTPS redirect
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name localhost;

    return 301 https://\$host\$request_uri;
}

# HTTPS server block using cert.pem and key.pem
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
        index index.html index.htm;
    }

    # WebSocket proxy at /ws/
    location /ws/ {
        proxy_pass http://127.0.0.1:8080/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "Upgrade";
        proxy_set_header Host \$host;
        proxy_cache_bypass \$http_upgrade;
    }

    # MQTT over WebSocket proxy at /mqtt/
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
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_cache_bypass \$http_upgrade;
    }

    location /health {
        proxy_pass http://localhost:3000/health;
    }

}
EOF

sudo ln -sf /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default || true

echo "=== Restarting NGINX..."
sudo nginx -t && sudo systemctl restart nginx

echo "=== NGINX reinstallation complete! Visit https://<your-ip>/ ==="
