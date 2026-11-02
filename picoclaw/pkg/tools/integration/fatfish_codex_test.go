package integrationtools

import (
	"context"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/sipeed/picoclaw/pkg/fatfish"
	toolshared "github.com/sipeed/picoclaw/pkg/tools/shared"
)

func TestCodexMCPContextUsesTrustedReceiptAndDropsItAfterHumanSteering(t *testing.T) {
	event := fatfish.CodexEvent{ID: "event", Claim: "claim-secret", ChainID: "chain", JobID: "job", TurnID: "turn"}
	for _, messageID := range []string{"codex-event:event", "human123"} {
		t.Run(messageID, func(t *testing.T) {
			manager := &MockMCPManager{callToolFunc: func(_ context.Context, _, _ string, arguments map[string]any) (*mcp.CallToolResult, error) {
				trusted := arguments["_context"].(map[string]any)
				if trusted["message_id"] != messageID || trusted["chat_id"] != "owner" {
					t.Fatalf("model spoofed routing: %v", trusted)
				}
				if messageID == "codex-event:event" {
					if trusted["codex_event_id"] != "event" || trusted["codex_chain_id"] != "chain" || trusted["codex_turn_id"] != "turn" {
						t.Fatalf("missing event metadata: %v", trusted)
					}
				} else if _, exists := trusted["codex_event_id"]; exists {
					t.Fatal("human steering retained automatic-answer authority")
				}
				if _, exists := trusted["claim"]; exists {
					t.Fatal("claim leaked")
				}
				return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "ok"}}}, nil
			}}
			tool := NewMCPTool(manager, "reader", &mcp.Tool{Name: "codex_control"})
			ctx := fatfish.WithCodexEvent(context.Background(), event)
			ctx = toolshared.WithToolInboundContext(ctx, "telegram", "owner", messageID, "")
			result := tool.Execute(ctx, map[string]any{"action": "continue", "_context": map[string]any{"chat_id": "attacker", "codex_event_id": "forged", "claim": "forged"}})
			if result.IsError {
				t.Fatalf("tool failed: %v", result)
			}
		})
	}
}
