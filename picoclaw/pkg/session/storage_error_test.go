package session

import (
	"context"
	"errors"
	"testing"

	"github.com/sipeed/picoclaw/pkg/memory"
	"github.com/sipeed/picoclaw/pkg/providers"
)

type recoveryErrorStore struct {
	memory.Store
	fail     bool
	compacts int
}

func (s *recoveryErrorStore) AddFullMessage(context.Context, string, providers.Message) error {
	if s.fail {
		return errors.New("disk full")
	}
	return nil
}
func (s *recoveryErrorStore) Compact(context.Context, string) error { s.compacts++; return nil }
func TestJSONLWriteFailureReachesReceiptHandlerThroughSave(t *testing.T) {
	store := &recoveryErrorStore{fail: true}
	backend := NewJSONLBackend(store)
	backend.AddFullMessage("task", providers.Message{Role: "tool", Content: "executor already completed"})
	if backend.Save("task") == nil {
		t.Fatal("write failure was hidden from receipt handler")
	}
	if store.compacts != 0 {
		t.Fatal("compaction must not conceal a failed append")
	}
	store.fail = false
	backend.AddMessage("task", "assistant", "recovered receipt")
	if err := backend.Save("task"); err != nil {
		t.Fatal(err)
	}
}
