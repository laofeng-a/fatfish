package agent

import (
	"path/filepath"
	"strings"

	"github.com/sipeed/picoclaw/pkg/providers"
	"github.com/sipeed/picoclaw/pkg/tools"
)

// toolDeliveryHistory deliberately never copies ForLLM: tool diagnostics are
// not evidence of something the assistant actually told a user.
func toolDeliveryHistory(result *tools.ToolResult, queued bool, attachments []providers.Attachment) []string {
	var facts []string
	if result == nil {
		return facts
	}
	if result.DeliverySummary != "" {
		facts = append(facts, result.DeliverySummary)
	}
	if queued && result.ForUser != "" {
		facts = append(facts, "Outgoing text queued; channel delivery not confirmed: "+result.ForUser)
	}
	for _, attachment := range attachments {
		facts = append(facts, "Confirmed sent attachment: "+filepath.Base(attachment.Filename)+" ("+attachment.ContentType+")")
	}
	if len(facts) == 0 && result.ResponseHandled && !result.IsError {
		facts = append(facts, "Tool output attempt handled; delivery details were not recorded. Do not infer that a question was answered.")
	}
	return facts
}

func deliveryHistoryContent(facts []string) string {
	if len(facts) == 0 {
		return "Tool output attempt handled; delivery details were not recorded."
	}
	content := "[Output history: delivery facts, not an additional message to the user]\n" + strings.Join(facts, "\n")
	runes := []rune(content)
	if len(runes) > 6000 {
		content = string(runes[:6000]) + "… [delivery history truncated]"
	}
	return content
}

const toolDiagnosticSeparator = "\n\n[Tool diagnostics]\n"

// Keep delivery facts on the real tool result, never as an assistant utterance.
func withToolDeliveryHistory(msg providers.Message, result *tools.ToolResult, queued bool, attachments []providers.Attachment) providers.Message {
	facts := toolDeliveryHistory(result, queued, attachments)
	if len(facts) > 0 {
		msg.Content = deliveryHistoryContent(facts) + toolDiagnosticSeparator + msg.Content
	}
	msg.Attachments = append(msg.Attachments, attachments...)
	return msg
}

func deliveryFactsForSummary(msg providers.Message) (providers.Message, bool) {
	if msg.Role != "tool" || !strings.HasPrefix(msg.Content, "[Output history: delivery facts,") {
		return msg, false
	}
	msg.Content, _, _ = strings.Cut(msg.Content, toolDiagnosticSeparator)
	return msg, true
}
