package fatfish

import (
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// EnsureProfileLinks keeps the harness and versioned memory writer on one set
// of profile files. It runs as the existing harness user, without new privileges.
func EnsureProfileLinks(workspace string) error {
	if !Enabled() {
		return nil
	}
	profile := os.Getenv("FATFISH_PROFILE_DIR")
	if profile == "" {
		core := os.Getenv("FATFISH_STRUCTURED_CORE_FILE")
		if core == "" {
			return nil
		}
		profile = filepath.Join(filepath.Dir(core), "profile")
	}
	return reconcileProfileLinks(workspace, profile)
}

func reconcileProfileLinks(workspace, profile string) error {
	for _, name := range []string{"AGENT.md", "SOUL.md", "USER.md", "HEARTBEAT.md"} {
		target, err := filepath.Abs(filepath.Join(profile, name))
		if err != nil {
			return err
		}
		info, err := os.Stat(target)
		if os.IsNotExist(err) {
			continue // A new installation may not have written this profile yet.
		}
		if err != nil || !info.Mode().IsRegular() {
			return fmt.Errorf("profile %s is unreadable or not a regular file", name)
		}
		path := filepath.Join(workspace, name)
		if current, resolveErr := filepath.EvalSymlinks(path); resolveErr == nil {
			if stat, statErr := os.Lstat(path); statErr == nil &&
				stat.Mode()&os.ModeSymlink != 0 && current == target {
				continue
			}
		}
		previous := ""
		if _, statErr := os.Lstat(path); statErr == nil {
			folder := filepath.Join(workspace, "state", "profile-copies")
			if err := os.MkdirAll(folder, 0o700); err != nil {
				return err
			}
			previous = filepath.Join(folder, name+"-"+time.Now().UTC().Format("20060102T150405.000000000"))
			if err := os.Rename(path, previous); err != nil {
				return err
			}
		} else if !os.IsNotExist(statErr) {
			return statErr
		}
		if err := os.Symlink(target, path); err != nil {
			if previous != "" {
				_ = os.Rename(previous, path)
			}
			return fmt.Errorf("link profile %s: %w", name, err)
		}
	}
	return nil
}
