package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v7/internal/registry"
	native "github.com/router-for-me/CLIProxyAPI/v7/internal/runtime/executor"
	coreauth "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/auth"
	"github.com/router-for-me/CLIProxyAPI/v7/sdk/config"
)

type fenjueTransport func(*http.Request) (*http.Response, error)

func (f fenjueTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func fenjueFixture(t *testing.T) (*gin.Engine, func(bool), *int) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	path := filepath.Join(t.TempDir(), "control.json")
	oldPath, oldClient := fenjueControlPath, fenjueClient
	oldBoot := fenjueBootID
	fenjueBootID = func() (string, error) { return "fixture-boot", nil }
	t.Cleanup(func() { fenjueBootID = oldBoot })
	fenjueControlPath = func() string { return path }
	t.Cleanup(func() { fenjueControlPath = oldPath; fenjueClient = oldClient })
	set := func(enabled bool) {
		data, _ := json.Marshal(fenjueControl{Schema: 1, Enabled: enabled, ClientKeySHA256: fenjueHash("fixture-key"), AuthID: "fenjue-fixture.json", AccountRef: fenjueHash("fixture-account"), GuardKey: strings.Repeat("k", 32), BootID: "fixture-boot"})
		if err := os.WriteFile(path, data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	set(false)
	m := loadAutomaticRoutingManifest(t, `{"accounts":[{"id":"pinned","authId":"fenjue-fixture.json","authKind":"oauth","chatgptAccountId":"fixture-account"}],"apiKeys":[{"key":"fixture-key","enabled":true,"accountIds":["pinned"]}],"modelIds":["gpt-6-astra"]}`)
	manager := coreauth.NewManager(nil, &coreauth.RoundRobinSelector{}, nil)
	manager.RegisterExecutor(native.NewCodexExecutor(&config.Config{}))
	_, err := manager.Register(context.Background(), &coreauth.Auth{ID: "fenjue-fixture.json", Provider: "codex", Status: coreauth.StatusActive, Attributes: map[string]string{"auth_kind": "oauth"}, Metadata: map[string]any{"access_token": "fixture-oauth", "account_id": "fixture-account"}})
	if err != nil {
		t.Fatal(err)
	}
	registry.GetGlobalRegistry().RegisterClient("fenjue-fixture.json", "codex", []*registry.ModelInfo{{ID: "gpt-6-astra", Object: "model"}})
	t.Cleanup(func() { registry.GetGlobalRegistry().UnregisterClient("fenjue-fixture.json") })
	server := &relayServer{manifest: m, authManager: manager}
	router := gin.New()
	router.Use(fenjueCapture())
	router.Use(func(c *gin.Context) {
		c.Request = c.Request.WithContext(context.WithValue(c.Request.Context(), clientAPIKeyContextKey, &m.APIKeys[0]))
		c.Next()
	})
	router.Use(server.fenjueMiddleware())
	count := new(int)
	router.POST("/v1/responses", func(c *gin.Context) { *count++; c.String(200, "ordinary") })
	router.POST("/v1/responses/compact", func(c *gin.Context) { *count++; c.String(200, "ordinary") })
	router.GET("/v1/responses", func(c *gin.Context) { *count++; c.String(200, "ordinary") })
	router.GET("/v1/fenjue/status", func(c *gin.Context) { c.Status(404) })
	return router, set, count
}

func TestFenjueToggleAndOriginalPayload(t *testing.T) {
	router, set, count := fenjueFixture(t)
	body := `{"model":"gpt-6-astra","reasoning":{"effort":"medium"},"input":[{"type":"compaction","encrypted_content":"OPAQUE"}],"tools":[{"type":"custom","name":"patch"}]}`
	calls := 0
	fenjueClient = &http.Client{Transport: fenjueTransport(func(r *http.Request) (*http.Response, error) {
		calls++
		raw, _ := io.ReadAll(r.Body)
		if string(raw) != body {
			t.Fatal("payload changed")
		}
		if r.Header.Get("X-Fenjue-OAuth") != "fixture-oauth" || r.Header.Get("X-Fenjue-Account") != "fixture-account" {
			t.Fatal("wrong identity")
		}
		if !strings.HasPrefix(r.URL.String(), fenjueEndpoint) {
			t.Fatal("wrong endpoint")
		}
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: io.NopCloser(strings.NewReader("data: {\"type\":\"response.completed\"}\n\n"))}, nil
	})}
	for _, enabled := range []bool{false, true, true, false} {
		set(enabled)
		req := httptest.NewRequest("POST", "/v1/responses", strings.NewReader(body))
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if w.Code != 200 {
			t.Fatalf("enabled=%v code=%d %s", enabled, w.Code, w.Body.String())
		}
		if enabled && w.Header().Get("X-Fenjue-Cockpit") != fenjueBuild {
			t.Fatal("missing evidence")
		}
	}
	if calls != 2 || *count != 2 || fenjueActive != 0 {
		t.Fatalf("guard=%d ordinary=%d active=%d", calls, *count, fenjueActive)
	}
}

func TestFenjueFailClosedAndNoWebsocket(t *testing.T) {
	router, set, count := fenjueFixture(t)
	set(true)
	fenjueClient = &http.Client{Transport: fenjueTransport(func(*http.Request) (*http.Response, error) { return nil, io.ErrUnexpectedEOF })}
	for _, method := range []string{"POST", "GET"} {
		w := httptest.NewRecorder()
		router.ServeHTTP(w, httptest.NewRequest(method, "/v1/responses", strings.NewReader(`{"model":"gpt-6-astra","input":"hi"}`)))
		if w.Code < 400 {
			t.Fatal("unexpected success")
		}
	}
	if *count != 0 {
		t.Fatal("fell back")
	}
	if err := os.WriteFile(fenjueControlPath(), []byte("broken"), 0600); err != nil {
		t.Fatal(err)
	}
	w := httptest.NewRecorder()
	router.ServeHTTP(w, httptest.NewRequest("POST", "/v1/responses", strings.NewReader(`{"model":"gpt-6-astra"}`)))
	if w.Code != 503 || *count != 0 {
		t.Fatal("invalid control bypassed")
	}
}

func TestFenjueDisabledStateAndStatus(t *testing.T) {
	router, _, _ := fenjueFixture(t)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, httptest.NewRequest("GET", "/v1/fenjue/status", nil))
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"enabled":false`) {
		t.Fatal(w.Body.String())
	}
	if err := os.Remove(fenjueControlPath()); err != nil {
		t.Fatal(err)
	}
	cfg, err := readFenjueControl()
	if err != nil || cfg.Enabled {
		t.Fatal("missing control must be default off")
	}
}

func TestFenjueNewBootDefaultsOff(t *testing.T) {
	_, set, _ := fenjueFixture(t)
	set(true)
	cfg, err := readFenjueControl()
	if err != nil || !cfg.Enabled {
		t.Fatal("same boot should retain takeover")
	}
	fenjueBootID = func() (string, error) { return "new-boot", nil }
	cfg, err = readFenjueControl()
	if err != nil || cfg.Enabled || !cfg.bootReset {
		t.Fatal("new boot must default off")
	}
}
