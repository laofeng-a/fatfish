package telegram

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

func telegramEmoteAsset(path string) bool {
	return strings.Contains("/"+filepath.ToSlash(filepath.Clean(path)), "/assets/emotes/")
}

func telegramMediaType(kind, path string) string {
	if !telegramEmoteAsset(path) {
		return kind
	}
	switch strings.ToLower(filepath.Ext(path)) {
	case ".webm":
		return "sticker"
	case ".gif", ".mp4":
		return "animation"
	case ".jpg", ".jpeg", ".png":
		return "image"
	}
	return kind
}

func emoteCatalogPath() string {
	if path := os.Getenv("FATFISH_EMOTE_CATALOG_FILE"); path != "" {
		return path
	}
	return "/opt/picoclaw/assets/emotes/catalog.json"
}

func emoteStatePath() string {
	if path := os.Getenv("FATFISH_EMOTE_STATE_FILE"); path != "" {
		return path
	}
	return "/var/lib/picoclaw/.picoclaw/workspace/state/emote-used.json"
}

type emoteUse struct {
	id, statePath string
}

func loadEmoteState(path string) (map[string]any, error) {
	state := map[string]any{}
	raw, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		return nil, err
	}
	if err == nil {
		if err = json.Unmarshal(raw, &state); err != nil {
			return nil, err
		}
	}
	if state == nil {
		return nil, fmt.Errorf("invalid emote ledger")
	}
	for _, key := range []string{"used", "sent_count", "receipts", "last_sent"} {
		if state[key] == nil {
			state[key] = map[string]any{}
		}
		if _, ok := state[key].(map[string]any); !ok {
			return nil, fmt.Errorf("invalid emote ledger field %s", key)
		}
	}
	return state, nil
}

func prepareEmoteUse(path, catalogPath, statePath string) (*emoteUse, error) {
	var catalog []struct{ ID, Path string }
	raw, err := os.ReadFile(catalogPath)
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(raw, &catalog); err != nil {
		return nil, err
	}
	state, err := loadEmoteState(statePath)
	if err != nil {
		return nil, err
	}
	used := state["used"].(map[string]any)
	id, count := "", 0
	for _, item := range catalog {
		if _, ok := used[item.ID]; ok {
			count++
		}
		if filepath.Clean(item.Path) == filepath.Clean(path) {
			id = item.ID
		}
	}
	if id == "" {
		return nil, fmt.Errorf("file is absent from the emote catalog")
	}
	if _, repeated := used[id]; repeated && count*2 < len(catalog) {
		return nil, fmt.Errorf("EMOTE-RULE: %s already used; library usage %d/%d is below half", id, count, len(catalog))
	}
	return &emoteUse{id: id, statePath: statePath}, nil
}

func (e *emoteUse) record(chat string, messageID int) error {
	state, err := loadEmoteState(e.statePath)
	if err != nil {
		return err
	}
	receipts := state["receipts"].(map[string]any)
	key := fmt.Sprintf("%s:%d", chat, messageID)
	if receipts[key] == e.id {
		return nil
	}
	now := time.Now().Format("2006-01-02T15:04:05-0700")
	used := state["used"].(map[string]any)
	if _, exists := used[e.id]; !exists {
		used[e.id] = now
	}
	counts := state["sent_count"].(map[string]any)
	previous, _ := counts[e.id].(float64)
	counts[e.id] = int(previous) + 1
	state["last_sent"].(map[string]any)[e.id] = now
	receipts[key] = e.id
	raw, err := json.MarshalIndent(state, "", " ")
	if err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(e.statePath), ".emote-used-*")
	if err != nil {
		return err
	}
	name := file.Name()
	defer os.Remove(name)
	if _, err = file.Write(append(raw, '\n')); err != nil {
		file.Close()
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	return os.Rename(name, e.statePath)
}
