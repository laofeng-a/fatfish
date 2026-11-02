package agent

import (
	"context"
	"strings"
	"testing"

	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/config"
	"github.com/sipeed/picoclaw/pkg/providers"
	"github.com/sipeed/picoclaw/pkg/tools"
)

func TestToolDeliveryHistoryDoesNotInventSentAnswer(t *testing.T) {
	result := &tools.ToolResult{ForLLM: "PRIVATE_TOOL_INTERNAL", ResponseHandled: true,
		DeliverySummary: "[Delivery receipt: sent] Confirmed sent media: sticker 得意"}
	content := deliveryHistoryContent(toolDeliveryHistory(result, false, nil))
	if !strings.Contains(content, "sticker 得意") || strings.Contains(content, "PRIVATE_TOOL_INTERNAL") || strings.Contains(content, "Requested output delivered") {
		t.Fatal(content)
	}
	queued := deliveryHistoryContent(toolDeliveryHistory(&tools.ToolResult{ForUser: "实际排队的文字", ResponseHandled: true}, true, nil))
	if !strings.Contains(queued, "delivery not confirmed: 实际排队的文字") {
		t.Fatal(queued)
	}
	failed := toolDeliveryHistory(&tools.ToolResult{IsError: true, ForLLM: "发送失败", ForUser: "未发出的文字"}, false, nil)
	if len(failed) != 0 {
		t.Fatalf("failure invented delivery: %v", failed)
	}
	attachment := deliveryHistoryContent(toolDeliveryHistory(&tools.ToolResult{ResponseHandled: true}, false, []providers.Attachment{{Filename: "image.png", ContentType: "image/png"}}))
	if !strings.Contains(attachment, "Confirmed sent attachment: image.png") {
		t.Fatal(attachment)
	}
}

type deliveryHistoryTool struct {
	name   string
	result *tools.ToolResult
}

func (t *deliveryHistoryTool) Name() string        { return t.name }
func (t *deliveryHistoryTool) Description() string { return "Delivery history regression fixture" }
func (t *deliveryHistoryTool) Parameters() map[string]any {
	return map[string]any{"type": "object", "properties": map[string]any{}}
}
func (t *deliveryHistoryTool) Execute(context.Context, map[string]any) *tools.ToolResult {
	return t.result
}

type mixedDeliveryProvider struct {
	calls         int
	summaryPrompt string
	final         string
}

func (p *mixedDeliveryProvider) GetDefaultModel() string { return "test" }
func (p *mixedDeliveryProvider) Chat(_ context.Context, messages []providers.Message, _ []providers.ToolDefinition, _ string, _ map[string]any) (*providers.LLMResponse, error) {
	p.calls++
	if len(messages) == 1 && strings.Contains(messages[0].Content, "CONVERSATION:") {
		p.summaryPrompt = messages[0].Content
		return &providers.LLMResponse{Content: "已保留发送事实"}, nil
	}
	if p.calls == 1 {
		return &providers.LLMResponse{ToolCalls: []providers.ToolCall{
			{ID: "read_fixture", Type: "function", Name: "history_read_fixture", Arguments: map[string]any{}},
			{ID: "send_fixture", Type: "function", Name: "history_send_fixture", Arguments: map[string]any{}},
		}}, nil
	}
	if p.final != "" {
		return &providers.LLMResponse{Content: p.final}, nil
	}
	return &providers.LLMResponse{Content: "NO_REPLY"}, nil
}

func TestDeliveryHistoryMixedReadSendSurvivesNoReplyAndCompaction(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "")
	cfg := config.DefaultConfig()
	cfg.Agents.Defaults.Workspace = t.TempDir()
	cfg.Agents.Defaults.ModelName = "test"
	cfg.Agents.Defaults.MaxToolIterations = 3
	provider := &mixedDeliveryProvider{}
	al := NewAgentLoop(cfg, bus.NewMessageBus(), provider)
	t.Cleanup(al.Close)
	al.RegisterTool(&deliveryHistoryTool{name: "history_read_fixture", result: tools.SilentResult("PRIVATE_READER_INTERNAL")})
	al.RegisterTool(&deliveryHistoryTool{name: "history_send_fixture", result: &tools.ToolResult{ForLLM: "PRIVATE_DELIVERY_INTERNAL", Silent: true, ResponseHandled: true, DeliverySummary: "[Delivery receipt: sent]\nConfirmed sent media: sticker 得意\nNo text answer was sent."}})
	_, err := al.processMessage(context.Background(), testInboundMessage(bus.InboundMessage{Channel: "telegram", ChatID: "chat1", SenderID: "user1", Content: "进度呢"}))
	if err != nil {
		t.Fatal(err)
	}
	if provider.calls != 2 {
		t.Fatalf("mixed batch should continue once then NO_REPLY, calls=%d", provider.calls)
	}
	agent := al.GetRegistry().GetDefaultAgent()
	keys := agent.Sessions.ListSessions()
	if len(keys) != 1 {
		t.Fatalf("unexpected sessions: %v", keys)
	}
	history := agent.Sessions.GetHistory(keys[0])
	found := false
	for _, msg := range history {
		if msg.Role == "assistant" && strings.Contains(msg.Content, "Output history:") {
			t.Fatal("internal delivery facts became an assistant utterance")
		}
		if msg.Role == "tool" && strings.HasPrefix(msg.Content, "[Output history:") {
			found = true
			fact, _ := deliveryFactsForSummary(msg)
			if !strings.Contains(fact.Content, "sticker 得意") || !strings.Contains(fact.Content, "No text answer") || strings.Contains(fact.Content, "PRIVATE_") {
				t.Fatal(msg.Content)
			}
		}
	}
	if !found {
		t.Fatal("mixed send facts disappeared after NO_REPLY")
	}
	// Add later exchanges so the delivery record really passes through the
	// default summarizer's role filtering instead of only checking a helper.
	for i := 0; i < 4; i++ {
		history = append(history, providers.Message{Role: "user", Content: "后来的闲聊"}, providers.Message{Role: "assistant", Content: "嗯"})
	}
	agent.Sessions.SetHistory(keys[0], history)
	manager := &legacyContextManager{al: al}
	manager.summarizeSession(agent, keys[0])
	if !strings.Contains(provider.summaryPrompt, "sticker 得意") || !strings.Contains(provider.summaryPrompt, "No text answer") {
		t.Fatal("actual compaction prompt lost delivery facts")
	}
	if strings.Contains(provider.summaryPrompt, "PRIVATE_READER_INTERNAL") || strings.Contains(provider.summaryPrompt, "PRIVATE_DELIVERY_INTERNAL") {
		t.Fatal("raw private tool content leaked into summary")
	}
}

func TestDeliveryHistoryBoundedAndCompactionKeepsDeliveryDistinctions(t *testing.T) {
	content := deliveryHistoryContent([]string{strings.Repeat("长", 7000)})
	if len([]rune(content)) > 6100 || !strings.Contains(content, "truncated") {
		t.Fatal("delivery history is not bounded")
	}
	for _, fact := range []string{"回答了什么", "部分送达", "只发表情", "还欠哪个答案", "已告知以确认送达为准"} {
		if !strings.Contains(fatfishCompactionPrompt, fact) {
			t.Fatalf("compaction missing %s", fact)
		}
	}
}

func TestDeliveryHistoryEchoIsNeitherAssistantHistoryNorChatOutput(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "")
	cfg := config.DefaultConfig()
	cfg.Agents.Defaults.Workspace = t.TempDir()
	cfg.Agents.Defaults.ModelName = "test"
	provider := &mixedDeliveryProvider{final: "[Output history: delivery facts, not an additional message to the user]\nConfirmed sent text: 之前的答复"}
	msgBus := bus.NewMessageBus()
	al := NewAgentLoop(cfg, msgBus, provider)
	t.Cleanup(al.Close)
	al.RegisterTool(&deliveryHistoryTool{name: "history_read_fixture", result: tools.SilentResult("已读取")})
	al.RegisterTool(&deliveryHistoryTool{name: "history_send_fixture", result: &tools.ToolResult{Silent: true, ResponseHandled: true, DeliverySummary: "[Delivery receipt: sent]\nConfirmed sent text: 之前的答复"}})
	response, err := al.processMessage(context.Background(), testInboundMessage(bus.InboundMessage{Channel: "telegram", ChatID: "123", SenderID: "123", Content: "进展呢"}))
	if err != nil {
		t.Fatal(err)
	}
	key := al.GetRegistry().GetDefaultAgent().Sessions.ListSessions()[0]
	al.PublishResponseIfNeeded(context.Background(), "telegram", "123", key, response)
	select {
	case msg := <-msgBus.OutboundChan():
		t.Fatalf("leaked: %s", msg.Content)
	default:
	}
	for _, msg := range al.GetRegistry().GetDefaultAgent().Sessions.GetHistory(key) {
		if msg.Role == "assistant" && strings.Contains(msg.Content, "Output history:") {
			t.Fatal("echo re-entered assistant history")
		}
	}
}

func TestCodexReceiptRetainsBrokerContentWithoutDuplicatedTemplate(t *testing.T) {
	event := codexWakeEvent()
	event.Content = "实际结果与当前任务的后续要求"
	msg := fatfishCodexInbound("123", event)
	if msg.Content != "[Codex协作回执]\n"+event.Content {
		t.Fatalf("broker content changed or obsolete instructions appended: %q", msg.Content)
	}
	if msg.Context.MessageID != "codex-event:"+event.ID ||
		msg.Context.Raw["source_kind"] != "codex_event" ||
		strings.Contains(msg.Content, event.Claim) {
		t.Fatal("receipt lost its distinct identity or exposed the local claim")
	}
	event.Kind = "query"
	if got := fatfishCodexInbound("123", event).Content; got != "[Codex查询结果]\n"+event.Content {
		t.Fatalf("query content changed: %q", got)
	}
}
