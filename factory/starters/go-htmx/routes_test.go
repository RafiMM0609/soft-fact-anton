package main

import (
	"database/sql"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestRegisterDomainModularRouting(t *testing.T) {
	called := false
	RegisterDomain(func(mux *http.ServeMux, db *sql.DB) {
		mux.HandleFunc("GET /domain-test", func(w http.ResponseWriter, r *http.Request) {
			called = true
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("domain ok"))
		})
	})

	mux := http.NewServeMux()
	RegisterRoutes(mux, nil)

	req := httptest.NewRequest("GET", "/domain-test", nil)
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected status 200, got %d", rec.Code)
	}
	if !called {
		t.Fatalf("expected domain handler to be called")
	}
	if rec.Body.String() != "domain ok" {
		t.Fatalf("unexpected body: %s", rec.Body.String())
	}
}

func TestHealthEndpoint(t *testing.T) {
	mux := http.NewServeMux()
	RegisterRoutes(mux, nil)

	req := httptest.NewRequest("GET", "/api/health", nil)
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected status 200, got %d", rec.Code)
	}
}
