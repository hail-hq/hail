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

// RFC 8628 § 3.5 token-endpoint error codes. Hoisted so the polling switch
// reads as a state machine instead of a wall of magic strings.
const (
	codeAuthorizationPending = "authorization_pending"
	codeSlowDown             = "slow_down"
	codeAccessDenied         = "access_denied"
	codeExpiredToken         = "expired_token"
)

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

			fmt.Fprintln(stdout, "Waiting for authorization…")
			token, err := pollForToken(ctx, authURL, code.DeviceCode, interval, expiresIn)
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

// postJSON sends a JSON-encoded body to url and decodes the response into out.
// Non-2xx responses are surfaced via decodeHTTPError. bearer is empty for
// unauthenticated calls.
func postJSON(ctx context.Context, url, bearer string, body, out any) error {
	payload, err := json.Marshal(body)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if bearer != "" {
		req.Header.Set("Authorization", "Bearer "+bearer)
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return decodeHTTPError(resp)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func requestDeviceCode(ctx context.Context, authURL string) (*deviceCodeResp, error) {
	var out deviceCodeResp
	if err := postJSON(ctx, authURL+"/api/auth/device/code", "", map[string]string{
		"client_id": deviceClientID,
	}, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

func exchangeForAPIKey(ctx context.Context, authURL, accessToken, name string) (*issueKeyResp, error) {
	var out issueKeyResp
	if err := postJSON(ctx, authURL+"/api/cli/issue-key", accessToken, map[string]string{
		"name": name,
	}, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// pollForToken loops until the user approves (returns token), denies, or the
// device code expires. The expiry is enforced by the context deadline so a
// stalled in-flight request can't push past it by `httpClient.Timeout`.
func pollForToken(ctx context.Context, authURL, deviceCode string, interval, expiresIn time.Duration) (*deviceTokenResp, error) {
	pollCtx, cancel := context.WithTimeout(ctx, expiresIn)
	defer cancel()

	body, _ := json.Marshal(map[string]string{
		"grant_type":  "urn:ietf:params:oauth:grant-type:device_code",
		"device_code": deviceCode,
		"client_id":   deviceClientID,
	})

	for {
		req, err := http.NewRequestWithContext(pollCtx, http.MethodPost, authURL+"/api/auth/device/token", bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := httpClient.Do(req)
		if err != nil {
			if errors.Is(pollCtx.Err(), context.DeadlineExceeded) {
				return nil, errors.New("login expired before authorization completed; run `hail login` again")
			}
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
		case codeAuthorizationPending:
			// Keep polling — user hasn't approved yet.
		case codeSlowDown:
			// RFC 8628 § 3.5: bump the interval by 5s, capped so a buggy
			// server can't push us past the deadline through repeated bumps.
			interval += 5 * time.Second
			if interval > time.Minute {
				interval = time.Minute
			}
		case codeAccessDenied:
			return nil, errors.New("authorization denied")
		case codeExpiredToken:
			return nil, errors.New("device code expired; run `hail login` again")
		default:
			if derr.Error != "" {
				return nil, fmt.Errorf("device token: %s — %s", derr.Error, derr.ErrorDescription)
			}
			return nil, fmt.Errorf("device token: HTTP %d", resp.StatusCode)
		}

		select {
		case <-pollCtx.Done():
			if errors.Is(pollCtx.Err(), context.DeadlineExceeded) {
				return nil, errors.New("login expired before authorization completed; run `hail login` again")
			}
			return nil, pollCtx.Err()
		case <-time.After(interval):
		}
	}
}

func decodeHTTPError(resp *http.Response) error {
	body, _ := io.ReadAll(resp.Body)
	var derr deviceErrResp
	if err := json.Unmarshal(body, &derr); err == nil && derr.Error != "" {
		if derr.ErrorDescription != "" {
			return fmt.Errorf("HTTP %d: %s — %s", resp.StatusCode, derr.Error, derr.ErrorDescription)
		}
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, derr.Error)
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
