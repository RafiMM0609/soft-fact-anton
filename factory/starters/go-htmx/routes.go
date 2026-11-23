package main

import (
	"database/sql"
	"encoding/json"
	"net/http"

	"app/views"
)

// DomainRegistrar allows modular domain route files (e.g. routes_kasir.go, routes_menu.go)
// to self-register endpoints without modifying routes.go, preventing git merge conflicts.
type DomainRegistrar func(mux *http.ServeMux, db *sql.DB)

var domainRegistrars []DomainRegistrar

// RegisterDomain is called by domain modules (typically in func init()) to register routes.
func RegisterDomain(registrar DomainRegistrar) {
	domainRegistrars = append(domainRegistrars, registrar)
}

// RegisterRoutes registers application endpoints on the ServeMux.
// Domain modules register themselves via RegisterDomain in init().
func RegisterRoutes(mux *http.ServeMux, db *sql.DB) {
	// Health check endpoint required by factory verification contract
	mux.HandleFunc("GET /api/health", handleHealth)

	// Vendored static asset for offline HTMX execution
	mux.HandleFunc("GET /assets/htmx.min.js", handleHtmxAsset)

	// Index page
	mux.HandleFunc("GET /", handleIndex)

	// Initialize self-registered modular domain routes
	for _, registrar := range domainRegistrars {
		registrar(mux, db)
	}
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

func handleHtmxAsset(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/javascript; charset=utf-8")
	w.Header().Set("Cache-Control", "public, max-age=3600")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(views.HtmxJS)
}

func handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	content := `
    <div class="card">
      <h3>Sistem Siap Digunakan</h3>
      <p style="color: var(--text-muted); margin-top: 8px;">
        Aplikasi Fullstack Go + HTMX + SQLite telah aktif.
      </p>
    </div>`
	_ = views.RenderPage(w, "Dashboard", content)
}
