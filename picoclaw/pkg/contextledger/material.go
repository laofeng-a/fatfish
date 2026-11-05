package contextledger

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

// ModelDialogue uses the same source tuple as Candidate.Quotes, so the model
// can copy a speaker/turn/text together instead of joining separate fields.
type ModelDialogue struct {
	Turn       int64            `json:"turn"`
	Time       string           `json:"time_utc8"`
	Dialogue   []Quote          `json:"dialogue"`
	References []ModelReference `json:"read_sources,omitempty"`
}

type ModelReference struct {
	Tool    string `json:"tool"`
	Content string `json:"content"`
}

// Execution details and reasoning remain in the ledger. Only source-reading
// results accompany dialogue into the conversational summary, labelled as
// references rather than newly spoken words or quote evidence.
func readSources(turn Turn) []ModelReference {
	allowed := map[string]bool{"history_find": true, "history_expand": true, "memory_note_read": true, "memory_find": true, "conversation_bundle": true, "diary_prepare": true}
	calls := map[string]string{}
	var out []ModelReference
	for _, msg := range turn.Messages {
		for _, call := range msg.ToolCalls {
			if call.Function != nil {
				name := strings.TrimPrefix(call.Function.Name, "mcp_reader_")
				var args map[string]any
				_ = json.Unmarshal([]byte(call.Function.Arguments), &args)
				if name == "turn_detail" && args["part"] == "dialogue" {
					name = "history_expand"
				}
				if name == "read_file" {
					path, _ := args["path"].(string)
					if regexp.MustCompile(`(?:^|/)(?:memory|notes)/\d{6}/\d{8}\.md$`).MatchString(strings.ReplaceAll(path, `\`, "/")) {
						name = "memory_note_read"
					}
				}
				calls[call.ID] = name
			}
		}
		name := calls[msg.ToolCallID]
		if msg.Role == "tool" && allowed[name] {
			content := []rune(msg.Content)
			if len(content) > 12000 {
				content = content[:12000]
			}
			out = append(out, ModelReference{Tool: name, Content: string(content)})
		}
	}
	return out
}

type ModelMaterial struct {
	Previous   *State          `json:"previous_accepted"`
	Legacy     string          `json:"legacy_unverified_reference,omitempty"`
	Agreements []Agreement     `json:"confirmed_agreements"`
	Quotes     []Quote         `json:"verified_previous_quotes"`
	Past       []ModelDialogue `json:"past_dialogue"`
	Recent     []ModelDialogue `json:"recent_corrections_only"`
	From       int64           `json:"allowed_from"`
	To         int64           `json:"allowed_to"`
	Template   Candidate       `json:"candidate_template"`
	Rejection  string          `json:"previous_rejection_reason,omitempty"`
	Contract   string          `json:"output_contract"`
}

func modelDialogue(turns []Turn, thoughts bool) []ModelDialogue {
	var out []ModelDialogue
	for _, turn := range turns {
		speaker := "jg"
		if turn.Kind == "member" {
			speaker = "用户"
		}
		t := ModelDialogue{Turn: turn.Number, Dialogue: []Quote{{Turn: turn.Number, Speaker: speaker, Text: turn.User}}}
		if turn.At > 0 {
			t.Time = time.Unix(0, int64(turn.At*1e9)).In(time.FixedZone("UTC+8", 8*3600)).Format(time.RFC3339)
		}
		for _, d := range turn.Deliveries {
			if d.State == "sent" {
				t.Dialogue = append(t.Dialogue, Quote{Turn: turn.Number, Speaker: "大肥鱼", Text: d.Text})
			}
		}
		if thoughts {
			t.References = readSources(turn)
		}
		out = append(out, t)
	}
	return out
}

func ModelMaterials(m Material) ModelMaterial {
	wire := ModelMaterial{Agreements: m.Previous.Agreements, Quotes: m.Previous.Quotes, Past: modelDialogue(m.Past, true), Recent: modelDialogue(m.Recent, false), From: m.From, To: m.To, Rejection: m.Rejection, Template: Candidate{From: m.From, To: m.To, Quotes: []Quote{}, Agreements: m.Previous.Agreements}}
	wire.Contract = fmt.Sprintf("现在请生成候选JSON。本次新增过往范围为#%d至#%d；近期材料仅校正约定。quotes选少量原话，summary用{{q1}}等占位符插入原话，程序会加入实际说话人与逐字原文；其余内容用概括叙述。渲染后的正文与约定合计最多1800字。", m.From, m.To)
	if m.Previous.Version > 0 {
		previous := m.Previous
		wire.Previous = &previous
	} else {
		wire.Legacy = m.Previous.Body
	}
	return wire
}

func ModelMaterialJSON(m Material) ([]byte, error) { return json.Marshal(ModelMaterials(m)) }

// A legacy paragraph is not proof of its own quotes. Recover only fragments
// which match an actual received or confirmed sent utterance in the indexed
// prefix, with its original fixed turn number and speaker.
func LegacyQuoteEvidence(body string, turns []Turn, covered int64) []Quote {
	var out []Quote
	seen := map[Quote]bool{}
	for _, match := range proseQuotes.FindAllStringSubmatch(body, -1) {
		text := match[1]
		if utf8.RuneCountInString(text) < 4 {
			continue
		}
		matchedSpeakers := map[string]bool{}
		for _, turn := range turns {
			if turn.Number <= 0 || turn.Number > covered {
				continue
			}
			var candidates []Quote
			if turn.Kind == "human" || turn.Kind == "member" {
				speaker := "jg"
				if turn.Kind == "member" {
					speaker = "用户"
				}
				if strings.Contains(turn.User, text) {
					candidates = append(candidates, Quote{Turn: turn.Number, Speaker: speaker, Text: text})
				}
			}
			for _, d := range turn.Deliveries {
				if d.State == "sent" && strings.Contains(d.Text, text) {
					candidates = append(candidates, Quote{Turn: turn.Number, Speaker: "大肥鱼", Text: text})
				}
			}
			for _, q := range candidates {
				if !seen[q] && !matchedSpeakers[q.Speaker] {
					out = append(out, q)
					seen[q] = true
					matchedSpeakers[q.Speaker] = true
				}
			}
		}
	}
	return out
}
