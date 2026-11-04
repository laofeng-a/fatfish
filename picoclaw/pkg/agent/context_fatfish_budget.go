package agent

import (
	"context"
	"fmt"
	"regexp"
	"time"

	"github.com/sipeed/picoclaw/pkg/contextledger"
	"github.com/sipeed/picoclaw/pkg/providers"
)

var fatfishClockBlock = regexp.MustCompile(`(?s)\n*\[fatfish_runtime_clock\].*?\[/fatfish_runtime_clock\]`)
var fatfishMinuteClock = regexp.MustCompile(`(?m)^## Current Time\n\d{4}-\d{2}-\d{2} \d{2}:\d{2} \([A-Za-z]+\)\n\n`)

// Mirror the gateway's clock relocation. Every admission/display/observation
// uses the same estimator, including the final clock and protocol overhead.
func fatfishCountInput(messages []providers.Message, defs []providers.ToolDefinition) (int, map[string]int) {
	blocks := map[string]int{"system": 0, "history": 0, "current": 0, "tools": EstimateToolDefsTokens(defs), "clock": 0}
	for _, msg := range messages {
		part := "current"
		if msg.Role == "system" {
			part = "system"
			msg.Content = fatfishClockBlock.ReplaceAllString(msg.Content, "")
			msg.Content = fatfishMinuteClock.ReplaceAllString(msg.Content, "")
			msg.SystemParts = nil
		} else if msg.HistoricalRecord != 0 {
			part = "history"
		}
		blocks[part] += EstimateMessageTokens(msg)
	}
	now := time.Now().In(time.FixedZone("UTC+8", 8*3600))
	weekday := []string{"日", "一", "二", "三", "四", "五", "六"}[int(now.Weekday())]
	clock := "[fatfish_runtime_clock]\n当前日期时间：" + now.Format("2006-01-02T15:04:05Z07:00") + "（星期" + weekday + "）\n时区：UTC+08:00，jg当地时间。每次模型调用前刷新，今天、明天、昨天按此理解。\n[/fatfish_runtime_clock]"
	blocks["clock"] = EstimateMessageTokens(providers.Message{Role: "system", Content: clock})
	total := 0
	for _, n := range blocks {
		total += n
	}
	return total, blocks
}

func fatfishContextPrompt(agent *AgentInstance, scope contextledger.Scope, history []providers.Message, summary string) []providers.Message {
	return agent.ContextBuilder.BuildMessagesFromPrompt(PromptBuildRequest{History: history, Summary: summary, Channel: scope.Channel, ChatID: scope.Chat})
}

func (m *fatfishHistoryManager) estimate(key string) (int, error) {
	agent := m.al.agentForSession(key)
	history, st, err := m.project(key)
	if err != nil {
		return 0, err
	}
	messages := fatfishContextPrompt(agent, fatfishScope(agent, key), history, st.Body)
	n, _ := fatfishCountInput(messages, agent.Tools.ToProviderDefs())
	return n, nil
}

func trimProjectedRecord(messages []providers.Message) ([]providers.Message, int64, bool) {
	var record, number int64
	for _, msg := range messages {
		if msg.HistoricalRecord != 0 {
			record = msg.HistoricalRecord
			number = msg.HistoricalTurn
			break
		}
	}
	if record == 0 {
		return messages, 0, false
	}
	result := make([]providers.Message, 0, len(messages))
	for _, msg := range messages {
		if msg.HistoricalRecord != record {
			result = append(result, msg)
		}
	}
	return result, number, true
}

func withUncoveredNotice(messages []providers.Message, numbers []int64) []providers.Message {
	if len(numbers) == 0 {
		return messages
	}
	copyMessages := append([]providers.Message(nil), messages...)
	note := fmt.Sprintf("\n[输入预算内临时未展开过往轮次：#%d 至 #%d。原文与工具/思考详情可按固定轮号查询；这些轮次尚未因此推进摘要覆盖范围。]", numbers[0], numbers[len(numbers)-1])
	if len(copyMessages) > 0 && copyMessages[0].Role == "system" {
		copyMessages[0].Content += note
		copyMessages[0].SystemParts = nil
	} else {
		copyMessages = append([]providers.Message{{Role: "system", Content: note}}, copyMessages...)
	}
	return copyMessages
}

func (m *fatfishHistoryManager) Admit(ctx context.Context, key string, messages []providers.Message, defs []providers.ToolDefinition, observe bool) ([]providers.Message, error) {
	agent := m.al.agentForSession(key)
	budget := agent.ContextWindow - agent.MaxTokens
	base := append([]providers.Message(nil), messages...)
	var omitted []int64
	for {
		view := withUncoveredNotice(base, omitted)
		n, blocks := fatfishCountInput(view, defs)
		if n <= budget {
			if observe {
				var numbers []int64
				seen := map[int64]bool{}
				for _, msg := range view {
					if msg.HistoricalTurn > 0 && !seen[msg.HistoricalTurn] {
						numbers = append(numbers, msg.HistoricalTurn)
						seen[msg.HistoricalTurn] = true
					}
				}
				if err := m.store.Observe(key, n, budget, blocks, numbers); err != nil {
					return nil, err
				}
			}
			return view, nil
		}
		var number int64
		var ok bool
		base, number, ok = trimProjectedRecord(base)
		if !ok {
			return nil, fmt.Errorf("protected current tool chain exceeds input budget: %d > %d tokens; no current message was truncated", n, budget)
		}
		if number > 0 {
			omitted = append(omitted, number)
		}
	}
}

func (m *fatfishHistoryManager) InputUsage(key string) (int, map[string]int, contextledger.State, error) {
	agent := m.al.agentForSession(key)
	if _, err := m.sync(agent, key, false); err != nil {
		return 0, nil, contextledger.State{}, err
	}
	history, st, err := m.project(key)
	if err != nil {
		return 0, nil, st, err
	}
	messages := fatfishContextPrompt(agent, fatfishScope(agent, key), history, st.Body)
	view, err := m.Admit(context.Background(), key, messages, agent.Tools.ToProviderDefs(), false)
	if err != nil {
		return 0, nil, st, err
	}
	n, b := fatfishCountInput(view, agent.Tools.ToProviderDefs())
	return n, b, st, nil
}
