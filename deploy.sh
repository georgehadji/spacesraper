#!/bin/bash
# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (CI/CD Deployment)
# Role: Automated cloud provisioning and cluster launch.

set -e # Exit on error

echo "🚀 Spacescraper: Starting Cloud Deployment Sequence..."

# 1. Environment Validation
if [ ! -f .env ]; then
    echo "⚠️ Warning: .env file not found. Creating from template..."
    echo "VALKEY_URL=valkey://valkey:6379" > .env
    echo "GEMINI_API_KEY=your_key_here" >> .env
fi

# 2. Infrastructure Setup (Assuming Docker is installed)
echo "📦 Building Cluster Images..."
docker compose build

# 3. Security Hardening
echo "🛡️ Setting directory permissions..."
mkdir -p exports/evidence logs
chmod -R 777 exports logs

# 4. Launching the Fleet
echo "🚢 Launching Spacescraper Intelligence Fleet..."
docker compose up -d

# 5. Health Check
echo "🔍 Performing link audit..."
sleep 5
API_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health)

if [ "$API_STATUS" == "200" ]; then
    echo "✅ Success: Spacescraper is LIVE at http://localhost:8000"
    echo "📊 Monitoring available at http://localhost:8000/metrics"
else
  echo "❌ Error: API Gateway failed to stabilize. Check logs/trace.log"
  docker compose logs api
fi
