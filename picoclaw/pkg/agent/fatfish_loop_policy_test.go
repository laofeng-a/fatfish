package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/fatfish"
	"github.com/sipeed/picoclaw/pkg/providers"
)

func TestFatfishBackgroundScenesYieldToHumanBeforeTool(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "")
	for _, scene := range []struct{ name, session string }{
		{"task", "agent:task-fixture"}, {"autonomy", "agent:autonomy-1"}, {"quota", "agent:quota"},
		{"legacy heartbeat", "heartbeat"}, {"diary", "heartbeat"}, {"cron", "agent:cron-fixture"},
	} {
		t.Run(scene.name, func(t *testing.T) {
			var busy atomic.Bool
			base := &toolCallRespProvider{toolName: "audit_write", toolArgs: map[string]any{}}
			provider := &yieldAfterModelProvider{base: base, busy: &busy}
			al, instance, cleanup := newTurnCoordTestLoop(t, provider)
			defer cleanup()
			tool := &yieldAuditTool{name: "audit_write", busy: &busy}
			instance.Tools.Register(tool)
			ctx := context.WithValue(fatfish.Background(context.Background()), backgroundYieldKey{}, func() bool { return busy.Load() })
			_, err := al.runAgentLoop(ctx, instance, processOptions{Dispatch: DispatchRequest{SessionKey: scene.session, UserMessage: "offline " + scene.name}, NoHistory: true, SendResponse: false})
			var yielded *backgroundYieldError
			if !errors.As(err, &yielded) || tool.calls != 0 || base.callCount != 1 {
				t.Fatalf("human takeover did not stop the next action: %v tool=%d model=%d", err, tool.calls, base.callCount)
			}
		})
	}
}

func TestFatfishLoopPolicyBoundariesWithoutProviderProbe(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "profile"), 0o700); err != nil {
		t.Fatal(err)
	}
	for name, text := range map[string]string{"token": "fixture", "core.md": "canonical core", "profile/SOUL.md": "canonical soul", "profile/AGENT.md": "canonical rules"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", filepath.Join(root, "token"))
	t.Setenv("FATFISH_STRUCTURED_CORE_FILE", filepath.Join(root, "core.md"))
	original := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = original })
	scenes := []struct {
		name    string
		session string
		kind    string
		managed bool
		taskID  string
	}{
		{"human", "private", "", false, ""},
		{"task", "agent:task-fixture", "task", true, "fixture"},
		{"autonomy", "agent:autonomy-1", "autonomy", true, ""},
		{"quota", "agent:quota", "quota", true, ""},
		{"legacy heartbeat", "heartbeat", "ordinary", false, ""},
		{"diary", "heartbeat", "diary", false, ""},
		{"cron", "agent:cron-fixture", "cron", false, ""},
	}
	for _, scene := range scenes {
		for _, condition := range []string{"allowed", "unavailable", "revoked"} {
			t.Run(scene.name+"/"+condition, func(t *testing.T) {
				requests := 0
				http.DefaultTransport = loopAuditTransport(func(request *http.Request) (*http.Response, error) {
					requests++
					if !strings.HasSuffix(request.URL.Path, "/loop_context") {
						t.Fatal("unexpected policy request", request.URL.Path)
					}
					if condition == "unavailable" {
						return nil, fmt.Errorf("fixture runtime unavailable")
					}
					payload := map[string]any{"allowed": true, "managed": scene.managed, "active": condition != "revoked", "report": true, "task_id": scene.taskID, "wake_kind": scene.kind, "context": "current fixture context"}
					raw, _ := json.Marshal(payload)
					return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(string(raw))), Header: make(http.Header)}, nil
				})
				ts := &turnState{sessionKey: scene.session, channel: "telegram", workspace: root,
					opts: processOptions{Dispatch: DispatchRequest{SessionKey: scene.session, InboundContext: &bus.InboundContext{Channel: "telegram", ChatID: "123", MessageID: "42"}}}}
				exec := &turnExecution{callMessages: []providers.Message{{Role: "user", Content: "fixture request"}}}
				err := prepareFatfishLoopRequest(context.Background(), ts, exec)
				strictOnFailure := strings.HasPrefix(scene.session, "agent:task-") || strings.HasPrefix(scene.session, "agent:autonomy-")
				blocked := condition == "revoked" && scene.managed || condition == "unavailable" && strictOnFailure
				if (err != nil) != blocked {
					t.Fatal("existing model-boundary policy changed", err, blocked)
				}
				toolAllowed := fatfishLoopToolAllowed(context.Background(), ts, "read_file", nil)
				if toolAllowed == blocked || requests != 2 {
					t.Fatal("model or tool boundary was bypassed", toolAllowed, blocked, requests)
				}
			})
		}
	}
}
