// Package config loads the optional JSON startup configuration file.
package config

import (
	"encoding/json"
	"os"

	"github.com/telmat/xdp-go/internal/maps"
)

// XDPConfig is the top-level structure for the JSON startup config file.
// All fields are optional; omitted fields fall back to built-in defaults.
//
// Example file:
//
//	{
//	  "redirect_dev": "eth1",
//	  "firewall_flags": { "block_all_tcp": true },
//	  "blocked_ports": { "tcp": [80, 443], "udp": [53] },
//	  "blocked_protocols": [89],
//	  "routes": [
//	    { "ip": "10.0.0.1", "dst_mac": "aa:bb:cc:dd:ee:ff",
//	      "src_mac": "11:22:33:44:55:66", "action": "redirect", "port_key": 0 }
//	  ]
//	}
type XDPConfig struct {
	// RedirectDev is the egress NIC name to seed into tx_port DEVMAP slot 0.
	// Takes precedence over the -redirect-dev CLI flag when both are set.
	RedirectDev string `json:"redirect_dev,omitempty"`

	// FirewallFlags selectively overrides firewall feature flags.
	// Only fields present in JSON are applied; others keep their defaults.
	FirewallFlags *FwFlagsConfig `json:"firewall_flags,omitempty"`

	// BlockedPorts replaces the default TCP/UDP blocked port lists when present.
	BlockedPorts *BlockedPorts `json:"blocked_ports,omitempty"`

	// BlockedProtocols is a list of IP protocol numbers (0-255) to block.
	BlockedProtocols []uint8 `json:"blocked_protocols,omitempty"`

	// Routes are forwarding table entries to insert at startup.
	Routes []maps.RouteEntry `json:"routes,omitempty"`
}

// FwFlagsConfig holds per-flag overrides for the fw_config BPF map.
// Use *bool so that unset (null/absent) fields are distinguishable from false.
type FwFlagsConfig struct {
	BlockICMPPing    *bool `json:"block_icmp_ping,omitempty"`
	BlockIPFragments *bool `json:"block_ip_fragments,omitempty"`
	BlockMalformedTC *bool `json:"block_malformed_tcp,omitempty"`
	BlockAllTCP      *bool `json:"block_all_tcp,omitempty"`
	BlockAllUDP      *bool `json:"block_all_udp,omitempty"`
	BlockBroadcast   *bool `json:"block_broadcast,omitempty"`
	BlockMulticast   *bool `json:"block_multicast,omitempty"`
	EventsEnabled         *bool `json:"events_enabled,omitempty"`          // true = sample PASS/TX/REDIRECT (default: off)
	SecurityEventsEnabled *bool `json:"security_events_enabled,omitempty"` // true = log DROP/TTL_EXCEEDED (default: on)
}

// BlockedPorts holds TCP and UDP destination port lists.
type BlockedPorts struct {
	TCP []uint16 `json:"tcp,omitempty"`
	UDP []uint16 `json:"udp,omitempty"`
}

// Load reads and parses an XDPConfig from a JSON file at path.
func Load(path string) (*XDPConfig, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var cfg XDPConfig
	if err := json.NewDecoder(f).Decode(&cfg); err != nil {
		return nil, err
	}
	return &cfg, nil
}

// Save writes cfg to path as indented JSON, replacing the file atomically.
func Save(path string, cfg *XDPConfig) error {
	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o644)
}
