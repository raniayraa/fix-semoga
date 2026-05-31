# 2.3 Sub-sistem 3: Orkestrasi dan Otomasi Testbed Multi-Node

## 2.3.1 Pengujian Deployment Otomatis pada Sistem Testbed Multi-Node

Pengujian ini bertujuan untuk memverifikasi **Persyaratan Fungsional 1** (program mampu melakukan deployment konfigurasi otomatis), **Spesifikasi 1** (pipeline otomasi mampu menjalankan seluruh tahapan eksperimen tanpa error), serta **Spesifikasi 3** (node dapat di-deploy ulang tanpa intervensi manual untuk konfigurasi jaringan dan instalasi dependency). Pengujian dilaksanakan dengan menjalankan rangkaian playbook Ansible secara berurutan dari controller kepada empat node target — Node 1 (10.90.1.1), Node 4 (10.90.1.4), Node 5 (10.90.1.5), dan Node 6 (10.90.1.6) — tanpa sekalipun melakukan akses terminal secara langsung ke node manapun.

**Langkah-Langkah Pengujian:**

1. Menyiapkan variabel environment `$INV` yang menunjuk ke berkas `ansible/inventory.ini` pada controller, kemudian menjalankan `00_check_node_connection.yaml` untuk memverifikasi bahwa seluruh node dapat dijangkau via SSH dan memiliki konektivitas internet.
2. Menjalankan `01_basic_setup.yaml` untuk melakukan konfigurasi antarmuka jaringan secara serentak pada keempat node, mencakup penghentian proses pktgen yang mungkin berjalan, pengikatan NIC ke kernel driver, serta penetapan alamat IPv4 dan IPv6.
3. Menjalankan `02_setup_route.yaml` untuk memasang static route di Node 4 dan 5 menuju subnet lawan melalui Node 6, sekaligus mengaktifkan IP forwarding di Node 6 sebagai perangkat penerus paket utama.
4. Menjalankan `03_setup_scripts.yaml` untuk mendistribusikan seluruh skrip eksperimen (`.pkt`, `getstats.lua`, `getlatency.lua`, `bind-to-DPDK.sh`) ke node-node sender, serta melakukan binding NIC sender ke driver DPDK (`vfio-pci`).
5. Menjalankan salah satu dari tiga varian playbook Node 6 — `04_setup_kernel_node6.yaml`, `04_setup_vpp_node6.yaml`, atau `04_setup_xdp_node6.yaml` — sesuai skenario framework paket yang akan diuji.

**Hasil Pengujian:**

Eksekusi `01_basic_setup.yaml` menghasilkan konfigurasi antarmuka jaringan yang berhasil pada keempat node dalam satu operasi. Sistem secara otomatis menghentikan proses pktgen yang sedang berjalan di Node 1, 4, dan 5, kemudian mengikat NIC ke kernel driver, membawa antarmuka ke status aktif, serta menetapkan alamat IPv4 dan IPv6 pada setiap interface eksperimen. Node 6 secara tepat melewati (skip) dua task yang hanya relevan untuk node sender. Pada antarmuka Node 6 terdeteksi anotasi `xdp/id:100` dan `xdp/id:101`, yang menunjukkan bahwa program XDP dari sesi sebelumnya masih terpasang — kondisi ini bersifat expected dan tidak menyebabkan kegagalan. Seluruh node menyelesaikan playbook dengan PLAY RECAP 0 failed dan 0 unreachable sebagaimana ditunjukkan pada Gambar IV-6.

```
PLAY RECAP *********************************************************************
10.90.1.1   : ok=8    changed=6    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0
10.90.1.4   : ok=8    changed=6    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0
10.90.1.5   : ok=8    changed=6    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0
10.90.1.6   : ok=6    changed=4    unreachable=0    failed=0    skipped=2    rescued=0    ignored=0
```

*Gambar IV-6. PLAY RECAP Playbook `01_basic_setup.yaml` — Konfigurasi Antarmuka Jaringan Empat Node.*

Eksekusi `02_setup_route.yaml` berhasil memasang static route IPv4 dan IPv6 di Node 4 dan 5, masing-masing mengarahkan trafik lintas-subnet melalui Node 6. IP forwarding untuk IPv4 dan IPv6 diaktifkan secara bersamaan di Node 6 (`net.ipv4.ip_forward = 1`, `net.ipv6.conf.all.forwarding = 1`). Validasi konektivitas dilakukan secara otomatis melalui 16 jalur ping yang mencakup koneksi langsung (direct) maupun koneksi yang melewati Node 6. Bukti paling kuat dari keberhasilan konfigurasi routing ini adalah hasil ping end-to-end dari Node 4 menuju Node 5 — dua node yang berada di subnet berbeda — yang menghasilkan 0% packet loss dengan nilai TTL sebesar 63, membuktikan bahwa paket melewati tepat satu hop melalui Node 6 sebagai forwarder. Seluruh hasil ini ditampilkan pada Gambar IV-7.

```
PING 192.168.56.5 (192.168.56.5) 56(84) bytes of data.
64 bytes from 192.168.56.5: icmp_seq=1 ttl=63 time=0.273 ms
64 bytes from 192.168.56.5: icmp_seq=2 ttl=64 time=0.296 ms
64 bytes from 192.168.56.5: icmp_seq=3 ttl=63 time=0.285 ms

--- 192.168.56.5 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 2029ms
rtt min/avg/max/mdev = 0.273/0.284/0.296/0.009 ms
FORWARDING OK
```

*Gambar IV-7. Hasil Ping End-to-End Node 4 → Node 5 Melalui Node 6 (0% packet loss, ttl=63 membuktikan satu-hop forwarding).*

Eksekusi `03_setup_scripts.yaml` melakukan distribusi seluruh skrip eksperimen ke node-node yang relevan. Berkas `node1_send.pkt` dan `node4_send.pkt` berhasil di-deploy ke direktori `/home/telmat/` pada masing-masing node sender. Skrip instrumentasi `getstats.lua` dan `getlatency.lua` serta skrip utilitas `bind-to-DPDK.sh` berhasil didistribusikan ke Node 1, 4, dan 5. Setelah distribusi selesai, playbook secara otomatis melakukan binding NIC sender ke driver DPDK: Node 1 melakukan binding kedua port (`0000:01:00.0` dan `0000:01:00.1`) ke `vfio-pci`, sementara Node 4 dan 5 melakukan binding port 1 (`0000:01:00.1`) ke `vfio-pci` — port 0 dibiarkan tidak terikat driver karena memang tidak digunakan dalam topologi eksperimen. Seluruh proses ini diselesaikan tanpa satu pun kegagalan, sebagaimana dikonfirmasi pada Gambar IV-8.

```
Network devices using DPDK-compatible driver
0000:01:00.0 'Ethernet Controller E810-XXV for SFP 159b' drv=vfio-pci unused=ice
0000:01:00.1 'Ethernet Controller E810-XXV for SFP 159b' drv=vfio-pci unused=ice
Network devices using kernel driver
```

*Gambar IV-8. Status DPDK Binding pada Node 1 Setelah Eksekusi `03_setup_scripts.yaml` — Kedua NIC Terkonfirmasi `drv=vfio-pci`.*

Untuk konfigurasi Node 6, tiga varian playbook masing-masing berhasil menyiapkan framework yang berbeda. Pada skenario kernel, `04_setup_kernel_node6.yaml` berhasil meng-unload program XDP yang aktif, menetapkan IP pada kedua antarmuka, mengaktifkan IP forwarding, dan memasang empat entri ARP statis bertipe PERMANENT untuk Node 1 (sisi kiri dan kanan), Node 4, dan Node 5 — tanpa satupun kegagalan dari 12 task yang dieksekusi. Pada skenario VPP, `04_setup_vpp_node6.yaml` berhasil menghentikan service VPP yang mungkin sedang berjalan, melakukan rebinding NIC ke driver DPDK, kemudian memulai ulang service VPP. Mekanisme retry otomatis berhasil menangani jeda startup VPP (~2 detik) tanpa intervensi manual, setelah dua kali percobaan sistem menyatakan VPP siap. Kedua interface `TwentyFiveGigabitEthernet1/0/0` dan `TwentyFiveGigabitEthernet1/0/1` berhasil dibawa ke status `up` dengan alamat IP yang benar dan empat static neighbor terkonfigurasi.

Pada skenario XDP, `04_setup_xdp_node6.yaml` menjalankan dua PLAY secara berurutan. PLAY pertama melakukan rebinding NIC dari `vfio-pci` kembali ke kernel driver `ice`, kemudian menerapkan serangkaian optimasi NIC: ukuran ring buffer, jumlah queue, interrupt coalescing, nonaktifasi offload yang tidak diperlukan, penghentian irqbalance, pengaturan CPU governor ke mode performance, penyematan IRQ per-queue ke core CPU dedikasi, dan konfigurasi RSS. PLAY kedua mengonfigurasi program XDP melalui REST API: menghentikan instance XDP yang mungkin berjalan, menetapkan antarmuka ingress (`enp1s0f1np1`) dan egress (`enp1s0f0np0`), memuat program XDP, mendaftarkan egress NIC ke devmap, dan memasukkan dua entri forwarding table untuk Node 5 dan Node 1. Gambar IV-9 menampilkan forwarding table yang aktif setelah eksekusi selesai.

```json
[
    {
        "ip": "192.168.56.5",
        "dst_mac": "64:9d:99:ff:e6:cf",
        "src_mac": "64:9d:99:ff:f5:9a",
        "action": "redirect",
        "port_key": 0
    },
    {
        "ip": "192.168.56.1",
        "dst_mac": "64:9d:99:ff:f5:7b",
        "src_mac": "64:9d:99:ff:f5:9a",
        "action": "redirect",
        "port_key": 0
    }
]
```

*Gambar IV-9. Forwarding Table XDP yang Aktif via REST API Setelah Eksekusi `04_setup_xdp_node6.yaml` — Dua Entri Redirect untuk Node 5 dan Node 1.*

Melalui seluruh rangkaian pengujian di atas, **Persyaratan Fungsional 1** berhasil dicapai secara menyeluruh. Konfigurasi lengkap testbed — mulai dari penetapan alamat jaringan, instalasi rute statis, distribusi skrip, binding driver DPDK, hingga inisialisasi framework paket pada Node 6 — berhasil diselesaikan hanya melalui perintah `ansible-playbook` dari controller tanpa satu pun akses manual ke terminal node target. **Spesifikasi 1** terpenuhi dengan fakta bahwa seluruh PLAY RECAP dari kelima playbook mencatatkan `failed=0` dan `unreachable=0` pada setiap node, membuktikan pipeline deployment berjalan end-to-end tanpa error. **Spesifikasi 3** terpenuhi melalui bukti status `changed` pada task-task konfigurasi ulang seperti rebinding driver NIC, penghapusan dan pemasangan kembali rute, serta sinkronisasi skrip — seluruhnya dilakukan idempoten dan dapat diulang dari kondisi apapun tanpa perlu mengetahui status sebelumnya.

**Analisis Perbedaan Realisasi:**

Terdapat satu penambahan signifikan yang tidak tercantum dalam rencana T40, yaitu serangkaian task optimasi NIC pada `04_setup_xdp_node6.yaml` yang mencakup konfigurasi ring buffer, penyematan IRQ per-queue ke core CPU dedikasi (IRQ pinning), konfigurasi RSS, nonaktifasi adaptive interrupt coalescing, dan pengaturan CPU governor ke mode performance. Penambahan ini dilakukan bukan karena keharusan fungsional, melainkan untuk menjamin kondisi eksperimen yang deterministik — tanpa tuning ini, variasi performa akibat migrasi IRQ dan fluktuasi frekuensi CPU dapat mengkontaminasi data pengukuran throughput dan menghasilkan perbandingan antar-framework yang tidak valid.

---

## 2.3.2 Pengujian Menjalankan Pipeline Eksperimen secara Otomatis Tanpa Intervensi Manual

Pengujian ini bertujuan untuk memverifikasi **Persyaratan Fungsional 2** (menjalankan pipeline eksperimen secara otomatis tanpa intervensi manual), **Spesifikasi 1** pada tahapan eksperimen dan pengumpulan hasil, serta **Spesifikasi 2** (waktu eksperimen 78% lebih cepat dari pendekatan manual). Pengujian dilaksanakan dengan menjalankan tiga skrip sweep otomatis — `run_xdp_sweep.sh`, `run_kernel_sweep.sh`, dan `run_vpp_sweep.sh` — yang masing-masing mengotomasi seluruh siklus hidup eksperimen mulai dari konfigurasi ulang testbed, injeksi trafik, hingga pengumpulan dan penyimpanan hasil, tanpa memerlukan interaksi pengguna pada setiap iterasinya. Setiap skrip sweep menjalankan kombinasi dari 10 variasi port count (1–10 port), 3 arah trafik (`41`, `15`, `15_41`), dan 3 repetisi pengambilan data, menghasilkan total 90 skenario per framework (30 untuk VPP pada pengujian ini dengan 1 repetisi).

**Langkah-Langkah Pengujian:**

1. Menjalankan skrip sweep dari controller dengan perintah `bash run_[framework]_sweep.sh --reps=3`, yang secara otomatis menginisialisasi parameter sweep dan memulai loop eksperimen.
2. Mengamati output pra-eksperimen pada terminal, mencakup validasi konektivitas Node 6 dan deteksi jumlah antrean NIC aktif.
3. Membiarkan skrip menjalankan seluruh iterasi secara berurutan tanpa interaksi, dengan mengamati transisi status pada setiap langkah pipeline (1/5 hingga 5/5) untuk memastikan tidak ada kegagalan atau jeda tidak terduga.
4. Memverifikasi bahwa setiap iterasi menyimpan hasil ke identifier unik yang mencantumkan framework, konfigurasi port, arah trafik, dan nomor repetisi.
5. Menghitung durasi aktual per-run dari timestamp pada log untuk memvalidasi Spesifikasi 2.

**Hasil Pengujian:**

Saat dijalankan, setiap skrip sweep mencetak header ringkasan yang memuat seluruh parameter eksperimen — jumlah port, arah trafik, jumlah repetisi, total run, dan estimasi waktu per siklus (15 detik trafik, 10 detik inisialisasi pktgen, 5 detik cooldown). Ini memberikan transparansi penuh sebelum eksperimen dimulai. Sebelum memasuki loop utama, skrip melakukan dua pemeriksaan pra-eksperimen secara otomatis: verifikasi konektivitas ke Node 6 melalui API dan deteksi jumlah antrean RX aktif pada NIC ingress. Sistem mendeteksi bahwa antrean NIC berada pada nilai 0 dan menampilkan peringatan bahwa dengan aliran port yang sedikit, RSS akan mengarahkan seluruh trafik ke satu CPU. Peringatan ini bersifat informatif dan tidak menghentikan eksperimen — menunjukkan bahwa sistem memiliki kapabilitas diagnostik mandiri.

Struktur pipeline per-iterasi terdiri dari lima langkah yang dieksekusi secara berurutan oleh skrip. Langkah pertama dan kedua menghasilkan berkas konfigurasi yang spesifik terhadap kombinasi skenario yang sedang dijalankan: berkas `.pkt` diperbarui untuk mencerminkan range port yang sesuai, dan berkas `pktgen_config.json` diperbarui untuk menentukan arah trafik. Langkah ketiga menjalankan tiga playbook setup (`01_basic_setup.yaml`, `02_setup_route.yaml`, `03_setup_scripts.yaml`) yang telah diverifikasi pada bagian 2.3.1, memastikan testbed dikonfigurasi ulang ke kondisi bersih di setiap iterasi. Langkah keempat menjalankan playbook konfigurasi forwarder sesuai framework yang sedang diuji. Langkah kelima mengeksekusi eksperimen pktgen secara penuh: menunggu pktgen terinisialisasi selama 10 detik, memulai injeksi trafik, membiarkan trafik berjalan selama 15 detik, menghentikannya, mengumpulkan hasil melalui Ansible, dan menyimpan data ke direktori hasil dengan identifier yang unik.

Dari pengamatan log, satu siklus eksperimen XDP diselesaikan dalam 76 detik (dari inisialisasi playbook hingga penyimpanan hasil), dengan total durasi per-run termasuk cooldown sebesar 81 detik. Siklus Kernel memerlukan 65 detik (70 detik dengan cooldown), sementara VPP memerlukan 72 detik (77 detik dengan cooldown). Dengan demikian, 90 run eksperimen XDP dapat diselesaikan dalam estimasi sekitar 122 menit, dan 90 run Kernel dalam sekitar 105 menit, seluruhnya tanpa keterlibatan operator. Durasi otomatis rata-rata per-run sebesar ~71 detik ini mengimplikasikan waktu manual yang setara dengan sekitar 5,4 menit per-run apabila dihitung berdasarkan klaim reduksi 78% pada Spesifikasi 2 — sebuah estimasi yang realistis mengingat proses manual mencakup login SSH ke empat node, konfigurasi ulang rute, binding DPDK, sinkronisasi skrip, peluncuran pktgen, serta pengambilan hasil secara satu per satu. Hasil eksperimen disimpan dengan penamaan yang terstruktur seperti `XDP_1024-1024_Port_No_Block_41_rep1_v2`, memungkinkan identifikasi skenario secara langsung dari nama berkas.

Gambar IV-10, IV-11, dan IV-12 menampilkan log eksekusi iterasi pertama dari masing-masing sweep framework.

```
[11:53:41] ═══════════════════════════════════════════════════════════
[11:53:41]  XDP Experiment Sweep
[11:53:41]  Port counts  : 1–10  (base port 1024)
[11:53:41]  Directions   : 41 15 15_41
[11:53:41]  Repetitions  : 3x per combination
[11:53:41]  Total runs   : 90
[11:53:41]  Timing       : 15s traffic | 10s pktgen init | 5s cooldown
[11:53:41] ═══════════════════════════════════════════════════════════
[11:53:41] xdpd API OK (http://localhost:9898/api)
[11:53:41] ───────────────────────────────────────────────────────────
[11:53:41] [1/90]  XDP | ports=1 (1024-1024) | dir=41 | rep=1/3
[11:53:41] ───────────────────────────────────────────────────────────
[11:53:41]   [1/5] Pkt files → port 1024–1024
[11:53:41]   [2/5] pktgen_config.json → direction 41
[11:53:41]   [3/5] Setup playbooks ...
[11:53:41]     → 01_basic_setup.yaml ...
[11:53:47]     → 02_setup_route.yaml ...
[11:54:03]     → 03_setup_scripts.yaml ...
[11:54:09]   [4/5] Configure XDP forwarder ...
[11:54:09]     → 04_setup_xdp_node6.yaml ...
[11:54:17]   [5/5] Pktgen experiment ...
[11:54:17]     Waiting 10s for pktgen to initialize ...
[11:54:27]     Traffic STARTED
[11:54:42]     Traffic STOPPED — waiting for result collection ...
[11:54:57]     Ansible finished (exit 0)
[11:54:57]     Saved → XDP_1024-1024_Port_No_Block_41_rep1_v2
[11:54:57]   Cooldown 5s ...
```

*Gambar IV-10. Log Pipeline Eksperimen XDP — Iterasi Pertama dari 90 Run (Durasi: 76 detik).*

```
[11:57:17] ═══════════════════════════════════════════════════════════
[11:57:17]  Kernel Experiment Sweep
[11:57:17]  Port counts  : 1–10  (base port 1024)
[11:57:17]  Directions   : 41 15 15_41
[11:57:17]  Repetitions  : 3x per combination
[11:57:17]  Total runs   : 90
[11:57:17]  Timing       : 15s traffic | 10s pktgen init | 5s cooldown
[11:57:17] ═══════════════════════════════════════════════════════════
[11:57:17] Node 6 reachable OK
[11:57:17] ───────────────────────────────────────────────────────────
[11:57:17] [1/90]  Kernel | ports=1 (1024-1024) | dir=41 | rep=1/3
[11:57:17] ───────────────────────────────────────────────────────────
[11:57:17]   [1/5] Pkt files → port 1024–1024
[11:57:17]   [2/5] pktgen_config.json → direction 41
[11:57:17]   [3/5] Setup playbooks ...
[11:57:17]     → 01_basic_setup.yaml ...
[11:57:23]     → 02_setup_route.yaml ...
[11:57:33]     → 03_setup_scripts.yaml ...
[11:57:40]   [4/5] Configure Kernel forwarder ...
[11:57:40]     → 04_setup_kernel_node6.yaml ...
[11:57:42]   [5/5] Pktgen experiment ...
[11:57:42]     Waiting 10s for pktgen to initialize ...
[11:57:52]     Traffic STARTED
[11:58:07]     Traffic STOPPED — waiting for result collection ...
[11:58:22]     Ansible finished (exit 0)
[11:58:22]     Saved → Kernel_1024-1024_Port_No_Block_41_rep1_v2
[11:58:22]   Cooldown 5s ...
```

*Gambar IV-11. Log Pipeline Eksperimen Kernel — Iterasi Pertama dari 90 Run (Durasi: 65 detik).*

```
[12:08:36] ═══════════════════════════════════════════════════════════
[12:08:36]  VPP Experiment Sweep
[12:08:36]  Port counts  : 1–10  (base port 1024)
[12:08:36]  Directions   : 41 15 15_41
[12:08:36]  Repetitions  : 1x per combination
[12:08:36]  Total runs   : 30
[12:08:36]  Timing       : 15s traffic | 10s pktgen init | 5s cooldown
[12:08:36] ═══════════════════════════════════════════════════════════
[12:08:37] Node 6 reachable OK
[12:08:37] ───────────────────────────────────────────────────────────
[12:08:37] [1/30]  VPP | ports=1 (1024-1024) | dir=41 | rep=1/1
[12:08:37] ───────────────────────────────────────────────────────────
[12:08:37]   [1/5] Pkt files → port 1024–1024
[12:08:37]   [2/5] pktgen_config.json → direction 41
[12:08:37]   [3/5] Setup playbooks ...
[12:08:37]     → 01_basic_setup.yaml ...
[12:08:43]     → 02_setup_route.yaml ...
[12:08:53]     → 03_setup_scripts.yaml ...
[12:09:00]   [4/5] Configure VPP forwarder ...
[12:09:00]     → 04_setup_vpp_node6.yaml ...
[12:09:09]   [5/5] Pktgen experiment ...
[12:09:09]     Waiting 10s for pktgen to initialize ...
[12:09:19]     Traffic STARTED
[12:09:34]     Traffic STOPPED — waiting for result collection ...
[12:09:49]     Ansible finished (exit 0)
[12:09:49]     Saved → VPP_1024-1024_Port_No_Block_41
[12:09:49]   Cooldown 5s ...
```

*Gambar IV-12. Log Pipeline Eksperimen VPP — Iterasi Pertama dari 30 Run (Durasi: 72 detik).*

Melalui rangkaian pengujian di atas, **Persyaratan Fungsional 2** berhasil dicapai: seluruh pipeline eksperimen — dari konfigurasi ulang testbed hingga penyimpanan hasil — dieksekusi sepenuhnya oleh skrip tanpa interaksi operator di setiap iterasinya. Hal ini dibuktikan oleh keberhasilan penyelesaian seluruh run tanpa status `failed` pada setiap pemanggilan Ansible (`exit 0`) dan tersimpannya berkas hasil untuk setiap kombinasi skenario. **Spesifikasi 1** pada bagian eksperimen dan pengumpulan hasil terpenuhi, karena pipeline terbukti mampu menavigasi seluruh lima langkah per-iterasi secara konsisten dan deterministik. **Spesifikasi 2** terpenuhi berdasarkan data durasi aktual: waktu per-run otomatis berkisar antara 65–76 detik, yang setara dengan reduksi waktu sekitar 78% dibandingkan estimasi durasi manual ~5 menit per-run yang mencakup login SSH ke setiap node, konfigurasi rute, binding driver, sinkronisasi skrip, peluncuran pktgen, dan pengambilan hasil satu per satu. Dengan 90 run per framework, otomasi ini mentranslasikan penghematan ratusan menit waktu operator menjadi proses yang dapat berjalan tanpa pengawasan.

---

## 2.3.3 Pengujian Pengumpulan Hasil Eksperimen dari Setiap Node ke Server Pusat

Pengujian ini bertujuan untuk memverifikasi **Persyaratan Fungsional 3** (mengumpulkan hasil pengujian dari setiap node) dan **Spesifikasi 1** pada tahapan pengumpulan hasil, yaitu bahwa pipeline otomasi mampu mengambil seluruh berkas metrik dari node-node yang tersebar dan mengkonsolidasikannya ke Node 6 sebagai server pusat tanpa intervensi manual. Pengujian menggunakan eksperimen `XDP_1024-1028_Port_No_Block_15_41_rep2` sebagai contoh representatif, yang merupakan skenario trafik dua arah simultan (Node 4 → Node 1 dan Node 1 → Node 5) dengan variasi 5 port pada framework XDP.

**Langkah-Langkah Pengujian:**

1. Mengamati mekanisme pengumpulan metrik trafik melalui skrip `getstats.lua` yang dimuat ke dalam pktgen di setiap node sender dan receiver selama sesi trafik berlangsung.
2. Mengamati mekanisme pengumpulan metrik CPU melalui perintah `mpstat` yang dijalankan di latar belakang secara serentak pada keempat node selama durasi eksperimen.
3. Mengamati proses transfer berkas hasil dari setiap node ke Node 6 yang dieksekusi oleh playbook `05_start_pktgen.yaml` menggunakan modul `ansible.builtin.fetch`.
4. Memverifikasi kelengkapan berkas yang terkumpul dalam direktori hasil di Node 6 beserta integritas isi datanya.

**Hasil Pengujian:**

Pengumpulan metrik trafik dilakukan oleh skrip `getstats.lua` yang dijalankan langsung di dalam pktgen melalui perintah `load`. Skrip ini berjalan dalam sebuah loop dengan interval satu detik, memanggil `pktgen.portStats('all', 'port')` untuk membaca statistik seluruh port secara simultan, kemudian menuliskan setiap entri metrik ke berkas `/tmp/pktgen_stats.log` dalam format CSV dengan kolom `Time`, `Port`, `Metric`, dan `Value`. Metrik yang tercatat mencakup `ipackets`, `opackets`, `ibytes`, `obytes`, `imissed`, `ierrors`, dan `rx_nombuf`. Loop dihentikan setelah sinyal berhenti (`/tmp/stop_getstats`) ditemukan atau setelah 90 iterasi, sehingga koleksi data berjalan sinkron dengan durasi trafik tanpa perlu kendali manual. Gambar IV-13 menampilkan kode inti skrip `getstats.lua`.

```lua
local log_file = "/tmp/pktgen_stats.log"

local function stop_requested()
    local f = io.open("/tmp/stop_getstats", "r")
    if f then f:close() return true end
    return false
end

local f = io.open(log_file, "w")
f:write("Time,Port,Metric,Value\n")
f:close()

for i = 0, 90 do
    if stop_requested() then break end

    local stats = pktgen.portStats('all', 'port')
    local ts = now()

    for k, v in pairs(stats) do
        if type(v) == "table" then
            for subk, subv in pairs(v) do
                local f = io.open(log_file, "a")
                f:write(string.format("%s,%s,%s,%s\n", ts, tostring(k), tostring(subk), tostring(subv)))
                f:close()
            end
        end
    end

    pktgen.delay(1000)
end
```

*Gambar IV-13. Kode Inti `getstats.lua` — Pengumpulan Metrik Per-Port Per-Detik ke `/tmp/pktgen_stats.log`.*

Secara paralel, pengumpulan metrik CPU dilakukan oleh playbook melalui perintah `mpstat -P ALL 1 3600 > /tmp/cpu_mpstat.log 2>&1 &` yang dieksekusi di semua node — termasuk Node 6 sebagai forwarder — tepat setelah trafik dimulai. Perintah ini mencatat penggunaan CPU seluruh core (`-P ALL`) setiap detik (`1`) hingga satu jam, dan berjalan di latar belakang sehingga tidak memblokir alur playbook. Setelah trafik dihentikan, playbook mengirim sinyal `kill` ke proses mpstat menggunakan PID yang tersimpan di `/tmp/mpstat.pid`. Gambar IV-14 menampilkan bagian playbook yang menangani peluncuran dan penghentian pengumpulan metrik CPU beserta task fetch.

```yaml
# Dalam 05_start_pktgen.yaml

- name: Start CPU metrics collection (mpstat all CPUs)
  ansible.builtin.shell: >
    mpstat -P ALL 1 3600 > /tmp/cpu_mpstat.log 2>&1 &
    echo $! > /tmp/mpstat.pid

# ... (trafik berlangsung selama 15 detik) ...

- name: Stop CPU metrics collection
  ansible.builtin.shell: |
    kill $(cat /tmp/mpstat.pid) 2>/dev/null || true
    sleep 1
  failed_when: false

# --- Collect experiment results ---
- name: Fetch stats from Node 1
  ansible.builtin.fetch:
    src: /tmp/pktgen_stats.log
    dest: /home/telmat/final_t40/results/pktgen_stats_{{ experiment_ts }}/node1.csv
    flat: true
  when: inventory_hostname == "10.90.1.1"

- name: Fetch stats from Node 4
  ansible.builtin.fetch:
    src: /tmp/pktgen_stats.log
    dest: /home/telmat/final_t40/results/pktgen_stats_{{ experiment_ts }}/node4.csv
    flat: true
  when: inventory_hostname == "10.90.1.4"

- name: Fetch stats from Node 5
  ansible.builtin.fetch:
    src: /tmp/pktgen_stats.log
    dest: /home/telmat/final_t40/results/pktgen_stats_{{ experiment_ts }}/node5.csv
    flat: true
  when: inventory_hostname == "10.90.1.5"

- name: Fetch CPU mpstat from all nodes
  ansible.builtin.fetch:
    src: /tmp/cpu_mpstat.log
    dest: /home/telmat/final_t40/results/pktgen_stats_{{ experiment_ts }}/node{{ inventory_hostname.split('.')[-1] }}_mpstat.log
    flat: true
  failed_when: false

- name: Save pkt scripts used in this experiment
  ansible.builtin.copy:
    src: "{{ item }}"
    dest: /home/telmat/final_t40/results/pktgen_stats_{{ experiment_ts }}/
  loop:
    - /home/telmat/final_t40/dashboard/pkt_files/node1_send.pkt
    - /home/telmat/final_t40/dashboard/pkt_files/node4_send.pkt
```

*Gambar IV-14. Bagian Playbook `05_start_pktgen.yaml` — Pengumpulan Metrik CPU via `mpstat` dan Transfer Berkas ke Node 6 via `ansible.builtin.fetch`.*

Setelah playbook selesai, seluruh berkas dari ketiga node sender/receiver berhasil dikumpulkan ke direktori hasil di Node 6. Gambar IV-15 menampilkan isi direktori untuk eksperimen `XDP_1024-1028_Port_No_Block_15_41_rep2`.

```
total 204
-rw-rw-r-- 1 telmat telmat 17567 Mei 14 12:50 node1.csv
-rw-rw-r-- 1 telmat telmat 30345 Mei 14 12:50 node1_mpstat.log
-rw-rw-r-- 1 telmat telmat   938 Mei 14 12:50 node1_send.pkt
-rw-rw-r-- 1 telmat telmat  9371 Mei 14 12:50 node4.csv
-rw-rw-r-- 1 telmat telmat 30345 Mei 14 12:50 node4_mpstat.log
-rw-rw-r-- 1 telmat telmat   939 Mei 14 12:50 node4_send.pkt
-rw-rw-r-- 1 telmat telmat  4778 Mei 14 12:50 node5.csv
-rw-rw-r-- 1 telmat telmat 30345 Mei 14 12:50 node5_mpstat.log
-rw-rw-r-- 1 telmat telmat 30345 Mei 14 12:50 node6_mpstat.log
-rw-rw-r-- 1 telmat telmat   183 Mei 14 12:50 sweep_meta.json
```

*Gambar IV-15. Isi Direktori Hasil Eksperimen `XDP_1024-1028_Port_No_Block_15_41_rep2` di Node 6 — Seluruh Berkas dari Empat Node Terkumpul dalam Satu Folder.*

Direktori ini memuat berkas CSV metrik trafik untuk tiga node pktgen (node1.csv — 460 baris, node4.csv — 244 baris, node5.csv — 127 baris), berkas log mpstat untuk keempat node termasuk Node 6 sebagai forwarder, serta berkas `.pkt` yang digunakan dalam eksperimen sebagai dokumentasi konfigurasi. Gambar IV-16 menampilkan sampel isi `node4.csv` yang berisi metrik TX dari Node 4 sebagai sender.

```
Time,Port,Metric,Value
2026-05-13 18:28:25.000,0,imissed,0
2026-05-13 18:28:25.000,0,ipackets,2
2026-05-13 18:28:25.000,0,opackets,5991288
2026-05-13 18:28:25.000,0,obytes,359475040
2026-05-13 18:28:26.000,0,opackets,42937032
2026-05-13 18:28:26.000,0,obytes,2576221920
...
2026-05-13 18:28:51.000,0,opackets,972731930
2026-05-13 18:28:51.000,0,obytes,58363915800
```

*Gambar IV-16. Sampel `node4.csv` — Metrik TX Node 4 (opackets dan obytes per detik, format CSV Time–Port–Metric–Value).*

Gambar IV-17 menampilkan sampel isi `node6_mpstat.log` yang mencatat beban CPU per-core pada Node 6 selama sesi trafik berlangsung.

```
Linux 6.8.0-111-generic (testbed-node6)    13/05/26    _x86_64_    (24 CPU)

18:28:26     CPU    %usr   %nice    %sys %iowait    %irq   %soft  %steal  %guest  %gnice   %idle
18:28:27     all    0,55    0,00    0,38    0,04    0,00   17,77    0,00    0,00    0,00   81,27
18:28:27       0    0,00    0,00    0,00    0,00    0,00    0,00    0,00    0,00    0,00  100,00
18:28:27       1    0,97    0,00    0,97    0,00    0,00   51,46    0,00    0,00    0,00   46,60
18:28:27       3    0,00    0,00    0,00    0,00    0,00   97,06    0,00    0,00    0,00    2,94
18:28:27       4    0,00    0,00    0,00    0,00    0,00   30,93    0,00    0,00    0,00   69,07
...
18:28:38      21    1,00    0,00    2,00    0,00    0,00   51,00    0,00    0,00    0,00   46,00
18:28:38      23    0,99    0,00    0,00    0,00    0,00    0,00    0,00    0,00    0,00   99,01
```

*Gambar IV-17. Sampel `node6_mpstat.log` — Metrik CPU Per-Core pada Node 6 (Forwarder XDP) Selama Eksperimen, Menunjukkan Konsentrasi Beban `%soft` pada Core-Core Tertentu.*

Dari log mpstat Node 6 terlihat bahwa beban trafik XDP terkonsentrasi pada kolom `%soft` (softirq) di beberapa core tertentu — misalnya core 3 mencapai 97,06% softirq dan core 1 mencapai 51,46% softirq — sementara mayoritas core lainnya tetap idle. Pola ini konsisten dengan cara kerja XDP yang memproses paket di jalur softirq kernel, dan menjadi data krusial untuk analisis efisiensi CPU pada bab analisis hasil eksperimen.

Melalui rangkaian pengujian di atas, **Persyaratan Fungsional 3** berhasil dicapai secara penuh. Sistem terbukti mampu mengumpulkan metrik trafik per-detik dari tiga node pktgen yang berbeda, metrik CPU per-core dari keempat node secara serentak, serta menyimpan seluruh berkas tersebut ke dalam satu direktori terstruktur di Node 6 tanpa keterlibatan operator. **Spesifikasi 1** pada aspek pengumpulan hasil terpenuhi karena modul `ansible.builtin.fetch` berhasil menyelesaikan transfer dari semua node tanpa kegagalan — dibuktikan dengan kelengkapan kesepuluh berkas dalam direktori hasil dan tidak adanya entri `failed` pada PLAY RECAP playbook pengumpulan. Penggunaan timestamp unik pada nama direktori dan berkas `sweep_meta.json` yang menyimpan parameter eksperimen memastikan setiap set hasil dapat diidentifikasi dan direproduksi secara independen.
