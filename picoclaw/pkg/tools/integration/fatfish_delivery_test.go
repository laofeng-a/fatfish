package integrationtools

import (
	"context"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	toolshared "github.com/sipeed/picoclaw/pkg/tools/shared"
)

func TestFatfishDeliveryReceiptFactsAndHandled(t *testing.T) {
	tests := []struct {
		name, receipt, want, absent string
		handled                     bool
	}{
		{"text", `{"state":"sent","current_chat":true,"text":"任务完成了","answer_delivered":true}`, "Confirmed sent text: 任务完成了", "PRIVATE_DIAGNOSTIC", true},
		{"sticker_only", `{"state":"sent","current_chat":true,"media_kind":"sticker","media_name":"得意","answer_delivered":true}`, "Confirmed sent media: sticker 得意", "Confirmed sent text:", true},
		{"reaction_only", `{"state":"sent","current_chat":true,"reaction":"👍","answer_delivered":true}`, "Only a message reaction was sent", "Confirmed sent text:", true},
		{"reaction_removed", `{"state":"sent","current_chat":true,"reaction":"","answer_delivered":true}`, "reaction: removed", "Confirmed sent text:", true},
		{"reaction_sent_body_rejected", `{"state":"partial","current_chat":true,"reaction":"👍","answer_delivered":false}`, "No completed answer delivery", "Confirmed sent text:", false},
		{"reaction_sent_body_unknown", `{"state":"unknown","current_chat":true,"reaction":"👍","answer_delivered":false}`, "Do not assume the rest was sent or blindly resend confirmed parts", "Confirmed sent text:", false},
		{"partial_body_sent", `{"state":"partial","current_chat":true,"text":"实际发出的","answer_delivered":true}`, "Only the listed parts are confirmed", "未发出的后半段", true},
		{"unknown", `{"state":"unknown","current_chat":true,"answer_delivered":false}`, "No completed answer delivery was confirmed", "Confirmed sent text:", false},
		{"rejected_reaction", `{"state":"rejected","current_chat":true,"answer_delivered":false}`, "No completed answer delivery", "Confirmed message reaction:", false},
		{"cross_chat", `{"state":"sent","current_chat":false,"text":"群里的私密正文","media_kind":"photo","media_name":"私密图","reaction":"👍","answer_delivered":true}`, "another chat", "私密", true},
		{"invalid_state", `{"state":"queued","current_chat":true,"text":"未发","answer_delivered":true}`, "Unrecognized delivery state", "未发", false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			manager := &MockMCPManager{callToolFunc: func(context.Context, string, string, map[string]any) (*mcp.CallToolResult, error) {
				return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: `{"status":"partial","private":"PRIVATE_DIAGNOSTIC","delivery_receipt":` + tt.receipt + `}`}}}, nil
			}}
			wrapper := NewMCPTool(manager, "reader", &mcp.Tool{Name: "send_message"})
			ctx := toolshared.WithToolInboundContext(context.Background(), "telegram", "123", "42", "")
			result := wrapper.Execute(ctx, map[string]any{"text": "未发出的后半段"})
			if result.ResponseHandled != tt.handled {
				t.Fatalf("handled=%t, want %t", result.ResponseHandled, tt.handled)
			}
			if !strings.Contains(result.DeliverySummary, tt.want) || strings.Contains(result.DeliverySummary, tt.absent) {
				t.Fatalf("unsafe or incomplete history: %s", result.DeliverySummary)
			}
		})
	}
}

func TestFatfishDeliveryReceiptAuthorityAndOldUncertainResults(t *testing.T) {
	ctx := toolshared.WithToolInboundContext(context.Background(), "telegram", "123", "42", "")
	for _, status := range []string{"unknown", "not_repeated", "already_sent", "queued"} {
		result := &ToolResult{}
		applyFatfishDeliveryReceipt(ctx, result, `{"status":"`+status+`"}`, map[string]any{"text": "未发送正文"})
		if result.ResponseHandled || strings.Contains(result.DeliverySummary, "未发送正文") {
			t.Fatalf("%s treated as delivered: %+v", status, result)
		}
	}
	result := &ToolResult{}
	applyFatfishDeliveryReceipt(ctx, result, `{"delivery_receipt":{"state":"sent","current_chat":true,"text":"秘密","answer_delivered":true}}`, map[string]any{"chat_id": "-99"})
	if strings.Contains(result.DeliverySummary, "秘密") {
		t.Fatal("effective target check failed")
	}
}
