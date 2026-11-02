package heartbeat

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSelfManagedHeartbeatReadsReplacedTarget(t *testing.T) {
	workspace := t.TempDir()
	target := filepath.Join(t.TempDir(), "HEARTBEAT.md")
	if err := os.WriteFile(target, []byte("- old-reading-topic"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(workspace, "HEARTBEAT.md")); err != nil {
		t.Fatal(err)
	}
	hs := &HeartbeatService{workspace: workspace}
	if !strings.Contains(hs.buildPrompt(), "old-reading-topic") {
		t.Fatal("initial task missing")
	}
	if err := os.WriteFile(target+".pending", []byte("- new-reading-topic"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(target+".pending", target); err != nil {
		t.Fatal(err)
	}
	prompt := hs.buildPrompt()
	if !strings.Contains(prompt, "new-reading-topic") || strings.Contains(prompt, "old-reading-topic") {
		t.Fatal("next tick kept stale heartbeat contents")
	}
}
