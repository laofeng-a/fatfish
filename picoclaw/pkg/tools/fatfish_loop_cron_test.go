package tools

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/sipeed/picoclaw/pkg/config"
	"github.com/sipeed/picoclaw/pkg/cron"
)

type loopCronAuditTransport func(*http.Request) (*http.Response, error)

func (f loopCronAuditTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestFatfishLoopCronLineageQuietFinalAndRevocation(t *testing.T) {
	token := filepath.Join(t.TempDir(), "token")
	os.WriteFile(token, []byte("fixture"), 0600)
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", token)
	active := true
	old := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = old })
	http.DefaultTransport = loopCronAuditTransport(func(r *http.Request) (*http.Response, error) {
		raw, _ := json.Marshal(map[string]any{"allowed": true, "managed": true, "active": active, "session_key": "agent:autonomy-1"})
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(string(raw))), Header: make(http.Header)}, nil
	})
	executor := &stubJobExecutor{response: "本轮没有打扰jg"}
	tool := newTestCronToolWithExecutorAndConfig(t, executor, config.DefaultConfig())
	schedule := cron.CronSchedule{Kind: "every"}
	interval := int64(900000)
	schedule.EveryMS = &interval
	job, err := tool.cronService.AddJob("自主阅读", schedule, loopCronPrefix+"agent:autonomy-1]\n接着读", "telegram", "123")
	if err != nil {
		t.Fatal(err)
	}
	independent, err := tool.cronService.AddJob("jg独立提醒", schedule, "正常提醒", "telegram", "123")
	if err != nil {
		t.Fatal(err)
	}
	if got := tool.ExecuteJob(context.Background(), job); got != "ok" {
		t.Fatal(got)
	}
	if executor.lastKey != "agent:autonomy-1" || executor.lastPrompt != "接着读" || executor.publishedResp != "" {
		t.Fatalf("lineage or final leak: %+v", executor)
	}
	active = false
	tool.PruneRevoked(context.Background())
	if _, ok := tool.cronService.GetJob(job.ID); ok {
		t.Fatal("revoked cron still queued")
	}
	if _, ok := tool.cronService.GetJob(independent.ID); !ok {
		t.Fatal("independent reminder removed")
	}
	if got := tool.ExecuteJob(context.Background(), job); got != "revoked" {
		t.Fatal("late cron restarted", got)
	}
}
