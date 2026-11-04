// Package contextledger keeps stable, scoped conversation indexes and versioned
// summaries independently of the replaceable model input view.
package contextledger

import (
	"bytes"
	"compress/zlib"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/sipeed/picoclaw/pkg/providers"
	_ "modernc.org/sqlite"
)

type Scope struct {
	Session string `json:"session"`
	Chat    string `json:"chat"`
	Channel string `json:"channel"`
}

type Origin struct {
	Key       string
	Kind      string
	MessageID string
	Text      string
	At        time.Time
}

type Input struct {
	Message providers.Message
	Origin  Origin
}

type Delivery struct {
	ID     string  `json:"id"`
	At     float64 `json:"at"`
	Text   string  `json:"text"`
	Kind   string  `json:"kind,omitempty"`
	State  string  `json:"state"`
	Source string  `json:"source"`
}

type Turn struct {
	ID         int64               `json:"record_id"`
	Number     int64               `json:"number"`
	Kind       string              `json:"kind"`
	MessageID  string              `json:"message_id,omitempty"`
	At         float64             `json:"at"`
	User       string              `json:"user"`
	Deliveries []Delivery          `json:"deliveries"`
	Reasoning  []string            `json:"思考"`
	ToolBrief  string              `json:"tool_brief,omitempty"`
	Complete   bool                `json:"complete"`
	Messages   []providers.Message `json:"-"`
}

// Make the two speakers explicit in summary materials. The model request's
// outer user role is task input, not the speaker of every quoted field.
func (t Turn) MarshalJSON() ([]byte, error) {
	type plain Turn
	speaker := "jg"
	if t.Kind == "member" {
		speaker = "群聊用户"
	}
	if t.Kind == "legacy" {
		speaker = "旧记录用户字段（来源未匹配）"
	}
	if t.Kind == "internal" {
		speaker = "系统事件"
	}
	when := ""
	if t.At > 0 {
		when = time.Unix(0, int64(t.At*1e9)).In(time.FixedZone("UTC+8", 8*3600)).Format(time.RFC3339)
	}
	return json.Marshal(struct {
		plain
		Speaker string `json:"user_speaker"`
		Time    string `json:"time_utc8"`
	}{plain(t), speaker, when})
}

func (d Delivery) MarshalJSON() ([]byte, error) {
	type plain Delivery
	return json.Marshal(struct {
		plain
		Speaker string `json:"speaker"`
	}{plain(d), "大肥鱼"})
}

type State struct {
	Version     int64       `json:"version"`
	Covered     int64       `json:"covered_through"`
	Body        string      `json:"body"`
	Agreements  []Agreement `json:"agreements"`
	Quotes      []Quote     `json:"verified_quotes,omitempty"`
	Corrections string      `json:"verified_agreement_updates,omitempty"`
	Cooldown    float64     `json:"cooldown_until"`
	Revision    int64       `json:"revision"`
}

type Store struct {
	DB           *sql.DB
	Path         string
	mu           sync.Mutex
	Reclassified bool
}

func Open(path string) (*Store, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o770); err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	s := &Store{DB: db, Path: path}
	_, err = db.Exec(`PRAGMA busy_timeout=3000; PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS context_sessions(session TEXT PRIMARY KEY,chat TEXT NOT NULL,channel TEXT NOT NULL,active_version INTEGER NOT NULL DEFAULT 0,covered INTEGER NOT NULL DEFAULT 0,cooldown REAL NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0,legacy_summary TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS context_turns(id INTEGER PRIMARY KEY,session TEXT NOT NULL,number INTEGER NOT NULL,source_key TEXT NOT NULL,kind TEXT NOT NULL,message_id TEXT NOT NULL,at REAL NOT NULL,user_text TEXT NOT NULL,complete INTEGER NOT NULL DEFAULT 0,deliveries TEXT NOT NULL DEFAULT '[]',UNIQUE(session,source_key));
CREATE UNIQUE INDEX IF NOT EXISTS context_turn_numbers ON context_turns(session,number) WHERE number>0;
CREATE TABLE IF NOT EXISTS context_payloads(hash TEXT PRIMARY KEY,data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS context_items(id INTEGER PRIMARY KEY,session TEXT NOT NULL,turn_id INTEGER NOT NULL,history_id TEXT NOT NULL,payload_hash TEXT NOT NULL,UNIQUE(session,history_id));
CREATE INDEX IF NOT EXISTS context_items_turn ON context_items(turn_id,id);
CREATE TABLE IF NOT EXISTS context_summaries(id INTEGER PRIMARY KEY,session TEXT NOT NULL,parent INTEGER NOT NULL,cover_from INTEGER NOT NULL,cover_to INTEGER NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,raw TEXT NOT NULL,body TEXT NOT NULL,agreements TEXT NOT NULL,created REAL NOT NULL,response TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS context_agreements(session TEXT NOT NULL,id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(session,id));
CREATE TABLE IF NOT EXISTS context_observations(id INTEGER PRIMARY KEY,session TEXT NOT NULL,at REAL NOT NULL,input_tokens INTEGER NOT NULL,input_budget INTEGER NOT NULL,summary_version INTEGER NOT NULL,covered INTEGER NOT NULL,history_numbers TEXT NOT NULL,blocks TEXT NOT NULL);`)
	if err != nil {
		db.Close()
		return nil, err
	}
	for _, name := range []string{path, path + "-wal", path + "-shm"} {
		if _, err := os.Stat(name); err == nil {
			if err = os.Chmod(name, 0o660); err != nil {
				db.Close()
				return nil, err
			}
		}
	}
	if err = s.repairInternalSources(); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) Close() error { return s.DB.Close() }
func hash(raw []byte) string  { v := sha256.Sum256(raw); return hex.EncodeToString(v[:]) }

// Payloads are content-addressed once, rather than repeated in every turn,
// summary version and detail page. Existing MCP artifact references stay intact.
func putPayload(tx *sql.Tx, msg providers.Message) (string, error) {
	raw, err := json.Marshal(msg)
	if err != nil {
		return "", err
	}
	key := hash(raw)
	var b bytes.Buffer
	w := zlib.NewWriter(&b)
	if _, err = w.Write(raw); err != nil {
		return "", err
	}
	if err = w.Close(); err != nil {
		return "", err
	}
	_, err = tx.Exec("INSERT OR IGNORE INTO context_payloads(hash,data) VALUES(?,?)", key, b.Bytes())
	return key, err
}

func DecodePayload(data []byte) (providers.Message, error) {
	r, err := zlib.NewReader(bytes.NewReader(data))
	if err != nil {
		return providers.Message{}, err
	}
	defer r.Close()
	var msg providers.Message
	err = json.NewDecoder(io.LimitReader(r, 16<<20)).Decode(&msg)
	return msg, err
}

func (s *Store) Sync(scope Scope, inputs []Input, complete bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, err := s.DB.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err = tx.Exec("INSERT INTO context_sessions(session,chat,channel) VALUES(?,?,?) ON CONFLICT(session) DO UPDATE SET chat=excluded.chat,channel=excluded.channel", scope.Session, scope.Chat, scope.Channel); err != nil {
		return err
	}
	var current int64
	changed := false
	for _, input := range inputs {
		msg, o := input.Message, input.Origin
		if msg.HistoryID == "" {
			return fmt.Errorf("message has no stable history identity")
		}
		if msg.Role == "user" || current == 0 {
			if o.Key == "" {
				o.Key = "orphan:" + msg.HistoryID
				o.Kind = "internal"
				o.Text = ""
			}
			if o.Kind == "" {
				o.Kind = "legacy"
			}
			err = tx.QueryRow("SELECT id FROM context_turns WHERE session=? AND source_key=?", scope.Session, o.Key).Scan(&current)
			if err == sql.ErrNoRows {
				var number int64
				if o.Kind != "internal" {
					if err = tx.QueryRow("SELECT COALESCE(MAX(number),0)+1 FROM context_turns WHERE session=?", scope.Session).Scan(&number); err != nil {
						return err
					}
				}
				at := float64(0)
				if !o.At.IsZero() {
					at = float64(o.At.UnixNano()) / 1e9
				}
				res, e := tx.Exec("INSERT INTO context_turns(session,number,source_key,kind,message_id,at,user_text) VALUES(?,?,?,?,?,?,?)", scope.Session, number, o.Key, o.Kind, o.MessageID, at, o.Text)
				if e != nil {
					return e
				}
				current, e = res.LastInsertId()
				if e != nil {
					return e
				}
			} else if err != nil {
				return err
			}
		}
		var exists int
		if err = tx.QueryRow("SELECT count(*) FROM context_items WHERE session=? AND history_id=?", scope.Session, msg.HistoryID).Scan(&exists); err != nil {
			return err
		}
		if exists > 0 {
			continue
		}
		key, e := putPayload(tx, msg)
		if e != nil {
			return e
		}
		if _, err = tx.Exec("INSERT INTO context_items(session,turn_id,history_id,payload_hash) VALUES(?,?,?,?)", scope.Session, current, msg.HistoryID, key); err != nil {
			return err
		}
		changed = true
	}
	if current != 0 {
		res, e := tx.Exec("UPDATE context_turns SET complete=1 WHERE session=? AND id<>? AND complete=0", scope.Session, current)
		if e != nil {
			return e
		}
		if n, _ := res.RowsAffected(); n > 0 {
			changed = true
		}
		if complete {
			res, e = tx.Exec("UPDATE context_turns SET complete=1 WHERE id=? AND complete=0", current)
			if e != nil {
				return e
			}
			if n, _ := res.RowsAffected(); n > 0 {
				changed = true
			}
		}
	}
	if changed {
		if _, err = tx.Exec("UPDATE context_sessions SET revision=revision+1 WHERE session=?", scope.Session); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s *Store) Turns(session string) ([]Turn, error) {
	return s.loadTurns(session, nil, true)
}

func (s *Store) Headers(session string) ([]Turn, error) { return s.loadTurns(session, nil, false) }
func (s *Store) Uncovered(session string, covered int64) ([]Turn, error) {
	return s.loadTurns(session, &covered, true)
}

func (s *Store) loadTurns(session string, covered *int64, details bool) ([]Turn, error) {
	query := "SELECT id,number,kind,message_id,at,user_text,complete,deliveries FROM context_turns WHERE session=?"
	args := []any{session}
	if covered != nil {
		query += " AND (number>? OR (number=0 AND complete=0))"
		args = append(args, *covered)
	}
	query += " ORDER BY id"
	rows, err := s.DB.Query(query, args...)
	if err != nil {
		return nil, err
	}
	var turns []Turn
	for rows.Next() {
		var t Turn
		var complete int
		var delivered string
		if err = rows.Scan(&t.ID, &t.Number, &t.Kind, &t.MessageID, &t.At, &t.User, &complete, &delivered); err != nil {
			rows.Close()
			return nil, err
		}
		t.Complete = complete != 0
		if err = json.Unmarshal([]byte(delivered), &t.Deliveries); err != nil {
			rows.Close()
			return nil, err
		}
		turns = append(turns, t)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	if !details {
		return turns, nil
	}
	for i := range turns {
		rows, err = s.DB.Query("SELECT p.data FROM context_items i JOIN context_payloads p ON p.hash=i.payload_hash WHERE turn_id=? ORDER BY i.id", turns[i].ID)
		if err != nil {
			return nil, err
		}
		var briefs []string
		seen := map[string]bool{}
		labels := map[string]string{}
		for rows.Next() {
			var raw []byte
			if err = rows.Scan(&raw); err != nil {
				rows.Close()
				return nil, err
			}
			msg, e := DecodePayload(raw)
			if e != nil {
				rows.Close()
				return nil, e
			}
			turns[i].Messages = append(turns[i].Messages, msg)
			for _, call := range msg.ToolCalls {
				if call.Function != nil {
					label := strings.TrimPrefix(call.Function.Name, "mcp_reader_")
					var args map[string]any
					if json.Unmarshal([]byte(call.Function.Arguments), &args) == nil {
						if action, ok := args["action"].(string); ok {
							label += "/" + action
						}
					}
					labels[call.ID] = label
				}
			}
			if r := strings.TrimSpace(msg.ReasoningContent); r != "" && !seen[r] {
				turns[i].Reasoning = append(turns[i].Reasoning, r)
				seen[r] = true
			}
			if msg.Role == "tool" {
				brief := ToolBrief(msg)
				if label := labels[msg.ToolCallID]; label != "" {
					brief = label + "：" + brief
				}
				briefs = append(briefs, brief)
			}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return nil, err
		}
		if len(briefs) > 3 {
			briefs = append(briefs[:2], fmt.Sprintf("另有%d项工具结果，明细按本轮编号读取", len(briefs)-2))
		}
		turns[i].ToolBrief = strings.Join(briefs, "；")
	}
	return turns, nil
}

func ToolBrief(msg providers.Message) string {
	content := msg.Content
	if _, tail, ok := strings.Cut(content, "\n\n[Tool diagnostics]\n"); ok {
		content = tail
	}
	state := "工具已返回"
	var value map[string]any
	if json.Unmarshal([]byte(content), &value) == nil {
		if blocks, ok := value["content"].([]any); ok && len(blocks) > 0 {
			if block, ok := blocks[0].(map[string]any); ok {
				if text, ok := block["text"].(string); ok {
					var inner map[string]any
					if json.Unmarshal([]byte(text), &inner) == nil {
						value = inner
					}
				}
			}
		}
		for _, key := range []string{"status", "state"} {
			if status, ok := value[key].(string); ok {
				switch status {
				case "queued", "pending", "processing", "running":
					state = "工具任务排队或执行中"
				case "unknown", "partial", "uncertain":
					state = "工具结果尚未完全确认"
				case "failed", "error", "rejected":
					state = "工具执行失败"
				case "saved", "ok", "completed", "sent", "success":
					state = "工具操作已完成"
				}
			}
		}
		if value["error"] != nil || value["isError"] == true {
			state = "工具返回错误"
		}
	}
	if strings.HasPrefix(content, "Error") || strings.Contains(content, "[Command exited with code 1]") {
		state = "工具执行失败"
	}
	return state
}

func (s *Store) State(session string) (State, error) {
	var st State
	err := s.DB.QueryRow("SELECT active_version,covered,cooldown,revision,legacy_summary FROM context_sessions WHERE session=?", session).Scan(&st.Version, &st.Covered, &st.Cooldown, &st.Revision, &st.Body)
	if err == sql.ErrNoRows {
		return st, nil
	}
	if err != nil {
		return st, err
	}
	if st.Version != 0 {
		var raw string
		if err = s.DB.QueryRow("SELECT body,raw FROM context_summaries WHERE id=? AND status='accepted'", st.Version).Scan(&st.Body, &raw); err != nil {
			return st, err
		}
		if c, e := ParseCandidate(raw); e == nil {
			st.Quotes = c.Quotes
		}
	}
	rows, err := s.DB.Query("SELECT data FROM context_agreements WHERE session=? ORDER BY id", session)
	if err != nil {
		return st, err
	}
	defer rows.Close()
	for rows.Next() {
		var text string
		var a Agreement
		if err = rows.Scan(&text); err != nil {
			return st, err
		}
		if err = json.Unmarshal([]byte(text), &a); err != nil {
			return st, err
		}
		st.Agreements = append(st.Agreements, a)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return st, err
	}
	if st.Version != 0 {
		var data string
		if err = s.DB.QueryRow("SELECT agreements FROM context_summaries WHERE id=?", st.Version).Scan(&data); err != nil {
			return st, err
		}
		var old []Agreement
		_ = json.Unmarshal([]byte(data), &old)
		previous := map[string]Agreement{}
		for _, a := range old {
			previous[a.ID] = a
		}
		for _, a := range st.Agreements {
			prior, ok := previous[a.ID]
			if (!ok || prior.Text != a.Text || prior.State != a.State) && a.Home == "" {
				st.Corrections += renderAgreement(a) + "\n"
			}
		}
	} else {
		for _, a := range st.Agreements {
			if a.Home == "" {
				st.Corrections += renderAgreement(a) + "\n"
			}
		}
	}
	return st, nil
}

func Render(t Turn) string {
	when := "时间未记录"
	if t.At > 0 {
		when = time.Unix(0, int64(t.At*1e9)).In(time.FixedZone("UTC+8", 8*3600)).Format("1月2日 15:04")
	}
	heading := fmt.Sprintf("[%s｜#%d]", when, t.Number)
	speaker := "jg"
	if t.Kind == "legacy" {
		speaker = "旧记录用户字段（原始接收事件未匹配）"
	}
	if t.Kind == "member" {
		speaker = "群聊用户"
	}
	if t.Kind == "internal" {
		heading = fmt.Sprintf("[%s｜系统事件 %d]", when, t.ID)
		speaker = "系统补充"
	}
	var body strings.Builder
	fmt.Fprintf(&body, "%s\n%s：%s", heading, speaker, t.User)
	for _, d := range t.Deliveries {
		if d.State == "sent" {
			fmt.Fprintf(&body, "\n大肥鱼：%s", d.Text)
		} else {
			fmt.Fprintf(&body, "\n工具发送状态：%s（送达正文未确认）", d.State)
		}
	}
	if len(t.Reasoning) > 0 {
		body.WriteString("\n思考（当时推断）：" + strings.Join(t.Reasoning, "\n"))
	}
	if t.ToolBrief != "" {
		body.WriteString("\n工具结果：" + t.ToolBrief)
	}
	return body.String()
}
