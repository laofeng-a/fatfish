package agent

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	runtimeevents "github.com/sipeed/picoclaw/pkg/events"
	"github.com/sipeed/picoclaw/pkg/fileutil"
	"github.com/sipeed/picoclaw/pkg/logger"
	"github.com/sipeed/picoclaw/pkg/providers"
)

const foldedToolPrefix = "[旧工具结果已折叠"
const summaryFallbackPrefix = "[摘要服务暂不可用；以下为有说话人标注的原话摘录]"

var oldCompressionNote = regexp.MustCompile(`\[Emergency compression dropped (\d+) oldest messages due to context limit\]`)
var combinedCompressionNote = regexp.MustCompile(`\[历史压缩记录：旧策略曾移出 (\d+) 条消息，可查聊天原文。\]`)
var toolLocator = regexp.MustCompile(`"(?:record_id|source_id|job_id|thread_id|status|state|error|url|path|revision|next_offset|next_cursor|message_ids)"\s*:\s*(?:"[^"\n]{0,180}"|[0-9]+|true|false|null|\[[0-9, ]{0,100}\])`)

// Projection only: the complete tool output remains in the session JSONL and
// reader records. User/assistant text and confirmed delivery text are untouched.
// Keep the newest unread tool batch in the active turn in full. A new user
// message makes preceding tool batches eligible, without touching dialogue.
func foldFatfishToolResults(messages []providers.Message) []providers.Message {
	boundary := len(messages)
	for i := len(messages) - 1; i >= 0; i-- {
		if messages[i].Role == "user" {
			boundary = i
			break
		}
	}
	for i := len(messages) - 1; i > boundary; i-- {
		if messages[i].Role == "assistant" && len(messages[i].ToolCalls) > 0 {
			boundary = i
			break
		}
	}
	result := append([]providers.Message(nil), messages...)
	for i := 0; i < boundary; i++ {
		msg := &result[i]
		if msg.Role != "tool" {
			continue
		}
		facts, diagnostic, hasFacts := strings.Cut(msg.Content, toolDiagnosticSeparator)
		if !hasFacts {
			diagnostic = msg.Content
			facts = ""
		}
		if strings.HasPrefix(diagnostic, foldedToolPrefix) || (utf8.RuneCountInString(diagnostic) <= 1000 && len(msg.Media) == 0) {
			continue
		}
		locators := toolLocator.FindAllString(diagnostic, 8)
		runes := []rune(diagnostic)
		preview := diagnostic
		if len(runes) > 480 {
			preview = string(runes[:360]) + " … " + string(runes[len(runes)-120:])
		}
		msg.Content = fmt.Sprintf("%s；call_id=%s；原文%d字符、%d个媒体引用；可用record_id查history_expand，或沿原读取入口取详情。]\n%s\n%s",
			foldedToolPrefix, msg.ToolCallID, len(runes), len(msg.Media), strings.Join(locators, "\n"), preview)
		msg.Media = nil
		if hasFacts {
			msg.Content = facts + toolDiagnosticSeparator + msg.Content
		}
	}
	return result
}

func normalizeFatfishSummary(summary string) string {
	total := 0
	for _, pattern := range []*regexp.Regexp{oldCompressionNote, combinedCompressionNote} {
		for _, match := range pattern.FindAllStringSubmatch(summary, -1) {
			n, _ := strconv.Atoi(match[1])
			total += n
		}
		summary = pattern.ReplaceAllString(summary, "")
	}
	summary = strings.TrimSpace(summary)
	if total > 0 {
		summary += fmt.Sprintf("\n[历史压缩记录：旧策略曾移出 %d 条消息，可查聊天原文。]", total)
	}
	return strings.TrimSpace(summary)
}

func foldedFatfishCompletedHistory(history []providers.Message) []providers.Message {
	projected := foldFatfishToolResults(append(append([]providers.Message(nil), history...), providers.Message{Role: "user"}))
	return projected[:len(history)]
}

func fatfishConversationSummaryInput(history []providers.Message) []providers.Message {
	var result []providers.Message
	for _, msg := range history {
		if fact, ok := deliveryFactsForSummary(msg); ok {
			result = append(result, fact)
			continue
		}
		if (msg.Role == "user" || msg.Role == "assistant") && strings.TrimSpace(msg.Content) != "" {
			result = append(result, providers.Message{Role: msg.Role, Content: msg.Content})
		}
	}
	return result
}

// If the provider is unavailable, retain attributed quotes instead of a count
// of deleted messages. This is explicitly an extract, never a made-up summary.
func fatfishQuoteFallback(existing string, messages []providers.Message) string {
	var lines []string
	if existing = normalizeFatfishSummary(existing); existing != "" {
		for strings.HasPrefix(existing, summaryFallbackPrefix) {
			existing = strings.TrimSpace(strings.TrimPrefix(existing, summaryFallbackPrefix))
		}
		lines = append(lines, existing)
	}
	for _, msg := range messages {
		text := []rune(strings.TrimSpace(msg.Content))
		if len(text) > 300 {
			text = append(text[:220], append([]rune(" … "), text[len(text)-70:]...)...)
		}
		lines = append(lines, msg.Role+": "+string(text))
	}
	text := []rune(strings.Join(lines, "\n"))
	if len(text) > 4500 {
		text = append(text[:2000], append([]rune("\n[…原话摘录节选，详情可查聊天记录…]\n"), text[len(text)-2400:]...)...)
	}
	return summaryFallbackPrefix + "\n" + string(text)
}

func (m *legacyContextManager) compactFatfish(ctx context.Context, sessionKey string, budget int) (compressionResult, bool) {
	agent := m.al.registry.GetDefaultAgent()
	if agent == nil {
		return compressionResult{}, false
	}
	history := agent.Sessions.GetHistory(sessionKey)
	turns := parseTurnBoundaries(history)
	if len(turns) < 2 {
		return compressionResult{}, false
	}
	if budget <= 0 {
		budget = agent.ContextWindow
	}
	existing := normalizeFatfishSummary(agent.Sessions.GetSummary(sessionKey))
	if ownerFatfishSession(agent, sessionKey) {
		existing = correctFatfishSummary(existing, os.Getenv("FATFISH_OWNER_CHAT"))
	}
	fixed := agent.MaxTokens + EstimateToolDefsTokens(agent.Tools.ToProviderDefs())
	if agent.ContextBuilder != nil {
		fixed += agent.ContextBuilder.EstimateSystemTokens(existing, nil)
		fixed += EstimateMessageTokens(providers.Message{Role: "system", Content: agent.ContextBuilder.fatfishMemoryContext()})
	}
	available := max(512, budget-fixed)
	// Reserve room for the replacement summary, retaining the largest recent
	// raw conversation tail that fits, rather than unconditionally half of it.
	target := max(256, available-min(2048, available/4))
	folded := foldedFatfishCompletedHistory(history)
	cut := turns[1]
	for _, start := range turns[1:] {
		cut = start
		if m.estimateTokens(folded[start:]) <= target {
			break
		}
	}
	conversation := fatfishConversationSummaryInput(history[:cut])
	summary := fatfishQuoteFallback(existing, conversation)
	if len(conversation) > 0 {
		var prompt strings.Builder
		prompt.WriteString(fatfishCompactionPrompt + "\n优先保留相处的连续性和原话：聊到哪、双方怎样回应、接过的梗、纠正与待续话题。工具操作只留实际结论与必要来源。用紧凑中文记录，保留事实与当时感受的区别。\n旧记录：\n" + existing + "\n对话：\n")
		for _, msg := range conversation {
			fmt.Fprintf(&prompt, "%s: %s\n", msg.Role, msg.Content)
		}
		callCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
		started := time.Now()
		m.al.activeRequestsInc()
		response, err := agent.Provider.Chat(callCtx, []providers.Message{{Role: "user", Content: prompt.String()}}, nil, agent.Model,
			map[string]any{"max_tokens": min(2048, agent.MaxTokens), "temperature": 0.2, "thinking_level": "off", "force_thinking_off": true})
		m.al.activeRequestsDec()
		cancel()
		if err == nil && response != nil && strings.TrimSpace(response.Content) != "" {
			summary = strings.TrimSpace(response.Content)
		} else {
			reason := "empty_response"
			if errors.Is(err, context.DeadlineExceeded) {
				reason = "timeout"
			} else if errors.Is(err, context.Canceled) {
				reason = "canceled"
			} else if err != nil {
				reason = "provider_error"
			}
			fields := map[string]any{"reason": reason, "elapsed_ms": time.Since(started).Milliseconds()}
			if err != nil {
				fields["error_type"] = fmt.Sprintf("%T", err)
			}
			if response != nil {
				fields["finish_reason"] = response.FinishReason
				fields["has_reasoning"] = strings.TrimSpace(response.ReasoningContent) != ""
			}
			logger.WarnCF("agent", "Conversation compaction used quote fallback", fields)
		}
	}
	// New messages may arrive while the summary is generated. Only retire the
	// exact prefix that was summarized, keeping every newly appended message.
	latest := agent.Sessions.GetHistory(sessionKey)
	if len(latest) < cut || !reflect.DeepEqual(latest[:cut], history[:cut]) {
		return compressionResult{}, false
	}
	archive, err := archiveFatfishPrefix(agent.Workspace, sessionKey, history[:cut])
	if err != nil {
		return compressionResult{}, false
	}
	summary += "\n[本次压缩原文备份：" + archive + "]"
	agent.Sessions.SetSummary(sessionKey, summary)
	agent.Sessions.TruncateHistory(sessionKey, len(latest)-cut)
	agent.Sessions.Save(sessionKey)
	m.al.emitEvent(runtimeevents.KindAgentSessionSummarize, m.al.newTurnEventScope(agent.ID, sessionKey, nil).meta(0, "compactFatfish", "turn.session.summarize"),
		SessionSummarizePayload{SummarizedMessages: len(conversation), KeptMessages: len(latest) - cut, SummaryLen: len(summary)})
	return compressionResult{DroppedMessages: cut, RemainingMessages: len(latest) - cut}, true
}

// JSONLBackend.Save physically compacts skipped lines. Save the retired raw
// prefix first, so semantic compression never becomes deletion of the source.
func archiveFatfishPrefix(workspace, sessionKey string, history []providers.Message) (string, error) {
	raw, err := json.Marshal(history)
	if err != nil {
		return "", err
	}
	dir := filepath.Join(workspace, "state", "context-archive", fmt.Sprintf("%x", sha256.Sum256([]byte(sessionKey))))
	if err = os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	path := filepath.Join(dir, fmt.Sprintf("%x.json", sha256.Sum256(raw)))
	return path, fileutil.WriteFileAtomic(path, raw, 0o600)
}
