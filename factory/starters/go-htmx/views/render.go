package views

import (
	_ "embed"
	"fmt"
	"html/template"
	"net/http"
	"strings"
)

//go:embed layout.html
var LayoutHTML string

//go:embed htmx.min.js
var HtmxJS []byte

// RenderPage renders the base layout with the given title and inner HTML content.
// The inner content is injected into the {{block "content" .}} slot of layout.html.
// Optional data parameter can supply custom template context (defaults to map with Title).
func RenderPage(w http.ResponseWriter, title string, contentHTML string, data ...any) error {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")

	var ctx any
	if len(data) > 0 && data[0] != nil {
		if m, ok := data[0].(map[string]any); ok {
			if _, hasTitle := m["Title"]; !hasTitle {
				m["Title"] = title
			}
			ctx = m
		} else {
			ctx = data[0]
		}
	} else {
		ctx = map[string]any{"Title": title}
	}

	// If contentHTML defines its own {{define "content"}}, parse layout + content
	page := LayoutHTML
	if strings.Contains(contentHTML, `{{define "content"}}`) {
		tmpl, err := template.New("page").Parse(LayoutHTML)
		if err != nil {
			return fmt.Errorf("failed to parse layout template: %w", err)
		}
		if _, err := tmpl.Parse(contentHTML); err != nil {
			return fmt.Errorf("failed to parse content template: %w", err)
		}
		return tmpl.Execute(w, ctx)
	}

	// Direct content slot injection
	tmplText := strings.Replace(page, `{{block "content" .}}`, `{{block "content" .}}`+contentHTML, 1)
	tmpl, err := template.New("page").Parse(tmplText)
	if err != nil {
		return fmt.Errorf("failed to parse merged template: %w", err)
	}
	return tmpl.Execute(w, ctx)
}

// RenderFragment sends an HTML fragment response directly for HTMX swaps.
func RenderFragment(w http.ResponseWriter, fragmentHTML string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(fragmentHTML))
}

// RenderFragmentWithStatus sends an HTML fragment response with explicit HTTP status (e.g. 400 validation error).
func RenderFragmentWithStatus(w http.ResponseWriter, status int, fragmentHTML string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(fragmentHTML))
}
