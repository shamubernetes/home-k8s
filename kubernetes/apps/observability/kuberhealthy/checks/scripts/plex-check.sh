#!/bin/sh
set -e

# Verify KH_REPORTING_URL is set (injected by Kuberhealthy)
if [ -z "$${KH_REPORTING_URL}" ]; then
  echo "ERROR: KH_REPORTING_URL is not set"
  exit 1
fi

# Check if we're in the maintenance window (5:00-7:00 AM ET)
CURRENT_HOUR=$(date +%H)
if [ "$${CURRENT_HOUR}" = "05" ] || [ "$${CURRENT_HOUR}" = "06" ]; then
  echo "OK: Maintenance window (05:00-07:00 AM ET) - allowing upgrades"
  curl -sf -X POST -H "Content-Type: application/json" \
    -d '{"Errors":[],"OK":true}' \
    "$${KH_REPORTING_URL}"
  exit 0
fi

# Query Tautulli for active streams
RESPONSE=$(curl -sf "http://tautulli.observability.svc.cluster.local/api/v2?apikey=$${TAUTULLI_API_KEY}&cmd=get_activity" || echo "")

# Extract stream count from response (handles "stream_count": "N" format)
STREAM_COUNT=$(echo "$${RESPONSE}" | grep -o '"stream_count": *"[0-9]*"' | grep -o '[0-9]*' || echo "0")

if [ "$${STREAM_COUNT}" = "0" ] || [ -z "$${STREAM_COUNT}" ]; then
  echo "OK: No active Plex streams (count: $${STREAM_COUNT:-0})"
  curl -sf -X POST -H "Content-Type: application/json" \
    -d '{"Errors":[],"OK":true}' \
    "$${KH_REPORTING_URL}"
else
  echo "FAIL: $${STREAM_COUNT} active Plex stream(s) detected - blocking upgrades"
  curl -sf -X POST -H "Content-Type: application/json" \
    -d "{\"Errors\":[\"$${STREAM_COUNT} active Plex stream(s) detected\"],\"OK\":false}" \
    "$${KH_REPORTING_URL}"
fi
