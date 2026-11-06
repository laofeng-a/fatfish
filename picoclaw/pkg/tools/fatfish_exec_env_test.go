package tools

import (
	"context"
	shared "github.com/sipeed/picoclaw/pkg/tools/shared"
	"os"
	"strings"
	"testing"
)

func TestFatfishExecEnvIsPerInvocationAndReplacesStaleOrigin(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "fixture")
	t.Setenv("FATFISH_TOOL_MESSAGE_ID", "stale-parent")
	ctx := shared.WithToolContext(context.Background(), "telegram", "123")
	ctx = shared.WithToolMessageContext(ctx, "heartbeat:wake", "")
	ctx = shared.WithToolSessionContext(ctx, "main", "agent:autonomy-3", nil)
	env := fatfishExecEnv(ctx)
	found := map[string]string{}
	for _, entry := range env {
		k, v, _ := strings.Cut(entry, "=")
		found[k] = v
	}
	if found["FATFISH_TOOL_MESSAGE_ID"] != "heartbeat:wake" || found["FATFISH_TOOL_CHAT_ID"] != "123" || found["FATFISH_TOOL_SESSION_KEY"] != "agent:autonomy-3" {
		t.Fatalf("missing per-call origin: %v", found)
	}
	if os.Getenv("FATFISH_TOOL_MESSAGE_ID") != "stale-parent" {
		t.Fatal("changed global environment")
	}
}

func TestFatfishExecEnvDoesNotAlterUnmanagedOrUnscopedCommands(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "")
	if fatfishExecEnv(context.Background()) != nil {
		t.Fatal("non-Fatfish command changed")
	}
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "fixture")
	if fatfishExecEnv(context.Background()) != nil {
		t.Fatal("unscoped programmatic command changed")
	}
}
