package agent

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/providers"
)

func TestFatfishTurnEventUsesMetadataAndUpdatedSteering(t *testing.T) {
	t.Setenv("FATFISH_OWNER_CHAT", "123")
	for _, tc := range []struct{ name, id, sender, wake, want string }{
		{"human", "5622", "123", "", "human_message"},
		{"task_human", "5624", "123", "task", "human_message"},
		{"callback_with_original_human_id", "5622", "async:lookup", "", "tool_callback"},
		{"executor", "codex-event:abc", "123", "", "executor_receipt"},
		{"autonomy", "heartbeat:a", "heartbeat", "autonomy", "autonomy"},
		{"companion", "heartbeat:chat", "heartbeat", "chat", "companion_tick"},
		{"patrol", "heartbeat:b", "heartbeat", "task", "task_patrol"},
		{"quota_for_task", "heartbeat:c", "heartbeat", "quota", "quota_reset"},
		{"diary", "heartbeat:d", "heartbeat", "diary", "diary_maintenance"},
		{"alarm", "heartbeat:e", "heartbeat", "alarm", "silent_alarm"},
		{"unknown_wakeup", "heartbeat:f", "heartbeat", "", "scheduled_wakeup"},
		{"independent_reminder", "", "cron", "", "cron"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ts := &turnState{opts: processOptions{Dispatch: DispatchRequest{InboundContext: &bus.InboundContext{MessageID: tc.id, SenderID: tc.sender}}}}
			messages := []providers.Message{{Role: "user", Content: "我是时钟，哔哔哔"}}
			got := withFatfishTurnEvent(ts, messages, tc.wake)
			if !strings.Contains(got[1].Content, `"kind":"`+tc.want+`"`) || got[0].Content != messages[0].Content || len(messages) != 1 {
				t.Fatalf("source guessed from wording or input changed: %+v", got)
			}
			if tc.name == "companion" && (strings.Contains(got[1].Content, "两分钟") || !strings.Contains(got[1].Content, "自主陪聊机会")) {
				t.Fatalf("companion event contains stale interval: %s", got[1].Content)
			}
		})
	}
	ts := &turnState{opts: processOptions{Dispatch: DispatchRequest{InboundContext: &bus.InboundContext{MessageID: "heartbeat:a", SenderID: "heartbeat"}}}}
	ts.adoptSteeringSource(providers.Message{SteeringMessageID: "5624", SteeringSenderID: "123"})
	got := withFatfishTurnEvent(ts, nil, "autonomy")
	if !strings.Contains(got[0].Content, `"kind":"human_message"`) || !strings.Contains(got[0].Content, `"message_id":"5624"`) {
		t.Fatal("steering kept old wake identity")
	}
}

func TestFatfishTurnEventKeepsToolProtocolAndOneView(t *testing.T) {
	ts := &turnState{opts: processOptions{Dispatch: DispatchRequest{InboundContext: &bus.InboundContext{MessageID: "5622", SenderID: "123"}}}}
	input := []providers.Message{{Role: "user", Content: "？"}, {Role: "assistant", ToolCalls: []providers.ToolCall{{ID: "one", Name: "lookup"}}}, {Role: "tool", ToolCallID: "one", Content: "真实结果"}}
	before, _ := json.Marshal(input)
	once := withFatfishTurnEvent(ts, input, "")
	twice := withFatfishTurnEvent(ts, once, "")
	got, _ := json.Marshal(twice[:len(input)])
	if string(got) != string(before) || len(twice) != len(input)+1 || !strings.Contains(twice[len(input)].Content, "tool_result_continuation") {
		t.Fatal("tool pair changed or event duplicated")
	}
}

func TestFatfishTurnEventActualProviderEveryCallAndNoHistoryPollution(t *testing.T) {
	t.Setenv("FATFISH_OWNER_CHAT", "123")
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "")
	calls := 0
	provider := &reformProvider{fn: func(messages []providers.Message) (*providers.LLMResponse, error) {
		calls++
		events := 0
		for _, m := range messages {
			if strings.HasPrefix(m.Content, fatfishTurnEventHeader+"\n") {
				events++
				if !strings.Contains(m.Content, `"kind":"human_message"`) || !strings.Contains(m.Content, `"message_id":"5622"`) || (calls == 2 && !strings.Contains(m.Content, "tool_result_continuation")) {
					t.Fatalf("actual request source lost on call %d: %s", calls, m.Content)
				}
			}
		}
		if events != 1 {
			t.Fatalf("call %d has %d current event views", calls, events)
		}
		if calls == 1 {
			return &providers.LLMResponse{FinishReason: "tool_calls", ToolCalls: []providers.ToolCall{{ID: "one", Name: "loop_probe", Arguments: map[string]any{}}}}, nil
		}
		return &providers.LLMResponse{Content: "接到了，哔哔哔。", FinishReason: "stop"}, nil
	}}
	al, agent, cleanup := newTurnCoordTestLoop(t, provider)
	defer cleanup()
	token := filepath.Join(t.TempDir(), "token")
	os.WriteFile(token, []byte("fixture"), 0600)
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", token)
	old := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = old })
	http.DefaultTransport = loopAuditTransport(func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"allowed":true}`)), Header: make(http.Header)}, nil
	})
	agent.Tools.Register(&loopProbe{run: func() {}})
	opts := makeTestProcessOpts("event-source-test")
	opts.Dispatch = DispatchRequest{SessionKey: "event-source-test", UserMessage: "哔哔哔", InboundContext: &bus.InboundContext{Channel: "telegram", ChatID: "123", SenderID: "123", MessageID: "5622"}}
	ts := newTurnState(agent, opts, turnEventScope{turnID: "event-source", context: newTurnContext(nil, nil, nil)})
	if _, err := al.runTurn(context.Background(), ts, NewPipeline(al)); err != nil || calls != 2 {
		t.Fatalf("calls=%d err=%v", calls, err)
	}
	for _, m := range agent.Sessions.GetHistory(opts.Dispatch.SessionKey) {
		if strings.Contains(m.Content, fatfishTurnEventHeader) {
			t.Fatal("derived event metadata persisted as dialogue")
		}
	}
}

// Opt-in projection of real received messages, with no Telegram or tool execution.
func TestFatfishTurnEventRealInputProjection(t *testing.T) {
	dir := os.Getenv("FATFISH_EVENT_REPLAY_DIR")
	if dir == "" {
		t.Skip("private real inputs not requested")
	}
	for _, id := range []string{"5622", "5624"} {
		var sample struct {
			Messages []providers.Message        `json:"messages"`
			Tools    []providers.ToolDefinition `json:"tool_definitions"`
			SenderID string                     `json:"sender_id"`
		}
		raw, err := os.ReadFile(filepath.Join(dir, "turn-"+id+".json"))
		if err != nil {
			t.Fatal(err)
		}
		if err = json.Unmarshal(raw, &sample); err != nil {
			t.Fatal(err)
		}
		t.Setenv("FATFISH_OWNER_CHAT", sample.SenderID)
		ts := &turnState{opts: processOptions{Dispatch: DispatchRequest{InboundContext: &bus.InboundContext{MessageID: id, SenderID: sample.SenderID}}}}
		messages := withFatfishTurnEvent(ts, sample.Messages, "")
		wire, err := json.Marshal(map[string]any{"messages": messages, "tools": sample.Tools, "source_message_id": id})
		if err != nil {
			t.Fatal(err)
		}
		if err = os.WriteFile(filepath.Join(dir, "candidate-"+id+".json"), wire, 0600); err != nil {
			t.Fatal(err)
		}
	}
}
