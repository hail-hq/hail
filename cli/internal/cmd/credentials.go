package cmd

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// Credentials is the JSON payload persisted at ~/.hail/credentials.json by
// `hail login`. The API key is the only mandatory field; api_url + base_url
// are baked in so that subsequent `hail` invocations resolve to the same
// backend the user authenticated against without forcing them to set env vars.
type Credentials struct {
	APIKey  string `json:"api_key"`
	APIURL  string `json:"api_url,omitempty"`
	BaseURL string `json:"base_url,omitempty"`
}

func credentialsPath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("locate home dir: %w", err)
	}
	return filepath.Join(home, ".hail", "credentials.json"), nil
}

// loadCredentials returns (nil, nil) when the file is missing — a fresh install
// is a normal state, not an error. Any other read/parse failure surfaces.
func loadCredentials() (*Credentials, error) {
	p, err := credentialsPath()
	if err != nil {
		return nil, err
	}
	b, err := os.ReadFile(p)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, fmt.Errorf("read %s: %w", p, err)
	}
	var c Credentials
	if err := json.Unmarshal(b, &c); err != nil {
		return nil, fmt.Errorf("parse %s: %w", p, err)
	}
	return &c, nil
}

// saveCredentials writes the file at 0600 inside ~/.hail (0700) so the API key
// is not world-readable on shared hosts.
func saveCredentials(c Credentials) (string, error) {
	p, err := credentialsPath()
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		return "", fmt.Errorf("create %s: %w", filepath.Dir(p), err)
	}
	b, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return "", err
	}
	if err := os.WriteFile(p, b, 0o600); err != nil {
		return "", fmt.Errorf("write %s: %w", p, err)
	}
	return p, nil
}
