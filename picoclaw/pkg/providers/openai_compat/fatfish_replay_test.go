package openai_compat

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestDeepSeekToolsReplayPlainAndToolTurnReasoningOnce(t *testing.T) {
	p := NewProvider("fixture", "http://127.0.0.1:18791/v1", "")
	p.SetProviderName("deepseek")
	messages := []Message{{Role: "user", Content: "真实原话"}, {Role: "assistant", Content: "已经发出的回答", ReasoningContent: "真实历史思考", HistoryID: "private-id", SourceChatID: "private-chat"}, {Role: "user", Content: "接着做"}}
	tools := []ToolDefinition{{Type: "function", Function: ToolFunctionDefinition{Name: "read_file"}}}
	body := p.buildRequestBody(messages, tools, "deepseek-flash", map[string]any{})
	raw, _ := json.Marshal(body)
	text := string(raw)
	if strings.Count(text, "真实历史思考") != 1 {
		t.Fatalf("reasoning was lost or duplicated: %s", text)
	}
	if strings.Contains(text, "private-id") || strings.Contains(text, "private-chat") {
		t.Fatalf("internal source metadata leaked into provider wire: %s", text)
	}
	without := p.buildRequestBody(messages, nil, "deepseek-flash", map[string]any{})
	raw, _ = json.Marshal(without)
	if strings.Contains(string(raw), "真实历史思考") {
		t.Fatal("non-tools behavior changed")
	}
}
