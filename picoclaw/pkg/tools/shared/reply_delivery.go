package toolshared

import (
	"context"
	"sync"
)

type replyDeliveryKey struct{}
type replyDeliveryState struct {
	mu        sync.Mutex
	messageID string
	targets   map[string]bool
}

func HasReplyDeliveryTracking(ctx context.Context) bool {
	_, ok := ctx.Value(replyDeliveryKey{}).(*replyDeliveryState)
	return ok
}

// One state belongs to one running conversation round, never a global session.
func WithReplyDeliveryTracking(ctx context.Context) context.Context {
	if _, ok := ctx.Value(replyDeliveryKey{}).(*replyDeliveryState); ok {
		return ctx
	}
	return context.WithValue(ctx, replyDeliveryKey{}, &replyDeliveryState{targets: map[string]bool{}})
}

func BeginReplyDeliveryMessage(ctx context.Context, messageID string) {
	if s, ok := ctx.Value(replyDeliveryKey{}).(*replyDeliveryState); ok {
		s.mu.Lock()
		defer s.mu.Unlock()
		if s.messageID != messageID {
			s.messageID = messageID
			s.targets = map[string]bool{}
		}
	}
}

func ReplyDeliveryMessageID(ctx context.Context) string {
	if s, ok := ctx.Value(replyDeliveryKey{}).(*replyDeliveryState); ok {
		s.mu.Lock()
		defer s.mu.Unlock()
		return s.messageID
	}
	return ""
}

func RecordReplyDelivery(ctx context.Context, channel, chatID string) {
	if s, ok := ctx.Value(replyDeliveryKey{}).(*replyDeliveryState); ok {
		s.mu.Lock()
		defer s.mu.Unlock()
		s.targets[channel+"\x00"+chatID] = true
	}
}

func HasReplyDelivery(ctx context.Context, channel, chatID string) bool {
	if s, ok := ctx.Value(replyDeliveryKey{}).(*replyDeliveryState); ok {
		s.mu.Lock()
		defer s.mu.Unlock()
		return s.targets[channel+"\x00"+chatID]
	}
	return false
}
