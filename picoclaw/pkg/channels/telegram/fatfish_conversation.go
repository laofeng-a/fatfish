package telegram

import (
	"context"
	"fmt"
	"github.com/mymmrac/telego"
	"github.com/sipeed/picoclaw/pkg/fatfish"
	"slices"
	"sync"
	"time"
)

type conversationBatch struct {
	messages   []*telego.Message
	timer      *time.Timer
	first      time.Time
	generation uint64
}
type conversationBuffer struct {
	mu             sync.Mutex
	batches        map[string]*conversationBatch
	delay, maximum time.Duration
	deliver        func([]*telego.Message)
	generation     uint64
}

func newConversationBuffer(delay, maximum time.Duration, deliver func([]*telego.Message)) *conversationBuffer {
	return &conversationBuffer{batches: make(map[string]*conversationBatch), delay: delay, maximum: maximum, deliver: deliver}
}
func conversationKey(msg *telego.Message) string {
	return fmt.Sprintf("%d:%d:%d", msg.Chat.ID, msg.MessageThreadID, msg.From.ID)
}
func (b *conversationBuffer) push(msg *telego.Message) {
	key := conversationKey(msg)
	copy := *msg
	copy.Photo = append([]telego.PhotoSize(nil), msg.Photo...)
	b.mu.Lock()
	group := b.batches[key]
	var overflow []*telego.Message
	if group == nil {
		if len(b.batches) >= 64 {
			oldestKey := ""
			var oldest *conversationBatch
			for candidate, item := range b.batches {
				if oldest == nil || item.first.Before(oldest.first) {
					oldestKey = candidate
					oldest = item
				}
			}
			if oldest != nil {
				delete(b.batches, oldestKey)
				if oldest.timer != nil {
					oldest.timer.Stop()
				}
				overflow = append(overflow, oldest.messages...)
			}
		}
		group = &conversationBatch{first: time.Now()}
		b.batches[key] = group
	}
	for _, old := range group.messages {
		if old.MessageID == msg.MessageID {
			b.mu.Unlock()
			return
		}
	}
	group.messages = append(group.messages, &copy)
	b.generation++
	group.generation = b.generation
	generation := group.generation
	if group.timer != nil {
		group.timer.Stop()
	}
	delay := min(b.delay, max(time.Duration(0), b.maximum-time.Since(group.first)))
	if len(group.messages) >= 16 {
		delay = 0
	}
	group.timer = time.AfterFunc(delay, func() { b.flushKey(key, generation) })
	b.mu.Unlock()
	if len(overflow) > 0 {
		slices.SortFunc(overflow, func(a, c *telego.Message) int { return a.MessageID - c.MessageID })
		b.deliver(overflow)
	}
}
func (b *conversationBuffer) flushKey(key string, generation uint64) {
	b.mu.Lock()
	group := b.batches[key]
	if group == nil || (generation != 0 && generation != group.generation) {
		b.mu.Unlock()
		return
	}
	delete(b.batches, key)
	if group.timer != nil {
		group.timer.Stop()
	}
	messages := append([]*telego.Message(nil), group.messages...)
	b.mu.Unlock()
	slices.SortFunc(messages, func(a, c *telego.Message) int { return a.MessageID - c.MessageID })
	b.deliver(messages)
}
func (b *conversationBuffer) flushAll() {
	b.mu.Lock()
	keys := make([]string, 0, len(b.batches))
	for key := range b.batches {
		keys = append(keys, key)
	}
	b.mu.Unlock()
	for _, key := range keys {
		b.flushKey(key, 0)
	}
}
func collectibleMedia(msg *telego.Message) map[string]any {
	if msg == nil {
		return nil
	}
	if s := msg.Sticker; s != nil {
		return map[string]any{"kind": "sticker", "file_id": s.FileID, "unique_id": s.FileUniqueID, "emoji": s.Emoji}
	}
	if a := msg.Animation; a != nil {
		return map[string]any{"kind": "animation", "file_id": a.FileID, "unique_id": a.FileUniqueID}
	}
	if len(msg.Photo) > 0 {
		p := msg.Photo[len(msg.Photo)-1]
		return map[string]any{"kind": "image", "file_id": p.FileID, "unique_id": p.FileUniqueID}
	}
	return nil
}
func (c *TelegramChannel) fatfishInbound(ctx context.Context, message *telego.Message, phases ...string) (fatfish.Decision, error) {
	reply := ""
	if message.ReplyToMessage != nil {
		reply = fmt.Sprint(message.ReplyToMessage.MessageID)
	}
	data := fatfishInboundData(message, reply)
	data["delivery_phase"] = "deliver"
	if len(phases) > 0 {
		data["delivery_phase"] = phases[0]
	}
	data["mentioned"] = c.isBotMentioned(message)
	data["reply_to_self"] = message.ReplyToMessage != nil && c.isOwnBotUser(message.ReplyToMessage.From)
	return fatfish.Call(ctx, "inbound", data)
}

func fatfishInboundData(message *telego.Message, reply string) map[string]any {
	return map[string]any{"chat_id": fmt.Sprint(message.Chat.ID), "sender_id": fmt.Sprint(message.From.ID), "sender_is_bot": message.From.IsBot, "sender_username": message.From.Username, "sender_name": message.From.FirstName, "thread_id": fmt.Sprint(message.MessageThreadID), "message_id": fmt.Sprint(message.MessageID), "text": message.Text, "caption": message.Caption, "forwarded": message.ForwardOrigin != nil, "chat_type": message.Chat.Type, "reply_to_message_id": reply, "media": collectibleMedia(message), "reply_media": collectibleMedia(message.ReplyToMessage)}
}
