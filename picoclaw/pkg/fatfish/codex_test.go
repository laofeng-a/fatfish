package fatfish

import (
	"context"
	"testing"
)

func TestCodexToolContextDoesNotLeakClaimOrSurviveHumanSteering(t *testing.T) {
	event := CodexEvent{ID: "aabb", Claim: "secret", ChainID: "chain", JobID: "job", TurnID: "turn"}
	ctx := WithCodexEvent(context.Background(), event)
	metadata := CodexToolContext(ctx, "codex-event:aabb")
	if len(metadata) != 4 || metadata["codex_event_id"] != "aabb" || metadata["codex_chain_id"] != "chain" {
		t.Fatalf("missing trusted event routing: %v", metadata)
	}
	if _, present := metadata["claim"]; present {
		t.Fatal("claim leaked into tool context")
	}
	if CodexToolContext(ctx, "1234") != nil || CodexToolContext(context.Background(), "codex-event:aabb") != nil {
		t.Fatal("human input or unrelated turn inherited Codex reply authority")
	}
	metadata["codex_event_id"] = "tampered"
	if CodexToolContext(ctx, "codex-event:aabb")["codex_event_id"] != "aabb" {
		t.Fatal("caller mutated trusted context")
	}
}
