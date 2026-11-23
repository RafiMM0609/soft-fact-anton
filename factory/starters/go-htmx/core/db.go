package core

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	_ "github.com/mattn/go-sqlite3"
)

// InitDB initializes SQLite connection with WAL mode and resilient concurrency settings.
func InitDB(schemaSQL string) (*sql.DB, error) {
	dbPath := resolveDBPath()

	// Ensure directory exists
	dir := filepath.Dir(dbPath)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return nil, fmt.Errorf("failed to create db directory %s: %w", dir, err)
	}

	// Defensive DSN: WAL mode prevents writer starvation, 5s busy timeout prevents SQLITE_BUSY
	dsn := fmt.Sprintf("file:%s?_journal_mode=WAL&_busy_timeout=5000&_synchronous=NORMAL&_foreign_keys=ON", dbPath)

	db, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, fmt.Errorf("failed to open sqlite database: %w", err)
	}

	// Flexible pool allows concurrent readers during active writes
	db.SetMaxOpenConns(10)
	db.SetMaxIdleConns(5)

	if err := db.Ping(); err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to ping sqlite database: %w", err)
	}

	// Run idempotent migrations/schema if provided
	if strings.TrimSpace(schemaSQL) != "" {
		if _, err := db.Exec(schemaSQL); err != nil {
			db.Close()
			return nil, fmt.Errorf("failed to execute schema.sql: %w", err)
		}
	}

	return db, nil
}

func resolveDBPath() string {
	// 1. Honor DATABASE_URL if provided
	if raw := os.Getenv("DATABASE_URL"); raw != "" {
		trimmed := strings.TrimPrefix(raw, "file:")
		if idx := strings.Index(trimmed, "?"); idx != -1 {
			trimmed = trimmed[:idx]
		}
		if trimmed != "" {
			return trimmed
		}
	}

	// 2. Honor DATA_DIR
	dataDir := os.Getenv("DATA_DIR")
	if dataDir == "" {
		dataDir = "data"
	}
	return filepath.Join(dataDir, "app.db")
}
