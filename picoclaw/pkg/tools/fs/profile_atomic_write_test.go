package fstools

import (
	"os"
	"path/filepath"
	"testing"
)

func TestHostAtomicWritePreservesProfileSymlink(t *testing.T) {
	root := t.TempDir()
	target, alias := filepath.Join(root, "profile.md"), filepath.Join(root, "USER.md")
	if err := os.WriteFile(target, []byte("old"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, alias); err != nil {
		t.Fatal(err)
	}
	if err := (&hostFs{}).WriteFile(alias, []byte("new")); err != nil {
		t.Fatal(err)
	}
	info, err := os.Lstat(alias)
	if err != nil || info.Mode()&os.ModeSymlink == 0 {
		t.Fatalf("link replaced: %v", err)
	}
	content, err := os.ReadFile(target)
	if err != nil || string(content) != "new" {
		t.Fatalf("target not updated: %q %v", content, err)
	}
}

func TestHostSymlinkWriteKeepsExistingTargetPermissions(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("requires the unprivileged service test user")
	}
	root := t.TempDir()
	protected := filepath.Join(root, "canonical")
	if err := os.Mkdir(protected, 0o700); err != nil {
		t.Fatal(err)
	}
	target, alias := filepath.Join(protected, "USER.md"), filepath.Join(root, "USER.md")
	if err := os.WriteFile(target, []byte("canonical revision"), 0o400); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, alias); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(protected, 0o500); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(protected, 0o700)
	if err := (&hostFs{}).WriteFile(alias, []byte("replacement")); err == nil {
		t.Fatal("write bypassed canonical directory permissions")
	}
	info, err := os.Lstat(alias)
	if err != nil || info.Mode()&os.ModeSymlink == 0 {
		t.Fatalf("failed write detached the link: %v", err)
	}
	content, err := os.ReadFile(target)
	if err != nil || string(content) != "canonical revision" {
		t.Fatalf("failed write changed canonical revision: %q %v", content, err)
	}
}
