package agent

// Only cancel waiting for reads. A send/publication may already have committed.
func fatfishInterruptibleRead(name string) bool {
	switch name {
	case "load_image", "mcp_deepseek_search", "mcp_deepseek_fetch", "mcp_reader_x_feed", "mcp_reader_x_post_read", "mcp_reader_bilibili_video", "mcp_reader_bilibili_comments", "mcp_reader_bilibili_discover", "mcp_reader_image_read", "mcp_reader_recall", "mcp_reader_get", "mcp_reader_list_recent":
		return true
	}
	return false
}
