package agent

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeTodoFixture(t *testing.T, path, status, at, text string) {
	t.Helper()
	raw, err := json.Marshal(map[string]any{"status": status, "synced_at": at, "source_url": "https://notion.so/test", "items": []map[string]any{{"id": "todo-1", "text": text, "parents": []string{"项目"}}}})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
}

func TestFatfishTodosPrivateFreshAndReadEveryTurn(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "test-enabled")
	t.Setenv("FATFISH_OWNER_CHAT", "123")
	path := filepath.Join(t.TempDir(), "todos.json")
	t.Setenv("FATFISH_TODO_SNAPSHOT_FILE", path)
	writeTodoFixture(t, path, "ok", time.Now().Format(time.RFC3339Nano), "待办原话甲")
	cb := NewContextBuilder(t.TempDir())
	build := func(chat string) string {
		messages := cb.BuildMessagesFromPrompt(PromptBuildRequest{Channel: "telegram", ChatID: chat, CurrentMessage: "hello"})
		var out strings.Builder
		for _, message := range messages {
			out.WriteString(message.Content)
		}
		return out.String()
	}
	if content := build("123"); !strings.Contains(content, "# 我的待办") || !strings.Contains(content, "待办原话甲") {
		t.Fatal("private owner prompt omitted todos")
	}
	if content := build("-99"); strings.Contains(content, "待办原话甲") || strings.Contains(content, "# 我的待办") {
		t.Fatal("personal todos leaked into group prompt")
	}
	writeTodoFixture(t, path, "ok", time.Now().Format(time.RFC3339Nano), "待办更新乙")
	if content := build("123"); !strings.Contains(content, "待办更新乙") || strings.Contains(content, "待办原话甲") {
		t.Fatal("snapshot update was hidden behind the static prompt cache")
	}
}

func TestFatfishTodoFailureAndStalenessAreVisible(t *testing.T) {
	path := filepath.Join(t.TempDir(), "todos.json")
	t.Setenv("FATFISH_TODO_SNAPSHOT_FILE", path)
	if text := fatfishTodoContext(); !strings.Contains(text, "暂不可用") || strings.Contains(text, "没有未完成事项") {
		t.Fatal("missing snapshot treated as an empty list")
	}
	for _, status := range []string{"ok", "sync_failed"} {
		writeTodoFixture(t, path, status, time.Now().Add(-time.Hour).Format(time.RFC3339Nano), "旧的待办")
		text := fatfishTodoContext()
		if !strings.Contains(text, "已过期") || !strings.Contains(text, "旧的待办") {
			t.Fatal("stale snapshot lost its contents or freshness warning")
		}
	}
}

func TestFatfishTodoLongListExplicitlyReportsOmissions(t *testing.T) {
	path := filepath.Join(t.TempDir(), "todos.json")
	t.Setenv("FATFISH_TODO_SNAPSHOT_FILE", path)
	items := make([]map[string]string, 100)
	for i := range items {
		items[i] = map[string]string{"text": strings.Repeat("长任务", 300)}
	}
	raw, _ := json.Marshal(map[string]any{"status": "ok", "synced_at": time.Now().Format(time.RFC3339Nano), "items": items})
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	text := fatfishTodoContext()
	if !strings.Contains(text, "未在简表展开") || !strings.Contains(text, "原文见my_todos") || len(text) > 25000 {
		t.Fatal("large checklist was silently dropped or unbounded")
	}
}
