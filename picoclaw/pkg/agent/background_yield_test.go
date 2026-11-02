package agent

import (
	"context"
	"errors"
	"os"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/sipeed/picoclaw/pkg/fatfish"
	"github.com/sipeed/picoclaw/pkg/providers"
	"github.com/sipeed/picoclaw/pkg/tools"
)

type yieldAfterModelProvider struct {
	base *toolCallRespProvider
	busy *atomic.Bool
}

func (p *yieldAfterModelProvider) GetDefaultModel() string { return p.base.GetDefaultModel() }
func (p *yieldAfterModelProvider) Chat(ctx context.Context, messages []providers.Message, defs []providers.ToolDefinition, model string, opts map[string]any) (*providers.LLMResponse, error) {
	response, err := p.base.Chat(ctx, messages, defs, model, opts)
	p.busy.Store(true)
	return response, err
}

func TestBackgroundModelBoundaryDoesNotStartNewAction(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "")
	var busy atomic.Bool
	base := &toolCallRespProvider{toolName: "audit_write", toolArgs: map[string]any{}}
	provider := &yieldAfterModelProvider{base: base, busy: &busy}
	al, instance, cleanup := newTurnCoordTestLoop(t, provider)
	defer cleanup()
	tool := &yieldAuditTool{name: "audit_write", busy: &busy}
	instance.Tools.Register(tool)
	ctx := context.WithValue(context.Background(), backgroundYieldKey{}, func() bool { return busy.Load() })
	_, err := al.runAgentLoop(ctx, instance, processOptions{Dispatch: DispatchRequest{SessionKey: "heartbeat", UserMessage: "offline"}, NoHistory: true, SendResponse: false})
	var yielded *backgroundYieldError
	if !errors.As(err, &yielded) || tool.calls != 0 || base.callCount != 1 {
		t.Fatalf("new action started while human waited: %v tool=%d model=%d", err, tool.calls, base.callCount)
	}
}

type yieldAuditTool struct {
	name      string
	busy      *atomic.Bool
	calls     int
	completed bool
}

func (t *yieldAuditTool) Name() string             { return t.name }
func (*yieldAuditTool) Description() string        { return "offline audit tool" }
func (*yieldAuditTool) Parameters() map[string]any { return map[string]any{"type": "object"} }
func (t *yieldAuditTool) Execute(ctx context.Context, _ map[string]any) *tools.ToolResult {
	t.calls++
	t.busy.Store(true) // A human message arrived while this action was executing.
	if ctx.Err() != nil {
		return &tools.ToolResult{IsError: true, ForLLM: "action was cancelled"}
	}
	t.completed = true
	return tools.NewToolResult("confirmed receipt job_id=job-123 message_id=456")
}

func TestBackgroundYieldsAfterFinishedToolWithoutReplay(t *testing.T) {
	for _, name := range []string{"audit_read", "audit_write"} {
		t.Run(name, func(t *testing.T) {
			t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "")
			provider := &toolCallRespProvider{toolName: name, toolArgs: map[string]any{}, response: "unnecessary next model call"}
			al, instance, cleanup := newTurnCoordTestLoop(t, provider)
			defer cleanup()
			var busy atomic.Bool
			tool := &yieldAuditTool{name: name, busy: &busy}
			instance.Tools.Register(tool)
			ctx := context.WithValue(context.Background(), backgroundYieldKey{}, func() bool { return busy.Load() })
			_, err := al.runAgentLoop(ctx, instance, processOptions{Dispatch: DispatchRequest{SessionKey: "heartbeat", UserMessage: "offline background task"}, NoHistory: true, SendResponse: false})
			var yielded *backgroundYieldError
			if !errors.As(err, &yielded) {
				t.Fatalf("expected a continuation, got %v", err)
			}
			if tool.calls != 1 || !tool.completed || provider.callCount != 1 {
				t.Fatalf("action/model replayed: tool=%d done=%t model=%d", tool.calls, tool.completed, provider.callCount)
			}
			body, readErr := os.ReadFile(yielded.checkpoint)
			if readErr != nil || !strings.Contains(string(body), "job-123") || !strings.Contains(string(body), "456") {
				t.Fatalf("confirmed receipts missing: %s %v", body, readErr)
			}
		})
	}
}

func TestBackgroundYieldIsNotAcknowledgedAsCompleted(t *testing.T) {
	provider := &silentHeartbeatProvider{response: "must not be called"}
	al := newSilentHeartbeatLoop(t, provider)
	key, _, _ := al.resolveSteeringTarget(codexWakeHumanMessage("123"))
	defer al.activeTurnStates.Delete(key)
	var done map[string]any
	_, err := al.processHeartbeat(context.Background(), "offline task", "telegram", "123", func(_ context.Context, action string, payload any) (fatfish.Decision, error) {
		if action == "wake" {
			al.activeTurnStates.Store(key, &turnState{turnID: "waiting-human"})
		}
		if action == "wake_done" {
			done = payload.(map[string]any)
		}
		return fatfish.Decision{Allowed: true, WakeID: "yield-test", Silent: true, Context: "offline task"}, nil
	})
	var yielded *backgroundYieldError
	if !errors.As(err, &yielded) || done == nil || done["outcome"] == "completed" {
		t.Fatalf("lost continuation: err=%v ack=%v", err, done)
	}
	if !strings.Contains(done["summary"].(string), yielded.checkpoint) {
		t.Fatalf("checkpoint not returned to scheduler: %v", done)
	}
	if provider.calls != 0 || len(al.workerSem) != 0 {
		t.Fatal("yield did not release capacity")
	}
}
