package agent

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/sipeed/picoclaw/pkg/fileutil"
	"github.com/sipeed/picoclaw/pkg/providers"
)

// Explicit, bounded deployment audit. Captures at most three distinct real
// owner messages, never a background callback or a synthetic Telegram test.
func (m *fatfishHistoryManager) sample(ts *turnState, messages []providers.Message, defs []providers.ToolDefinition) {
	id := ts.opts.Dispatch.MessageID()
	if ts.channel != "telegram" || ts.chatID != os.Getenv("FATFISH_OWNER_CHAT") || id == "" || strings.Contains(id, ":") {
		return
	}
	m.mu.Lock()
	if len(m.sampled) >= m.sampleMax || m.sampled[id] {
		m.mu.Unlock()
		return
	}
	m.sampled[id] = true
	m.mu.Unlock()
	n, blocks := fatfishCountInput(messages, defs)
	st, err := m.store.State(ts.sessionKey)
	if err != nil {
		return
	}
	raw, err := json.MarshalIndent(map[string]any{"source": "real_owner_turn", "message_id": id, "session_key": ts.sessionKey, "observed_at": time.Now().Format(time.RFC3339), "input_estimate": n, "input_budget": ts.agent.ContextWindow - ts.agent.MaxTokens, "blocks": blocks, "summary_version": st.Version, "covered_through": st.Covered, "messages": messages, "tool_definitions": defs}, "", "  ")
	if err != nil {
		return
	}
	dir := filepath.Join(ts.workspace, "state", "context-samples")
	if os.MkdirAll(dir, 0o700) != nil {
		return
	}
	_ = fileutil.WriteFileAtomic(filepath.Join(dir, "turn-"+id+".json"), raw, 0o600)
}
