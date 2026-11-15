package integrationtools

import (
	"context"
	"path/filepath"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	toolshared "github.com/sipeed/picoclaw/pkg/tools/shared"
)

func TestFatfishMCPBackgroundRoutesKeepHarnessContextWithoutProbe(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", filepath.Join(t.TempDir(), "missing-runtime-token"))
	for _, session := range []string{"private", "agent:task-fixture", "agent:autonomy-1", "agent:quota", "heartbeat", "agent:cron-fixture"} {
		t.Run(session, func(t *testing.T) {
			calls := 0
			manager := &MockMCPManager{callToolFunc: func(ctx context.Context, server, tool string, args map[string]any) (*mcp.CallToolResult, error) {
				calls++
				trusted := args["_context"].(map[string]any)
				if trusted["session_key"] != session || trusted["chat_id"] != "123" || trusted["message_id"] != "42" {
					t.Fatal("harness context changed", trusted)
				}
				return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: `{"records":[]}`}}}, nil
			}}
			wrapper := NewMCPTool(manager, "reader", &mcp.Tool{Name: "history_find"})
			ctx := toolshared.WithToolInboundContext(context.Background(), "telegram", "123", "42", "")
			ctx = toolshared.WithToolSessionContext(ctx, "main", session, nil)
			result := wrapper.Execute(ctx, map[string]any{"_context": map[string]any{"session_key": "untrusted"}})
			if result.IsError || calls != 1 {
				t.Fatal("tool depended on the retired runtime probe", result, calls)
			}
		})
	}
}
