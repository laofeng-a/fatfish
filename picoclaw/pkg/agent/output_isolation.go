package agent

import (
	"context"
	"strings"
	"time"

	"github.com/sipeed/picoclaw/pkg/fatfish"
	"github.com/sipeed/picoclaw/pkg/logger"
	toolshared "github.com/sipeed/picoclaw/pkg/tools/shared"
)

// Automatic background finals are working notes. Explicit sending tools keep
// their ordinary delivery path. A real human steering message takes precedence
// over the original callback's context, even if that callback was silent.
func internalAutomaticFinal(ctx context.Context, sessionKey, messageID string) bool {
	if !fatfish.Enabled() {
		return false
	}
	if strings.HasPrefix(sessionKey, "agent:task-") || strings.HasPrefix(sessionKey, "agent:autonomy-") || sessionKey == "agent:quota" {
		return true
	}
	if sessionKey == "heartbeat" || fatfish.IsBackground(ctx) {
		return true
	}
	if !fatfish.IsCodexEvent(ctx) {
		return false
	}
	if messageID == "" {
		messageID = toolshared.ReplyDeliveryMessageID(ctx)
	}
	return messageID == "" || fatfish.CodexToolContext(ctx, messageID) != nil
}

func recordInternalFinal(ctx context.Context, sessionKey, chatID, messageID, content, reason string) {
	if !fatfish.Enabled() || content == "" {
		return
	}
	if messageID == "" {
		messageID = toolshared.ReplyDeliveryMessageID(ctx)
	}
	recordCtx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	_, err := fatfish.Call(recordCtx, "outbound", map[string]any{
		"chat_id": chatID, "session_key": sessionKey, "message_id": messageID,
		"content": content, "mode": "background", "delivery_origin": "internal_final", "internal_reason": reason,
	})
	if err != nil {
		logger.WarnCF("agent", "Internal final audit unavailable", map[string]any{"session_key": sessionKey, "error": err.Error()})
	}
}
