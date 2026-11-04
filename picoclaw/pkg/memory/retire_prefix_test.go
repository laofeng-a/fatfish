package memory

import (
	"context"
	"testing"

	"github.com/sipeed/picoclaw/pkg/providers"
)

func TestRetirePrefixKeepsMessagesAppendedAfterSnapshot(t *testing.T) {
	s, err := NewJSONLStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	for _, id := range []string{"old", "recent", "new-arrival"} {
		if err = s.AddFullMessage(ctx, "session", providers.Message{HistoryID: id, Role: "user", Content: id}); err != nil {
			t.Fatal(err)
		}
	}
	if err = s.RetirePrefix(ctx, "session", []string{"old"}); err != nil {
		t.Fatal(err)
	}
	got, err := s.GetHistory(ctx, "session")
	if err != nil || len(got) != 2 || got[0].HistoryID != "recent" || got[1].HistoryID != "new-arrival" {
		t.Fatal(got, err)
	}
	if err = s.RetirePrefix(ctx, "session", []string{"wrong"}); err == nil {
		t.Fatal("mismatched prefix retired")
	}
	got, _ = s.GetHistory(ctx, "session")
	if len(got) != 2 {
		t.Fatal("failed retirement changed history")
	}
}
