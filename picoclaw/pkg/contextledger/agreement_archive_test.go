package contextledger

import (
	"encoding/json"
	"strings"
	"testing"
)

func nextAgreementCandidate(t *testing.T, s *Store, scope Scope) (Material, Candidate) {
	t.Helper()
	st, err := s.State(scope.Session)
	if err != nil {
		t.Fatal(err)
	}
	turns, err := s.Uncovered(scope.Session, st.Covered)
	if err != nil || len(turns) == 0 {
		t.Fatal("missing uncovered turns", turns, err)
	}
	m := Material{Previous: st, Past: turns[:1], Recent: turns[1:], From: turns[0].Number, To: turns[0].Number, MaxNumber: turns[len(turns)-1].Number,
		Homes: map[string]string{"USER.md": "执行这项一次性约定"}}
	return m, Candidate{From: m.From, To: m.To, Summary: "接着聊下一件事。"}
}

func TestTerminalAgreementArchivesAfterOneAcceptedDisplay(t *testing.T) {
	for _, state := range []string{"completed", "revoked", "superseded"} {
		for _, carry := range []bool{false, true} {
			name := state + "/omitted"
			if carry {
				name = state + "/carried"
			}
			t.Run(name, func(t *testing.T) {
				s, scope := testStore(t)
				inputs := []Input{userInput("1", "可以开始"), userInput("2", "已完成，也已撤销，由新约定替代。"), userInput("3", "继续聊天")}
				if err := s.Sync(scope, inputs, true); err != nil {
					t.Fatal(err)
				}
				// Existing rows have neither lifecycle field. They stay readable.
				old := Agreement{ID: "one-off", Text: "执行这项一次性约定", State: "active", Sources: []Quote{{Turn: 1, Speaker: "jg", Text: "可以开始"}}, Home: "USER.md", HomeQuote: "执行这项一次性约定"}
				data, _ := json.Marshal(old)
				if _, err := s.DB.Exec("INSERT INTO context_agreements VALUES(?,?,?)", scope.Session, old.ID, string(data)); err != nil {
					t.Fatal(err)
				}
				m, c := nextAgreementCandidate(t, s, scope)
				completed := old
				completed.State = state
				completed.ChangedBy = []Quote{{Turn: 2, Speaker: "jg", Text: "已完成，也已撤销，由新约定替代。"}}
				// Candidate metadata cannot shorten the required accepted display.
				completed.TerminalSince, completed.ArchivedVersion = 123, 124
				c.Agreements = []Agreement{completed}
				body, err := Validate(c, m, "stop")
				if err != nil || !strings.Contains(body, old.Text) {
					t.Fatal("first terminal version must display even with a home", body, err)
				}
				raw, _ := json.Marshal(c)
				v1, err := s.RecordCandidate(scope.Session, m, c, string(raw), body, "", true, 0)
				if err != nil {
					t.Fatal(err)
				}
				m, c = nextAgreementCandidate(t, s, scope)
				if len(m.Previous.Agreements) != 1 || m.Previous.Agreements[0].TerminalSince != v1 || m.Previous.Agreements[0].ArchivedVersion != 0 {
					t.Fatalf("first accepted display not recorded: %+v", m.Previous)
				}
				if carry {
					c.Agreements = append([]Agreement(nil), m.Previous.Agreements...)
					c.Agreements[0].TerminalSince = 0 // The model may omit metadata.
				}
				// A failed candidate cannot advance the archive clock.
				if _, err = s.RecordCandidate(scope.Session, m, c, "rejected", "", "summary_too_long", false, 0); err != nil {
					t.Fatal(err)
				}
				after, err := s.State(scope.Session)
				if err != nil || after.Version != v1 || len(after.Agreements) != 1 || after.Agreements[0].TerminalSince != v1 {
					t.Fatal("rejected candidate changed lifecycle", after, err)
				}
				c.Summary = strings.Repeat("字", 1800)
				body, err = Validate(c, m, "stop")
				if err != nil || strings.Contains(body, old.Text) {
					t.Fatal("retiring agreement still consumes the body budget", body, err)
				}
				raw, _ = json.Marshal(c)
				v2, err := s.RecordCandidate(scope.Session, m, c, string(raw), body, "", true, 0)
				if err != nil {
					t.Fatal(err)
				}
				st, err := s.State(scope.Session)
				if err != nil || st.Version != v2 || len(st.Agreements) != 0 || strings.Contains(st.Body+st.Corrections, old.Text) {
					t.Fatal("archived agreement still visible", st, err)
				}
				var stored string
				if err = s.DB.QueryRow("SELECT data FROM context_agreements WHERE session=? AND id=?", scope.Session, old.ID).Scan(&stored); err != nil {
					t.Fatal("archive record lost", err)
				}
				var archived Agreement
				if err = json.Unmarshal([]byte(stored), &archived); err != nil || archived.TerminalSince != v1 || archived.ArchivedVersion != v2 || archived.Text != old.Text {
					t.Fatal("archive data changed", archived, err)
				}
				m, c = nextAgreementCandidate(t, s, scope)
				wire := ModelMaterials(m)
				if len(wire.Agreements) != 0 || len(wire.Template.Agreements) != 0 || len(wire.Previous.Agreements) != 0 {
					t.Fatal("archive leaked into model materials", wire)
				}
				body, err = Validate(c, m, "stop")
				if err != nil || strings.Contains(body, old.Text) {
					t.Fatal("automatic carry restored archive", body, err)
				}
			})
		}
	}
}

func TestRejectedTerminalAgreementWaitsForFirstAcceptedDisplay(t *testing.T) {
	s, scope, m, c := candidateFixture(t)
	c.Agreements = []Agreement{{ID: "done", Text: "执行这项一次性约定", State: "completed", Sources: []Quote{{Turn: 1, Speaker: "jg", Text: "可以开始"}}, ChangedBy: []Quote{{Turn: 1, Speaker: "jg", Text: "后来已经完成"}}, TerminalSince: 123, ArchivedVersion: 124}}
	if _, err := s.RecordCandidate(scope.Session, m, c, "rejected", "", "empty_summary", false, 0); err != nil {
		t.Fatal(err)
	}
	st, err := s.State(scope.Session)
	if err != nil || len(st.Agreements) != 1 || st.Agreements[0].TerminalSince != 0 || st.Agreements[0].ArchivedVersion != 0 {
		t.Fatal("rejection started terminal lifecycle", st, err)
	}
	m.Previous = st
	c.Agreements = nil // The accepted candidate may omit the saved correction.
	body, err := Validate(c, m, "stop")
	if err != nil || !strings.Contains(body, "执行这项一次性约定") {
		t.Fatal("first accepted display missing", body, err)
	}
	id, err := s.RecordCandidate(scope.Session, m, c, "accepted", body, "", true, 0)
	if err != nil {
		t.Fatal(err)
	}
	st, err = s.State(scope.Session)
	if err != nil || len(st.Agreements) != 1 || st.Agreements[0].TerminalSince != id {
		t.Fatal("omitted agreement did not start its accepted lifecycle", st, err)
	}
}

func TestActiveAgreementClearsTerminalLifecycleAndStaysActive(t *testing.T) {
	s, scope := testStore(t)
	if err := s.Sync(scope, []Input{userInput("1", "可以开始"), userInput("2", "重新开始这件事"), userInput("3", "继续聊天")}, true); err != nil {
		t.Fatal(err)
	}
	old := Agreement{ID: "resumed", Text: "执行这项一次性约定", State: "completed", Sources: []Quote{{Turn: 1, Speaker: "jg", Text: "可以开始"}}, TerminalSince: 1}
	data, _ := json.Marshal(old)
	if _, err := s.DB.Exec("INSERT INTO context_agreements VALUES(?,?,?)", scope.Session, old.ID, string(data)); err != nil {
		t.Fatal(err)
	}
	m, c := nextAgreementCandidate(t, s, scope)
	active := old
	active.State = "active"
	active.ChangedBy = []Quote{{Turn: 2, Speaker: "jg", Text: "重新开始这件事"}}
	c.Agreements = []Agreement{active}
	for i := 0; i < 2; i++ {
		body, err := Validate(c, m, "stop")
		if err != nil {
			t.Fatal(err)
		}
		if _, err = s.RecordCandidate(scope.Session, m, c, "accepted", body, "", true, 0); err != nil {
			t.Fatal(err)
		}
		st, err := s.State(scope.Session)
		if err != nil || len(st.Agreements) != 1 || st.Agreements[0].State != "active" || st.Agreements[0].TerminalSince != 0 || st.Agreements[0].ArchivedVersion != 0 {
			t.Fatal("active agreement was retired or retained terminal clock", st, err)
		}
		m, c = nextAgreementCandidate(t, s, scope)
	}
}
