#!/bin/bash
# ClaudIO v0.1.0 - Quick Start Script

echo "╔══════════════════════════════════════════════════════╗"
echo "║          ClaudIO v0.1.0 - Quick Start                ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# Check UV installed
if ! command -v uv &> /dev/null; then
    echo "❌ UV not found. Installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo "✅ UV installed"
    echo ""
fi

# Check LM Studio
echo "Checking LM Studio..."
if curl -s http://100.127.255.172:1234/v1/models > /dev/null 2>&1; then
    echo "✅ LM Studio is running"
else
    echo "⚠️  LM Studio not detected at http://100.127.255.172:1234"
    echo ""
    echo "Please start LM Studio and load a model (e.g., gpt-oss-20b)"
    echo ""
fi

echo ""
echo "Starting ClaudIO CLI..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Run with arguments
uv run src/claudio/ui/cli.py "$@"
