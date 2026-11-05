package agent

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/sipeed/picoclaw/pkg/fatfish"
	"github.com/sipeed/picoclaw/pkg/providers"
)

func loopIdentity(ts *turnState) map[string]any {
	return map[string]any{"session_key": ts.sessionKey, "message_id": ts.opts.Dispatch.MessageID()}
}

func loopTaskMessages(messages []providers.Message, canonical, current string) []providers.Message {
	out := []providers.Message{{Role: "system", Content: canonical + "\n\n" + current}}
	for _, m := range messages {
		if m.Role != "system" {
			out = append(out, m)
		}
	}
	return out
}

func taskAgentRules(text string) string {
	var out strings.Builder
	keep := true
	for _, line := range strings.Split(text, "\n") {
		if strings.HasPrefix(line, "# ") {
			keep = strings.TrimSpace(strings.TrimPrefix(line, "# ")) != "和jg相处"
		}
		if keep {
			out.WriteString(line + "\n")
		}
	}
	return out.String()
}

func prepareFatfishLoopRequest(ctx context.Context, ts *turnState, exec *turnExecution) error {
	if !fatfish.Enabled() || ts.channel != "telegram" {
		return nil
	}
	identity := loopIdentity(ts)
	state, err := fatfish.Call(ctx, "loop_context", identity)
	if err != nil {
		if strings.HasPrefix(ts.sessionKey, "agent:task-") || strings.HasPrefix(ts.sessionKey, "agent:autonomy-") {
			return err
		}
		exec.callMessages = withFatfishTurnEvent(ts, exec.callMessages, "")
		return nil
	}
	if !state.Managed {
		exec.callMessages = withFatfishTurnEvent(ts, exec.callMessages, state.WakeKind)
		return nil
	}
	if !state.Active {
		return fmt.Errorf("managed activity revoked at action boundary")
	}
	if state.TaskID != "" || ts.sessionKey == "agent:quota" {
		root := filepath.Dir(os.Getenv("FATFISH_STRUCTURED_CORE_FILE"))
		var canonical strings.Builder
		for _, p := range []string{filepath.Join(root, "profile", "SOUL.md"), filepath.Join(root, "core.md"), filepath.Join(root, "profile", "AGENT.md")} {
			raw, e := os.ReadFile(p)
			if e != nil {
				return fmt.Errorf("task canonical source unavailable: %s", filepath.Base(p))
			}
			text := string(raw)
			if filepath.Base(p) == "AGENT.md" {
				text = taskAgentRules(text)
			}
			canonical.WriteString("\n# " + filepath.Base(p) + "\n" + text)
		}
		if guide, err := os.ReadFile(filepath.Join(ts.workspace, "guides", "OPERATIONS.md")); err == nil {
			if _, section, ok := strings.Cut(string(guide), "# 把事情办实\n"); ok {
				section, _, _ = strings.Cut(section, "\n# ")
				canonical.WriteString("\n# 操作指南：把事情办实（同源读取）\n" + section)
			}
		}
		messages := exec.callMessages
		if !strings.HasPrefix(ts.sessionKey, "agent:task-") && exec.currentTurnStart > 0 && exec.currentTurnStart < len(messages) {
			messages = messages[exec.currentTurnStart:]
		}
		exec.callMessages = loopTaskMessages(messages, canonical.String(), state.Context)
	}
	exec.callMessages = withFatfishTurnEvent(ts, exec.callMessages, state.WakeKind)
	return nil
}

func auditFatfishLoopRequest(ctx context.Context, ts *turnState, exec *turnExecution) error {
	if !fatfish.Enabled() || len(exec.callMessages) == 0 {
		return nil
	}
	_, tail, found := strings.Cut(exec.callMessages[0].Content, "[任务上下文 task_id=")
	if !found {
		return nil
	}
	_, tail, found = strings.Cut(tail, " version=")
	if !found {
		return nil
	}
	var version int
	if _, err := fmt.Sscanf(tail, "%d", &version); err != nil {
		return err
	}
	identity := loopIdentity(ts)
	identity["assembled_task_version"] = version
	identity["assembled_request"] = map[string]any{"messages": exec.callMessages, "tools": exec.providerToolDefs, "model": exec.llmModel}
	_, err := fatfish.Call(ctx, "loop_context", identity)
	return err
}

func fatfishLoopActive(ctx context.Context, ts *turnState) bool {
	if !fatfish.Enabled() || ts.channel != "telegram" {
		return true
	}
	state, err := fatfish.Call(ctx, "loop_context", loopIdentity(ts))
	if err != nil && !strings.HasPrefix(ts.sessionKey, "agent:task-") && !strings.HasPrefix(ts.sessionKey, "agent:autonomy-") {
		return true
	}
	return err == nil && (!state.Managed || state.Active)
}

func fatfishLoopToolAllowed(ctx context.Context, ts *turnState, name string, args map[string]any) bool {
	if !fatfish.Enabled() || ts.channel != "telegram" {
		return true
	}
	state, err := fatfish.Call(ctx, "loop_context", loopIdentity(ts))
	if err != nil {
		return !strings.HasPrefix(ts.sessionKey, "agent:task-") && !strings.HasPrefix(ts.sessionKey, "agent:autonomy-")
	}
	if !state.Managed {
		return true
	}
	if !state.Active {
		return false
	}
	if state.TaskID != "" && !state.Report {
		if name == "message" || name == "send_file" || name == "mcp_reader_send_message" || name == "mcp_reader_emote_send" {
			return false
		}
		if name == "exec" {
			command, _ := args["command"].(string)
			if strings.Contains(command, "emote_send.py") {
				return false
			}
		}
	}
	return true
}

func (al *AgentLoop) pruneLoopCrons(ctx context.Context) {
	if agent := al.GetRegistry().GetDefaultAgent(); agent != nil {
		if tool, ok := agent.Tools.Get("cron"); ok {
			if p, ok := tool.(interface{ PruneRevoked(context.Context) }); ok {
				p.PruneRevoked(ctx)
			}
		}
	}
}
