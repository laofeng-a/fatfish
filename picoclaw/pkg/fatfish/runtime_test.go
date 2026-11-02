package fatfish

import (
	"context"
	"errors"
	"testing"
)

func TestFatfishTurnOutcomeUsesOnlyObservedState(t *testing.T) {
	cases := []struct {
		text string
		err  error
		want string
	}{
		{"NO_REPLY", nil, "no_reply"}, {" [NO_REPLY] ", nil, "no_reply"},
		{"ok", nil, "completed"}, {"", nil, "completed"},
		{"", context.Canceled, "interrupted"}, {"", context.DeadlineExceeded, "error"},
		{"", errors.New("credential must never be serialized"), "error"},
	}
	for _, c := range cases {
		if got := TurnOutcome(c.text, c.err); got != c.want {
			t.Fatalf("got %q want %q", got, c.want)
		}
	}
}
