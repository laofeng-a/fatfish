package agent

import (
	"context"
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
	"github.com/sipeed/picoclaw/pkg/fileutil"
	"github.com/sipeed/picoclaw/pkg/providers"
	"github.com/sipeed/picoclaw/pkg/providers/common"
)

// Explicit private-data audit, never part of ordinary tests. It assembles past
// real owner messages in a copied workspace; no inbound event or Telegram send
// is created. Optional real summary generation stays in that private copy.
func TestFatfishReformPrivateInputAudit(t *testing.T) {
	dir := os.Getenv("FATFISH_CONTEXT_AUDIT_DIR")
	if dir == "" {
		t.Skip("opt-in private snapshot audit")
	}
	cfg, err := config.LoadConfig(filepath.Join(dir, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(cfg.Agents.Defaults.Workspace, dir) {
		t.Fatal("audit must use a copied workspace")
	}
	p, model, err := providers.CreateProvider(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if model != "" {
		cfg.Agents.Defaults.ModelName = model
	}
	b := bus.NewMessageBus()
	defer b.Close()
	al := NewAgentLoop(cfg, b, p)
	defer al.Close()
	if err = al.ensureMCPInitialized(context.Background()); err != nil {
		t.Fatal(err)
	}
	m, ok := al.contextManager.(*fatfishHistoryManager)
	if !ok {
		t.Fatal("new context manager not active")
	}
	a := al.registry.GetDefaultAgent()
	key := os.Getenv("FATFISH_CONTEXT_AUDIT_SESSION")
	if key == "" {
		t.Fatal("real source session required")
	}
	if _, err = m.sync(a, key, true); err != nil {
		t.Fatal(err)
	}
	if os.Getenv("FATFISH_CONTEXT_AUDIT_PROTOCOL") == "1" {
		projected, _, e := m.project(key)
		if e != nil {
			t.Fatal(e)
		}
		msgs := []providers.Message{{Role: "system", Content: "服务器内部工具协议验收。保留历史仅用于检查协议，本次不接续其中话题。按最新测试要求调用context_probe，随后回答验收完成。"}}
		for i := len(projected) - 1; i > 0; i-- {
			if projected[i].Role == "assistant" && projected[i].ReasoningContent != "" && len(projected[i].ToolCalls) == 0 {
				msgs = append(msgs, projected[i-1], projected[i])
				break
			}
		}
		if len(msgs) != 3 {
			t.Fatal("real recorded plain reasoning source required")
		}
		msgs = append(msgs, providers.Message{Role: "user", Content: "这是服务器内部协议测试，不是jg的聊天。请调用context_probe，参数value为1，然后根据工具返回回答验收完成。"})
		defs := []providers.ToolDefinition{{Type: "function", Function: providers.ToolFunctionDefinition{Name: "context_probe", Description: "内部协议探针，只返回固定验收结果，不访问外部应用。", Parameters: map[string]any{"type": "object", "properties": map[string]any{"value": map[string]any{"type": "integer"}}, "required": []string{"value"}}}}}
		ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		defer cancel()
		opts := map[string]any{"max_tokens": 2048, "temperature": 0.0, "thinking_level": "low"}
		first, e := a.Provider.Chat(ctx, msgs, defs, a.Model, opts)
		if e != nil {
			t.Fatal(e)
		}
		if first == nil || len(first.ToolCalls) == 0 {
			t.Fatal("real provider did not return probe tool call")
		}
		// Match Pipeline.CallLLM's provider-response to persisted-message step.
		var wireCalls []providers.ToolCall
		for _, rawCall := range first.ToolCalls {
			call := providers.NormalizeToolCall(rawCall)
			arguments, _ := json.Marshal(call.Arguments)
			wireCalls = append(wireCalls, providers.ToolCall{ID: call.ID, Type: "function", Function: &providers.FunctionCall{Name: call.Name, Arguments: string(arguments)}})
		}
		msgs = append(msgs, providers.Message{Role: "assistant", Content: first.Content, ReasoningContent: first.ReasoningContent, ToolCalls: wireCalls})
		var ids []string
		for _, call := range first.ToolCalls {
			name := call.Name
			if call.Function != nil {
				name = call.Function.Name
			}
			if name != "context_probe" {
				t.Fatal("unexpected test tool")
			}
			ids = append(ids, call.ID)
			msgs = append(msgs, providers.Message{Role: "tool", ToolCallID: call.ID, Content: `{"status":"ok","value":1}`})
		}
		second, e := a.Provider.Chat(ctx, msgs, defs, a.Model, opts)
		if e != nil {
			t.Fatal(e)
		}
		if second == nil || second.FinishReason != "stop" || second.Content == "" {
			t.Fatal("tool continuation did not finish")
		}
		receipt := map[string]any{"kind": "real_provider_internal_protocol_fixture", "provider": fmt.Sprintf("%T", a.Provider), "model": a.Model, "plain_reasoning_replayed": true, "tool_call_ids": ids, "first_finish": first.FinishReason, "second_finish": second.FinishReason, "telegram_messages_sent": 0, "first": first, "second": second, "continuation_messages": common.SerializeMessages(msgs)}
		raw, _ := json.MarshalIndent(receipt, "", "  ")
		if e = fileutil.WriteFileAtomic(filepath.Join(dir, "provider-protocol.json"), raw, 0o600); e != nil {
			t.Fatal(e)
		}
	}
	if os.Getenv("FATFISH_CONTEXT_AUDIT_LIVE_SUMMARY") == "1" {
		if err = m.generate(context.Background(), key); err != nil {
			t.Fatal(err)
		}
	}
	turns, err := m.store.Turns(key)
	if err != nil {
		t.Fatal(err)
	}
	var real []contextledger.Turn
	for _, turn := range turns {
		if turn.Kind == "human" && turn.Complete && turn.MessageID != "" {
			real = append(real, turn)
		}
	}
	if len(real) < 3 {
		t.Fatal("three actual received owner turns are required")
	}
	history, st, err := m.project(key)
	if err != nil {
		t.Fatal(err)
	}
	for i, target := range real[len(real)-3:] {
		var previous []providers.Message
		for _, msg := range history {
			if msg.HistoricalTurn > 0 && msg.HistoricalTurn < target.Number {
				previous = append(previous, msg)
			}
		}
		current := target.User
		for _, msg := range target.Messages {
			if msg.Role == "user" {
				current = msg.Content
				break
			}
		}
		scope := fatfishScope(a, key)
		messages := a.ContextBuilder.BuildMessagesFromPrompt(PromptBuildRequest{History: previous, Summary: st.Body, CurrentMessage: current, Channel: scope.Channel, ChatID: scope.Chat})
		defs := a.Tools.ToProviderDefs()
		messages, err = m.Admit(context.Background(), key, messages, defs, false)
		if err != nil {
			t.Fatal(err)
		}
		n, blocks := fatfishCountInput(messages, defs)
		var numbers []int64
		seen := map[int64]bool{}
		for _, msg := range messages {
			if msg.HistoricalTurn > 0 && !seen[msg.HistoricalTurn] {
				numbers = append(numbers, msg.HistoricalTurn)
				seen[msg.HistoricalTurn] = true
			}
		}
		wire := map[string]any{"model": a.Model, "messages": common.SerializeMessages(messages), "tools": defs, "max_tokens": a.MaxTokens}
		raw, e := json.MarshalIndent(map[string]any{"source": "real_received_turn_replay_not_new_traffic", "source_message_id": target.MessageID, "source_number": target.Number, "source_time": target.At, "observed_at": time.Now().Format(time.RFC3339), "summary_version": st.Version, "covered_through": st.Covered, "history_numbers": numbers, "input_estimate": n, "input_budget": a.ContextWindow - a.MaxTokens, "blocks": blocks, "messages": messages, "wire": wire}, "", "  ")
		if e != nil {
			t.Fatal(e)
		}
		if err = fileutil.WriteFileAtomic(filepath.Join(dir, []string{"input-1.json", "input-2.json", "input-3.json"}[i]), raw, 0o600); err != nil {
			t.Fatal(err)
		}
		if i == 2 {
			if err = fileutil.WriteFileAtomic(filepath.Join(dir, "one-round.txt"), []byte(contextledger.Render(target)), 0o600); err != nil {
				t.Fatal(err)
			}
		}
	}
	receipt := map[string]any{"numbered_turns": len(real), "summary_version": st.Version, "covered_through": st.Covered, "visible_tools": len(a.Tools.ToProviderDefs()), "context_manager": "fatfish", "telegram_messages_sent": 0, "new_inbound_events": 0}
	raw, _ := json.MarshalIndent(receipt, "", "  ")
	if err = fileutil.WriteFileAtomic(filepath.Join(dir, "receipt.json"), raw, 0o600); err != nil {
		t.Fatal(err)
	}
	if os.Getenv("FATFISH_CONTEXT_AUDIT_HOLD") == "1" {
		t.Fatal("audit hold requested")
	}
}

// A positive source-validation control uses one actual recorded conversation
// in a separate private replay session, without the unrelated legacy summary.
// Its numbering and outcome must never be reported as a live production turn.
func TestFatfishReformRealProviderPositiveBatch(t *testing.T) {
	dir := os.Getenv("FATFISH_CONTEXT_AUDIT_DIR")
	if dir == "" {
		t.Skip("opt-in real-provider source control")
	}
	cfg, err := config.LoadConfig(filepath.Join(dir, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	p, model, err := providers.CreateProvider(cfg)
	if err != nil {
		t.Fatal(err)
	}
	source, err := contextledger.Open(filepath.Join(dir, "readers/turn-context.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer source.Close()
	key := os.Getenv("FATFISH_CONTEXT_AUDIT_SESSION")
	turns, err := source.Turns(key)
	if err != nil {
		t.Fatal(err)
	}
	var original contextledger.Turn
	for _, turn := range turns {
		if turn.Number == 526 && turn.Kind == "human" {
			original = turn
			break
		}
	}
	if original.Number == 0 {
		t.Fatal("recorded real source required")
	}
	store, err := contextledger.Open(filepath.Join(dir, "positive-control.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	scope := contextledger.Scope{Session: "isolated-real-source-positive-control", Channel: "telegram", Chat: "private-replay"}
	var inputs []contextledger.Input
	for _, msg := range original.Messages {
		origin := contextledger.Origin{}
		if msg.Role == "user" {
			origin = contextledger.Origin{Key: "actual-source:" + original.MessageID, Kind: "human", MessageID: original.MessageID, Text: original.User, At: time.Unix(0, int64(original.At*1e9))}
		}
		inputs = append(inputs, contextledger.Input{Message: msg, Origin: origin})
	}
	if err = store.Sync(scope, inputs, true); err != nil {
		t.Fatal(err)
	}
	if _, err = store.DB.Exec("UPDATE context_turns SET deliveries=? WHERE session=?", mustAuditJSON(original.Deliveries), scope.Session); err != nil {
		t.Fatal(err)
	}
	actual, err := store.Turns(scope.Session)
	if err != nil {
		t.Fatal(err)
	}
	previous, err := store.State(scope.Session)
	if err != nil {
		t.Fatal(err)
	}
	material := contextledger.Material{Previous: previous, Past: actual, From: 1, To: 1, MaxNumber: 1}
	raw, _ := json.Marshal(material)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	response, err := p.Chat(ctx, []providers.Message{{Role: "system", Content: fatfishSummaryPrompt}, {Role: "user", Content: string(raw)}}, nil, model, map[string]any{"max_tokens": 4096, "temperature": 0.2, "force_thinking_off": true, "thinking_level": "off"})
	if err != nil {
		t.Fatal(err)
	}
	candidate, err := contextledger.ParseCandidate(response.Content)
	if err != nil {
		t.Fatal(err)
	}
	body, err := contextledger.Validate(candidate, material, response.FinishReason)
	if err != nil {
		t.Fatal(err)
	}
	version, err := store.RecordCandidate(scope.Session, material, candidate, response.Content, body, "", true, 10*time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	receipt := map[string]any{"kind": "real_provider_isolated_real_source_positive_control", "original_round": 526, "isolated_replay_round": 1, "summary_version": version, "covered_through": 1, "body": body, "candidate": candidate, "response": response, "new_inbound_events": 0, "telegram_messages_sent": 0}
	if err = fileutil.WriteFileAtomic(filepath.Join(dir, "positive-candidate.json"), []byte(mustAuditJSON(receipt)), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Logf("real provider positive control accepted version=%d covered=1 chars=%d", version, len([]rune(body)))
}

func mustAuditJSON(value any) string {
	raw, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	return string(raw)
}
