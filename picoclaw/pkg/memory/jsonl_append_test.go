package memory

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

type failingAppend struct {
	*os.File
	failWrite bool
	failSync  bool
}

func (f *failingAppend) Write(p []byte) (int, error) {
	if f.failWrite {
		n, _ := f.File.Write(p[:len(p)/2])
		return n, errors.New("disk full")
	}
	return f.File.Write(p)
}
func (f *failingAppend) Sync() error {
	if f.failSync {
		f.failSync = false
		return errors.New("sync failed")
	}
	return f.File.Sync()
}
func TestJSONLAppendRollbackAfterWriteAndSyncFailure(t *testing.T) {
	for _, failWrite := range []bool{true, false} {
		t.Run(map[bool]string{true: "write", false: "sync"}[failWrite], func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "session.jsonl")
			before := []byte("{\"role\":\"user\",\"content\":\"kept\"}\n")
			if err := os.WriteFile(path, before, 0600); err != nil {
				t.Fatal(err)
			}
			file, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0600)
			if err != nil {
				t.Fatal(err)
			}
			bad := &failingAppend{File: file, failWrite: failWrite, failSync: !failWrite}
			if err := appendJSONLRecord(bad, []byte("{\"role\":\"assistant\",\"content\":\"lost\"}\n")); err == nil {
				t.Fatal("expected storage error")
			}
			if err := appendJSONLRecord(file, []byte("{\"role\":\"assistant\",\"content\":\"recovered\"}\n")); err != nil {
				t.Fatal(err)
			}
			file.Close()
			history, err := readMessages(path, 0)
			if err != nil {
				t.Fatal(err)
			}
			if len(history) != 2 || history[0].Content != "kept" || history[1].Content != "recovered" {
				t.Fatalf("wrong recovered history: %+v", history)
			}
		})
	}
}
