package telegram

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"github.com/mymmrac/telego"
	"github.com/stretchr/testify/require"

	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/channels"
	"github.com/sipeed/picoclaw/pkg/config"
	"github.com/sipeed/picoclaw/pkg/media"
)

// Explicit opt-in: this test sends one real catalog item and records its receipt.
func TestLiveEmoteReceipt(t *testing.T) {
	path, chat := os.Getenv("FATFISH_LIVE_EMOTE_PATH"), os.Getenv("FATFISH_LIVE_EMOTE_CHAT")
	if path == "" || chat == "" {
		t.Skip("real send requires explicit asset and chat")
	}
	raw, err := os.ReadFile("/var/lib/picoclaw/.picoclaw/config.json")
	require.NoError(t, err)
	var cfg struct {
		ChannelList map[string]struct {
			Settings struct{ Token string }
		} `json:"channel_list"`
	}
	require.NoError(t, json.Unmarshal(raw, &cfg))
	bot, err := telego.NewBot(cfg.ChannelList["telegram"].Settings.Token, telego.WithDiscardLogger())
	require.NoError(t, err)
	base := channels.NewBaseChannel("telegram", nil, nil, nil, channels.WithMaxMessageLength(4000))
	base.SetRunning(true)
	ch := &TelegramChannel{BaseChannel: base, bot: bot, chatIDs: make(map[string]int64),
		bc:    &config.Channel{Type: config.ChannelTelegram, Enabled: true},
		tgCfg: &config.TelegramSettings{}, progress: channels.NewToolFeedbackAnimator(nil)}
	store := media.NewFileMediaStore()
	ch.SetMediaStore(store)
	ref, err := store.Store(path, media.MediaMeta{Filename: filepath.Base(path), CleanupPolicy: media.CleanupPolicyForgetOnly}, "live-emote-acceptance")
	require.NoError(t, err)
	ids, err := ch.SendMedia(context.Background(), bus.OutboundMediaMessage{
		ChatID: chat, SessionKey: "cli:emote-acceptance", Parts: []bus.MediaPart{{Type: "file", Ref: ref}},
	})
	require.NoError(t, err)
	require.Len(t, ids, 1)
	fmt.Printf("LIVE_EMOTE_RECEIPT message_id=%s media_type=%s recorded=true\n", ids[0], telegramMediaType("file", path))
}
