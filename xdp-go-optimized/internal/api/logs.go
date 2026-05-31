package api

import (
	"net/http"
	"strconv"
	"time"

	"github.com/telmat/xdp-go/internal/db"
)

// handleLogs queries traffic_logs with optional filters.
//
// Query params:
//
//	action=0-4        filter by pkt_action enum value
//	proto=0-255       filter by IP protocol number
//	range=30s|5m|30m|1h|6h|24h  filter by time range (from now)
//	limit=N           max rows (default 1000, max 5000)
func (s *Server) handleLogs(w http.ResponseWriter, r *http.Request) {
	q := db.LogQuery{}

	if v := r.URL.Query().Get("action"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 || n > 4 {
			writeError(w, http.StatusBadRequest, "invalid action")
			return
		}
		q.Action = &n
	}
	if v := r.URL.Query().Get("proto"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 || n > 255 {
			writeError(w, http.StatusBadRequest, "invalid proto")
			return
		}
		q.Protocol = &n
	}
	if v := r.URL.Query().Get("from_ns"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err == nil {
			q.FromNs = &n
		}
	} else if v := r.URL.Query().Get("range"); v != "" {
		dur := parseTimeRange(v)
		if dur > 0 {
			fromNs := time.Now().Add(-dur).UnixNano()
			q.FromNs = &fromNs
		}
	}
	if v := r.URL.Query().Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err == nil && n > 0 {
			q.Limit = n
		}
	}

	logs, err := s.store.QueryLogs(r.Context(), q)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if logs == nil {
		logs = []db.TrafficLog{}
	}
	writeJSON(w, http.StatusOK, logs)
}

// parseTimeRange converts a time-range string to a time.Duration.
// Supported values: 30s, 5m, 30m, 1h, 6h, 24h.
// Returns 0 for unknown values (meaning no time filter).
func parseTimeRange(s string) time.Duration {
	switch s {
	case "30s":
		return 30 * time.Second
	case "5m":
		return 5 * time.Minute
	case "30m":
		return 30 * time.Minute
	case "1h":
		return time.Hour
	case "6h":
		return 6 * time.Hour
	case "24h":
		return 24 * time.Hour
	default:
		return 0
	}
}
