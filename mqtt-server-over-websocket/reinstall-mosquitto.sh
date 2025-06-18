#!/bin/bash

set -e

echo "=== Removing existing Mosquitto and cleaning config..."

# Stop Mosquitto service if running
sudo systemctl stop mosquitto || true

# Remove existing Mosquitto installation
sudo apt purge -y mosquitto mosquitto-clients
sudo apt autoremove -y
sudo apt clean

# Remove old Mosquitto config and data
sudo rm -rf /etc/mosquitto
sudo rm -rf /var/lib/mosquitto
sudo rm -rf /var/log/mosquitto

echo "=== Installing Mosquitto with dependencies..."

# Update and reinstall Mosquitto
sudo apt update
sudo apt install -y mosquitto mosquitto-clients

echo "=== Rebuilding Mosquitto configuration..."

# Recreate config directories
sudo mkdir -p /etc/mosquitto/conf.d
sudo mkdir -p /var/lib/mosquitto
sudo mkdir -p /var/log/mosquitto

# Write main mosquitto.conf file
sudo tee /etc/mosquitto/mosquitto.conf > /dev/null <<EOF
# Place your local configuration in /etc/mosquitto/conf.d/
#
# A full description of the configuration file is at
# /usr/share/doc/mosquitto/examples/mosquitto.conf.example

pid_file /run/mosquitto/mosquitto.pid

persistence true
persistence_location /var/lib/mosquitto/

log_dest file /var/log/mosquitto/mosquitto.log

include_dir /etc/mosquitto/conf.d
EOF

# Clean up conflicting conf files
sudo rm -f /etc/mosquitto/conf.d/*.conf

# Write new listener config
sudo tee /etc/mosquitto/conf.d/wss.conf > /dev/null <<EOF
listener 1883
protocol mqtt

listener 9001
protocol websockets
allow_anonymous true
password_file /etc/mosquitto/passwd
EOF

echo "=== Creating password file for user 'admin'..."

# Create Mosquitto password file
sudo mosquitto_passwd -b -c /etc/mosquitto/passwd admin admin1234S

# Set correct ownership and permissions
sudo chown mosquitto: /etc/mosquitto/passwd
sudo chmod 600 /etc/mosquitto/passwd

echo "=== Enabling and restarting Mosquitto..."

# Enable and start the service
sudo systemctl enable mosquitto
sudo systemctl restart mosquitto

echo "=== ✅ Mosquitto installed and configured with WebSocket on port 9001 ==="
echo "=== 🔐 Login: admin / admin1234S ==="
