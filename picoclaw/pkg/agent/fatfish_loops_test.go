package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/providers"
	"github.com/sipeed/picoclaw/pkg/tools"
)

type loopAuditTransport func(*http.Request) (*http.Response, error)

func (f loopAuditTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

type loopProbe struct{ run func() }

func (*loopProbe) Name() string               { return "loop_probe" }
func (*loopProbe) Description() string        { return "offline task boundary probe" }
func (*loopProbe) Parameters() map[string]any { return map[string]any{"type": "object"} }
func (p *loopProbe) Execute(context.Context, map[string]any) *tools.ToolResult {
	p.run()
	return tools.NewToolResult("real fixture receipt thread_id=full-thread-id job_id=full-job-id turn_id=full-turn-id")
}

func TestFatfishLoopActualProviderGetsLatestTaskOnEveryCall(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "")
	var version atomic.Int32
	version.Store(1)
	calls := 0
	audits := 0
	provider := &reformProvider{fn: func(messages []providers.Message) (*providers.LLMResponse, error) {
		calls++
		raw, _ := json.Marshal(messages)
		text := string(raw)
		for _, want := range []string{"task_id=task-actual", fmt.Sprintf("version=%d", calls), "full canonical AGENT", "canonical SOUL", "canonical core"} {
			if !strings.Contains(text, want) {
				t.Fatalf("actual provider missing %q: %s", want, text)
			}
		}
		if strings.Contains(text, "ordinary chat summary") || strings.Contains(text, "unrelated companionship memory") {
			t.Fatal("task inherited ordinary chat")
		}
		if calls == 1 {
			return &providers.LLMResponse{ToolCalls: []providers.ToolCall{{ID: "one", Name: "loop_probe", Arguments: map[string]any{}}}, FinishReason: "tool_calls"}, nil
		}
		return &providers.LLMResponse{Content: "内部任务结论", FinishReason: "stop"}, nil
	}}
	al, agent, cleanup := newTurnCoordTestLoop(t, provider)
	defer cleanup()
	root := t.TempDir()
	os.Mkdir(filepath.Join(root, "profile"), 0700)
	for path, text := range map[string]string{"core.md": "canonical core", "profile/SOUL.md": "canonical SOUL", "profile/AGENT.md": "full canonical AGENT", "token": "fixture"} {
		if e := os.WriteFile(filepath.Join(root, path), []byte(text), 0600); e != nil {
			t.Fatal(e)
		}
	}
	t.Setenv("FATFISH_STRUCTURED_CORE_FILE", filepath.Join(root, "core.md"))
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", filepath.Join(root, "token"))
	old := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = old })
	http.DefaultTransport = loopAuditTransport(func(r *http.Request) (*http.Response, error) {
		var input map[string]any
		json.NewDecoder(r.Body).Decode(&input)
		result := map[string]any{"allowed": true}
		if strings.HasSuffix(r.URL.Path, "/loop_context") {
			result["managed"] = true
			result["active"] = true
			result["task_id"] = "task-actual"
			result["version"] = version.Load()
			result["context"] = fmt.Sprintf("[任务上下文 task_id=task-actual version=%d] goal=真实目标", version.Load())
			if input["assembled_request"] != nil {
				audits++
				raw, _ := json.Marshal(input["assembled_request"])
				if !strings.Contains(string(raw), fmt.Sprintf("version=%d", version.Load())) {
					t.Fatal("audited request stale")
				}
			}
		}
		raw, _ := json.Marshal(result)
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(string(raw))), Header: make(http.Header)}, nil
	})
	agent.Tools.Register(&loopProbe{run: func() { version.Store(2) }})
	opts := makeTestProcessOpts("agent:task-task-actual")
	opts.Dispatch = DispatchRequest{SessionKey: "agent:task-task-actual", UserMessage: "巡检任务", InboundContext: &bus.InboundContext{Channel: "telegram", ChatID: "123"}}
	opts.NoHistory = false
	agent.Sessions.SetSummary(opts.Dispatch.SessionKey, "ordinary chat summary")
	ts := newTurnState(agent, opts, turnEventScope{turnID: "task-loop-test", context: newTurnContext(nil, nil, nil)})
	_, err := al.runTurn(context.Background(), ts, NewPipeline(al))
	if err != nil {
		t.Fatal(err)
	}
	if calls != 2 || audits != 2 {
		t.Fatalf("calls=%d audits=%d", calls, audits)
	}
}

func TestFatfishDiaryInjectionOnlyExistingThreeDayPaths(t *testing.T) {
	root := t.TempDir()
	cb := NewContextBuilder(root)
	now := time.Now().In(time.FixedZone("UTC+8", 8*3600))
	for i := 0; i < 4; i++ {
		day := now.AddDate(0, 0, -i)
		path := filepath.Join(root, "memory", day.Format("200601"), day.Format("20060102")+".md")
		os.MkdirAll(filepath.Dir(path), 0700)
		os.WriteFile(path, []byte(strings.Repeat("PRIVATE_DIARY_BODY", 1000)), 0600)
	}
	text := cb.fatfishMemoryContext()
	if strings.Contains(text, "PRIVATE_DIARY_BODY") {
		t.Fatal("diary body injected")
	}
	for i := 0; i < 4; i++ {
		name := now.AddDate(0, 0, -i).Format("20060102") + ".md"
		if strings.Contains(text, name) != (i < 3) {
			t.Fatalf("wrong diary directory %s", text)
		}
	}
}

func TestFatfishLoopRevocationBetweenToolsKeepsResultsAndStopsDispatch(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "")
	provider := &reformProvider{fn: func([]providers.Message) (*providers.LLMResponse, error) {
		return &providers.LLMResponse{FinishReason: "tool_calls", ToolCalls: []providers.ToolCall{{ID: "first", Name: "loop_probe", Arguments: map[string]any{}}, {ID: "second", Name: "loop_probe", Arguments: map[string]any{}}}}, nil
	}}
	al, agent, cleanup := newTurnCoordTestLoop(t, provider)
	defer cleanup()
	token := filepath.Join(t.TempDir(), "token")
	os.WriteFile(token, []byte("fixture"), 0600)
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", token)
	var active atomic.Bool
	active.Store(true)
	old := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = old })
	http.DefaultTransport = loopAuditTransport(func(r *http.Request) (*http.Response, error) {
		raw, _ := json.Marshal(map[string]any{"allowed": true, "managed": true, "active": active.Load(), "report": true})
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(string(raw))), Header: make(http.Header)}, nil
	})
	calls := 0
	agent.Tools.Register(&loopProbe{run: func() { calls++; active.Store(false) }})
	opts := processOptions{Dispatch: DispatchRequest{SessionKey: "agent:autonomy-1", UserMessage: "继续兴趣", InboundContext: &bus.InboundContext{Channel: "telegram", ChatID: "123"}}, NoHistory: false}
	ts := newTurnState(agent, opts, turnEventScope{turnID: "boundary", context: newTurnContext(nil, nil, nil)})
	_, err := al.runTurn(context.Background(), ts, NewPipeline(al))
	if err == nil || calls != 1 {
		t.Fatalf("err=%v actions=%d", err, calls)
	}
	history := agent.Sessions.GetHistory(opts.Dispatch.SessionKey)
	results := map[string]string{}
	for _, m := range history {
		if m.Role == "tool" {
			results[m.ToolCallID] = m.Content
		}
	}
	if !strings.Contains(results["first"], "real fixture receipt") || !strings.Contains(results["second"], "未派发") {
		t.Fatalf("tool protocol or completed receipt lost: %+v", results)
	}
}
