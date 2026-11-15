package openai_compat

import (
	"context"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/sipeed/picoclaw/pkg/fatfish"
)

func TestFatfishBackgroundChatDoesNotRequireRuntimeProbe(t *testing.T) {
	// Runtime availability belongs to the harness policy boundaries. A missing
	// runtime token must not turn the provider's old constant-allow probe into a failure.
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", filepath.Join(t.TempDir(), "missing-runtime-token"))
	for _, background := range []bool{false, true} {
		calls := 0
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls++
			if r.URL.Path != "/chat/completions" {
				t.Error("unexpected model request path", r.URL.Path)
			}
			if (r.Header.Get("X-Fatfish-Activity") == "background") != background {
				t.Error("activity accounting header changed")
			}
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`))
		}))
		p := NewProvider("fixture", server.URL, "")
		ctx := context.Background()
		if background {
			ctx = fatfish.Background(ctx)
		}
		response, err := p.Chat(ctx, []Message{{Role: "user", Content: "fixture"}}, nil, "fixture", nil)
		server.Close()
		if err != nil || response == nil || response.Content != "ok" || calls != 1 {
			t.Fatal("model call depended on the retired runtime probe", response, err, calls)
		}
	}
}
