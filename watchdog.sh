#!/usr/bin/env bash
# ==============================================================================
# Label Bench - Docker Compose Watchdog & Auto-Healer
# ==============================================================================
# Monitors Label Bench and automatically runs 'docker compose up -d' if the
# service becomes unreachable or stops running.
#
# Usage:
#   ./watchdog.sh                     # Smart mode: checks health every 10s, runs compose up if down
#   ./watchdog.sh --periodic 30       # Periodic mode: runs compose up every 30 seconds automatically
#   ./watchdog.sh --periodic 15 -b    # Periodic mode with --build every 15 seconds
#   ./watchdog.sh --once              # Run once and exit
#   ./watchdog.sh --help              # Show help
# ==============================================================================

set -u

# --- Configuration & Defaults ---
PORT="${PORT:-8013}"
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"
INTERVAL="${INTERVAL:-10}"
PERIODIC=0
BUILD_FLAG=""
RUN_ONCE=0

# Detect Docker Compose command (support modern 'docker compose' and legacy 'docker-compose')
if docker compose version >/dev/null 2>&1; then
    COMPOSE_BIN="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_BIN="docker-compose"
else
    COMPOSE_BIN="docker compose"
fi

# --- Argument Parsing ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--periodic)
            PERIODIC=1
            if [[ $# -gt 1 && "$2" =~ ^[0-9]+$ ]]; then
                INTERVAL="$2"
                shift
            fi
            shift
            ;;
        -i|--interval)
            if [[ $# -gt 1 && "$2" =~ ^[0-9]+$ ]]; then
                INTERVAL="$2"
                shift
            else
                echo "[!] Error: --interval requires a numeric argument (seconds)." >&2
                exit 1
            fi
            shift
            ;;
        -b|--build)
            BUILD_FLAG="--build"
            shift
            ;;
        --once)
            RUN_ONCE=1
            shift
            ;;
        -h|--help)
            echo "Label Bench Watchdog & Auto-Healer"
            echo ""
            echo "Usage: ./watchdog.sh [options]"
            echo ""
            echo "Options:"
            echo "  -p, --periodic [SEC]   Run '$COMPOSE_BIN up -d' automatically every SEC seconds (default: 10)"
            echo "  -i, --interval SEC     Polling interval in seconds (default: 10)"
            echo "  -b, --build            Include --build flag when running compose up"
            echo "  --once                 Check status / run compose up once, then exit"
            echo "  -h, --help             Show this help message"
            echo ""
            echo "Examples:"
            echo "  ./watchdog.sh                  # Smart auto-healing (restarts only when down)"
            echo "  ./watchdog.sh --periodic 20    # Automatic compose up every 20 seconds"
            echo "  ./watchdog.sh -p 15 -b         # Automatic compose up --build every 15 seconds"
            exit 0
            ;;
        *)
            echo "[!] Unknown option: $1 (Use --help for usage)" >&2
            exit 1
            ;;
    esac
done

timestamp() {
    date "+%Y-%m-%d %H:%M:%S"
}

run_compose_up() {
    echo "[$(timestamp)] [watchdog] Executing: $COMPOSE_BIN up -d $BUILD_FLAG ..."
    if $COMPOSE_BIN up -d $BUILD_FLAG; then
        echo "[$(timestamp)] [watchdog] $COMPOSE_BIN up -d succeeded."
    else
        echo "[$(timestamp)] [watchdog] Warning: $COMPOSE_BIN up -d returned non-zero exit code." >&2
    fi
}

check_health() {
    # Test HTTP endpoint with a 3-second timeout
    if command -v curl >/dev/null 2>&1; then
        curl -s -f -m 3 "$HEALTH_URL" >/dev/null 2>&1
        return $?
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O /dev/null -T 3 "$HEALTH_URL" >/dev/null 2>&1
        return $?
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c "import urllib.request; urllib.request.urlopen('$HEALTH_URL', timeout=3)" >/dev/null 2>&1
        return $?
    else
        # Fallback: check if docker container is running
        $COMPOSE_BIN ps -q label-bench >/dev/null 2>&1
        return $?
    fi
}

# --- Banner ---
echo "================================================================="
echo " Label Bench Watchdog & Auto-Healer"
echo "================================================================="
echo " Mode:     $([ "$PERIODIC" -eq 1 ] && echo "Periodic compose up (every ${INTERVAL}s)" || echo "Smart healthcheck (checks every ${INTERVAL}s)")"
echo " Endpoint: $HEALTH_URL"
echo " Command:  $COMPOSE_BIN up -d $BUILD_FLAG"
echo "================================================================="
echo "Press Ctrl+C to stop."
echo ""

# Handle graceful shutdown
trap 'echo ""; echo "[$(timestamp)] [watchdog] Stopping watchdog."; exit 0' SIGINT SIGTERM

# Initial compose up on start to ensure baseline running state
if [ "$RUN_ONCE" -eq 1 ]; then
    run_compose_up
    exit 0
fi

# Ensure initial container is up
if ! check_health; then
    echo "[$(timestamp)] [watchdog] Initial check failed. Starting container..."
    run_compose_up
    sleep 3
fi

consecutive_failures=0

while true; do
    if [ "$PERIODIC" -eq 1 ]; then
        # Periodic mode: run compose up on every tick
        run_compose_up
    else
        # Smart healthcheck mode: only run compose up when unhealthy
        if check_health; then
            if [ "$consecutive_failures" -gt 0 ]; then
                echo "[$(timestamp)] [watchdog] Service recovered and is now healthy."
            fi
            consecutive_failures=0
        else
            consecutive_failures=$((consecutive_failures + 1))
            echo "[$(timestamp)] [watchdog] Health check failed ($consecutive_failures/2) on $HEALTH_URL"
            if [ "$consecutive_failures" -ge 2 ]; then
                echo "[$(timestamp)] [watchdog] Service unresponsive. Re-triggering $COMPOSE_BIN up -d..."
                run_compose_up
                consecutive_failures=0
                # Give the container a moment to initialize before next check
                sleep 5
            fi
        fi
    fi

    sleep "$INTERVAL"
done
