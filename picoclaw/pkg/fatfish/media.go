package fatfish

import (
	"os"
	"path/filepath"
	"sort"
	"time"
)

// PruneMedia operates only in a dedicated generated-file cache, never on user assets.
func PruneMedia(directory string) {
	entries, err := os.ReadDir(directory)
	if err != nil {
		return
	}
	type entry struct {
		path     string
		size     int64
		modified time.Time
	}
	var files []entry
	var total int64
	for _, e := range entries {
		if !e.Type().IsRegular() {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		files = append(files, entry{filepath.Join(directory, e.Name()), info.Size(), info.ModTime()})
		total += info.Size()
	}
	sort.Slice(files, func(i, j int) bool { return files[i].modified.Before(files[j].modified) })
	for _, e := range files {
		if time.Since(e.modified) > 72*time.Hour || total > 64*1024*1024 {
			if os.Remove(e.path) == nil {
				total -= e.size
			}
		}
	}
}
