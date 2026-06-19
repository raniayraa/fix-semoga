#!/bin/bash

# Cache sudo credentials upfront so background processes don't prompt
sudo -v

# Kill processes on backend, frontend, xdpd-turbo, and fwd ports
for PORT in 8765 5173 9898 8081; do
    PID=$(lsof -ti tcp:$PORT)
    if [ -n "$PID" ]; then
        echo "Killing process on port $PORT (PID: $PID)"
        kill -9 $PID
    else
        echo "No process on port $PORT"
    fi
done

sleep 1

# Reset xdpd traffic log DB
sudo rm -f /tmp/xdpd.db
echo "DB reset."

# Start backend
echo "Starting backend..."
cd ~/final_t40/dashboard/backend
nohup uvicorn main:app --host 0.0.0.0 --port 8765 --reload > /tmp/backend.log 2>&1 &
echo "Backend PID: $!"

# Start frontend
echo "Starting frontend..."
cd ~/final_t40/dashboard/frontend
nohup npm run dev -- --host > /tmp/frontend.log 2>&1 &
echo "Frontend PID: $!"

# Start xdpd via start_turbo.sh (handles system tuning + launches xdpd on :9898)
echo "Starting xdpd (turbo mode)..."
cd ~/final_t40/xdp-go-optimized
nohup sudo bash start_turbo.sh > /tmp/xdpd.log 2>&1 &
echo "xdpd turbo PID: $!"

# Start fwd
echo "Starting fwd..."
cd ~/final_t40/linux-fw-dashboard
nohup sudo ./fwd -addr :8081 -static ./frontend/dist -config ./config.json > /tmp/fwd.log 2>&1 &
echo "fwd PID: $!"

echo ""
echo "All services started. Following logs (Ctrl+C to stop)..."
echo ""
tail -f /tmp/backend.log /tmp/frontend.log /tmp/xdpd.log /tmp/fwd.log
