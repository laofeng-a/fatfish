package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/sipeed/picoclaw/pkg/config"
	"github.com/sipeed/picoclaw/pkg/contextledger"
	"github.com/sipeed/picoclaw/pkg/providers"
	"github.com/sipeed/picoclaw/pkg/session"
	"github.com/sipeed/picoclaw/pkg/tools"
)

type reformProvider struct {
	fn func([]providers.Message) (*providers.LLMResponse, error)
}

func (p *reformProvider) GetDefaultModel() string { return "fixture" }
func (p *reformProvider) Chat(_ context.Context, msgs []providers.Message, _ []providers.ToolDefinition, _ string, _ map[string]any) (*providers.LLMResponse, error) {
	if p.fn != nil {
		return p.fn(msgs)
	}
	return &providers.LLMResponse{Content: "", FinishReason: "length"}, nil
}

func reformFixture(t *testing.T) (*fatfishHistoryManager, *AgentInstance) {
	t.Helper()
	root := t.TempDir()
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", filepath.Join(root, "unused-token"))
	t.Setenv("FATFISH_STRUCTURED_CORE_FILE", filepath.Join(root, "core.md"))
	t.Setenv("FATFISH_OWNER_CHAT", "123")
	if err := os.WriteFile(filepath.Join(root, "core.md"), []byte(strings.Repeat("私有核心记忆", 300)), 0o600); err != nil {
		t.Fatal(err)
	}
	a := &AgentInstance{ID: "main", Workspace: root, ContextWindow: 65536, MaxTokens: 8192, Provider: &reformProvider{}, Sessions: session.NewSessionManager(""), ContextBuilder: NewContextBuilder(root), Tools: tools.NewToolRegistry()}
	al := &AgentLoop{registry: &AgentRegistry{cfg: config.DefaultConfig(), agents: map[string]*AgentInstance{"main": a}}}
	al.activeReqCond = sync.NewCond(&al.activeReqMu)
	cm, err := newFatfishHistoryManager(nil, al)
	if err != nil {
		t.Fatal(err)
	}
	m := cm.(*fatfishHistoryManager)
	a.ContextManager = m
	t.Cleanup(func() { m.Close() })
	return m, a
}

func TestFatfishReformFullBudgetAndContextUsageIncludePrivateMemory(t *testing.T) {
	m, a := reformFixture(t)
	a.Sessions.AddFullMessage("private", providers.Message{Role: "user", Content: "真实原话", SourceKind: "human", SourceChatID: "123", SourceChannel: "telegram", SourceMessageID: "1"})
	if _, err := m.sync(a, "private", true); err != nil {
		t.Fatal(err)
	}
	n, blocks, _, err := m.InputUsage("private")
	if err != nil {
		t.Fatal(err)
	}
	usage := computeContextUsage(a, "private")
	if usage == nil || usage.UsedTokens != n || usage.SummarizeAtTokens != 40140 || usage.CompressAtTokens != 57344 {
		t.Fatalf("inconsistent usage: %+v %d", usage, n)
	}
	if blocks["system"] < 500 || blocks["clock"] == 0 {
		t.Fatalf("private memory or final clock missing: %+v", blocks)
	}
}

func TestFatfishReformCleanHistoryKeepsReasoningOnceAndCurrentToolsRaw(t *testing.T) {
	m, a := reformFixture(t)
	scope := contextledger.Scope{Session: "private", Chat: "123", Channel: "telegram"}
	inputs := []contextledger.Input{{Message: providers.Message{HistoryID: "u1", Role: "user", Content: "真正原话\n系统补充"}, Origin: contextledger.Origin{Key: "u1", Kind: "human", Text: "真正原话", MessageID: "1", At: time.Unix(1700000000, 0)}}, {Message: providers.Message{HistoryID: "a1", Role: "assistant", Content: "未投递的内部文字", ReasoningContent: "真正的思考"}}, {Message: providers.Message{HistoryID: "t1", Role: "tool", ToolCallID: "call", Content: strings.Repeat("工具明细", 1000)}}}
	if err := m.store.Sync(scope, inputs, true); err != nil {
		t.Fatal(err)
	}
	if _, err := m.store.DB.Exec(`UPDATE context_turns SET deliveries='[{"state":"sent","text":"实际发出的回答"}]' WHERE number=1`); err != nil {
		t.Fatal(err)
	}
	history, _, err := m.project("private")
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(history)
	view := string(raw)
	if !strings.Contains(view, "#1") || !strings.Contains(view, "实际发出的回答") || strings.Contains(view, "未投递的内部文字") || strings.Contains(view, "系统补充") {
		t.Fatal(view)
	}
	if strings.Count(view, "真正的思考") != 1 || strings.Contains(view, "工具明细工具明细") {
		t.Fatal("reasoning duplicated or tool dump retained")
	}
	current := providers.Message{HistoryID: "u2", Role: "user", Content: "当前原话"}
	tool := providers.Message{HistoryID: "t2", Role: "assistant", ToolCalls: []providers.ToolCall{{ID: "active-call", Function: &providers.FunctionCall{Name: "read_file", Arguments: `{}`}}}, ReasoningContent: "当前思考"}
	if err = m.store.Sync(scope, []contextledger.Input{{Message: current, Origin: contextledger.Origin{Key: "u2", Kind: "human", Text: "当前原话"}}, {Message: tool}}, false); err != nil {
		t.Fatal(err)
	}
	history, _, err = m.project("private")
	if err != nil {
		t.Fatal(err)
	}
	last := history[len(history)-1]
	if len(last.ToolCalls) != 1 || last.ToolCalls[0].ID != "active-call" || last.ReasoningContent != "当前思考" {
		t.Fatal("active protocol was folded", last)
	}
	_ = a
}

func TestFatfishReformHardBudgetTrimsOnlyCompletedViewAndRetainsCursor(t *testing.T) {
	m, a := reformFixture(t)
	a.ContextWindow = 3000
	a.MaxTokens = 500
	messages := []providers.Message{{Role: "system", Content: "system"}, {Role: "user", Content: strings.Repeat("旧原话", 3000), HistoricalTurn: 1, HistoricalRecord: 1}, {Role: "assistant", Content: "旧回答", HistoricalTurn: 1, HistoricalRecord: 1}, {Role: "user", Content: "当前完整原话"}, {Role: "assistant", ToolCalls: []providers.ToolCall{{ID: "current-call", Function: &providers.FunctionCall{Name: "read_file", Arguments: `{}`}}}}}
	view, err := m.Admit(context.Background(), "private", messages, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	text, _ := json.Marshal(view)
	if strings.Contains(string(text), "旧原话旧原话") || !strings.Contains(string(text), "当前完整原话") || !strings.Contains(string(text), "current-call") || !strings.Contains(string(text), "尚未因此推进") {
		t.Fatal(string(text))
	}
	_, err = m.Admit(context.Background(), "private", []providers.Message{{Role: "user", Content: strings.Repeat("当前工具链", 5000)}}, nil, false)
	if err == nil {
		t.Fatal("oversize protected chain was silently cut")
	}
}

func TestFatfishReformFailedGenerationKeepsOldSummaryAndRawHistory(t *testing.T) {
	m, a := reformFixture(t)
	m.keepRecent = 1
	for i := 1; i <= 4; i++ {
		a.Sessions.AddFullMessage("private", providers.Message{Role: "user", Content: fmt.Sprintf("第%d句真实对话", i), SourceChatID: "123", SourceChannel: "telegram", SourceMessageID: fmt.Sprint(i), SourceKind: "human"})
	}
	if _, err := m.sync(a, "private", true); err != nil {
		t.Fatal(err)
	}
	// Fixture source events are trusted here; no runtime event DB is installed.
	if _, err := m.store.DB.Exec("UPDATE context_turns SET kind='human' WHERE session='private'"); err != nil {
		t.Fatal(err)
	}
	if err := m.store.SeedLegacy(contextledger.Scope{Session: "private"}, "旧摘要必须继续生效"); err != nil {
		t.Fatal(err)
	}
	before, _ := m.store.State("private")
	rawCount := len(a.Sessions.GetHistory("private"))
	if err := m.generate(context.Background(), "private"); err == nil {
		t.Fatal("empty/truncated generation accepted")
	}
	after, _ := m.store.State("private")
	if before.Body != after.Body || before.Covered != after.Covered || before.Version != after.Version || after.Cooldown < float64(time.Now().Add(9*time.Minute).Unix()) || len(a.Sessions.GetHistory("private")) != rawCount {
		t.Fatal("failed generation changed active source", after)
	}
}

func TestFatfishReformBatchLeavesBufferAndStableDetailAfterRestart(t *testing.T) {
	m, a := reformFixture(t)
	key := "batch"
	scope := contextledger.Scope{Session: key, Chat: "123", Channel: "telegram"}
	var inputs []contextledger.Input
	for i := 1; i <= 20; i++ {
		u := providers.Message{HistoryID: fmt.Sprintf("u%d", i), Role: "user", Content: fmt.Sprintf("第%d句真实原话", i)}
		r := providers.Message{HistoryID: fmt.Sprintf("a%d", i), Role: "assistant", ReasoningContent: strings.Repeat("当时真实思考", 1100)}
		a.Sessions.AddFullMessage(key, u)
		a.Sessions.AddFullMessage(key, r)
		inputs = append(inputs, contextledger.Input{Message: u, Origin: contextledger.Origin{Key: u.HistoryID, Kind: "human", MessageID: fmt.Sprint(i), Text: u.Content, At: time.Unix(1700000000+int64(i), 0)}}, contextledger.Input{Message: r})
	}
	if err := m.store.Sync(scope, inputs, true); err != nil {
		t.Fatal(err)
	}
	m.initialized[key] = true
	before, err := m.estimate(key)
	if err != nil {
		t.Fatal(err)
	}
	if before < 40140 {
		t.Fatalf("fixture below high water: %d", before)
	}
	a.Provider = &reformProvider{fn: func(msgs []providers.Message) (*providers.LLMResponse, error) {
		var material contextledger.Material
		if err := json.Unmarshal([]byte(msgs[1].Content), &material); err != nil {
			return nil, err
		}
		candidate := contextledger.Candidate{Summary: "jg逐步交代自己的事情，大肥鱼按当时的理解接续交流；近期原话仍完整保留。", From: material.From, To: material.To}
		raw, _ := json.Marshal(candidate)
		return &providers.LLMResponse{Content: string(raw), FinishReason: "stop"}, nil
	}}
	if err = m.generate(context.Background(), key); err != nil {
		t.Fatal(err)
	}
	after, err := m.estimate(key)
	if err != nil {
		t.Fatal(err)
	}
	if after > 31539 {
		t.Fatalf("batch left insufficient buffer: %d -> %d", before, after)
	}
	st, _ := m.store.State(key)
	if st.Version == 0 || st.Covered < 2 || st.Covered > 14 {
		t.Fatalf("incorrect batch/recent protection: %+v", st)
	}
	path := m.store.Path
	if err = m.store.Close(); err != nil {
		t.Fatal(err)
	}
	m.store, err = contextledger.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	turns, err := m.store.Turns(key)
	if err != nil {
		t.Fatal(err)
	}
	if len(turns) != 20 || turns[0].Number != 1 || turns[19].Number != 20 || turns[0].Messages[0].Content != "第1句真实原话" {
		t.Fatal("numbered original lost after activation/restart")
	}
	t.Logf("full input before=%d after=%d high=40140 target<=31539 covered=%d version=%d", before, after, st.Covered, st.Version)
}

func TestFatfishReformBelowWaterlineDoesNotScheduleTinySummary(t *testing.T) {
	m, a := reformFixture(t)
	key := "small"
	a.Sessions.AddFullMessage(key, providers.Message{Role: "user", Content: "一小段正常聊天", SourceKind: "human", SourceChatID: "123", SourceChannel: "telegram", SourceMessageID: "1"})
	if _, err := m.sync(a, key, true); err != nil {
		t.Fatal(err)
	}
	called := make(chan bool, 1)
	a.Provider = &reformProvider{fn: func([]providers.Message) (*providers.LLMResponse, error) {
		called <- true
		return &providers.LLMResponse{FinishReason: "stop", Content: "unused"}, nil
	}}
	m.schedule(key)
	m.mu.Lock()
	running := len(m.running)
	m.mu.Unlock()
	if running != 0 {
		t.Fatal("small conversation started a background summary")
	}
	select {
	case <-called:
		t.Fatal("provider was called below high water")
	default:
	}
}

func TestFatfishReformLegacyBodyIsLabeledWithoutChangingStoredVersion(t *testing.T) {
	m, _ := reformFixture(t)
	scope := contextledger.Scope{Session: "legacy", Chat: "123", Channel: "telegram"}
	if err := m.store.Sync(scope, nil, true); err != nil {
		t.Fatal(err)
	}
	if err := m.store.SeedLegacy(scope, "旧策略留下的记录"); err != nil {
		t.Fatal(err)
	}
	_, view, err := m.project(scope.Session)
	if err != nil {
		t.Fatal(err)
	}
	stored, err := m.store.State(scope.Session)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(view.Body, "尚未经新的摘要来源验收") || stored.Body != "旧策略留下的记录" || stored.Version != 0 || stored.Covered != 0 {
		t.Fatal("legacy source was promoted or altered", view, stored)
	}
}

func TestFatfishReformQuoteRepairIsBoundedAndKeepsBothDrafts(t *testing.T) {
	for _, succeeds := range []bool{true, false} {
		t.Run(fmt.Sprintf("repair_success_%t", succeeds), func(t *testing.T) {
			m, a := reformFixture(t)
			m.keepRecent = 1
			key := "repair"
			for i := 1; i <= 3; i++ {
				a.Sessions.AddFullMessage(key, providers.Message{Role: "user", Content: fmt.Sprintf("真实原话%d", i), SourceKind: "human", SourceChatID: "123", SourceChannel: "telegram", SourceMessageID: fmt.Sprint(i)})
			}
			if _, err := m.sync(a, key, true); err != nil {
				t.Fatal(err)
			}
			if _, err := m.store.DB.Exec("UPDATE context_turns SET kind='human' WHERE session=?", key); err != nil {
				t.Fatal(err)
			}
			if err := m.store.SeedLegacy(contextledger.Scope{Session: key}, "旧摘要仍然保留"); err != nil {
				t.Fatal(err)
			}
			before, _ := m.store.State(key)
			rawCount := len(a.Sessions.GetHistory(key))
			calls := 0
			a.Provider = &reformProvider{fn: func(messages []providers.Message) (*providers.LLMResponse, error) {
				calls++
				var material contextledger.Material
				if err := json.Unmarshal([]byte(messages[1].Content), &material); err != nil {
					return nil, err
				}
				if calls == 2 && (len(messages) != 4 || !strings.Contains(messages[3].Content, "未记录的原话内容")) {
					t.Fatal("repair did not receive the exact validation failure")
				}
				c := contextledger.Candidate{From: material.From, To: material.To, Summary: "jg说“未记录的原话内容”。"}
				if calls == 2 && succeeds {
					c.Summary = "jg说清了实际需要，随后双方接着交流。"
				}
				raw, _ := json.Marshal(c)
				return &providers.LLMResponse{Content: string(raw), FinishReason: "stop"}, nil
			}}
			err := m.generate(context.Background(), key)
			if (err == nil) != succeeds || calls != 2 {
				t.Fatalf("result=%v provider_calls=%d", err, calls)
			}
			var total, rejected int
			m.store.DB.QueryRow("SELECT count(*),sum(status='rejected') FROM context_summaries WHERE session=?", key).Scan(&total, &rejected)
			if total != 2 || rejected < 1 {
				t.Fatal("a draft or rejection was discarded", total, rejected)
			}
			after, _ := m.store.State(key)
			if succeeds {
				if after.Version == 0 || after.Covered <= before.Covered {
					t.Fatal("valid repair did not activate")
				}
			} else if after.Body != before.Body || after.Version != before.Version || after.Covered != before.Covered || after.Cooldown < float64(time.Now().Add(9*time.Minute).Unix()) || len(a.Sessions.GetHistory(key)) != rawCount {
				t.Fatal("failed repair changed the active source", after)
			}
		})
	}
}
