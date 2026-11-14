package contextledger

import (
	"encoding/json"
	"github.com/sipeed/picoclaw/pkg/providers"
	"strings"
	"testing"
)

func TestSummaryMaterialKeepsReadSourcesAndLeavesExecutionInRawLedger(t *testing.T) {
	turn := Turn{Number: 1, Kind: "human", User: "争执后我们说开了", Reasoning: []string{"执行计划与内部推断"}, ToolBrief: "重启服务成功", Messages: []providers.Message{
		{Role: "assistant", ToolCalls: []providers.ToolCall{{ID: "read", Function: &providers.FunctionCall{Name: "mcp_reader_memory_note_read", Arguments: `{"path":"202611/20261103.md"}`}}, {ID: "exec", Function: &providers.FunctionCall{Name: "exec", Arguments: `{}`}}}},
		{Role: "tool", ToolCallID: "read", Content: `{"path":"202611/20261103.md","content":"相处细节：他笑着接住了这个梗"}`},
		{Role: "tool", ToolCallID: "exec", Content: "部署日志与测试统计"},
	}}
	raw, _ := ModelMaterialJSON(Material{Past: []Turn{turn}, From: 1, To: 1})
	if !strings.Contains(string(raw), "相处细节") || strings.Contains(string(raw), "部署日志") || strings.Contains(string(raw), "内部推断") {
		t.Fatal(string(raw))
	}
	if len(turn.Messages) != 3 || len(turn.Reasoning) != 1 {
		t.Fatal("raw record mutated")
	}
}

func TestModelMaterialsSeparateUnverifiedLegacyAndRecentCorrections(t *testing.T) {
	old := Agreement{ID: "decision", Text: "确认过的决定", State: "active", Sources: []Quote{{Turn: 3, Speaker: "jg", Text: "这件事按这个做"}}}
	m := Material{Previous: State{Covered: 5, Body: "旧策略的未经核对摘要", Agreements: []Agreement{old}}, From: 6, To: 6, Past: []Turn{{Number: 6, Kind: "human", User: "过去的用户原话", Deliveries: []Delivery{{State: "sent", Text: "过去的真实回复"}, {State: "unknown", Text: "未确认投递"}}, Reasoning: []string{"过去的实际推断"}, ToolBrief: "工具已完成"}}, Recent: []Turn{{Number: 7, Kind: "human", User: "这件事已经完成", Deliveries: []Delivery{{State: "sent", Text: "知道了"}}, Reasoning: []string{"近期整段思考不属于校正原话"}, ToolBrief: "近期冗长的工具结论"}}}
	wire := ModelMaterials(m)
	if wire.Previous != nil || wire.Legacy != m.Previous.Body || len(wire.Agreements) != 1 || wire.Template.From != 6 || wire.Template.To != 6 {
		t.Fatalf("legacy trusted or known data lost: %+v", wire)
	}
	if len(wire.Past[0].Dialogue) != 2 || wire.Past[0].Dialogue[1].Speaker != "大肥鱼" || wire.Past[0].Dialogue[1].Turn != 6 {
		t.Fatal("source tuple lost")
	}
	raw, err := ModelMaterialJSON(m)
	if err != nil {
		t.Fatal(err)
	}
	for _, unexpected := range []string{"过去的实际推断", "工具已完成", "recorded_thoughts", "tool_result", "未确认投递", "近期整段思考不属于校正原话", "近期冗长的工具结论"} {
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
	body, err := Validate(c, m, "stop")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(body, "“虚构原话内容”") || !strings.Contains(body, "虚构原话内容") {
		t.Fatal("unsourced quotation was kept or its words were dropped", body)
	}
	m.Previous.Quotes = []Quote{{Turn: 3, Speaker: "jg", Text: "虚构原话内容"}}
	body, err = Validate(c, m, "stop")
	if err != nil {
		t.Fatal("a separately verified old source was rejected", err)
	}
	if !strings.Contains(body, "“虚构原话内容”") {
		t.Fatal("sourced quotation lost its marks", body)
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
