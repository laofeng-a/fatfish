package fstools

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/sipeed/picoclaw/pkg/config"
	"github.com/sipeed/picoclaw/pkg/fatfish"
	"github.com/sipeed/picoclaw/pkg/media"
)

// LoadImageTool loads a local image file into the MediaStore and returns a
// media:// reference. The agent loop's resolveMediaRefs will then base64-encode
// it and attach it as an image_url part in the next LLM request, enabling
// vision on local files — the same pipeline used when a user sends an image
// through a chat channel.
//
// This is intentionally different from SendFileTool:
//   - SendFileTool  → MediaResult + WithResponseHandled() → sends file to user, ends turn
//   - LoadImageTool → plain ToolResult with media:// in ForLLM  → LLM sees the image next turn
type LoadImageTool struct {
	workspace   string
	restrict    bool
	maxFileSize int
	mediaStore  media.MediaStore
	allowPaths  []*regexp.Regexp

	defaultChannel string
	defaultChatID  string
}

func NewLoadImageTool(
	workspace string,
	restrict bool,
	maxFileSize int,
	store media.MediaStore,
	allowPaths ...[]*regexp.Regexp,
) *LoadImageTool {
	if maxFileSize <= 0 {
		maxFileSize = config.DefaultMaxMediaSize
	}
	var patterns []*regexp.Regexp
	if len(allowPaths) > 0 {
		patterns = allowPaths[0]
	}
	return &LoadImageTool{
		workspace:   workspace,
		restrict:    restrict,
		maxFileSize: maxFileSize,
		mediaStore:  store,
		allowPaths:  patterns,
	}
}

func (t *LoadImageTool) Name() string { return "load_image" }

func (t *LoadImageTool) Description() string {
	return "Load a local image file so you can analyze its contents with vision. " +
		"Supported formats: JPEG, PNG, GIF, WebP, BMP. " +
		"Use the image with the conversation context; a natural reaction is fine and a full description is not required."
}

func (t *LoadImageTool) Parameters() map[string]any {
	return map[string]any{
		"type": "object",
		"properties": map[string]any{
			"path": map[string]any{
				"type":        "string",
				"description": "Path to the local image file. Relative paths are resolved from workspace.",
			},
		},
		"required": []string{"path"},
	}
}

func (t *LoadImageTool) SetContext(channel, chatID string) {
	t.defaultChannel = channel
	t.defaultChatID = chatID
}

func (t *LoadImageTool) SetMediaStore(store media.MediaStore) {
	t.mediaStore = store
}

func (t *LoadImageTool) Execute(ctx context.Context, args map[string]any) *ToolResult {
	path, _ := args["path"].(string)
	if strings.TrimSpace(path) == "" {
		return ErrorResult("path is required")
	}

	// Prefer context-injected channel/chatID (set by ExecuteWithContext), fall back to SetContext values.
	channel := ToolChannel(ctx)
	if channel == "" {
		channel = t.defaultChannel
	}
	chatID := ToolChatID(ctx)
	if chatID == "" {
		chatID = t.defaultChatID
	}
	if channel == "" || chatID == "" {
		return ErrorResult("no target channel/chat available")
	}

	if t.mediaStore == nil {
		return ErrorResult("media store not configured")
	}

	resolved, err := validatePathWithAllowPaths(path, t.workspace, t.restrict, t.allowPaths)
	if err != nil {
		return ErrorResult(fmt.Sprintf("invalid path: %v", err))
	}

	info, err := os.Stat(resolved)
	if err != nil {
		return ErrorResult(fmt.Sprintf("file not found: %v", err))
	}
	if info.IsDir() {
		return ErrorResult("path is a directory, expected an image file")
	}
	if info.Size() > int64(t.maxFileSize) {
		return ErrorResult(fmt.Sprintf(
			"file too large: %d bytes (max %d bytes)", info.Size(), t.maxFileSize,
		))
	}
	if _, err := fatfish.Call(ctx, "image_allowed", map[string]any{"chat_id": chatID, "path": resolved}); err != nil {
		return ErrorResult("这张图片不属于当前会话或缓存已过期")
	}
	note := ""
	if python := os.Getenv("FATFISH_IMAGE_PYTHON"); python != "" {
		worker := os.Getenv("FATFISH_IMAGE_WORKER")
		dir := filepath.Join(t.workspace, "media", "normalized")
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return ErrorResult("image cache unavailable")
		}
		fatfish.PruneMedia(dir)
		output, err := os.CreateTemp(dir, "img-*.jpg")
		if err != nil {
			return ErrorResult("image cache unavailable")
		}
		outputPath := output.Name()
		output.Close()
		command := exec.CommandContext(ctx, python, worker, "--file", resolved, "--output", outputPath)
		raw, err := command.Output()
		if err != nil {
			os.Remove(outputPath)
			return ErrorResult("图片过大、格式异常或服务器资源暂时不足，没有读取到画面")
		}
		var metadata struct {
			Note string `json:"note"`
		}
		if json.Unmarshal(raw, &metadata) != nil {
			os.Remove(outputPath)
			return ErrorResult("image normalization metadata invalid")
		}
		resolved = outputPath
		note = metadata.Note
	}

	// Detect MIME type — reuse the helper already in send_file.go
	mediaType := detectMediaType(resolved)
	if !strings.HasPrefix(mediaType, "image/") {
		return ErrorResult(fmt.Sprintf(
			"file does not appear to be an image (detected type: %s)", mediaType,
		))
	}

	filename := filepath.Base(resolved)
	scope := fmt.Sprintf("tool:load_image:%s:%s", channel, chatID)

	ref, err := t.mediaStore.Store(resolved, media.MediaMeta{
		Filename:      filename,
		ContentType:   mediaType,
		Source:        "tool:load_image",
		CleanupPolicy: media.CleanupPolicyForgetOnly,
	}, scope)
	if err != nil {
		return ErrorResult(fmt.Sprintf("failed to register image in media store: %v", err))
	}

	// Build the tool result text. The media:// ref in Media will be picked
	// up by resolveMediaRefs in agent_media.go and base64-encoded for tool
	// result messages (role="tool"), so the LLM can see the image content.
	msg := fmt.Sprintf("Image loaded: %s\n%s\n[image: photo]", filename, note)

	return &ToolResult{
		ForLLM:  msg,
		ForUser: fmt.Sprintf("Loaded image: %s", filename),
		// Media refs inside ForLLM are resolved by resolveMediaRefs in the
		// agent loop before the next LLM call. Do NOT use MediaResult here —
		// that would send the file to the user channel instead.
		Media: []string{ref},
	}
}
