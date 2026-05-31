# Ansible Playbook Runbook

Run each command, then immediately verify before proceeding to the next.

```bash
# Set inventory path once — use this variable in all commands below
INV=~/final_t40/ansible/inventory.ini
```

---

## Command 1 — Basic Interface Setup

```bash
ansible-playbook -i $INV ~/final_t40/ansible/01_basic_setup.yaml 2>&1 | tee /tmp/run_01_basic_setup.txt
```

## Verify 1 — IP addresses assigned on all nodes

```bash
# Node 1 → enp1s0f0np0=192.168.46.1, enp1s0f1np1=192.168.56.1
ansible -i $INV 10.90.1.1 -m shell -a "ip addr show enp1s0f0np0 | grep inet; ip addr show enp1s0f1np1 | grep inet" 2>&1 | tee /tmp/verify_01_node1.txt

# Node 4 → enp1s0f1np1=192.168.46.4
ansible -i $INV 10.90.1.4 -m shell -a "ip addr show enp1s0f1np1 | grep inet" 2>&1 | tee /tmp/verify_01_node4.txt

# Node 5 → enp1s0f1np1=192.168.56.5
ansible -i $INV 10.90.1.5 -m shell -a "ip addr show enp1s0f1np1 | grep inet" 2>&1 | tee /tmp/verify_01_node5.txt

# Node 6 → enp1s0f0np0=192.168.56.6, enp1s0f1np1=192.168.46.6
ansible -i $INV 10.90.1.6 -m shell -a "ip addr show enp1s0f0np0 | grep inet; ip addr show enp1s0f1np1 | grep inet" 2>&1 | tee /tmp/verify_01_node6.txt
```

**Expected:** Each node shows the correct IP address on each interface. No `inet` line means the address was not assigned — re-run Command 1.

---

## Command 2 — Static Routes & IP Forwarding

```bash
ansible-playbook -i $INV ~/final_t40/ansible/02_setup_route.yaml 2>&1 | tee /tmp/run_02_setup_route.txt
```

## Verify 2 — Routes, forwarding, and end-to-end ping

```bash
# Node 4 → must have route 192.168.56.0/24 via 192.168.46.6
ansible -i $INV 10.90.1.4 -m shell -a "ip route show | grep 192.168" 2>&1 | tee /tmp/verify_02_node4_routes.txt

# Node 5 → must have route 192.168.46.0/24 via 192.168.56.6
ansible -i $INV 10.90.1.5 -m shell -a "ip route show | grep 192.168" 2>&1 | tee /tmp/verify_02_node5_routes.txt

# Node 6 → ip_forward must be 1
ansible -i $INV 10.90.1.6 -m shell -a "sysctl net.ipv4.ip_forward; sysctl net.ipv6.conf.all.forwarding" 2>&1 | tee /tmp/verify_02_node6_forward.txt

# End-to-end: Node 4 → Node 5 through Node 6 (critical path)
ansible -i $INV 10.90.1.4 -m shell -a "ping -c3 -W2 192.168.56.5 && echo 'FORWARDING OK' || echo 'FORWARDING FAIL'" 2>&1 | tee /tmp/verify_02_e2e.txt
```

**Expected:** Routes exist on nodes 4 and 5. Node 6 shows `ip_forward = 1`. End-to-end ping shows `FORWARDING OK`.

---

## Command 3 — Deploy Scripts & Bind DPDK

```bash
ansible-playbook -i $INV ~/final_t40/ansible/03_setup_scripts.yaml 2>&1 | tee /tmp/run_03_setup_scripts.txt
```

## Verify 3 — pkt files, lua scripts, and DPDK binding

```bash
# Node 1 → pkt file, lua scripts, DPDK binding on both ports
ansible -i $INV 10.90.1.1 -m shell -a "
  ls -la /home/telmat/node1_send.pkt /home/telmat/scripts/getstats.lua /home/telmat/scripts/bind-to-DPDK.sh;
  python3 /home/telmat/dpdk/usertools/dpdk-devbind.py -s 2>&1 | grep -E '01:00|Network'
" 2>&1 | tee /tmp/verify_03_node1.txt

# Node 4 → pkt file + DPDK binding
ansible -i $INV 10.90.1.4 -m shell -a "
  ls -la /home/telmat/node4_send.pkt;
  python3 /home/telmat/dpdk/usertools/dpdk-devbind.py -s 2>&1 | grep -E '01:00|Network'
" 2>&1 | tee /tmp/verify_03_node4.txt

# Node 5 → DPDK binding only
ansible -i $INV 10.90.1.5 -m shell -a "
  python3 /home/telmat/dpdk/usertools/dpdk-devbind.py -s 2>&1 | grep -E '01:00|Network'
" 2>&1 | tee /tmp/verify_03_node5.txt
```

**Expected:** Files exist on nodes 1 and 4. All relevant NICs on nodes 1, 4, 5 show `drv=vfio-pci` (DPDK-bound).

---

## Command 4A — Kernel Forwarder on Node 6

> Run **one** of Command 4A, 4B, or 4C depending on which forwarder you are testing.

```bash
ansible-playbook -i $INV ~/final_t40/ansible/04_setup_kernel_node6.yaml 2>&1 | tee /tmp/run_04_kernel.txt
```

## Verify 4A — Kernel forwarding: IPs, routes, static ARP

```bash
ansible -i $INV 10.90.1.6 -b -m shell -a "
  echo '=== IPs ===';
  ip addr show enp1s0f1np1 | grep inet;
  ip addr show enp1s0f0np0 | grep inet;
  echo '=== Routes ===';
  ip route show | grep 192.168;
  echo '=== Static ARP (PERMANENT entries) ===';
  ip neigh show | grep PERMANENT;
  echo '=== IP Forward ===';
  sysctl net.ipv4.ip_forward
" 2>&1 | tee /tmp/verify_04_kernel_node6.txt
```

**Expected:** IPs present on both interfaces. Routes to `192.168.46.0/24` and `192.168.56.0/24`. Four `PERMANENT` ARP entries (nodes 1, 4, 5). `ip_forward = 1`.

---

## Command 4B — VPP Forwarder on Node 6

```bash
ansible-playbook -i $INV ~/final_t40/ansible/04_setup_vpp_node6.yaml 2>&1 | tee /tmp/run_04_vpp.txt
```

## Verify 4B — VPP: service, interfaces, neighbors

```bash
ansible -i $INV 10.90.1.6 -b -m shell -a "
  echo '=== VPP Service ===';
  systemctl is-active vpp;
  echo '=== VPP Interfaces ===';
  vppctl show int;
  echo '=== VPP Interface Addresses ===';
  vppctl show int addr;
  echo '=== VPP IP Neighbors ===';
  vppctl show ip neighbors
" 2>&1 | tee /tmp/verify_04_vpp_node6.txt
```

**Expected:** `vpp` service is `active`. Both `TwentyFiveGigabitEthernet1/0/0` and `1/0/1` are `up`. IPs `192.168.56.6` and `192.168.46.6` assigned. Four static neighbors visible.

---

## Command 4C — XDP Forwarder on Node 6

> Requires `xdpd` already running: `bash ~/final_t40/start2.sh`

```bash
ansible-playbook -i $INV ~/final_t40/ansible/04_setup_xdp_node6.yaml 2>&1 | tee /tmp/run_04_xdp.txt
```

## Verify 4C — XDP: NIC driver, queue count, API routes

```bash
# Node 6 — NIC must be back on kernel driver, not vfio-pci
ansible -i $INV 10.90.1.6 -b -m shell -a "
  echo '=== NIC driver (must NOT be vfio-pci) ===';
  python3 /home/telmat/dpdk/usertools/dpdk-devbind.py -s | grep 01:00;
  echo '=== Queue count (expect Combined: 4) ===';
  ethtool -l enp1s0f1np1 | grep -A5 'Current hardware';
  echo '=== VPP must be stopped ===';
  systemctl is-active vpp || echo stopped
" 2>&1 | tee /tmp/verify_04_xdp_node6_nic.txt

# XDP REST API — forwarding table and settings (runs on localhost)
curl -s http://localhost:9898/api/routes | python3 -m json.tool 2>&1 | tee /tmp/verify_04_xdp_routes.txt
curl -s http://localhost:9898/api/system/settings | python3 -m json.tool 2>&1 | tee /tmp/verify_04_xdp_settings.txt
```

**Expected:** NIC shows kernel driver (e.g. `drv=ice`), not `vfio-pci`. Queue count `Combined: 4`. VPP is `inactive/stopped`. API returns two forwarding entries (`192.168.56.5` and `192.168.56.1`).

---

## Command 5 — Start Pktgen

> Ensure Command 4A, 4B, or 4C has been verified before this step.
> This playbook **waits** for a start signal (`/tmp/ansible_pktgen_start`) and a stop signal (`/tmp/ansible_pktgen_stop`) — the sweep scripts send these automatically. To test manually, open a second terminal and run:
> ```bash
> sleep 15 && touch /tmp/ansible_pktgen_start && sleep 20 && touch /tmp/ansible_pktgen_stop
> ```

```bash
ansible-playbook -i $INV ~/final_t40/ansible/05_start_pktgen.yaml 2>&1 | tee /tmp/run_05_pktgen.txt
```

## Verify 5 — Pktgen running in tmux and stats being collected

```bash
# All sender nodes → tmux session named 'pktgen' must exist
ansible -i $INV 10.90.1.1,10.90.1.4,10.90.1.5 -m shell -a "
  echo '=== tmux sessions ===';
  tmux list-sessions 2>&1;
  echo '=== pktgen process ===';
  pgrep -a pktgen || echo 'not running'
" 2>&1 | tee /tmp/verify_05_pktgen_tmux.txt

# Stats log should be growing (run ~10s after pktgen starts)
ansible -i $INV 10.90.1.1,10.90.1.4,10.90.1.5 -m shell -a "
  wc -l /tmp/pktgen_stats.log 2>&1 || echo 'no stats file yet'
" 2>&1 | tee /tmp/verify_05_pktgen_stats.txt
```

**Expected:** `tmux list-sessions` shows `pktgen` session on nodes 1, 4, 5. `/tmp/pktgen_stats.log` has lines being written. Results collected to `~/final_t40/results/pktgen_stats_<timestamp>/`.

---

## Log Files Summary

| Step | Run log | Verify log(s) |
|------|---------|---------------|
| 01 Basic setup | `/tmp/run_01_basic_setup.txt` | `/tmp/verify_01_node{1,4,5,6}.txt` |
| 02 Routes | `/tmp/run_02_setup_route.txt` | `/tmp/verify_02_node{4,5}_routes.txt`, `verify_02_node6_forward.txt`, `verify_02_e2e.txt` |
| 03 Scripts | `/tmp/run_03_setup_scripts.txt` | `/tmp/verify_03_node{1,4,5}.txt` |
| 04A Kernel | `/tmp/run_04_kernel.txt` | `/tmp/verify_04_kernel_node6.txt` |
| 04B VPP | `/tmp/run_04_vpp.txt` | `/tmp/verify_04_vpp_node6.txt` |
| 04C XDP | `/tmp/run_04_xdp.txt` | `/tmp/verify_04_xdp_node6_nic.txt`, `verify_04_xdp_routes.txt`, `verify_04_xdp_settings.txt` |
| 05 Pktgen | `/tmp/run_05_pktgen.txt` | `/tmp/verify_05_pktgen_tmux.txt`, `verify_05_pktgen_stats.txt` |
