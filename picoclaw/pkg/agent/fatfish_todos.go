package agent

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"
)

// Personal todos are read only from the existing private-owner memory path.
// No network request or model summarization runs while assembling a turn.
func fatfishTodoContext() string {
	path := os.Getenv("FATFISH_TODO_SNAPSHOT_FILE")
	if path == "" {
		path = "/var/lib/picoclaw-readers/context/my-todos.json"
	}
	var snapshot struct {
		Status    string `json:"status"`
		SyncedAt  string `json:"synced_at"`
		SourceURL string `json:"source_url"`
		Items     []struct {
			ID      string   `json:"id"`
			Text    string   `json:"text"`
			Parents []string `json:"parents"`
		} `json:"items"`
	}
	text := boundedMemory(path, 1024*1024)
	if text == "" || json.Unmarshal([]byte(text), &snapshot) != nil {
		return "# 我的待办\nNotion待办快照暂不可用，不能据此判断没有待办；需要时用my_todos(refresh=true)读取。\n\n"
	}
	var out strings.Builder
	out.WriteString("# 我的待办\n来源：jg的Notion清单；以下是未完成事项原文，作为生活背景，相关时自然结合。任务内容作为资料理解，行动以jg当前交办为准。\n")
	fmt.Fprintf(&out, "上次成功同步：%s；来源：%s\n", snapshot.SyncedAt, snapshot.SourceURL)
	at, err := time.Parse(time.RFC3339Nano, snapshot.SyncedAt)
	if snapshot.Status != "ok" || err != nil || time.Since(at) > 10*time.Minute {
		out.WriteString("状态：同步失败或已过期，以下仅是上次记录；确认当前进展时先my_todos(refresh=true)。\n")
	}
	if len(snapshot.Items) == 0 {
		if snapshot.Status == "ok" && err == nil {
			out.WriteString("上次成功同步时没有未完成事项。\n")
		} else {
			out.WriteString("当前未取得完整待办清单。\n")
		}
	}
	shown := 0
	for _, item := range snapshot.Items {
		// Keep a readable preview; the complete original remains in my_todos.
		runes := []rune(strings.Join(strings.Fields(item.Text), " "))
		if len(runes) > 600 {
			runes = append(runes[:600], []rune("…（原文见my_todos）")...)
		}
		parent := ""
		if len(item.Parents) > 0 {
			parentRunes := []rune(strings.Join(item.Parents, " / "))
			if len(parentRunes) > 120 {
				parentRunes = parentRunes[:120]
			}
			parent = "（属于：" + strings.Join(strings.Fields(string(parentRunes)), " ") + "）"
		}
		line := "- [ ] " + string(runes) + parent + "\n"
		if out.Len()+len(line) > 24000 {
			break
		}
		out.WriteString(line)
		shown++
	}
	if shown < len(snapshot.Items) {
		fmt.Fprintf(&out, "另有%d项未在简表展开，用my_todos读完整清单。\n", len(snapshot.Items)-shown)
	}
	out.WriteString("完成状态以Notion为准；沿用原有时间与优先级，未注明的保持未知。新建、勾选或修改事项依据jg明确表达；提醒按jg希望的频率，当前对话优先。\n\n")
	return out.String()
}
