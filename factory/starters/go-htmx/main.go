package main

import (
	_ "embed"
	"fmt"
	"log"
	"net/http"

	"app/core"
)

//go:embed schema.sql
var schemaSQL string

func main() {
	// 1. Initialize SQLite with defensive WAL concurrency settings
	db, err := core.InitDB(schemaSQL)
	if err != nil {
		log.Fatalf("Fatal: failed to initialize database: %v", err)
	}
	defer db.Close()

	// 2. Setup standard HTTP router with Go 1.22+ pattern matching
	mux := http.NewServeMux()
	RegisterRoutes(mux, db)

	// 3. Start server
	addr := fmt.Sprintf("%s:%s", core.GetHost(), core.GetPort())
	log.Printf("Starting Go + HTMX application on http://%s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil && err != http.ErrServerClosed {
		log.Fatalf("Server error: %v", err)
	}
}
