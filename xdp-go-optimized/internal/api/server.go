// Package api provides the HTTP REST API and static file server.
package api

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"sync"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/telmat/xdp-go/internal/db"
	"github.com/telmat/xdp-go/internal/xdp"
)

// Server holds shared state for all HTTP handlers.
type Server struct {
	mgr        *xdp.Manager
	store      *db.Store
	cfgPath    string // path to turbo.json; empty means no persistence
	mu         sync.Mutex
	rbufCancel context.CancelFunc // cancels the ring buffer consumer goroutine
}

// NewServer creates a Server with the given XDP manager, SQLite store, and
// optional config file path. When cfgPath is non-empty, PUT /api/config
// persists changes back to that file so they survive daemon restarts.
func NewServer(mgr *xdp.Manager, store *db.Store, cfgPath string) *Server {
	return &Server{mgr: mgr, store: store, cfgPath: cfgPath}
}

// Router builds and returns the chi router with all API routes and static file serving.
func (s *Server) Router(staticDir string) http.Handler {
	r := chi.NewRouter()
	r.Use(middleware.Logger)
	r.Use(middleware.Recoverer)
	r.Use(corsMiddleware)

	r.Route("/api", func(r chi.Router) {
		r.Get("/status", s.handleStatus)
		r.Post("/start", s.handleStart)
		r.Post("/stop", s.handleStop)
		r.Post("/restart", s.handleRestart)
		r.Get("/config", s.handleGetConfig)
		r.Put("/config", s.handlePutConfig)
		r.Get("/stats/live", s.handleStatsLive)
		r.Get("/logs", s.handleLogs)
		r.Get("/routes", s.handleGetRoutes)
		r.Post("/routes", s.handlePostRoute)
		r.Delete("/routes/{ip}", s.handleDeleteRoute)
		r.Get("/devmap", s.handleGetDevmap)
		r.Post("/devmap", s.handlePostDevmap)
		r.Delete("/devmap/{slot}", s.handleDeleteDevmap)
		r.Get("/system/cpu", s.handleGetCPU)
		r.Put("/system/cpu", s.handlePutCPU)
		r.Get("/system/settings", s.handleGetSettings)
		r.Put("/system/settings", s.handlePutSettings)
	})

	// Serve React build output; fall back to index.html for SPA routing.
	r.Handle("/*", spaHandler(staticDir))

	return r
}

// corsMiddleware adds permissive CORS headers for development.
func corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// spaHandler serves static files and falls back to index.html for any path
// that doesn't match an existing file. This is required for React Router's
// client-side routing (e.g. /monitoring, /routes navigate directly in browser).
func spaHandler(dir string) http.Handler {
	fsys := http.Dir(dir)
	fileServer := http.FileServer(fsys)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Check if the requested path exists as a file.
		f, err := fsys.Open(r.URL.Path)
		if err != nil {
			if os.IsNotExist(err) {
				// Not a real file → serve index.html so React Router handles it.
				http.ServeFile(w, r, filepath.Join(dir, "index.html"))
				return
			}
		} else {
			f.Close()
		}
		fileServer.ServeHTTP(w, r)
	})
}

// writeJSON encodes v as JSON and writes it to w with the given status code.
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// writeError writes a JSON error response.
func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// decodeJSON decodes the request body into dst. Returns false and writes an
// error response if decoding fails.
func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	if err := json.NewDecoder(r.Body).Decode(dst); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return false
	}
	return true
}
