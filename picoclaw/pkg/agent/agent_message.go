// PicoClaw - Ultra-lightweight personal AI agent

package agent

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/constants"
	"github.com/sipeed/picoclaw/pkg/fatfish"
	"github.com/sipeed/picoclaw/pkg/logger"
	"github.com/sipeed/picoclaw/pkg/routing"
	"github.com/sipeed/picoclaw/pkg/session"
	"github.com/sipeed/picoclaw/pkg/utils"
)

func (al *AgentLoop) buildContinuationTarget(msg bus.InboundMessage) (*continuationTarget, error) {
	if msg.Channel == "system" {
		return nil, nil
	}

	route, _, err := al.resolveMessageRoute(msg)
	if err != nil {
		return nil, err
	}
	allocation := al.allocateRouteSession(route, msg)

	return &continuationTarget{
		SessionKey: resolveScopeKey(allocation.SessionKey, msg.SessionKey),
		Channel:    msg.Channel,
		ChatID:     msg.ChatID,
	}, nil
}

func (al *AgentLoop) ProcessDirect(
	ctx context.Context,
	content, sessionKey string,
) (string, error) {
	return al.ProcessDirectWithChannel(ctx, content, sessionKey, "cli", "direct")
}

func (al *AgentLoop) ProcessDirectWithChannel(
	ctx context.Context,
	content, sessionKey, channel, chatID string,
) (string, error) {
	if err := al.ensureHooksInitialized(ctx); err != nil {
		return "", err
	}
	if err := al.ensureMCPInitialized(ctx); err != nil {
		return "", err
	}

	msg := bus.InboundMessage{
		Context: bus.InboundContext{
			Channel:  channel,
			ChatID:   chatID,
			ChatType: "direct",
			SenderID: "cron",
		},
		Content:    content,
		SessionKey: sessionKey,
	}

	return al.processMessage(ctx, msg)
}

func (al *AgentLoop) ProcessHeartbeat(
	ctx context.Context,
	content, channel, chatID string,
) (string, error) {
	if fatfish.Enabled() {
		channel = "telegram"
		chatID = os.Getenv("FATFISH_OWNER_CHAT")
	}
	return al.processHeartbeat(ctx, content, channel, chatID, fatfish.Call)
}

// Background checks yield to an active human turn and never wait in the worker
// queue. The broker is consulted only after bounded local capacity is reserved.
func (al *AgentLoop) processHeartbeat(
	ctx context.Context,
	content, channel, chatID string,
	call fatfishCodexCall,
) (response string, processErr error) {
	if ctx.Err() != nil {
		return "HEARTBEAT_OK", nil
	}
	ownerMessage := bus.NormalizeInboundMessage(bus.InboundMessage{
		Context: bus.InboundContext{Channel: channel, ChatID: chatID, ChatType: "direct", SenderID: chatID},
		Sender:  bus.SenderInfo{Platform: channel, PlatformID: chatID},
	})
	ownerSession, _, ownerRoutable := al.resolveSteeringTarget(ownerMessage)
	ownerBusy := func() bool { return ownerRoutable && al.getActiveTurnState(ownerSession) != nil }
	if ownerBusy() {
		return "HEARTBEAT_OK", nil
	}
	select {
	case al.workerSem <- struct{}{}:
		defer func() { <-al.workerSem }()
	default:
		return "HEARTBEAT_OK", nil
	}
	if ownerBusy() {
		return "HEARTBEAT_OK", nil
	}
	placeholder := &turnState{turnID: makePendingTurnID("heartbeat", al.turnSeq.Add(1)), phase: TurnPhaseSetup}
	if _, busy := al.activeTurnStates.LoadOrStore("heartbeat", placeholder); busy {
		return "HEARTBEAT_OK", nil
	}
	defer al.releaseSessionTurnState("heartbeat", placeholder)
	decision, policyErr := call(ctx, "wake", map[string]any{"chat_id": chatID})
	if policyErr != nil || !decision.Allowed || decision.Skip {
		return "HEARTBEAT_OK", nil
	}
	// A running wake renews its lease; elapsed wall time is not a failed task.
	leaseCtx, stopLease := context.WithCancel(ctx)
	leaseDone := make(chan struct{})
	go func() {
		defer close(leaseDone)
		ticker := time.NewTicker(15 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-leaseCtx.Done():
				return
			case <-ticker.C:
				touchCtx, cancelTouch := context.WithTimeout(leaseCtx, 4*time.Second)
				_, _ = call(touchCtx, "wake_touch", map[string]any{"wake_id": decision.WakeID})
				cancelTouch()
			}
		}
	}()
	defer func() { stopLease(); <-leaseDone }()
	defer func() {
		if recovered := recover(); recovered != nil {
			logger.ErrorC("agent", "Heartbeat processing panicked")
			response, processErr = "", fmt.Errorf("heartbeat processing panicked")
		}
		outcome := "completed"
		summary := strings.TrimSpace(response)
		if processErr != nil || ctx.Err() != nil {
			outcome, summary = "error", "后台检查未成功完成"
			if processErr == nil {
				processErr = ctx.Err()
			}
		}
		var yielded *backgroundYieldError
		if errors.As(processErr, &yielded) {
			summary = yielded.Error()
		}
		if len([]rune(summary)) > 2000 {
			summary = string([]rune(summary)[:2000])
		}
		ackCtx, ackCancel := context.WithTimeout(context.Background(), 4*time.Second)
		defer ackCancel()
		_, ackErr := call(ackCtx, "wake_done", map[string]any{
			"wake_id": decision.WakeID, "outcome": outcome, "summary": summary,
		})
		if ackErr != nil {
			logger.WarnC("agent", "Heartbeat completion acknowledgement unconfirmed")
		}
	}()
	if decision.Silent {
		// A silent task must not inherit ordinary proactive-sharing work.
		if strings.TrimSpace(decision.Context) == "" {
			return "", fmt.Errorf("silent heartbeat has no task context")
		}
		content = decision.Context
	} else if strings.TrimSpace(decision.Context) != "" {
		content += "\n\n" + decision.Context
	}
	ctx = fatfish.Background(ctx)
	ctx = context.WithValue(ctx, backgroundYieldKey{}, ownerBusy)
	if err := al.ensureHooksInitialized(ctx); err != nil {
		return "", err
	}
	if err := al.ensureMCPInitialized(ctx); err != nil {
		return "", err
	}

	agent := al.GetRegistry().GetDefaultAgent()
	if agent == nil {
		return "", fmt.Errorf("no default agent for heartbeat")
	}
	dispatch := DispatchRequest{
		SessionKey:  "heartbeat",
		UserMessage: content,
	}
	if channel != "" || chatID != "" {
		dispatch.InboundContext = &bus.InboundContext{
			Channel:   channel,
			ChatID:    chatID,
			ChatType:  "direct",
			SenderID:  "heartbeat",
			MessageID: "heartbeat:" + decision.WakeID,
		}
	}
	return al.runAgentLoop(ctx, agent, processOptions{
		Dispatch:             dispatch,
		DefaultResponse:      defaultResponse,
		EnableSummary:        false,
		SendResponse:         false,
		SuppressToolFeedback: true,
		NoHistory:            true, // Don't load session history for heartbeat
	})
}

func (al *AgentLoop) prepareInboundMessageForAgent(
	ctx context.Context,
	msg bus.InboundMessage,
) bus.InboundMessage {
	msg = bus.NormalizeInboundMessage(msg)

	var hadAudio bool
	msg, hadAudio = al.transcribeAudioInMessage(ctx, msg)

	// For audio messages the placeholder was deferred by the channel.
	// Now that transcription (and optional feedback) is done, send it.
	if hadAudio && al.channelManager != nil {
		al.channelManager.SendPlaceholder(ctx, msg.Channel, msg.ChatID)
	}

	return msg
}

func (al *AgentLoop) processMessage(ctx context.Context, msg bus.InboundMessage) (result string, processErr error) {
	msg = al.prepareInboundMessageForAgent(ctx, msg)
	if fatfish.Enabled() && msg.Channel == "telegram" && msg.Context.MessageID != "" {
		fatfish.TraceTurn(msg.ChatID, msg.Context.MessageID, "processing")
		defer func() {
			fatfish.TraceTurn(msg.ChatID, msg.Context.MessageID, fatfish.TurnOutcome(result, processErr))
		}()
	}

	// Add message preview to log (show full content for error messages)
	var logContent string
	if strings.Contains(msg.Content, "Error:") || strings.Contains(msg.Content, "error") {
		logContent = msg.Content // Full content for errors
	} else {
		logContent = utils.Truncate(msg.Content, 80)
	}
	logger.InfoCF(
		"agent",
		fmt.Sprintf("Processing message from %s:%s (%d chars)", msg.Channel, msg.SenderID, len(logContent)),
		map[string]any{
			"channel":     msg.Channel,
			"chat_id":     msg.ChatID,
			"sender_id":   msg.SenderID,
			"session_key": msg.SessionKey,
		},
	)

	// Route system messages to processSystemMessage
	if msg.Channel == "system" {
		return al.processSystemMessage(ctx, msg)
	}

	route, agent, routeErr := al.resolveMessageRoute(msg)
	if routeErr != nil {
		return "", routeErr
	}

	allocation := al.allocateRouteSession(route, msg)

	// Resolve session key from the route allocation, while preserving explicit
	// agent-scoped keys supplied by the caller.
	scopeKey := resolveScopeKey(allocation.SessionKey, msg.SessionKey)
	sessionKey := scopeKey

	// Reset message-tool state for this round so we don't skip publishing due to a previous round.
	if tool, ok := agent.Tools.Get("message"); ok {
		if resetter, ok := tool.(interface{ ResetSentInRound(sessionKey string) }); ok {
			resetter.ResetSentInRound(sessionKey)
		}
	}

	logger.InfoCF("agent", "Routed message",
		map[string]any{
			"agent_id":           agent.ID,
			"scope_key":          scopeKey,
			"session_key":        sessionKey,
			"matched_by":         route.MatchedBy,
			"route_agent":        route.AgentID,
			"route_channel":      route.Channel,
			"route_main_session": allocation.MainSessionKey,
		})

	opts := processOptions{
		Dispatch: DispatchRequest{
			SessionKey:     sessionKey,
			SessionAliases: buildSessionAliases(sessionKey, append(allocation.SessionAliases, msg.SessionKey)...),
			InboundContext: cloneInboundContext(&msg.Context),
			RouteResult:    cloneResolvedRoute(&route),
			SessionScope:   session.CloneScope(&allocation.Scope),
			UserMessage:    msg.Content,
			Media:          append([]string(nil), msg.Media...),
		},
		SenderID:                msg.SenderID,
		SenderDisplayName:       msg.Sender.DisplayName,
		DefaultResponse:         defaultResponse,
		EnableSummary:           true,
		SendResponse:            false,
		AllowInterimPicoPublish: true,
	}
	var err error
	opts, err = resolveTurnProfileOptions(al.GetConfig(), opts)
	if err != nil {
		return "", err
	}

	// context-dependent commands check their own Runtime fields and report
	// "unavailable" when the required capability is nil.
	if response, handled := al.handleCommand(ctx, msg, agent, &opts); handled {
		return response, nil
	}

	if pending := al.takePendingSkills(opts.Dispatch.SessionKey); len(pending) > 0 {
		opts.ForcedSkills = append(opts.ForcedSkills, pending...)
		logger.InfoCF("agent", "Applying pending skill override",
			map[string]any{
				"session_key": opts.Dispatch.SessionKey,
				"skills":      strings.Join(pending, ","),
			})
	}

	return al.runAgentLoop(ctx, agent, opts)
}

func (al *AgentLoop) resolveMessageRoute(msg bus.InboundMessage) (routing.ResolvedRoute, *AgentInstance, error) {
	registry := al.GetRegistry()
	inboundCtx := normalizedInboundContext(msg)
	route := registry.ResolveRoute(inboundCtx)

	agent, ok := registry.GetAgent(route.AgentID)
	if !ok {
		agent = registry.GetDefaultAgent()
	}
	if agent == nil {
		return routing.ResolvedRoute{}, nil, fmt.Errorf("no agent available for route (agent_id=%s)", route.AgentID)
	}

	return route, agent, nil
}

func (al *AgentLoop) allocateRouteSession(route routing.ResolvedRoute, msg bus.InboundMessage) session.Allocation {
	return session.AllocateRouteSession(session.AllocationInput{
		AgentID:       route.AgentID,
		Context:       normalizedInboundContext(msg),
		SessionPolicy: route.SessionPolicy,
	})
}

func (al *AgentLoop) processSystemMessage(
	ctx context.Context,
	msg bus.InboundMessage,
) (string, error) {
	if msg.Channel != "system" {
		return "", fmt.Errorf(
			"processSystemMessage called with non-system message channel: %s",
			msg.Channel,
		)
	}

	logger.InfoCF("agent", "Processing system message",
		map[string]any{
			"sender_id": msg.SenderID,
			"chat_id":   msg.ChatID,
		})

	// Parse origin channel from chat_id (format: "channel:chat_id")
	var originChannel, originChatID string
	if idx := strings.Index(msg.ChatID, ":"); idx > 0 {
		originChannel = msg.ChatID[:idx]
		originChatID = msg.ChatID[idx+1:]
	} else {
		originChannel = "cli"
		originChatID = msg.ChatID
	}

	// Extract subagent result from message content
	// Format: "Task 'label' completed.\n\nResult:\n<actual content>"
	content := msg.Content
	if idx := strings.Index(content, "Result:\n"); idx >= 0 {
		content = content[idx+8:] // Extract just the result part
	}

	// Skip internal channels - only log, don't send to user
	if constants.IsInternalChannel(originChannel) {
		logger.InfoCF("agent", "Subagent completed (internal channel)",
			map[string]any{
				"sender_id":   msg.SenderID,
				"content_len": len(content),
				"channel":     originChannel,
			})
		return "", nil
	}

	// Use default agent for system messages
	agent := al.GetRegistry().GetDefaultAgent()
	if agent == nil {
		return "", fmt.Errorf("no default agent for system message")
	}

	// Use the origin session for context
	sessionKey := session.BuildMainSessionKey(agent.ID)
	dispatch := DispatchRequest{
		SessionKey:  sessionKey,
		UserMessage: fmt.Sprintf("[System: %s] %s", msg.SenderID, msg.Content),
	}
	if originChannel != "" || originChatID != "" {
		dispatch.InboundContext = &bus.InboundContext{
			Channel:  originChannel,
			ChatID:   originChatID,
			ChatType: "direct",
			SenderID: msg.SenderID,
		}
	}

	return al.runAgentLoop(ctx, agent, processOptions{
		Dispatch:        dispatch,
		DefaultResponse: "Background task completed.",
		EnableSummary:   false,
		SendResponse:    true,
	})
}
