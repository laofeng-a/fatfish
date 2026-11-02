package integrationtools

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	toolshared "github.com/sipeed/picoclaw/pkg/tools/shared"
)

// applyFatfishDeliveryReceipt converts only the reader's structured delivery
// receipt to history. ForLLM may contain private diagnostics and is never used
// as an alleged message sent to a user.
func applyFatfishDeliveryReceipt(ctx context.Context, result *ToolResult, raw string, args map[string]any) {
	var response map[string]any
	if json.Unmarshal([]byte(raw), &response) != nil {
		result.DeliverySummary = "[Delivery receipt: unconfirmed] No valid delivery receipt; do not assume the user received an answer."
		return
	}
	receipt, modern := response["delivery_receipt"].(map[string]any)
	status, _ := response["status"].(string)
	currentChat := true
	if target, ok := args["chat_id"].(string); ok && target != "" && target != toolshared.ToolChatID(ctx) {
		currentChat = false
	}
	if !modern {
		// Compatibility with the previous reader: only an explicit sent result
		// supports a success claim, never queued/unknown/already_sent alone.
		if status != "sent" {
			result.DeliverySummary = "[Delivery receipt: unconfirmed] The tool did not confirm delivery; do not assume an answer was sent or blindly repeat it."
			return
		}
		receipt = map[string]any{"state": "sent", "answer_delivered": true}
		if text, ok := args["text"].(string); ok {
			receipt["text"] = text
		} else if parts, ok := args["parts"].([]any); ok {
			var texts []string
			for _, part := range parts {
				if text, ok := part.(string); ok {
					texts = append(texts, text)
				}
			}
			receipt["text"] = strings.Join(texts, "\n")
		}
		if emote, ok := response["emote"].(string); ok && emote != "" {
			receipt["media_kind"], receipt["media_name"] = "emote", emote
		}
		if reaction, ok := response["reaction"].(map[string]any); ok {
			if emoji, ok := reaction["emoji"].(string); ok {
				receipt["reaction"] = emoji
			}
		}
	} else {
		// The reader knows the effective target. Both sources must agree before
		// copying message contents into this chat's history.
		inCurrentChat, _ := receipt["current_chat"].(bool)
		currentChat = currentChat && inCurrentChat
	}
	state, _ := receipt["state"].(string)
	switch state {
	case "sent", "partial", "unknown", "rejected", "failed", "already_sent":
	default:
		result.DeliverySummary = "[Delivery receipt: unconfirmed] Unrecognized delivery state; do not assume the user received an answer."
		return
	}
	answerDelivered, _ := receipt["answer_delivered"].(bool)
	reaction, reactionConfirmed := receipt["reaction"].(string)
	// Only the reader can confirm that the requested primary output completed.
	// A successful reaction must not end a turn whose text/media failed. The
	// reader marks a genuinely reaction-only request complete separately.
	result.ResponseHandled = answerDelivered
	if !currentChat {
		result.DeliverySummary = fmt.Sprintf("[Delivery receipt: %s] Output targeted another chat; its text, media and reaction are omitted here. Answer delivered: %t. This does not answer the current chat.", state, answerDelivered)
		return
	}
	text, _ := receipt["text"].(string)
	mediaKind, _ := receipt["media_kind"].(string)
	if answerDelivered && (state == "sent" || state == "partial" || state == "already_sent") &&
		(strings.TrimSpace(text) != "" || mediaKind != "") {
		toolshared.RecordReplyDelivery(ctx, toolshared.ToolChannel(ctx), toolshared.ToolChatID(ctx))
	}
	var facts []string
	if text, ok := receipt["text"].(string); ok && strings.TrimSpace(text) != "" {
		facts = append(facts, "Confirmed sent text: "+deliveryExcerpt(text, 2500))
	}
	if kind, ok := receipt["media_kind"].(string); ok && kind != "" {
		name, _ := receipt["media_name"].(string)
		facts = append(facts, "Confirmed sent media: "+deliveryExcerpt(kind+" "+name, 160))
	}
	if reactionConfirmed {
		if reaction == "" {
			reaction = "removed"
		}
		facts = append(facts, "Confirmed message reaction: "+deliveryExcerpt(reaction, 32))
	}
	if len(facts) == 0 {
		facts = append(facts, "No message text or media details were confirmed.")
	}
	if !answerDelivered {
		facts = append(facts, "No completed answer delivery was confirmed; a reaction alone does not answer a pending question.")
	} else if reactionConfirmed {
		text, _ := receipt["text"].(string)
		kind, _ := receipt["media_kind"].(string)
		if strings.TrimSpace(text) == "" && kind == "" {
			facts = append(facts, "Only a message reaction was sent; this does not answer a pending question or report a task result.")
		}
	}
	if state == "partial" || state == "unknown" {
		facts = append(facts, "Only the listed parts are confirmed. Do not assume the rest was sent or blindly resend confirmed parts.")
	}
	result.DeliverySummary = "[Delivery receipt: " + state + "]\n" + strings.Join(facts, "\n")
}

func deliveryExcerpt(value string, limit int) string {
	runes := []rune(strings.TrimSpace(value))
	if len(runes) > limit {
		return string(runes[:limit]) + "… [truncated]"
	}
	return string(runes)
}
