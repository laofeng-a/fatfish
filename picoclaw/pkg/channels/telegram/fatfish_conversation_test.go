package telegram

import (
	"context"
	"github.com/mymmrac/telego"
	ta "github.com/mymmrac/telego/telegoapi"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/channels"
)

func TestFatfishDoubleMentionKeepsOriginalTextForModel(t *testing.T) {
	enableParagraphPolicy(t)
	ch := newTestChannel(t, &stubCaller{callFn: func(_ context.Context, _ string, _ *ta.RequestData) (*ta.Response, error) {
		return successUserResponse(t, &telego.User{ID: 8816926280, Username: "fissssshhhhhhhhh_bot", IsBot: true}), nil
	}})
	messageBus := bus.NewMessageBus()
	ch.BaseChannel = channels.NewBaseChannel("telegram", nil, messageBus, nil)
	ch.ctx = context.Background()
	msg := conversationMessage(318, 456, -99, "@fissssshhhhhhhhh_bot @fiiiiiiiiiiishbot token来")
	msg.Chat.Type = "group"
	msg.Entities = []telego.MessageEntity{{Type: telego.EntityTypeMention, Offset: 0, Length: len("@fissssshhhhhhhhh_bot")}}
	if err := ch.handleMessages(context.Background(), []*telego.Message{msg}); err != nil {
		t.Fatal(err)
	}
	select {
	case inbound := <-messageBus.InboundChan():
		if !strings.Contains(inbound.Content, msg.Text) {
			t.Fatalf("lost original double mention: %q", inbound.Content)
		}
	case <-time.After(time.Second):
		t.Fatal("double mention not delivered to model")
	}
}

func TestFatfishBotIdentityUsesTelegramMetadata(t *testing.T) {
	msg := conversationMessage(4, 888, -9, "I am jg")
	msg.From.IsBot = true
	msg.From.Username = "peer_bot"
	msg.MessageThreadID = 55
	data := fatfishInboundData(msg, "3")
	if data["sender_is_bot"] != true || data["sender_id"] != "888" || data["thread_id"] != "55" || data["sender_username"] != "peer_bot" {
		t.Fatalf("untrusted sender metadata: %v", data)
	}
}

func TestFatfishPassiveBotDoesNotEnterConversationBuffer(t *testing.T) {
	enableParagraphPolicy(t)
	http.DefaultTransport = paragraphRoundTripper(func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"allowed":true,"handled":true}`))}, nil
	})
	ch := newTestChannel(t, &stubCaller{})
	ch.fatfishBatches = newConversationBuffer(time.Hour, time.Hour, func([]*telego.Message) { t.Error("passive bot triggered delivery") })
	msg := conversationMessage(4, 888, -9, "hello")
	msg.From.IsBot = true
	if err := ch.handleMessage(context.Background(), msg); err != nil {
		t.Fatal(err)
	}
	if len(ch.fatfishBatches.batches) != 0 {
		t.Fatal("passive bot was buffered")
	}
}

func TestFatfishHumanTurnIncludesQuotedBotContext(t *testing.T) {
	enableParagraphPolicy(t)
	http.DefaultTransport = paragraphRoundTripper(func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"allowed":true,"context":"[其他机器人原话] peer_bot: 月亮是方的"}`))}, nil
	})
	messageBus := bus.NewMessageBus()
	ch := &TelegramChannel{BaseChannel: channels.NewBaseChannel("telegram", nil, messageBus, nil), chatIDs: make(map[string]int64), ctx: context.Background()}
	msg := conversationMessage(5, 123, -9, "你怎么看她说的")
	msg.Chat.Type = "group"
	if err := ch.handleMessages(context.Background(), []*telego.Message{msg}); err != nil {
		t.Fatal(err)
	}
	select {
	case inbound := <-messageBus.InboundChan():
		if !strings.Contains(inbound.Content, "peer_bot: 月亮是方的") || !strings.Contains(inbound.Content, "你怎么看她说的") || inbound.Context.SenderID != "123" {
			t.Fatalf("missing context or changed real sender: %+v", inbound)
		}
	case <-time.After(time.Second):
		t.Fatal("human turn was suppressed")
	}
}

func conversationMessage(id int, sender, chat int64, text string) *telego.Message {
	return &telego.Message{MessageID: id, Text: text, Chat: telego.Chat{ID: chat}, From: &telego.User{ID: sender}}
}
func TestFatfishConversationMergesWithoutMixingSenders(t *testing.T) {
	var batches [][]*telego.Message
	b := newConversationBuffer(time.Hour, time.Hour, func(m []*telego.Message) { batches = append(batches, m) })
	b.push(conversationMessage(3, 1, -9, "最后"))
	b.push(conversationMessage(1, 1, -9, "先说"))
	b.push(conversationMessage(1, 1, -9, "重复"))
	b.push(conversationMessage(2, 2, -9, "另一个人"))
	b.flushKey("-9:0:1", 0)
	if len(batches) != 1 || len(batches[0]) != 2 || batches[0][0].Text != "先说" || batches[0][1].Text != "最后" {
		t.Fatalf("wrong merge: %+v", batches)
	}
	b.flushAll()
	if len(batches) != 2 || batches[1][0].From.ID != 2 {
		t.Fatal("senders mixed")
	}
}
func TestFatfishConversationStaleTimerCannotFlushNewBatch(t *testing.T) {
	count := 0
	b := newConversationBuffer(time.Hour, time.Hour, func(m []*telego.Message) { count++ })
	m := conversationMessage(1, 1, 9, "first")
	b.push(m)
	old := b.batches[conversationKey(m)].generation
	b.flushAll()
	b.push(conversationMessage(2, 1, 9, "new"))
	b.flushKey(conversationKey(m), old)
	if count != 1 {
		t.Fatal("stale callback flushed newer batch")
	}
	b.flushAll()
}
func TestFatfishConversationMaximumWaitAndCollectibleMetadata(t *testing.T) {
	delivered := make(chan []*telego.Message, 1)
	b := newConversationBuffer(time.Hour, 20*time.Millisecond, func(m []*telego.Message) { delivered <- m })
	b.push(conversationMessage(1, 1, 9, "one"))
	select {
	case <-delivered:
	case <-time.After(time.Second):
		t.Fatal("maximum wait ignored")
	}
	msg := conversationMessage(2, 1, 9, "")
	msg.Sticker = &telego.Sticker{FileID: "opaque", FileUniqueID: "unique", Emoji: "😂"}
	got := collectibleMedia(msg)
	if got["file_id"] != "opaque" || got["kind"] != "sticker" {
		t.Fatalf("bad media: %v", got)
	}
}
