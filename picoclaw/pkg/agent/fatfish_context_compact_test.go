package agent

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/sipeed/picoclaw/pkg/providers"
)

func compactToolCall(id string) providers.Message {
	return providers.Message{Role: "assistant", ToolCalls: []providers.ToolCall{{ID: id, Name: "notion", Function: &providers.FunctionCall{Name: "notion", Arguments: "{}"}}}}
}

func TestFatfishToolFoldingPreservesAllDialogueAndRawInput(t *testing.T) {
	long := strings.Repeat("工具原文", 3000)
	history := []providers.Message{{Role: "user", Content: "菜量小，我还想跟你聊聊"}, compactToolCall("old"),
		{Role: "tool", ToolCallID: "old", Content: long}, {Role: "assistant", Content: strings.Repeat("这是已经说出的回答", 500)}, {Role: "user", Content: "继续聊"}}
	before := append([]providers.Message(nil), history...)
	folded := foldFatfishToolResults(history)
	if !reflect.DeepEqual(before, history) {
		t.Fatal("projection changed stored source")
	}
	for _, index := range []int{0, 1, 3, 4} {
		if !reflect.DeepEqual(folded[index], history[index]) {
			t.Fatal("dialogue/call changed", index)
		}
	}
	if !strings.HasPrefix(folded[2].Content, foldedToolPrefix) || len(folded[2].Content) >= len(long)/4 {
		t.Fatal("old result did not shrink")
	}
	if !reflect.DeepEqual(folded, foldFatfishToolResults(folded)) {
		t.Fatal("folding is not idempotent")
	}
}

func TestFatfishToolFoldingProtectsWholeLatestBatch(t *testing.T) {
	long := strings.Repeat("raw", 1000)
	history := []providers.Message{{Role: "user", Content: "read"}, compactToolCall("old"), {Role: "tool", ToolCallID: "old", Content: long},
		{Role: "assistant", ToolCalls: []providers.ToolCall{{ID: "a"}, {ID: "b"}}},
		{Role: "tool", ToolCallID: "a", Content: long}, {Role: "tool", ToolCallID: "b", Content: long}}
	folded := foldFatfishToolResults(history)
	if folded[2].Content == long {
		t.Fatal("consumed batch not folded")
	}
	if !reflect.DeepEqual(folded[3:], history[3:]) {
		t.Fatal("unread parallel batch was split or folded")
	}
}

func TestFatfishFoldingKeepsDeliveryAndSourceLocators(t *testing.T) {
	fact := "[Output history: delivery facts, not an additional message to the user]\nConfirmed sent text: 陪你聊着呢"
	content := fact + toolDiagnosticSeparator + `{"record_id":987,"job_id":"abc","status":"completed","data":"` + strings.Repeat("x", 3000) + `"}`
	history := []providers.Message{{Role: "user", Content: "before"}, compactToolCall("sent"), {Role: "tool", Content: content}, {Role: "user", Content: "now"}}
	folded := foldFatfishToolResults(history)
	if !strings.HasPrefix(folded[2].Content, fact+toolDiagnosticSeparator) || !strings.Contains(folded[2].Content, `"record_id":987`) {
		t.Fatal("delivery/source lost")
	}
}

func TestFatfishFoldingOnlyRetiresOldToolMedia(t *testing.T) {
	history := []providers.Message{{Role: "user", Content: "picture", Media: []string{"user-image"}}, compactToolCall("old"),
		{Role: "tool", ToolCallID: "old", Content: "image loaded", Media: []string{"old-tool-image"}}, {Role: "user", Content: "new picture"}, compactToolCall("new"),
		{Role: "tool", ToolCallID: "new", Content: "image loaded", Media: []string{"new-tool-image"}}}
	folded := foldFatfishToolResults(history)
	if len(folded[2].Media) != 0 || history[2].Media[0] != "old-tool-image" {
		t.Fatal("old tool media source was changed")
	}
	if !reflect.DeepEqual(folded[0], history[0]) || !reflect.DeepEqual(folded[5], history[5]) {
		t.Fatal("user or current tool image lost")
	}
}

func TestFatfishCompressionNotesCoalesceWithoutLosingText(t *testing.T) {
	summary := "聊过菜量小，想继续聊天。\n" + strings.Repeat("[Emergency compression dropped 12 oldest messages due to context limit]\n", 24)
	clean := normalizeFatfishSummary(summary)
	if strings.Contains(clean, "Emergency compression") || strings.Count(clean, "历史压缩记录") != 1 || !strings.Contains(clean, "288") || !strings.Contains(clean, "菜量小") {
		t.Fatal(clean)
	}
	if normalizeFatfishSummary(clean) != clean {
		t.Fatal("coalescing changed count twice")
	}
}

type compactProbeProvider struct {
	response string
	err      error
	input    string
	opts     map[string]any
	during   func()
}

func (p *compactProbeProvider) GetDefaultModel() string { return "fixture" }
func (p *compactProbeProvider) Chat(_ context.Context, messages []providers.Message, _ []providers.ToolDefinition, _ string, opts map[string]any) (*providers.LLMResponse, error) {
	p.input = messages[0].Content
	p.opts = opts
	if p.during != nil {
		p.during()
	}
	return &providers.LLMResponse{Content: p.response}, p.err
}

func compactTestLoop(t *testing.T, provider *compactProbeProvider) (*legacyContextManager, *AgentInstance) {
	t.Helper()
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", filepath.Join(t.TempDir(), "no-runtime"))
	al, agent, cleanup := newTurnCoordTestLoop(t, provider)
	t.Cleanup(cleanup)
	agent.ContextWindow = 65536
	agent.MaxTokens = 2048
	var history []providers.Message
	for i := 0; i < 10; i++ {
		history = append(history, providers.Message{Role: "user", Content: fmt.Sprintf("原话%d：菜量小，想继续聊", i)}, providers.Message{Role: "assistant", Content: fmt.Sprintf("接话%d：陪着呢", i)})
	}
	agent.Sessions.SetHistory("fixture-compact", history)
	return &legacyContextManager{al: al}, agent
}

func TestFatfishEmergencySummarizesBeforeRetiringMinimalDialogue(t *testing.T) {
	p := &compactProbeProvider{response: "jg说菜量小，想继续聊天；我接住话题。"}
	m, agent := compactTestLoop(t, p)
	result, ok := m.compactFatfish(context.Background(), "fixture-compact", agent.ContextWindow)
	if !ok || result.DroppedMessages != 2 || result.RemainingMessages != 18 {
		t.Fatalf("retired too much: %+v %v", result, ok)
	}
	if !strings.Contains(p.input, "原话0") || !strings.HasPrefix(agent.Sessions.GetSummary("fixture-compact"), p.response) {
		t.Fatal("semantic summary not retained")
	}
	if p.opts["thinking_level"] != "off" || p.opts["force_thinking_off"] != true || p.opts["max_tokens"] != 2048 {
		t.Fatal("summary is not lightweight")
	}
	if agent.Sessions.GetHistory("fixture-compact")[0].Content != "原话1：菜量小，想继续聊" {
		t.Fatal("recent dialogue changed")
	}
}

func TestFatfishRepeatedSummaryFailureDoesNotNestFallbackHeaders(t *testing.T) {
	existing := summaryFallbackPrefix + "\n" + summaryFallbackPrefix + "\nuser: 之前的话"
	summary := fatfishQuoteFallback(existing, []providers.Message{{Role: "user", Content: "新的话"}})
	if strings.Count(summary, summaryFallbackPrefix) != 1 || !strings.Contains(summary, "之前的话") || !strings.Contains(summary, "新的话") {
		t.Fatal(summary)
	}
}

func TestFatfishSummaryFailureKeepsAttributedQuotes(t *testing.T) {
	p := &compactProbeProvider{err: errors.New("temporary outage")}
	m, agent := compactTestLoop(t, p)
	agent.Sessions.SetSummary("fixture-compact", "旧约定：日常轻松聊天。")
	_, ok := m.compactFatfish(context.Background(), "fixture-compact", agent.ContextWindow)
	summary := agent.Sessions.GetSummary("fixture-compact")
	if !ok || !strings.Contains(summary, "user: 原话0") || !strings.Contains(summary, "assistant: 接话0") || !strings.Contains(summary, "旧约定") || !strings.Contains(summary, "原话摘录") {
		t.Fatal(summary)
	}
}

func TestFatfishCompactionPreservesMessagesArrivingDuringSummary(t *testing.T) {
	p := &compactProbeProvider{response: "保留了之前的聊天"}
	m, agent := compactTestLoop(t, p)
	p.during = func() { agent.Sessions.AddMessage("fixture-compact", "user", "新到的消息也要保留") }
	_, ok := m.compactFatfish(context.Background(), "fixture-compact", agent.ContextWindow)
	history := agent.Sessions.GetHistory("fixture-compact")
	if !ok || len(history) != 19 || history[len(history)-1].Content != "新到的消息也要保留" {
		t.Fatal("new message retired")
	}
}

func TestFatfishSummaryIncludesDialogueAndDeliveryWithoutToolDump(t *testing.T) {
	history := []providers.Message{{Role: "user", Content: "别急着做任务"}, {Role: "assistant", Content: "好，接着聊"},
		{Role: "tool", Content: "PRIVATE_TOOL_DUMP"}, {Role: "tool", Content: "[Output history: delivery facts, not an additional message to the user]\nConfirmed sent text: 嗯" + toolDiagnosticSeparator + "PRIVATE_DETAIL"}}
	input := fmt.Sprint(fatfishConversationSummaryInput(history))
	if !strings.Contains(input, "别急着") || !strings.Contains(input, "Confirmed sent text: 嗯") || strings.Contains(input, "PRIVATE_") {
		t.Fatal(input)
	}
}

func TestFatfishCompactionArchivesOriginalMessagesBeforeSessionRewrite(t *testing.T) {
	p := &compactProbeProvider{response: "简短摘要"}
	m, agent := compactTestLoop(t, p)
	_, ok := m.compactFatfish(context.Background(), "fixture-compact", agent.ContextWindow)
	if !ok {
		t.Fatal("did not compact")
	}
	found := false
	filepath.WalkDir(filepath.Join(agent.Workspace, "state", "context-archive"), func(path string, entry os.DirEntry, err error) error {
		if err == nil && !entry.IsDir() && strings.HasSuffix(path, ".json") {
			raw, _ := os.ReadFile(path)
			if strings.Contains(string(raw), "原话0") {
				found = true
			}
		}
		return nil
	})
	if !found {
		t.Fatal("retired raw conversation was not archived before session rewrite")
	}
}
