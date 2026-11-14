package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/sipeed/picoclaw/pkg/contextledger"
	"github.com/sipeed/picoclaw/pkg/providers"
)

func TestFatfishSummaryRepairsRecordedFailureKinds(t *testing.T) {
	for _, kind := range []string{"quote", "home", "json", "truncated", "length", "quote_still_invalid", "quote_during_delivery"} {
		t.Run(kind, func(t *testing.T) {
			m, agent := reformFixture(t)
			m.keepRecent = 1
			key := "repair-errors"
			for i := 1; i <= 3; i++ {
				agent.Sessions.AddFullMessage(key, providers.Message{Role: "user", Content: "第一句。\n\n第二句。", SourceKind: "human", SourceChatID: "123", SourceChannel: "telegram", SourceMessageID: fmt.Sprint(i)})
			}
			if _, err := m.sync(agent, key, true); err != nil {
				t.Fatal(err)
			}
			if _, err := m.store.DB.Exec("UPDATE context_turns SET kind='human' WHERE session=?", key); err != nil {
				t.Fatal(err)
			}
			profile := filepath.Join(agent.Workspace, "profile")
			if err := os.MkdirAll(profile, 0o700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(profile, "USER.md"), []byte("认真问题按需要讲清楚。"), 0o600); err != nil {
				t.Fatal(err)
			}
			before, _ := m.store.State(key)
			rawCount := len(agent.Sessions.GetHistory(key))
			calls := 0
			agent.Provider = &reformProvider{fn: func(messages []providers.Message) (*providers.LLMResponse, error) {
				calls++
				var material contextledger.ModelMaterial
				if err := json.Unmarshal([]byte(messages[1].Content), &material); err != nil {
					return nil, err
				}
				candidate := contextledger.Candidate{From: material.From, To: material.To, Summary: "两人把当时的感受说清楚，接着聊了下去。"}
				if calls == 2 {
					if len(messages) != 4 || !strings.Contains(messages[3].Content, "上一候选已记录为拒绝") {
						t.Fatal("specific correction missing")
					}
					if (kind == "quote" || kind == "quote_still_invalid" || kind == "quote_during_delivery") && !strings.Contains(messages[3].Content, `第一句。\n\n第二句。`) {
						t.Fatal("repair omitted the actual source punctuation")
					}
				}
				finish := "stop"
				if calls == 1 && kind == "quote_during_delivery" {
					// Live 2026-11-13: the triggering reply's delivery is recorded
					// on a recent turn while the summary is being generated.
					if _, err := m.store.DB.Exec("UPDATE context_sessions SET revision=revision+1 WHERE session=?", key); err != nil {
						t.Fatal(err)
					}
				}
				if calls == 1 || kind == "quote_still_invalid" {
					switch kind {
					case "quote", "quote_still_invalid", "quote_during_delivery":
						candidate.Quotes = []contextledger.Quote{{Turn: material.From, Speaker: "jg", Text: "第一句。……第二句。"}}
					case "home":
						candidate.Agreements = []contextledger.Agreement{{ID: "agreement", Text: "双方确认过的表达", State: "active", Sources: []contextledger.Quote{{Turn: material.From, Speaker: "jg", Text: "第一句。"}}, Home: "USER.md", HomeQuote: "正本里没有的句子"}}
					case "json":
						return &providers.LLMResponse{Content: `{"summary":`, FinishReason: "stop"}, nil
					case "truncated":
						return &providers.LLMResponse{Content: `{"summary":"尚未结束`, FinishReason: "truncated"}, nil
					case "length":
						candidate.Summary = strings.Repeat("当时两人接着说。", 300)
					}
				}
				if kind == "home" && calls == 2 {
					if material.Homes["USER.md"] != "认真问题按需要讲清楚。" {
						t.Fatal("canonical source missing from generation input")
					}
				}
				raw, _ := json.Marshal(candidate)
				return &providers.LLMResponse{Content: string(raw), FinishReason: finish}, nil
			}}
			err := m.generate(context.Background(), key)
			// An unverifiable home is cleared instead of rejected, so it needs no correction.
			want := 2
			if kind == "home" {
				want = 1
			}
			if calls != want {
				t.Fatalf("expected one correction, got %d calls: %v", calls, err)
			}
			after, _ := m.store.State(key)
			if kind == "quote_still_invalid" {
				if err == nil || after.Version != before.Version || after.Covered != before.Covered || len(agent.Sessions.GetHistory(key)) != rawCount {
					t.Fatal("rejected correction changed active state or raw history")
				}
			} else if err != nil || after.Version == before.Version || after.Covered <= before.Covered {
				t.Fatalf("valid correction did not activate: %v, %+v", err, after)
			}
			var drafts int
			if err := m.store.DB.QueryRow("SELECT count(*) FROM context_summaries WHERE session=?", key).Scan(&drafts); err != nil || drafts != want {
				t.Fatal("draft history was lost", drafts, err)
			}
		})
	}
}

func TestFatfishSummaryAlwaysPreservesConfiguredRecentTurns(t *testing.T) {
	m, agent := reformFixture(t)
	for i := 1; i <= 7; i++ {
		agent.Sessions.AddFullMessage("recent", providers.Message{Role: "user", Content: fmt.Sprint(i), SourceKind: "human", SourceChatID: "123", SourceChannel: "telegram", SourceMessageID: fmt.Sprint(i)})
		if _, err := m.sync(agent, "recent", true); err != nil {
			t.Fatal(err)
		}
		if _, err := m.store.DB.Exec("UPDATE context_turns SET kind='human' WHERE session='recent'"); err != nil {
			t.Fatal(err)
		}
		material, err := m.material("recent")
		if i <= 6 {
			if err == nil || err.Error() != "not_enough_complete_turns" {
				t.Fatal("compressed within the protected six turns", i, err)
			}
		} else if err != nil || len(material.Recent) != 6 || len(material.Past) != 1 {
			t.Fatal("recent buffer changed", material, err)
		}
	}
}
