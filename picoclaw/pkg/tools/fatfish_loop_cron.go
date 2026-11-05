package tools

import (
	"context"
	"github.com/sipeed/picoclaw/pkg/fatfish"
	"strings"
)

const loopCronPrefix = "[fatfish-session="

func cronLineage(text string) (string, string) {
	if strings.HasPrefix(text, loopCronPrefix) {
		if key, tail, ok := strings.Cut(strings.TrimPrefix(text, loopCronPrefix), "]\n"); ok && (strings.HasPrefix(key, "agent:task-") || strings.HasPrefix(key, "agent:autonomy-")) {
			return key, tail
		}
	}
	return "", text
}

func (t *CronTool) PruneRevoked(ctx context.Context) {
	if !fatfish.Enabled() {
		return
	}
	for _, job := range t.cronService.ListJobs(true) {
		key, _ := cronLineage(job.Payload.Message)
		if key == "" {
			continue
		}
		state, err := fatfish.Call(ctx, "loop_context", map[string]any{"session_key": key})
		if err == nil && state.Managed && !state.Active {
			t.cronService.RemoveJob(job.ID)
		}
	}
}

func (t *CronTool) stampLoopCron(ctx context.Context, args map[string]any) map[string]any {
	if !fatfish.Enabled() {
		return args
	}
	message, has := args["message"].(string)
	if !has {
		return args
	}
	key, _ := cronLineage(message)
	if key == "" {
		if id, ok := args["job_id"].(string); ok {
			if job, ok := t.cronService.GetJob(id); ok {
				key, _ = cronLineage(job.Payload.Message)
			}
		}
	}
	if key == "" {
		state, err := fatfish.Call(ctx, "loop_context", map[string]any{"session_key": ToolSessionKey(ctx), "message_id": ToolMessageID(ctx)})
		if err == nil && state.Managed {
			key = state.SessionKey
		}
	}
	if key != "" {
		_, message = cronLineage(message)
		copy := map[string]any{}
		for k, v := range args {
			copy[k] = v
		}
		copy["message"] = loopCronPrefix + key + "]\n" + message
		return copy
	}
	return args
}
