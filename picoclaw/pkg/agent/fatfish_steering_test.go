package agent

import (
	"context"
	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/config"
	"github.com/sipeed/picoclaw/pkg/providers"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type fatfishInterruptProvider struct {
	calls                         atomic.Int32
	sawPeer                       atomic.Bool
	started, cancelled, corrected chan struct{}
}

func (p *fatfishInterruptProvider) GetDefaultModel() string { return "test" }
func (p *fatfishInterruptProvider) Chat(ctx context.Context, messages []providers.Message, _ []providers.ToolDefinition, _ string, _ map[string]any) (*providers.LLMResponse, error) {
	if p.calls.Add(1) == 1 {
		close(p.started)
		<-ctx.Done()
		close(p.cancelled)
		return nil, ctx.Err()
	}
	var text string
	for _, m := range messages {
		text += m.Content
	}
	if strings.Contains(text, "PEER_MUST_NOT_INTERRUPT") {
		p.sawPeer.Store(true)
	}
	if strings.Contains(text, "先查旧话题") && strings.Contains(text, "改聊新话题") {
		select {
		case <-p.corrected:
		default:
			close(p.corrected)
		}
	}
	return &providers.LLMResponse{Content: "好，聊新的。"}, nil
}

func TestFatfishPeerBotCannotInterruptHumanGeneration(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "test-enabled")
	cfg := &config.Config{Agents: config.AgentsConfig{Defaults: config.AgentDefaults{Workspace: t.TempDir(), ModelName: "test", MaxTokens: 4096, MaxToolIterations: 6, SteeringMode: "all"}}}
	p := &fatfishInterruptProvider{started: make(chan struct{}), cancelled: make(chan struct{}), corrected: make(chan struct{})}
	b := bus.NewMessageBus()
	al := NewAgentLoop(cfg, b, p)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	finished := make(chan error, 1)
	go func() { finished <- al.Run(ctx) }()
	publish := func(id, text string, isBot bool) {
		raw := map[string]string{}
		if isBot {
			raw["sender_is_bot"] = "true"
		}
		err := b.PublishInbound(ctx, bus.InboundMessage{Channel: "telegram", ChatID: "-9", Context: bus.InboundContext{Channel: "telegram", ChatID: "-9", ChatType: "group", SenderID: "123", MessageID: id, Raw: raw}, Content: text})
		if err != nil {
			t.Fatal(err)
		}
	}
	publish("1", "先查旧话题", false)
	select {
	case <-p.started:
	case <-time.After(5 * time.Second):
		t.Fatal("initial human turn missing")
	}
	publish("2", "PEER_MUST_NOT_INTERRUPT", true)
	publish("3", "改聊新话题", false)
	select {
	case <-p.corrected:
	case <-time.After(5 * time.Second):
		t.Fatal("real human correction was not processed")
	}
	if p.sawPeer.Load() {
		t.Fatal("peer bot entered the human steering queue")
	}
	cancel()
	select {
	case <-finished:
	case <-time.After(3 * time.Second):
		t.Fatal("agent did not stop")
	}
}
func TestFatfishNewMessageCancelsOldGenerationAndPreservesConversation(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "test-enabled")
	cfg := &config.Config{Agents: config.AgentsConfig{Defaults: config.AgentDefaults{Workspace: t.TempDir(), ModelName: "test", MaxTokens: 4096, MaxToolIterations: 6, SteeringMode: "all"}}}
	p := &fatfishInterruptProvider{started: make(chan struct{}), cancelled: make(chan struct{}), corrected: make(chan struct{})}
	b := bus.NewMessageBus()
	al := NewAgentLoop(cfg, b, p)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	finished := make(chan error, 1)
	go func() { finished <- al.Run(ctx) }()
	publish := func(id, text string) {
		t.Helper()
		err := b.PublishInbound(ctx, bus.InboundMessage{Context: bus.InboundContext{Channel: "telegram", ChatID: "123", ChatType: "direct", SenderID: "123", MessageID: id}, Content: text})
		if err != nil {
			t.Fatal(err)
		}
	}
	publish("1", "先查旧话题")
	select {
	case <-p.started:
	case <-time.After(5 * time.Second):
		t.Fatal("initial call missing")
	}
	publish("2", "改聊新话题")
	select {
	case <-p.cancelled:
	case <-time.After(3 * time.Second):
		t.Fatal("old provider request not cancelled")
	}
	select {
	case <-p.corrected:
	case <-time.After(3 * time.Second):
		t.Fatal("new turn lost original or corrective message")
	}
	cancel()
	select {
	case <-finished:
	case <-time.After(3 * time.Second):
		t.Fatal("agent did not stop")
	}
}
func TestFatfishSteeringUpdatesTrustedMessageIdentity(t *testing.T) {
	original := &bus.InboundContext{Channel: "telegram", ChatID: "-9", SenderID: "owner", MessageID: "1"}
	ts := &turnState{opts: processOptions{Dispatch: DispatchRequest{InboundContext: original}}}
	ts.adoptSteeringSource(providers.Message{SteeringSenderID: "member", SteeringMessageID: "2", SteeringReplyID: "8"})
	if ts.opts.Dispatch.MessageID() != "2" || ts.opts.Dispatch.InboundContext.SenderID != "member" || original.SenderID != "owner" {
		t.Fatal("trusted identity was stale or original context mutated")
	}
}

func TestFatfishInterruptNeverCancelsDeliveryOrPublication(t *testing.T) {
	for _, name := range []string{"mcp_reader_send_message", "mcp_reader_emote_send", "mcp_reader_bilibili_comment_submit", "mcp_reader_reminder", "mcp_reader_emote_save"} {
		if fatfishInterruptibleRead(name) {
			t.Fatalf("write marked interruptible: %s", name)
		}
	}
	if !fatfishInterruptibleRead("mcp_deepseek_search") {
		t.Fatal("search not interruptible")
	}
}
