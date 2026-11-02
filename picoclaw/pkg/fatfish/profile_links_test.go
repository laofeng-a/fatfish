package fatfish

import (
	"os"
	"path/filepath"
	"testing"
)

func TestProfileLinksDoNotUseProductionDefaultsInOtherInstances(t *testing.T) {
	t.Setenv("FATFISH_RUNTIME_TOKEN_FILE", "test")
	t.Setenv("FATFISH_PROFILE_DIR", "")
	t.Setenv("FATFISH_STRUCTURED_CORE_FILE", "")
	workspace := t.TempDir()
	if err := EnsureProfileLinks(workspace); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(workspace)
	if err != nil || len(entries) != 0 {
		t.Fatalf("unconfigured instance changed: %v %v", entries, err)
	}
}

func TestProfileLinksReconcileAndKeepOldCopies(t *testing.T) {
	root := t.TempDir()
	workspace, profile := filepath.Join(root, "workspace"), filepath.Join(root, "profile")
	if err := os.MkdirAll(workspace, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(profile, 0o700); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"AGENT.md", "SOUL.md", "USER.md", "HEARTBEAT.md"} {
		if err := os.WriteFile(filepath.Join(profile, name), []byte("current "+name), 0o640); err != nil {
			t.Fatal(err)
		}
	}
	user := filepath.Join(workspace, "USER.md")
	if err := os.WriteFile(user, []byte("stale user"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := reconcileProfileLinks(workspace, profile); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"AGENT.md", "SOUL.md", "USER.md", "HEARTBEAT.md"} {
		path := filepath.Join(workspace, name)
		info, err := os.Lstat(path)
		if err != nil || info.Mode()&os.ModeSymlink == 0 {
			t.Fatalf("%s is not linked: %v", name, err)
		}
		data, err := os.ReadFile(path)
		if err != nil || string(data) != "current "+name {
			t.Fatalf("%s: %q %v", name, data, err)
		}
	}
	backups, err := filepath.Glob(filepath.Join(workspace, "state", "profile-copies", "*"))
	if err != nil || len(backups) != 1 {
		t.Fatalf("backups %v: %v", backups, err)
	}
	old, err := os.ReadFile(backups[0])
	if err != nil || string(old) != "stale user" {
		t.Fatalf("old content lost: %q %v", old, err)
	}
	if err := reconcileProfileLinks(workspace, profile); err != nil {
		t.Fatal(err)
	}
	again, _ := filepath.Glob(filepath.Join(workspace, "state", "profile-copies", "*"))
	if len(again) != 1 {
		t.Fatalf("idempotent check added copies: %v", again)
	}
	if err := os.WriteFile(filepath.Join(profile, "USER.md"), []byte("revised"), 0o640); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(user)
	if string(data) != "revised" {
		t.Fatal("workspace did not see canonical revision")
	}
}
