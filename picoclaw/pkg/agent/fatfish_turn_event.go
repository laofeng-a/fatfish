package agent

import (
	"encoding/json"
	"os"
	"strconv"
	"strings"

	"github.com/sipeed/picoclaw/pkg/providers"
)

const fatfishTurnEventHeader = "[fatfish_turn_event]"

// Use dispatch metadata, never the wording of a message, to identify its source.
// This view is rebuilt for every provider call and is not persisted as dialogue.
func withFatfishTurnEvent(ts *turnState, messages []providers.Message, wakeKind string) []providers.Message {
	id, sender := ts.opts.Dispatch.MessageID(), ts.opts.Dispatch.SenderID()
	kind, source := "internal_event", "程序内部事件"
	switch {
	case strings.HasPrefix(sender, "async:"):
		kind, source = "tool_callback", "异步工具结果回调"
	case strings.HasPrefix(id, "codex-event:"):
		kind, source = "executor_receipt", "编码执行端回执"
	case strings.HasPrefix(id, "heartbeat:") || sender == "heartbeat":
		kind, source = "scheduled_wakeup", "后台定时唤醒"
		switch wakeKind {
		case "chat":
			kind, source = "companion_tick", "两分钟自主陪聊机会"
		case "autonomy":
			kind, source = "autonomy", "自主活动唤醒"
		case "task":
			kind, source = "task_patrol", "任务巡检"
		case "quota":
			kind, source = "quota_reset", "额度重置到点检查"
		case "diary":
			kind, source = "diary_maintenance", "日记维护唤醒"
		case "silent", "alarm":
			kind, source = "silent_alarm", "静默闹钟到点"
		}
	case sender == "cron":
		kind, source = "cron", "定时任务到点"
	default:
		_, parseErr := strconv.ParseUint(id, 10, 64)
		if parseErr == nil || (sender != "" && sender != "system" && !strings.Contains(sender, ":")) {
			kind, source = "human_message", "Telegram用户的新消息"
			if sender != "" && sender == os.Getenv("FATFISH_OWNER_CHAT") {
				source = "jg的Telegram真人消息"
			}
		}
	}
	out := make([]providers.Message, 0, len(messages)+1)
	for _, message := range messages {
		if message.Role != "system" || !strings.HasPrefix(message.Content, fatfishTurnEventHeader+"\n") {
			out = append(out, message)
		}
	}
	stage := "current_input"
	if len(out) > 0 && out[len(out)-1].Role == "tool" {
		stage = "tool_result_continuation"
	}
	event, _ := json.Marshal(map[string]string{"kind": kind, "source": source, "message_id": id, "stage": stage})
	text := fatfishTurnEventHeader + "\n" + string(event) + "\n"
	if kind == "human_message" {
		text += "当前回应对象是这条真人消息的发送者，接续他的本轮原话；先前的等待安排随眼前对话判断。\n"
	} else {
		text += "按这次事件及其对应上下文推进；是否联系jg与本轮内部结论分别处理。\n"
	}
	text += "[/fatfish_turn_event]"
	return append(out, providers.Message{Role: "system", Content: text})
}
