package tools

import (
	"context"
	"os"
	"strings"

	"github.com/sipeed/picoclaw/pkg/fatfish"
)

// fatfishExecEnv attaches this tool call's origin to its own subprocess only.
// Shell-backed sending helpers can then use the same outbound/receipt gate.
func fatfishExecEnv(ctx context.Context) []string {
	if !fatfish.Enabled() {
		return nil // Preserve ordinary exec.Cmd environment inheritance.
	}
	keys := map[string]string{
		"FATFISH_TOOL_SESSION_KEY": ToolSessionKey(ctx),
		"FATFISH_TOOL_MESSAGE_ID":  ToolMessageID(ctx),
		"FATFISH_TOOL_CHAT_ID":     ToolChatID(ctx),
	}
	if keys["FATFISH_TOOL_SESSION_KEY"] == "" && keys["FATFISH_TOOL_MESSAGE_ID"] == "" && keys["FATFISH_TOOL_CHAT_ID"] == "" {
		return nil
	}
	environment := make([]string, 0, len(os.Environ())+len(keys))
	for _, entry := range os.Environ() {
		name, _, _ := strings.Cut(entry, "=")
		if _, owned := keys[strings.ToUpper(name)]; !owned {
			environment = append(environment, entry)
		}
	}
	for _, name := range []string{"FATFISH_TOOL_SESSION_KEY", "FATFISH_TOOL_MESSAGE_ID", "FATFISH_TOOL_CHAT_ID"} {
		environment = append(environment, name+"="+keys[name])
	}
	return environment
}
