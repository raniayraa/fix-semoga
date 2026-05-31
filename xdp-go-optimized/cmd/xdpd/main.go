// xdpd — XDP Firewall + Forwarder daemon with REST API and React frontend.
//
// Usage:
//
//	sudo ./xdpd -iface <NIC> [-redirect-dev <NIC>] [-config <file>] [-addr :8080] [-db /path/to/events.db] [-static ./frontend/dist]
//	sudo ./xdpd -iface <NIC> -stats [-stats-interval 2]
package main

import (
	"context"
	"encoding/json"
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
	logsMode      := flag.Bool("logs",             false,             "follow firewall logs in real-time (XDP daemon must be running)")
	logsFilter    := flag.String("logs-filter",    "drop",            "action to watch: drop|ttl|all")
	logsLast      := flag.Int("logs-last",         20,                "number of past entries to show before following")
	flag.Parse()

	if os.Getuid() != 0 {
		log.Fatal("xdpd must run as root (UID 0) to load BPF programs")
	}

	// ── Stats mode: read pinned maps from a running daemon and print a table ──
	if *statsMode {
		runStats(*iface, *statsInterval)
		return
	}

	// ── Logs mode: tail firewall drop events via HTTP API ────────────────────
	if *logsMode {
		runLogs(*addr, *logsFilter, *logsLast)
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

// runLogs polls the daemon's HTTP API for firewall events, printing new entries
// as they arrive. Uses the API rather than opening SQLite directly to avoid
// multi-process WAL locking issues with the pure-Go SQLite driver.
func runLogs(listenAddr, filter string, tail int) {
	// Build the base URL from the listen address (e.g. ":8080" → "http://localhost:8080").
	base := listenAddr
	if strings.HasPrefix(base, ":") {
		base = "http://localhost" + base
	} else if !strings.HasPrefix(base, "http") {
		base = "http://" + base
	}
	base = strings.TrimRight(base, "/") + "/api/logs"

	// Map filter → action query param.
	var actionParam string
	switch filter {
	case "drop":
		actionParam = "action=0"
	case "ttl":
		actionParam = "action=4"
	case "all":
		actionParam = ""
	default:
		log.Fatalf("unknown -logs-filter value %q (use: drop|ttl|all)", filter)
	}

	client := &http.Client{Timeout: 5 * time.Second}

	fetchLogs := func(fromNs int64, limit int) ([]db.TrafficLog, error) {
		u := fmt.Sprintf("%s?limit=%d", base, limit)
		if actionParam != "" {
			u += "&" + actionParam
		}
		if fromNs > 0 {
			u += fmt.Sprintf("&from_ns=%d", fromNs)
		}
		resp, err := client.Get(u)
		if err != nil {
			return nil, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
		}
		var logs []db.TrafficLog
		if err := json.NewDecoder(resp.Body).Decode(&logs); err != nil {
			return nil, err
		}
		return logs, nil
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// Show recent entries on start (DESC order from API; reverse to print oldest-first).
	recent, err := fetchLogs(0, tail)
	if err != nil {
		log.Fatalf("connect to daemon at %s: %v\n(is xdpd running?)", base, err)
	}
	for i := len(recent) - 1; i >= 0; i-- {
		printDropLog(recent[i])
	}

	var lastNs int64
	if len(recent) > 0 {
		lastNs = recent[0].TimestampNs
	}

	log.Printf("following %s events — Ctrl+C to stop", filter)

	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			rows, err := fetchLogs(lastNs+1, 500)
			if err != nil {
				continue
			}
			for i := len(rows) - 1; i >= 0; i-- {
				printDropLog(rows[i])
				if rows[i].TimestampNs > lastNs {
					lastNs = rows[i].TimestampNs
				}
			}
		}
	}
}

func printDropLog(l db.TrafficLog) {
	ts := time.Unix(0, l.TimestampNs).Format("2006-01-02 15:04:05.000")
	src := fmt.Sprintf("%s:%d", l.SrcIP, l.SrcPort)
	dst := fmt.Sprintf("%s:%d", l.DstIP, l.DstPort)
	fmt.Printf("[%s] %-12s %-5s %-25s → %-25s %dB\n",
		ts, actionName(l.Action), protoName(l.Protocol), src, dst, l.PktLen)
}

func actionName(a int) string {
	switch a {
	case 0:
		return "DROP"
	case 1:
		return "PASS"
	case 2:
		return "TX"
	case 3:
		return "REDIRECT"
	case 4:
		return "TTL_EXCEEDED"
	default:
		return fmt.Sprintf("ACTION(%d)", a)
	}
}

func protoName(p int) string {
	switch p {
	case 1:
		return "ICMP"
	case 6:
		return "TCP"
	case 17:
		return "UDP"
	default:
		return fmt.Sprintf("PROTO%d", p)
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
