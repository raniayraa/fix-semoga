# Final T40 — XDP Firewall & Network Performance Testbed

Platform eksperimen jaringan berbasis XDP/eBPF yang menggabungkan **XDP firewall + fast forwarder**, **dashboard monitoring**, dan **otomasi eksperimen** menggunakan Ansible dan pktgen.

---

## Arsitektur Sistem

```
┌─────────────────────────────────────────────────────────────────┐
│                        Node 6 (DUT)                             │
│                                                                  │
│  ┌─────────────────┐    ┌──────────────────┐                   │
│  │  xdp-go-optimized│    │ linux-fw-dashboard│                   │
│  │  (XDP Firewall + │    │  (fwd — firewall  │                   │
│  │   Fast Forwarder)│    │   config UI)      │                   │
│  │  Port :9898      │    │  Port :8081       │                   │
│  └─────────────────┘    └──────────────────┘                   │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Dashboard (Ansible Controller + Experiment Manager)     │   │
│  │  Backend  (FastAPI/Python) — Port :8765                  │   │
│  │  Frontend (React/Vite)    — Port :5173                   │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
         │                              │
   Node 1, 4, 5                    Node 1, 4, 5
   (pktgen sender)              (pktgen receiver)
```

---

## Port yang Digunakan

| Port | Servis | Akses |
|------|--------|-------|
| **5173** | Dashboard Frontend (React/Vite dev server) | `http://localhost:5173` |
| **8765** | Dashboard Backend (FastAPI REST + WebSocket) | `http://localhost:8765` |
| **9898** | XDP Daemon (`xdpd`) — monitoring & firewall config XDP | `http://localhost:9898` |
| **8081** | `fwd` — Linux Firewall Dashboard (config UI alternatif) | `http://localhost:8081` |

> **Catatan:** Port 5173 (frontend) secara otomatis mem-proxy request `/api/*` dan `/ws/*` ke port 8765 (backend), sehingga user hanya perlu membuka `http://localhost:5173`.

---

## Deployment

### Prasyarat

- Linux kernel >= 5.10 (XDP support)
- Python 3.10+ dengan `pip`
- Node.js 18+ dan `npm`
- Go 1.23+
- `clang`, `llvm`, `libbpf-dev` (untuk kompilasi eBPF)
- `ethtool`, `lsof`
- Ansible (untuk otomasi eksperimen)

### Install Dependensi Backend

```bash
cd dashboard/backend
pip install -r requirements.txt
```

### Menjalankan Semua Servis

```bash
bash start2.sh
```

Script ini secara otomatis:
1. **Mematikan** proses lama di port 8765, 5173, 9898, dan 8081
2. **Mereset** database traffic log (`/tmp/xdpd.db`)
3. **Menjalankan** backend FastAPI di port 8765
4. **Menjalankan** frontend React dev server di port 5173
5. **Menjalankan** `xdpd` dalam turbo mode di port 9898
6. **Menjalankan** `fwd` (Linux FW Dashboard) di port 8081
7. **Menampilkan** live log dari semua servis

Setelah berjalan, buka browser ke:
- **http://localhost:5173** — Dashboard utama (Ansible + Experiment Manager)
- **http://localhost:9898** — XDP Firewall monitoring
- **http://localhost:8081** — Linux Firewall config UI

---

## Struktur Direktori

```
final_t40/
├── start2.sh                   # Entry point deployment
├── dashboard/
│   ├── backend/                # FastAPI server (Python)
│   │   ├── main.py             # Entrypoint, definisi semua API route
│   │   ├── runner.py           # Eksekusi Ansible playbook via subprocess
│   │   ├── metrics.py          # Parsing hasil CSV pktgen
│   │   ├── cpu_metrics.py      # Parsing CPU usage dari mpstat
│   │   ├── pktgen_config.py    # Baca/tulis konfigurasi pktgen JSON
│   │   ├── pkt_editor.py       # Edit file .pkt (pktgen script)
│   │   ├── node_registry.py    # Registry node (IP, role, dll.)
│   │   ├── ws_manager.py       # WebSocket connection manager
│   │   ├── models.py           # Pydantic model (request/response schema)
│   │   └── requirements.txt    # Dependensi Python
│   ├── frontend/               # React + Vite (TypeScript)
│   ├── pkt_files/              # Template script pktgen (.pkt)
│   └── pktgen_config.json      # Konfigurasi pktgen aktif
│
├── xdp-go-optimized/           # XDP Daemon (Go + eBPF)
│   ├── bpf/
│   │   └── xdp_prog_kern.c     # Program XDP kernel (eBPF C)
│   ├── internal/
│   │   ├── api/                # HTTP handler (stats, logs, firewall config)
│   │   ├── maps/               # Interface ke BPF maps
│   │   ├── db/                 # SQLite (traffic log)
│   │   ├── config/             # Parse turbo.json
│   │   └── xdp/                # Attach/detach program XDP ke NIC
│   ├── cmd/xdpd/               # main() — entrypoint xdpd
│   ├── turbo.json              # Konfigurasi firewall & sampling
│   ├── start_turbo.sh          # System tuning + launch xdpd
│   └── xdpd                    # Binary xdpd (pre-built)
│
├── linux-fw-dashboard/         # Firewall dashboard alternatif (Go)
│   ├── cmd/                    # main() — entrypoint fwd
│   ├── internal/               # Handler firewall rules
│   ├── frontend/dist/          # Static frontend (pre-built)
│   ├── config.json             # Konfigurasi firewall flags aktif
│   └── fwd                     # Binary fwd (pre-built)
│
├── ansible/                    # Playbook Ansible
│   ├── inventory.ini           # Daftar node (IP, user, SSH key)
│   ├── 01_basic_setup.yaml     # Setup dasar node
│   ├── 02_setup_route.yaml     # Konfigurasi routing
│   ├── 04_setup_xdp_node6.yaml # Deploy xdpd ke Node 6
│   ├── 04_setup_vpp_node6.yaml # Deploy VPP ke Node 6
│   ├── 04_setup_kernel_node6.yaml # Setup kernel forwarding
│   └── 05_start_pktgen.yaml    # Start traffic generator
│
├── results/                    # Hasil eksperimen
│   └── FINAL_PLOT/             # Plot SVG/PNG hasil analisis
│
└── experiment_runner.py        # Runner otomasi sweep eksperimen
```

---

## Dokumentasi Komponen

### 1. XDP Kernel Program (`xdp-go-optimized/bpf/xdp_prog_kern.c`)

Program eBPF yang diload ke kernel dan diattach ke NIC. Menjalankan dua fungsi utama:

**Firewall (L3 + L4):**
- Drop IP fragment, broadcast, multicast
- Block protokol tertentu (TCP/UDP/ICMP)
- Block port TCP/UDP tertentu
- Drop malformed TCP flags

**Fast Forwarder:**
- Lookup tabel forwarding (`fwd_table`) berdasarkan destination IP
- Rewrite MAC address (src + dst)
- Decrement TTL + update checksum (RFC 1624)
- Forward via `XDP_TX` atau `bpf_redirect_map()`

**Optimasi performa:**
- Event sampling: hanya 1 dari N paket yang dikirim ke userspace (kecuali DROP/TTL exceeded yang selalu dikirim)
- `PERCPU_ARRAY` untuk counter sampling (zero atomic contention)

### 2. XDP Daemon — `xdpd` (`xdp-go-optimized/`)

Userspace daemon (Go) yang:
- Load dan attach program eBPF ke NIC
- Expose HTTP API di `:9898` untuk monitoring dan kontrol firewall
- Simpan traffic log ke SQLite (`/tmp/xdpd.db`)
- Serve frontend monitoring dashboard (static files)

Konfigurasi di `turbo.json`:
```json
{
  "firewall_flags": {
    "block_icmp_ping": false,
    "block_ip_fragments": false,
    "block_malformed_tcp": false,
    "block_all_tcp": false,
    "block_all_udp": false,
    "block_broadcast": false,
    "block_multicast": false,
    "security_events_enabled": true
  },
  "blocked_ports": {
    "tcp": [1120]
  }
}
```

### 3. Dashboard Backend (`dashboard/backend/main.py`)

FastAPI server yang menjadi controller untuk:
- Menjalankan Ansible playbook (`runner.py`)
- Membaca hasil eksperimen CSV pktgen (`metrics.py`)
- Membaca CPU usage dari mpstat (`cpu_metrics.py`)
- Mengedit konfigurasi pktgen dan file `.pkt`
- Manajemen node registry
- WebSocket untuk streaming log real-time ke frontend

**API Endpoints utama:**
| Method | Path | Fungsi |
|--------|------|--------|
| GET | `/api/playbooks` | List semua Ansible playbook |
| POST | `/api/run` | Jalankan playbook |
| GET | `/api/jobs/{id}` | Status job |
| GET | `/api/metrics` | Hasil eksperimen |
| GET | `/api/nodes` | Registry node |
| WS | `/ws/logs` | Stream log real-time |

### 4. Linux Firewall Dashboard — `fwd` (`linux-fw-dashboard/`)

Alternatif UI berbasis Go untuk mengatur firewall flags via HTTP, tanpa XDP. Serve static frontend di port `:8081`.

### 5. Ansible Playbooks (`ansible/`)

Otomasi setup dan eksperimen multi-node:
- `00_check_node_connection.yaml` — Cek konektivitas semua node
- `01_basic_setup.yaml` — Install dependensi dasar
- `02_setup_route.yaml` — Konfigurasi routing antar node
- `04_setup_xdp_node6.yaml` — Deploy dan start xdpd di Node 6
- `04_setup_vpp_node6.yaml` — Deploy dan start VPP di Node 6
- `04_setup_kernel_node6.yaml` — Setup kernel IP forwarding di Node 6
- `05_start_pktgen.yaml` — Start pktgen di node sender

---

## Menjalankan Eksperimen

### Via Dashboard UI

1. Buka `http://localhost:5173`
2. Pilih playbook dari daftar
3. Klik **Run** — log akan muncul secara real-time

### Via Script Sweep

```bash
# Sweep kernel forwarding
bash run_kernel_sweep.sh

# Sweep VPP
bash run_vpp_sweep.sh

# Sweep XDP
bash run_xdp_sweep.sh
```

### Via Python Runner

```bash
python3 experiment_runner.py
```

---

## Log Files

Semua log servis ditulis ke `/tmp/`:

| File | Isi |
|------|-----|
| `/tmp/backend.log` | Log FastAPI backend |
| `/tmp/frontend.log` | Log Vite dev server |
| `/tmp/xdpd.log` | Log xdpd (XDP daemon + system tuning) |
| `/tmp/fwd.log` | Log fwd (Linux FW dashboard) |
| `/tmp/xdpd.db` | SQLite traffic log (di-reset setiap `start2.sh`) |
