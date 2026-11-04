package contextledger

import (
	"database/sql"
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/sipeed/picoclaw/pkg/providers"
)

func testStore(t *testing.T) (*Store, Scope) {
	t.Helper()
	s, err := Open(filepath.Join(t.TempDir(), "turns.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	return s, Scope{Session: "private", Chat: "123", Channel: "telegram"}
}
func userInput(id, text string) Input {
	return Input{Message: providers.Message{HistoryID: id, Role: "user", Content: text}, Origin: Origin{Key: "inbound:" + id, Kind: "human", MessageID: id, Text: text, At: time.Unix(1700000000, 0)}}
}
func seedTurn(t *testing.T, s *Store, scope Scope) {
	t.Helper()
	if err := s.Sync(scope, []Input{userInput("1", "可以开始；后来已经完成。")}, true); err != nil {
		t.Fatal(err)
	}
}

func TestStableNumbersSurviveReplayRestartAndSummary(t *testing.T) {
	s, scope := testStore(t)
	seedTurn(t, s, scope)
	replay := userInput("other-payload-id", "可以开始；后来已经完成。")
	replay.Origin.Key = "inbound:1"
	if err := s.Sync(scope, []Input{replay, userInput("2", "下一句话")}, true); err != nil {
		t.Fatal(err)
	}
	turns, err := s.Turns(scope.Session)
	if err != nil || len(turns) != 2 || turns[0].Number != 1 || turns[1].Number != 2 {
		t.Fatalf("numbers: %+v %v", turns, err)
	}
	path := s.Path
	s.Close()
	reopened, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	again, err := reopened.Turns(scope.Session)
	if err != nil || again[1].Number != 2 {
		t.Fatal(again, err)
	}
}

func TestInternalCallbackNeverAllocatesHumanNumber(t *testing.T) {
	s, scope := testStore(t)
	seedTurn(t, s, scope)
	input := Input{Message: providers.Message{HistoryID: "callback", Role: "user", Content: "执行端结果"}, Origin: Origin{Key: "event:one", Kind: "internal", Text: "执行端结果"}}
	if err := s.Sync(scope, []Input{userInput("1", "可以开始；后来已经完成。"), input, userInput("2", "新的真人消息")}, true); err != nil {
		t.Fatal(err)
	}
	turns, _ := s.Turns(scope.Session)
	if len(turns) != 3 || turns[1].Number != 0 || turns[2].Number != 2 {
		t.Fatalf("callback became a human round: %+v", turns)
	}
}

func TestWrappedCallbacksAndEarlyLegacyIndexRepairKeepHumanNumbers(t *testing.T) {
	for _, backend := range []string{"Codex", "DSH", "Claude Code"} {
		for _, kind := range []string{"查询结果", "协作回执"} {
			o := ResolveOrigin(nil, Scope{}, providers.Message{HistoryID: "callback", Content: "[" + backend + kind + "]\n执行入口的结果"})
			if o.Kind != "internal" {
				t.Fatalf("wrapper misclassified: %+v", o)
			}
		}
	}
	s, scope := testStore(t)
	seedTurn(t, s, scope)
	legacy := userInput("old-callback", "[Codex协作回执]\nCodex回合状态：completed")
	legacy.Origin.Kind = "legacy"
	if err := s.Sync(scope, []Input{legacy, userInput("next", "下一句真人原话")}, true); err != nil {
		t.Fatal(err)
	}
	path := s.Path
	s.Close()
	reopened, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	turns, err := reopened.Turns(scope.Session)
	if err != nil {
		t.Fatal(err)
	}
	if !reopened.Reclassified || turns[0].Number != 1 || turns[1].Kind != "internal" || turns[1].Number != 0 || turns[2].Number != 3 {
		t.Fatalf("human number changed or callback survived: %+v", turns)
	}
}

func TestOriginalToolsAndReasoningAreRecoverableFromOnePayloadIndex(t *testing.T) {
	s, scope := testStore(t)
	long := strings.Repeat("真实工具明细", 4000)
	inputs := []Input{userInput("1", "改文件"), {Message: providers.Message{HistoryID: "reason", Role: "assistant", ReasoningContent: "当时的真实推断", ToolCalls: []providers.ToolCall{{ID: "call1", Function: &providers.FunctionCall{Name: "write_file", Arguments: `{"path":"private.md"}`}}}}}, {Message: providers.Message{HistoryID: "tool", Role: "tool", ToolCallID: "call1", Content: long}}}
	if err := s.Sync(scope, inputs, true); err != nil {
		t.Fatal(err)
	}
	if err := s.Sync(scope, inputs, true); err != nil {
		t.Fatal(err)
	}
	turns, err := s.Turns(scope.Session)
	if err != nil || turns[0].Messages[2].Content != long || len(turns[0].Reasoning) != 1 {
		t.Fatal("lost detail", err)
	}
	var count int
	s.DB.QueryRow("SELECT count(*) FROM context_payloads").Scan(&count)
	if count != 3 {
		t.Fatalf("duplicated payloads: %d", count)
	}
	if strings.Contains(turns[0].ToolBrief, "private.md") || strings.Contains(turns[0].ToolBrief, long[:100]) {
		t.Fatal("raw tool dump leaked into daily brief")
	}
}

func runtimeFixture(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", filepath.Join(t.TempDir(), "runtime.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	_, err = db.Exec(`CREATE TABLE events(id INTEGER PRIMARY KEY,chat TEXT,msg TEXT,kind TEXT,content TEXT,created REAL);
CREATE TABLE sends(ticket TEXT,chat TEXT,status TEXT,content TEXT,kind TEXT,created REAL,message_ids TEXT);
CREATE TABLE send_turn_attempts(chat TEXT,msg TEXT,ticket TEXT);
CREATE TABLE send_actions(ticket TEXT,position INTEGER,method TEXT,status TEXT,content TEXT,message_id TEXT);`)
	if err != nil {
		t.Fatal(err)
	}
	return db
}

func TestTrustedUserTextExcludesSystemSupplementAndCallbacks(t *testing.T) {
	db := runtimeFixture(t)
	db.Exec("INSERT INTO events VALUES(1,'123','42','user','原始用户的话',1700000000)")
	msg := providers.Message{HistoryID: "msg", Role: "user", Content: "原始用户的话\n[检索与表达经验补充]", SourceMessageID: "42"}
	o := ResolveOrigin(db, Scope{Chat: "123"}, msg)
	if o.Text != "原始用户的话" || o.Kind != "human" || o.MessageID != "42" {
		t.Fatal(o)
	}
	msg.SourceMessageID = ""
	msg.Content = "先前的Codex查询已返回\n原始用户的话"
	if o = ResolveOrigin(db, Scope{Chat: "123"}, msg); o.Kind != "internal" {
		t.Fatal("callback misattributed", o)
	}
}

func TestConfirmedNativeMCPMediaAndUncertainSendsStayDistinct(t *testing.T) {
	s, scope := testStore(t)
	seedTurn(t, s, scope)
	db := runtimeFixture(t)
	for _, v := range []struct{ id, state, text, kind, ids string }{{"native", "sent", "原生message正文", "text", `["11"]`}, {"mcp", "partial", "不应把这整段当已发送", "text", `["12"]`}, {"uncertain", "reserved", "从未确认的正文", "text", ""}} {
		db.Exec("INSERT INTO sends VALUES(?,?,?,?,?,?,?)", v.id, "123", v.state, v.text, v.kind, 1700000001, v.ids)
		db.Exec("INSERT INTO send_turn_attempts VALUES('123','1',?)", v.id)
	}
	content, _ := json.Marshal(map[string]any{"text": "已发送的那一段", "media_kind": "贴纸", "media_name": "emote.webm"})
	db.Exec("INSERT INTO send_actions VALUES('mcp',1,'sendSticker','sent',?,'12')", string(content))
	if err := s.RefreshDeliveries(scope, db); err != nil {
		t.Fatal(err)
	}
	turns, err := s.Turns(scope.Session)
	if err != nil {
		t.Fatal(err)
	}
	view := Render(turns[0])
	for _, text := range []string{"原生message正文", "已发送的那一段", "贴纸", "reserved"} {
		if !strings.Contains(view, text) {
			t.Fatalf("missing %q: %s", text, view)
		}
	}
	if strings.Contains(view, "从未确认的正文") || strings.Contains(view, "不应把这整段当已发送") {
		t.Fatal("unconfirmed delivery treated as conversation")
	}
}

func candidateFixture(t *testing.T) (*Store, Scope, Material, Candidate) {
	s, scope := testStore(t)
	seedTurn(t, s, scope)
	_ = s.SeedLegacy(scope, "旧摘要继续生效")
	st, _ := s.State(scope.Session)
	turns, _ := s.Turns(scope.Session)
	m := Material{Previous: st, Past: turns, From: 1, To: 1, MaxNumber: 1, Homes: map[string]string{}}
	c := Candidate{Summary: "jg说可以开始，大肥鱼接着处理，后来jg说明已经完成。", From: 1, To: 1, Quotes: []Quote{{Turn: 1, Speaker: "jg", Text: "可以开始"}}}
	return s, scope, m, c
}

func TestCandidateRejectsTruncationBadSourceBoundsAndLength(t *testing.T) {
	_, _, m, c := candidateFixture(t)
	for _, kind := range []string{"empty", "length", "source", "bounds", "too_long"} {
		t.Run(kind, func(t *testing.T) {
			bad := c
			finish := "stop"
			switch kind {
			case "empty":
				bad.Summary = ""
			case "length":
				finish = "length"
			case "source":
				bad.Quotes = []Quote{{Turn: 1, Speaker: "jg", Text: "从未说过"}}
			case "bounds":
				bad.To = 2
			case "too_long":
				bad.Summary = strings.Repeat("字", 1801)
			}
			if _, err := Validate(bad, m, finish); err == nil {
				t.Fatal("invalid summary accepted")
			}
		})
	}
}

func TestCandidateDoesNotPublishRecentDialogueAsCoveredPast(t *testing.T) {
	_, _, m, c := candidateFixture(t)
	m.Recent = []Turn{{Number: 2, Kind: "human", User: "近期还没转入过往区的原话", Complete: true}}
	c.Summary = "后来jg说“近期还没转入过往区的原话”。"
	if _, err := Validate(c, m, "stop"); err == nil {
		t.Fatal("recent-only quote copied into covered past")
	}
	c.Summary = "过去的真实经过。"
	c.Quotes = []Quote{{Turn: 2, Speaker: "jg", Text: "近期还没转入过往区的原话"}}
	if _, err := Validate(c, m, "stop"); err == nil {
		t.Fatal("recent-only quote claimed as covered source")
	}
}

func TestKnownAgreementRequiresPreservationAndNewChangeEvidence(t *testing.T) {
	_, _, m, c := candidateFixture(t)
	old := Agreement{ID: "task", Text: "处理原任务", State: "active", Sources: []Quote{{Turn: 1, Speaker: "jg", Text: "可以开始"}}}
	m.Previous.Agreements = []Agreement{old}
	if _, err := Validate(c, m, "stop"); err == nil {
		t.Fatal("lost agreement accepted")
	}
	changed := old
	changed.State = "completed"
	c.Agreements = []Agreement{changed}
	if _, err := Validate(c, m, "stop"); err == nil {
		t.Fatal("unsourced completion accepted")
	}
	m.Recent = []Turn{{Number: 2, Kind: "human", User: "后来已经完成。", Complete: true}}
	changed.ChangedBy = []Quote{{Turn: 2, Speaker: "jg", Text: "已经完成"}}
	c.Agreements = []Agreement{changed}
	body, err := Validate(c, m, "stop")
	if err != nil || !strings.Contains(body, "已确认完成") {
		t.Fatal(body, err)
	}
	if c.To != 1 {
		t.Fatal("recent correction advanced coverage")
	}
}

func TestRejectedCandidateRetainsSummaryRawAndCursorAndCoolsDown(t *testing.T) {
	s, scope, m, c := candidateFixture(t)
	_, err := s.RecordCandidate(scope.Session, m, c, `{"truncated":true}`, "", "generation_not_finished:length", false, 10*time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	after, _ := s.State(scope.Session)
	if after.Body != m.Previous.Body || after.Covered != 0 || after.Version != 0 || after.Cooldown < float64(time.Now().Add(9*time.Minute).Unix()) {
		t.Fatal(after)
	}
	turns, _ := s.Turns(scope.Session)
	if len(turns) != 1 || turns[0].Messages[0].Content == "" {
		t.Fatal("source was discarded")
	}
}

func TestAcceptedCandidatePublishesDurablyAndRejectsStaleInput(t *testing.T) {
	s, scope, m, c := candidateFixture(t)
	body, err := Validate(c, m, "stop")
	if err != nil {
		t.Fatal(err)
	}
	id, err := s.RecordCandidate(scope.Session, m, c, "original model output", body, "", true, 10*time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	st, _ := s.State(scope.Session)
	if st.Version != id || st.Covered != 1 || st.Body != body {
		t.Fatal(st)
	}
	if _, err = s.RecordCandidate(scope.Session, m, c, "stale output", body, "", true, 10*time.Minute); err == nil {
		t.Fatal("stale candidate replaced current summary")
	}
	after, _ := s.State(scope.Session)
	if after.Version != id || after.Covered != 1 {
		t.Fatal("stale activation changed coverage")
	}
}

func TestRecentGroundedCompletionSurvivesRejectedProseWithoutMovingCoverage(t *testing.T) {
	s, scope, m, c := candidateFixture(t)
	old := Agreement{ID: "task", Text: "处理原任务", State: "active", Sources: []Quote{{Turn: 1, Speaker: "jg", Text: "可以开始"}}}
	m.Previous.Agreements = []Agreement{old}
	m.Recent = []Turn{{Number: 2, Kind: "human", User: "后来已经完成。", Complete: true}}
	updated := old
	updated.State = "completed"
	updated.ChangedBy = []Quote{{Turn: 2, Speaker: "jg", Text: "已经完成"}}
	c.Agreements = []Agreement{updated}
	c.Summary = ""
	if _, err := s.RecordCandidate(scope.Session, m, c, "empty prose", "", "empty_summary", false, 10*time.Minute); err != nil {
		t.Fatal(err)
	}
	after, err := s.State(scope.Session)
	if err != nil {
		t.Fatal(err)
	}
	if after.Body != m.Previous.Body || after.Covered != m.Previous.Covered || after.Version != m.Previous.Version || len(after.Agreements) != 1 || after.Agreements[0].State != "completed" || !strings.Contains(after.Corrections, "完成") {
		t.Fatalf("grounded update or old state lost: %+v", after)
	}
}
