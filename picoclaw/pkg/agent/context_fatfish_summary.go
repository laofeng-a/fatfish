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
		return contextledger.Material{}, fmt.Errorf("not_enough_complete_turns")
	}
	material := contextledger.Material{Previous: st, MaxNumber: last, Homes: map[string]string{}}
	if st.Version == 0 && st.Body != "" {
		headers, e := m.store.Headers(key)
		if e != nil {
			return material, e
		}
		material.Previous.Quotes = contextledger.LegacyQuoteEvidence(st.Body, headers, st.Covered)
	}
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
		raw, _ := contextledger.ModelMaterialJSON(material)
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

type fatfishSummaryAttempt struct {
	Candidate         contextledger.Candidate
	Raw, Body, Reason string
	ID                int64
	Accepted          bool
	State             contextledger.State
	// Material is the snapshot after any revision rebase during recording.
	Material contextledger.Material
}

func (m *fatfishHistoryManager) summaryAttempt(callCtx context.Context, key string, material contextledger.Material, messages []providers.Message) (fatfishSummaryAttempt, error) {
	agent := m.al.agentForSession(key)
	m.al.activeRequestsInc()
	response, callErr := agent.Provider.Chat(callCtx, messages, nil, agent.Model, map[string]any{"max_tokens": 4096, "temperature": 0.2, "thinking_level": "off", "force_thinking_off": true})
	m.al.activeRequestsDec()
	var c contextledger.Candidate
	var body, reason, generated string
	accepted := false
	var err error
	if callErr != nil {
		reason = "provider_error:" + fmt.Sprintf("%T", callErr)
	} else if callCtx.Err() != nil {
		reason = "summary_canceled_or_timeout"
	} else if response == nil || strings.TrimSpace(response.Content) == "" {
		reason = "empty_response"
	} else {
		generated = response.Content
		c, err = contextledger.ParseCandidate(generated)
		if response.FinishReason != "stop" && response.FinishReason != "end_turn" {
			reason = "generation_not_finished:" + response.FinishReason
		} else if err != nil {
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
		return fatfishSummaryAttempt{}, err
	}
	// Deliveries of the reply that triggered this summary may still be recorded
	// on recent turns; they do not invalidate the summarized past range.
	if _, err = m.store.Rebase(key, &material); err != nil {
		return fatfishSummaryAttempt{}, err
	}
	id, err := m.store.RecordCandidate(key, material, c, generated, body, reason, accepted, m.cooldown)
	if err != nil {
		return fatfishSummaryAttempt{}, err
	}
	if response != nil {
		original, _ := json.Marshal(response)
		if _, e := m.store.DB.Exec("UPDATE context_summaries SET response=? WHERE id=?", string(original), id); e != nil {
			return fatfishSummaryAttempt{}, e
		}
	}
	st, err := m.store.State(key)
	if err != nil {
		return fatfishSummaryAttempt{}, err
	}
	return fatfishSummaryAttempt{Candidate: c, Raw: generated, Body: body, Reason: reason, ID: id, Accepted: accepted && st.Version == id, State: st, Material: material}, nil
}

func (m *fatfishHistoryManager) generate(ctx context.Context, key string) error {
	material, err := m.material(key)
	if err != nil {
		return err
	}
	agent := m.al.agentForSession(key)
	raw, err := contextledger.ModelMaterialJSON(material)
	if err != nil {
		return err
	}
	messages := []providers.Message{{Role: "system", Content: fatfishSummaryPrompt}, {Role: "user", Content: string(raw)}}
	callCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	attempt, err := m.summaryAttempt(callCtx, key, material, messages)
	if err != nil {
		return err
	}
	// One source-grounded repair shares the original deadline and snapshot.
	material = attempt.Material
	instruction := contextledger.CandidateRepairInstruction(attempt.Candidate, material, attempt.Reason)
	skip := ""
	switch {
	case attempt.Accepted:
	case instruction == "":
		skip = "no_repair_for_reason"
	case callCtx.Err() != nil:
		skip = "deadline"
	case m.humanBusy(key):
		skip = "human_busy"
	case attempt.State.Version != material.Previous.Version || attempt.State.Covered != material.Previous.Covered:
		skip = "active_version_changed"
	case attempt.State.Revision != material.Previous.Revision:
		skip = "snapshot_changed"
	}
	if skip != "" {
		logger.InfoCF("agent", "Summary repair skipped", map[string]any{"session_key": key, "reason": attempt.Reason, "skip": skip})
	}
	if !attempt.Accepted && skip == "" {
		material.Previous.Agreements = attempt.State.Agreements
		raw, err = contextledger.ModelMaterialJSON(material)
		if err != nil {
			return err
		}
		repair := []providers.Message{{Role: "system", Content: fatfishSummaryPrompt}, {Role: "user", Content: string(raw)}, {Role: "assistant", Content: attempt.Raw}, {Role: "user", Content: instruction}}
		if tokens, _ := fatfishCountInput(repair, nil); tokens <= agent.ContextWindow-4096 {
			attempt, err = m.summaryAttempt(callCtx, key, material, repair)
			if err != nil {
				return err
			}
		}
	}
	if !attempt.Accepted {
		return fmt.Errorf("candidate_rejected:%s", attempt.Reason)
	}
	st, id, body := attempt.State, attempt.ID, attempt.Body
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
