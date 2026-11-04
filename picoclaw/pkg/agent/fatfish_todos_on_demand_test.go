package agent

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/sipeed/picoclaw/pkg/providers"
)

func TestFatfishTodosAreOnDemand(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "test-enabled")
	t.Setenv("FATFISH_OWNER_CHAT", "123")
	workspace := t.TempDir()
	core := filepath.Join(t.TempDir(), "core.md")
	if err := os.WriteFile(core, []byte("STRUCTURED_MEMORY_STILL_PRESENT"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FATFISH_STRUCTURED_CORE_FILE", core)
	if err := os.WriteFile(filepath.Join(workspace, "AGENT.md"), []byte("待办需要时调用my_todos读取。"), 0600); err != nil {
		t.Fatal(err)
	}
	snapshot := filepath.Join(t.TempDir(), "todos.json")
	t.Setenv("FATFISH_TODO_SNAPSHOT_FILE", snapshot)
	cb := NewContextBuilder(workspace)
	for _, content := range []string{
		`{"status":"ok","items":[{"text":"PRIVATE_TODO_NOT_REQUESTED"}]}`,
		`{"status":"sync_failed","items":[{"text":"PRIVATE_TODO_NOT_REQUESTED"}]}`,
		`invalid-json`,
	} {
		if err := os.WriteFile(snapshot, []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
		messages := cb.BuildMessagesFromPrompt(PromptBuildRequest{Channel: "telegram", ChatID: "123", CurrentMessage: "随便聊聊"})
		var full strings.Builder
		for _, message := range messages {
			full.WriteString(message.Content)
		}
		text := full.String()
		if strings.Contains(text, "PRIVATE_TODO_NOT_REQUESTED") || strings.Contains(text, "# 我的待办\n") {
			t.Fatal("unrequested todo snapshot entered normal chat context")
		}
		if !strings.Contains(text, "my_todos") || !strings.Contains(text, "STRUCTURED_MEMORY_STILL_PRESENT") {
			t.Fatal("on-demand tool pointer or unrelated memory was removed")
		}
	}
}

// Explicit, read-only deployment audit: assemble an existing history without an LLM call.
func TestFatfishOnDemandContextAudit(t *testing.T) {
	inputPath := os.Getenv("FATFISH_CONTEXT_AUDIT_INPUT")
	outputPath := os.Getenv("FATFISH_CONTEXT_AUDIT_OUTPUT")
	if inputPath == "" || outputPath == "" {
		t.Skip("live context audit not requested")
	}
	var input struct {
		Workspace string              `json:"workspace"`
		Owner     string              `json:"owner"`
		Summary   string              `json:"summary"`
		History   []providers.Message `json:"history"`
	}
	raw, err := os.ReadFile(inputPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, &input); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "test-enabled")
	t.Setenv("FATFISH_OWNER_CHAT", input.Owner)
	cb := NewContextBuilder(input.Workspace)
	messages := cb.BuildMessagesFromPrompt(PromptBuildRequest{Channel: "telegram", ChatID: input.Owner, Summary: input.Summary, History: input.History, CurrentMessage: "上下文容量只读核对"})
	tokens, historyTokens := 0, 0
	for _, message := range messages {
		tokens += EstimateMessageTokens(message)
	}
	for _, message := range foldFatfishToolResults(input.History) {
		historyTokens += EstimateMessageTokens(message)
	}
	system := messages[0].Content
	result := map[string]any{"assembled_message_tokens": tokens, "history_tokens": historyTokens, "history_messages": len(input.History),
		"automatic_todo_section": strings.Contains(system, "# 我的待办\n"), "on_demand_pointer": strings.Contains(system, "my_todos"),
		"message_budget_64k": 65536 - 8192, "tool_definition_tokens_included": false}
	raw, err = json.Marshal(result)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(outputPath, raw, 0600); err != nil {
		t.Fatal(err)
	}
}
