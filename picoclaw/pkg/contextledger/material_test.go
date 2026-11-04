package contextledger

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestModelMaterialsSeparateUnverifiedLegacyAndRecentCorrections(t *testing.T) {
	old := Agreement{ID: "decision", Text: "确认过的决定", State: "active", Sources: []Quote{{Turn: 3, Speaker: "jg", Text: "这件事按这个做"}}}
	m := Material{Previous: State{Covered: 5, Body: "旧策略的未经核对摘要", Agreements: []Agreement{old}}, From: 6, To: 6, Past: []Turn{{Number: 6, Kind: "human", User: "过去的用户原话", Deliveries: []Delivery{{State: "sent", Text: "过去的真实回复"}, {State: "unknown", Text: "未确认投递"}}, Reasoning: []string{"过去的实际推断"}, ToolBrief: "工具已完成"}}, Recent: []Turn{{Number: 7, Kind: "human", User: "这件事已经完成", Deliveries: []Delivery{{State: "sent", Text: "知道了"}}, Reasoning: []string{"近期整段思考不属于校正原话"}, ToolBrief: "近期冗长的工具结论"}}}
	wire := ModelMaterials(m)
	if wire.Previous != nil || wire.Legacy != m.Previous.Body || len(wire.Agreements) != 1 || wire.Template.From != 6 || wire.Template.To != 6 {
		t.Fatalf("legacy trusted or known data lost: %+v", wire)
	}
	if len(wire.Past[0].Dialogue) != 2 || wire.Past[0].Dialogue[1].Speaker != "大肥鱼" || wire.Past[0].Dialogue[1].Turn != 6 || len(wire.Past[0].Reasoning) != 1 {
		t.Fatal("source tuple or historical reasoning lost")
	}
	raw, err := ModelMaterialJSON(m)
	if err != nil {
		t.Fatal(err)
	}
	for _, unexpected := range []string{"未确认投递", "近期整段思考不属于校正原话", "近期冗长的工具结论"} {
		if strings.Contains(string(raw), unexpected) {
			t.Fatalf("unexpected model material %q", unexpected)
		}
	}
	var object map[string]any
	if err = json.Unmarshal(raw, &object); err != nil {
		t.Fatal(err)
	}
	m.Previous.Version = 1
	if ModelMaterials(m).Previous == nil {
		t.Fatal("accepted version was hidden")
	}
}

func TestLegacyQuoteEvidenceUsesActualSpeakerAndFixedNumber(t *testing.T) {
	turns := []Turn{{Number: 3, Kind: "human", User: "原来用户确实说过的话", Deliveries: []Delivery{{State: "sent", Text: "这是已经送达的回复"}, {State: "unknown", Text: "这是未确认的长句"}}}, {Number: 4, Kind: "internal", User: "系统写入的一段长句"}, {Number: 7, Kind: "human", User: "这是近期原话内容"}}
	quotes := LegacyQuoteEvidence("旧文写“用户确实说过的话”、“已经送达的回复”、“未确认的长句”、“系统写入的一段长句”、“近期原话内容”、“凭空出现的一段话”。", turns, 5)
	if len(quotes) != 2 || quotes[0] != (Quote{Turn: 3, Speaker: "jg", Text: "用户确实说过的话"}) || quotes[1] != (Quote{Turn: 3, Speaker: "大肥鱼", Text: "已经送达的回复"}) {
		t.Fatalf("invented source or attribution: %+v", quotes)
	}
}

func TestLegacyBodyCannotVerifyItsOwnUnmatchedQuotes(t *testing.T) {
	m := Material{Previous: State{Covered: 5, Body: "未经核对的旧文声称“虚构原话内容”。"}, From: 6, To: 6, Past: []Turn{{Number: 6, Kind: "human", User: "这是真实新增原话", Complete: true}}}
	c := Candidate{From: 6, To: 6, Summary: "沿用“虚构原话内容”这一说法。"}
	if _, err := Validate(c, m, "stop"); err == nil {
		t.Fatal("unverified old summary proved its own quote")
	}
	m.Previous.Quotes = []Quote{{Turn: 3, Speaker: "jg", Text: "虚构原话内容"}}
	if _, err := Validate(c, m, "stop"); err != nil {
		t.Fatal("a separately verified old source was rejected", err)
	}
}

func TestQuoteReferencesRenderExactSpeakerAndOriginalPunctuation(t *testing.T) {
	m := Material{From: 1, To: 1, Past: []Turn{{Number: 1, Kind: "human", User: "原话一。\n\n原话二！", Complete: true}}}
	c := Candidate{From: 1, To: 1, Summary: "这次交流中，{{q1}}，随后接着聊。", Quotes: []Quote{{Turn: 1, Speaker: "jg", Text: "原话一。\n\n原话二！"}}}
	body, err := Validate(c, m, "stop")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(body, "jg：“原话一。\n\n原话二！”") || strings.Contains(body, "{{q") {
		t.Fatal("original text/speaker changed", body)
	}
	c.Summary = "引用{{q2}}"
	if _, err = Validate(c, m, "stop"); err == nil {
		t.Fatal("missing reference accepted")
	}
	c.Summary = "引用{{q1}}"
	c.Quotes[0].Text = "编造的原话"
	if _, err = Validate(c, m, "stop"); err == nil {
		t.Fatal("rendering bypassed source validation")
	}
}
