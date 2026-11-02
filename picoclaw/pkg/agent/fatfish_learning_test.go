package agent

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestFatfishDiaryDetailsAreOnDemandAndCoreIsFresh(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "memory")
	os.MkdirAll(dir, 0o700)
	os.WriteFile(filepath.Join(dir, "MEMORY.md"), []byte("稳定资料A"), 0o600)
	now := time.Now().In(time.FixedZone("UTC+8", 8*3600))
	name := filepath.Join(now.Format("200601"), now.Format("20060102")+".md")
	os.MkdirAll(filepath.Dir(filepath.Join(dir, name)), 0o700)
	os.WriteFile(filepath.Join(dir, name), []byte("PRIVATE_DIARY_BODY"+strings.Repeat("当天细节", 1000)), 0o600)
	cb := NewContextBuilder(root)
	first := cb.fatfishMemoryContext()
	if strings.Contains(first, "PRIVATE_DIARY_BODY") || !strings.Contains(first, name) || !strings.Contains(first, "memory_note_read") {
		t.Fatal("diary not on demand")
	}
	os.WriteFile(filepath.Join(dir, "MEMORY.md"), []byte("更新资料B"), 0o600)
	second := cb.fatfishMemoryContext()
	if !strings.Contains(second, "更新资料B") || strings.Contains(second, "稳定资料A") {
		t.Fatal("core was frozen")
	}
}

func TestFatfishCorrectionProjectionKeepsCurrentValueAndChatScope(t *testing.T) {
	root := t.TempDir()
	core := filepath.Join(root, "core.md")
	t.Setenv("FATFISH_STRUCTURED_CORE_FILE", core)
	raw := []byte(`{"chat":"123","corrections":[{"old":"旧偏好A","new":"新偏好B","new_id":4,"source_id":9,"category":"core","expires":null}]}`)
	os.WriteFile(filepath.Join(root, "corrections.json"), raw, 0o600)
	if got := correctFatfishSummary("旧摘要：旧偏好A。", "123"); !strings.HasPrefix(got, "旧摘要：旧偏好A。") || !strings.Contains(got, "新偏好B") || !strings.Contains(got, "旧说法仅作历史记录") {
		t.Fatal(got)
	}
	if got := correctFatfishSummary("旧偏好A", "-99"); got != "旧偏好A" {
		t.Fatal("private correction crossed chat")
	}
	os.WriteFile(filepath.Join(root, "corrections.json"), []byte(`{"chat":"123","corrections":[{"old":"旧偏好A","new":"最新偏好C"}]}`), 0o600)
	if !strings.Contains(correctFatfishSummary("旧偏好A", "123"), "最新偏好C") {
		t.Fatal("new correction frozen")
	}
}

func TestFatfishExpiredCorrectionDoesNotRestoreOldValue(t *testing.T) {
	root := t.TempDir()
	t.Setenv("FATFISH_STRUCTURED_CORE_FILE", filepath.Join(root, "core.md"))
	raw, _ := json.Marshal(map[string]any{"chat": "123", "corrections": []map[string]any{{"old": "旧临时状态", "new": "已过期状态", "expires": time.Now().Unix() - 1}}})
	os.WriteFile(filepath.Join(root, "corrections.json"), raw, 0o600)
	got := correctFatfishSummary("之前记着旧临时状态", "123")
	if !strings.Contains(got, "旧临时状态") || strings.Contains(got, "已过期状态") || !strings.Contains(got, "已停用或过期") {
		t.Fatal(got)
	}
}

func TestFatfishNegatedCorrectionProjectionIsIdempotent(t *testing.T) {
	root := t.TempDir()
	t.Setenv("FATFISH_STRUCTURED_CORE_FILE", filepath.Join(root, "core.md"))
	os.WriteFile(filepath.Join(root, "corrections.json"), []byte(`{"chat":"123","corrections":[{"old":"喜欢猫","new":"不喜欢猫"}]}`), 0o600)
	once := correctFatfishSummary("喜欢猫", "123")
	twice := correctFatfishSummary(once, "123")
	if !strings.HasPrefix(once, "喜欢猫") || !strings.Contains(once, "不喜欢猫") || twice != once {
		t.Fatal(once, twice)
	}
}
