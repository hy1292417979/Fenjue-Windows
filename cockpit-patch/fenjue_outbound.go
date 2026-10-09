package main

// Fenjue extension for Cockpit v1.3.66. Only HTTP Responses for one local key.
// Control lives next to the binary, outside Cockpit's generated configuration.
import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	coreauth "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/auth"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/executor"
	coreusage "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/usage"
	sdktranslator "github.com/router-for-me/CLIProxyAPI/v7/sdk/translator"
	"github.com/tidwall/gjson"
)

const fenjueBuild = "fenjue-outbound-1"
const fenjueEndpoint = "http://127.0.0.1:17878/cockpit/v1/responses"

var fenjueMu sync.Mutex
var fenjueActive int
var fenjueControlPath = func() string {
	p, _ := os.Executable()
	return filepath.Join(filepath.Dir(p), "fenjue-guard", "control.local.json")
}
var fenjueClient = &http.Client{Transport: &http.Transport{Proxy: nil, ResponseHeaderTimeout: 320 * time.Second}, Timeout: 360 * time.Second,
	CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}

type fenjueControl struct {
	Schema          int    `json:"schema"`
	Enabled         bool   `json:"enabled"`
	ClientKeySHA256 string `json:"client_key_sha256"`
	AuthID          string `json:"auth_id"`
	AccountRef      string `json:"account_ref"`
	GuardKey        string `json:"guard_key"`
	ClientKeyID     string `json:"client_key_id"`
	BootID          string `json:"boot_id"`
	bootReset       bool
}

func readFenjueControl() (fenjueControl, error) {
	var cfg fenjueControl
	raw, err := os.ReadFile(fenjueControlPath())
	if os.IsNotExist(err) {
		return cfg, nil
	}
	if err != nil {
		return cfg, errors.New("Fenjue control unreadable; refusing unprotected requests")
	}
	if len(raw) > 8192 || json.Unmarshal(raw, &cfg) != nil || cfg.Schema != 1 {
		return cfg, errors.New("Fenjue control invalid")
	}
	if cfg.Enabled && (len(cfg.ClientKeySHA256) != 64 || len(cfg.AccountRef) != 64 || cfg.AuthID == "" || len(cfg.GuardKey) < 24) {
		return cfg, errors.New("Fenjue binding incomplete")
	}
	if cfg.Enabled {
		boot, err := fenjueBootID()
		if err != nil || cfg.BootID == "" {
			return cfg, errors.New("Fenjue boot binding unavailable; restart Fenjue")
		}
		if boot != cfg.BootID {
			cfg.Enabled = false
			cfg.bootReset = true
		}
	}
	return cfg, nil
}

func fenjueHash(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}
func fenjueResponsePath(path string) bool {
	return path == "/v1/fenjue/probe" || path == "/v1/responses" || path == "/v1/responses/compact" || path == "/v1/chat/completions/v1/responses" || path == "/v1/chat/completions/v1/responses/compact"
}

// Capture before Cockpit's policy rewrite, but authenticate and enforce policy
// before this copy is ever used. No raw body is persisted or logged here.
func fenjueCapture() gin.HandlerFunc {
	return func(c *gin.Context) {
		if c.Request.Method == http.MethodPost && fenjueResponsePath(c.Request.URL.Path) {
			c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 32*1024*1024)
			raw, err := readAndRestoreBody(c.Request)
			if err != nil {
				c.AbortWithStatus(413)
				return
			}
			c.Set("fenjue_original", raw)
		}
		c.Next()
	}
}

func (s *relayServer) fenjueMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		path := c.Request.URL.Path
		if path != "/v1/fenjue/status" && !fenjueResponsePath(path) {
			c.Next()
			return
		}
		spec, ok := s.requireAPIKey(c)
		if !ok {
			return
		}
		fenjueMu.Lock()
		cfg, err := readFenjueControl()
		if path == "/v1/fenjue/status" {
			active := fenjueActive
			fenjueMu.Unlock()
			if err != nil {
				c.AbortWithStatusJSON(503, gin.H{"build": fenjueBuild, "error": "invalid_control", "active": active})
				return
			}
			c.AbortWithStatusJSON(200, gin.H{"build": fenjueBuild, "enabled": cfg.Enabled, "key_matches": cfg.ClientKeySHA256 == fenjueHash(spec.Key), "active": active, "quality_gain_verified": false})
			return
		}
		if err != nil {
			fenjueMu.Unlock()
			writeAPIError(c, 503, err.Error(), "fenjue_control_invalid")
			c.Abort()
			return
		}
		if !cfg.Enabled || (cfg.ClientKeySHA256 != fenjueHash(spec.Key) && (cfg.ClientKeyID == "" || cfg.ClientKeyID != spec.ID)) {
			fenjueMu.Unlock()
			c.Next()
			return
		}
		if cfg.ClientKeySHA256 != fenjueHash(spec.Key) {
			fenjueMu.Unlock()
			writeAPIError(c, 503, "Cockpit key rotated; restart Fenjue to rebind", "fenjue_key_rotated")
			c.Abort()
			return
		}
		if c.Request.Method != http.MethodPost {
			fenjueMu.Unlock()
			writeAPIError(c, 409, "Fenjue protects HTTP Responses only; disable WebSockets", "fenjue_transport_unsupported")
			c.Abort()
			return
		}
		raw, _ := c.Get("fenjue_original")
		body, _ := raw.([]byte)
		model := requestBodyModel(body)
		// Non-GPT providers remain owned by Cockpit. Never map them to GPT.
		if !strings.HasPrefix(model, "gpt-") && !strings.HasPrefix(model, "codex") {
			fenjueMu.Unlock()
			c.Next()
			return
		}
		fenjueActive++
		fenjueMu.Unlock()
		defer func() { fenjueMu.Lock(); fenjueActive--; fenjueMu.Unlock() }()
		c.Abort()
		current, _ := readAndRestoreBody(c.Request)
		if model != requestBodyModel(current) || spec.ModelPrefix != "" || spec.ProviderGateway != nil || !routedClientModelAllowed(s.manifest, spec, model) {
			writeAPIError(c, 409, "Fenjue refuses model remapping or a disallowed model", "fenjue_policy_mismatch")
			return
		}
		if s.authManager == nil {
			writeAPIError(c, 503, "Cockpit account manager unavailable", "fenjue_no_account")
			return
		}
		account := s.manifest.accountByAuthID[strings.ToLower(cfg.AuthID)]
		allowed := false
		if account != nil {
			for _, id := range spec.AccountIDs {
				if id == account.ID {
					allowed = true
				}
			}
		}
		if !allowed {
			writeAPIError(c, 403, "Pinned account is outside this Cockpit API key scope", "fenjue_scope_denied")
			return
		}
		_, opts := buildExecutorRequest(c, body, model, sdktranslator.FormatOpenAIResponse, "", false)
		if opts.Metadata == nil {
			opts.Metadata = map[string]any{}
		}
		opts.Metadata[cliproxyexecutor.PinnedAuthMetadataKey] = cfg.AuthID
		auth, err := s.authManager.SelectAuthByKind(relayContext(c), "codex", model, coreauth.AuthKindOAuth, opts)
		if err != nil || auth == nil || !strings.EqualFold(auth.ID, cfg.AuthID) || fenjueHash(codexAuthChatGPTAccountID(auth)) != cfg.AccountRef {
			writeAPIError(c, 503, "Pinned Cockpit GPT account unavailable; no account fallback", "fenjue_account_unavailable")
			return
		}
		token, _ := auth.Metadata["access_token"].(string)
		if token == "" {
			writeAPIError(c, 503, "Cockpit OAuth credential unavailable", "fenjue_no_credential")
			return
		}
		started := time.Now()
		var observed bytes.Buffer
		completed := false
		responseStatus := 502
		defer func() {
			if path == "/v1/fenjue/probe" {
				return
			}
			result := gjson.ParseBytes(observed.Bytes())
			if strings.Contains(observed.String(), "data:") {
				for _, line := range strings.Split(observed.String(), "\n") {
					if strings.HasPrefix(line, "data:") {
						event := gjson.Parse(strings.TrimSpace(strings.TrimPrefix(line, "data:")))
						if event.Get("type").String() == "response.completed" {
							result = event.Get("response")
							completed = true
						}
					}
				}
			} else {
				completed = result.Get("status").String() == "completed" || result.Get("object").String() == "response.compaction"
			}
			failed := !completed || responseStatus != 200
			record := coreusage.Record{Provider: "codex", ExecutorType: fenjueBuild, Model: model, APIKey: spec.Key, AuthID: auth.ID, AuthType: "oauth", RequestedAt: started, Latency: time.Since(started), Stream: gjson.GetBytes(body, "stream").Bool(), Failed: failed, ReasoningEffort: gjson.GetBytes(body, "reasoning.effort").String(), ResponseServiceTier: result.Get("service_tier").String()}
			if record.ReasoningEffort == "" {
				record.ReasoningEffort = "max"
			}
			record.Detail = coreusage.Detail{InputTokens: result.Get("usage.input_tokens").Int(), OutputTokens: result.Get("usage.output_tokens").Int(), TotalTokens: result.Get("usage.total_tokens").Int(), CachedTokens: result.Get("usage.input_tokens_details.cached_tokens").Int(), ReasoningTokens: result.Get("usage.output_tokens_details.reasoning_tokens").Int()}
			if failed {
				record.Fail = coreusage.Failure{StatusCode: responseStatus, Body: "Fenjue rejected or incomplete; no fallback"}
			}
			coreusage.PublishRecord(relayContext(c), record)
		}()
		endpoint := fenjueEndpoint
		if path == "/v1/fenjue/probe" {
			endpoint = "http://127.0.0.1:17878/cockpit/probe"
		}
		if strings.HasSuffix(path, "/compact") {
			endpoint += "/compact"
		}
		// Exactly one submission. Original bytes, explicit selected identity; never
		// allow redirects to move the OAuth credential off the fixed loopback URL.
		req, err := http.NewRequestWithContext(c.Request.Context(), http.MethodPost, endpoint, bytes.NewReader(body))
		if err != nil {
			writeAPIError(c, 500, "Fenjue request construction failed", "fenjue_request_failed")
			return
		}
		for _, key := range []string{"Session_id", "Conversation_id", "X-Codex-Turn-State", "X-Client-Request-Id", "OpenAI-Beta", "Originator", "User-Agent", "X-Codex-Beta-Features"} {
			if value := c.Request.Header.Get(key); value != "" {
				req.Header.Set(key, value)
			}
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+cfg.GuardKey)
		req.Header.Set("X-Fenjue-OAuth", token)
		req.Header.Set("X-Fenjue-Account", codexAuthChatGPTAccountID(auth))
		req.Header.Set("X-Fenjue-Auth-ID", auth.ID)
		resp, err := fenjueClient.Do(req)
		if err != nil {
			writeAPIError(c, 502, "Fenjue unavailable; no direct fallback was attempted", "fenjue_unavailable")
			return
		}
		defer resp.Body.Close()
		responseStatus = resp.StatusCode
		for _, key := range []string{"Content-Type", "X-Codex-Turn-State", "X-Request-ID", "X-Fenjue-Route", "X-Fenjue-Delivery"} {
			if value := resp.Header.Get(key); value != "" {
				c.Header(key, value)
			}
		}
		c.Header("X-Fenjue-Cockpit", fenjueBuild)
		c.Header("Cache-Control", "no-store")
		c.Status(resp.StatusCode)
		buffer := make([]byte, 32*1024)
		for {
			n, readErr := resp.Body.Read(buffer)
			if n > 0 {
				if observed.Len()+n <= 32*1024*1024 {
					observed.Write(buffer[:n])
				}
				if _, err = c.Writer.Write(buffer[:n]); err != nil {
					return
				}
				c.Writer.Flush()
			}
			if readErr != nil {
				if readErr != io.EOF {
					responseStatus = 502
				}
				return
			}
		}
	}
}
