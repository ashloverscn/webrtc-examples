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

# Skipping NodeSource GPG key and repo setup
echo "=== Skipping NodeSource setup. Using default APT repository ==="

echo "=== Installing Node.js and npm from default repository ==="
sudo apt install -y nodejs npm

echo "=== Verifying installation ==="
node -v
npm -v

echo "✅ Node.js reinstallation complete using default APT source!"
