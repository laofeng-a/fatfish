package session

import (
	"crypto/rand"
	"encoding/hex"
	"time"

	"github.com/sipeed/picoclaw/pkg/providers"
)

func stampMessage(msg providers.Message) providers.Message {
	if msg.HistoryID == "" {
		if msg.SourceKind == "human" && msg.SourceChatID != "" && msg.SourceMessageID != "" {
			msg.HistoryID = "inbound:" + msg.SourceChatID + ":" + msg.SourceMessageID
		} else {
			var raw [16]byte
			if _, err := rand.Read(raw[:]); err != nil {
				panic(err)
			}
			msg.HistoryID = "message:" + hex.EncodeToString(raw[:])
		}
	}
	ensureMessageCreatedAt(&msg, time.Now())
	return msg
}
