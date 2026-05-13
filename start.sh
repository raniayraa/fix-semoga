#!/bin/bash

# Cache sudo credentials upfront so background xdpd doesn't prompt
sudo -v

# Kill processes on backend and frontend ports
for PORT in 8765 5173; do
    PID=$(lsof -ti tcp:$PORT)
    if [ -n "$PID" ]; then
        echo "Killing process on port $PORT (PID: $PID)"
        kill -9 $PID
    else
        echo "No process on port $PORT"
    fi
done

sleep 1

# Start backend in background
echo "Starting backend..."
cd ~/final_t40/dashboard/backend
nohup uvicorn main:app --host 0.0.0.0 --port 8765 --reload > /tmp/backend.log 2>&1 &
echo "Backend PID: $!"

# Start frontend in background
echo "Starting frontend..."
cd ~/final_t40/dashboard/frontend
npm install
nohup npm run dev -- --host > /tmp/frontend.log 2>&1 &
echo "Frontend PID: $!"

# Start xdpd in background
echo "Starting xdpd..."
cd ~/final_t40/xdp-go-optimized
nohup sudo ./xdpd -iface enp1s0f1np1 -redirect-dev enp1s0f0np0 -addr :8080 -static ./frontend/dist -db /tmp/xdpd.db > /tmp/xdpd.log 2>&1 &
echo "xdpd PID: $!"

# Start fwd in background
echo "Starting fwd..."
cd ~/final_t40/linux-fw-dashboard
nohup sudo ./fwd -addr :8081 -static ./frontend/dist -config ./config.json > /tmp/fwd.log 2>&1 &
echo "fwd PID: $!"

echo ""
echo "All services started. Following logs (Ctrl+C to stop)..."
echo ""
tail -f /tmp/backend.log /tmp/frontend.log /tmp/xdpd.log /tmp/fwd.log
