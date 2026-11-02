package agent

import (
	"context"
	"testing"

	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/fatfish"
)

func TestInternalOutputNeverPublishesButNormalSharesDo(t *testing.T) {
	p := &codexWakeProvider{}
	al := newCodexWakeTestLoop(t, p)
	ctx := context.Background()
	cases := []struct {
		ctx           context.Context
		session, text string
	}{
		{ctx, "private", "[Output history: delivery facts, not an additional message to the user]\nConfirmed sent text: duplicate"},
		{ctx, "heartbeat", "已记录\nHEARTBEAT_OK"},
		{fatfish.WithCodexEvent(ctx, codexWakeEvent()), "private", "已记录\nHEARTBEAT_OK"},
	}
	for _, tc := range cases {
		al.PublishResponseIfNeeded(tc.ctx, "telegram", "123", tc.session, tc.text)
	}
	select {
	case <-al.bus.(*bus.MessageBus).OutboundChan():
		t.Fatal("internal output reached outbound bus")
	default:
	}
	al.PublishResponseIfNeeded(ctx, "telegram", "123", "heartbeat", "这个挺有意思，给你看看。")
	select {
	case msg := <-al.bus.(*bus.MessageBus).OutboundChan():
		if msg.Content != "这个挺有意思，给你看看。" {
			t.Fatal(msg.Content)
		}
	default:
		t.Fatal("normal background share was suppressed")
	}
}
