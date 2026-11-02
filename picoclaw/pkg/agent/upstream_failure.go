package agent

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/sipeed/picoclaw/pkg/fatfish"
	"github.com/sipeed/picoclaw/pkg/fileutil"
	"github.com/sipeed/picoclaw/pkg/logger"
	"github.com/sipeed/picoclaw/pkg/providers/common"
	toolshared "github.com/sipeed/picoclaw/pkg/tools/shared"
)

var upstreamStatusPattern = regexp.MustCompile("(?i)\\b(?:status(?:\\s+code)?|http(?:\\s+error)?)\\s*[:=]?\\s*([1-5][0-9]{2})\\b")
var upstreamSecretPattern = regexp.MustCompile("(?i)sk-[a-z0-9_-]+|bearer\\s+[a-z0-9_.:-]+")

func upstreamFailureDetails(err error) (status int, code, body string) {
	if err == nil {
		return
	}
	var httpErr *common.HTTPError
	if errors.As(err, &httpErr) {
		status, code, body = httpErr.StatusCode, httpErr.UpstreamCode, httpErr.BodyPreview
		if httpErr.UpstreamStatus != 0 {
			status = httpErr.UpstreamStatus
		}
		if httpErr.UpstreamBody != "" {
			body = httpErr.UpstreamBody
		}
	} else {
		body = err.Error()
		if match := upstreamStatusPattern.FindStringSubmatch(body); len(match) == 2 {
			_, _ = fmt.Sscanf(match[1], "%d", &status)
		}
	}
	body = upstreamSecretPattern.ReplaceAllString(body, "[redacted]")
	if text := []rune(body); len(text) > 1024 {
		body = string(text[:1024])
	}
	return
}

func permanentForbidden(code, body string) bool {
	if start := strings.Index(body, "{"); start >= 0 {
		var envelope struct {
			Error struct {
				Code    string
				Message string
			}
		}
		if json.NewDecoder(strings.NewReader(body[start:])).Decode(&envelope) == nil {
			if code == "" {
				code = envelope.Error.Code
			}
			body = envelope.Error.Message
		}
	}
	if strings.Contains(strings.ToLower(code), "invalid_request") {
		return true
	}
	text := strings.ToLower(code + " " + body)
	for _, marker := range []string{"content_filter", "content_policy", "data_inspection_failed", "safety_violation",
		"inappropriate_content", "content_blocked", "content_moderation", "invalid_parameter", "invalidparameter",
		"invalid_argument", "bad_request", "invalid parameter", "invalid argument", "内容违规", "内容审核未通过", "参数错误"} {
		if strings.Contains(text, marker) {
			return true
		}
	}
	return false
}

// Notices are operational feedback, not an assistant answer or delivery receipt.
// One failed incident is shared across turns until a successful model call;
// a single turn also never emits a second notice after an intermediate recovery.
func (al *AgentLoop) notifyUpstreamFailure(ctx context.Context, ts *turnState, exec *turnExecution, retrying, contextLimit bool) {
	if !fatfish.Enabled() || ts.channel != "telegram" || ts.opts.NoHistory ||
		internalAutomaticFinal(ctx, ts.sessionKey, toolshared.ReplyDeliveryMessageID(ctx)) || exec.upstreamNoticeSent {
		return
	}
	if _, loaded := al.upstreamNotices.LoadOrStore(ts.sessionKey, true); loaded {
		return
	}
	text := "我这头被服务挡了一下，这次暂时没接上。"
	if retrying {
		text = "我这头被挡了一下，正在重试。"
	}
	if contextLimit && retrying {
		text = "这轮内容有点长，我整理一下再试。"
	}
	notice := outboundMessageForTurnWithOptions(ts, text, outboundTurnMessageOptions{
		kind: "runtime_status", raw: map[string]string{"delivery_origin": "runtime_notice"},
	})
	sendCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 3*time.Second)
	defer cancel()
	if err := al.bus.PublishOutbound(sendCtx, notice); err != nil {
		al.upstreamNotices.Delete(ts.sessionKey)
		logger.WarnCF("agent", "Upstream notice could not be queued", map[string]any{"error": err.Error()})
		return
	}
	exec.upstreamNoticeSent = true
}

// Separate per-turn records retain diagnostics without changing chat history,
// task scheduling, or forwarding the provider's raw error to the user.
func recordUpstreamFailure(ts *turnState, err error, attempts, iteration int) string {
	if !fatfish.Enabled() || errors.Is(err, context.Canceled) {
		return ""
	}
	status, code, body := upstreamFailureDetails(err)
	record := map[string]any{"turn_id": ts.turnID, "session_key": ts.sessionKey, "chat_id": ts.chatID,
		"message_id": ts.opts.Dispatch.MessageID(), "at": time.Now().Format(time.RFC3339Nano),
		"status": status, "upstream_code": code, "body_summary": body, "attempts": attempts,
		"iteration": iteration, "terminal": true}
	raw, marshalErr := json.MarshalIndent(record, "", "  ")
	dir := filepath.Join(ts.workspace, "state", "upstream-failures")
	if marshalErr == nil {
		marshalErr = os.MkdirAll(dir, 0o700)
	}
	path := filepath.Join(dir, fmt.Sprintf("%x.json", sha256.Sum256([]byte(ts.turnID))))
	if marshalErr == nil {
		marshalErr = fileutil.WriteFileAtomic(path, raw, 0o600)
	}
	if marshalErr != nil {
		logger.WarnCF("agent", "Failure record could not be saved", map[string]any{"error": marshalErr.Error()})
		return ""
	}
	return path
}
