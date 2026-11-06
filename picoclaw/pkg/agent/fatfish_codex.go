package agent

import (
	"context"
	"errors"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/fatfish"
	"github.com/sipeed/picoclaw/pkg/logger"
	toolshared "github.com/sipeed/picoclaw/pkg/tools/shared"
)

const fatfishCodexPollInterval = 5 * time.Second

var fatfishCodexReceiptID = regexp.MustCompile(`^[a-f0-9]{24}$`)

type fatfishCodexCall func(context.Context, string, any) (fatfish.Decision, error)

func fatfishCodexInbound(owner string, event fatfish.CodexEvent) bus.InboundMessage {
	kind := "Codex协作回执"
	if event.Kind == "query" {
		kind = "Codex查询结果"
	}
	return bus.NormalizeInboundMessage(bus.InboundMessage{
		SessionKey: event.SessionKey,
		Context: bus.InboundContext{
			Channel: "telegram", ChatID: owner, ChatType: "direct",
			// Route into the owner's existing private session even when the
			// policy includes sender or has an owner-specific dispatch rule.
			// This routing identity is not evidence of a new human instruction.
			SenderID: owner, MessageID: "codex-event:" + event.ID,
			Raw: map[string]string{"source_kind": "codex_event", "source_sender_id": "codex"},
		},
		Sender:  bus.SenderInfo{Platform: "codex", PlatformID: "codex", DisplayName: "Codex 协作回执"},
		Content: "[" + kind + "]\n" + event.Content,
	})
}

func (al *AgentLoop) runFatfishCodexEvents(ctx context.Context) {
	owner := strings.TrimSpace(os.Getenv("FATFISH_OWNER_CHAT"))
	if owner == "" {
		return
	}
	ticker := time.NewTicker(fatfishCodexPollInterval)
	defer ticker.Stop()
	for {
		if ctx.Err() != nil || !al.running.Load() {
			return
		}
		// Poll returns at most one receipt; no detached model jobs accumulate.
		decision, err := fatfish.Call(ctx, "codex_event_poll", map[string]any{})
		if err == nil && decision.Event != nil {
			al.processFatfishCodexEvent(ctx, owner, *decision.Event, fatfish.Call)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// The broker owns durable deduplication. Claiming is reversible until start;
// once started, a crash or uncertain acknowledgement must never replay writes.
func (al *AgentLoop) processFatfishCodexEvent(ctx context.Context, owner string, event fatfish.CodexEvent, call fatfishCodexCall) {
	ctx = toolshared.WithReplyDeliveryTracking(ctx)
	if !fatfishCodexReceiptID.MatchString(event.ID) || !fatfishCodexReceiptID.MatchString(event.Claim) {
		return
	}
	receipt := map[string]any{"event_id": event.ID, "claim": event.Claim}
	release := func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
		defer cancel()
		_, _ = call(cleanupCtx, "codex_event_release", receipt)
	}
	if event.ChatID != owner || strings.TrimSpace(event.Content) == "" || len(event.Content) > 28000 {
		release()
		return
	}
	msg := fatfishCodexInbound(owner, event)
	sessionKey, _, ok := al.resolveSteeringTarget(msg)
	if !ok {
		release()
		return
	}
	ownerMessage := bus.NormalizeInboundMessage(bus.InboundMessage{
		Context: bus.InboundContext{Channel: msg.Channel, ChatID: owner, ChatType: "direct", SenderID: owner},
		Sender:  bus.SenderInfo{Platform: msg.Channel, PlatformID: owner},
	})
	ownerSession, _, routable := al.resolveSteeringTarget(ownerMessage)
	if routable && ownerSession != sessionKey {
		ownerBusy := func() bool { return al.getActiveTurnState(ownerSession) != nil }
		if ownerBusy() {
			release()
			return
		}
		ctx = context.WithValue(ctx, backgroundYieldKey{}, ownerBusy)
	}
	placeholder := &turnState{turnID: makePendingTurnID(sessionKey, al.turnSeq.Add(1)), phase: TurnPhaseSetup}
	if _, busy := al.activeTurnStates.LoadOrStore(sessionKey, placeholder); busy {
		// Codex results wait for the current human turn; they never steer it.
		release()
		return
	}
	defer al.releaseSessionTurnState(sessionKey, placeholder)
	select {
	case al.workerSem <- struct{}{}:
		defer func() { <-al.workerSem }()
	default:
		release()
		return
	}
	if ctx.Err() != nil {
		release()
		return
	}
	started, err := call(ctx, "codex_event_start", receipt)
	if err != nil || !started.Accepted || started.Duplicate {
		// An uncertain start is left in the durable broker for inspection.
		// Releasing here could replay a start accepted before a timeout.
		return
	}
	outcome := "panicked"
	defer func() {
		if recovered := recover(); recovered != nil {
			logger.ErrorCF("agent", "Codex event processing panicked", map[string]any{"event_id": event.ID})
		}
		ackCtx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
		defer cancel()
		ack, ackErr := call(ackCtx, "codex_event_ack", map[string]any{"event_id": event.ID, "claim": event.Claim, "outcome": outcome})
		if ackErr != nil || !ack.Accepted {
			logger.WarnCF("agent", "Codex event acknowledgement unconfirmed; receipt will not be replayed", map[string]any{"event_id": event.ID})
		}
	}()
	if al.takePendingStop(sessionKey) {
		outcome = "interrupted"
		return
	}
	if al.channelManager != nil {
		defer al.channelManager.InvokeTypingStop(msg.Channel, msg.ChatID)
	}
	// Keep the durable receipt live while processing, without a fixed turn deadline.
	leaseCtx, stopLease := context.WithCancel(ctx)
	leaseDone := make(chan struct{})
	go func() {
		defer close(leaseDone)
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-leaseCtx.Done():
				return
			case <-ticker.C:
				touchCtx, cancelTouch := context.WithTimeout(leaseCtx, 4*time.Second)
				_, _ = call(touchCtx, "codex_event_touch", receipt)
				cancelTouch()
			}
		}
	}()
	defer func() { stopLease(); <-leaseDone }()
	eventCtx := fatfish.WithCodexEvent(ctx, event)
	response, processErr := al.processMessage(eventCtx, msg)
	outcome = fatfish.TurnOutcome(response, processErr)
	var yielded *backgroundYieldError
	if errors.As(processErr, &yielded) {
		outcome = "yielded"
	}
	if outcome == "no_reply" {
		outcome = "completed"
	}
	if ctx.Err() != nil || errors.Is(processErr, context.Canceled) {
		outcome = "interrupted"
	}
	// The publisher keeps callback conclusions internal. A real human steering
	// message changes the tracked source and retains its normal reply path.
	al.publishResponseOrError(eventCtx, msg.Channel, msg.ChatID, sessionKey, response, processErr)
	// Human messages received during processing retain normal continuation
	// behavior, but run without the old Codex-event tool authority.
	continued, continueErr := al.drainQueuedSteeringContinuations(ctx, &continuationTarget{SessionKey: sessionKey, Channel: msg.Channel, ChatID: msg.ChatID})
	if continueErr == nil && continued != "" {
		al.PublishResponseIfNeeded(ctx, msg.Channel, msg.ChatID, sessionKey, continued)
	}
}
