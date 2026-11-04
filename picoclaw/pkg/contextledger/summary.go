package contextledger

import (
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"time"
	"unicode/utf8"
)

type Quote struct {
	Turn    int64  `json:"turn"`
	Speaker string `json:"speaker"`
	Text    string `json:"text"`
}
type Agreement struct {
	ID        string  `json:"id"`
	Text      string  `json:"text"`
	State     string  `json:"state"`
	Sources   []Quote `json:"sources"`
	ChangedBy []Quote `json:"changed_by,omitempty"`
	Home      string  `json:"home,omitempty"`
	HomeQuote string  `json:"home_quote,omitempty"`
}
type Candidate struct {
	Summary    string      `json:"summary"`
	From       int64       `json:"covered_from"`
	To         int64       `json:"covered_to"`
	Quotes     []Quote     `json:"quotes"`
	Agreements []Agreement `json:"agreements"`
}
type Material struct {
	Previous  State             `json:"previous_accepted"`
	Past      []Turn            `json:"past_dialogue"`
	Recent    []Turn            `json:"recent_corrections_only"`
	From      int64             `json:"allowed_from"`
	To        int64             `json:"allowed_to"`
	MaxNumber int64             `json:"snapshot_last_number"`
	Homes     map[string]string `json:"-"`
	Rejection string            `json:"previous_rejection_reason,omitempty"`
}

var proseQuotes = regexp.MustCompile(`[“「"]([^”」"\n]{2,})[”」"]`)
var quoteReference = regexp.MustCompile(`\{\{q([0-9]+)\}\}`)

// Quotes are checked against real sources before rendering. A reference inserts
// the recorded speaker and exact text together, keeping editorial paraphrases
// outside quotation marks.
func renderQuoteReferences(prose string, quotes []Quote) (string, error) {
	var invalid bool
	text := quoteReference.ReplaceAllStringFunc(prose, func(ref string) string {
		var number int
		if _, err := fmt.Sscanf(ref, "{{q%d}}", &number); err != nil || number < 1 || number > len(quotes) {
			invalid = true
			return ref
		}
		q := quotes[number-1]
		return q.Speaker + "：“" + q.Text + "”"
	})
	if invalid || strings.Contains(text, "{{q") {
		return "", fmt.Errorf("invalid_quote_reference")
	}
	return text, nil
}

func proseQuoteHasSource(text string, m Material) bool {
	if m.Previous.Version > 0 && strings.Contains(m.Previous.Body, text) {
		return true
	}
	for _, source := range m.Previous.Quotes {
		if strings.Contains(source.Text, text) {
			return true
		}
	}
	for _, t := range m.Past {
		if strings.Contains(t.User, text) {
			return true
		}
		for _, d := range t.Deliveries {
			if d.State == "sent" && strings.Contains(d.Text, text) {
				return true
			}
		}
	}
	return false
}

// This is a repair request, never a replacement summary or fallback extract.
// The revised response must pass the same validation before it can activate.
func QuoteRepairInstruction(c Candidate, m Material) string {
	prose, _ := renderQuoteReferences(c.Summary, c.Quotes)
	var missing []string
	for _, quote := range proseQuotes.FindAllStringSubmatch(prose, -1) {
		if !proseQuoteHasSource(quote[1], m) {
			missing = append(missing, quote[1])
		}
	}
	data, _ := json.Marshal(map[string]any{"reason": "unmatched_prose_quote", "unmatched_quoted_fragments": missing, "allowed_from": m.From, "allowed_to": m.To})
	return "上一候选已拒绝，未替换旧记录。请根据原始材料核对并修正以下问题，返回完整候选JSON：有原文依据的引文放入quotes并用{{q1}}等引用；概括用叙述表达，缺少依据的内容删除。保留已有约定id和有效来源，覆盖范围保持不变。\n" + string(data)
}

func ParseCandidate(raw string) (Candidate, error) {
	text := strings.TrimSpace(raw)
	if strings.HasPrefix(text, "```json\n") && strings.HasSuffix(text, "```") {
		text = strings.TrimSuffix(strings.TrimPrefix(text, "```json\n"), "```")
	}
	var c Candidate
	err := json.Unmarshal([]byte(text), &c)
	return c, err
}

func validQuote(q Quote, m Material) bool {
	if strings.TrimSpace(q.Text) == "" {
		return false
	}
	for _, t := range append(append([]Turn(nil), m.Past...), m.Recent...) {
		if t.Number != q.Turn {
			continue
		}
		if q.Speaker == "jg" && t.Kind == "human" && strings.Contains(t.User, q.Text) {
			return true
		}
		if q.Speaker == "用户" && (t.Kind == "human" || t.Kind == "member") && strings.Contains(t.User, q.Text) {
			return true
		}
		if q.Speaker == "大肥鱼" {
			for _, d := range t.Deliveries {
				if d.State == "sent" && strings.Contains(d.Text, q.Text) {
					return true
				}
			}
		}
	}
	for _, a := range m.Previous.Agreements {
		for _, source := range append(append([]Quote(nil), a.Sources...), a.ChangedBy...) {
			if q == source {
				return true
			}
		}
	}
	for _, source := range m.Previous.Quotes {
		if q == source {
			return true
		}
	}
	return false
}

func renderAgreement(a Agreement) string {
	if a.Home != "" {
		return ""
	}
	var sources []string
	for _, q := range a.Sources {
		sources = append(sources, fmt.Sprintf("#%d", q.Turn))
	}
	text := fmt.Sprintf("当时（%s）两人确认：%s", strings.Join(sources, "、"), a.Text)
	switch a.State {
	case "completed":
		text += "；后来的对话已确认完成。"
	case "revoked":
		text += "；后来已明确撤销。"
	case "superseded":
		text += "；后来已由新约定替代。"
	default:
		text += "。"
	}
	return text
}

// Validate proves structural/source constraints, not arbitrary semantic truth.
// Agreements are rendered from the checked fields, so omission from prose does
// not silently remove an existing agreement.
func Validate(c Candidate, m Material, finish string) (string, error) {
	if finish != "stop" && finish != "end_turn" {
		return "", fmt.Errorf("generation_not_finished:%s", finish)
	}
	if strings.TrimSpace(c.Summary) == "" {
		return "", fmt.Errorf("empty_summary")
	}
	if c.From != m.From || c.To != m.To || c.From <= m.Previous.Covered || c.To < c.From {
		return "", fmt.Errorf("coverage_out_of_bounds")
	}
	allowed := map[int64]bool{}
	for _, t := range m.Past {
		if !t.Complete || t.Number <= 0 || t.Number < c.From || t.Number > c.To {
			return "", fmt.Errorf("invalid_past_turn")
		}
		allowed[t.Number] = true
	}
	if !allowed[c.From] || !allowed[c.To] {
		return "", fmt.Errorf("missing_coverage_source")
	}
	for _, q := range c.Quotes {
		if !validQuote(q, m) {
			return "", fmt.Errorf("unmatched_quote:#%d", q.Turn)
		}
		if q.Turn > c.To {
			return "", fmt.Errorf("recent_quote_in_past_body:#%d", q.Turn)
		}
	}
	previous := map[string]Agreement{}
	for _, a := range m.Previous.Agreements {
		previous[a.ID] = a
	}
	seen := map[string]bool{}
	prose, err := renderQuoteReferences(strings.TrimSpace(c.Summary), c.Quotes)
	if err != nil {
		return "", err
	}
	body := prose
	for _, a := range c.Agreements {
		if a.ID == "" || seen[a.ID] || strings.TrimSpace(a.Text) == "" || len(a.Sources) == 0 {
			return "", fmt.Errorf("invalid_agreement")
		}
		seen[a.ID] = true
		if a.State != "active" && a.State != "completed" && a.State != "revoked" && a.State != "superseded" {
			return "", fmt.Errorf("invalid_agreement_state")
		}
		for _, q := range append(append([]Quote(nil), a.Sources...), a.ChangedBy...) {
			if !validQuote(q, m) {
				return "", fmt.Errorf("unmatched_agreement_source:%s", a.ID)
			}
		}
		if old, ok := previous[a.ID]; ok && (old.Text != a.Text || old.State != a.State || old.Home != a.Home) {
			changed := false
			boundary := m.Previous.Covered
			for _, q := range append(append([]Quote(nil), old.Sources...), old.ChangedBy...) {
				if q.Turn > boundary {
					boundary = q.Turn
				}
			}
			for _, q := range a.ChangedBy {
				for _, t := range append(append([]Turn(nil), m.Past...), m.Recent...) {
					if t.Number == q.Turn && t.Number > boundary {
						changed = true
					}
				}
			}
			if !changed {
				return "", fmt.Errorf("agreement_changed_without_new_source:%s", a.ID)
			}
		}
		if _, exists := previous[a.ID]; !exists && a.State != "active" && len(a.ChangedBy) == 0 {
			return "", fmt.Errorf("agreement_status_requires_change_source:%s", a.ID)
		}
		if a.Home != "" {
			home, ok := m.Homes[a.Home]
			if !ok || strings.TrimSpace(a.HomeQuote) == "" || !strings.Contains(home, a.HomeQuote) {
				return "", fmt.Errorf("unverified_agreement_home:%s", a.ID)
			}
		}
		if rendered := renderAgreement(a); rendered != "" {
			body += "\n" + rendered
		}
	}
	for id := range previous {
		if !seen[id] {
			return "", fmt.Errorf("agreement_omitted:%s", id)
		}
	}
	if utf8.RuneCountInString(body) > 1800 {
		return "", fmt.Errorf("summary_too_long")
	}
	// Explicitly attributed quoted words in prose must also have a source.
	quotes := proseQuotes.FindAllStringSubmatch(prose, -1)
	for _, q := range quotes {
		if !proseQuoteHasSource(q[1], m) {
			return "", fmt.Errorf("unmatched_prose_quote")
		}
	}
	return body, nil
}

func (s *Store) SeedLegacy(scope Scope, body string) error {
	_, err := s.DB.Exec("UPDATE context_sessions SET legacy_summary=? WHERE session=? AND active_version=0 AND legacy_summary=''", body, scope.Session)
	return err
}

func (s *Store) RecordCandidate(session string, m Material, c Candidate, raw, body, reason string, accepted bool, cooldown time.Duration) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, err := s.DB.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	var version, covered, revision, maxNumber int64
	if err = tx.QueryRow("SELECT active_version,covered,revision FROM context_sessions WHERE session=?", session).Scan(&version, &covered, &revision); err != nil {
		return 0, err
	}
	if err = tx.QueryRow("SELECT COALESCE(MAX(number),0) FROM context_turns WHERE session=?", session).Scan(&maxNumber); err != nil {
		return 0, err
	}
	if accepted && (version != m.Previous.Version || covered != m.Previous.Covered || revision != m.Previous.Revision || maxNumber != m.MaxNumber) {
		accepted = false
		reason = "input_changed"
	}
	status := "rejected"
	if accepted {
		status = "accepted"
	}
	agreements, _ := json.Marshal(c.Agreements)
	res, err := tx.Exec("INSERT INTO context_summaries(session,parent,cover_from,cover_to,status,reason,raw,body,agreements,created) VALUES(?,?,?,?,?,?,?,?,?,?)", session, m.Previous.Version, m.From, m.To, status, reason, raw, body, string(agreements), float64(time.Now().UnixNano())/1e9)
	if err != nil {
		return 0, err
	}
	id, err := res.LastInsertId()
	if err != nil {
		return 0, err
	}
	// Independently keep newly grounded agreements even if only prose/length
	// validation failed. A changed input snapshot is never used for this update.
	if revision == m.Previous.Revision && version == m.Previous.Version {
		old := map[string]Agreement{}
		for _, a := range m.Previous.Agreements {
			old[a.ID] = a
		}
		for _, a := range c.Agreements {
			valid := a.ID != "" && a.Text != "" && (a.State == "active" || a.State == "completed" || a.State == "revoked" || a.State == "superseded") && len(a.Sources) > 0
			for _, q := range a.Sources {
				if !validQuote(q, m) {
					valid = false
				}
			}
			prior, exists := old[a.ID]
			if exists && (prior.Text != a.Text || prior.State != a.State || prior.Home != a.Home) {
				changed := false
				boundary := m.Previous.Covered
				for _, q := range append(append([]Quote(nil), prior.Sources...), prior.ChangedBy...) {
					if q.Turn > boundary {
						boundary = q.Turn
					}
				}
				for _, q := range a.ChangedBy {
					if q.Turn > boundary && validQuote(q, m) {
						changed = true
					}
				}
				valid = changed && len(a.Sources) > 0
				for _, q := range append(append([]Quote(nil), a.Sources...), a.ChangedBy...) {
					if !validQuote(q, m) {
						valid = false
					}
				}
			}
			if !exists && a.State != "active" {
				changed := false
				for _, q := range a.ChangedBy {
					if q.Turn > m.Previous.Covered && validQuote(q, m) {
						changed = true
					}
				}
				valid = valid && changed
			}
			if a.Home != "" {
				home, ok := m.Homes[a.Home]
				if !ok || strings.TrimSpace(a.HomeQuote) == "" || !strings.Contains(home, a.HomeQuote) {
					valid = false
				}
			}
			if accepted || valid {
				data, _ := json.Marshal(a)
				if _, err = tx.Exec("INSERT INTO context_agreements(session,id,data) VALUES(?,?,?) ON CONFLICT(session,id) DO UPDATE SET data=excluded.data", session, a.ID, string(data)); err != nil {
					return 0, err
				}
			}
		}
	}
	if accepted {
		_, err = tx.Exec("UPDATE context_sessions SET active_version=?,covered=?,cooldown=0,revision=revision+1 WHERE session=?", id, m.To, session)
	} else {
		_, err = tx.Exec("UPDATE context_sessions SET cooldown=? WHERE session=?", float64(time.Now().Add(cooldown).Unix()), session)
	}
	if err != nil {
		return 0, err
	}
	if err = tx.Commit(); err != nil {
		return 0, err
	}
	if !accepted && reason == "input_changed" {
		return id, fmt.Errorf("input_changed")
	}
	return id, nil
}

func (s *Store) Observe(session string, input, budget int, blocks map[string]int, numbers []int64) error {
	st, err := s.State(session)
	if err != nil {
		return err
	}
	sort.Slice(numbers, func(i, j int) bool { return numbers[i] < numbers[j] })
	raw, _ := json.Marshal(blocks)
	n, _ := json.Marshal(numbers)
	_, err = s.DB.Exec("INSERT INTO context_observations(session,at,input_tokens,input_budget,summary_version,covered,history_numbers,blocks) VALUES(?,?,?,?,?,?,?,?)", session, float64(time.Now().UnixNano())/1e9, input, budget, st.Version, st.Covered, string(n), string(raw))
	return err
}

func (s *Store) Reset(session string) error {
	// Clearing a conversation starts a fresh visible coverage epoch. Original
	// numbered records and previous summary versions remain queryable.
	var last int64
	if err := s.DB.QueryRow("SELECT COALESCE(MAX(number),0) FROM context_turns WHERE session=?", session).Scan(&last); err != nil {
		return err
	}
	_, err := s.DB.Exec("UPDATE context_sessions SET covered=?,active_version=0,legacy_summary='',cooldown=0,revision=revision+1 WHERE session=?", last, session)
	return err
}
