package contextledger

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/sipeed/picoclaw/pkg/providers"
)

func (s *Store) Origins(session string) (map[string]Origin, error) {
	rows, err := s.DB.Query("SELECT i.history_id,t.source_key,t.kind,t.message_id,t.user_text,t.at FROM context_items i JOIN context_turns t ON t.id=i.turn_id WHERE i.session=?", session)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]Origin{}
	for rows.Next() {
		var id string
		var o Origin
		var at float64
		if err = rows.Scan(&id, &o.Key, &o.Kind, &o.MessageID, &o.Text, &at); err != nil {
			return nil, err
		}
		if at > 0 {
			o.At = time.Unix(0, int64(at*1e9))
		}
		result[id] = o
	}
	return result, rows.Err()
}

func OpenRuntime(path string) (*sql.DB, error) {
	if path == "" {
		return nil, nil
	}
	if _, err := os.Stat(path); err != nil {
		return nil, nil
	}
	db, err := sql.Open("sqlite", "file:"+filepath.ToSlash(path)+"?mode=ro&_pragma=busy_timeout(1500)")
	if err == nil {
		db.SetMaxOpenConns(1)
	}
	return db, err
}

// Exact inbound event IDs take priority over augmented prompt text. Legacy
// records are matched only against actual received text and recorded time.
func InternalSourceText(text string) bool {
	text = strings.TrimSpace(text)
	for _, prefix := range []string{"Codex回合状态：", "DSH回合状态：", "Claude Code回合状态：", "先前的Codex查询已返回", "先前的DSH查询已返回", "先前的Claude Code查询已返回", "[Codex查询结果]", "[Codex协作回执]", "[DSH查询结果]", "[DSH协作回执]", "[Claude Code查询结果]", "[Claude Code协作回执]"} {
		if strings.HasPrefix(text, prefix) {
			return true
		}
	}
	return false
}

// An early index may contain old callback wrappers as legacy user fields.
// Repair only positively identified internal sources; human numbers stay fixed.
func (s *Store) repairInternalSources() error {
	rows, err := s.DB.Query("SELECT id,user_text FROM context_turns WHERE kind='legacy'")
	if err != nil {
		return err
	}
	var ids []int64
	for rows.Next() {
		var id int64
		var text string
		if err = rows.Scan(&id, &text); err != nil {
			rows.Close()
			return err
		}
		if InternalSourceText(text) {
			ids = append(ids, id)
		}
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, id := range ids {
		if _, err = s.DB.Exec("UPDATE context_turns SET kind='internal',number=0 WHERE id=?", id); err != nil {
			return err
		}
	}
	s.Reclassified = len(ids) > 0
	if s.Reclassified {
		_, err = s.DB.Exec("UPDATE context_sessions SET revision=revision+1")
	}
	return err
}

func ResolveOrigin(db *sql.DB, scope Scope, msg providers.Message) Origin {
	o := Origin{Key: "legacy:" + msg.HistoryID, Kind: "legacy", Text: msg.Content}
	if msg.CreatedAt != nil {
		o.At = *msg.CreatedAt
	}
	if msg.SourceKind == "internal" || msg.SourceKind == "continuation" || strings.HasPrefix(msg.SourceMessageID, "codex-event:") || strings.HasPrefix(msg.SourceMessageID, "heartbeat:") {
		o.Key = "internal:" + msg.HistoryID
		o.Kind = "internal"
		return o
	}
	if InternalSourceText(msg.Content) {
		o.Key = "internal:" + msg.HistoryID
		o.Kind = "internal"
		return o
	}
	if db == nil {
		return o
	}
	var content, kind, id string
	var at float64
	if msg.SourceMessageID != "" {
		err := db.QueryRow("SELECT content,kind,msg,created FROM events WHERE chat=? AND msg=? AND kind IN ('user','member') ORDER BY id LIMIT 1", scope.Chat, msg.SourceMessageID).Scan(&content, &kind, &id, &at)
		if err == nil {
			o.Key = "inbound:" + scope.Chat + ":" + id
			o.Kind = "human"
			if kind == "member" {
				o.Kind = "member"
			}
			o.MessageID = id
			o.Text = content
			o.At = time.Unix(0, int64(at*1e9))
			return o
		}
	}
	if o.At.IsZero() {
		return o
	}
	rows, err := db.Query("SELECT content,kind,msg,created FROM events WHERE chat=? AND kind IN ('user','member') AND created BETWEEN ? AND ? ORDER BY abs(created-?) LIMIT 30", scope.Chat, float64(o.At.Unix())-600, float64(o.At.Unix())+5, float64(o.At.Unix()))
	if err != nil {
		return o
	}
	defer rows.Close()
	for rows.Next() {
		if rows.Scan(&content, &kind, &id, &at) != nil {
			continue
		}
		if strings.TrimSpace(content) == "" || !strings.Contains(msg.Content, content) {
			continue
		}
		o.Key = "inbound:" + scope.Chat + ":" + id
		o.Kind = "human"
		if kind == "member" {
			o.Kind = "member"
		}
		o.MessageID = id
		o.Text = content
		o.At = time.Unix(0, int64(at*1e9))
		break
	}
	return o
}

func (s *Store) RefreshDeliveries(scope Scope, runtime *sql.DB) error {
	return s.refreshDeliveries(scope, runtime, false)
}

func (s *Store) RefreshAllDeliveries(scope Scope, runtime *sql.DB) error {
	return s.refreshDeliveries(scope, runtime, true)
}

func (s *Store) refreshDeliveries(scope Scope, runtime *sql.DB, all bool) error {
	if runtime == nil {
		return nil
	}
	state, err := s.State(scope.Session)
	if err != nil {
		return err
	}
	covered := &state.Covered
	if all {
		covered = nil
	}
	turns, err := s.loadTurns(scope.Session, covered, false)
	if err != nil {
		return err
	}
	if len(turns) == 0 {
		return nil
	}
	bySource := map[string]int{}
	for i, t := range turns {
		if t.Number > 0 && t.MessageID != "" {
			bySource[t.MessageID] = i
		}
	}
	var earliest float64
	for _, t := range turns {
		if t.At > 0 && (earliest == 0 || t.At < earliest) {
			earliest = t.At
		}
	}
	rows, err := runtime.Query("SELECT s.ticket,s.status,s.content,s.kind,s.created,COALESCE(s.message_ids,'[]'),COALESCE(a.msg,'') FROM sends s LEFT JOIN send_turn_attempts a ON a.ticket=s.ticket WHERE s.chat=? AND s.created>=? ORDER BY s.created,s.ticket", scope.Chat, earliest)
	if err != nil {
		return err
	}
	type sent struct {
		ticket, status, text, kind, ids, source string
		at                                      float64
	}
	var sends []sent
	for rows.Next() {
		var d sent
		if err = rows.Scan(&d.ticket, &d.status, &d.text, &d.kind, &d.at, &d.ids, &d.source); err != nil {
			rows.Close()
			return err
		}
		sends = append(sends, d)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	deliveries := make([][]Delivery, len(turns))
	seen := map[string]bool{}
	for _, d := range sends {
		index, ok := bySource[d.source]
		if !ok {
			index = -1
			for i, t := range turns {
				if t.Number > 0 && t.At > 0 && t.At <= d.at {
					index = i
				}
			}
			if index < 0 {
				continue
			}
		}
		if seen[d.ticket] {
			continue
		}
		seen[d.ticket] = true
		actions, e := runtime.Query("SELECT position,method,status,content,COALESCE(message_id,'') FROM send_actions WHERE ticket=? ORDER BY position", d.ticket)
		if e != nil {
			return e
		}
		count := 0
		for actions.Next() {
			var position int
			var method, status, content, id string
			if e = actions.Scan(&position, &method, &status, &content, &id); e != nil {
				actions.Close()
				return e
			}
			count++
			if status != "sent" || id == "" {
				continue
			}
			var obj map[string]any
			if json.Unmarshal([]byte(content), &obj) != nil {
				continue
			}
			text, _ := obj["text"].(string)
			kind, _ := obj["media_kind"].(string)
			if kind != "" {
				name, _ := obj["media_name"].(string)
				text += fmt.Sprintf(" [已发送%s：%s]", kind, name)
			}
			if reaction, _ := obj["reaction"].(string); reaction != "" {
				text += " [已确认回应：" + reaction + "]"
			}
			if strings.TrimSpace(text) == "" {
				text = "[已确认媒体发送；原记录未描述内容]"
			}
			deliveries[index] = append(deliveries[index], Delivery{ID: id, At: d.at, Text: text, Kind: kind, State: "sent", Source: "send_action:" + d.ticket})
		}
		e = actions.Err()
		actions.Close()
		if e != nil {
			return e
		}
		if count == 0 && d.status == "sent" {
			var ids []string
			_ = json.Unmarshal([]byte(d.ids), &ids)
			if len(ids) > 0 {
				text := d.text
				if strings.TrimSpace(text) == "" {
					text = "[已确认媒体发送；原记录未描述内容]"
				}
				deliveries[index] = append(deliveries[index], Delivery{ID: strings.Join(ids, ","), At: d.at, Text: text, Kind: d.kind, State: "sent", Source: "send:" + d.ticket})
			}
		}
		if d.status == "unknown" || d.status == "reserved" || d.status == "partial" {
			deliveries[index] = append(deliveries[index], Delivery{ID: d.ticket, At: d.at, State: d.status, Source: "send:" + d.ticket})
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, err := s.DB.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	changed := false
	for i, t := range turns {
		old, _ := json.Marshal(t.Deliveries)
		raw, _ := json.Marshal(deliveries[i])
		if string(old) == string(raw) {
			continue
		}
		if _, err = tx.Exec("UPDATE context_turns SET deliveries=? WHERE id=?", string(raw), t.ID); err != nil {
			return err
		}
		changed = true
	}
	if changed {
		if _, err = tx.Exec("UPDATE context_sessions SET revision=revision+1 WHERE session=?", scope.Session); err != nil {
			return err
		}
	}
	return tx.Commit()
}
