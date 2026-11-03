package telegram

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"

	ta "github.com/mymmrac/telego/telegoapi"
	"github.com/stretchr/testify/require"

	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/media"
)

func TestSendMedia_EmoteFormats(t *testing.T) {
	for _, test := range []struct{ extension, method, field string }{
		{".webm", "sendSticker", "sticker"},
		{".jpg", "sendPhoto", "photo"},
		{".png", "sendPhoto", "photo"},
		{".gif", "sendAnimation", "animation"},
		{".mp4", "sendAnimation", "animation"},
	} {
		t.Run(test.extension, func(t *testing.T) {
			constructor := &multipartRecordingConstructor{}
			caller := &stubCaller{callFn: func(context.Context, string, *ta.RequestData) (*ta.Response, error) {
				return successResponse(t), nil
			}}
			ch := newTestChannelWithConstructor(t, caller, constructor)
			store := media.NewFileMediaStore()
			ch.SetMediaStore(store)
			directory := filepath.Join(t.TempDir(), "assets", "emotes")
			require.NoError(t, os.MkdirAll(directory, 0o755))
			path := filepath.Join(directory, "fixture"+test.extension)
			require.NoError(t, os.WriteFile(path, []byte("fixture-media"), 0o600))
			ref, err := store.Store(path, media.MediaMeta{Filename: filepath.Base(path)}, "test")
			require.NoError(t, err)
			ids, err := ch.SendMedia(context.Background(), bus.OutboundMediaMessage{
				ChatID: "12345", Parts: []bus.MediaPart{{Type: "file", Ref: ref}},
			})
			require.NoError(t, err)
			require.Len(t, ids, 1)
			require.Len(t, caller.calls, 1)
			require.Contains(t, caller.calls[0].URL, test.method)
			require.Equal(t, len("fixture-media"), constructor.calls[0].FileSizes[test.field])
		})
	}
}

func TestSendMedia_EmoteLedgerRequiresSuccessfulDelivery(t *testing.T) {
	enableParagraphPolicy(t)
	directory := filepath.Join(t.TempDir(), "assets", "emotes")
	require.NoError(t, os.MkdirAll(directory, 0o755))
	path := filepath.Join(directory, "one.webm")
	require.NoError(t, os.WriteFile(path, []byte("fixture-media"), 0o600))
	catalogPath, statePath := filepath.Join(directory, "catalog.json"), filepath.Join(directory, "used.json")
	raw, err := json.Marshal([]map[string]string{{"id": "one", "path": path}, {"id": "two"}, {"id": "three"}})
	require.NoError(t, err)
	require.NoError(t, os.WriteFile(catalogPath, raw, 0o600))
	t.Setenv("FATFISH_EMOTE_CATALOG_FILE", catalogPath)
	t.Setenv("FATFISH_EMOTE_STATE_FILE", statePath)
	fail := true
	caller := &stubCaller{callFn: func(context.Context, string, *ta.RequestData) (*ta.Response, error) {
		if fail {
			return nil, errors.New("fixture send rejected")
		}
		return successResponse(t), nil
	}}
	ch := newTestChannelWithConstructor(t, caller, &multipartRecordingConstructor{})
	store := media.NewFileMediaStore()
	ch.SetMediaStore(store)
	ref, err := store.Store(path, media.MediaMeta{Filename: "one.webm"}, "test")
	require.NoError(t, err)
	message := bus.OutboundMediaMessage{ChatID: "12345", Parts: []bus.MediaPart{{Type: "file", Ref: ref}}}
	_, err = ch.SendMedia(context.Background(), message)
	require.Error(t, err)
	_, err = os.Stat(statePath)
	require.True(t, os.IsNotExist(err))
	fail = false
	_, err = ch.SendMedia(context.Background(), message)
	require.NoError(t, err)
	state, err := loadEmoteState(statePath)
	require.NoError(t, err)
	require.EqualValues(t, 1, state["sent_count"].(map[string]any)["one"])
	before := len(caller.calls)
	_, err = ch.SendMedia(context.Background(), message)
	require.ErrorContains(t, err, "EMOTE-RULE")
	require.Len(t, caller.calls, before)
}

func TestTelegramEmoteRoutingPreservesOrdinaryFiles(t *testing.T) {
	require.Equal(t, "video", telegramMediaType("video", "/tmp/movie.mp4"))
	require.Equal(t, "file", telegramMediaType("file", "/tmp/movie.webm"))
	require.Equal(t, "file", telegramMediaType("file", "/tmp/assets/emotes-backup/movie.webm"))
	require.Equal(t, "sticker", telegramMediaType("file", "/tmp/assets/emotes/STICKER.WEBM"))
}

func TestEmotePhotosKeepIndividualLedgerPath(t *testing.T) {
	store := media.NewFileMediaStore()
	root := t.TempDir()
	emotePath, ordinaryPath := filepath.Join(root, "assets", "emotes", "photo.png"), filepath.Join(root, "photo.png")
	require.NoError(t, os.MkdirAll(filepath.Dir(emotePath), 0o755))
	require.NoError(t, os.WriteFile(emotePath, []byte("fixture"), 0o600))
	require.NoError(t, os.WriteFile(ordinaryPath, []byte("fixture"), 0o600))
	emote, err := store.Store(emotePath, media.MediaMeta{}, "test")
	require.NoError(t, err)
	ordinary, err := store.Store(ordinaryPath, media.MediaMeta{}, "test")
	require.NoError(t, err)
	require.True(t, telegramHasEmoteAssets(store, []bus.MediaPart{{Type: "image", Ref: emote}, {Type: "image", Ref: ordinary}}))
	require.False(t, telegramHasEmoteAssets(store, []bus.MediaPart{{Type: "image", Ref: ordinary}, {Type: "image", Ref: ordinary}}))
}

func TestEmoteLedgerHalfRuleAndConfirmedReceipt(t *testing.T) {
	directory := t.TempDir()
	catalogPath, statePath := filepath.Join(directory, "catalog.json"), filepath.Join(directory, "used.json")
	catalog := []map[string]string{}
	for _, id := range []string{"one", "two", "three", "four", "five", "six"} {
		catalog = append(catalog, map[string]string{"id": id, "path": "/assets/emotes/" + id + ".webm"})
	}
	raw, err := json.Marshal(catalog)
	require.NoError(t, err)
	require.NoError(t, os.WriteFile(catalogPath, raw, 0o600))
	require.NoError(t, os.WriteFile(statePath, []byte(`{"cycle":7,"used":{"one":"old","removed":"old"},"sent_count":{"one":1}}`), 0o600))
	_, err = prepareEmoteUse("/assets/emotes/one.webm", catalogPath, statePath)
	require.ErrorContains(t, err, "EMOTE-RULE")
	fresh, err := prepareEmoteUse("/assets/emotes/two.webm", catalogPath, statePath)
	require.NoError(t, err)
	state, err := loadEmoteState(statePath)
	require.NoError(t, err)
	require.Nil(t, state["used"].(map[string]any)["two"], "selection alone must not mark")
	require.NoError(t, fresh.record("owner", 42))
	require.NoError(t, fresh.record("owner", 42))
	state, err = loadEmoteState(statePath)
	require.NoError(t, err)
	require.EqualValues(t, 7, state["cycle"])
	require.EqualValues(t, 1, state["sent_count"].(map[string]any)["two"])
	_, err = prepareEmoteUse("/assets/emotes/one.webm", catalogPath, statePath)
	require.Error(t, err, "removed IDs do not count toward half")
	third, err := prepareEmoteUse("/assets/emotes/three.webm", catalogPath, statePath)
	require.NoError(t, err)
	require.NoError(t, third.record("owner", 43))
	_, err = prepareEmoteUse("/assets/emotes/one.webm", catalogPath, statePath)
	require.NoError(t, err, "exactly half permits repeats")
}
