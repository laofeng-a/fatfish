package agent

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestSelfManagedUserSymlinkRefreshesCachedContext(t *testing.T) {
	workspace := t.TempDir()
	profile := t.TempDir()
	target := filepath.Join(profile, "USER.md")
	if err := os.WriteFile(target, []byte("old-user-preference"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(workspace, "USER.md")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(workspace, "AGENT.md"), []byte("agent instructions"), 0o600); err != nil {
		t.Fatal(err)
	}
	cb := NewContextBuilder(workspace)
	if !strings.Contains(cb.BuildSystemPromptWithCache(), "old-user-preference") {
		t.Fatal("initial preference missing")
	}
	pending := target + ".pending"
	if err := os.WriteFile(pending, []byte("new-user-preference"), 0o640); err != nil {
		t.Fatal(err)
	}
	stamp := time.Now().Add(2 * time.Second)
	if err := os.Chtimes(pending, stamp, stamp); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(pending, target); err != nil {
		t.Fatal(err)
	}
	prompt := cb.BuildSystemPromptWithCache()
	if !strings.Contains(prompt, "new-user-preference") || strings.Contains(prompt, "old-user-preference") {
		t.Fatal("atomic target replacement did not refresh cached context")
	}
}
