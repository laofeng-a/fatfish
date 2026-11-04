package agent

import (
	"strings"

	"github.com/sipeed/picoclaw/pkg/providers"
)

func (ts *turnState) historyUserOrigin(msg providers.Message) providers.Message {
	msg.SourceKind = "internal"
	msg.SourceChatID = ts.chatID
	msg.SourceChannel = ts.channel
	msg.SourceMessageID = ts.opts.Dispatch.MessageID()
	if inbound := ts.opts.Dispatch.InboundContext; inbound != nil {
		msg.SourceSenderID = inbound.SenderID
		if ts.channel == "telegram" && msg.SourceMessageID != "" && !strings.Contains(msg.SourceMessageID, ":") && inbound.Raw["sender_is_bot"] != "true" {
			msg.SourceKind = "human"
		}
	}
	return msg
}
