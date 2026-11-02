package agent

import (
	"context"
	"fmt"
	"testing"

	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/fatfish"
	"github.com/sipeed/picoclaw/pkg/providers"
	"github.com/sipeed/picoclaw/pkg/tools"
)

type toolLoopProgressProbe struct{ calls int }

func (p *toolLoopProgressProbe) Name() string               { return "progress_probe" }
func (p *toolLoopProgressProbe) Description() string        { return "Advance one independent probe step." }
func (p *toolLoopProgressProbe) Parameters() map[string]any { return map[string]any{"type": "object"} }
func (p *toolLoopProgressProbe) Execute(context.Context, map[string]any) *tools.ToolResult {
	p.calls++
	return &tools.ToolResult{ForLLM: fmt.Sprintf("step %d completed", p.calls)}
}

func TestFatfishUnlimitedToolLoopCompletesBeyondTwelveRounds(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "")
	p := &sequenceProvider{}
	for i := 0; i < 18; i++ {
		p.responses = append(p.responses, &providers.LLMResponse{ToolCalls: []providers.ToolCall{{ID: fmt.Sprintf("step-%d", i), Name: "progress_probe", Arguments: map[string]any{}}}, FinishReason: "tool_calls"})
	}
	p.responses = append(p.responses, &providers.LLMResponse{Content: "all eighteen steps completed", FinishReason: "stop"})
	al, agent, cleanup := newTurnCoordTestLoop(t, p)
	defer cleanup()
	agent.MaxIterations = -1
	probe := &toolLoopProgressProbe{}
	agent.Tools.Register(probe)
	ts := newTurnState(agent, makeTestProcessOpts("unlimited-progress"), turnEventScope{turnID: "unlimited-progress", context: newTurnContext(nil, nil, nil)})
	result, err := al.runTurn(context.Background(), ts, NewPipeline(al))
	if err != nil || probe.calls != 18 || result.finalContent != "all eighteen steps completed" {
		t.Fatalf("unbounded progress lost: calls=%d final=%q err=%v", probe.calls, result.finalContent, err)
	}
}

func TestFatfishSilentQueryReceiptRunsWithoutAutomaticChatOutput(t *testing.T) {
	p := &codexWakeProvider{}
	al := newCodexWakeTestLoop(t, p)
	// Exercise the deployed Fatfish routing branch without contacting its broker.
	// newCodexWakeTestLoop disables that branch for its generic lifecycle tests.
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", t.TempDir()+"/missing-test-credential")
	event := codexWakeEvent()
	event.Kind = "query"
	event.Silent = true
	event.ThreadID = "test-native-thread"
	al.processFatfishCodexEvent(context.Background(), "123", event, func(context.Context, string, any) (fatfish.Decision, error) {
		return fatfish.Decision{Allowed: true, Accepted: true}, nil
	})
	if p.calls != 1 || !p.sawEvent {
		t.Fatal("query result did not reach the model")
	}
	select {
	case <-al.bus.(*bus.MessageBus).OutboundChan():
		t.Fatal("silent query was sent as a chat message")
	default:
	}
}
