// Package fatfish bridges the bounded personal-tool policy to the pinned harness.
package fatfish

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

type Decision struct {
	SessionKey      string      `json:"session_key"`
	Managed         bool        `json:"managed"`
	Report          bool        `json:"report"`
	Active          bool        `json:"active"`
	ReceiptRecovery bool        `json:"receipt_recovery,omitempty"`
	TaskID          string      `json:"task_id"`
	Version         int         `json:"version"`
	Allowed         bool        `json:"allowed"`
	Handled         bool        `json:"handled"`
	Reply           string      `json:"reply"`
	Context         string      `json:"context"`
	WakeID          string      `json:"wake_id"`
	WakeKind        string      `json:"wake_kind"`
	Silent          bool        `json:"silent"`
	Ticket          string      `json:"ticket"`
	Skip            bool        `json:"skip"`
	RecordID        int         `json:"record_id"`
	Accepted        bool        `json:"accepted"`
	Duplicate       bool        `json:"duplicate"`
	Event           *CodexEvent `json:"event,omitempty"`
}

func Enabled() bool { return os.Getenv("FATFISH_RUNTIME_TOKEN_FILE") != "" }

func Call(ctx context.Context, action string, payload any) (Decision, error) {
	if !Enabled() {
		return Decision{Allowed: true}, nil
	}
	if strings.ContainsAny(action, "/.?") {
		return Decision{}, fmt.Errorf("invalid runtime action")
	}
	token, err := os.ReadFile(os.Getenv("FATFISH_RUNTIME_TOKEN_FILE"))
	if err != nil {
		return Decision{}, fmt.Errorf("runtime credential unavailable")
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return Decision{}, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://127.0.0.1:18792/runtime/"+action, bytes.NewReader(data))
	if err != nil {
		return Decision{}, err
	}
	request.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(token)))
	request.Header.Set("Content-Type", "application/json")
	client := http.Client{Timeout: 8 * time.Second}
	response, err := client.Do(request)
	if err != nil {
		return Decision{}, fmt.Errorf("runtime policy unavailable")
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		return Decision{}, fmt.Errorf("runtime policy refused request")
	}
	var result Decision
	if err := json.NewDecoder(io.LimitReader(response.Body, 2*1024*1024)).Decode(&result); err != nil {
		return Decision{}, err
	}
	if !result.Allowed {
		return result, fmt.Errorf("runtime action not allowed")
	}
	return result, nil
}

func Complete(ticket string, ids []string, sendErr error) {
	if ticket == "" || !Enabled() {
		return
	}
	status := "sent"
	if sendErr != nil || len(ids) == 0 {
		status = "unknown"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	_, _ = Call(ctx, "complete", map[string]any{"ticket": ticket, "status": status, "message_ids": ids})
}

type backgroundKey struct{}

func Background(ctx context.Context) context.Context {
	return context.WithValue(ctx, backgroundKey{}, true)
}
func IsBackground(ctx context.Context) bool {
	value, _ := ctx.Value(backgroundKey{}).(bool)
	return value
}

// TurnOutcome reports observable results only; it never forwards model/error text.
func TurnOutcome(result string, err error) string {
	if errors.Is(err, context.Canceled) {
		return "interrupted"
	}
	if err != nil {
		return "error"
	}
	if text := strings.TrimSpace(result); text == "NO_REPLY" || text == "[NO_REPLY]" {
		return "no_reply"
	}
	return "completed"
}

// TraceTurn is a bounded, best-effort local receipt, independent of user/tool permissions.
func TraceTurn(chatID, messageID, state string) {
	if !Enabled() || chatID == "" || messageID == "" {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	_, _ = Call(ctx, "turn_trace", map[string]any{"chat_id": chatID, "message_id": messageID, "state": state})
}
