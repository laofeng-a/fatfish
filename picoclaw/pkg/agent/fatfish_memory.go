package agent

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/sipeed/picoclaw/pkg/session"
)

// Memory files are read per private request, not put in the shared static cache.
func boundedMemory(path string, limit int) string {
	file, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return ""
	}
	raw, err := io.ReadAll(io.LimitReader(file, int64(limit)))
	if err != nil {
		return ""
	}
	for len(raw) > 0 && !utf8.Valid(raw) {
		raw = raw[:len(raw)-1]
	}
	return string(raw)
}

func (cb *ContextBuilder) fatfishMemoryContext() string {
	var text strings.Builder
	core := os.Getenv("FATFISH_STRUCTURED_CORE_FILE")
	if core != "" {
		if content := boundedMemory(core, 6000); content != "" {
			text.WriteString("# 有来源的结构化核心记忆\n" + content + "\n\n")
		}
	}
	root := filepath.Join(cb.workspace, "memory")
	if content := boundedMemory(filepath.Join(root, "MEMORY.md"), 8000); content != "" {
		text.WriteString("# 自己整理的长期文字记忆（事实、评价和玩笑按记录区分）\n" + content + "\n\n")
	}
	zone := time.FixedZone("UTC+8", 8*3600)
	now := time.Now().In(zone)
	var diaryPaths []string
	for offset := 0; offset < 3; offset++ {
		day := now.AddDate(0, 0, -offset)
		name := filepath.Join(day.Format("200601"), day.Format("20060102")+".md")
		if info, err := os.Stat(filepath.Join(root, name)); err == nil && info.Mode().IsRegular() {
			diaryPaths = append(diaryPaths, name)
		}
	}
	if len(diaryPaths) > 0 {
		text.WriteString("# 近日日记目录\n具体经历用memory_find按话题定位、memory_note_read按路径取原文：\n" + strings.Join(diaryPaths, "\n") + "\n\n")
	}
	corrections := loadFatfishCorrections(os.Getenv("FATFISH_OWNER_CHAT"))
	for i, item := range corrections {
		if i >= 3 {
			break
		}
		current := item.currentText()
		if current != "" {
			text.WriteString(fmt.Sprintf("最新纠正（%s，memory_id=%d，source_id=%d）：%s\n", item.Category, item.NewID, item.SourceID, current))
		}
	}
	return text.String()
}

type fatfishCorrection struct {
	Old      string   `json:"old"`
	New      string   `json:"new"`
	NewID    int      `json:"new_id"`
	SourceID int      `json:"source_id"`
	Category string   `json:"category"`
	Expires  *float64 `json:"expires"`
}

func (c fatfishCorrection) currentText() string {
	if c.Expires != nil && *c.Expires <= float64(time.Now().Unix()) {
		return ""
	}
	return c.New
}

func loadFatfishCorrections(chat string) []fatfishCorrection {
	core := os.Getenv("FATFISH_STRUCTURED_CORE_FILE")
	if core == "" || chat == "" {
		return nil
	}
	var envelope struct {
		Chat        string              `json:"chat"`
		Corrections []fatfishCorrection `json:"corrections"`
	}
	if json.Unmarshal([]byte(boundedMemory(filepath.Join(filepath.Dir(core), "corrections.json"), 256*1024)), &envelope) != nil || envelope.Chat != chat {
		return nil
	}
	return envelope.Corrections
}

func correctFatfishSummary(summary, chat string) string {
	const marker = "[记忆纠正覆盖：旧说法仅作历史记录]"
	base, _, _ := strings.Cut(summary, marker)
	base = strings.TrimSpace(base)
	var updates []string
	for _, item := range loadFatfishCorrections(chat) {
		if item.Old == "" || !strings.Contains(base, item.Old) {
			continue
		}
		current := item.currentText()
		if current == "" {
			current = "该临时记录已停用或过期"
		}
		updates = append(updates, fmt.Sprintf("历史记录：%s；当前%s记录：%s（memory_id=%d，source_id=%d）", item.Old, item.Category, current, item.NewID, item.SourceID))
		if len(updates) >= 4 {
			break
		}
	}
	if len(updates) == 0 {
		return base
	}
	return base + "\n" + marker + "\n" + strings.Join(updates, "\n")
}

func ownerFatfishSession(agent *AgentInstance, key string) bool {
	store, ok := agent.Sessions.(session.MetadataAwareSessionStore)
	if !ok {
		return false
	}
	scope := store.GetSessionScope(key)
	return scope != nil && scope.Channel == "telegram" && scope.Values["chat"] == "direct:"+os.Getenv("FATFISH_OWNER_CHAT") && os.Getenv("FATFISH_OWNER_CHAT") != ""
}
