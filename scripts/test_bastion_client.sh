#!/usr/bin/env bash
# ── BASTION Client Test Script ──────────────────────────────────────
# Tests Ollama access patterns: direct (DANGEROUS) vs through BASTION (SAFE).
# Run with BASTION stopped first to see failure, then start it.
#
# Usage:
#   sudo systemctl stop bastion
#   bash scripts/test_bastion_client.sh
#   sudo systemctl start bastion
#   bash scripts/test_bastion_client.sh

set -euo pipefail

BASTION_URL="http://localhost:11434"   # SAFE — through BASTION proxy
OLLAMA_URL="http://localhost:11435"    # DANGEROUS — direct to Ollama (no use_mmap injection)

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "==================================================================="
echo " BASTION Client Access Test"
echo "==================================================================="
echo ""

# ── Test 1: Check BASTION status ─────────────────────────────────
echo -e "${YELLOW}[Test 1] Check if BASTION is running${NC}"
BASTION_UP=false
if curl -sf "$BASTION_URL/" > /dev/null 2>&1; then
    echo -e "  ${GREEN}[OK] BASTION is UP on :11434${NC}"
    BASTION_UP=true
else
    echo -e "  ${RED}[FAIL] BASTION is DOWN on :11434${NC}"
fi
echo ""

# ── Test 2: Try direct Ollama access (should be BLOCKED by iptables) ──
echo -e "${YELLOW}[Test 2] Try DIRECT Ollama access on :11435 (should be blocked)${NC}"
if curl -sf --connect-timeout 3 "$OLLAMA_URL/" > /dev/null 2>&1; then
    echo -e "  ${RED}[FAIL] WARNING: Direct Ollama access ALLOWED -- iptables rule missing!${NC}"
    echo -e "  ${RED}        Requests bypass use_mmap:false injection = CRASH RISK${NC}"
else
    echo -e "  ${GREEN}[OK] Direct Ollama access BLOCKED (iptables working)${NC}"
fi
echo ""

# ── Test 3: List models through BASTION ──────────────────────────
echo -e "${YELLOW}[Test 3] List models via BASTION (/api/tags)${NC}"
TAGS_RESP=$(curl -sf "$BASTION_URL/api/tags" 2>&1) && {
    MODEL_COUNT=$(echo "$TAGS_RESP" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('models',[])))" 2>/dev/null || echo "?")
    echo -e "  ${GREEN}[OK] Got $MODEL_COUNT models from /api/tags${NC}"
} || {
    echo -e "  ${RED}[FAIL] /api/tags unavailable -- BASTION not responding${NC}"
}
echo ""

# ── Test 4: BASTION health check ─────────────────────────────────
echo -e "${YELLOW}[Test 4] BASTION health endpoint (/broker/health)${NC}"
HEALTH_RESP=$(curl -sf "$BASTION_URL/broker/health" 2>&1) && {
    echo -e "  ${GREEN}[OK] Health response:${NC}"
    echo "$HEALTH_RESP" | python3 -m json.tool 2>/dev/null | head -10 | sed 's/^/    /'
} || {
    echo -e "  ${RED}[FAIL] Health endpoint unavailable (BASTION down?)${NC}"
}
echo ""

# ── Test 5: BASTION broker status ────────────────────────────────
echo -e "${YELLOW}[Test 5] BASTION broker status (/broker/status)${NC}"
STATUS_RESP=$(curl -sf "$BASTION_URL/broker/status" 2>&1) && {
    echo -e "  ${GREEN}[OK] Broker status:${NC}"
    echo "$STATUS_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f\"    State:      {d.get('state','?')}\")
print(f\"    Uptime:     {d.get('uptime_seconds',0):.0f}s\")
print(f\"    Queue:      {d.get('total_queue_depth',0)} pending\")
gpu = d.get('gpu', {})
print(f\"    GPU temp:   {gpu.get('temperature_c','?')}C\")
print(f\"    VRAM used:  {gpu.get('vram_used_mb',0):.0f} MB\")
loaded = d.get('loaded_models', [])
names = ', '.join(m.get('name','?') for m in loaded) if loaded else '(none)'
print(f\"    Models:     {names}\")
" 2>/dev/null
} || {
    echo -e "  ${RED}[FAIL] Broker status unavailable${NC}"
}
echo ""

# ── Test 6: Generate through BASTION (the SAFE way) ─────────────
echo -e "${YELLOW}[Test 6] Generate request via BASTION (safe, with use_mmap:false)${NC}"
if [ "$BASTION_UP" = true ]; then
    GEN_RESP=$(curl -sf "$BASTION_URL/api/generate" \
        -d '{"model":"qwen3:8b","prompt":"Say hello in exactly 5 words.","stream":false}' \
        --max-time 120 2>&1) && {
        echo "$GEN_RESP" | python3 -c "
import sys, json
resp = json.load(sys.stdin)
print(f\"  Response: {resp.get('response','(empty)')[:200]}\")
print(f\"  Duration: {resp.get('total_duration',0)/1e9:.2f}s\")
" 2>/dev/null
        echo -e "  ${GREEN}[OK] Generation completed safely through BASTION${NC}"
    } || {
        echo -e "  ${RED}[FAIL] Generation failed${NC}"
    }
else
    echo -e "  ${RED}SKIPPED -- BASTION is down. Start it with: sudo systemctl start bastion${NC}"
fi
echo ""

# ── Test 7: Generate with priority header ────────────────────────
echo -e "${YELLOW}[Test 7] Generate with explicit priority tier (interactive)${NC}"
if [ "$BASTION_UP" = true ]; then
    PRI_RESP=$(curl -sf "$BASTION_URL/api/generate" \
        -H "X-Broker-Priority: interactive" \
        -d '{"model":"qwen3:8b","prompt":"What is 2+2? Answer with just the number.","stream":false}' \
        --max-time 120 2>&1) && {
        echo "$PRI_RESP" | python3 -c "
import sys, json
resp = json.load(sys.stdin)
print(f\"  Response: {resp.get('response','(empty)')[:100]}\")
" 2>/dev/null
        echo -e "  ${GREEN}[OK] Interactive-priority request completed${NC}"
    } || {
        echo -e "  ${RED}[FAIL] Request failed${NC}"
    }
else
    echo -e "  ${RED}SKIPPED -- BASTION is down${NC}"
fi
echo ""

# ── Summary ──────────────────────────────────────────────────────
echo "==================================================================="
if [ "$BASTION_UP" = true ]; then
    echo -e " ${GREEN}BASTION is running. All requests go through :11434 safely.${NC}"
else
    echo -e " ${RED}BASTION is DOWN. Ollama is unprotected!${NC}"
    echo -e " ${RED}Start with: sudo systemctl start bastion${NC}"
fi
echo "==================================================================="
