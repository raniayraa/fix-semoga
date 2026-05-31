package api

import (
	"log"
	"net/http"

	"github.com/telmat/xdp-go/internal/config"
	"github.com/telmat/xdp-go/internal/maps"
)

// configResponse is the full firewall configuration returned by GET /api/config.
type configResponse struct {
	Flags    maps.FwFlags `json:"flags"`
	TCPPorts []uint16     `json:"tcp_ports"`
	UDPPorts []uint16     `json:"udp_ports"`
	Protos   []int        `json:"protos"`
}

// configRequest is the body for PUT /api/config.
// All fields are pointers so callers can send partial updates.
type configRequest struct {
	Flags    *maps.FwFlags `json:"flags"`
	TCPPorts []uint16      `json:"tcp_ports"`
	UDPPorts []uint16      `json:"udp_ports"`
	Protos   []int         `json:"protos"`
}

func (s *Server) handleGetConfig(w http.ResponseWriter, r *http.Request) {
	if !s.mgr.IsAttached() {
		writeError(w, http.StatusServiceUnavailable, "XDP not running")
		return
	}
	objs := s.mgr.Objects()

	flags, err := maps.ReadFlags(objs.FwConfig)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	tcpPorts, err := maps.ListPorts(objs.BlockedPortsTcp)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	udpPorts, err := maps.ListPorts(objs.BlockedPortsUdp)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	protos, err := maps.ListProtos(objs.BlockedProtos)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	protoInts := make([]int, len(protos))
	for i, p := range protos {
		protoInts[i] = int(p)
	}

	writeJSON(w, http.StatusOK, configResponse{
		Flags:    flags,
		TCPPorts: tcpPorts,
		UDPPorts: udpPorts,
		Protos:   protoInts,
	})
}

func (s *Server) handlePutConfig(w http.ResponseWriter, r *http.Request) {
	if !s.mgr.IsAttached() {
		writeError(w, http.StatusServiceUnavailable, "XDP not running")
		return
	}

	var req configRequest
	if !decodeJSON(w, r, &req) {
		return
	}

	objs := s.mgr.Objects()

	if req.Flags != nil {
		if err := maps.WriteFlags(objs.FwConfig, *req.Flags); err != nil {
			writeError(w, http.StatusInternalServerError, "write flags: "+err.Error())
			return
		}
	}
	if req.TCPPorts != nil {
		if err := maps.SetPorts(objs.BlockedPortsTcp, req.TCPPorts); err != nil {
			writeError(w, http.StatusInternalServerError, "write tcp ports: "+err.Error())
			return
		}
	}
	if req.UDPPorts != nil {
		if err := maps.SetPorts(objs.BlockedPortsUdp, req.UDPPorts); err != nil {
			writeError(w, http.StatusInternalServerError, "write udp ports: "+err.Error())
			return
		}
	}
	if req.Protos != nil {
		protoBytes := make([]uint8, len(req.Protos))
		for i, p := range req.Protos {
			protoBytes[i] = uint8(p)
		}
		if err := maps.SetProtos(objs.BlockedProtos, protoBytes); err != nil {
			writeError(w, http.StatusInternalServerError, "write protos: "+err.Error())
			return
		}
	}

	// Snapshot the full current BPF map state so we can persist it and keep
	// the manager's in-memory config in sync. Both are needed: the file so the
	// daemon re-reads it on process restart; the in-memory update so the next
	// Stop+Start cycle (Ansible) uses the latest port list without a full restart.
	tcpPorts, _ := maps.ListPorts(objs.BlockedPortsTcp)
	udpPorts, _ := maps.ListPorts(objs.BlockedPortsUdp)
	protos, _ := maps.ListProtos(objs.BlockedProtos)
	flags, _ := maps.ReadFlags(objs.FwConfig)

	saveCfg := &config.XDPConfig{
		BlockedPorts: &config.BlockedPorts{
			TCP: tcpPorts,
			UDP: udpPorts,
		},
		BlockedProtocols: protos,
		FirewallFlags:    fwFlagsToConfig(flags),
	}

	// Update in-memory config so the next Start() call (after Ansible stop+start)
	// applies the saved port list instead of the original startup defaults.
	s.mgr.SetConfig(saveCfg)

	if s.cfgPath != "" {
		if err := config.Save(s.cfgPath, saveCfg); err != nil {
			log.Printf("warning: failed to persist config to %s: %v", s.cfgPath, err)
		}
	}

	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func fwFlagsToConfig(f maps.FwFlags) *config.FwFlagsConfig {
	b := func(v bool) *bool { return &v }
	return &config.FwFlagsConfig{
		BlockICMPPing:         b(f.BlockICMPPing),
		BlockIPFragments:      b(f.BlockIPFragments),
		BlockMalformedTC:      b(f.BlockMalformedTC),
		BlockAllTCP:           b(f.BlockAllTCP),
		BlockAllUDP:           b(f.BlockAllUDP),
		BlockBroadcast:        b(f.BlockBroadcast),
		BlockMulticast:        b(f.BlockMulticast),
		EventsEnabled:         b(f.EventsEnabled),
		SecurityEventsEnabled: b(f.SecurityEventsEnabled),
	}
}
