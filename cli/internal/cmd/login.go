package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

// DefaultAuthURL is the dev-default for the website hosting Better Auth.
const DefaultAuthURL = "http://localhost:3000"

// deviceClientID identifies this CLI to the device-authorization plugin.
// Free-form for now (no validateClient on the server) but kept stable so
// audit logs / future client allowlists remain coherent.
const deviceClientID = "hail-cli"

// httpClient bounds individual auth requests. Without a timeout, a server
// that accepts the connection and stalls mid-response would hang the CLI
// indefinitely (ctx is only cancelled on Ctrl-C).
var httpClient = &http.Client{Timeout: 30 * time.Second}

type deviceCodeResp struct {
	DeviceCode              string `json:"device_code"`
	UserCode                string `json:"user_code"`
	VerificationURI         string `json:"verification_uri"`
	VerificationURIComplete string `json:"verification_uri_complete"`
	ExpiresIn               int    `json:"expires_in"`
	Interval                int    `json:"interval"`
}

type deviceTokenResp struct {
	AccessToken string `json:"access_token"`
	TokenType   string `json:"token_type"`
	ExpiresIn   int    `json:"expires_in"`
	Scope       string `json:"scope"`
}

type deviceErrResp struct {
	Error            string `json:"error"`
	ErrorDescription string `json:"error_description"`
}

type issueKeyResp struct {
	ID     string `json:"id"`
	Key    string `json:"key"`
	Name   string `json:"name"`
	Prefix string `json:"prefix"`
}

func newLoginCmd(opts *Options) *cobra.Command {
	var authURLFlag string

	cmd := &cobra.Command{
		Use:   "login",
		Short: "Authenticate with Hail and save an API key to ~/.hail/credentials.json",
		Long: `Run the OAuth 2.0 device-authorization flow against the Hail website,
exchange the resulting session for a long-lived API key, and persist that key
locally so subsequent commands authenticate automatically.

Resolution order for the auth URL:
  --auth-url flag > $HAIL_AUTH_URL > ` + DefaultAuthURL,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()

			authURL := authURLFlag
			if authURL == "" {
				authURL = os.Getenv("HAIL_AUTH_URL")
			}
			if authURL == "" {
				authURL = DefaultAuthURL
			}
			authURL = strings.TrimRight(authURL, "/")

			stdout := opts.Stdout
			stderr := opts.Stderr

			code, err := requestDeviceCode(ctx, authURL)
			if err != nil {
				return fmt.Errorf("request device code: %w", err)
			}

			verifyURL := code.VerificationURIComplete
			if verifyURL == "" {
				verifyURL = code.VerificationURI
			}
			// The plugin returns the configured `verificationUri` as a path;
			// resolve it against the auth host so the printed URL is clickable.
			if verifyURL != "" && !strings.HasPrefix(verifyURL, "http://") && !strings.HasPrefix(verifyURL, "https://") {
				verifyURL = authURL + verifyURL
			}

			fmt.Fprintln(stdout, "Open this URL in your browser to authorize the CLI:")
			fmt.Fprintf(stdout, "  %s\n\n", verifyURL)
			fmt.Fprintf(stdout, "Confirm the code matches: %s\n\n", code.UserCode)

			if err := openBrowser(verifyURL); err != nil {
				fmt.Fprintln(stderr, "(could not open browser automatically:", err.Error()+")")
			}

			interval := time.Duration(code.Interval) * time.Second
			if interval < time.Second {
				interval = 5 * time.Second
			}
			expiresIn := time.Duration(code.ExpiresIn) * time.Second
			if expiresIn < time.Minute {
				expiresIn = 30 * time.Minute
			}
			deadline := time.Now().Add(expiresIn)

			fmt.Fprintln(stdout, "Waiting for authorization…")
			token, err := pollForToken(ctx, authURL, code.DeviceCode, interval, deadline)
			if err != nil {
				return err
			}

			host, _ := os.Hostname()
			today := time.Now().UTC().Format("2006-01-02")
			keyName := fmt.Sprintf("hail-cli · %s · %s", host, today)

			apiKey, err := exchangeForAPIKey(ctx, authURL, token.AccessToken, keyName)
			if err != nil {
				return fmt.Errorf("issue API key: %w", err)
			}

			path, err := saveCredentials(Credentials{
				APIKey:  apiKey.Key,
				APIURL:  opts.APIURL,
				AuthURL: authURL,
			})
			if err != nil {
				return fmt.Errorf("save credentials: %w", err)
			}

			fmt.Fprintf(stdout, "\n● Logged in. Saved %s API key to %s\n", apiKey.Prefix, path)
			return nil
		},
	}

	cmd.Flags().StringVar(&authURLFlag, "auth-url", "", "Auth server base URL (default: $HAIL_AUTH_URL or "+DefaultAuthURL+")")
	return cmd
}

func requestDeviceCode(ctx context.Context, authURL string) (*deviceCodeResp, error) {
	body, _ := json.Marshal(map[string]string{"client_id": deviceClientID})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, authURL+"/api/auth/device/code", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, decodeHTTPError(resp)
	}
	var out deviceCodeResp
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("decode device code response: %w", err)
	}
	return &out, nil
}

func pollForToken(ctx context.Context, authURL, deviceCode string, interval time.Duration, deadline time.Time) (*deviceTokenResp, error) {
	body, _ := json.Marshal(map[string]string{
		"grant_type":  "urn:ietf:params:oauth:grant-type:device_code",
		"device_code": deviceCode,
		"client_id":   deviceClientID,
	})

	for {
		if time.Now().After(deadline) {
			return nil, errors.New("login expired before authorization completed; run `hail login` again")
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodPost, authURL+"/api/auth/device/token", bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := httpClient.Do(req)
		if err != nil {
			return nil, err
		}

		if resp.StatusCode == http.StatusOK {
			var out deviceTokenResp
			err := json.NewDecoder(resp.Body).Decode(&out)
			resp.Body.Close()
			if err != nil {
				return nil, fmt.Errorf("decode device token response: %w", err)
			}
			return &out, nil
		}

		var derr deviceErrResp
		_ = json.NewDecoder(resp.Body).Decode(&derr)
		resp.Body.Close()

		switch derr.Error {
		case "authorization_pending":
		case "slow_down":
			// RFC 8628 § 3.5: bump the interval by 5s when the server tells us
			// we're polling too quickly, then continue.
			interval += 5 * time.Second
		case "access_denied":
			return nil, errors.New("authorization denied")
		case "expired_token":
			return nil, errors.New("device code expired; run `hail login` again")
		default:
			if derr.Error != "" {
				return nil, fmt.Errorf("device token: %s — %s", derr.Error, derr.ErrorDescription)
			}
			return nil, fmt.Errorf("device token: HTTP %d", resp.StatusCode)
		}

		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(interval):
		}
	}
}

func exchangeForAPIKey(ctx context.Context, authURL, accessToken, name string) (*issueKeyResp, error) {
	body, _ := json.Marshal(map[string]string{"name": name})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, authURL+"/api/cli/issue-key", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+accessToken)

	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, decodeHTTPError(resp)
	}
	var out issueKeyResp
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("decode issue-key response: %w", err)
	}
	return &out, nil
}

func decodeHTTPError(resp *http.Response) error {
	body, _ := io.ReadAll(resp.Body)
	var derr deviceErrResp
	if err := json.Unmarshal(body, &derr); err == nil && derr.Error != "" {
		return fmt.Errorf("HTTP %d: %s — %s", resp.StatusCode, derr.Error, derr.ErrorDescription)
	}
	// Some endpoints (e.g. /api/cli/issue-key) return {"error": "..."} on 4xx/5xx.
	var generic struct {
		Error string `json:"error"`
	}
	if err := json.Unmarshal(body, &generic); err == nil && generic.Error != "" {
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, generic.Error)
	}
	return fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
}

func openBrowser(url string) error {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", url)
	case "linux":
		cmd = exec.Command("xdg-open", url)
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	default:
		return fmt.Errorf("unsupported platform: %s", runtime.GOOS)
	}
	return cmd.Start()
}
