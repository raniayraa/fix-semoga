// xdpd — XDP Firewall + Forwarder daemon with REST API and React frontend.
//
// Usage:
//
//	sudo ./xdpd -iface <NIC> [-redirect-dev <NIC>] [-config <file>] [-addr :8080] [-db /path/to/events.db] [-static ./frontend/dist]
//	sudo ./xdpd -iface <NIC> -stats [-stats-interval 2]
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/cilium/ebpf"
	"github.com/telmat/xdp-go/internal/api"
	"github.com/telmat/xdp-go/internal/config"
	"github.com/telmat/xdp-go/internal/db"
	"github.com/telmat/xdp-go/internal/maps"
	"github.com/telmat/xdp-go/internal/xdp"
)

func main() {
	iface         := flag.String("iface",          "eth0",            "ingress NIC name (XDP attaches here)")
	redirectDev   := flag.String("redirect-dev",   "",                "egress NIC for XDP_REDIRECT; seeds tx_port DEVMAP slot 0")
	configPath    := flag.String("config",         "",                "JSON config file to pre-seed maps at attach time")
	dbPath        := flag.String("db",             "/tmp/xdpd.db",   "SQLite database path for traffic logs")
	addr          := flag.String("addr",           ":8080",           "HTTP listen address")
	static        := flag.String("static",         "./frontend/dist", "React build directory to serve")
	statsMode     := flag.Bool("stats",            false,             "print live stats table instead of starting HTTP server (XDP must already be running)")
	statsInterval := flag.Int("stats-interval",    2,                 "stats refresh interval in seconds (used with -stats)")
	flag.Parse()

	if os.Getuid() != 0 {
		log.Fatal("xdpd must run as root (UID 0) to load BPF programs")
	}

	// ── Stats mode: read pinned maps from a running daemon and print a table ──
	if *statsMode {
		runStats(*iface, *statsInterval)
		return
	}

	// ── Load optional startup config file ────────────────────────────────────
	var cfg *config.XDPConfig
	if *configPath != "" {
		var err error
		cfg, err = config.Load(*configPath)
		if err != nil {
			log.Fatalf("load config %s: %v", *configPath, err)
		}
		log.Printf("loaded config from %s", *configPath)
	}

	// ── Normal daemon mode ────────────────────────────────────────────────────
	if err := os.MkdirAll(filepath.Dir(*dbPath), 0o755); err != nil {
		log.Fatalf("create db dir: %v", err)
	}

	store, err := db.Open(*dbPath)
	if err != nil {
		log.Fatalf("open database: %v", err)
	}
	defer store.Close()

	mgr := xdp.NewManager(*iface, *redirectDev, cfg)
	srv := api.NewServer(mgr, store, *configPath)

	httpSrv := &http.Server{
		Addr:         *addr,
		Handler:      srv.Router(*static),
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 30 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		log.Printf("xdpd listening on %s  (iface=%s  db=%s)", *addr, *iface, *dbPath)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Println("shutting down...")

	shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = httpSrv.Shutdown(shutCtx)

	if mgr.IsAttached() {
		if err := mgr.Stop(); err != nil {
			log.Printf("stop XDP: %v", err)
		}
	}
	log.Println("bye")
}

// runStats opens the pinned xdp_stats map from a running daemon and prints a
// refreshing table of per-action packet and byte rates.
func runStats(iface string, intervalSec int) {
	pinPath := filepath.Join(xdp.PinBaseDir, iface, "xdp_stats")
	statsMap, err := ebpf.LoadPinnedMap(pinPath, nil)
	if err != nil {
		log.Fatalf("open pinned stats map at %s: %v\n(is xdpd running and XDP attached on %s?)", pinPath, err, iface)
	}
	defer statsMap.Close()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	log.Printf("watching stats on %s — Ctrl+C to stop", iface)
	if err := maps.PollStats(ctx, statsMap, intervalSec, printStats); err != nil {
		log.Fatalf("stats: %v", err)
	}
}

// printStats formats and prints a delta StatsMap as a refreshing table.
func printStats(delta maps.StatsMap, intervalSec int) {
	// Clear screen and move cursor to top-left.
	fmt.Print("\033[2J\033[H")

	fmt.Printf("%-16s %12s %8s %12s %8s\n", "Action", "Packets", "pps", "Bytes", "Mbps")
	fmt.Println(strings.Repeat("─", 60))

	rows := []struct {
		label string
		rec   maps.StatsRec
	}{
		{"DROP", delta.Drop},
		{"TX", delta.TX},
		{"REDIRECT", delta.Redirect},
		{"PASS", delta.Pass},
		{"TTL_EXCEEDED", delta.TTLExceeded},
	}

	for _, row := range rows {
		pps := row.rec.Packets / uint64(intervalSec)
		mbps := float64(row.rec.Bytes*8) / float64(intervalSec) / 1e6
		fmt.Printf("%-16s %12d %8d %12d %8.1f\n",
			row.label, row.rec.Packets, pps, row.rec.Bytes, mbps)
	}
	fmt.Printf("\n[interval %ds — Ctrl+C to stop]\n", intervalSec)
}
