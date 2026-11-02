package agent

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/config"
	"github.com/sipeed/picoclaw/pkg/fatfish"
	"github.com/sipeed/picoclaw/pkg/providers"
)

type codexWakeProvider struct {
	calls     int
	sawEvent  bool
	err       error
	panicCall bool
}

func (*codexWakeProvider) GetDefaultModel() string { return "test" }
func (p *codexWakeProvider) Chat(ctx context.Context, messages []providers.Message, _ []providers.ToolDefinition, _ string, _ map[string]any) (*providers.LLMResponse, error) {
	p.calls++
	for _, msg := range messages {
		if strings.Contains(msg.Content, "REAL_CODEX_FINAL") {
			p.sawEvent = fatfish.CodexToolContext(ctx, "codex-event:"+strings.Repeat("a", 24)) != nil
		}
	}
	if p.panicCall {
		panic("provider panic")
	}
	if p.err != nil {
		return nil, p.err
	}
	return &providers.LLMResponse{Content: "已处理协作结果"}, nil
}

func newCodexWakeTestLoop(t *testing.T, p *codexWakeProvider) *AgentLoop {
	t.Helper()
	// This test supplies its own broker, so unrelated runtime calls stay off.
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "")
	cfg := &config.Config{Agents: config.AgentsConfig{Defaults: config.AgentDefaults{Workspace: t.TempDir(), ModelName: "test", MaxTokens: 4096, MaxToolIterations: 3}}, Session: config.DefaultConfig().Session}
	al := NewAgentLoop(cfg, bus.NewMessageBus(), p)
	t.Cleanup(al.Close)
	return al
}

func codexWakeEvent() fatfish.CodexEvent {
	return fatfish.CodexEvent{ID: strings.Repeat("a", 24), Claim: strings.Repeat("b", 24), ChatID: "123", ChainID: "chain", JobID: "job", TurnID: "turn", TurnStatus: "completed", Content: "REAL_CODEX_FINAL"}
}

func codexWakeHumanMessage(owner string) bus.InboundMessage {
	return bus.NormalizeInboundMessage(bus.InboundMessage{
		Context: bus.InboundContext{Channel: "telegram", ChatID: owner, ChatType: "direct", SenderID: owner, MessageID: "42"},
		Sender:  bus.SenderInfo{Platform: "telegram", PlatformID: owner, DisplayName: "jg"},
		Content: "继续刚才的任务",
	})
}

func TestCodexEventUsesActualHumanSessionAcrossPoliciesAndDispatch(t *testing.T) {
	policies := []struct {
		name       string
		dimensions []string
	}{
		{"default", config.DefaultConfig().Session.Dimensions},
		{"sender", []string{"sender"}},
		{"chat_sender", []string{"chat", "sender"}},
	}
	for _, policy := range policies {
		for _, dispatch := range []bool{false, true} {
			name := policy.name
			if dispatch {
				name += "/owner_dispatch"
			}
			t.Run(name, func(t *testing.T) {
				al := newCodexWakeTestLoop(t, &codexWakeProvider{})
				cfg := al.GetConfig()
				cfg.Session.Dimensions = policy.dimensions
				if dispatch {
					cfg.Agents.Dispatch = &config.DispatchConfig{Rules: []config.DispatchRule{{Name: "owner", Agent: "main", When: config.DispatchSelector{Channel: "telegram", Sender: "123"}, SessionDimensions: policy.dimensions}}}
				}
				human := codexWakeHumanMessage("123")
				event := fatfishCodexInbound("123", codexWakeEvent())
				humanKey, humanAgent, humanOK := al.resolveSteeringTarget(human)
				eventKey, eventAgent, eventOK := al.resolveSteeringTarget(event)
				if !humanOK || !eventOK || humanKey != eventKey || humanAgent != eventAgent {
					t.Fatalf("event forked the owner's session: human=(%s,%s) event=(%s,%s)", humanKey, humanAgent, eventKey, eventAgent)
				}
				if dispatch {
					route, _, _ := al.resolveMessageRoute(event)
					if route.MatchedBy != "dispatch.rule:owner" {
						t.Fatalf("event missed owner dispatch: %s", route.MatchedBy)
					}
				}
				if event.Sender.DisplayName != "Codex 协作回执" || event.Sender.PlatformID != "codex" || event.Context.Raw["source_kind"] != "codex_event" || !strings.Contains(event.Content, "[Codex协作回执]") {
					t.Fatal("session reuse lost the distinct Codex source")
				}
			})
		}
	}
}

func TestCodexEventStartsActualModelAndAcknowledgesAfterItReturns(t *testing.T) {
	p := &codexWakeProvider{}
	al := newCodexWakeTestLoop(t, p)
	var actions []string
	call := func(_ context.Context, action string, payload any) (fatfish.Decision, error) {
		actions = append(actions, action)
		if action == "codex_event_start" && p.calls != 0 {
			t.Fatal("model ran before durable start")
		}
		if action == "codex_event_ack" {
			if p.calls != 1 || payload.(map[string]any)["outcome"] != "completed" {
				t.Fatal("ack before successful model execution")
			}
		}
		return fatfish.Decision{Allowed: true, Accepted: true}, nil
	}
	al.processFatfishCodexEvent(context.Background(), "123", codexWakeEvent(), call)
	if !p.sawEvent || p.calls != 1 {
		t.Fatalf("receipt did not reach actual model with trusted context: %+v", p)
	}
	if !reflect.DeepEqual(actions, []string{"codex_event_start", "codex_event_ack"}) {
		t.Fatalf("lifecycle: %v", actions)
	}
}

func TestCodexEventBusyHumanTurnIsReleasedWithoutSteering(t *testing.T) {
	for _, dimensions := range [][]string{{"chat"}, {"sender"}, {"chat", "sender"}} {
		t.Run(strings.Join(dimensions, "_"), func(t *testing.T) {
			p := &codexWakeProvider{}
			al := newCodexWakeTestLoop(t, p)
			al.GetConfig().Session.Dimensions = dimensions
			event := codexWakeEvent()
			msg := codexWakeHumanMessage("123")
			key, _, _ := al.resolveSteeringTarget(msg)
			active := &turnState{turnID: "human-turn"}
			al.activeTurnStates.Store(key, active)
			var actions []string
			al.processFatfishCodexEvent(context.Background(), "123", event, func(_ context.Context, action string, _ any) (fatfish.Decision, error) {
				actions = append(actions, action)
				return fatfish.Decision{Allowed: true, Accepted: true}, nil
			})
			if p.calls != 0 || !reflect.DeepEqual(actions, []string{"codex_event_release"}) || al.getActiveTurnState(key) != active || al.pendingSteeringCountForScope(key) != 0 {
				t.Fatalf("Codex event interrupted busy human turn: actions=%v calls=%d", actions, p.calls)
			}
			al.activeTurnStates.Delete(key)
		})
	}
}

func TestCodexEventUncertainStartDoesNotRunOrRelease(t *testing.T) {
	p := &codexWakeProvider{}
	al := newCodexWakeTestLoop(t, p)
	var actions []string
	al.processFatfishCodexEvent(context.Background(), "123", codexWakeEvent(), func(_ context.Context, action string, _ any) (fatfish.Decision, error) {
		actions = append(actions, action)
		return fatfish.Decision{}, errors.New("start response lost")
	})
	if p.calls != 0 || !reflect.DeepEqual(actions, []string{"codex_event_start"}) {
		t.Fatalf("uncertain start replayed: %v", actions)
	}
}

func TestCodexEventDuplicateDurableStartCannotRunModelAgain(t *testing.T) {
	p := &codexWakeProvider{}
	al := newCodexWakeTestLoop(t, p)
	var actions []string
	al.processFatfishCodexEvent(context.Background(), "123", codexWakeEvent(), func(_ context.Context, action string, _ any) (fatfish.Decision, error) {
		actions = append(actions, action)
		return fatfish.Decision{Allowed: true, Accepted: true, Duplicate: true}, nil
	})
	if p.calls != 0 || !reflect.DeepEqual(actions, []string{"codex_event_start"}) {
		t.Fatalf("duplicate receipt re-executed: actions=%v calls=%d", actions, p.calls)
	}
}

func TestCodexEventPendingHumanStopCancelsWithoutCallingModel(t *testing.T) {
	p := &codexWakeProvider{}
	al := newCodexWakeTestLoop(t, p)
	msg := codexWakeHumanMessage("123")
	key, _, _ := al.resolveSteeringTarget(msg)
	al.markPendingStop(key)
	var outcome string
	al.processFatfishCodexEvent(context.Background(), "123", codexWakeEvent(), func(_ context.Context, action string, payload any) (fatfish.Decision, error) {
		if action == "codex_event_ack" {
			outcome = payload.(map[string]any)["outcome"].(string)
		}
		return fatfish.Decision{Allowed: true, Accepted: true}, nil
	})
	if p.calls != 0 || outcome != "interrupted" || al.getActiveTurnState(key) != nil {
		t.Fatalf("pending stop ignored: calls=%d outcome=%q", p.calls, outcome)
	}
}

func TestCodexEventProviderFailureAcknowledgesTerminalOutcome(t *testing.T) {
	for _, failure := range []string{"error", "panic"} {
		t.Run(failure, func(t *testing.T) {
			p := &codexWakeProvider{err: errors.New("bounded failure"), panicCall: failure == "panic"}
			al := newCodexWakeTestLoop(t, p)
			var outcome string
			al.processFatfishCodexEvent(context.Background(), "123", codexWakeEvent(), func(_ context.Context, action string, payload any) (fatfish.Decision, error) {
				if action == "codex_event_ack" {
					outcome = payload.(map[string]any)["outcome"].(string)
				}
				return fatfish.Decision{Allowed: true, Accepted: true}, nil
			})
			want := "error"
			if failure == "panic" {
				want = "panicked"
			}
			if outcome != want || p.calls != 1 {
				t.Fatalf("outcome=%q calls=%d", outcome, p.calls)
			}
		})
	}
}
