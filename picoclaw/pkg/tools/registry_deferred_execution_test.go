package tools

import (
	"context"
	"testing"
)

func TestDeferredToolExecutesByKnownNameWithoutLoadingSchemas(t *testing.T) {
	for _, name := range []string{"mcp_reader_memory_note_read", "mcp_reader_memory_note_write"} {
		t.Run(name, func(t *testing.T) {
			r := NewToolRegistry()
			r.RegisterHidden(newMockTool(name, "versioned personal notes"))
			if _, ok := r.Get(name); ok || len(r.ToProviderDefs()) != 0 {
				t.Fatal("cold deferred schema was exposed")
			}
			result := r.Execute(context.Background(), name, map[string]any{})
			if result.IsError || result.ForLLM != "ok" {
				t.Fatalf("registered deferred tool failed: %+v", result)
			}
			if len(r.ToProviderDefs()) != 0 {
				t.Fatal("direct execution unexpectedly loaded schemas")
			}
		})
	}
}

func TestDeferredToolExecutesAfterSchemaTTLExpires(t *testing.T) {
	r := NewToolRegistry()
	tool := &mockContextAwareTool{mockRegistryTool: *newMockTool("mcp_reader_memory_note_read", "read notes")}
	r.RegisterHidden(tool)
	r.PromoteTools([]string{tool.Name()}, 2)
	r.TickTTL()
	r.TickTTL()
	if _, ok := r.Get(tool.Name()); ok {
		t.Fatal("expired schema stayed visible")
	}
	result := r.ExecuteWithContext(context.Background(), tool.Name(), map[string]any{}, "telegram", "123", nil)
	if result.IsError || tool.lastCtx == nil || ToolChatID(tool.lastCtx) != "123" {
		t.Fatal("expired deferred call lost execution or conversation context")
	}
}

func TestDeferredExecutionPreservesAllowlistAndSchemaValidation(t *testing.T) {
	r := NewToolRegistry()
	tool := &mockContextAwareTool{mockRegistryTool: *newMockTool("mcp_reader_memory_note_write", "write notes")}
	tool.params = map[string]any{"type": "object", "properties": map[string]any{"path": map[string]any{"type": "string"}}, "required": []string{"path"}}
	r.RegisterHidden(tool)
	if result := r.Execute(context.Background(), tool.Name(), map[string]any{}); !result.IsError || tool.lastCtx != nil {
		t.Fatal("deferred execution skipped argument validation")
	}
	r.SetAllowlist([]string{"read_file"})
	if result := r.Execute(context.Background(), tool.Name(), map[string]any{"path": "SOUL.md"}); !result.IsError || tool.lastCtx != nil {
		t.Fatal("deferred execution bypassed agent allowlist")
	}
	if result := r.Execute(context.Background(), "mcp_reader_nonexistent", map[string]any{}); !result.IsError {
		t.Fatal("unknown tool name unexpectedly succeeded")
	}
}
