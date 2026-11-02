package agent

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Export only under an explicit local audit request; never send or invoke tools.
func TestFatfishStyleExport(t *testing.T) {
	workspace := os.Getenv("FATFISH_AUDIT_WORKSPACE")
	output := os.Getenv("FATFISH_STYLE_EXPORT_OUTPUT")
	if workspace == "" || output == "" {
		t.Skip("style export not requested")
	}
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "test-enabled")
	t.Setenv("FATFISH_OWNER_CHAT", "123456789")
	cb := NewContextBuilder(workspace)
	for _, server := range []mcpServerPromptContributor{{serverName: "reader", toolCount: 26}, {serverName: "deepseek", toolCount: 2}} {
		if err := cb.RegisterPromptContributor(server); err != nil {
			t.Fatal(err)
		}
	}
	chat := os.Getenv("FATFISH_STYLE_EXPORT_CHAT")
	if chat == "" {
		chat = "123456789"
	}
	messages := cb.BuildMessagesFromPrompt(PromptBuildRequest{Channel: "telegram", ChatID: chat, SenderID: "123456789", SenderDisplayName: "jg", CurrentMessage: "style-audit"})
	var system string
	for _, message := range messages {
		if message.Role == "system" {
			system += message.Content
		}
	}
	raw, err := json.Marshal(map[string]string{"system": system, "image_followup": toolImageFollowUpPromptMessage(nil).Content})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(output, raw, 0600); err != nil {
		t.Fatal(err)
	}
	t.Logf("Prompt exported for controlled style evaluation; bytes=%d", len(system))
}

func TestFatfishCoreIsPrivateAndRefreshes(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "test-enabled")
	t.Setenv("FATFISH_OWNER_CHAT", "123")
	workspace := t.TempDir()
	if err := os.MkdirAll(filepath.Join(workspace, "memory"), 0700); err != nil {
		t.Fatal(err)
	}
	core := filepath.Join(workspace, "memory", "MEMORY.md")
	if err := os.WriteFile(core, []byte("PRIVATE_CORE_MARKER"), 0600); err != nil {
		t.Fatal(err)
	}
	cb := NewContextBuilder(workspace)
	build := func(chat string) string {
		messages := cb.BuildMessagesFromPrompt(PromptBuildRequest{Channel: "telegram", ChatID: chat, CurrentMessage: "hello"})
		var content string
		for _, message := range messages {
			content += message.Content
		}
		return content
	}
	if !strings.Contains(build("123"), "PRIVATE_CORE_MARKER") {
		t.Fatal("owner private core was not loaded")
	}
	if strings.Contains(build("-99"), "PRIVATE_CORE_MARKER") {
		t.Fatal("private core leaked into group")
	}
	if err := os.WriteFile(core, []byte("UPDATED_CORE_MARKER"), 0600); err != nil {
		t.Fatal(err)
	}
	updated := build("123")
	if strings.Contains(updated, "PRIVATE_CORE_MARKER") || !strings.Contains(updated, "UPDATED_CORE_MARKER") {
		t.Fatal("core correction did not take effect")
	}
}

func TestFatfishNativeDiaryIsolationAndBounds(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "test-enabled")
	t.Setenv("FATFISH_OWNER_CHAT", "123")
	workspace := t.TempDir()
	now := time.Now().In(time.FixedZone("UTC+8", 8*3600))
	folder := filepath.Join(workspace, "memory", now.Format("200601"))
	if err := os.MkdirAll(folder, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(folder, now.Format("20060102")+".md"), []byte("PRIVATE_DIARY_MARKER"+strings.Repeat("鲸", 1600)), 0600); err != nil {
		t.Fatal(err)
	}
	core := filepath.Join(t.TempDir(), "core.md")
	if err := os.WriteFile(core, []byte("STRUCTURED_MARKER"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FATFISH_STRUCTURED_CORE_FILE", core)
	cb := NewContextBuilder(workspace)
	build := func(chat string) string {
		messages := cb.BuildMessagesFromPrompt(PromptBuildRequest{Channel: "telegram", ChatID: chat, CurrentMessage: "hello"})
		var text string
		for _, m := range messages {
			text += m.Content
		}
		return text
	}
	private := build("123")
	diaryName := filepath.Join(now.Format("200601"), now.Format("20060102")+".md")
	if strings.Contains(private, "PRIVATE_DIARY_MARKER") || !strings.Contains(private, diaryName) || !strings.Contains(private, "STRUCTURED_MARKER") {
		t.Fatal("diary should be indexed on demand while structured core stays present")
	}
	if strings.Contains(private, strings.Repeat("鲸", 700)) {
		t.Fatal("diary exceeds prompt byte budget")
	}
	group := build("-99")
	if strings.Contains(group, "PRIVATE_DIARY_MARKER") || strings.Contains(group, "STRUCTURED_MARKER") || strings.Contains(group, diaryName) {
		t.Fatal("private memory leaked to group")
	}
	if strings.Contains(private, "You are picoclaw") {
		t.Fatal("framework identity overrides SOUL")
	}
}

// Optional server audit uses real prompt files without a model call or printing private content.
func TestFatfishDeployedPrompt(t *testing.T) {
	workspace := os.Getenv("FATFISH_AUDIT_WORKSPACE")
	if workspace == "" {
		t.Skip("live workspace audit not requested")
	}
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "test-enabled")
	t.Setenv("FATFISH_OWNER_CHAT", "123456789")
	cb := NewContextBuilder(workspace)
	definition := cb.LoadAgentDefinition()
	if definition.Agent == nil || definition.Soul == nil || definition.User == nil {
		t.Fatal("native prompt files missing")
	}
	if definition.Agent.FrontmatterErr != "" {
		t.Fatal(definition.Agent.FrontmatterErr)
	}
	messages := cb.BuildMessagesFromPrompt(PromptBuildRequest{Channel: "telegram", ChatID: "123456789", CurrentMessage: "audit"})
	var text string
	for _, m := range messages {
		text += m.Content
	}
	for _, marker := range []string{"不必持续证明某种性格", "jg", "memory_note_write", "先有jg的许可"} {
		if !strings.Contains(text, marker) {
			t.Fatalf("missing prompt marker %s", marker)
		}
	}
	if strings.Contains(text, "自由读写尚未开放") || strings.Contains(text, "You are picoclaw") {
		t.Fatal("obsolete instructions remain")
	}
	t.Logf("Live prompt assembled from AGENT/SOUL/USER; bytes=%d; no model or platform call", len(text))
}
