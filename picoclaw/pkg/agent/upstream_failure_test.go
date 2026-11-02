package agent

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/fatfish"
	"github.com/sipeed/picoclaw/pkg/providers"
	"github.com/sipeed/picoclaw/pkg/providers/common"
)

func Test403RetryClassificationPreservesPermanentErrors(t *testing.T) {
	for _, row := range []struct {
		name, body string
		retry      bool
	}{
		{"unpurchase", "AccessDenied.Unpurchased model access denied", true},
		{"gateway_auth", "invalid_api_key authentication failed", true},
		{"generic_type", "{\"error\":{\"code\":\"AccessDenied.Unpurchased\",\"type\":\"invalid_request_error\",\"message\":\"model access denied\"}}", true},
		{"risk_control", "risk_control gateway denied", true},
		{"html_edge", "<html>403 Forbidden</html>", true},
		{"content", "content_policy_violation", false},
		{"inspection", "Data_Inspection_Failed 内容审核未通过", false},
		{"parameter", "InvalidParameter.Range 参数错误", false},
	} {
		t.Run(row.name, func(t *testing.T) {
			err := &common.HTTPError{StatusCode: 403, UpstreamStatus: 403, UpstreamBody: row.body}
			_, got := transientLLMRetryReason(err)
			if got != row.retry {
				t.Fatalf("retry=%v want=%v", got, row.retry)
			}
		})
	}
	_, retry := transientLLMRetryReason(errors.New("API request failed: Status: 403 Body: edge denied"))
	if !retry {
		t.Fatal("legacy status-only 403 lost retry")
	}
}

type noticeProbeProvider struct {
	sequenceProvider
	beforeCall func(int)
}

func (p *noticeProbeProvider) Chat(ctx context.Context, m []providers.Message, d []providers.ToolDefinition, model string, opts map[string]any) (*providers.LLMResponse, error) {
	if p.beforeCall != nil {
		p.beforeCall(p.callCount)
	}
	return p.sequenceProvider.Chat(ctx, m, d, model, opts)
}
func faultTestTurn(t *testing.T, p *noticeProbeProvider, session string) (*AgentLoop, *turnState, func()) {
	t.Helper()
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", t.TempDir()+"/missing-runtime-token")
	al, agent, cleanup := newTurnCoordTestLoop(t, p)
	al.cfg.Agents.Defaults.MaxLLMRetries = 2
	al.cfg.Agents.Defaults.LLMRetryBackoffSecs = 1
	opts := makeTestProcessOpts(session)
	opts.Channel = "telegram"
	opts.ChatID = "123"
	opts.Dispatch.InboundContext = &bus.InboundContext{Channel: "telegram", ChatID: "123", MessageID: "fault-message", SenderID: "123"}
	ts := newTurnState(agent, opts, turnEventScope{turnID: session, context: newTurnContext(nil, nil, nil)})
	return al, ts, cleanup
}
func retry403() error {
	return &common.HTTPError{StatusCode: 403, UpstreamStatus: 403, UpstreamCode: "AccessDenied.Unpurchased", UpstreamBody: "model access denied"}
}

func Test403NoticePrecedesRetryAndFinalStillDelivered(t *testing.T) {
	p := &noticeProbeProvider{sequenceProvider: sequenceProvider{errors: []error{retry403(), retry403(), nil}}}
	al, ts, cleanup := faultTestTurn(t, p, "403-recovery")
	defer cleanup()
	p.beforeCall = func(index int) {
		if index > 0 && len(al.bus.(*bus.MessageBus).OutboundChan()) != 1 {
			t.Fatal("retry started before one notice was queued")
		}
	}
	result, err := al.runTurn(context.Background(), ts, NewPipeline(al))
	if err != nil || p.callCount != 3 || result.finalContent != "ok" {
		t.Fatalf("calls=%d result=%q err=%v", p.callCount, result.finalContent, err)
	}
	al.PublishResponseIfNeeded(context.Background(), "telegram", "123", ts.sessionKey, result.finalContent)
	first := <-al.bus.(*bus.MessageBus).OutboundChan()
	second := <-al.bus.(*bus.MessageBus).OutboundChan()
	if !strings.Contains(first.Content, "正在重试") || first.Context.Raw["delivery_origin"] != "runtime_notice" || second.Content != "ok" {
		t.Fatalf("notice/final order: %q %q", first.Content, second.Content)
	}
	select {
	case msg := <-al.bus.(*bus.MessageBus).OutboundChan():
		t.Fatalf("extra output: %q", msg.Content)
	default:
	}
}

func Test403ExhaustionRecordsThreeAttemptsAndOnlyOneNotice(t *testing.T) {
	p := &noticeProbeProvider{sequenceProvider: sequenceProvider{errors: []error{retry403(), retry403(), retry403()}}}
	al, ts, cleanup := faultTestTurn(t, p, "403-exhausted")
	defer cleanup()
	_, err := al.runTurn(context.Background(), ts, NewPipeline(al))
	if err == nil || p.callCount != 3 {
		t.Fatalf("calls=%d err=%v", p.callCount, err)
	}
	al.publishResponseOrError(context.Background(), "telegram", "123", ts.sessionKey, "", err)
	if len(al.bus.(*bus.MessageBus).OutboundChan()) != 1 {
		t.Fatal("failure/raw error or repeated notice was published")
	}
	files, _ := filepath.Glob(filepath.Join(ts.workspace, "state", "upstream-failures", "*.json"))
	if len(files) != 1 {
		t.Fatal("missing per-turn failure record")
	}
	raw, _ := os.ReadFile(files[0])
	var record map[string]any
	if json.Unmarshal(raw, &record) != nil || record["status"] != float64(403) || record["attempts"] != float64(3) || record["body_summary"] != "model access denied" {
		t.Fatalf("incomplete failure record: %s", raw)
	}
}

func TestPermanent403AndParameter400DoNotRetry(t *testing.T) {
	for _, row := range []struct {
		status int
		body   string
	}{
		{403, "content_filter blocked"}, {403, "invalid_parameter"}, {400, "invalid max_tokens argument"},
	} {
		t.Run(row.body, func(t *testing.T) {
			p := &noticeProbeProvider{sequenceProvider: sequenceProvider{errors: []error{&common.HTTPError{StatusCode: row.status, BodyPreview: row.body}}}}
			al, ts, cleanup := faultTestTurn(t, p, "permanent-"+row.body)
			defer cleanup()
			_, err := al.runTurn(context.Background(), ts, NewPipeline(al))
			if err == nil || p.callCount != 1 {
				t.Fatalf("permanent error retried %d times: %v", p.callCount, err)
			}
		})
	}
}

func TestFaultNoticeDeduplicatesAcrossFailedTurnsAndResetsSilently(t *testing.T) {
	p := &noticeProbeProvider{sequenceProvider: sequenceProvider{errors: []error{retry403(), retry403(), retry403(), retry403(), retry403(), retry403(), nil, retry403(), nil}}}
	al, ts, cleanup := faultTestTurn(t, p, "same-outage")
	defer cleanup()
	for n := 0; n < 4; n++ {
		next := newTurnState(ts.agent, ts.opts, turnEventScope{turnID: ts.turnID, context: newTurnContext(nil, nil, nil)})
		_, _ = al.runTurn(context.Background(), next, NewPipeline(al))
		want := 1
		if n == 3 {
			want = 2
		}
		if len(al.bus.(*bus.MessageBus).OutboundChan()) != want {
			t.Fatalf("turn %d notice count incorrect", n)
		}
	}
}

func TestBackgroundAndCallbackFaultsStayInternal(t *testing.T) {
	p := &noticeProbeProvider{sequenceProvider: sequenceProvider{errors: []error{retry403(), retry403(), retry403()}}}
	al, ts, cleanup := faultTestTurn(t, p, "heartbeat")
	defer cleanup()
	ts.opts.NoHistory = true
	_, _ = al.runTurn(context.Background(), ts, NewPipeline(al))
	if len(al.bus.(*bus.MessageBus).OutboundChan()) != 0 {
		t.Fatal("background diagnostic leaked")
	}
}

func TestCallbackFailureNoticeRemainsInternal(t *testing.T) {
	p := &noticeProbeProvider{}
	al, ts, cleanup := faultTestTurn(t, p, "callback-outage")
	defer cleanup()
	ctx := fatfish.WithCodexEvent(context.Background(), codexWakeEvent())
	al.notifyUpstreamFailure(ctx, ts, &turnExecution{}, true, false)
	if len(al.bus.(*bus.MessageBus).OutboundChan()) != 0 {
		t.Fatal("Codex callback notice leaked into private chat")
	}
}

func TestRateLimitTimeoutAndContextNoticesRemainShortAndSingle(t *testing.T) {
	for _, err := range []error{&common.HTTPError{StatusCode: 429, BodyPreview: "rate limited"}, context.DeadlineExceeded} {
		p := &noticeProbeProvider{sequenceProvider: sequenceProvider{errors: []error{err, nil}}}
		al, ts, cleanup := faultTestTurn(t, p, "transient-notice")
		_, got := al.runTurn(context.Background(), ts, NewPipeline(al))
		if got != nil || p.callCount != 2 || len(al.bus.(*bus.MessageBus).OutboundChan()) != 1 {
			t.Fatalf("calls=%d err=%v", p.callCount, got)
		}
		cleanup()
	}
	p := &noticeProbeProvider{}
	al, ts, cleanup := faultTestTurn(t, p, "context-notice")
	defer cleanup()
	exec := &turnExecution{}
	al.notifyUpstreamFailure(context.Background(), ts, exec, true, true)
	al.notifyUpstreamFailure(context.Background(), ts, exec, true, true)
	msg := <-al.bus.(*bus.MessageBus).OutboundChan()
	if !strings.Contains(msg.Content, "整理一下") || len(al.bus.(*bus.MessageBus).OutboundChan()) != 0 {
		t.Fatal("context notice missing/duplicated")
	}
}
