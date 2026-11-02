package fatfish

import "context"

// CodexEvent is a single durable broker receipt. The claim stays local to the
// consumer; it is never included in model text or tool arguments.
type CodexEvent struct {
	ID         string `json:"id"`
	Claim      string `json:"claim"`
	ChatID     string `json:"chat_id"`
	ChainID    string `json:"chain_id"`
	JobID      string `json:"job_id"`
	TurnID     string `json:"turn_id"`
	TurnStatus string `json:"turn_status"`
	Content    string `json:"content"`
	Kind       string `json:"kind,omitempty"`
	Silent     bool   `json:"silent,omitempty"`
	ThreadID   string `json:"thread_id,omitempty"`
}

type codexEventKey struct{}

func WithCodexEvent(ctx context.Context, event CodexEvent) context.Context {
	return context.WithValue(ctx, codexEventKey{}, event)
}

func IsCodexEvent(ctx context.Context) bool {
	event, ok := ctx.Value(codexEventKey{}).(CodexEvent)
	return ok && event.ID != ""
}

// CodexToolContext returns trusted routing metadata only while the tool still
// belongs to this receipt. A human steering message changes messageID and must
// not inherit permission to automatically answer an old Codex event.
func CodexToolContext(ctx context.Context, messageID string) map[string]any {
	event, ok := ctx.Value(codexEventKey{}).(CodexEvent)
	if !ok || event.ID == "" || messageID != "codex-event:"+event.ID {
		return nil
	}
	result := map[string]any{
		"codex_event_id": event.ID,
		"codex_chain_id": event.ChainID,
		"codex_job_id":   event.JobID,
		"codex_turn_id":  event.TurnID,
	}
	if event.ThreadID != "" {
		result["codex_thread_id"] = event.ThreadID
	}
	if event.Silent {
		result["codex_event_silent"] = true
	}
	return result
}
