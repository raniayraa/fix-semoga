// Package xdp manages the lifecycle of the XDP kernel program:
// loading, attaching to a NIC, pinning maps, and detaching.
package xdp

import (
	"errors"
	"fmt"
	"log"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sync"

	"github.com/cilium/ebpf"
	"github.com/cilium/ebpf/asm"
	"github.com/cilium/ebpf/link"
	"github.com/telmat/xdp-go/internal/bpfobj"
	"github.com/telmat/xdp-go/internal/config"
	"github.com/telmat/xdp-go/internal/maps"
)

const PinBaseDir = "/sys/fs/bpf"

// Manager owns a loaded XDP program and its associated BPF maps.
// All exported methods are safe for concurrent use.
type Manager struct {
	mu          sync.RWMutex
	ifname      string
	redirectDev string          // egress NIC for XDP_REDIRECT; seeds tx_port DEVMAP slot 0
	cfg         *config.XDPConfig // optional startup config; nil = use built-in defaults only
	pinDir      string
	objs        bpfobj.XdpProgObjects
	xdpLink     link.Link
	egressLink  link.Link // XDP pass program on egress NIC; required for DEVMAP redirect (kernel 5.9+)
}

// NewManager creates a Manager for the given ingress interface.
// redirectDev is the egress NIC name to seed into tx_port DEVMAP slot 0 (empty = skip).
// cfg is the optional startup JSON config (nil = use built-in defaults only).
func NewManager(ifname, redirectDev string, cfg *config.XDPConfig) *Manager {
	return &Manager{
		ifname:      ifname,
		redirectDev: redirectDev,
		cfg:         cfg,
		pinDir:      filepath.Join(PinBaseDir, ifname),
	}
}

// Start loads the BPF object, pins all maps under /sys/fs/bpf/<ifname>/,
// attaches the XDP program to the ingress NIC, seeds default blocked port
// lists, and applies any startup config.
// Returns an error if already attached or if loading fails.
func (m *Manager) Start() error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if m.xdpLink != nil {
		return fmt.Errorf("XDP already attached to %s", m.ifname)
	}

	if err := os.MkdirAll(m.pinDir, 0o700); err != nil {
		return fmt.Errorf("create pin dir %s: %w", m.pinDir, err)
	}

	opts := &ebpf.CollectionOptions{
		Maps: ebpf.MapOptions{
			PinPath: m.pinDir,
		},
	}

	if err := bpfobj.LoadXdpProgObjects(&m.objs, opts); err != nil {
		return fmt.Errorf("load BPF objects: %w", err)
	}

	// cilium/ebpf does NOT auto-pin maps unless the BPF C spec has
	// __uint(pinning, LIBBPF_PIN_BY_NAME). Explicitly pin each map so that
	// out-of-process tools (e.g. -stats mode) can open them by path.
	if err := m.pinMaps(); err != nil {
		m.objs.Close()
		return fmt.Errorf("pin maps: %w", err)
	}

	iface, err := net.InterfaceByName(m.ifname)
	if err != nil {
		m.objs.Close()
		return fmt.Errorf("interface %q not found: %w", m.ifname, err)
	}

	// Detach any existing XDP program (legacy or BPF-link) before attaching.
	_ = exec.Command("ip", "link", "set", "dev", m.ifname, "xdp", "off").Run()

	m.xdpLink, err = link.AttachXDP(link.XDPOptions{
		Program:   m.objs.XdpFirewallFwd,
		Interface: iface.Index,
		Flags:     link.XDPDriverMode,
	})
	if err != nil {
		m.objs.Close()
		return fmt.Errorf("attach XDP to %s: %w", m.ifname, err)
	}

	// ── Default: security events ON, normal traffic logging OFF ──────────────
	// fw_config BPF ARRAY is zero-initialized by the kernel.
	// FW_CFG_SECURITY_EVENTS=1 keeps DROP/TTL_EXCEEDED logged for visibility.
	// FW_CFG_EVENTS_ENABLED stays 0 — PASS/TX/REDIRECT incur zero ring buffer
	// overhead, eliminating the DB bottleneck on the forwarding fast path.
	// Both flags can be toggled at runtime via PUT /api/config.
	if err := maps.SetFlag(m.objs.FwConfig, maps.FwCfgSecurityEvents, true); err != nil {
		log.Printf("warn: set security_events_enabled default: %v", err)
	}

	// ── Seed default port blocklists (only when maps are fresh / empty) ──────
	if existing, _ := maps.ListPorts(m.objs.BlockedPortsTcp); len(existing) == 0 {
		if err := maps.SetPorts(m.objs.BlockedPortsTcp, maps.DefaultTCPPorts); err != nil {
			log.Printf("warn: seed default TCP ports: %v", err)
		}
	}
	if existing, _ := maps.ListPorts(m.objs.BlockedPortsUdp); len(existing) == 0 {
		if err := maps.SetPorts(m.objs.BlockedPortsUdp, maps.DefaultUDPPorts); err != nil {
			log.Printf("warn: seed default UDP ports: %v", err)
		}
	}

	// ── Seed tx_port DEVMAP slot 0 ───────────────────────────────────────────
	// Config file redirect_dev takes precedence over the CLI flag.
	redirectDev := m.redirectDev
	if m.cfg != nil && m.cfg.RedirectDev != "" {
		redirectDev = m.cfg.RedirectDev
	}
	if redirectDev != "" {
		rIface, err := net.InterfaceByName(redirectDev)
		if err != nil {
			log.Printf("warn: redirect-dev %q not found: %v", redirectDev, err)
		} else {
			if err := maps.SetDevmapSlot(m.objs.TxPort, 0, uint32(rIface.Index)); err != nil {
				log.Printf("warn: seed tx_port DEVMAP: %v", err)
			}
			// Kernel 5.9+: DEVMAP redirect requires an XDP program on the egress NIC.
			// Attach a trivial pass program so bpf_redirect_map() doesn't silently fail.
			if err := m.attachEgressPass(redirectDev); err != nil {
				log.Printf("warn: attach egress XDP pass to %s: %v", redirectDev, err)
			}
		}
	}

	// ── Apply startup config (overrides defaults where specified) ────────────
	if m.cfg != nil {
		m.applyConfig()
	}

	return nil
}

// applyConfig writes the startup XDPConfig into the live BPF maps.
// Must be called with m.mu held (called from Start).
func (m *Manager) applyConfig() {
	cfg := m.cfg

	// Firewall flags — only touch flags that are explicitly set in JSON.
	if cfg.FirewallFlags != nil {
		flags, err := maps.ReadFlags(m.objs.FwConfig)
		if err != nil {
			log.Printf("warn: read fw_config: %v", err)
		} else {
			ff := cfg.FirewallFlags
			if ff.BlockICMPPing != nil {
				flags.BlockICMPPing = *ff.BlockICMPPing
			}
			if ff.BlockIPFragments != nil {
				flags.BlockIPFragments = *ff.BlockIPFragments
			}
			if ff.BlockMalformedTC != nil {
				flags.BlockMalformedTC = *ff.BlockMalformedTC
			}
			if ff.BlockAllTCP != nil {
				flags.BlockAllTCP = *ff.BlockAllTCP
			}
			if ff.BlockAllUDP != nil {
				flags.BlockAllUDP = *ff.BlockAllUDP
			}
			if ff.BlockBroadcast != nil {
				flags.BlockBroadcast = *ff.BlockBroadcast
			}
			if ff.BlockMulticast != nil {
				flags.BlockMulticast = *ff.BlockMulticast
			}
			if ff.EventsEnabled != nil {
				flags.EventsEnabled = *ff.EventsEnabled
			}
			if ff.SecurityEventsEnabled != nil {
				flags.SecurityEventsEnabled = *ff.SecurityEventsEnabled
			}
			if err := maps.WriteFlags(m.objs.FwConfig, flags); err != nil {
				log.Printf("warn: write fw_config: %v", err)
			}
		}
	}

	// Blocked ports — override the defaults seeded above.
	if cfg.BlockedPorts != nil {
		if cfg.BlockedPorts.TCP != nil {
			if err := maps.SetPorts(m.objs.BlockedPortsTcp, cfg.BlockedPorts.TCP); err != nil {
				log.Printf("warn: set blocked TCP ports from config: %v", err)
			}
		}
		if cfg.BlockedPorts.UDP != nil {
			if err := maps.SetPorts(m.objs.BlockedPortsUdp, cfg.BlockedPorts.UDP); err != nil {
				log.Printf("warn: set blocked UDP ports from config: %v", err)
			}
		}
	}

	// Blocked protocols.
	if len(cfg.BlockedProtocols) > 0 {
		if err := maps.SetProtos(m.objs.BlockedProtos, cfg.BlockedProtocols); err != nil {
			log.Printf("warn: set blocked protos from config: %v", err)
		}
	}

	// Forwarding routes.
	for _, r := range cfg.Routes {
		if err := maps.AddRoute(m.objs.FwdTable, r); err != nil {
			log.Printf("warn: add route %s from config: %v", r.IP, err)
		}
	}
}

// pinMaps explicitly pins every BPF map under m.pinDir.
// cilium/ebpf only reuses pinned maps via PinPath; it does not auto-pin new
// maps unless the BPF C spec sets __uint(pinning, LIBBPF_PIN_BY_NAME).
// Calling Pin() on an already-pinned path is a no-op (EEXIST is ignored).
func (m *Manager) pinMaps() error {
	type entry struct {
		name string
		bm   *ebpf.Map
	}
	for _, e := range []entry{
		{"xdp_stats", m.objs.XdpStats},
		{"blocked_ports_tcp", m.objs.BlockedPortsTcp},
		{"blocked_ports_udp", m.objs.BlockedPortsUdp},
		{"blocked_protos", m.objs.BlockedProtos},
		{"fw_config", m.objs.FwConfig},
		{"fwd_table", m.objs.FwdTable},
		{"tx_port", m.objs.TxPort},
		{"packet_events", m.objs.PacketEvents},
	} {
		pinPath := filepath.Join(m.pinDir, e.name)
		if err := e.bm.Pin(pinPath); err != nil && !errors.Is(err, os.ErrExist) {
			return fmt.Errorf("%s: %w", e.name, err)
		}
	}
	return nil
}

// attachEgressPass creates a minimal XDP_PASS program and attaches it to the
// egress NIC. This is required on Linux 5.9+ for bpf_redirect_map() with
// BPF_MAP_TYPE_DEVMAP: without an XDP program on the target device, the kernel
// silently drops the redirected packet.
func (m *Manager) attachEgressPass(ifname string) error {
	passSpec := &ebpf.ProgramSpec{
		Name:    "xdp_pass",
		Type:    ebpf.XDP,
		License: "GPL",
		Instructions: asm.Instructions{
			asm.Mov.Imm(asm.R0, 2), // XDP_PASS = 2
			asm.Return(),
		},
	}
	passProg, err := ebpf.NewProgram(passSpec)
	if err != nil {
		return fmt.Errorf("create xdp_pass program: %w", err)
	}
	defer passProg.Close() // link holds its own reference after AttachXDP

	iface, err := net.InterfaceByName(ifname)
	if err != nil {
		return fmt.Errorf("egress interface %q: %w", ifname, err)
	}

	// Detach any pre-existing XDP program on egress before attaching ours.
	_ = exec.Command("ip", "link", "set", "dev", ifname, "xdp", "off").Run()

	egressLnk, err := link.AttachXDP(link.XDPOptions{
		Program:   passProg,
		Interface: iface.Index,
		Flags:     link.XDPDriverMode,
	})
	if err != nil {
		return fmt.Errorf("attach xdp_pass to %s: %w", ifname, err)
	}
	m.egressLink = egressLnk
	return nil
}

// Stop detaches the XDP program, closes BPF objects, and removes pinned maps.
func (m *Manager) Stop() error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if m.egressLink != nil {
		m.egressLink.Close()
		m.egressLink = nil
	}
	if m.xdpLink != nil {
		m.xdpLink.Close()
		m.xdpLink = nil
	}
	m.objs.Close()

	if err := os.RemoveAll(m.pinDir); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("remove pin dir %s: %w", m.pinDir, err)
	}
	return nil
}

// IsAttached reports whether the XDP program is currently attached.
func (m *Manager) IsAttached() bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.xdpLink != nil
}

// Objects returns a pointer to the loaded BPF objects.
// Caller must not call this after Stop().
func (m *Manager) Objects() *bpfobj.XdpProgObjects {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return &m.objs
}

// PinDir returns the BPF map pin directory for this interface.
func (m *Manager) PinDir() string {
	return m.pinDir
}

// Ifname returns the ingress interface name.
func (m *Manager) Ifname() string {
	return m.ifname
}

// RedirectDev returns the egress interface name (empty if not set).
func (m *Manager) RedirectDev() string {
	return m.redirectDev
}

// SetConfig replaces the in-memory startup config used by the next Start() call.
// This keeps the manager's config in sync when handlePutConfig persists changes
// to turbo.json, so a subsequent Stop+Start restores the updated port lists.
func (m *Manager) SetConfig(cfg *config.XDPConfig) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.cfg = cfg
}

// Reconfigure updates the ingress and egress interface names.
// XDP must be stopped before calling this.
func (m *Manager) Reconfigure(ifname, redirectDev string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.xdpLink != nil {
		return fmt.Errorf("XDP is attached; call Stop() first")
	}
	m.ifname = ifname
	m.redirectDev = redirectDev
	m.pinDir = filepath.Join(PinBaseDir, ifname)
	return nil
}
