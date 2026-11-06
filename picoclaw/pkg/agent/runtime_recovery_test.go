package agent

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"

	"github.com/sipeed/picoclaw/pkg/providers"
)

type schemaRecoveryProvider struct {
	calls int
	t     *testing.T
}

func (*schemaRecoveryProvider) GetDefaultModel() string { return "fixture" }
func (p *schemaRecoveryProvider) Chat(_ context.Context, _ []providers.Message, defs []providers.ToolDefinition, _ string, _ map[string]any) (*providers.LLMResponse, error) {
	p.calls++
	found := false
	for _, d := range defs {
		if d.Function.Name == "loop_probe" {
			found = true
		}
	}
	if (p.calls <= 2) != found {
		p.t.Fatalf("call %d schema present=%v", p.calls, found)
	}
	if p.calls == 1 {
		return &providers.LLMResponse{ToolCalls: []providers.ToolCall{{ID: "probe", Name: "loop_probe", Arguments: map[string]any{}}}, FinishReason: "tool_calls"}, nil
	}
	return &providers.LLMResponse{Content: "done", FinishReason: "stop"}, nil
}

func TestRuntimeRecoverySchemaSurvivesTTLOnlyWithinTurn(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "")
	p := &schemaRecoveryProvider{t: t}
	al, agent, cleanup := newTurnCoordTestLoop(t, p)
	defer cleanup()
	agent.Tools.RegisterHidden(&loopProbe{run: func() {}})
	agent.Tools.PromoteTools([]string{"loop_probe"}, 1)
	for _, session := range []string{"first-turn", "second-turn"} {
		if _, err := al.runAgentLoop(context.Background(), agent, makeTestProcessOpts(session)); err != nil {
			t.Fatal(err)
		}
	}
	if p.calls != 3 {
		t.Fatalf("provider calls=%d", p.calls)
	}
}

func TestRuntimeRecoveryManagedTaskYieldsAfterCompletedTool(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "")
	for _, key := range []string{"agent:task-fixture", "agent:autonomy-1"} {
		t.Run(key, func(t *testing.T) {
			busy := false
			calls := 0
			provider := &reformProvider{fn: func([]providers.Message) (*providers.LLMResponse, error) {
				calls++
				return &providers.LLMResponse{ToolCalls: []providers.ToolCall{{ID: "probe", Name: "loop_probe", Arguments: map[string]any{}}}, FinishReason: "tool_calls"}, nil
			}}
			al, agent, cleanup := newTurnCoordTestLoop(t, provider)
			defer cleanup()
			agent.Tools.Register(&loopProbe{run: func() { busy = true }})
			opts := makeTestProcessOpts(key)
			opts.NoHistory = strings.HasPrefix(key, "agent:autonomy-")
			ctx := context.WithValue(context.Background(), backgroundYieldKey{}, func() bool { return busy })
			_, err := al.runAgentLoop(ctx, agent, opts)
			var yielded *backgroundYieldError
			if !errors.As(err, &yielded) {
				t.Fatalf("expected boundary yield, got %v", err)
			}
			raw, err := os.ReadFile(yielded.checkpoint)
			if err != nil {
				t.Fatal(err)
			}
			if calls != 1 || !strings.Contains(string(raw), "full-job-id") {
				t.Fatalf("tool receipt not preserved: calls=%d %s", calls, raw)
			}
		})
	}
}
