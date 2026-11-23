package core

import (
	"os"
)

// GetPort returns the port to listen on from PORT env variable (default 8080).
func GetPort() string {
	port := os.Getenv("PORT")
	if port == "" {
		return "8080"
	}
	return port
}

// GetHost returns the host to bind to from HOST env variable (default 127.0.0.1).
func GetHost() string {
	host := os.Getenv("HOST")
	if host == "" {
		return "127.0.0.1"
	}
	return host
}
