package agent

import (
	"context"
	"errors"
	"testing"

	"github.com/sipeed/picoclaw/pkg/bus"
)

func TestFatfishProcessingFailureHasNoPublicErrorReply(t *testing.T) {
	p := &codexWakeProvider{}
	al := newCodexWakeTestLoop(t, p)
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "enabled-for-failure-routing-test")
	err := errors.New("LLM call failed after retries: API request failed: Status: 502 Body: upstream failed")
	al.publishResponseOrError(context.Background(), "telegram", "123", "private", "", err)
	select {
	case msg := <-al.bus.(*bus.MessageBus).OutboundChan():
		t.Fatalf("raw failure leaked: %s", msg.Content)
	default:
	}
}

func TestTransientFailureCanRecoverOnTheThirdAttempt(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "")
	transient := errors.New("API request failed: Status: 503 Body: service unavailable")
	p := &sequenceProvider{errors: []error{transient, transient, nil}}
	al, agent, cleanup := newTurnCoordTestLoop(t, p)
	defer cleanup()
	al.cfg.Agents.Defaults.MaxLLMRetries = 2
	al.cfg.Agents.Defaults.LLMRetryBackoffSecs = 1
	ts := newTurnState(agent, makeTestProcessOpts("third-attempt"), turnEventScope{turnID: "third-attempt", context: newTurnContext(nil, nil, nil)})
	result, err := al.runTurn(context.Background(), ts, NewPipeline(al))
	if err != nil || p.callCount != 3 || result.finalContent != "ok" {
		t.Fatalf("calls=%d final=%q error=%v", p.callCount, result.finalContent, err)
	}
}
