#!/bin/bash

set -e

echo "=== Removing existing Node.js and npm ==="
sudo apt remove --purge -y nodejs npm
sudo rm -rf /etc/apt/sources.list.d/nodesource.list
sudo rm -rf /etc/apt/keyrings/nodesource.gpg
sudo apt autoremove -y
sudo apt clean

echo "=== Cleaning old binaries if any ==="
sudo rm -f /usr/local/bin/node
sudo rm -f /usr/local/bin/npm
sudo rm -rf /usr/local/lib/node_modules
sudo rm -rf ~/.npm
sudo rm -rf ~/.node-gyp

echo "=== Updating package index ==="
sudo apt update

echo "=== Installing required tools ==="
sudo apt install -y curl ca-certificates gnupg

echo "=== Adding NodeSource GPG key ==="
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | \
  sudo gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg

echo "=== Creating NodeSource repo file for Node.js 20.x LTS ==="
NODE_MAJOR=20
echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_$NODE_MAJOR.x nodistro main" | \
  sudo tee /etc/apt/sources.list.d/nodesource.list

echo "=== Updating package index again ==="
sudo apt update

echo "=== Installing Node.js and npm ==="
sudo apt install -y nodejs

echo "=== Verifying installation ==="
node -v
npm -v

echo "✅ Node.js reinstallation complete!"
