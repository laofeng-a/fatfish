package agent

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/config"
	"github.com/sipeed/picoclaw/pkg/fatfish"
	"github.com/sipeed/picoclaw/pkg/providers"
)

type silentHeartbeatProvider struct {
	calls       int
	messages    []providers.Message
	background  bool
	deadline    time.Duration
	response    string
	err         error
	panicCall   bool
	waitTimeout bool
}

func (*silentHeartbeatProvider) GetDefaultModel() string { return "test" }
func (p *silentHeartbeatProvider) Chat(ctx context.Context, messages []providers.Message, _ []providers.ToolDefinition, _ string, _ map[string]any) (*providers.LLMResponse, error) {
	p.calls++
	p.messages = append([]providers.Message(nil), messages...)
	p.background = fatfish.IsBackground(ctx)
	if deadline, ok := ctx.Deadline(); ok {
		p.deadline = time.Until(deadline)
	}
	if p.panicCall {
		panic("test panic")
	}
	if p.waitTimeout {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	if p.err != nil {
		return nil, p.err
	}
	return &providers.LLMResponse{Content: p.response}, nil
}

func newSilentHeartbeatLoop(t *testing.T, p *silentHeartbeatProvider) *AgentLoop {
	t.Helper()
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "")
	cfg := &config.Config{
		Agents: config.AgentsConfig{Defaults: config.AgentDefaults{
			Workspace: t.TempDir(), ModelName: "test", MaxTokens: 4096, MaxToolIterations: 2,
		}},
		Session: config.DefaultConfig().Session,
	}
	al := NewAgentLoop(cfg, bus.NewMessageBus(), p)
	t.Cleanup(al.Close)
	return al
}

func TestSilentHeartbeatUsesOnlyTrustedTaskAndCompletesOnceWithoutPublishing(t *testing.T) {
	p := &silentHeartbeatProvider{response: strings.Repeat("安", 2100)}
	al := newSilentHeartbeatLoop(t, p)
	al.GetRegistry().GetDefaultAgent().Sessions.SetHistory("heartbeat", []providers.Message{{Role: "user", Content: "STALE_HEARTBEAT_HISTORY"}})
	var actions []string
	var done map[string]any
	response, err := al.processHeartbeat(context.Background(), "ORDINARY_SHARE_TASK", "telegram", "123", func(_ context.Context, action string, payload any) (fatfish.Decision, error) {
		actions = append(actions, action)
		if action == "wake_done" {
			done = payload.(map[string]any)
			if p.calls != 1 {
				t.Fatal("acknowledged before the actual model returned")
			}
		}
		return fatfish.Decision{Allowed: true, WakeID: "wake-test", Silent: true, Context: "SILENT_QUOTA_TASK"}, nil
	})
	if err != nil || response != p.response || p.calls != 1 || !p.background || p.deadline != 0 {
		t.Fatalf("model execution: calls=%d background=%t deadline=%s err=%v", p.calls, p.background, p.deadline, err)
	}
	var combined strings.Builder
	for _, msg := range p.messages {
		combined.WriteString(msg.Content)
	}
	if !strings.Contains(combined.String(), "SILENT_QUOTA_TASK") || strings.Contains(combined.String(), "ORDINARY_SHARE_TASK") || strings.Contains(combined.String(), "STALE_HEARTBEAT_HISTORY") {
		t.Fatal("silent wake mixed in ordinary sharing work or stale history")
	}
	if !reflect.DeepEqual(actions, []string{"wake", "wake_done"}) || done["wake_id"] != "wake-test" || done["outcome"] != "completed" || len([]rune(done["summary"].(string))) != 2000 {
		t.Fatalf("completion lifecycle or bounded summary incorrect: actions=%v done=%v", actions, done)
	}
	select {
	case outbound := <-al.bus.(*bus.MessageBus).OutboundChan():
		t.Fatalf("silent final text was published: %+v", outbound)
	default:
	}
}

func TestOrdinaryHeartbeatKeepsPromptAndAddsBrokerContext(t *testing.T) {
	p := &silentHeartbeatProvider{response: "HEARTBEAT_OK"}
	al := newSilentHeartbeatLoop(t, p)
	_, err := al.processHeartbeat(context.Background(), "ORDINARY_TASK", "telegram", "123", func(_ context.Context, action string, _ any) (fatfish.Decision, error) {
		return fatfish.Decision{Allowed: true, WakeID: "ordinary", Context: "BROKER_CONTEXT"}, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, msg := range p.messages {
		if strings.Contains(msg.Content, "ORDINARY_TASK") && strings.Contains(msg.Content, "BROKER_CONTEXT") {
			return
		}
	}
	t.Fatal("ordinary task or broker context was lost")
}

func TestHeartbeatWithoutFatfishSupportsNonTelegramChannels(t *testing.T) {
	for _, channel := range []string{"cli", "telegram"} {
		t.Run(channel, func(t *testing.T) {
			p := &silentHeartbeatProvider{response: "HEARTBEAT_OK"}
			al := newSilentHeartbeatLoop(t, p)
			response, err := al.ProcessHeartbeat(context.Background(), "GENERIC_HEARTBEAT_TASK", channel, "direct")
			if err != nil || response != "HEARTBEAT_OK" || p.calls != 1 {
				t.Fatalf("generic heartbeat regressed: channel=%s calls=%d response=%q err=%v", channel, p.calls, response, err)
			}
			for _, msg := range p.messages {
				if strings.Contains(msg.Content, "GENERIC_HEARTBEAT_TASK") {
					return
				}
			}
			t.Fatal("generic heartbeat lost its original prompt")
		})
	}
}

func TestBusyHeartbeatDoesNotClaimBrokerWork(t *testing.T) {
	for _, busy := range []string{"human", "worker", "heartbeat"} {
		t.Run(busy, func(t *testing.T) {
			p := &silentHeartbeatProvider{}
			al := newSilentHeartbeatLoop(t, p)
			switch busy {
			case "human":
				key, _, _ := al.resolveSteeringTarget(codexWakeHumanMessage("123"))
				al.activeTurnStates.Store(key, &turnState{turnID: "human-turn"})
				defer al.activeTurnStates.Delete(key)
			case "worker":
				al.workerSem <- struct{}{}
				defer func() { <-al.workerSem }()
			case "heartbeat":
				al.activeTurnStates.Store("heartbeat", &turnState{turnID: "existing-heartbeat"})
				defer al.activeTurnStates.Delete("heartbeat")
			}
			calls := 0
			response, err := al.processHeartbeat(context.Background(), "task", "telegram", "123", func(context.Context, string, any) (fatfish.Decision, error) {
				calls++
				return fatfish.Decision{Allowed: true}, nil
			})
			if err != nil || response != "HEARTBEAT_OK" || calls != 0 || p.calls != 0 {
				t.Fatalf("busy check claimed work: broker=%d model=%d response=%q err=%v", calls, p.calls, response, err)
			}
		})
	}
}

func TestHeartbeatFailureCompletesExactWakeOnceAsError(t *testing.T) {
	for _, failure := range []string{"missing_context", "provider", "panic", "timeout"} {
		t.Run(failure, func(t *testing.T) {
			p := &silentHeartbeatProvider{response: "ok"}
			switch failure {
			case "provider":
				p.err = errors.New("private provider failure")
			case "panic":
				p.panicCall = true
			case "timeout":
				p.waitTimeout = true
			}
			al := newSilentHeartbeatLoop(t, p)
			ctx := context.Background()
			if failure == "timeout" {
				var cancel context.CancelFunc
				ctx, cancel = context.WithTimeout(ctx, 25*time.Millisecond)
				defer cancel()
			}
			doneCalls := 0
			_, err := al.processHeartbeat(ctx, "ordinary", "telegram", "123", func(_ context.Context, action string, payload any) (fatfish.Decision, error) {
				if action == "wake_done" {
					doneCalls++
					value := payload.(map[string]any)
					if value["wake_id"] != failure || value["outcome"] != "error" || strings.Contains(value["summary"].(string), "private") {
						t.Fatalf("unsafe or incorrect failure receipt: %v", value)
					}
				}
				content := "silent task"
				if failure == "missing_context" {
					content = ""
				}
				return fatfish.Decision{Allowed: true, WakeID: failure, Silent: true, Context: content}, nil
			})
			if err == nil || doneCalls != 1 || len(al.workerSem) != 0 || al.getActiveTurnState("heartbeat") != nil {
				t.Fatalf("failure leaked wake or local capacity: err=%v done=%d workers=%d", err, doneCalls, len(al.workerSem))
			}
		})
	}
}

func TestSkippedHeartbeatHasNoCompletionAndReleasesCapacity(t *testing.T) {
	p := &silentHeartbeatProvider{}
	al := newSilentHeartbeatLoop(t, p)
	var actions []string
	_, err := al.processHeartbeat(context.Background(), "task", "telegram", "123", func(_ context.Context, action string, _ any) (fatfish.Decision, error) {
		actions = append(actions, action)
		return fatfish.Decision{Allowed: true, Skip: true}, nil
	})
	if err != nil || !reflect.DeepEqual(actions, []string{"wake"}) || p.calls != 0 || len(al.workerSem) != 0 || al.getActiveTurnState("heartbeat") != nil {
		t.Fatalf("skip incorrectly completed or leaked work: actions=%v err=%v", actions, err)
	}
}
