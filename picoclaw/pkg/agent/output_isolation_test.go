package agent

import (
	"context"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/config"
	"github.com/sipeed/picoclaw/pkg/fatfish"
	"github.com/sipeed/picoclaw/pkg/providers"
	integrationtools "github.com/sipeed/picoclaw/pkg/tools/integration"
	toolshared "github.com/sipeed/picoclaw/pkg/tools/shared"
)

type isolationProvider struct {
	calls int
	tools bool
	human bool
}

func (*isolationProvider) GetDefaultModel() string { return "test" }
func (p *isolationProvider) Chat(_ context.Context, msgs []providers.Message, _ []providers.ToolDefinition, _ string, _ map[string]any) (*providers.LLMResponse, error) {
	p.calls++
	for _, m := range msgs {
		if strings.Contains(m.Content, "HUMAN_NEW_MESSAGE") {
			p.human = true
		}
	}
	if p.human {
		return &providers.LLMResponse{Content: "正常回复真人"}, nil
	}
	if p.tools && p.calls == 1 {
		return &providers.LLMResponse{ToolCalls: []providers.ToolCall{
			{ID: "send", Type: "function", Name: "mcp_reader_send_message", Arguments: map[string]any{"text": "指定的主动分享"}},
			{ID: "read", Type: "function", Name: "mcp_reader_history_find", Arguments: map[string]any{}},
		}}, nil
	}
	return &providers.LLMResponse{Content: "内部收尾：已经检查完，这句不是给用户的。"}, nil
}

type isolationMCP struct {
	sent      []string
	afterSend func()
}

func (m *isolationMCP) CallTool(_ context.Context, _, name string, args map[string]any) (*mcp.CallToolResult, error) {
	text := `{"status":"read","content":"实际读取结果"}`
	if name == "send_message" {
		m.sent = append(m.sent, args["text"].(string))
		if m.afterSend != nil {
			m.afterSend()
		}
		text = `{"status":"sent","delivery_receipt":{"state":"sent","current_chat":true,"text":"指定的主动分享","answer_delivered":true}}`
	}
	return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: text}}}, nil
}

func newIsolationLoop(t *testing.T, p *isolationProvider) *AgentLoop {
	t.Helper()
	// A nonexistent credential enables production routing without live requests.
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", t.TempDir()+"/missing-test-credential")
	cfg := config.DefaultConfig()
	cfg.Agents.Defaults.Workspace = t.TempDir()
	cfg.Agents.Defaults.ModelName = "test"
	cfg.Agents.Defaults.MaxToolIterations = 5
	al := NewAgentLoop(cfg, bus.NewMessageBus(), p)
	t.Cleanup(al.Close)
	return al
}
func assertNoIsolationOutbound(t *testing.T, al *AgentLoop) {
	t.Helper()
	select {
	case msg := <-al.bus.(*bus.MessageBus).OutboundChan():
		t.Fatalf("unexpected Telegram output: %s", msg.Content)
	default:
	}
}

func TestOutputIsolationBackgroundFinals(t *testing.T) {
	for _, silent := range []bool{false, true} {
		t.Run(map[bool]string{false: "callback", true: "silent_callback"}[silent], func(t *testing.T) {
			p := &isolationProvider{}
			al := newIsolationLoop(t, p)
			ev := codexWakeEvent()
			ev.Silent = silent
			al.processFatfishCodexEvent(context.Background(), "123", ev, func(context.Context, string, any) (fatfish.Decision, error) {
				return fatfish.Decision{Allowed: true, Accepted: true}, nil
			})
			if p.calls != 1 {
				t.Fatalf("model not run: %d", p.calls)
			}
			assertNoIsolationOutbound(t, al)
			for _, a := range al.GetRegistry().GetDefaultAgent().Sessions.ListSessions() {
				for _, msg := range al.GetRegistry().GetDefaultAgent().Sessions.GetHistory(a) {
					if msg.Role == "assistant" && strings.Contains(msg.Content, "内部收尾") {
						t.Fatal("working note became a conversation utterance")
					}
				}
			}
		})
	}
	for _, silent := range []bool{false, true} {
		t.Run(map[bool]string{false: "heartbeat", true: "silent_alarm"}[silent], func(t *testing.T) {
			al := newIsolationLoop(t, &isolationProvider{})
			_, err := al.processHeartbeat(context.Background(), "后台整理", "telegram", "123", func(_ context.Context, action string, _ any) (fatfish.Decision, error) {
				return fatfish.Decision{Allowed: true, WakeID: strings.Repeat("a", 24), Silent: silent, Context: "只做内部整理"}, nil
			})
			if err != nil {
				t.Fatal(err)
			}
			assertNoIsolationOutbound(t, al)
		})
	}
}

func TestOutputIsolationExplicitMCPThenReadAndWrapup(t *testing.T) {
	for _, background := range []bool{false, true} {
		t.Run(map[bool]string{false: "foreground", true: "background"}[background], func(t *testing.T) {
			p := &isolationProvider{tools: true}
			al := newIsolationLoop(t, p)
			manager := &isolationMCP{}
			for _, name := range []string{"send_message", "history_find"} {
				al.RegisterTool(integrationtools.NewMCPTool(manager, "reader", &mcp.Tool{Name: name, InputSchema: map[string]any{"type": "object"}}))
			}
			ctx := toolshared.WithReplyDeliveryTracking(context.Background())
			msg := codexWakeHumanMessage("123")
			if background {
				msg = fatfishCodexInbound("123", codexWakeEvent())
				ctx = fatfish.WithCodexEvent(ctx, codexWakeEvent())
			}
			response, err := al.processMessage(ctx, msg)
			if err != nil {
				t.Fatal(err)
			}
			key, _, _ := al.resolveSteeringTarget(msg)
			al.PublishResponseIfNeeded(ctx, "telegram", "123", key, response)
			if len(manager.sent) != 1 || manager.sent[0] != "指定的主动分享" || p.calls != 2 {
				t.Fatalf("sent=%v calls=%d", manager.sent, p.calls)
			}
			if !toolshared.HasReplyDelivery(ctx, "telegram", "123") {
				t.Fatal("MCP receipt was lost after an unrelated read")
			}
			assertNoIsolationOutbound(t, al)
		})
	}
}

func TestOutputIsolationHumanSteeringDuringSilentCallback(t *testing.T) {
	p := &isolationProvider{tools: true}
	al := newIsolationLoop(t, p)
	ev := codexWakeEvent()
	ev.Silent = true
	msg := fatfishCodexInbound("123", ev)
	key, agentID, _ := al.resolveSteeringTarget(msg)
	manager := &isolationMCP{afterSend: func() {
		if err := al.enqueueSteeringMessage(key, agentID, providers.Message{Role: "user", Content: "HUMAN_NEW_MESSAGE", SteeringMessageID: "456", SteeringSenderID: "123"}); err != nil {
			t.Fatal(err)
		}
	}}
	for _, name := range []string{"send_message", "history_find"} {
		al.RegisterTool(integrationtools.NewMCPTool(manager, "reader", &mcp.Tool{Name: name, InputSchema: map[string]any{"type": "object"}}))
	}
	al.processFatfishCodexEvent(context.Background(), "123", ev, func(context.Context, string, any) (fatfish.Decision, error) {
		return fatfish.Decision{Allowed: true, Accepted: true}, nil
	})
	if !p.human || len(manager.sent) != 1 {
		t.Fatal("human steering not processed")
	}
	select {
	case reply := <-al.bus.(*bus.MessageBus).OutboundChan():
		if reply.Content != "正常回复真人" || reply.Context.MessageID != "456" {
			t.Fatalf("human reply lost or misrouted: %+v", reply)
		}
	default:
		t.Fatal("silent callback swallowed the new human reply")
	}
	assertNoIsolationOutbound(t, al)
}
