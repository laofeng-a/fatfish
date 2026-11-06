package agent

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/sipeed/picoclaw/pkg/fileutil"
	"github.com/sipeed/picoclaw/pkg/providers"
)

type backgroundYieldKey struct{}
type backgroundYieldError struct{ checkpoint string }

func (e *backgroundYieldError) Error() string {
	return "真人消息优先，后台本轮让位。已完成工具的回执保存在 " + e.checkpoint +
		"；接续时先读取并复用这些结果，再继续原任务。"
}

func backgroundWantsYield(ctx context.Context, ts *turnState) bool {
	check, _ := ctx.Value(backgroundYieldKey{}).(func() bool)
	return check != nil && check()
}

// Called only between model/tool batches. Never cancels a dispatched mutation.
func saveBackgroundYield(ts *turnState, exec *turnExecution) error {
	results := make([]providers.Message, 0)
	for _, message := range exec.messages {
		if message.Role == "tool" {
			results = append(results, message)
		}
	}
	content, err := json.MarshalIndent(map[string]any{
		"turn_id": ts.turnID, "completed_tool_results": results,
	}, "", "  ")
	if err != nil {
		return err
	}
	directory := filepath.Join(ts.workspace, "state", "background-progress")
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return err
	}
	// Different pending tasks must retain separate continuation receipts.
	path := filepath.Join(directory, fmt.Sprintf("%x.json", sha256.Sum256([]byte(ts.turnID))))
	if err := fileutil.WriteFileAtomic(path, content, 0o600); err != nil {
		return fmt.Errorf("save background continuation: %w", err)
	}
	return &backgroundYieldError{checkpoint: path}
}
