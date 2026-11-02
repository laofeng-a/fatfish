package integrationtools

import (
	"context"
	toolshared "github.com/sipeed/picoclaw/pkg/tools/shared"
	"testing"
)

func TestReplyDeliveryTracksOnlyConfirmedCurrentChatAnswer(t *testing.T) {
	for _, tc := range []struct {
		name, receipt string
		want          bool
	}{
		{"sent", `{"state":"sent","current_chat":true,"text":"正文","answer_delivered":true}`, true},
		{"media", `{"state":"sent","current_chat":true,"media_kind":"photo","answer_delivered":true}`, true},
		{"already_sent", `{"state":"already_sent","current_chat":true,"text":"正文","answer_delivered":true}`, true},
		{"other_chat", `{"state":"sent","current_chat":false,"text":"正文","answer_delivered":true}`, false},
		{"unknown", `{"state":"unknown","current_chat":true,"text":"预期正文","answer_delivered":false}`, false},
		{"reaction", `{"state":"sent","current_chat":true,"reaction":"👍","answer_delivered":true}`, false},
		{"partial", `{"state":"partial","current_chat":true,"text":"已发一半","answer_delivered":false}`, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx := toolshared.WithReplyDeliveryTracking(context.Background())
			ctx = toolshared.WithToolInboundContext(ctx, "telegram", "123", "42", "")
			toolshared.BeginReplyDeliveryMessage(ctx, "42")
			result := &ToolResult{}
			applyFatfishDeliveryReceipt(ctx, result, `{"delivery_receipt":`+tc.receipt+`}`, nil)
			if toolshared.HasReplyDelivery(ctx, "telegram", "123") != tc.want {
				t.Fatal("incorrect delivery tracking")
			}
			toolshared.BeginReplyDeliveryMessage(ctx, "42")
			if toolshared.HasReplyDelivery(ctx, "telegram", "123") != tc.want {
				t.Fatal("same-source tracking erased")
			}
			toolshared.BeginReplyDeliveryMessage(ctx, "43")
			if toolshared.HasReplyDelivery(ctx, "telegram", "123") {
				t.Fatal("old receipt suppressed a new human message")
			}
		})
	}
}
