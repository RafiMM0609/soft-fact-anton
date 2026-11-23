package core

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"html"
	"strconv"
)

// RandomID generates a cryptographically secure random alphanumeric hex ID.
func RandomID(prefix string) string {
	bytes := make([]byte, 8)
	_, _ = rand.Read(bytes)
	res := hex.EncodeToString(bytes)
	if prefix != "" {
		return prefix + "-" + res
	}
	return res
}

// Hash returns SHA-256 hex string of input text.
func Hash(text string) string {
	h := sha256.Sum256([]byte(text))
	return hex.EncodeToString(h[:])
}

// ParseInt safely parses integer string with a fallback value.
func ParseInt(s string, fallback int64) int64 {
	val, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return fallback
	}
	return val
}

// EscapeHTML escapes HTML special characters for content position and attribute position.
func EscapeHTML(s string) string {
	return html.EscapeString(s)
}
