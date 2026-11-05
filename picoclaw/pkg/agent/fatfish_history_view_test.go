package agent

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/config"
	"github.com/sipeed/picoclaw/pkg/contextledger"
	"github.com/sipeed/picoclaw/pkg/providers"
)

// Opt-in private copy only: replay a real received utterance without Telegram
// or executable tools. Save actual provider input and output for inspection.
func TestFatfishHistoryRealReplay(t *testing.T) {
	dir := os.Getenv("FATFISH_HISTORY_VIEW_AUDIT_DIR")
	if dir == "" {
		t.Skip("private replay not requested")
	}
	cfg, err := config.LoadConfig(filepath.Join(dir, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(cfg.Agents.Defaults.Workspace, dir+string(os.PathSeparator)) {
		t.Fatal("copied workspace required")
	}
	p, model, err := providers.CreateProvider(cfg)
	if err != nil {
		t.Fatal(err)
	}
	b := bus.NewMessageBus()
	defer b.Close()
	al := NewAgentLoop(cfg, b, p)
	defer al.Close()
	m, ok := al.contextManager.(*fatfishHistoryManager)
	if !ok {
		t.Fatal("fatfish manager required")
	}
	a := al.registry.GetDefaultAgent()
	key := os.Getenv("FATFISH_CONTEXT_AUDIT_SESSION")
	if _, err = m.sync(a, key, true); err != nil {
		t.Fatal(err)
	}
	history, st, err := m.project(key)
	if err != nil {
		t.Fatal(err)
	}
	turns, err := m.store.Turns(key)
	if err != nil {
		t.Fatal(err)
	}
	var target contextledger.Turn
	for _, turn := range turns {
		if turn.Kind == "human" && turn.Complete {
			target = turn
		}
	}
	if target.Number == 0 {
		t.Fatal("real completed turn required")
	}
	var previous []providers.Message
	for _, msg := range history {
		if msg.HistoricalTurn > 0 && msg.HistoricalTurn < target.Number {
			previous = append(previous, msg)
		}
	}
	messages := a.ContextBuilder.BuildMessagesFromPrompt(PromptBuildRequest{History: previous, Summary: st.Body, CurrentMessage: target.User, Channel: "telegram", ChatID: os.Getenv("FATFISH_OWNER_CHAT")})
	for _, msg := range messages {
		if msg.Role == "assistant" && (strings.HasPrefix(msg.Content, "大肥鱼：") || strings.Contains(msg.Content, fatfishEchoedHistoryNote)) {
			t.Fatal("polluted history in actual input")
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	if model == "" {
		model = a.Model
	}
	response, err := a.Provider.Chat(ctx, messages, nil, model, map[string]any{"max_tokens": 1000, "temperature": .2, "thinking_level": "off", "force_thinking_off": true})
	if err != nil {
		t.Fatal(err)
	}
	if response == nil || strings.TrimSpace(response.Content) == "" {
		t.Fatal("empty replay response")
	}
	passed := !strings.HasPrefix(strings.TrimSpace(response.Content), "大肥鱼：") && !strings.Contains(response.Content, fatfishEchoedHistoryNote)
	raw, _ := json.MarshalIndent(map[string]any{"passed": passed, "source_number": target.Number, "messages": messages, "response": response, "telegram_messages_sent": 0, "new_inbound_events": 0}, "", "  ")
	if err = os.WriteFile(filepath.Join(dir, "real-replay.json"), raw, 0600); err != nil {
		t.Fatal(err)
	}
	if !passed {
		t.Fatal("provider copied history formatting")
	}
}

func TestFatfishHistoryAssistantContentIsOnlyDeliveredSpeech(t *testing.T) {
	m, _ := reformFixture(t)
	scope := contextledger.Scope{Session: "view", Chat: "123", Channel: "telegram"}
	inputs := []contextledger.Input{
		{Message: providers.Message{HistoryID: "u", Role: "user", Content: "等我回来"}, Origin: contextledger.Origin{Key: "u", Kind: "human", Text: "等我回来", MessageID: "1"}},
		{Message: providers.Message{HistoryID: "a", Role: "assistant", ReasoningContent: "当时真实推断"}},
		{Message: providers.Message{HistoryID: "tool", Role: "tool", ToolCallID: "call", Content: `{"status":"completed"}`}},
	}
	if err := m.store.Sync(scope, inputs, true); err != nil {
		t.Fatal(err)
	}
	deliveries := `[{"state":"sent","text":"好，等你。"},{"state":"sent","text":"回来再接着说。"},{"state":"unknown","text":"不确定是否发出的正文"}]`
	if _, err := m.store.DB.Exec("UPDATE context_turns SET deliveries=? WHERE session='view'", deliveries); err != nil {
		t.Fatal(err)
	}
	history, _, err := m.project("view")
	if err != nil {
		t.Fatal(err)
	}
	if len(history) != 2 {
		t.Fatalf("history=%+v", history)
	}
	a := history[1]
	if a.Role != "assistant" || a.Content != "好，等你。\n\n回来再接着说。" || a.ReasoningContent != "当时真实推断" {
		t.Fatalf("assistant voice polluted: %+v", a)
	}
	if !strings.Contains(history[0].Content, "<历史回合资料>") || !strings.Contains(history[0].Content, "unknown") || !strings.Contains(history[0].Content, "工具结果：") {
		t.Fatal("record metadata lost", history[0])
	}
	var unchanged string
	if err = m.store.DB.QueryRow("SELECT deliveries FROM context_turns WHERE session='view'").Scan(&unchanged); err != nil {
		t.Fatal(err)
	}
	if unchanged != deliveries {
		t.Fatal("raw delivery archive changed")
	}
	turns, err := m.store.Turns("view")
	if err != nil {
		t.Fatal(err)
	}
	if len(turns[0].Messages) != 3 || turns[0].Reasoning[0] != "当时真实推断" {
		t.Fatal("raw tool or reasoning lost")
	}
	raw, _ := json.Marshal(history)
	if strings.Contains(string(raw), fatfishEchoedHistoryNote) {
		t.Fatal("synthetic thinking cue in model input")
	}
}

func TestFatfishHistoryEchoCleanupIsFingerprintBounded(t *testing.T) {
	polluted := "大肥鱼：好，等你。\n" + fatfishEchoedHistoryNote
	hash := fmt.Sprintf("%x", sha256.Sum256([]byte(polluted)))
	fatfishHistoryEchoes[hash] = true
	defer delete(fatfishHistoryEchoes, hash)
	if actual := fatfishDeliveredView(polluted); actual != "好，等你。" {
		t.Fatal(actual)
	}
	for _, legitimate := range []string{"大肥鱼：这是一句台词\njg：下一句", "示例：\n```\n" + fatfishEchoedHistoryNote + "\n```", "我当时说了‘大肥鱼：你好’。"} {
		if actual := fatfishDeliveredView(legitimate); actual != legitimate {
			t.Fatal("legitimate quote altered", actual)
		}
	}
}
