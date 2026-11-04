package agent

import (
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/sipeed/picoclaw/pkg/contextledger"
	"github.com/sipeed/picoclaw/pkg/logger"
	"github.com/sipeed/picoclaw/pkg/providers"
)

//go:embed fatfish_summary_prompt.txt
var fatfishSummaryPrompt string

func (m *fatfishHistoryManager) schedule(key string) {
	st, err := m.store.State(key)
	if err != nil || float64(time.Now().Unix()) < st.Cooldown {
		return
	}
	used, err := m.estimate(key)
	if err != nil {
		return
	}
	agent := m.al.agentForSession(key)
	if used < (agent.ContextWindow-agent.MaxTokens)*70/100 {
		return
	}
	m.mu.Lock()
	if len(m.running) > 0 {
		m.mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(m.ctx)
	m.running[key] = cancel
	m.mu.Unlock()
	go func() {
		defer func() { cancel(); m.mu.Lock(); delete(m.running, key); m.mu.Unlock() }()
		defer func() {
			if r := recover(); r != nil {
				logger.WarnCF("agent", "Background summary panic", map[string]any{"type": fmt.Sprintf("%T", r)})
			}
		}()
		for m.humanBusy(key) {
			select {
			case <-ctx.Done():
				return
			case <-time.After(200 * time.Millisecond):
			}
		}
		if ctx.Err() != nil {
			return
		}
		if err := m.generate(ctx, key); err != nil {
			if err.Error() == "not_enough_complete_turns" || err.Error() == "past_batch_does_not_fit_summary_budget" {
				return
			}
			logger.WarnCF("agent", "Background summary kept previous version", map[string]any{"session_key": key, "reason": err.Error()})
		}
	}()
}

func (m *fatfishHistoryManager) material(key string) (contextledger.Material, error) {
	agent := m.al.agentForSession(key)
	scope, err := m.sync(agent, key, false)
	if err != nil {
		return contextledger.Material{}, err
	}
	_ = scope
	st, err := m.store.State(key)
	if err != nil {
		return contextledger.Material{}, err
	}
	turns, err := m.store.Uncovered(key, st.Covered)
	if err != nil {
		return contextledger.Material{}, err
	}
	var completed []contextledger.Turn
	var last int64
	for _, t := range turns {
		if t.Number > last {
			last = t.Number
		}
		if t.Number > st.Covered && t.Complete && (t.Kind == "human" || t.Kind == "member") {
			completed = append(completed, t)
		}
	}
	keep := m.keepRecent
	if len(completed) <= keep {
		keep = 1
	}
	if len(completed) <= keep {
		return contextledger.Material{}, fmt.Errorf("not_enough_complete_turns")
	}
	material := contextledger.Material{Previous: st, MaxNumber: last, Homes: map[string]string{}}
	_ = m.store.DB.QueryRow("SELECT reason FROM context_summaries WHERE session=? AND status='rejected' ORDER BY id DESC LIMIT 1", key).Scan(&material.Rejection)
	root := filepath.Dir(os.Getenv("FATFISH_STRUCTURED_CORE_FILE"))
	for _, name := range []string{"SOUL.md", "USER.md", "AGENT.md", "HEARTBEAT.md", "core.md", "MEMORY.md"} {
		path := filepath.Join(root, "profile", name)
		if name == "core.md" {
			path = filepath.Join(root, name)
		}
		if name == "MEMORY.md" {
			path = filepath.Join(root, "notes", name)
		}
		if raw, e := os.ReadFile(path); e == nil {
			material.Homes[name] = string(raw)
		}
	}
	material.Recent = append([]contextledger.Turn(nil), completed[len(completed)-keep:]...)
	used, err := m.estimate(key)
	if err != nil {
		return material, err
	}
	target := (agent.ContextWindow - agent.MaxTokens) * 55 / 100
	saved := 0
	for _, t := range completed[:len(completed)-keep] {
		material.Past = append(material.Past, t)
		material.From = material.Past[0].Number
		material.To = t.Number
		raw, _ := json.Marshal(material)
		summaryInput := []providers.Message{{Role: "system", Content: fatfishSummaryPrompt}, {Role: "user", Content: string(raw)}}
		n, _ := fatfishCountInput(summaryInput, nil)
		if n > agent.ContextWindow-4096 {
			material.Past = material.Past[:len(material.Past)-1]
			if len(material.Past) > 0 {
				material.To = material.Past[len(material.Past)-1].Number
			}
			break
		}
		saved += EstimateMessageTokens(providers.Message{Role: "user", Content: contextledger.Render(t)})
		if used-saved+2800 <= target {
			break
		}
	}
	if len(material.Past) == 0 {
		return material, fmt.Errorf("past_batch_does_not_fit_summary_budget")
	}
	return material, nil
}

func (m *fatfishHistoryManager) generate(ctx context.Context, key string) error {
	material, err := m.material(key)
	if err != nil {
		return err
	}
	agent := m.al.agentForSession(key)
	raw, err := json.Marshal(material)
	if err != nil {
		return err
	}
	callCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	m.al.activeRequestsInc()
	response, callErr := agent.Provider.Chat(callCtx, []providers.Message{{Role: "system", Content: fatfishSummaryPrompt}, {Role: "user", Content: string(raw)}}, nil, agent.Model, map[string]any{"max_tokens": 4096, "temperature": 0.2, "thinking_level": "off", "force_thinking_off": true})
	m.al.activeRequestsDec()
	var c contextledger.Candidate
	var body, reason, generated string
	accepted := false
	if callErr != nil {
		reason = "provider_error:" + fmt.Sprintf("%T", callErr)
	} else if callCtx.Err() != nil {
		reason = "summary_canceled_or_timeout"
	} else if response == nil || strings.TrimSpace(response.Content) == "" {
		reason = "empty_response"
	} else {
		generated = response.Content
		c, err = contextledger.ParseCandidate(generated)
		if err != nil {
			reason = "invalid_candidate_json"
		} else {
			body, err = contextledger.Validate(c, material, response.FinishReason)
			if err != nil {
				reason = err.Error()
			} else {
				accepted = true
			}
		}
	}
	if _, err = m.sync(agent, key, false); err != nil {
		return err
	}
	id, err := m.store.RecordCandidate(key, material, c, generated, body, reason, accepted, m.cooldown)
	if err != nil {
		return err
	}
	if response != nil {
		original, _ := json.Marshal(response)
		if _, e := m.store.DB.Exec("UPDATE context_summaries SET response=? WHERE id=?", string(original), id); e != nil {
			return e
		}
	}
	st, err := m.store.State(key)
	if err != nil {
		return err
	}
	if !accepted || st.Version != id {
		return fmt.Errorf("candidate_rejected:%s", reason)
	}
	// The accepted version and coverage are durable first. Source retirement is
	// an optional, identity-checked space reclamation; assembly already uses the
	// active SQLite version and therefore safely recovers a crash here.
	agent.Sessions.SetSummary(key, body)
	if err = m.retire(agent, key, st.Covered); err != nil {
		logger.WarnCF("agent", "Summary activated; original source retained", map[string]any{"reason": err.Error()})
	}
	if err = agent.Sessions.Save(key); err != nil {
		return err
	}
	logger.InfoCF("agent", "Background conversation summary activated", map[string]any{"session_key": key, "version": id, "covered_from": material.From, "covered_through": st.Covered})
	return nil
}
