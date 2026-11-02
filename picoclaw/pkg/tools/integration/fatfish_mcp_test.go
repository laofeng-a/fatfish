package integrationtools

import (
 "context"
 "testing"
 "github.com/modelcontextprotocol/go-sdk/mcp"
 toolshared "github.com/sipeed/picoclaw/pkg/tools/shared"
)

func TestFatfishMCPAuthorityComesFromHarness(t *testing.T) {
 manager:=&MockMCPManager{callToolFunc:func(ctx context.Context,server,tool string,args map[string]any)(*mcp.CallToolResult,error){
  trusted:=args["_context"].(map[string]any)
  if trusted["chat_id"]!="-99" || trusted["message_id"]!="42" || trusted["channel"]!="telegram" {t.Fatalf("model-supplied authority was trusted: %#v",trusted)}
  return &mcp.CallToolResult{Content:[]mcp.Content{&mcp.TextContent{Text:`{"status":"sent"}`}}},nil
 }}
 wrapper:=NewMCPTool(manager,"reader",&mcp.Tool{Name:"emote_send"})
 ctx:=toolshared.WithToolInboundContext(context.Background(),"telegram","-99","42","")
 result:=wrapper.Execute(ctx,map[string]any{"id":"whale-001","_context":map[string]any{"chat_id":"123","message_id":"fake"}})
 if result.IsError || !result.ResponseHandled {t.Fatal("successful sticker tool must complete the reply without a second send")}
}
