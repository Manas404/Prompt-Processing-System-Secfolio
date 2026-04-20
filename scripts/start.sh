#!/usr/bin/env bash
set -e

echo "🚀 Starting Prompt Processing System..."

# Copy env if not present
if [ ! -f .env ]; then
  cp .env.example .env
  echo "⚠️  Created .env from .env.example – add your API keys before running."
fi

# Start the full stack
docker-compose up -d --build

echo ""
echo "✅ Stack is up!"
echo "   API:    http://localhost:8000"
echo "   Docs:   http://localhost:8000/docs"
echo "   Flower: http://localhost:5555"
echo ""
echo "Submit a test prompt:"
echo "  curl -X POST http://localhost:8000/api/v1/prompts \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"prompt\": \"Explain token buckets in 2 sentences\"}'"
