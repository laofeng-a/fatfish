package contextledger

import (
	"strings"
	"testing"
)

func proseFixture() (Material, Candidate) {
	m := Material{From: 1, To: 1, Past: []Turn{{Number: 1, Kind: "human", User: "原话就在这里", Complete: true}}}
	c := Candidate{From: 1, To: 1, Summary: "他提到“原话就在这里”，也说“这是编出来的”。"}
	return m, c
}

func TestUnmatchedProseQuoteBecomesParaphrase(t *testing.T) {
	m, c := proseFixture()
	body, err := Validate(c, m, "stop")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(body, "“原话就在这里”") {
		t.Fatal("sourced quotation lost its marks", body)
	}
	if strings.Contains(body, "“这是编出来的”") || !strings.Contains(body, "这是编出来的") {
		t.Fatal("unsourced quotation was not relaxed", body)
	}
}

func TestEmptyOrUnverifiedHomeIsClearedWithoutRejecting(t *testing.T) {
	m := Material{From: 1, To: 1, Past: []Turn{{Number: 1, Kind: "human", User: "可以开始", Complete: true}}, Homes: map[string]string{"USER.md": "喜欢简短回复"}}
	source := Quote{Turn: 1, Speaker: "jg", Text: "可以开始"}
	c := Candidate{From: 1, To: 1, Summary: "接上一次。", Agreements: []Agreement{{ID: "pref", Text: "喜欢简短", State: "active", Sources: []Quote{source}, Home: "USER.md"}}}
	body, err := Validate(c, m, "stop")
	if err != nil {
		t.Fatal(err)
	}
	if c.Agreements[0].Home != "" || c.Agreements[0].HomeQuote != "" {
		t.Fatalf("empty home quote was kept: %+v", c.Agreements[0])
	}
	if !strings.Contains(body, "喜欢简短") {
		t.Fatal("agreement was not rendered once home was cleared", body)
	}
	c.Agreements[0].Home = "USER.md"
	c.Agreements[0].HomeQuote = "文件里没有的句子"
	body, err = Validate(c, m, "stop")
	if err != nil {
		t.Fatal("mismatched home quote rejected the candidate", err)
	}
	if c.Agreements[0].Home != "" || c.Agreements[0].HomeQuote != "" || !strings.Contains(body, "喜欢简短") {
		t.Fatalf("mismatched home was kept or agreement not rendered: %+v %s", c.Agreements[0], body)
	}
	c.Agreements[0].Home = "USER.md"
	c.Agreements[0].HomeQuote = "喜欢简短回复"
	body, err = Validate(c, m, "stop")
	if err != nil {
		t.Fatal(err)
	}
	if c.Agreements[0].Home != "USER.md" || strings.Contains(body, "当时（") {
		t.Fatal("verified home was cleared or rendered again", body, c.Agreements[0].Home)
	}
}

func TestNewInactiveAgreementWithoutChangeIsDropped(t *testing.T) {
	m, c := proseFixture()
	c.Summary = "接上一次。"
	kept := Agreement{ID: "keep", Text: "沿用的约定", State: "active", Sources: []Quote{{Turn: 1, Speaker: "jg", Text: "原话就在这里"}}}
	dropped := Agreement{ID: "new-done", Text: "不该留下的约定", State: "completed", Sources: []Quote{{Turn: 1, Speaker: "jg", Text: "原话就在这里"}}}
	m.Previous.Agreements = []Agreement{kept}
	c.Agreements = []Agreement{dropped}
	if body, err := Validate(c, m, "stop"); err != nil || !strings.Contains(body, "沿用的约定") || strings.Contains(body, "不该留下的约定") {
		t.Fatal("omitted previous agreement was not carried, or the new inactive one was kept", err, body)
	}
	c.Agreements = []Agreement{kept, dropped}
	body, err := Validate(c, m, "stop")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(body, "不该留下的约定") || !strings.Contains(body, "沿用的约定") {
		t.Fatal("new inactive agreement was kept or the previous one was dropped", body)
	}
	changed := kept
	changed.State = "completed"
	c.Agreements = []Agreement{changed}
	if _, err = Validate(c, m, "stop"); err == nil || !strings.Contains(err.Error(), "agreement_changed_without_new_source:keep") {
		t.Fatal("existing agreement bypassed the old change rule", err)
	}
}

func TestAcceptedSummaryDoesNotActivateDroppedAgreement(t *testing.T) {
	s, scope, m, c := candidateFixture(t)
	c.Agreements = []Agreement{{ID: "new-done", Text: "不该留下的约定", State: "completed", Sources: []Quote{{Turn: 1, Speaker: "jg", Text: "可以开始"}}}}
	body, err := Validate(c, m, "stop")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(body, "不该留下的约定") {
		t.Fatal(body)
	}
	if _, err = s.RecordCandidate(scope.Session, m, c, "model output", body, "", true, 0); err != nil {
		t.Fatal(err)
	}
	st, err := s.State(scope.Session)
	if err != nil {
		t.Fatal(err)
	}
	if len(st.Agreements) != 0 {
		t.Fatalf("dropped agreement was activated: %+v", st.Agreements)
	}
}

// Live failure 2026-11-13: USER.md was rewritten, so the stored home_quote of a
// carried agreement no longer exists. Carrying, clearing or moving the home must
// not deadlock the summary while text and state stay unchanged.
func TestStaleHomeOfCarriedAgreementDoesNotDeadlock(t *testing.T) {
	source := Quote{Turn: 1, Speaker: "jg", Text: "可以开始"}
	stale := Agreement{ID: "comfort", Text: "他累时直接陪聊", State: "active", Sources: []Quote{source}, Home: "USER.md", HomeQuote: "已经被改写掉的旧句子"}
	m := Material{From: 2, To: 2, Past: []Turn{{Number: 2, Kind: "human", User: "继续", Complete: true}},
		Homes: map[string]string{"USER.md": "新版用户画像", "MEMORY.md": "真难受时：不问原因"}}
	m.Previous.Agreements = []Agreement{stale}
	m.Previous.Covered = 1
	for name, carried := range map[string]Agreement{
		"copied stale quote": stale,
		"cleared home":       {ID: "comfort", Text: stale.Text, State: "active", Sources: stale.Sources},
		"moved home":         {ID: "comfort", Text: stale.Text, State: "active", Sources: stale.Sources, Home: "MEMORY.md", HomeQuote: "真难受时：不问原因"},
	} {
		t.Run(name, func(t *testing.T) {
			c := Candidate{From: 2, To: 2, Summary: "接上一次。", Agreements: []Agreement{carried}}
			body, err := Validate(c, m, "stop")
			if err != nil {
				t.Fatal(err)
			}
			got := c.Agreements[0]
			if name == "moved home" {
				if got.Home != "MEMORY.md" || strings.Contains(body, stale.Text) {
					t.Fatalf("verified new home was lost: %+v %s", got, body)
				}
				return
			}
			if got.Home != "" || got.HomeQuote != "" || !strings.Contains(body, stale.Text) {
				t.Fatalf("stale home kept or agreement not rendered: %+v %s", got, body)
			}
		})
	}
	changed := stale
	changed.Text = "改写过的约定"
	c := Candidate{From: 2, To: 2, Summary: "接上一次。", Agreements: []Agreement{changed}}
	if _, err := Validate(c, m, "stop"); err == nil || !strings.Contains(err.Error(), "agreement_changed_without_new_source:comfort") {
		t.Fatal("text change bypassed the change-source rule", err)
	}
}

func TestPriorVerifiedHomeSurvivesBadCandidateHome(t *testing.T) {
	source := Quote{Turn: 1, Speaker: "jg", Text: "可以开始"}
	prior := Agreement{ID: "pref", Text: "喜欢简短", State: "active", Sources: []Quote{source}, Home: "USER.md", HomeQuote: "喜欢简短回复"}
	m := Material{From: 1, To: 1, Past: []Turn{{Number: 1, Kind: "human", User: "可以开始", Complete: true}}, Homes: map[string]string{"USER.md": "喜欢简短回复"}}
	m.Previous.Agreements = []Agreement{prior}
	bad := prior
	bad.Home, bad.HomeQuote = "SOUL.md", "编造的句子"
	c := Candidate{From: 1, To: 1, Summary: "接上一次。", Agreements: []Agreement{bad}}
	body, err := Validate(c, m, "stop")
	if err != nil {
		t.Fatal(err)
	}
	if c.Agreements[0].Home != "USER.md" || c.Agreements[0].HomeQuote != "喜欢简短回复" || strings.Contains(body, "当时（") {
		t.Fatalf("prior verified home was not restored: %+v %s", c.Agreements[0], body)
	}
}

func TestNewAgreementWithInventedHomeIsStoredWithoutHome(t *testing.T) {
	s, scope, m, c := candidateFixture(t)
	m.Homes = map[string]string{"SOUL.md": "现有人格正文"}
	c.Agreements = []Agreement{{ID: "proactive", Text: "主动找他聊", State: "active", Sources: []Quote{{Turn: 1, Speaker: "jg", Text: "可以开始"}}, Home: "SOUL.md", HomeQuote: "文件里没有这句"}}
	body, err := Validate(c, m, "stop")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.RecordCandidate(scope.Session, m, c, "model output", body, "", true, 0); err != nil {
		t.Fatal(err)
	}
	st, err := s.State(scope.Session)
	if err != nil {
		t.Fatal(err)
	}
	if len(st.Agreements) != 1 || st.Agreements[0].Home != "" || st.Agreements[0].HomeQuote != "" || !strings.Contains(body, "主动找他聊") {
		t.Fatalf("invented home was stored or agreement lost: %+v %s", st.Agreements, body)
	}
}

func TestOmittedAgreementRepairListsMissingAgreement(t *testing.T) {
	kept := Agreement{ID: "comfort", Text: "他累时直接陪聊", State: "active", Sources: []Quote{{Turn: 1, Speaker: "jg", Text: "可以开始"}}}
	m := Material{From: 1, To: 1}
	m.Previous.Agreements = []Agreement{kept}
	got := CandidateRepairInstruction(Candidate{}, m, "agreement_omitted:comfort")
	if !strings.Contains(got, "omitted_agreements") || !strings.Contains(got, "他累时直接陪聊") {
		t.Fatal("omitted agreement repair lacks the missing agreement", got)
	}
}

// Live failures 2026-11-13 (#67, #72, #74, #76): the model dropped an existing
// agreement. The program carries it unchanged instead of rejecting the draft.
func TestOmittedPreviousAgreementIsCarriedUnchanged(t *testing.T) {
	source := Quote{Turn: 1, Speaker: "jg", Text: "可以开始"}
	homed := Agreement{ID: "homed", Text: "正本里有的约定", State: "active", Sources: []Quote{source}, Home: "USER.md", HomeQuote: "喜欢简短回复"}
	stale := Agreement{ID: "stale", Text: "出处已失效的约定", State: "active", Sources: []Quote{source}, Home: "USER.md", HomeQuote: "已经删掉的句子"}
	m := Material{From: 2, To: 2, Past: []Turn{{Number: 2, Kind: "human", User: "继续", Complete: true}}, Homes: map[string]string{"USER.md": "喜欢简短回复"}}
	m.Previous.Agreements = []Agreement{homed, stale}
	m.Previous.Covered = 1
	c := Candidate{From: 2, To: 2, Summary: "接上一次。"}
	body, err := Validate(c, m, "stop")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(body, "正本里有的约定") {
		t.Fatal("agreement with a verified home was rendered again", body)
	}
	if !strings.Contains(body, "出处已失效的约定") {
		t.Fatal("agreement whose home is stale was not rendered", body)
	}
}

func TestRebaseAcceptsOnlyRecentTurnChanges(t *testing.T) {
	s, scope := testStore(t)
	seedTurn(t, s, scope)
	st, _ := s.State(scope.Session)
	turns, _ := s.Turns(scope.Session)
	var maxNumber int64
	for _, turn := range turns {
		if turn.Number > maxNumber {
			maxNumber = turn.Number
		}
	}
	m := Material{Previous: st, Past: turns, From: 1, To: 1, MaxNumber: maxNumber}
	if ok, err := s.Rebase(scope.Session, &m); err != nil || !ok {
		t.Fatal("unchanged snapshot was not current", ok, err)
	}
	if _, err := s.DB.Exec("UPDATE context_sessions SET revision=revision+1 WHERE session=?", scope.Session); err != nil {
		t.Fatal(err)
	}
	if ok, err := s.Rebase(scope.Session, &m); err != nil || !ok || m.Previous.Revision != st.Revision+1 {
		t.Fatal("revision change outside the past range was not rebased", ok, err, m.Previous.Revision)
	}
	if _, err := s.DB.Exec("UPDATE context_turns SET user_text=user_text||'改' WHERE session=? AND number=1", scope.Session); err != nil {
		t.Fatal(err)
	}
	if _, err := s.DB.Exec("UPDATE context_sessions SET revision=revision+1 WHERE session=?", scope.Session); err != nil {
		t.Fatal(err)
	}
	if ok, err := s.Rebase(scope.Session, &m); err != nil || ok {
		t.Fatal("changed past turn was rebased", ok, err)
	}
	m.MaxNumber++
	if ok, _ := s.Rebase(scope.Session, &m); ok {
		t.Fatal("new turn count was rebased")
	}
}
