package session

import (
	"context"
	"fmt"

	"github.com/sipeed/picoclaw/pkg/providers"
)

// RetirePrefix uses stable message identities, not a stale keep-last count.
// Appends between summary generation and retirement remain untouched.
func (sm *SessionManager) RetirePrefix(key string, ids []string) error {
	sm.mu.Lock()
	defer sm.mu.Unlock()
	s := sm.sessions[key]
	if s == nil {
		return fmt.Errorf("session not found")
	}
	if !matchesPrefix(s.Messages, ids) {
		return fmt.Errorf("history prefix changed")
	}
	s.Messages = append([]providers.Message(nil), s.Messages[len(ids):]...)
	return nil
}

func matchesPrefix(messages []providers.Message, ids []string) bool {
	if len(messages) < len(ids) {
		return false
	}
	for i, id := range ids {
		if id == "" || messages[i].HistoryID != id {
			return false
		}
	}
	return true
}

func (b *JSONLBackend) RetirePrefix(key string, ids []string) error {
	retirer, ok := b.store.(interface {
		RetirePrefix(context.Context, string, []string) error
	})
	if !ok {
		return fmt.Errorf("source store does not support atomic prefix retirement")
	}
	return retirer.RetirePrefix(context.Background(), b.resolveSessionKey(key), ids)
}
